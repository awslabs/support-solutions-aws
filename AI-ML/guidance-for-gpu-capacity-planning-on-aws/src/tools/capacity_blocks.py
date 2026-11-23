# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Capacity Blocks for ML availability tool.

Capacity Blocks for ML are pre-reserved slots on ML-focused instance
types (p4d, p5, p5e, p5en, and their successors — see the AWS Capacity
Blocks for ML documentation for the current supported list) available
in 1-day and multi-week chunks. The agent recommends Capacity Blocks
as a Tier 1 mitigation in its verdict text when risk is high; this
tool lets the agent check the public inventory before making that
recommendation rather than suggesting it blindly.

Non-ML instance families (g5, g6, etc.) are not part of the Capacity
Blocks programme; queries against those types return an empty
offerings list, which this tool surfaces as
``capacity_signal="unavailable"``.

Shape matches ``src/tools/spot_placement_score.py`` deliberately —
every tool in ``src/tools/`` wraps one public AWS API call behind a
narrow, agent-facing signature and returns a dict with a categorical
``capacity_signal`` plus a short human-readable ``interpretation``.

IAM permission required: ``ec2:DescribeCapacityBlockOfferings``.
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client


# DescribeCapacityBlockOfferings accepts CapacityDurationHours = 24
# (one day) or multiples of 7*24 = 168 (whole weeks). The public API
# documents durations up to 26 weeks (~half a year); anything longer
# needs account-team involvement. Values outside this grid are clamped
# rather than rejected so the agent doesn't see a blunt API error when
# the model produces a slightly off number.
_MIN_DURATION_HOURS = 24
_WEEK_HOURS = 7 * 24
_MAX_DURATION_HOURS = 26 * _WEEK_HOURS


def _ec2_client(region: str):
    return get_client("ec2", region)


def _normalize_duration(hours: int) -> int:
    """Snap a user-supplied duration to the valid API grid.

    Valid values: 24, or multiples of 168 (168, 336, ..., 4368). Anything
    else is rounded to the nearest valid value on the grid.
    """
    if hours <= _MIN_DURATION_HOURS:
        return _MIN_DURATION_HOURS
    # Round to nearest whole week, clamped to 26 weeks.
    weeks = max(1, round(hours / _WEEK_HOURS))
    return min(weeks * _WEEK_HOURS, _MAX_DURATION_HOURS)


def _classify_offerings(offerings: list[dict[str, Any]]) -> tuple[str, str]:
    """Map the raw offerings list to a categorical capacity_signal and
    a short human-readable interpretation.
    """
    if not offerings:
        return (
            "unavailable",
            "No Capacity Block for ML offerings match this request. "
            "Either the instance type is not part of the Capacity "
            "Blocks programme (only p4d, p5, and successor ML families "
            "are eligible — check the AWS Capacity Blocks for ML "
            "documentation for the current list) or no offerings match "
            "the requested duration / count / date range.",
        )
    return (
        "available",
        f"{len(offerings)} Capacity Block offering(s) match this "
        "request. Purchasing a matching block guarantees the requested "
        "capacity for the block's duration — treat this as a hard "
        "mitigation for a high-risk deployment.",
    )


def _summarize_offerings(
    raw_offerings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Shrink each raw boto3 offering to the fields the agent needs.

    Preserving the raw shape would waste tokens and expose datetime
    objects the agent's serializer can't handle. This projection keeps
    everything JSON-serializable.
    """
    summarized = []
    for offering in raw_offerings:
        start = offering.get("StartDate")
        end = offering.get("EndDate")
        summarized.append(
            {
                "capacity_block_offering_id": offering.get(
                    "CapacityBlockOfferingId",
                ),
                "instance_type": offering.get("InstanceType"),
                "availability_zone": offering.get("AvailabilityZone"),
                "instance_count": offering.get("InstanceCount"),
                "start_date": start.isoformat() if start else None,
                "end_date": end.isoformat() if end else None,
                "capacity_block_duration_hours": offering.get(
                    "CapacityBlockDurationHours",
                ),
                "upfront_fee": offering.get("UpfrontFee"),
                "currency_code": offering.get("CurrencyCode"),
                "tenancy": offering.get("Tenancy"),
            }
        )
    return summarized


def _pick_cheapest(
    offerings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the offering with the lowest upfront fee, or None."""
    cheapest = None
    cheapest_fee: float | None = None
    for offering in offerings:
        raw_fee = offering.get("upfront_fee")
        try:
            fee = float(raw_fee) if raw_fee is not None else None
        except (TypeError, ValueError):
            continue
        if fee is None:
            continue
        if cheapest_fee is None or fee < cheapest_fee:
            cheapest = offering
            cheapest_fee = fee
    return cheapest


def get_capacity_blocks_offerings(
    instance_type: str,
    region: str,
    target_count: int = 1,
    capacity_duration_hours: int = 24,
) -> dict[str, Any]:
    """Query the public Capacity Blocks for ML inventory.

    Args:
        instance_type: EC2 instance type (e.g. ``"p5.48xlarge"``). The
            ``ml.`` prefix is accepted and stripped.
        region: AWS region code (e.g. ``"us-east-1"``).
        target_count: Number of instances requested. Clamped to >= 1.
        capacity_duration_hours: Block duration. Snapped to 24 or a
            multiple of 168 up to 4368 (26 weeks).

    Returns:
        dict with:
            instance_type: normalized (no ml.)
            region: AWS region
            target_count: normalized (>= 1)
            capacity_duration_hours: normalized to the valid grid
            offerings_count: number of matching offerings
            offerings: list of summarized offerings
            cheapest_offering: offering with the lowest upfront fee, or None
            capacity_signal: 'available' | 'unavailable' | 'unknown'
            interpretation: human-readable summary
            source: 'ec2.DescribeCapacityBlockOfferings'
    """
    bare_type = instance_type.removeprefix("ml.")
    target_count = max(1, int(target_count))
    duration = _normalize_duration(int(capacity_duration_hours))

    try:
        client = _ec2_client(region)
        # MaxResults=20 is the API's paging max. For an agent-scale
        # query, 20 offerings is plenty for the LLM to pick a
        # cheapest / earliest / most-convenient one.
        resp = client.describe_capacity_block_offerings(
            InstanceType=bare_type,
            InstanceCount=target_count,
            CapacityDurationHours=duration,
            MaxResults=20,
        )
        raw_offerings = resp.get("CapacityBlockOfferings", []) or []
        offerings = _summarize_offerings(raw_offerings)
        cheapest = _pick_cheapest(offerings)
        capacity_signal, interpretation = _classify_offerings(offerings)

        return {
            "instance_type": bare_type,
            "region": region,
            "target_count": target_count,
            "capacity_duration_hours": duration,
            "offerings_count": len(offerings),
            "offerings": offerings,
            "cheapest_offering": cheapest,
            "capacity_signal": capacity_signal,
            "interpretation": interpretation,
            "source": "ec2.DescribeCapacityBlockOfferings",
        }

    except Exception as exc:  # noqa: BLE001
        error_type = type(exc).__name__
        return {
            "instance_type": bare_type,
            "region": region,
            "target_count": target_count,
            "capacity_duration_hours": duration,
            "offerings_count": 0,
            "offerings": [],
            "cheapest_offering": None,
            "capacity_signal": "unknown",
            "interpretation": (
                f"Unable to query Capacity Block offerings: "
                f"{error_type}: {exc}. This may indicate insufficient "
                "IAM permissions (ec2:DescribeCapacityBlockOfferings) "
                "or that the API is not available in this region."
            ),
            "source": "ec2.DescribeCapacityBlockOfferings",
        }


TOOL_SPEC = {
    "name": "get_capacity_blocks_offerings",
    "description": (
        "Query the public Capacity Blocks for ML inventory for an "
        "instance type / region / count / duration. Capacity Blocks "
        "are pre-reserved slots on ML-focused instance types (p4d, p5, "
        "p5e, p5en, and their successors) available in 1-day and "
        "multi-week chunks. USE THIS when you are about to recommend "
        "Capacity Blocks as a mitigation — do not recommend Capacity "
        "Blocks without first checking whether any are actually "
        "available for the target instance type and region. Non-ML "
        "instance families (g5, g6, etc.) will return "
        "capacity_signal='unavailable'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g. 'p5.48xlarge', "
                    "'p4d.24xlarge'). The ml. prefix is accepted and "
                    "stripped."
                ),
            },
            "region": {
                "type": "string",
                "description": ("AWS region code (e.g. 'us-east-1', 'us-west-2')."),
            },
            "target_count": {
                "type": "integer",
                "description": (
                    "Number of instances to reserve. Use the "
                    "customer's requested fleet size. Default 1."
                ),
            },
            "capacity_duration_hours": {
                "type": "integer",
                "description": (
                    "Block duration in hours. Must be 24 (1 day) or a "
                    "multiple of 168 (whole weeks) up to 4368 (26 "
                    "weeks). Values outside this grid are clamped."
                ),
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_capacity_blocks_offerings,
}
