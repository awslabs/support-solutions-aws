# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Region capacity maturity model.

Externalizes region classification from static prompt text into a
data-driven, independently updatable model. Supports per-GPU-family
capacity tiers — because a region might have strong g5 capacity but
no p5 at all.

The model can be updated without code changes by modifying the
REGION_CAPACITY_DATA dict (or loading from a JSON config file / DynamoDB
in a future version).

Capacity tiers:
- "established": Deep capacity, low ICE risk, multiple AZs with GPU pools
- "moderate": Adequate capacity for moderate fleets, some ICE risk for large asks
- "limited": Shallow capacity, high ICE risk for fleets >10, newer GPU families may not be GA
- "none": Instance family not available in this region
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Default region capacity data — updatable via external JSON file
# Structure: region → gpu_family → tier
# This can be overridden by placing a region_capacity.json file in the config directory
REGION_CAPACITY_DATA: dict[str, dict[str, str]] = {
    # Established high-capacity regions
    "us-east-1": {
        "g5": "established",
        "g6": "established",
        "g6e": "established",
        "p4d": "established",
        "p4de": "established",
        "p5": "established",
        "p5e": "moderate",
        "inf2": "established",
        "trn1": "established",
        "_overall": "established",
    },
    "us-west-2": {
        "g5": "established",
        "g6": "established",
        "g6e": "established",
        "p4d": "established",
        "p4de": "established",
        "p5": "established",
        "p5e": "moderate",
        "inf2": "established",
        "trn1": "established",
        "_overall": "established",
    },
    "eu-west-1": {
        "g5": "established",
        "g6": "established",
        "g6e": "moderate",
        "p4d": "established",
        "p4de": "moderate",
        "p5": "moderate",
        "inf2": "established",
        "trn1": "moderate",
        "_overall": "established",
    },
    "ap-northeast-1": {
        "g5": "established",
        "g6": "established",
        "g6e": "moderate",
        "p4d": "established",
        "p5": "moderate",
        "inf2": "moderate",
        "trn1": "limited",
        "_overall": "established",
    },
    # Moderate-capacity regions
    "eu-central-1": {
        "g5": "established",
        "g6": "moderate",
        "g6e": "limited",
        "p4d": "moderate",
        "p5": "limited",
        "inf2": "moderate",
        "_overall": "moderate",
    },
    "ap-southeast-1": {
        "g5": "established",
        "g6": "moderate",
        "p4d": "moderate",
        "p5": "limited",
        "inf2": "moderate",
        "_overall": "moderate",
    },
    "ap-southeast-2": {
        "g5": "established",
        "g6": "moderate",
        "p4d": "moderate",
        "p5": "limited",
        "inf2": "moderate",
        "_overall": "moderate",
    },
    "ca-central-1": {
        "g5": "moderate",
        "g6": "limited",
        "p4d": "limited",
        "_overall": "moderate",
    },
    "ap-south-1": {
        "g5": "moderate",
        "g6": "moderate",
        "p4d": "limited",
        "p5": "limited",
        "inf2": "limited",
        "_overall": "moderate",
    },
    "us-east-2": {
        "g5": "established",
        "g6": "moderate",
        "p4d": "moderate",
        "p5": "limited",
        "_overall": "moderate",
    },
    "eu-west-2": {
        "g5": "moderate",
        "g6": "moderate",
        "p4d": "moderate",
        "p5": "limited",
        "_overall": "moderate",
    },
    "ap-northeast-2": {
        "g5": "moderate",
        "g6": "limited",
        "p4d": "limited",
        "_overall": "moderate",
    },
    # Limited-capacity / newer regions
    "sa-east-1": {
        "g5": "moderate",
        "g6": "limited",
        "g6e": "none",
        "p4d": "limited",
        "p5": "none",
        "p5e": "none",
        "inf2": "none",
        "trn1": "none",
        "_overall": "limited",
    },
    "af-south-1": {
        "g5": "limited",
        "g6": "none",
        "g6e": "none",
        "p4d": "limited",
        "p5": "none",
        "p5e": "none",
        "inf2": "none",
        "trn1": "none",
        "_overall": "limited",
    },
    "me-south-1": {
        "g5": "limited",
        "g6": "none",
        "p4d": "none",
        "p5": "none",
        "_overall": "limited",
    },
    "ap-south-2": {
        "g5": "limited",
        "g6": "none",
        "p4d": "none",
        "_overall": "limited",
    },
    "eu-south-1": {
        "g5": "limited",
        "g6": "limited",
        "p4d": "none",
        "p5": "none",
        "_overall": "limited",
    },
    "eu-south-2": {
        "g5": "limited",
        "g6": "none",
        "_overall": "limited",
    },
    "me-central-1": {
        "g5": "limited",
        "g6": "none",
        "p4d": "none",
        "_overall": "limited",
    },
    "il-central-1": {
        "g5": "limited",
        "g6": "limited",
        "p4d": "none",
        "_overall": "limited",
    },
}

# Tier → numeric score for confidence calculations
TIER_SCORES = {
    "established": 1.0,
    "moderate": 0.6,
    "limited": 0.3,
    "none": 0.0,
}


def _load_custom_data() -> dict[str, dict[str, str]] | None:
    """Load custom region capacity data from JSON file if available."""
    # Check for override file in project root or config directory
    for path in [
        Path(os.environ.get("REGION_CAPACITY_FILE", "")),
        Path(__file__).parent.parent / "region_capacity.json",
        Path(__file__).parent / "region_capacity.json",
    ]:
        if path.is_file():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _get_data() -> dict[str, dict[str, str]]:
    """Get region capacity data, preferring external file over hardcoded."""
    custom = _load_custom_data()
    if custom:
        return custom
    return REGION_CAPACITY_DATA


def get_region_tier(region: str, gpu_family: str | None = None) -> str:
    """Get the capacity tier for a region and optional GPU family.

    Args:
        region: AWS region code (e.g., "af-south-1")
        gpu_family: GPU family prefix (e.g., "g5", "p4d", "g6e").
                    If None, returns the overall region tier.

    Returns:
        One of: "established", "moderate", "limited", "none", "unknown"
    """
    data = _get_data()
    region_data = data.get(region, {})

    if not region_data:
        return "unknown"

    if gpu_family:
        # Normalize family (e.g., "g5.2xlarge" → "g5")
        family = gpu_family.split(".")[0].removeprefix("ml.")
        return region_data.get(family, region_data.get("_overall", "unknown"))

    return region_data.get("_overall", "unknown")


def get_region_score(region: str, gpu_family: str | None = None) -> float:
    """Get numeric capacity score (0.0-1.0) for a region and GPU family.

    Used by the confidence scoring module.
    """
    tier = get_region_tier(region, gpu_family)
    return TIER_SCORES.get(tier, 0.5)


def get_region_assessment(region: str, instance_type: str) -> dict[str, Any]:
    """Get a full capacity assessment for a region + instance type.

    Returns structured data the agent can include in its risk assessment.
    """
    # Extract GPU family from instance type
    bare_type = instance_type.removeprefix("ml.")
    family = bare_type.split(".")[0]

    data = _get_data()
    region_data = data.get(region, {})

    overall_tier = region_data.get("_overall", "unknown")
    family_tier = region_data.get(family, "unknown")
    family_score = TIER_SCORES.get(family_tier, 0.5)
    overall_score = TIER_SCORES.get(overall_tier, 0.5)

    # Determine risk factors
    risk_factors = []
    if overall_tier == "limited":
        risk_factors.append(
            "Region is newer/limited-capacity — smaller GPU pools overall"
        )
    if family_tier == "none":
        risk_factors.append(f"{family} family NOT available in this region")
    elif family_tier == "limited":
        risk_factors.append(
            f"{family} capacity is shallow — high ICE risk for fleets >10"
        )
    elif family_tier == "moderate":
        risk_factors.append(
            f"{family} capacity adequate for moderate fleets, monitor for large asks"
        )

    # Recommendations based on tier
    recommendations = []
    if family_tier in ("none", "limited"):
        recommendations.append("Pre-create ODCR before launch — do not deploy ad-hoc")
        recommendations.append(
            "Consider alternative regions with deeper capacity for this family"
        )
    if family_tier == "none":
        recommendations.append(
            f"Check if {family} has a planned GA date in {region} (consult account team)"
        )

    return {
        "region": region,
        "instance_family": family,
        "overall_tier": overall_tier,
        "family_tier": family_tier,
        "overall_score": overall_score,
        "family_score": family_score,
        "risk_factors": risk_factors,
        "recommendations": recommendations,
        "source": "region_capacity_model (updatable via region_capacity.json)",
    }
