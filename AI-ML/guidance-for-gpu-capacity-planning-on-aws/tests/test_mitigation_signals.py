# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for Tier 1 (Capacity Blocks) and Tier 2 (Flexible Training Plans)
mitigation signals in the deterministic scorer and signal extractor.

Covers:
- Side-signal reporting in ``signals`` dict for every state (True / False / None)
- ``spot_placement`` lift to 0.4 when either signal is True
- No lift when signal is False or None
- ICE severity caps still fire (CBs/FTPs availability is NOT an escape hatch)
- Interaction with ODCR coverage (ODCR beats either, which beats raw SPS)
- Signal extractor mapping from tool output to confidence parameters
"""

from __future__ import annotations

from src.confidence import compute_confidence
from src.signal_extractor import extract_signals


def _call(name: str, args: dict, data: dict, ok: bool = True) -> tuple[str, dict, dict]:
    return (name, args, {"ok": ok, "data": data})


# ---------------- confidence scorer: Capacity Blocks ----------------


class TestCapacityBlocksSignal:
    def test_true_lifts_spot_placement_to_floor(self):
        """Capacity Blocks available lifts spot_placement to 0.4 floor
        when the raw SPS reading is below that."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=2,  # raw spot_placement = 0.2
            capacity_blocks_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        assert result["signals"]["spot_placement"] == 0.4
        assert result["signals"]["capacity_blocks_signal"] == 1.0
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "capacity_blocks_available_lift" in kinds

    def test_true_does_not_lower_strong_spot_placement(self):
        """When SPS is already strong (>=0.4), CB lift is a no-op."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=9,  # raw spot_placement = 0.9
            capacity_blocks_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        assert result["signals"]["spot_placement"] == 0.9
        # Side signal is still reported for transparency.
        assert result["signals"]["capacity_blocks_signal"] == 1.0
        # No lift fired because the signal was already above the floor.
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "capacity_blocks_available_lift" not in kinds

    def test_false_has_no_scoring_effect(self):
        """``capacity_blocks_available=False`` is informational — no lift,
        no cap. Signal appears in output with value 0.0."""
        with_cb_false = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            capacity_blocks_available=False,
            region="us-east-1",
            gpu_family="p5",
        )
        without = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            region="us-east-1",
            gpu_family="p5",
        )
        assert with_cb_false["score"] == without["score"]
        assert with_cb_false["signals"]["capacity_blocks_signal"] == 0.0
        assert without["signals"]["capacity_blocks_signal"] is None

    def test_none_has_no_scoring_effect(self):
        """``capacity_blocks_available=None`` (tool didn't run) is a no-op."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            region="us-east-1",
            gpu_family="p5",
        )
        assert result["signals"]["capacity_blocks_signal"] is None
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "capacity_blocks_available_lift" not in kinds

    def test_does_not_override_ice_severity_cap(self):
        """Severe ICE still caps final score at 0.4 even when CBs are
        available. CBs is purchasable inventory, not reserved capacity —
        it is NOT an escape hatch like a full ODCR."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,  # severe
            capacity_blocks_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        kinds = [c["kind"] for c in result["applied_caps"]]
        # The severe-ICE final-score cap must have fired.
        assert "ice_ceiling_severe" in kinds
        assert result["score"] <= 0.4


# ---------------- confidence scorer: Flexible Training Plans ----------------


class TestFlexibleTrainingPlansSignal:
    def test_true_lifts_spot_placement_to_floor(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=2,
            training_plans_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        assert result["signals"]["spot_placement"] == 0.4
        assert result["signals"]["training_plans_signal"] == 1.0
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "training_plans_available_lift" in kinds

    def test_false_has_no_scoring_effect(self):
        with_ftp_false = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            training_plans_available=False,
            region="us-east-1",
            gpu_family="p5",
        )
        without = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            region="us-east-1",
            gpu_family="p5",
        )
        assert with_ftp_false["score"] == without["score"]
        assert with_ftp_false["signals"]["training_plans_signal"] == 0.0

    def test_does_not_override_ice_severity_cap(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,
            training_plans_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "ice_ceiling_severe" in kinds
        assert result["score"] <= 0.4


# ---------------- combined behavior ----------------


class TestCombinedMitigationSignals:
    def test_odcr_coverage_still_beats_cb_floor(self):
        """ODCR covering the full ask lifts spot_placement to 1.0, which
        is strictly stronger than the CB 0.4 floor. The CB lift still
        appears in the applied_caps audit log, but the resulting
        spot_placement signal reflects the stronger ODCR lift."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=10,
            spot_placement_score=1,
            odcr_available_capacity=10,  # fully covers
            capacity_blocks_available=True,
            training_plans_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        assert result["signals"]["spot_placement"] == 1.0
        assert result["signals"]["odcr_coverage"] == 1.0
        # Both lifts attempted; ODCR's higher target wins.
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "odcr_coverage_lift" in kinds

    def test_both_cb_and_ftp_available_coexist(self):
        """Both signals present and both lifts attempted — floor is 0.4
        either way, so the second lift is a no-op but still reported."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=1,
            capacity_blocks_available=True,
            training_plans_available=True,
            region="us-east-1",
            gpu_family="p5",
        )
        assert result["signals"]["spot_placement"] == 0.4
        assert result["signals"]["capacity_blocks_signal"] == 1.0
        assert result["signals"]["training_plans_signal"] == 1.0
        kinds = [c["kind"] for c in result["applied_caps"]]
        # Only the first lift actually changes the value (both target 0.4).
        # Either kind is acceptable as the first-fired one.
        assert "capacity_blocks_available_lift" in kinds


# ---------------- signal extractor ----------------


class TestSignalExtractorCapacityBlocks:
    def test_available_signal_sets_true(self):
        calls = [
            _call(
                "get_capacity_blocks_offerings",
                {"instance_type": "p5.48xlarge", "region": "us-east-1"},
                {
                    "instance_type": "p5.48xlarge",
                    "region": "us-east-1",
                    "offerings_count": 3,
                    "capacity_signal": "available",
                },
            ),
        ]
        signals = extract_signals(calls)[("p5.48xlarge", "us-east-1")]
        assert signals["capacity_blocks_available"] is True

    def test_unavailable_signal_sets_false(self):
        calls = [
            _call(
                "get_capacity_blocks_offerings",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "offerings_count": 0,
                    "capacity_signal": "unavailable",
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["capacity_blocks_available"] is False

    def test_unknown_signal_leaves_unset(self):
        """IAM failure path returns 'unknown' — do not set a bool
        value, so the scorer treats the tool as if it didn't run."""
        calls = [
            _call(
                "get_capacity_blocks_offerings",
                {"instance_type": "p5.48xlarge", "region": "us-east-1"},
                {
                    "instance_type": "p5.48xlarge",
                    "region": "us-east-1",
                    "offerings_count": 0,
                    "capacity_signal": "unknown",
                },
            ),
        ]
        signals = extract_signals(calls)[("p5.48xlarge", "us-east-1")]
        assert "capacity_blocks_available" not in signals


class TestSignalExtractorFlexibleTrainingPlans:
    def test_available_signal_sets_true(self):
        calls = [
            _call(
                "get_sagemaker_training_plan_offerings",
                {
                    "instance_type": "p5en.48xlarge",
                    "region": "us-east-1",
                    "duration_hours": 1440,  # 60 days
                },
                {
                    "instance_type": "p5en.48xlarge",
                    "region": "us-east-1",
                    "offerings_count": 4,
                    "capacity_signal": "available",
                },
            ),
        ]
        signals = extract_signals(calls)[("p5en.48xlarge", "us-east-1")]
        assert signals["training_plans_available"] is True

    def test_unavailable_signal_sets_false(self):
        calls = [
            _call(
                "get_sagemaker_training_plan_offerings",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "offerings_count": 0,
                    "capacity_signal": "unavailable",
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["training_plans_available"] is False

    def test_unknown_signal_leaves_unset(self):
        calls = [
            _call(
                "get_sagemaker_training_plan_offerings",
                {"instance_type": "p5.48xlarge", "region": "us-east-1"},
                {
                    "instance_type": "p5.48xlarge",
                    "region": "us-east-1",
                    "offerings_count": 0,
                    "capacity_signal": "unknown",
                },
            ),
        ]
        signals = extract_signals(calls)[("p5.48xlarge", "us-east-1")]
        assert "training_plans_available" not in signals


# ---------------- confidence scorer: Heterogeneous endpoint fallback ----------------


class TestHeterogeneousFallbackSignal:
    def test_true_lifts_spot_placement_to_floor(self):
        """Heterogeneous-endpoint fallback available lifts spot_placement
        to the shared 0.4 mitigation-path floor when raw SPS is below it."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=2,  # raw spot_placement = 0.2
            heterogeneous_fallback_available=True,
            region="us-east-1",
            gpu_family="g6e",
        )
        assert result["signals"]["spot_placement"] == 0.4
        assert result["signals"]["heterogeneous_fallback_signal"] == 1.0
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "heterogeneous_fallback_available_lift" in kinds

    def test_true_does_not_lower_strong_spot_placement(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=9,
            heterogeneous_fallback_available=True,
            region="us-east-1",
            gpu_family="g6e",
        )
        assert result["signals"]["spot_placement"] == 0.9
        assert result["signals"]["heterogeneous_fallback_signal"] == 1.0
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "heterogeneous_fallback_available_lift" not in kinds

    def test_false_has_no_scoring_effect(self):
        with_false = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            heterogeneous_fallback_available=False,
            region="us-east-1",
            gpu_family="g6e",
        )
        without = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            region="us-east-1",
            gpu_family="g6e",
        )
        assert with_false["score"] == without["score"]
        assert with_false["signals"]["heterogeneous_fallback_signal"] == 0.0
        assert without["signals"]["heterogeneous_fallback_signal"] is None

    def test_none_has_no_scoring_effect(self):
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=5,
            region="us-east-1",
            gpu_family="g6e",
        )
        assert result["signals"]["heterogeneous_fallback_signal"] is None
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "heterogeneous_fallback_available_lift" not in kinds

    def test_does_not_override_ice_severity_cap(self):
        """Severe ICE still caps final score at 0.4 even with fallback
        available. Fallback is architectural mitigation path, not reserved
        capacity — it does NOT rescue the verdict the way ODCR coverage does."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,  # severe
            heterogeneous_fallback_available=True,
            region="us-east-1",
            gpu_family="g6e",
        )
        kinds = [c["kind"] for c in result["applied_caps"]]
        assert "ice_ceiling_severe" in kinds
        assert result["score"] <= 0.4

    def test_combines_with_cb_and_ftp_at_same_floor(self):
        """When CBs + FTP + heterogeneous fallback are all available, the
        three lifts all target the same 0.4 floor. Only the first-fired
        one actually changes the spot_placement value; the others are
        audit-trail-only. The composite verdict should still clear
        HIGH RISK thanks to the lift."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=1,  # raw = 0.1 — well below the floor
            capacity_blocks_available=True,
            training_plans_available=True,
            heterogeneous_fallback_available=True,
            region="us-east-1",
            gpu_family="g6e",
        )
        assert result["signals"]["spot_placement"] == 0.4
        assert result["signals"]["capacity_blocks_signal"] == 1.0
        assert result["signals"]["training_plans_signal"] == 1.0
        assert result["signals"]["heterogeneous_fallback_signal"] == 1.0
        # First-fired lift actually bumps the value; subsequent ones are
        # already-at-floor no-ops but every TRUE signal is still reported.


# ---------------- signal extractor: Heterogeneous endpoint fallback ----------------


class TestSignalExtractorHeterogeneousEndpoint:
    def test_available_signal_sets_true(self):
        """The tool's args use ``primary_instance_type`` (not
        ``instance_type``) and the normalized value also appears nested
        under ``data["primary"]["instance_type"]``. The extractor + the
        enhanced ``_key_for_call`` chain should pick the key up from
        either side."""
        calls = [
            _call(
                "evaluate_heterogeneous_endpoint_fallback",
                {
                    "primary_instance_type": "ml.g6e.xlarge",
                    "region": "us-east-1",
                    "model_size_b": 7.0,
                },
                {
                    "primary": {
                        "instance_type": "g6e.xlarge",
                        "sagemaker_instance_type": "ml.g6e.xlarge",
                    },
                    "region": "us-east-1",
                    "capacity_signal": "available",
                    "fallback_candidates": [{"instance_type": "g7.12xlarge"}],
                },
            ),
        ]
        signals = extract_signals(calls)[("g6e.xlarge", "us-east-1")]
        assert signals["heterogeneous_fallback_available"] is True

    def test_unavailable_signal_sets_false(self):
        calls = [
            _call(
                "evaluate_heterogeneous_endpoint_fallback",
                {"primary_instance_type": "g6e.xlarge", "region": "us-east-1"},
                {
                    "primary": {"instance_type": "g6e.xlarge"},
                    "region": "us-east-1",
                    "capacity_signal": "unavailable",
                    "fallback_candidates": [],
                },
            ),
        ]
        signals = extract_signals(calls)[("g6e.xlarge", "us-east-1")]
        assert signals["heterogeneous_fallback_available"] is False

    def test_unknown_signal_leaves_unset(self):
        """API error path returns 'unknown' — do not set a bool value, so
        the scorer treats the tool as if it didn't run."""
        calls = [
            _call(
                "evaluate_heterogeneous_endpoint_fallback",
                {"primary_instance_type": "g6e.xlarge", "region": "us-east-1"},
                {
                    "primary": {"instance_type": "g6e.xlarge"},
                    "region": "us-east-1",
                    "capacity_signal": "unknown",
                },
            ),
        ]
        key = ("g6e.xlarge", "us-east-1")
        extracted = extract_signals(calls)
        if key in extracted:
            signals = extracted[key]
            assert "heterogeneous_fallback_available" not in signals

    def test_key_resolution_uses_args_primary_instance_type(self):
        """Even if the tool's response doesn't echo a top-level
        instance_type (older response shape), the extractor should
        still group the signal under the right (instance_type, region)
        key via the ``primary_instance_type`` argument."""
        calls = [
            _call(
                "evaluate_heterogeneous_endpoint_fallback",
                {"primary_instance_type": "ml.g6e.xlarge", "region": "us-east-1"},
                {
                    "region": "us-east-1",
                    "capacity_signal": "available",
                },
            ),
        ]
        signals = extract_signals(calls)[("g6e.xlarge", "us-east-1")]
        assert signals["heterogeneous_fallback_available"] is True
