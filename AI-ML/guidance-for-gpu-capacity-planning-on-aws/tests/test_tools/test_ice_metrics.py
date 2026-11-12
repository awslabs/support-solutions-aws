# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ice_metrics (CloudWatch metric-filter) tool.

get_ice_metrics is the precise, retrospective alternative to the
CloudTrail LookupEvents scan performed by get_ice_history. When the
customer has deployed the companion metric-filter stack, this tool
returns an exact count via a single GetMetricStatistics call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.tools.ice_metrics import (
    DEFAULT_LOOKBACK_DAYS,
    MAX_LOOKBACK_DAYS,
    TOOL_SPEC,
    _classify_signal,
    _sum_datapoints,
    get_ice_metrics,
)


def _dp(value: float) -> dict:
    """Build a CloudWatch datapoint dict shaped like the API returns."""
    return {"Sum": value, "Unit": "Count"}


class TestSumDatapoints:
    def test_empty_list_returns_zero(self):
        assert _sum_datapoints([]) == 0

    def test_none_returns_zero(self):
        assert _sum_datapoints(None) == 0

    def test_integer_sums(self):
        assert _sum_datapoints([_dp(2), _dp(3), _dp(1)]) == 6

    def test_float_sums_truncate_to_int(self):
        assert _sum_datapoints([_dp(2.9), _dp(3.1)]) == 5

    def test_missing_sum_key_treated_as_zero(self):
        assert _sum_datapoints([{"Unit": "Count"}, _dp(4)]) == 4

    def test_none_value_treated_as_zero(self):
        assert _sum_datapoints([{"Sum": None}, _dp(2)]) == 2

    def test_non_numeric_string_skipped(self):
        assert _sum_datapoints([{"Sum": "oops"}, _dp(2)]) == 2


class TestClassifySignal:
    def test_zero_is_clean(self):
        signal, interpretation = _classify_signal(0)
        assert signal == "clean"
        assert (
            "zero" in interpretation.lower() or "no capacity" in interpretation.lower()
        )

    def test_one_is_isolated(self):
        assert _classify_signal(1)[0] == "isolated"

    def test_three_still_isolated(self):
        assert _classify_signal(3)[0] == "isolated"

    def test_four_is_recurring(self):
        assert _classify_signal(4)[0] == "recurring"

    def test_ten_is_recurring(self):
        assert _classify_signal(10)[0] == "recurring"

    def test_eleven_is_severe(self):
        assert _classify_signal(11)[0] == "severe"

    def test_hundred_is_severe(self):
        assert _classify_signal(100)[0] == "severe"


class TestGetIceMetricsHappyPath:
    """When CloudWatch returns real datapoints, get_ice_metrics classifies
    the total via the same bands as get_ice_history so downstream code
    doesn't have to branch on which source produced the reading."""

    def _mock_client(self, ec2_datapoints=None, sm_datapoints=None):
        client = MagicMock()

        def _responder(**kwargs):
            metric = kwargs.get("MetricName")
            if metric == "EC2CapacityErrors":
                return {"Datapoints": ec2_datapoints or []}
            if metric == "SageMakerCapacityErrors":
                return {"Datapoints": sm_datapoints or []}
            return {"Datapoints": []}

        client.get_metric_statistics.side_effect = _responder
        return client

    def test_ec2_only_data_still_severe(self):
        """Historical (pre-2026-07-13) behavior guarded EC2/SageMaker
        queries behind ``deployment_target``. That let a mis-picked
        target hide a real signal — workshop testing found the LLM
        reading "ml.<type> for prod inference" as "sagemaker" and
        missing a seeded severe EC2-side signal.

        The tool now ALWAYS queries both branches. Even if the caller
        passes ``deployment_target="ec2"``, the SageMaker branch is
        also queried (returns 0) so the response shape stays consistent
        and no signal can hide.
        """
        client = self._mock_client(ec2_datapoints=[_dp(20)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target="ec2")
        assert result["capacity_signal"] == "severe"
        assert result["event_count"] == 20
        # SageMaker branch is queried too, comes back 0.
        assert result["events_by_target"] == {"ec2": 20, "sagemaker": 0}
        # ``deployment_target`` on the response is provenance only.
        assert result["deployment_target"] == "ec2"

    def test_ec2_clean_signal(self):
        client = self._mock_client(ec2_datapoints=[_dp(0)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target="ec2")
        assert result["capacity_signal"] == "clean"
        assert result["event_count"] == 0
        # Both branches queried; SM branch returned nothing (empty list)
        # so its count is 0 not None.
        assert result["events_by_target"] == {"ec2": 0, "sagemaker": 0}

    def test_ec2_severe_signal(self):
        client = self._mock_client(ec2_datapoints=[_dp(9), _dp(8)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target="ec2")
        assert result["capacity_signal"] == "severe"
        assert result["event_count"] == 17

    def test_ml_prefix_is_stripped(self):
        """CloudWatch metric filter dimension records the bare EC2 type
        (e.g. ``g5.2xlarge``), not the ml.-prefixed SageMaker form."""
        client = self._mock_client(ec2_datapoints=[_dp(2)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics(
                "ml.g5.2xlarge", "us-east-1", deployment_target="ec2"
            )
        assert result["instance_type"] == "g5.2xlarge"
        # First call is EC2 (dimensioned); verify the bare type made it
        # into the dimension value.
        ec2_call = next(
            call
            for call in client.get_metric_statistics.call_args_list
            if call[1].get("MetricName") == "EC2CapacityErrors"
        )
        dims = {d["Name"]: d["Value"] for d in ec2_call[1]["Dimensions"]}
        assert dims["InstanceType"] == "g5.2xlarge"

    def test_sagemaker_call_is_undimensioned(self):
        """The SageMaker metric is a regional aggregate — CloudWatch Logs
        metric filters cannot dimension by an EndpointConfig indirection."""
        client = self._mock_client(sm_datapoints=[_dp(5)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics(
                "g5.2xlarge", "us-east-1", deployment_target="sagemaker"
            )
        assert result["capacity_signal"] == "recurring"
        # Both branches always queried now, but SM had the data.
        assert result["events_by_target"] == {"ec2": 0, "sagemaker": 5}
        # The SageMaker call itself must be undimensioned (regardless of
        # the caller's deployment_target hint).
        sm_call = next(
            call
            for call in client.get_metric_statistics.call_args_list
            if call[1].get("MetricName") == "SageMakerCapacityErrors"
        )
        assert "Dimensions" not in sm_call[1]

    def test_sagemaker_hint_still_queries_ec2_branch(self):
        """Regression guard for the workshop testing finding
        (2026-07-13). A caller passing ``deployment_target="sagemaker"``
        must still see the EC2-side signal, because SageMaker endpoints
        run on the same underlying EC2 instances."""
        client = self._mock_client(ec2_datapoints=[_dp(30)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics(
                "g5.4xlarge", "us-east-1", deployment_target="sagemaker"
            )
        # Signal came from the EC2 branch, not the SageMaker one.
        assert result["events_by_target"] == {"ec2": 30, "sagemaker": 0}
        assert result["event_count"] == 30
        assert result["capacity_signal"] == "severe"

    def test_auto_queries_both_branches(self):
        client = self._mock_client(ec2_datapoints=[_dp(2)], sm_datapoints=[_dp(3)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1")
        assert result["deployment_target"] == "auto"
        assert result["event_count"] == 5
        assert result["events_by_target"] == {"ec2": 2, "sagemaker": 3}
        assert result["capacity_signal"] == "recurring"

    def test_both_calls_issued_regardless_of_hint(self):
        """Belt-and-braces: for every value of deployment_target, both
        MetricName queries fire exactly once."""
        for hint in ("ec2", "sagemaker", "auto"):
            client = self._mock_client(ec2_datapoints=[_dp(1)], sm_datapoints=[_dp(1)])
            with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
                get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target=hint)
            metrics_called = {
                call[1].get("MetricName")
                for call in client.get_metric_statistics.call_args_list
            }
            assert metrics_called == {
                "EC2CapacityErrors",
                "SageMakerCapacityErrors",
            }, f"deployment_target={hint} skipped a branch"

    def test_source_field_cites_api(self):
        client = self._mock_client(ec2_datapoints=[_dp(0)])
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target="ec2")
        assert result["source"] == "cloudwatch.GetMetricStatistics"


class TestGetIceMetricsNoData:
    """When both metrics return zero datapoints, we cannot distinguish
    "metric filter not deployed" from "metric filter deployed and never
    fired". The tool must not falsely claim ``clean`` — the honest
    answer is ``metric_filter_not_deployed``, which the scorer treats
    like ``unresolved`` (refuses DEPLOYABLE)."""

    def test_no_datapoints_yields_metric_filter_not_deployed(self):
        client = MagicMock()
        client.get_metric_statistics.return_value = {"Datapoints": []}
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1")
        assert result["capacity_signal"] == "metric_filter_not_deployed"
        # Refusing to inflate to 'clean' is the whole point.
        assert "clean" not in result["capacity_signal"]
        assert "companion CloudFormation stack" in result["interpretation"]

    def test_zero_valued_datapoint_is_clean_not_undeployed(self):
        """A datapoint whose ``Sum`` is exactly zero MUST be treated as
        a real "the filter ran and no errors matched" reading — not as
        an undeployed filter."""
        client = MagicMock()

        def _responder(**kwargs):
            if kwargs.get("MetricName") == "EC2CapacityErrors":
                return {"Datapoints": [_dp(0)]}
            return {"Datapoints": []}

        client.get_metric_statistics.side_effect = _responder
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target="ec2")
        assert result["capacity_signal"] == "clean"


class TestGetIceMetricsErrorPaths:
    def test_iam_denied_yields_unknown_signal(self):
        client = MagicMock()
        client.get_metric_statistics.side_effect = Exception(
            "AccessDenied: cloudwatch:GetMetricStatistics"
        )
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics("g5.2xlarge", "us-east-1")
        assert result["capacity_signal"] == "unknown"
        assert result["event_count"] is None
        assert "AccessDenied" in result["interpretation"]

    def test_client_ctor_error_yields_unknown_signal(self):
        with patch(
            "src.tools.ice_metrics._cloudwatch_client",
            side_effect=Exception("could not resolve region"),
        ):
            result = get_ice_metrics("g5.2xlarge", "us-east-1")
        assert result["capacity_signal"] == "unknown"
        assert result["event_count"] is None

    def test_invalid_deployment_target_returns_unknown(self):
        result = get_ice_metrics("g5.2xlarge", "us-east-1", deployment_target="fargate")
        assert result["capacity_signal"] == "unknown"
        assert "Invalid deployment_target" in result["interpretation"]


class TestGetIceMetricsLookbackClamping:
    def _client_capturing_kwargs(self, capture):
        client = MagicMock()

        def _responder(**kwargs):
            capture.append(kwargs)
            return {"Datapoints": []}

        client.get_metric_statistics.side_effect = _responder
        return client

    def test_lookback_above_max_is_clamped(self):
        captured: list[dict] = []
        client = self._client_capturing_kwargs(captured)
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics(
                "g5.2xlarge",
                "us-east-1",
                lookback_days=9999,
                deployment_target="ec2",
            )
        assert result["lookback_days"] == MAX_LOOKBACK_DAYS

    def test_lookback_below_one_is_clamped(self):
        captured: list[dict] = []
        client = self._client_capturing_kwargs(captured)
        with patch("src.tools.ice_metrics._cloudwatch_client", return_value=client):
            result = get_ice_metrics(
                "g5.2xlarge",
                "us-east-1",
                lookback_days=0,
                deployment_target="ec2",
            )
        assert result["lookback_days"] == 1

    def test_default_lookback_days_is_30(self):
        assert DEFAULT_LOOKBACK_DAYS == 30


class TestToolSpec:
    def test_tool_spec_is_wired(self):
        assert TOOL_SPEC["name"] == "get_ice_metrics"
        assert TOOL_SPEC["handler"] is get_ice_metrics

    def test_input_schema_requires_instance_type_and_region(self):
        required = TOOL_SPEC["input_schema"]["required"]
        assert set(required) == {"instance_type", "region"}

    def test_description_advertises_call_order(self):
        """The model reads this description to know it should call
        get_ice_metrics FIRST and fall back to get_ice_history when the
        metric filter isn't deployed."""
        desc = TOOL_SPEC["description"]
        assert "FIRST" in desc
        assert "get_ice_history" in desc
        assert "metric_filter_not_deployed" in desc

    def test_deployment_target_enum(self):
        enum = TOOL_SPEC["input_schema"]["properties"]["deployment_target"]["enum"]
        assert set(enum) == {"ec2", "sagemaker", "auto"}
