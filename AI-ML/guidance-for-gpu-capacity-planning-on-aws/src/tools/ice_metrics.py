# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Customer-side capacity-error history via CloudWatch metric filters.

This is the precise, retrospective alternative to ``get_ice_history``'s
CloudTrail LookupEvents scan. When the customer has deployed the
companion stack shipped in
``docs/samples/customer-side-ice-metric-filter.yaml``, capacity-error
counts land in the ``GPUCapacityAgent/CustomerICE`` CloudWatch
namespace under two metrics:

- ``EC2CapacityErrors`` — dimensioned by ``InstanceType``. Every
  ``RunInstances`` failure matching the ICE error-code pattern
  increments the count for that instance type.
- ``SageMakerCapacityErrors`` — undimensioned (SageMaker's
  ``CreateEndpoint`` payload references the endpoint config by name,
  and CloudWatch Logs metric filters can't dereference it at index
  time — so the metric filter counts SageMaker-side capacity errors
  globally per region and this tool falls back to the aggregate
  when queried on the SageMaker path).

Why this tool exists (versus just using ``get_ice_history``):
CloudTrail LookupEvents does NOT accept ``errorCode`` as a lookup
attribute, so the agent's LookupEvents scan is paging-limited and
gets drowned by successful events on active fleets. A CloudWatch
metric filter indexes every matching event server-side, so this tool
gets a precise count in a single GetMetricStatistics call. When both
tools ran and the metrics tool returned a real signal, the scorer
prefers this tool's count over LookupEvents' (see
``src/signal_extractor.py``).

**Namespace / metric names / dimensions here MUST match the customer
CFN template.** If either side changes, the wiring is broken silently
— the tool returns ``metric_filter_not_deployed`` and the scorer will
still be honest (refuses DEPLOYABLE), but the customer's precise
data becomes invisible to the agent.

IAM required (both sides — Lambda role and customer trust policy):
    cloudwatch:GetMetricStatistics
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client


# Must match src/../docs/samples/customer-side-ice-metric-filter.yaml.
_METRIC_NAMESPACE = "GPUCapacityAgent/CustomerICE"
_EC2_METRIC_NAME = "EC2CapacityErrors"
_EC2_DIMENSION_KEY = "InstanceType"
_SAGEMAKER_METRIC_NAME = "SageMakerCapacityErrors"

# GetMetricStatistics allows at most 1440 datapoints per call. That means
# the period must scale with the lookback window: at 5-min buckets, a 30-day
# lookback would request 8,640 datapoints and CloudWatch rejects with
# ``InvalidParameterCombinationException``. See ``_period_for_lookback``.
# History (2026-07-13): the original code hard-coded 300s. Workshop dry
# run q_results.json showed Q5 (seeded severe signal for g5.4xlarge in
# us-east-1) came back ``capacity_signal="unknown"`` because the tool
# hit that exception at cold-start and the LLM then hallucinated the
# error message as "API rate limits". Fixed by making period adaptive.
_MAX_DATAPOINTS_PER_CALL = 1440
_MIN_STATISTICS_PERIOD_SECONDS = 300


def _period_for_lookback(lookback_days: int) -> int:
    """Return the smallest period (seconds) that keeps the CloudWatch
    ``GetMetricStatistics`` datapoint count under 1440 for the given
    lookback window. Uses 5-minute buckets for short windows (<=5 days)
    and grows monotonically from there.

    Math: period_s = max(300, ceil(lookback_seconds / 1440)). At 30 days
    that yields 1800s (30-min buckets); at 90 days it yields 5400s (90-min
    buckets). The metric filter emits per-event datapoints so aggregating
    into wider buckets does not lose the count — the Sum statistic sums
    identically at every resolution.
    """
    lookback_seconds = int(lookback_days) * 86_400
    needed = (
        lookback_seconds + _MAX_DATAPOINTS_PER_CALL - 1
    ) // _MAX_DATAPOINTS_PER_CALL
    # CloudWatch requires the period be a multiple of 60. Round up.
    needed = ((needed + 59) // 60) * 60
    return max(_MIN_STATISTICS_PERIOD_SECONDS, needed)


# Bound the lookback window. CloudWatch retains detailed metrics for 15
# months; matches CloudTrail's typical retention window and keeps
# consistency with get_ice_history's 90-day cap.
DEFAULT_LOOKBACK_DAYS = 30
MAX_LOOKBACK_DAYS = 90

# Valid deployment_target values. Matches get_ice_history.
_VALID_TARGETS = ("ec2", "sagemaker", "auto")


def _cloudwatch_client(region: str):
    """CloudWatch client scoped to ``region``.

    Goes through the same session-resolution path as the CloudTrail
    client so cross-account queries (``customer_context`` on the
    request) route through the assumed customer role. IAM required on
    both sides: ``cloudwatch:GetMetricStatistics``.
    """
    return get_client("cloudwatch", region)


def _classify_signal(event_count: int) -> tuple[str, str]:
    """Same event-count bands as get_ice_history for consistency."""
    if event_count == 0:
        return (
            "clean",
            "No capacity errors observed for this instance type in the "
            "lookback window — the customer's CloudWatch metric filter "
            "recorded zero matching events on the queried deployment "
            "target(s).",
        )
    if event_count <= 3:
        return (
            "isolated",
            f"{event_count} capacity error(s) observed in the lookback "
            "window — isolated incidents, may have been transient.",
        )
    if event_count <= 10:
        return (
            "recurring",
            f"{event_count} capacity errors observed in the lookback "
            "window — recurring capacity issues. Pool depth is "
            "demonstrably tight.",
        )
    return (
        "severe",
        f"{event_count} capacity errors observed in the lookback "
        "window — severe and persistent capacity issues. ODCR or "
        "Capacity Blocks are required before any new deployment.",
    )


def _sum_datapoints(datapoints: list[dict[str, Any]]) -> int:
    """CloudWatch returns ``Sum`` as float. Cast to int (event counts are
    whole numbers by construction — metric filter emits ``MetricValue=1``
    per match). Missing / empty list → 0.
    """
    total = 0
    for point in datapoints or []:
        try:
            total += int(point.get("Sum", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _fetch_metric_sum(
    client,
    metric_name: str,
    window_start: datetime,
    window_end: datetime,
    dimensions: list[dict[str, str]] | None,
    period_seconds: int,
) -> tuple[int, bool]:
    """Return ``(event_count, saw_data)``.

    ``saw_data`` distinguishes "the metric exists and returned 0
    datapoints" (the filter never fired — genuinely clean) from "the
    metric doesn't exist at all" (the customer hasn't deployed the
    companion stack). CloudWatch returns an empty ``Datapoints`` list
    in BOTH cases, so we can't tell them apart from a single response
    — the ``saw_data`` bit is optimistic (True when non-empty). The
    caller uses no-data as the primary "metric filter not deployed"
    signal; the response then falls back to ``get_ice_history``'s
    LookupEvents scan, which the scorer already handles.
    """
    kwargs = {
        "Namespace": _METRIC_NAMESPACE,
        "MetricName": metric_name,
        "StartTime": window_start,
        "EndTime": window_end,
        "Period": period_seconds,
        "Statistics": ["Sum"],
    }
    if dimensions:
        kwargs["Dimensions"] = dimensions
    resp = client.get_metric_statistics(**kwargs)
    datapoints = resp.get("Datapoints") or []
    return _sum_datapoints(datapoints), bool(datapoints)


def get_ice_metrics(
    instance_type: str,
    region: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    deployment_target: str = "auto",
) -> dict[str, Any]:
    """Query the ``GPUCapacityAgent/CustomerICE`` CloudWatch namespace for
    capacity-error counts on this instance type + region.

    Args:
        instance_type: EC2 instance type (e.g. ``"g5.2xlarge"``). ``ml.``
            prefix is accepted and stripped — the EC2 metric-filter
            dimension records the bare EC2 type.
        region: AWS region whose CloudWatch metrics to query.
        lookback_days: How many days of history to sum. Default 30,
            clamped to [1, 90].
        deployment_target: ``"ec2"``, ``"sagemaker"``, or ``"auto"``
            (default). Matches ``get_ice_history`` semantics — see that
            tool's docstring.

    Returns:
        dict with:
            instance_type: normalized (no ml.)
            region: AWS region
            deployment_target: the target queried
            lookback_days: clamped lookback window
            window_start / window_end: ISO timestamps
            event_count: total capacity errors matched (int) or None on error
            events_by_target: {"ec2": int|None, "sagemaker": int|None}
            capacity_signal: 'clean' | 'isolated' | 'recurring' | 'severe' |
                'metric_filter_not_deployed' | 'unknown'
            interpretation: human-readable summary
            source: API citation ("cloudwatch.GetMetricStatistics")

    Signal semantics on the "no data" path:

    - Both metrics return zero datapoints AND the deployment_target
      would have queried at least one branch → ``metric_filter_not_deployed``.
      The scorer treats this like ``unresolved`` — refuses DEPLOYABLE
      because we can't distinguish "customer hasn't deployed the
      companion stack" from "customer deployed it but nothing ever fired".
      The agent should call ``get_ice_history`` as a follow-up.
    - Some datapoints present but total = 0 → ``clean``. The metric
      filter is present and the count really is zero.
    - IAM denied / other API error → ``unknown``. Scorer refuses
      DEPLOYABLE.
    """
    bare_type = instance_type.removeprefix("ml.")
    lookback_days = max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS))

    if deployment_target not in _VALID_TARGETS:
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": deployment_target,
            "event_count": None,
            "capacity_signal": "unknown",
            "interpretation": (
                f"Invalid deployment_target={deployment_target!r}. Must be "
                f"one of {_VALID_TARGETS}."
            ),
            "source": "cloudwatch.GetMetricStatistics",
        }

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=lookback_days)

    try:
        client = _cloudwatch_client(region)
        period_seconds = _period_for_lookback(lookback_days)

        # ``deployment_target`` is retained on the response for provenance
        # but no longer scopes the query. Both branches are ALWAYS queried
        # so query wording cannot hide a real capacity signal.
        #
        # History (2026-07-13 workshop testing): the LLM interpreted
        # "ml.g5.4xlarge for prod inference" queries as SageMaker and
        # passed ``deployment_target="sagemaker"``. That skipped the
        # dimensioned EC2 branch which had the seeded severe signal (63
        # events) and the tool returned ``capacity_signal="clean"``. Since
        # SageMaker endpoints run on EC2 instances under the hood,
        # RunInstances failures for the underlying type are legitimate
        # capacity signals for BOTH deployment paths — always querying
        # both is correct behavior, not just a workshop workaround.
        #
        # Cost impact: 1 extra GetMetricStatistics call per invocation
        # (~$0.01 per 10,000 calls). Negligible.
        ec2_count, ec2_saw_data = _fetch_metric_sum(
            client,
            _EC2_METRIC_NAME,
            window_start,
            window_end,
            dimensions=[
                {"Name": _EC2_DIMENSION_KEY, "Value": bare_type},
            ],
            period_seconds=period_seconds,
        )
        # SageMaker metric is undimensioned by design (see module
        # docstring). We can't filter on ``ml.<type>`` at the metric
        # layer, so this count is a regional aggregate for the
        # SageMaker capacity-error family. Note that in the
        # ``interpretation`` so the model doesn't over-index on it.
        sm_count, sm_saw_data = _fetch_metric_sum(
            client,
            _SAGEMAKER_METRIC_NAME,
            window_start,
            window_end,
            dimensions=None,
            period_seconds=period_seconds,
        )

        total = ec2_count + sm_count
        any_saw_data = ec2_saw_data or sm_saw_data

        if not any_saw_data:
            # No datapoints anywhere — most likely cause is that the
            # customer hasn't deployed the companion metric-filter stack.
            # Do NOT claim clean; the LookupEvents fallback is the honest
            # answer here.
            capacity_signal = "metric_filter_not_deployed"
            interpretation = (
                "No CloudWatch datapoints for the GPUCapacityAgent/"
                "CustomerICE namespace in the lookback window. The most "
                "likely cause is that the customer has NOT deployed the "
                "companion CloudFormation stack "
                "(docs/samples/customer-side-ice-metric-filter.yaml). Do "
                "NOT treat this as a clean signal — fall back to "
                "get_ice_history (CloudTrail LookupEvents scan)."
            )
        else:
            capacity_signal, interpretation = _classify_signal(total)

        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": deployment_target,
            "lookback_days": lookback_days,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "event_count": total,
            # Both branches are always queried; counts are always ints, not
            # None. See the block-comment on the fetch calls above for why
            # the ``deployment_target`` parameter no longer scopes the query.
            "events_by_target": {
                "ec2": ec2_count,
                "sagemaker": sm_count,
            },
            "capacity_signal": capacity_signal,
            "interpretation": interpretation,
            "source": "cloudwatch.GetMetricStatistics",
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": deployment_target,
            "lookback_days": lookback_days,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "event_count": None,
            "events_by_target": {"ec2": None, "sagemaker": None},
            "capacity_signal": "unknown",
            "interpretation": (
                f"Unable to query CloudWatch: {type(exc).__name__}: {exc}. "
                "This may indicate insufficient IAM permissions "
                "(cloudwatch:GetMetricStatistics) or a transient API "
                "error. Do NOT treat this as a clean signal — fall back "
                "to get_ice_history."
            ),
            "source": "cloudwatch.GetMetricStatistics",
        }


TOOL_SPEC = {
    "name": "get_ice_metrics",
    "description": (
        "Query the customer's CloudWatch metric filters for capacity-error "
        "counts. This is the PRECISE, retrospective alternative to "
        "get_ice_history — it returns an exact count from every matching "
        "event, without CloudTrail LookupEvents' paging limitations. It "
        "requires the customer to have deployed the companion "
        "CloudFormation stack (docs/samples/customer-side-ice-metric-"
        "filter.yaml), which creates the GPUCapacityAgent/CustomerICE "
        "namespace. "
        "CALL ORDER: try get_ice_metrics FIRST when checking capacity "
        "history. If it returns capacity_signal='metric_filter_not_deployed' "
        "or 'unknown', fall back to get_ice_history. When "
        "get_ice_metrics returns a real signal (clean / isolated / "
        "recurring / severe), that signal is authoritative and outranks "
        "any get_ice_history reading. Do NOT call get_ice_history when "
        "get_ice_metrics has already produced a real signal. "
        "capacity_signal='metric_filter_not_deployed' is NOT a clean "
        "signal — the deterministic scorer refuses DEPLOYABLE off it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g., 'g5.2xlarge', 'p4d.24xlarge'). "
                    "The ml. prefix is accepted and stripped."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region to query (e.g., 'us-east-1').",
            },
            "lookback_days": {
                "type": "integer",
                "description": (
                    "Days of history to sum. Default 30, clamped to [1, 90]."
                ),
            },
            "deployment_target": {
                "type": "string",
                "description": (
                    "Advisory only — informs the returned response. This "
                    "tool ALWAYS queries both the EC2 (dimensioned by "
                    "InstanceType) and SageMaker (regional aggregate) "
                    "metric filters and returns the combined event count. "
                    "SageMaker endpoints run on EC2 instances, so "
                    "RunInstances failures for a given type are a real "
                    "capacity signal for both deployment paths. Pass 'ec2', "
                    "'sagemaker', or 'auto' (default) for provenance only; "
                    "it does not scope the query."
                ),
                "enum": ["ec2", "sagemaker", "auto"],
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_ice_metrics,
}
