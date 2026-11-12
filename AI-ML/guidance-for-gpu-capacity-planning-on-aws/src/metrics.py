# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom metrics module using CloudWatch Embedded Metric Format (EMF).

Emits tool-level performance metrics (duration, invocations) to CloudWatch
via structured log lines printed to stdout. The EMF format is parsed by
CloudWatch Logs and automatically creates metrics without additional API calls.

Requirements: 3.1, 3.2, 3.3, 3.5
"""

from __future__ import annotations

import json
import time


class ToolMetricsEmitter:
    """Emits tool execution metrics using CloudWatch Embedded Metric Format.

    Metrics are emitted as JSON to stdout with the _aws key that CloudWatch
    Logs recognizes as EMF. This avoids the need for PutMetricData API calls.
    """

    def __init__(self, namespace: str = "CapacityAgent/Tools"):
        self._namespace = namespace

    def emit_invocation_metric(
        self,
        duration_ms: float,
        endpoint: str,
    ) -> None:
        """Emit a per-invocation duration metric dimensioned by endpoint.

        Args:
            duration_ms: Total handler wall-clock in milliseconds.
            endpoint: Either "ApiGateway" or "FunctionUrl". Callers can pass
                "Unknown" if the event shape doesn't identify the source
                (e.g. synthetic test invocations).

        This method never raises — see ``emit_tool_metric`` for rationale.
        """
        try:
            emf_payload = {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": self._namespace,
                            "Dimensions": [["Endpoint"]],
                            "Metrics": [
                                {"Name": "InvocationDuration", "Unit": "Milliseconds"},
                            ],
                        }
                    ],
                },
                "Endpoint": endpoint,
                "InvocationDuration": duration_ms,
            }
            print(json.dumps(emf_payload))
        except Exception:  # nosec B110 - metric emission is best-effort; never propagate
            # Same policy as emit_tool_metric — never propagate.
            pass

    def emit_tool_metric(
        self,
        tool_name: str,
        duration_ms: float,
        status: str,
    ) -> None:
        """Emit an EMF-formatted metric to stdout.

        Args:
            tool_name: Name of the tool that was executed.
            duration_ms: Execution duration in milliseconds.
            status: "Success" or "Failure".

        This method never raises — all exceptions are silently caught
        to ensure metric emission failures do not affect tool execution.
        """
        try:
            emf_payload = {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": self._namespace,
                            "Dimensions": [["ToolName", "Status"]],
                            "Metrics": [
                                {"Name": "Duration", "Unit": "Milliseconds"},
                                {"Name": "Invocations", "Unit": "Count"},
                            ],
                        }
                    ],
                },
                "ToolName": tool_name,
                "Status": status,
                "Duration": duration_ms,
                "Invocations": 1,
            }
            print(json.dumps(emf_payload))
        except Exception:  # nosec B110 - metric emission failures never propagate
            # Requirement 3.5: metric emission failures never propagate
            pass
