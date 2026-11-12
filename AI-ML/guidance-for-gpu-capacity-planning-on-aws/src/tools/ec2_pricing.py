# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
EC2 on-demand pricing tool — for self-managed GPU fleets and EKS node groups.

Uses the AWS Pricing API (ServiceCode: AmazonEC2) to get on-demand hourly
rates for bare EC2 instances. This is the pricing that applies to:
- Self-managed GPU fleets (raw EC2 instances)
- EKS GPU node groups (EC2 instances managed by EKS)
- EC2-backed inference (not SageMaker managed)

IAM permission required: pricing:GetProducts
"""

from __future__ import annotations

import json
from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client

PRICING_API_REGION = "us-east-1"

# Map regionCode → AWS location name
REGION_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "sa-east-1": "South America (Sao Paulo)",
    "me-south-1": "Middle East (Bahrain)",
    "af-south-1": "Africa (Cape Town)",
}


def _pricing_client():
    # See src/tools/pricing.py for rationale — Pricing API is not
    # customer-scoped data, always use the deploy account.
    return get_client("pricing", PRICING_API_REGION, use_deploy_account=True)


def get_ec2_pricing(
    instance_type: str,
    region: str,
    operating_system: str = "Linux",
) -> dict[str, Any]:
    """Get EC2 on-demand hourly price for a given instance type and region.

    Use this for:
    - Self-managed GPU fleets (raw EC2)
    - EKS GPU node groups
    - EC2-backed inference (not SageMaker managed)

    Args:
        instance_type: EC2 instance type (e.g., "g5.2xlarge", "p4d.24xlarge").
                       Do NOT include "ml." prefix.
        region: AWS region code (e.g., "us-east-1", "sa-east-1").
        operating_system: OS type — "Linux" (default), "Windows", "RHEL".

    Returns:
        dict with hourly price, availability status, and source citation.
    """
    # Strip ml. prefix if present (customer may copy from SageMaker context)
    bare_type = instance_type.removeprefix("ml.")

    location = REGION_LOCATION.get(region)
    if location is None:
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": "EC2/EKS",
            "hourly_usd": None,
            "available": False,
            "source": "AWS Pricing API (AmazonEC2)",
            "_notes": f"Unknown region code: {region}",
        }

    client = _pricing_client()
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": bare_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": operating_system},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ]

    try:
        response = client.get_products(
            ServiceCode="AmazonEC2",
            Filters=filters,
            MaxResults=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": "EC2/EKS",
            "hourly_usd": None,
            "available": False,
            "source": "AWS Pricing API (AmazonEC2)",
            "_notes": f"Pricing API error: {type(exc).__name__}: {exc}",
        }

    price_list = response.get("PriceList", [])
    if not price_list:
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": "EC2/EKS",
            "hourly_usd": None,
            "available": False,
            "source": "AWS Pricing API (AmazonEC2)",
            "_notes": (
                f"No EC2 on-demand pricing for {bare_type} in {region}. "
                "Instance type may not be offered in this region."
            ),
        }

    # Extract the hourly rate
    for entry_str in price_list:
        entry = json.loads(entry_str) if isinstance(entry_str, str) else entry_str
        on_demand = entry.get("terms", {}).get("OnDemand", {})
        for _, term in on_demand.items():
            for _, dim in term.get("priceDimensions", {}).items():
                rate = dim.get("pricePerUnit", {}).get("USD")
                if rate is not None and float(rate) > 0:
                    return {
                        "instance_type": bare_type,
                        "region": region,
                        "deployment_target": "EC2/EKS",
                        "operating_system": operating_system,
                        "hourly_usd": float(rate),
                        "available": True,
                        "source": "AWS Pricing API (AmazonEC2)",
                        "interpretation": (
                            f"EC2 on-demand: {bare_type} in {region} = "
                            f"${float(rate):.4f}/hr ({operating_system}). "
                            "This is the raw EC2 price — applicable to self-managed "
                            "GPU fleets and EKS node groups."
                        ),
                        "_notes": None,
                    }

    return {
        "instance_type": bare_type,
        "region": region,
        "deployment_target": "EC2/EKS",
        "hourly_usd": None,
        "available": False,
        "source": "AWS Pricing API (AmazonEC2)",
        "_notes": "Found SKU records but no extractable hourly rate.",
    }


TOOL_SPEC = {
    "name": "get_ec2_pricing",
    "description": (
        "Get EC2 on-demand hourly price for an instance type — use this when the "
        "customer is deploying on raw EC2 instances or EKS GPU node groups (NOT "
        "SageMaker managed endpoints). The price returned is the bare EC2 compute "
        "cost. For SageMaker-managed deployments, use get_sagemaker_pricing instead. "
        "EC2 pricing is typically lower than SageMaker pricing for the same instance "
        "type because SageMaker adds management overhead. Use this to compare "
        "self-managed vs. SageMaker-managed deployment costs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g., 'g5.2xlarge', 'p4d.24xlarge'). "
                    "Do NOT include ml. prefix."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region code (e.g., 'us-east-1', 'sa-east-1')",
            },
            "operating_system": {
                "type": "string",
                "description": "OS type: 'Linux' (default), 'Windows', 'RHEL'",
                "default": "Linux",
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_ec2_pricing,
}
