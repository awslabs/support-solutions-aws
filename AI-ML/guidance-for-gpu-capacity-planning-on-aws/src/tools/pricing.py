# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
SageMaker hosting pricing tool — REAL implementation via AWS Pricing API.

Pricing API is only available in us-east-1, ap-south-1, eu-central-1.
We use us-east-1 as the API endpoint.

Returns hourly on-demand price for ml.* instance types for SageMaker hosting.
If the instance type isn't priced in the requested region, returns
not_available signal — which itself is meaningful (e.g., g7e.2xlarge in GRU is
NOT yet GA in SageMaker hosting).
"""

from __future__ import annotations

import json
from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client

# Pricing API only lives in these regions
PRICING_API_REGION = "us-east-1"

# Map regionCode → AWS location name (used by Pricing API)
REGION_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "eu-south-2": "Europe (Spain)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "sa-east-1": "South America (Sao Paulo)",
    "me-central-1": "Middle East (UAE)",
    "me-south-1": "Middle East (Bahrain)",
    "il-central-1": "Israel (Tel Aviv)",
    "af-south-1": "Africa (Cape Town)",
}


def _normalize_instance_type(instance_type: str) -> str:
    """Ensure ml. prefix for SageMaker types."""
    if instance_type.startswith("ml."):
        return instance_type
    return f"ml.{instance_type}"


def _pricing_client():
    # Pricing API responses are identical regardless of caller account —
    # they carry the public rate card. Always use the deploy account so we
    # don't force customer-side roles to grant pricing:GetProducts and
    # don't waste an STS call on every price lookup.
    return get_client("pricing", PRICING_API_REGION, use_deploy_account=True)


def _extract_hourly_price(pricing_terms_json: str) -> float | None:
    """Pull the OnDemand USD/hour rate from a Pricing API priceList entry."""
    try:
        terms = (
            json.loads(pricing_terms_json)
            if isinstance(pricing_terms_json, str)
            else pricing_terms_json
        )
        on_demand = terms.get("OnDemand", {})
        for _, term in on_demand.items():
            for _, dim in term.get("priceDimensions", {}).items():
                rate = dim.get("pricePerUnit", {}).get("USD")
                if rate is not None:
                    return float(rate)
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None
    return None


def get_sagemaker_pricing(
    instance_type: str,
    region: str,
    component: str = "Hosting",
) -> dict[str, Any]:
    """Look up SageMaker on-demand hourly price for a given instance + region.

    Args:
        instance_type: e.g., "ml.g7e.2xlarge" or "g7e.2xlarge" (ml. added if missing)
        region: AWS region code, e.g., "sa-east-1"
        component: SageMaker component — "Hosting" (default), "Training", "Processing", "AsyncInf"

    Returns:
        {
            "instance_type": str,
            "region": str,
            "component": str,
            "hourly_usd": float | None,    # None if not priced in region
            "available": bool,             # True if at least one matching SKU returned
            "source": "AWS Pricing API",
            "_notes": str | None
        }
    """
    instance_type = _normalize_instance_type(instance_type)
    location = REGION_LOCATION.get(region)
    if location is None:
        return {
            "instance_type": instance_type,
            "region": region,
            "component": component,
            "hourly_usd": None,
            "available": False,
            "source": "AWS Pricing API",
            "_notes": f"Unknown region code: {region}",
        }

    client = _pricing_client()
    filters = [
        {"Type": "TERM_MATCH", "Field": "instanceName", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        {"Type": "TERM_MATCH", "Field": "platoinstancetype", "Value": component},
    ]

    try:
        response = client.get_products(
            ServiceCode="AmazonSageMaker",
            Filters=filters,
            MaxResults=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "instance_type": instance_type,
            "region": region,
            "component": component,
            "hourly_usd": None,
            "available": False,
            "source": "AWS Pricing API",
            "_notes": f"Pricing API error: {type(exc).__name__}: {exc}",
        }

    price_list = response.get("PriceList", [])
    if not price_list:
        return {
            "instance_type": instance_type,
            "region": region,
            "component": component,
            "hourly_usd": None,
            "available": False,
            "source": "AWS Pricing API",
            "_notes": (
                f"No SageMaker {component} pricing for {instance_type} in {region}. "
                "Likely not yet GA in this region."
            ),
        }

    for entry_str in price_list:
        entry = json.loads(entry_str) if isinstance(entry_str, str) else entry_str
        rate = _extract_hourly_price(entry.get("terms", {}))
        if rate is not None:
            return {
                "instance_type": instance_type,
                "region": region,
                "component": component,
                "hourly_usd": rate,
                "available": True,
                "source": "AWS Pricing API",
                "_notes": None,
            }

    return {
        "instance_type": instance_type,
        "region": region,
        "component": component,
        "hourly_usd": None,
        "available": False,
        "source": "AWS Pricing API",
        "_notes": "Found SKU records but no extractable hourly rate. Possibly tiered or upfront-only.",
    }


TOOL_SPEC = {
    "name": "get_sagemaker_pricing",
    "description": (
        "Get the on-demand hourly USD price for a SageMaker instance type in a "
        "specific region from the AWS Pricing API. "
        "Use this to estimate MRR/ARR or to verify whether an instance type is GA "
        "for SageMaker in a region. If hourly_usd is None and available is False, "
        "the instance type is NOT publicly priced in that region (often means not GA)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": "Instance type, with or without ml. prefix (e.g., 'g7e.2xlarge' or 'ml.g7e.2xlarge')",
            },
            "region": {
                "type": "string",
                "description": "AWS region code, e.g., 'sa-east-1', 'us-west-2', 'ap-south-2'",
            },
            "component": {
                "type": "string",
                "description": "SageMaker component. One of: Hosting (default), Training, Processing, AsyncInf",
                "default": "Hosting",
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_sagemaker_pricing,
}
