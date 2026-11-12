# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
On-Demand Capacity Reservation (ODCR) lookup tool.

Uses the public EC2 DescribeCapacityReservations API to find the customer's
existing reservations for a given instance type in a region. This is a key
capacity signal the agent should consider BEFORE recommending the customer
create a new ODCR — if one already exists with enough free capacity, the
verdict changes.

ODCR state semantics (per EC2 docs):
- active           — reservation is held and instances may be launched into it
- payment-pending  — reservation is being purchased
- scheduled        — reservation will become active at a future time
- expired          — reservation end date passed
- cancelled        — reservation was cancelled
- payment-failed   — payment problem

This tool reports ODCRs in the first three states (active, payment-pending,
scheduled) since these are the ones that affect launch feasibility. Expired
or cancelled reservations are filtered out.

IAM permission required: ec2:DescribeCapacityReservations
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client

# State values considered "honored" for capacity planning purposes
HONORED_STATES = {"active", "payment-pending", "scheduled"}

# Pagination safety net. Each page returns up to 100 ODCRs; a 5-page cap
# means the tool sums up to 500 ODCRs of the same instance type in the
# same region. In practice even large enterprises rarely hold more than
# a handful of same-type ODCRs per region; the cap protects the Lambda
# from a pathological account (thousands of tiny reservations) burning
# the 29s API Gateway timeout on a single tool call. When the cap trips
# we set ``truncated=True`` on the response so the deterministic scorer
# can note that the aggregated capacity may be understated.
MAX_PAGES = 5
PAGE_SIZE = 100


def _ec2_client(region: str):
    return get_client("ec2", region)


def check_existing_odcrs(
    instance_type: str,
    region: str,
    availability_zone: str | None = None,
) -> dict[str, Any]:
    """Find the customer's existing On-Demand Capacity Reservations.

    Args:
        instance_type: EC2 instance type (e.g., "g5.2xlarge"). The ``ml.``
            prefix is accepted and stripped — ODCRs are held against the
            underlying EC2 instance type, not the SageMaker-prefixed alias.
        region: AWS region code (e.g., "us-east-1").
        availability_zone: Optional AZ filter (e.g., "us-east-1a"). When
            omitted, reservations from all AZs in the region are returned.

    Returns:
        dict with:
            instance_type: normalized (no ml.) instance type
            region: AWS region
            availability_zone: filter applied (or None)
            reservation_count: number of honored ODCRs matched
            total_capacity: sum of TotalInstanceCount across honored ODCRs
            available_capacity: sum of AvailableInstanceCount across honored ODCRs
            reservations: list of per-ODCR detail dicts
            capacity_signal: 'covered' | 'partial' | 'none' | 'unknown'
            interpretation: human-readable summary
            source: API citation
    """
    bare_type = instance_type.removeprefix("ml.")

    try:
        client = _ec2_client(region)

        filters: list[dict[str, Any]] = [
            {"Name": "instance-type", "Values": [bare_type]},
            {"Name": "state", "Values": sorted(HONORED_STATES)},
        ]
        if availability_zone:
            filters.append({"Name": "availability-zone", "Values": [availability_zone]})

        # DescribeCapacityReservations is paginated. Cap total pages to keep
        # the tool's worst-case latency bounded; ``truncated`` is surfaced
        # in the response when the cap trips.
        reservations: list[dict[str, Any]] = []
        next_token: str | None = None
        truncated = False
        for page_idx in range(MAX_PAGES):
            kwargs: dict[str, Any] = {"Filters": filters, "MaxResults": PAGE_SIZE}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = client.describe_capacity_reservations(**kwargs)
            reservations.extend(resp.get("CapacityReservations", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
            if page_idx == MAX_PAGES - 1:
                truncated = True

        if not reservations:
            return {
                "instance_type": bare_type,
                "region": region,
                "availability_zone": availability_zone,
                "reservation_count": 0,
                "total_capacity": 0,
                "available_capacity": 0,
                "reservations": [],
                "capacity_signal": "none",
                "interpretation": (
                    f"No active On-Demand Capacity Reservations found for {bare_type} "
                    f"in {region}"
                    + (f" / {availability_zone}" if availability_zone else "")
                    + ". Any launch will draw from the shared on-demand pool."
                ),
                "truncated": False,
                "source": "ec2.DescribeCapacityReservations",
            }

        # Aggregate counts and surface per-ODCR detail
        total_capacity = sum(
            int(r.get("TotalInstanceCount", 0) or 0) for r in reservations
        )
        available_capacity = sum(
            int(r.get("AvailableInstanceCount", 0) or 0) for r in reservations
        )

        detail = [
            {
                "reservation_id": r.get("CapacityReservationId"),
                "state": r.get("State"),
                "availability_zone": r.get("AvailabilityZone"),
                "instance_count": int(r.get("TotalInstanceCount", 0) or 0),
                "available_count": int(r.get("AvailableInstanceCount", 0) or 0),
                "tenancy": r.get("Tenancy"),
                "instance_platform": r.get("InstancePlatform"),
                "end_date_type": r.get("EndDateType"),
                "end_date": (
                    r.get("EndDate").isoformat() if r.get("EndDate") else None
                ),
            }
            for r in reservations
        ]

        if available_capacity == 0:
            capacity_signal = "fully_consumed"
        elif available_capacity < total_capacity:
            capacity_signal = "partial"
        else:
            capacity_signal = "covered"

        interpretation = (
            f"Found {len(reservations)} honored ODCR(s) for {bare_type} in {region}"
            + (f" / {availability_zone}" if availability_zone else "")
            + f": total reserved capacity {total_capacity}, currently available {available_capacity}. "
        )
        if capacity_signal == "covered":
            interpretation += (
                "All reserved capacity is free — feasibility verdict can lean on "
                "this guaranteed capacity for up to the available count."
            )
        elif capacity_signal == "partial":
            interpretation += (
                "Some reserved capacity is in use. The remaining available count "
                "is guaranteed; anything beyond it draws from the shared on-demand "
                "pool and should be assessed against Spot Placement Score."
            )
        else:
            interpretation += (
                "All reserved capacity is currently consumed; the launch will draw "
                "from the shared on-demand pool. Treat as if no ODCR exists when "
                "scoring capacity risk."
            )

        if truncated:
            interpretation += (
                f" Note: pagination cap of {MAX_PAGES * PAGE_SIZE} ODCR records "
                "was reached — the customer may hold more reservations of this "
                "type; the summed totals here are a lower bound."
            )

        return {
            "instance_type": bare_type,
            "region": region,
            "availability_zone": availability_zone,
            "reservation_count": len(reservations),
            "total_capacity": total_capacity,
            "available_capacity": available_capacity,
            "reservations": detail,
            "capacity_signal": capacity_signal,
            "interpretation": interpretation,
            "truncated": truncated,
            "source": "ec2.DescribeCapacityReservations",
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "instance_type": bare_type,
            "region": region,
            "availability_zone": availability_zone,
            "reservation_count": None,
            "total_capacity": None,
            "available_capacity": None,
            "reservations": [],
            "capacity_signal": "unknown",
            "interpretation": (
                f"Unable to query ODCRs: {type(exc).__name__}: {exc}. "
                "This may indicate insufficient IAM permissions "
                "(ec2:DescribeCapacityReservations) or a transient API error. "
                "Proceed with capacity assessment as if no ODCR exists."
            ),
            "truncated": False,
            "source": "ec2.DescribeCapacityReservations",
        }


TOOL_SPEC = {
    "name": "check_existing_odcrs",
    "description": (
        "Check the customer's existing On-Demand Capacity Reservations (ODCRs) "
        "for an EC2 instance type in a region. Uses the public "
        "DescribeCapacityReservations API and returns honored reservations "
        "(state in active, payment-pending, or scheduled) with their total and "
        "available capacity. Call this BEFORE recommending the customer create a "
        "new ODCR — if existing reserved capacity already covers (or partially "
        "covers) the ask, the verdict changes materially. The capacity_signal "
        "field reports 'covered' (existing ODCRs have free capacity), 'partial' "
        "(some free, some consumed), 'fully_consumed' (all reserved capacity in "
        "use), or 'none' (no ODCRs exist). When the customer has an active ODCR "
        "with available capacity equal to or exceeding the ask, treat the "
        "capacity-depth signal as strong even if Spot Placement Score is low."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g., 'g5.2xlarge', 'p4d.24xlarge'). "
                    "The ml. prefix is accepted and stripped — ODCRs are held "
                    "against the underlying EC2 type."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region code (e.g., 'us-east-1', 'sa-east-1').",
            },
            "availability_zone": {
                "type": "string",
                "description": (
                    "Optional AZ filter (e.g., 'us-east-1a'). Omit to include all "
                    "AZs in the region."
                ),
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": check_existing_odcrs,
}
