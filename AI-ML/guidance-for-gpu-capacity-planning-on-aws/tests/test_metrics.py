# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ToolMetricsEmitter class.

Validates that EMF JSON is correctly structured and that emission
failures never propagate.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.metrics import ToolMetricsEmitter


class TestToolMetricsEmitter:
    """Unit tests for ToolMetricsEmitter."""

    def test_emit_tool_metric_produces_valid_emf_json(self, capsys):
        """Verify emitted JSON has correct EMF structure."""
        emitter = ToolMetricsEmitter()
        emitter.emit_tool_metric("check_instance_availability", 245.3, "Success")

        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())

        # Verify _aws key structure
        assert "_aws" in payload
        assert "Timestamp" in payload["_aws"]
        assert isinstance(payload["_aws"]["Timestamp"], int)

        cw_metrics = payload["_aws"]["CloudWatchMetrics"]
        assert len(cw_metrics) == 1
        assert cw_metrics[0]["Namespace"] == "CapacityAgent/Tools"
        assert cw_metrics[0]["Dimensions"] == [["ToolName", "Status"]]

        metrics = cw_metrics[0]["Metrics"]
        assert len(metrics) == 2
        metric_names = {m["Name"]: m["Unit"] for m in metrics}
        assert metric_names["Duration"] == "Milliseconds"
        assert metric_names["Invocations"] == "Count"

        # Verify dimension and metric values
        assert payload["ToolName"] == "check_instance_availability"
        assert payload["Status"] == "Success"
        assert payload["Duration"] == 245.3
        assert payload["Invocations"] == 1

    def test_emit_tool_metric_failure_status(self, capsys):
        """Verify failure status is correctly emitted."""
        emitter = ToolMetricsEmitter()
        emitter.emit_tool_metric("get_pricing", 1500.0, "Failure")

        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())

        assert payload["ToolName"] == "get_pricing"
        assert payload["Status"] == "Failure"
        assert payload["Duration"] == 1500.0

    def test_emit_tool_metric_custom_namespace(self, capsys):
        """Verify custom namespace is used when provided."""
        emitter = ToolMetricsEmitter(namespace="Custom/Namespace")
        emitter.emit_tool_metric("my_tool", 100.0, "Success")

        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())

        assert (
            payload["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Custom/Namespace"
        )

    def test_emit_tool_metric_never_raises_on_json_error(self):
        """Verify that emission failures are silently caught."""
        emitter = ToolMetricsEmitter()

        # Patch json.dumps to raise an exception
        with patch("src.metrics.json.dumps", side_effect=TypeError("not serializable")):
            # Should not raise
            emitter.emit_tool_metric("tool", 100.0, "Success")

    def test_emit_tool_metric_never_raises_on_print_error(self):
        """Verify that print failures are silently caught."""
        emitter = ToolMetricsEmitter()

        # Patch print to raise an exception
        with patch("builtins.print", side_effect=IOError("stdout broken")):
            # Should not raise
            emitter.emit_tool_metric("tool", 100.0, "Success")

    def test_emit_tool_metric_zero_duration(self, capsys):
        """Verify zero duration is handled correctly."""
        emitter = ToolMetricsEmitter()
        emitter.emit_tool_metric("fast_tool", 0.0, "Success")

        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())

        assert payload["Duration"] == 0.0
        assert payload["Invocations"] == 1


class TestInvocationMetric:
    """regression guard: emit_invocation_metric produces
    endpoint-dimensioned EMF JSON so the p99 latency alarm can split
    API Gateway from Function URL traffic.
    """

    def test_api_gateway_dimension_emitted(self, capsys):
        emitter = ToolMetricsEmitter(namespace="CapacityAgent/Invocation")
        emitter.emit_invocation_metric(duration_ms=1500.0, endpoint="ApiGateway")

        payload = json.loads(capsys.readouterr().out.strip())

        cw = payload["_aws"]["CloudWatchMetrics"][0]
        assert cw["Namespace"] == "CapacityAgent/Invocation"
        assert cw["Dimensions"] == [["Endpoint"]]
        metric_names = {m["Name"]: m["Unit"] for m in cw["Metrics"]}
        assert metric_names == {"InvocationDuration": "Milliseconds"}
        assert payload["Endpoint"] == "ApiGateway"
        assert payload["InvocationDuration"] == 1500.0

    def test_function_url_dimension_emitted(self, capsys):
        emitter = ToolMetricsEmitter(namespace="CapacityAgent/Invocation")
        emitter.emit_invocation_metric(duration_ms=45000.0, endpoint="FunctionUrl")

        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["Endpoint"] == "FunctionUrl"
        assert payload["InvocationDuration"] == 45000.0

    def test_emission_never_raises_on_print_error(self):
        emitter = ToolMetricsEmitter(namespace="CapacityAgent/Invocation")
        with patch("builtins.print", side_effect=IOError("stdout broken")):
            emitter.emit_invocation_metric(duration_ms=100.0, endpoint="ApiGateway")

    def test_emission_never_raises_on_json_error(self):
        emitter = ToolMetricsEmitter(namespace="CapacityAgent/Invocation")
        with patch("src.metrics.json.dumps", side_effect=TypeError("boom")):
            emitter.emit_invocation_metric(duration_ms=100.0, endpoint="ApiGateway")
