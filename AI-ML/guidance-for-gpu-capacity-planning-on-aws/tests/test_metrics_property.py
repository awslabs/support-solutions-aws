# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for the ToolMetricsEmitter class.

Feature: production-observability-resilience
Property 1: EMF Metric Emission Structure

For any tool execution (with any tool name, any duration value, and any
success/failure outcome), the emitted metric SHALL be a valid EMF JSON
structure containing namespace "CapacityAgent/Tools", dimensions
["ToolName", "Status"], and metric names "Duration" (Milliseconds) and
"Invocations" (Count) with correct values.

**Validates: Requirements 3.1, 3.2, 3.3**
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from hypothesis import given, settings
from hypothesis import strategies as st

from src.metrics import ToolMetricsEmitter


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for tool names: printable strings of reasonable length
tool_name_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=100,
)

# Strategy for duration in milliseconds: 0 to 60000ms
duration_ms_st = st.floats(
    min_value=0.0, max_value=60000.0, allow_nan=False, allow_infinity=False
)

# Strategy for status values
status_st = st.sampled_from(["Success", "Failure"])


# ---------------------------------------------------------------------------
# Property test: EMF Metric Emission Structure
# ---------------------------------------------------------------------------


class TestEMFMetricEmissionStructure:
    """Property 1: EMF Metric Emission Structure.

    Feature: production-observability-resilience
    Property 1: EMF Metric Emission Structure

    **Validates: Requirements 3.1, 3.2, 3.3**
    """

    @given(
        tool_name=tool_name_st,
        duration_ms=duration_ms_st,
        status=status_st,
    )
    @settings(max_examples=100)
    def test_emitted_json_is_valid_emf_structure(
        self, tool_name: str, duration_ms: float, status: str
    ):
        """**Validates: Requirements 3.1, 3.2, 3.3**

        For any tool name, duration (0-60000ms), and status ("Success"/"Failure"),
        the emitted JSON is valid EMF with correct namespace, dimensions,
        metric names, units, and values.
        """
        emitter = ToolMetricsEmitter()

        # Capture stdout using redirect_stdout (compatible with hypothesis)
        captured = io.StringIO()
        with redirect_stdout(captured):
            emitter.emit_tool_metric(tool_name, duration_ms, status)

        output = captured.getvalue().strip()

        # Property: output must be valid JSON
        payload = json.loads(output)

        # Property: must contain _aws key (EMF marker)
        assert "_aws" in payload, "EMF payload must contain '_aws' key"

        aws_section = payload["_aws"]

        # Property: _aws must contain a valid Timestamp (integer milliseconds)
        assert "Timestamp" in aws_section, "_aws must contain 'Timestamp'"
        assert isinstance(aws_section["Timestamp"], int), "Timestamp must be an integer"
        assert aws_section["Timestamp"] > 0, "Timestamp must be positive"

        # Property: _aws must contain CloudWatchMetrics array
        assert "CloudWatchMetrics" in aws_section, (
            "_aws must contain 'CloudWatchMetrics'"
        )
        cw_metrics = aws_section["CloudWatchMetrics"]
        assert isinstance(cw_metrics, list), "CloudWatchMetrics must be a list"
        assert len(cw_metrics) == 1, "CloudWatchMetrics must have exactly 1 entry"

        metric_def = cw_metrics[0]

        # Property: Namespace must be "CapacityAgent/Tools"
        assert metric_def["Namespace"] == "CapacityAgent/Tools", (
            f"Namespace must be 'CapacityAgent/Tools', got '{metric_def['Namespace']}'"
        )

        # Property: Dimensions must be [["ToolName", "Status"]]
        assert metric_def["Dimensions"] == [["ToolName", "Status"]], (
            f"Dimensions must be [['ToolName', 'Status']], "
            f"got {metric_def['Dimensions']}"
        )

        # Property: Metrics must contain Duration (Milliseconds) and
        # Invocations (Count)
        metrics = metric_def["Metrics"]
        assert len(metrics) == 2, f"Must have exactly 2 metrics, got {len(metrics)}"

        metric_map = {m["Name"]: m["Unit"] for m in metrics}
        assert "Duration" in metric_map, "Must have 'Duration' metric"
        assert metric_map["Duration"] == "Milliseconds", (
            f"Duration unit must be 'Milliseconds', got '{metric_map['Duration']}'"
        )
        assert "Invocations" in metric_map, "Must have 'Invocations' metric"
        assert metric_map["Invocations"] == "Count", (
            f"Invocations unit must be 'Count', got '{metric_map['Invocations']}'"
        )

        # Property: Dimension values must match inputs
        assert payload["ToolName"] == tool_name, (
            f"ToolName must be '{tool_name}', got '{payload['ToolName']}'"
        )
        assert payload["Status"] == status, (
            f"Status must be '{status}', got '{payload['Status']}'"
        )

        # Property: Metric values must match inputs
        assert payload["Duration"] == duration_ms, (
            f"Duration must be {duration_ms}, got {payload['Duration']}"
        )
        assert payload["Invocations"] == 1, (
            f"Invocations must be 1, got {payload['Invocations']}"
        )
