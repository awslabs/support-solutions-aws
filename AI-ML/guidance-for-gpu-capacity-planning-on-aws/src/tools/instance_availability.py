# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Instance availability tool — checks if an instance type is offered in a region/AZ.

Uses the public EC2 DescribeInstanceTypeOfferings API.
This replaces a static roadmap file approach with a real-time check
any AWS customer can run.
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client


def _ec2_client(region: str):
    """Return an EC2 client honoring the current cross-account session, if any."""
    return get_client("ec2", region)


def check_instance_availability(instance_type: str, region: str) -> dict[str, Any]:
    """Check if an instance type is offered in a region and which AZs support it.

    Args:
        instance_type: EC2 instance type (e.g., "g7e.2xlarge")
        region: AWS region code (e.g., "sa-east-1")

    Returns:
        dict with availability status, supported AZs, and source citation.
    """
    bare_type = instance_type.removeprefix("ml.")

    try:
        client = _ec2_client(region)

        # Check region-level offering
        region_resp = client.describe_instance_type_offerings(
            LocationType="region",
            Filters=[{"Name": "instance-type", "Values": [bare_type]}],
        )
        offered_in_region = len(region_resp.get("InstanceTypeOfferings", [])) > 0

        if not offered_in_region:
            return {
                "instance_type": bare_type,
                "region": region,
                "available": False,
                "azs": [],
                "interpretation": (
                    f"{bare_type} is NOT offered in {region}. "
                    "This instance type cannot be launched here regardless of capacity."
                ),
                "source": "ec2.DescribeInstanceTypeOfferings (region)",
            }

        # Check AZ-level availability
        az_resp = client.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [bare_type]}],
        )
        azs = sorted(o["Location"] for o in az_resp.get("InstanceTypeOfferings", []))

        return {
            "instance_type": bare_type,
            "region": region,
            "available": True,
            "azs": azs,
            "az_count": len(azs),
            "interpretation": (
                f"{bare_type} is offered in {region} across {len(azs)} AZ(s): "
                f"{', '.join(azs)}."
            ),
            "source": "ec2.DescribeInstanceTypeOfferings (availability-zone)",
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "instance_type": bare_type,
            "region": region,
            "available": None,
            "azs": [],
            "interpretation": f"API error: {type(exc).__name__}: {exc}",
            "source": "ec2.DescribeInstanceTypeOfferings",
        }


TOOL_SPEC = {
    "name": "check_instance_availability",
    "description": (
        "Check whether an EC2 instance type is offered in a given AWS region "
        "and which availability zones support it. Uses the public "
        "DescribeInstanceTypeOfferings API. If the instance type is NOT offered "
        "in the region, it cannot be launched regardless of quotas or capacity — "
        "this is a categorical blocker. Use this as the FIRST check before "
        "pricing or quota lookups."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g., 'g7e.2xlarge', 'p5.48xlarge'). "
                    "Do not include ml. prefix."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region code (e.g., 'us-east-1', 'sa-east-1')",
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": check_instance_availability,
}
