# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guards: the deterministic scorer is auditable.

Every ``compute_confidence`` return dict MUST let a downstream consumer
reproduce the final ``score`` from ``signals``, ``weights``, ``base_score``,
and ``applied_caps``. If any of these fields disappears or an adjustment
fires without a matching entry in ``applied_caps``, the promised
"auditable" quality of the score breaks.

These tests pin the contract with concrete inputs, so any refactor of
``confidence.py`` that silently changes what's published trips CI.
"""

from __future__ import annotations

from src.confidence import compute_confidence


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------


class TestReturnShape:
    def test_required_keys_present(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            region="us-east-1",
            gpu_family="g5",
        )
        for key in (
            "score",
            "label",
            "verdict",
            "signals",
            "weights",
            "base_score",
            "applied_caps",
        ):
            assert key in result, f"missing key {key!r} in compute_confidence output"

    def test_applied_caps_is_list_of_records(self):
        # Any invocation that triggers no caps still returns a list (not None).
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            region="us-east-1",
            gpu_family="g5",
        )
        assert isinstance(result["applied_caps"], list)
        for entry in result["applied_caps"]:
            assert set(entry.keys()) >= {
                "kind",
                "target",
                "from_value",
                "to_value",
                "description",
            }
            assert isinstance(entry["kind"], str) and entry["kind"]
            assert isinstance(entry["target"], str) and entry["target"]
            assert isinstance(entry["from_value"], (int, float))
            assert isinstance(entry["to_value"], (int, float))
            # A cap must be a real change — from != to.
            assert entry["from_value"] != entry["to_value"]

    def test_weights_sum_to_one(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            region="us-east-1",
            gpu_family="g5",
        )
        total = sum(result["weights"].values())
        assert abs(total - 1.0) < 1e-9, f"weights must sum to 1.0, got {total}"


# ---------------------------------------------------------------------------
# Reproducibility invariants
# ---------------------------------------------------------------------------


class TestBaseScoreReproducibility:
    """``base_score`` must equal ``sum(signals × weights)`` — i.e. a consumer
    with only ``signals`` and ``weights`` in hand must be able to reproduce
    the pre-cap score exactly.
    """

    def test_base_score_matches_weighted_signals_no_caps(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            region="us-east-1",
            gpu_family="g5",
        )
        signals = result["signals"]
        weights = result["weights"]
        expected = round(sum(signals[k] * weights[k] for k in weights), 4)
        assert result["base_score"] == expected

    def test_base_score_reproducible_when_ice_cap_fires(self):
        # Severe ICE caps spot_placement to 0.1 — the signals dict reflects
        # the post-cap value, so base_score must be computed from that.
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=10,
            spot_placement_score=9,  # would have been 0.9
            customer_ice_events=15,  # severe → caps spot_placement to 0.1
            region="us-east-1",
            gpu_family="g5",
        )
        signals = result["signals"]
        weights = result["weights"]
        assert signals["spot_placement"] == 0.1, (
            f"severe ICE must cap spot_placement to 0.1, got {signals['spot_placement']}"
        )
        expected = round(sum(signals[k] * weights[k] for k in weights), 4)
        assert result["base_score"] == expected

    def test_base_score_reproducible_when_odcr_lift_fires(self):
        # ODCR fully covers a low SPS pool — signals["spot_placement"] is
        # lifted upward. base_score must equal signals × weights against the
        # post-lift value.
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=2,  # weak pool
            odcr_available_capacity=20,  # fully covers the ask → lift to 1.0
            region="us-east-1",
            gpu_family="g5",
        )
        signals = result["signals"]
        weights = result["weights"]
        assert signals["spot_placement"] >= 0.9  # lifted from 0.2 to 1.0
        expected = round(sum(signals[k] * weights[k] for k in weights), 4)
        assert result["base_score"] == expected


class TestScoreReproducibilityViaAppliedCaps:
    """The final ``score`` must equal ``base_score`` when no caps fire, and
    when caps DO fire the final score must match the last cap's
    ``to_value`` (after clamping/rounding).
    """

    def test_no_caps_final_score_equals_base(self):
        # Must supply a clean ICE observation, otherwise F4's
        # ice_signal_unavailable cap fires.
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            customer_ice_events=0,
            region="us-east-1",
            gpu_family="g5",
        )
        # Filter out ICE / ODCR signal-level caps (those change signals but
        # not the final score directly); the invariant is about final-score
        # caps only.
        final_score_caps = [c for c in result["applied_caps"] if c["target"] == "score"]
        assert final_score_caps == [], (
            f"unexpected final-score cap fired: {final_score_caps}"
        )
        # Final score should equal base_score rounded to 2 dp.
        assert result["score"] == round(result["base_score"], 2)

    def test_unavailable_instance_hard_gate_recorded(self):
        result = compute_confidence(
            instance_available=False,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            region="us-east-1",
            gpu_family="g5",
        )
        # Score capped at 0.1
        assert result["score"] <= 0.1
        # And there must be an applied_caps entry that documents WHY.
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "hard_gate_unavailable" in kinds
        # Verdict follows.
        assert result["verdict"] == "NOT FEASIBLE"

    def test_not_priced_hard_gate_recorded(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=False,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=7,
            region="us-east-1",
            gpu_family="g5",
        )
        assert result["score"] <= 0.2
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "hard_gate_not_ga" in kinds
        assert result["verdict"] == "NOT FEASIBLE"

    def test_severe_ice_ceiling_recorded_and_capped_score(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=10,
            spot_placement_score=10,  # would score high without ICE
            customer_ice_events=25,  # severe
            odcr_available_capacity=0,  # no ODCR rescue
            region="us-east-1",
            gpu_family="g5",
        )
        # Score is capped at 0.4 (HIGH RISK ceiling)
        assert result["score"] <= 0.4
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_history_severe" in kinds  # signal-level cap
        assert "ice_ceiling_severe" in kinds  # final-score cap

    def test_recurring_ice_ceiling_recorded(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=10,
            spot_placement_score=10,
            customer_ice_events=7,  # recurring (4-10)
            odcr_available_capacity=0,
            region="us-east-1",
            gpu_family="g5",
        )
        assert result["score"] <= 0.65
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_history_recurring" in kinds
        assert "ice_ceiling_recurring" in kinds

    def test_full_odcr_coverage_defeats_severe_ice_ceiling(self):
        """ODCR that fully covers the ask must let the score reflect the
        guaranteed capacity even when severe ICE is present on the shared
        pool. Verify no ice_ceiling_severe cap fires.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=10,
            spot_placement_score=3,  # weak shared pool
            customer_ice_events=25,  # severe ICE
            odcr_available_capacity=10,  # fully covers the ask
            region="us-east-1",
            gpu_family="g5",
        )
        kinds = {c["kind"] for c in result["applied_caps"]}
        # The ICE signal-level cap still fires…
        assert "ice_history_severe" in kinds
        # …but the FINAL-score ceiling does NOT because ODCR fully covers.
        assert "ice_ceiling_severe" not in kinds
        # And ODCR lift is recorded.
        assert "odcr_coverage_lift" in kinds


# ---------------------------------------------------------------------------
# Applied-cap targets validate against known targets only
# ---------------------------------------------------------------------------


class TestAppliedCapTargetValidity:
    """Every ``applied_caps[].target`` must be either a known signal name
    (prefixed ``signals.``) or the literal ``score``. Reviewers reading the
    audit trail rely on these being stable.
    """

    def test_targets_are_well_formed(self):
        known_signals = {
            "availability",
            "pricing",
            "quota",
            "spot_placement",
            "region_maturity",
        }
        for ice in (0, 2, 5, 15):
            for odcr in (0, 5, 20):
                result = compute_confidence(
                    instance_available=True,
                    pricing_available=True,
                    quota_limit=100,
                    requested_count=10,
                    spot_placement_score=5,
                    customer_ice_events=ice,
                    odcr_available_capacity=odcr,
                    region="us-east-1",
                    gpu_family="g5",
                )
                for cap in result["applied_caps"]:
                    target = cap["target"]
                    if target == "score":
                        continue
                    assert target.startswith("signals."), (
                        f"applied_caps target must be signals.* or score, got {target!r}"
                    )
                    signal_name = target.split(".", 1)[1]
                    assert signal_name in known_signals, (
                        f"unknown applied_caps signal target: {signal_name!r}"
                    )
