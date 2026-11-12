# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for deterministic confidence scoring."""

from src.confidence import compute_confidence


class TestComputeConfidence:
    """Tests for the compute_confidence function."""

    def test_all_signals_positive_high_confidence(self):
        """All green signals → HIGH confidence, DEPLOYABLE.

        F4 requires ICE history to be observed (not skipped) before
        the scorer will return DEPLOYABLE. ``customer_ice_events=0`` is
        the "queried and clean" case.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=50,
            requested_count=20,
            spot_placement_score=9,
            customer_ice_events=0,
            region="us-east-1",
        )
        assert result["label"] == "HIGH"
        assert result["verdict"] == "DEPLOYABLE"
        assert result["score"] >= 0.8

    def test_instance_not_available_caps_score(self):
        """Instance not offered → NOT FEASIBLE regardless of other signals."""
        result = compute_confidence(
            instance_available=False,
            pricing_available=False,
            quota_limit=100,
            requested_count=10,
            spot_placement_score=10,
            region="us-east-1",
        )
        assert result["verdict"] == "NOT FEASIBLE"
        assert result["score"] <= 0.1

    def test_pricing_not_available_caps_score(self):
        """No pricing (not GA) → NOT FEASIBLE."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=False,
            quota_limit=50,
            requested_count=10,
            spot_placement_score=8,
            region="us-east-1",
        )
        assert result["verdict"] == "NOT FEASIBLE"
        assert result["score"] <= 0.2

    def test_quota_far_below_request_lowers_confidence(self):
        """Quota 2 vs requested 175 → low quota signal."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=2,
            requested_count=175,
            spot_placement_score=4,
            region="sa-east-1",
        )
        # Quota ratio: 2/175 ≈ 0.011, very low
        assert result["signals"]["quota"] < 0.02
        # Overall: availability + pricing strong, but quota + SPS + region drag it down
        assert result["score"] < 0.7
        assert result["verdict"] in ("DEPLOYABLE WITH PREPARATION", "HIGH RISK")

    def test_limited_region_lowers_confidence(self):
        """af-south-1 (limited) vs us-east-1 (established), holding ICE clean."""
        limited = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=6,
            customer_ice_events=0,
            region="af-south-1",
        )
        established = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=20,
            requested_count=20,
            spot_placement_score=6,
            customer_ice_events=0,
            region="us-east-1",
        )
        assert limited["score"] < established["score"]
        assert limited["signals"]["region_maturity"] == 0.3
        assert established["signals"]["region_maturity"] == 1.0

    def test_low_spot_placement_score_lowers_confidence(self):
        """SPS 2/10 → weak capacity signal, lowers overall score vs SPS 9."""
        low_sps = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=50,
            requested_count=20,
            spot_placement_score=2,
            region="us-east-1",
        )
        high_sps = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=50,
            requested_count=20,
            spot_placement_score=9,
            region="us-east-1",
        )
        assert low_sps["signals"]["spot_placement"] == 0.2
        assert high_sps["signals"]["spot_placement"] == 0.9
        # Low SPS should produce lower overall score than high SPS
        assert low_sps["score"] < high_sps["score"]

    def test_unknown_signals_use_neutral_value(self):
        """None/unknown signals default to 0.5 (neutral)."""
        result = compute_confidence(
            instance_available=None,
            pricing_available=None,
            quota_limit=None,
            requested_count=10,
            spot_placement_score=None,
            region="",
        )
        assert result["signals"]["availability"] == 0.5
        assert result["signals"]["pricing"] == 0.5
        assert result["signals"]["quota"] == 0.5
        assert result["signals"]["spot_placement"] == 0.5

    def test_score_is_deterministic(self):
        """Same inputs always produce same score."""
        kwargs = dict(
            instance_available=True,
            pricing_available=True,
            quota_limit=10,
            requested_count=22,
            spot_placement_score=5,
            region="sa-east-1",
        )
        r1 = compute_confidence(**kwargs)
        r2 = compute_confidence(**kwargs)
        assert r1["score"] == r2["score"]
        assert r1["label"] == r2["label"]
        assert r1["verdict"] == r2["verdict"]

    def test_score_clamped_between_0_and_1(self):
        """Score is always in [0, 1]."""
        # Best case
        best = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=1000,
            requested_count=1,
            spot_placement_score=10,
            region="us-east-1",
        )
        assert 0.0 <= best["score"] <= 1.0

        # Worst case
        worst = compute_confidence(
            instance_available=False,
            pricing_available=False,
            quota_limit=0,
            requested_count=500,
            spot_placement_score=1,
            region="af-south-1",
        )
        assert 0.0 <= worst["score"] <= 1.0

    def test_verdict_mapping(self):
        """Verify label-to-verdict mapping is consistent."""
        # HIGH → DEPLOYABLE (with clean ICE observation)
        high = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            customer_ice_events=0,
            region="us-east-1",
        )
        assert high["verdict"] == "DEPLOYABLE"

        # NOT FEASIBLE
        blocked = compute_confidence(
            instance_available=False,
            pricing_available=False,
            quota_limit=0,
            requested_count=100,
            spot_placement_score=1,
            region="af-south-1",
        )
        assert blocked["verdict"] == "NOT FEASIBLE"

    def test_missing_ice_signal_caps_at_deployable_with_preparation(self):
        """regression guard: skipping the ICE query CANNOT return
        DEPLOYABLE. The scorer treats missing ICE as inconclusive and
        caps the verdict at DEPLOYABLE WITH PREPARATION until the
        signal is obtained.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            # NO customer_ice_events supplied.
            region="us-east-1",
        )
        assert result["verdict"] == "DEPLOYABLE WITH PREPARATION"
        assert result["score"] <= 0.65
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_signal_unavailable" in kinds

    def test_rate_limited_ice_signal_is_not_clean(self):
        """regression guard: ``rate_limited`` from the ICE tool
        must be treated the same as 'not queried' — cap at DEPLOYABLE
        WITH PREPARATION, do NOT return DEPLOYABLE.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            customer_ice_events=None,
            customer_ice_capacity_signal="rate_limited",
            region="us-east-1",
        )
        assert result["verdict"] == "DEPLOYABLE WITH PREPARATION"
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_signal_unavailable" in kinds

    def test_metric_filter_not_deployed_ice_signal_is_not_clean(self):
        """regression guard: when ``get_ice_metrics`` returns
        ``metric_filter_not_deployed`` and no ``get_ice_history``
        follow-up rescued the signal, the scorer MUST refuse
        DEPLOYABLE and MUST cite the reason in the applied_caps audit
        trail. Without this branch the customer would get a false
        clean reading whenever they hadn't yet deployed the companion
        metric-filter stack.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            customer_ice_events=None,
            customer_ice_capacity_signal="metric_filter_not_deployed",
            region="us-east-1",
        )
        assert result["verdict"] == "DEPLOYABLE WITH PREPARATION"
        assert result["score"] <= 0.65
        caps = result["applied_caps"]
        kinds = {c["kind"] for c in caps}
        assert "ice_signal_unavailable" in kinds
        # The audit-trail description must call out the missing
        # companion stack so the customer knows what to fix.
        applied = next(c for c in caps if c["kind"] == "ice_signal_unavailable")
        assert (
            "customer-side-ice-metric-filter.yaml" in applied["description"]
            or "companion" in applied["description"]
        )

    def test_odcr_full_coverage_defeats_missing_ice_cap(self):
        """The 'ice signal unavailable' cap does NOT fire when an ODCR
        fully covers the ask — reserved capacity makes the shared-pool
        history irrelevant.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            odcr_available_capacity=5,
            # NO ICE query.
            region="us-east-1",
        )
        assert result["verdict"] == "DEPLOYABLE"
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_signal_unavailable" not in kinds

    def test_critical_sps_caps_score_at_high_risk(self):
        """regression guard: SPS ≤ 3 without ODCR cover forces
        HIGH RISK regardless of other signals. Before this cap, a lone
        SPS=2 barely moved the score — customers could land DEPLOYABLE
        on a shallow pool.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=2,
            customer_ice_events=0,
            region="us-east-1",
        )
        assert result["verdict"] == "HIGH RISK"
        assert result["score"] <= 0.4
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "sps_ceiling_critical" in kinds

    def test_odcr_full_coverage_defeats_critical_sps_cap(self):
        """When an ODCR fully covers the ask, the SPS-critical cap must
        NOT fire — reserved capacity is unaffected by shared-pool depth.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=2,
            odcr_available_capacity=5,
            customer_ice_events=0,
            region="us-east-1",
        )
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "sps_ceiling_critical" not in kinds


class TestCustomerICEHistory:
    """Tests for the customer_ice_events side signal."""

    def test_zero_ice_events_no_cap_full_signal(self):
        """0 ICE events → signal 1.0, no penalty to spot_placement."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=9,
            customer_ice_events=0,
            region="us-east-1",
        )
        assert result["signals"]["ice_history_signal"] == 1.0
        # No cap applied — spot_placement stays at 0.9
        assert result["signals"]["spot_placement"] == 0.9

    def test_isolated_ice_caps_spot_at_06(self):
        """1-3 ICE events → cap spot_placement at 0.6."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,  # would be 1.0
            customer_ice_events=2,
            region="us-east-1",
        )
        assert result["signals"]["ice_history_signal"] == 0.7
        assert result["signals"]["spot_placement"] == 0.6  # capped

    def test_recurring_ice_caps_spot_at_03(self):
        """4-10 ICE events → cap at 0.3."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=9,
            customer_ice_events=7,
            region="us-east-1",
        )
        assert result["signals"]["ice_history_signal"] == 0.3
        assert result["signals"]["spot_placement"] == 0.3  # was 0.9, capped to 0.3

    def test_severe_ice_caps_spot_at_01(self):
        """11+ ICE events → cap at 0.1; SPS irrelevant."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,
            region="us-east-1",
        )
        assert result["signals"]["ice_history_signal"] == 0.0
        assert result["signals"]["spot_placement"] == 0.1

    def test_ice_does_not_raise_a_lower_spot_score(self):
        """ICE cap is a ceiling, not a floor — weak SPS stays weak."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=1,  # → 0.1
            customer_ice_events=2,  # cap at 0.6 (above 0.1)
            region="us-east-1",
        )
        # min(0.1, 0.6) = 0.1
        assert result["signals"]["spot_placement"] == 0.1

    def test_unknown_ice_signal_is_none(self):
        """When customer_ice_events is not provided, signal renders as None."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=8,
            region="us-east-1",
        )
        assert result["signals"]["ice_history_signal"] is None
        # No cap applied
        assert result["signals"]["spot_placement"] == 0.8

    def test_ice_then_odcr_lifts_back(self):
        """Severe ICE caps spot at 0.1, then ODCR coverage lifts back up."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,  # caps spot to 0.1
            odcr_available_capacity=20,  # coverage 1.0, lifts spot to 1.0
            region="us-east-1",
        )
        assert result["signals"]["ice_history_signal"] == 0.0
        assert result["signals"]["odcr_coverage"] == 1.0
        # ICE caps to 0.1, then ODCR lifts to coverage=1.0
        assert result["signals"]["spot_placement"] == 1.0
        # And full ODCR coverage defeats the ICE hard cap → verdict stays DEPLOYABLE
        assert result["verdict"] == "DEPLOYABLE"

    def test_severe_ice_without_odcr_caps_at_high_risk(self):
        """Severe ICE + no ODCR → score forced down to HIGH RISK ceiling."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,
            region="us-east-1",
        )
        assert result["score"] <= 0.4
        assert result["verdict"] == "HIGH RISK"

    def test_recurring_ice_without_odcr_caps_at_dwp(self):
        """Recurring ICE + no ODCR → score forced to DEPLOYABLE WITH PREPARATION ceiling."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=7,
            region="us-east-1",
        )
        assert result["score"] <= 0.65
        assert result["verdict"] == "DEPLOYABLE WITH PREPARATION"

    def test_partial_odcr_does_not_defeat_ice_hard_cap(self):
        """Partial ODCR (covers < requested) does NOT defeat the ICE hard cap."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=10,
            customer_ice_events=25,  # severe
            odcr_available_capacity=10,  # only half-covers
            region="us-east-1",
        )
        # Hard cap still applies because ODCR doesn't fully cover
        assert result["score"] <= 0.4
        assert result["verdict"] == "HIGH RISK"

    def test_recurring_ice_lowers_overall_score(self):
        """Recurring ICE materially drops the score compared to a clean
        (queried-and-zero) observation. Both must still land at
        DEPLOYABLE WITH PREPARATION or below under the new post-F4
        contract (a real observation of 7 events plus its ceiling cap
        lands at 0.65; a clean observation of 0 events would land higher).
        """
        with_ice = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=9,
            customer_ice_events=7,
            region="us-east-1",
        )
        clean_ice = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=20,
            spot_placement_score=9,
            customer_ice_events=0,
            region="us-east-1",
        )
        assert with_ice["score"] < clean_ice["score"]
        # Recurring ICE in established region should drop to MODERATE or below
        assert with_ice["verdict"] in (
            "DEPLOYABLE WITH PREPARATION",
            "HIGH RISK",
            "NOT FEASIBLE",
        )
        # Clean observation should let it land DEPLOYABLE.
        assert clean_ice["verdict"] == "DEPLOYABLE"
