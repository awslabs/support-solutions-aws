# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Service Quotas tool — checks a specific SageMaker endpoint quota.

SageMaker publishes MULTIPLE endpoint-usage quotas per instance type,
one per endpoint variant:

- Real-time endpoint:          ``ml.<type> for endpoint usage``
- Asynchronous inference:      ``ml.<type> for async inference endpoint usage``
- Shadow endpoint:             ``ml.<type> for shadow endpoint usage``
- Batch transform:             ``ml.<type> for batch transform job usage``
- (Serverless inference)       has a global memory-based quota, not
                                per-instance-type — call this tool with
                                ``endpoint_variant="serverless"`` and the
                                tool will return a helpful message.

The prior version of this tool matched on substring alone and picked the
first response the Service Quotas API returned — non-deterministic and
frequently wrong (a customer deploying real-time endpoints could receive
their async-inference quota as the reported limit, or vice versa). This
rewrite requires the caller to name the endpoint variant they care about
and matches on the CANONICAL Service Quotas name string exactly.

IAM permission required: ``servicequotas:ListServiceQuotas``.
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client


# SageMaker service code used by the Service Quotas API.
SAGEMAKER_SERVICE_CODE = "sagemaker"

# Canonical Service Quotas ``QuotaName`` templates per endpoint variant.
# ``{type}`` is filled with the ``ml.<...>`` instance type. Match is
# case-insensitive on the QuotaName field returned by the API.
_QUOTA_NAME_TEMPLATES: dict[str, str] = {
    "realtime": "{type} for endpoint usage",
    "async": "{type} for async inference endpoint usage",
    "shadow": "{type} for shadow endpoint usage",
    "batch": "{type} for batch transform job usage",
}

# All accepted variant tokens. ``serverless`` is a valid input but has no
# per-instance-type quota to look up — we return an explanatory result.
_VALID_VARIANTS = tuple(_QUOTA_NAME_TEMPLATES.keys()) + ("serverless",)


def _sq_client(region: str):
    return get_client("service-quotas", region)


def get_sagemaker_quota(
    instance_type: str,
    region: str,
    endpoint_variant: str = "realtime",
) -> dict[str, Any]:
    """Get the SageMaker endpoint quota for a specific instance type +
    variant.

    Args:
        instance_type: e.g. ``"ml.g5.2xlarge"`` or ``"g5.2xlarge"``. The
            ``ml.`` prefix is added automatically if missing — Service
            Quotas names use the ``ml.`` form.
        region: AWS region code.
        endpoint_variant: One of ``"realtime"`` (default), ``"async"``,
            ``"shadow"``, ``"batch"``, or ``"serverless"``. Serverless
            inference has no per-instance-type quota — request it and
            the tool returns a note explaining why.

    Returns:
        dict with:
            instance_type: normalized (with ml. prefix)
            region: AWS region
            endpoint_variant: the variant queried
            found: bool (True when a canonical match was found)
            quota_code / quota_name / current_limit / adjustable: from the
                matched quota, when found
            candidates: any other quotas matching this instance type,
                surfaced so the caller can debug when the variant they
                asked for wasn't found but siblings were
            interpretation: human-readable summary
            source: API citation
    """
    ml_type = (
        instance_type if instance_type.startswith("ml.") else f"ml.{instance_type}"
    )

    if endpoint_variant not in _VALID_VARIANTS:
        return {
            "instance_type": ml_type,
            "region": region,
            "endpoint_variant": endpoint_variant,
            "found": False,
            "interpretation": (
                f"Invalid endpoint_variant={endpoint_variant!r}. Must be one "
                f"of {_VALID_VARIANTS}."
            ),
            "source": "service-quotas.ListServiceQuotas",
        }

    if endpoint_variant == "serverless":
        return {
            "instance_type": ml_type,
            "region": region,
            "endpoint_variant": endpoint_variant,
            "found": False,
            "interpretation": (
                "SageMaker Serverless Inference does NOT have a per-instance-"
                "type quota. The relevant quotas are `Maximum number of "
                "endpoints per Region` and the global memory / concurrency "
                "budget. Query those directly via the Service Quotas console "
                "or omit this call for serverless workloads."
            ),
            "source": "service-quotas.ListServiceQuotas",
        }

    target_name = _QUOTA_NAME_TEMPLATES[endpoint_variant].format(type=ml_type)
    target_name_lower = target_name.lower()

    try:
        client = _sq_client(region)
        paginator = client.get_paginator("list_service_quotas")

        primary: dict[str, Any] | None = None
        candidates: list[dict[str, Any]] = []

        for page in paginator.paginate(ServiceCode=SAGEMAKER_SERVICE_CODE):
            for quota in page.get("Quotas", []):
                quota_name = quota.get("QuotaName", "") or ""
                quota_name_lower = quota_name.lower()

                if quota_name_lower == target_name_lower:
                    # Canonical exact match — capture and keep looking only
                    # to fill candidates for diagnostic output.
                    if primary is None:
                        primary = {
                            "quota_code": quota.get("QuotaCode"),
                            "quota_name": quota_name,
                            "value": quota.get("Value"),
                            "adjustable": quota.get("Adjustable", False),
                            "unit": quota.get("Unit", "None"),
                        }
                    continue

                # Track sibling quotas that mention the same instance type
                # for debugging when the requested variant isn't found.
                if ml_type.lower() in quota_name_lower and (
                    "endpoint" in quota_name_lower or "transform" in quota_name_lower
                ):
                    candidates.append(
                        {
                            "quota_code": quota.get("QuotaCode"),
                            "quota_name": quota_name,
                            "value": quota.get("Value"),
                        }
                    )

        if primary is None:
            return {
                "instance_type": ml_type,
                "region": region,
                "endpoint_variant": endpoint_variant,
                "found": False,
                "quota_name_expected": target_name,
                "candidates": candidates,
                "interpretation": (
                    f"No Service Quotas entry named {target_name!r} in "
                    f"{region}. The instance type may not support this "
                    "endpoint variant in this region, or Service Quotas has "
                    "not yet published this quota for the account. "
                    + (
                        f"Sibling quotas mentioning {ml_type}: "
                        f"{[c['quota_name'] for c in candidates]}"
                        if candidates
                        else "No sibling quotas mentioning this instance type."
                    )
                ),
                "source": "service-quotas.ListServiceQuotas",
            }

        return {
            "instance_type": ml_type,
            "region": region,
            "endpoint_variant": endpoint_variant,
            "found": True,
            "quota_code": primary["quota_code"],
            "quota_name": primary["quota_name"],
            "current_limit": primary["value"],
            "adjustable": primary["adjustable"],
            "candidates": candidates,
            "interpretation": (
                f"{ml_type} {endpoint_variant} endpoint quota in {region}: "
                f"{int(primary['value'])} instances. "
                f"{'Adjustable via Service Quotas console.' if primary['adjustable'] else 'Not adjustable.'}"
            ),
            "source": "service-quotas.ListServiceQuotas",
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "instance_type": ml_type,
            "region": region,
            "endpoint_variant": endpoint_variant,
            "found": False,
            "interpretation": f"API error: {type(exc).__name__}: {exc}",
            "source": "service-quotas.ListServiceQuotas",
        }


TOOL_SPEC = {
    "name": "get_sagemaker_quota",
    "description": (
        "Get the SageMaker quota (limit) for a specific instance type + "
        "endpoint variant in a region. SageMaker publishes DIFFERENT quotas "
        "for different endpoint variants (real-time, async inference, "
        "shadow, batch transform); pass endpoint_variant to disambiguate. "
        "Default is 'realtime'. Returns the applied quota value, whether "
        "it's adjustable, and a list of sibling quotas mentioning the "
        "same instance type (helpful when the requested variant doesn't "
        "exist yet). Use this to determine if the customer's requested "
        "instance count exceeds their current limit for the deployment "
        "target they intend."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "SageMaker instance type (e.g., 'ml.g7e.2xlarge' or 'g7e.2xlarge'). "
                    "ml. prefix added automatically if missing."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region code (e.g., 'us-east-1', 'sa-east-1')",
            },
            "endpoint_variant": {
                "type": "string",
                "description": (
                    "Which endpoint variant's quota to look up. "
                    "'realtime' (default) → 'ml.X for endpoint usage'. "
                    "'async' → async-inference quota. "
                    "'shadow' → shadow-endpoint quota. "
                    "'batch' → batch-transform-job quota. "
                    "'serverless' returns an explanatory note (no per-instance quota)."
                ),
                "enum": list(_VALID_VARIANTS),
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_sagemaker_quota,
}
