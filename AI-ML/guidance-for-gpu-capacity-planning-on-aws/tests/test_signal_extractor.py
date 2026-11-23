# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the signal extractor that drives deterministic confidence scoring."""

from __future__ import annotations

from src.signal_extractor import (
    _gpu_family,
    _normalize_type,
    assess_targets,
    extract_signals,
)


def _call(name: str, args: dict, data: dict, ok: bool = True) -> tuple[str, dict, dict]:
    """Build a tool_call tuple in the shape agent.run() produces."""
    return (name, args, {"ok": ok, "data": data})


class TestNormalization:
    def test_strips_ml_prefix(self):
        assert _normalize_type("ml.g5.2xlarge") == "g5.2xlarge"

    def test_lowercases(self):
        assert _normalize_type("ML.G5.2xlarge") == "g5.2xlarge"

    def test_empty_returns_none(self):
        assert _normalize_type("") is None
        assert _normalize_type(None) is None
        assert _normalize_type(42) is None

    def test_gpu_family_from_instance_type(self):
        assert _gpu_family("g5.2xlarge") == "g5"
        assert _gpu_family("p4d.24xlarge") == "p4d"
        assert _gpu_family("inf2.xlarge") == "inf2"

    def test_gpu_family_handles_no_dot(self):
        assert _gpu_family("g5") == "g5"


class TestExtractSignals:
    def test_no_tool_calls_returns_empty(self):
        assert extract_signals([]) == {}

    def test_none_input_returns_empty(self):
        assert extract_signals(None) == {}

    def test_skips_failed_tool_calls(self):
        calls = [
            (
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {"ok": False, "error": "boom"},
            ),
        ]
        assert extract_signals(calls) == {}

    def test_skips_unknown_tools(self):
        calls = [
            _call(
                "find_alternative_instances",
                {"instance_type": "g7e.2xlarge", "region": "sa-east-1"},
                {"alternatives": [{"instance_type": "g5.2xlarge"}]},
            ),
        ]
        # find_alternative_instances is not in the extractor map → ignored
        assert extract_signals(calls) == {}

    def test_skips_calls_missing_instance_type_or_region(self):
        calls = [
            _call("check_instance_availability", {}, {"available": True}),
        ]
        assert extract_signals(calls) == {}

    def test_groups_by_instance_and_region(self):
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "ml.g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                    "hourly_usd": 1.52,
                },
            ),
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "current_limit": 50,
                },
            ),
        ]
        grouped = extract_signals(calls)
        # Both ml.g5.2xlarge and g5.2xlarge collapse to the same normalized key
        assert list(grouped.keys()) == [("g5.2xlarge", "us-east-1")]
        signals = grouped[("g5.2xlarge", "us-east-1")]
        assert signals["instance_available"] is True
        assert signals["pricing_available"] is True
        assert signals["quota_limit"] == 50

    def test_quota_not_found_becomes_zero(self):
        calls = [
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g6.2xlarge", "region": "af-south-1"},
                {
                    "instance_type": "ml.g6.2xlarge",
                    "region": "af-south-1",
                    "found": False,
                },
            ),
        ]
        signals = extract_signals(calls)[("g6.2xlarge", "af-south-1")]
        assert signals["quota_limit"] == 0

    def test_spot_score_and_requested_count_extracted(self):
        calls = [
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 22,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 22,
                    "score": 7,
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["spot_placement_score"] == 7
        assert signals["requested_count"] == 22

    def test_requested_count_keeps_max_across_calls(self):
        calls = [
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 22,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 22,
                    "score": 7,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 175,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 175,
                    "score": 3,
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["requested_count"] == 175

    def test_odcr_available_capacity_extracted(self):
        calls = [
            _call(
                "check_existing_odcrs",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available_capacity": 30,
                    "reservation_count": 2,
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["odcr_available_capacity"] == 30

    def test_multi_region_groups_separately(self):
        calls = [
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "eu-west-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "eu-west-1",
                    "available": True,
                },
            ),
        ]
        grouped = extract_signals(calls)
        assert set(grouped.keys()) == {
            ("g5.2xlarge", "us-east-1"),
            ("g5.2xlarge", "eu-west-1"),
        }


class TestAssessTargets:
    def test_empty_tool_calls_returns_empty(self):
        assert assess_targets([]) == []

    def test_produces_high_confidence_for_clean_signals(self):
        """g5 in us-east-1 with strong signals AND a clean ICE
        observation → HIGH / DEPLOYABLE.

        Per F4, the ICE query must have run and returned 0 events
        before the scorer can return DEPLOYABLE. Without it, the score
        caps at 0.65.
        """
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "current_limit": 100,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                    "score": 9,
                },
            ),
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 0,
                    "capacity_signal": "clean",
                    "deployment_target": "auto",
                },
            ),
        ]
        results = assess_targets(calls)
        assert len(results) == 1
        r = results[0]
        assert r["instance_type"] == "g5.2xlarge"
        assert r["region"] == "us-east-1"
        assert r["requested_count"] == 20
        assert r["confidence"]["label"] == "HIGH"
        assert r["confidence"]["verdict"] == "DEPLOYABLE"
        assert r["confidence"]["score"] >= 0.8

    def test_not_feasible_when_instance_unavailable(self):
        """g6 not offered in af-south-1 → NOT FEASIBLE regardless of other signals."""
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g6.2xlarge", "region": "af-south-1"},
                {
                    "instance_type": "g6.2xlarge",
                    "region": "af-south-1",
                    "available": False,
                    "azs": [],
                },
            ),
        ]
        results = assess_targets(calls)
        assert len(results) == 1
        assert results[0]["confidence"]["verdict"] == "NOT FEASIBLE"
        assert results[0]["confidence"]["score"] <= 0.1

    def test_odcr_coverage_lifts_capacity_signal(self):
        """ODCR with enough capacity boosts a weak spot placement score."""
        # Customer wants 20 g5s in us-east-1. Spot score is weak (3) but they
        # already have an ODCR with 25 available — score should still be good.
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "current_limit": 100,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                    "score": 3,
                },
            ),
            _call(
                "check_existing_odcrs",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available_capacity": 25,
                    "reservation_count": 1,
                },
            ),
        ]
        results = assess_targets(calls)
        r = results[0]
        # ODCR coverage = min(25/20, 1.0) = 1.0
        assert r["confidence"]["signals"]["odcr_coverage"] == 1.0
        # spot_placement signal lifted from 0.3 to at least 1.0
        assert r["confidence"]["signals"]["spot_placement"] == 1.0
        assert r["confidence"]["label"] in ("HIGH", "MODERATE")

    def test_partial_odcr_coverage(self):
        """ODCR with capacity below request → partial coverage signal."""
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 100,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 100,
                    "score": 2,
                },
            ),
            _call(
                "check_existing_odcrs",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available_capacity": 25,
                    "reservation_count": 1,
                },
            ),
        ]
        results = assess_targets(calls)
        r = results[0]
        # ODCR coverage = 25/100 = 0.25
        assert r["confidence"]["signals"]["odcr_coverage"] == 0.25
        # spot signal lifted from 0.2 to 0.25 (the better of the two)
        assert r["confidence"]["signals"]["spot_placement"] == 0.25

    def test_lowest_score_first_sorting(self):
        """Multi-region results: riskiest target appears first."""
        calls = [
            # us-east-1: clean
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "current_limit": 100,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                    "score": 9,
                },
            ),
            # af-south-1: not offered
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "af-south-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "af-south-1",
                    "available": False,
                },
            ),
        ]
        results = assess_targets(calls)
        assert [r["region"] for r in results] == ["af-south-1", "us-east-1"]

    def test_malformed_tool_calls_dont_crash(self):
        """Defensive: malformed tool_calls entries are skipped, not raised."""
        calls = [
            ("garbage",),  # too short
            None,
            ("check_instance_availability", "not a dict", {"ok": True, "data": {}}),
            (
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                "not a dict",
            ),
        ]
        # Should produce no assessments and no exceptions
        assert assess_targets(calls) == []


class TestICEHistorySignal:
    """The signal_extractor pulls customer_ice_events from get_ice_history results."""

    def test_zero_ice_events_extracted(self):
        calls = [
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 0,
                    "capacity_signal": "clean",
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["customer_ice_events"] == 0

    def test_recurring_ice_events_extracted(self):
        calls = [
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 8,
                    "capacity_signal": "recurring",
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert signals["customer_ice_events"] == 8

    def test_ice_api_error_leaves_signal_unset(self):
        """When get_ice_history fails, event_count is None — leave signal unset."""
        calls = [
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": None,
                    "capacity_signal": "unknown",
                },
            ),
        ]
        signals = extract_signals(calls)[("g5.2xlarge", "us-east-1")]
        assert "customer_ice_events" not in signals

    def test_recurring_ice_dominates_strong_spot_score(self):
        """When ICE is recurring, the verdict reflects ground truth, not SPS."""
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "current_limit": 100,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 20,
                    "score": 10,
                },
            ),
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 12,
                    "capacity_signal": "severe",
                },
            ),
        ]
        results = assess_targets(calls)
        r = results[0]
        # Even with perfect SPS (10) and 100 quota, severe ICE caps spot_placement
        assert r["confidence"]["signals"]["spot_placement"] == 0.1
        assert r["confidence"]["signals"]["ice_history_signal"] == 0.0
        # And the verdict drops below the "all green" HIGH/DEPLOYABLE level
        assert r["confidence"]["label"] != "HIGH"


class TestICEMetricsSignal:
    """The signal_extractor also honors get_ice_metrics results, with
    the metrics reading taking precedence over the history reading when
    both are real. Metric filters index every matching CloudTrail event
    server-side, while LookupEvents is paging-limited on active fleets.
    """

    def _key(self):
        return ("g5.2xlarge", "us-east-1")

    def test_metrics_only_populates_signal(self):
        calls = [
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 4,
                    "capacity_signal": "recurring",
                },
            ),
        ]
        signals = extract_signals(calls)[self._key()]
        assert signals["customer_ice_events"] == 4
        assert signals["customer_ice_capacity_signal"] == "recurring"

    def test_metrics_wins_over_history_when_both_real(self):
        """When BOTH tools reported real signals, metrics wins because
        LookupEvents can miss events on active fleets."""
        calls = [
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 2,
                    "capacity_signal": "isolated",
                },
            ),
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 15,
                    "capacity_signal": "severe",
                },
            ),
        ]
        signals = extract_signals(calls)[self._key()]
        # Metrics count (15) wins, not history's 2.
        assert signals["customer_ice_events"] == 15
        assert signals["customer_ice_capacity_signal"] == "severe"

    def test_metrics_wins_over_history_when_history_ran_last(self):
        """Precedence must NOT depend on call order — even if history
        was appended to tool_calls AFTER metrics, metrics still wins."""
        calls = [
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 12,
                    "capacity_signal": "severe",
                },
            ),
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 1,
                    "capacity_signal": "isolated",
                },
            ),
        ]
        signals = extract_signals(calls)[self._key()]
        assert signals["customer_ice_events"] == 12
        assert signals["customer_ice_capacity_signal"] == "severe"

    def test_history_wins_when_metrics_inconclusive(self):
        """When metrics returned ``metric_filter_not_deployed`` (or
        ``unknown``), the history reading — a real signal — MUST win.
        This is the primary reason the agent falls back to history."""
        calls = [
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 0,
                    "capacity_signal": "metric_filter_not_deployed",
                },
            ),
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 5,
                    "capacity_signal": "recurring",
                },
            ),
        ]
        signals = extract_signals(calls)[self._key()]
        assert signals["customer_ice_events"] == 5
        assert signals["customer_ice_capacity_signal"] == "recurring"

    def test_history_wins_when_metrics_unknown(self):
        calls = [
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": None,
                    "capacity_signal": "unknown",
                },
            ),
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 0,
                    "capacity_signal": "clean",
                },
            ),
        ]
        signals = extract_signals(calls)[self._key()]
        # History (clean) is authoritative because metrics was unknown.
        assert signals["customer_ice_events"] == 0
        assert signals["customer_ice_capacity_signal"] == "clean"

    def test_both_inconclusive_yields_inconclusive_signal(self):
        """When BOTH tools returned inconclusive signals, whichever ran
        last stays on record. Either way the scorer refuses DEPLOYABLE
        via the ice_signal_unavailable cap."""
        calls = [
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": None,
                    "capacity_signal": "metric_filter_not_deployed",
                },
            ),
            _call(
                "get_ice_history",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": None,
                    "capacity_signal": "rate_limited",
                },
            ),
        ]
        signals = extract_signals(calls)[self._key()]
        assert signals["customer_ice_capacity_signal"] in {
            "metric_filter_not_deployed",
            "rate_limited",
        }
        assert "customer_ice_events" not in signals

    def test_internal_source_marker_stripped_from_output(self):
        """The ``_ice_signal_source`` bookkeeping key must NOT appear on
        raw_signals_used (payload cleanliness) and must NOT be passed as
        a kwarg to compute_confidence (which would raise TypeError)."""
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": 0,
                    "capacity_signal": "clean",
                },
            ),
        ]
        results = assess_targets(calls)
        assert results, "assess_targets should produce at least one assessment"
        exposed = results[0]["raw_signals_used"]
        assert all(not k.startswith("_") for k in exposed), (
            f"internal keys leaked into raw_signals_used: {list(exposed)}"
        )

    def test_metric_filter_not_deployed_alone_caps_verdict(self):
        """If ``get_ice_metrics`` returns ``metric_filter_not_deployed``
        and no other ICE reading exists, the deterministic scorer must
        refuse DEPLOYABLE via the ice_signal_unavailable cap."""
        calls = [
            _call(
                "check_instance_availability",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_pricing",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "available": True,
                },
            ),
            _call(
                "get_sagemaker_quota",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "current_limit": 100,
                },
            ),
            _call(
                "get_spot_placement_score",
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 5,
                },
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "target_capacity": 5,
                    "score": 10,
                },
            ),
            _call(
                "get_ice_metrics",
                {"instance_type": "g5.2xlarge", "region": "us-east-1"},
                {
                    "instance_type": "g5.2xlarge",
                    "region": "us-east-1",
                    "event_count": None,
                    "capacity_signal": "metric_filter_not_deployed",
                },
            ),
        ]
        results = assess_targets(calls)
        r = results[0]
        assert r["confidence"]["score"] <= 0.65
        cap_kinds = [c.get("kind") for c in r["confidence"].get("applied_caps", [])]
        assert "ice_signal_unavailable" in cap_kinds
        assert r["confidence"]["verdict"] != "DEPLOYABLE"
