# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Spot Placement Score tool — proxy signal for real capacity availability.

Uses the public EC2 GetSpotPlacementScores API to assess how likely a fleet
of the requested instance type and size would be fulfilled in a region.

While this API is designed for Spot instances, the scores provide a useful
proxy for overall capacity depth in a region/AZ — a low score indicates
tight supply, which correlates with higher ICE (Insufficient Capacity Error)
risk for on-demand launches as well.

Score interpretation:
- 1-3: Very low capacity availability — high ICE risk
- 4-6: Moderate capacity — some risk for large fleets
- 7-9: Good capacity — low risk for moderate fleets
- 10:  Excellent capacity — very unlikely to encounter ICE

IAM permission required: ec2:GetSpotPlacementScores
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client


def _ec2_client(region: str):
    return get_client("ec2", region)


def get_spot_placement_score(
    instance_type: str,
    region: str,
    target_capacity: int = 1,
) -> dict[str, Any]:
    """Get Spot Placement Score as a proxy for capacity availability.

    Args:
        instance_type: EC2 instance type (e.g., "g5.2xlarge"). No ml. prefix.
        region: AWS region code (e.g., "us-east-1").
        target_capacity: Number of instances to assess capacity for (default 1).

    Returns:
        dict with placement scores per AZ, interpretation, and source citation.
    """
    bare_type = instance_type.removeprefix("ml.")

    try:
        client = _ec2_client(region)

        # GetSpotPlacementScores evaluates how likely a Spot request would be
        # fulfilled. We use it as a capacity depth proxy.
        resp = client.get_spot_placement_scores(
            InstanceTypes=[bare_type],
            TargetCapacity=max(1, min(target_capacity, 1000)),
            TargetCapacityUnitType="units",
            RegionNames=[region],
            SingleAvailabilityZone=False,
        )

        scores = resp.get("SpotPlacementScores", [])

        if not scores:
            return {
                "instance_type": bare_type,
                "region": region,
                "target_capacity": target_capacity,
                "score": None,
                "scores_by_az": [],
                "interpretation": (
                    f"No placement score data available for {bare_type} in {region}. "
                    "The instance type may not support Spot or may not be offered in this region."
                ),
                "capacity_signal": "unknown",
                "source": "ec2.GetSpotPlacementScores",
            }

        # Extract region-level score (non-AZ-specific)
        region_scores = [s for s in scores if not s.get("AvailabilityZoneId")]
        az_scores = [s for s in scores if s.get("AvailabilityZoneId")]

        # Use the region-level score if available, otherwise average AZ
        # scores. F10: the prior implementation used integer
        # floor-division which systematically biased averages downward
        # ([8, 7] became 7, not 7.5). Round to the nearest integer so
        # the score bands (>=8 strong, >=5 moderate, >=3 weak) get an
        # honest reading — a small bias would push customers into the
        # ODCR-recommended zone unnecessarily.
        if region_scores:
            primary_score = region_scores[0].get("Score", 0)
        elif az_scores:
            score_values = [int(s.get("Score", 0) or 0) for s in az_scores]
            avg = sum(score_values) / len(score_values)
            primary_score = int(round(avg))
        else:
            primary_score = scores[0].get("Score", 0)

        # Interpret the score
        if primary_score >= 8:
            capacity_signal = "strong"
            interpretation = (
                f"Spot Placement Score {primary_score}/10 for {target_capacity}× {bare_type} in {region}. "
                "Strong capacity signal — low risk of ICE (Insufficient Capacity Error) for this fleet size."
            )
        elif primary_score >= 5:
            capacity_signal = "moderate"
            interpretation = (
                f"Spot Placement Score {primary_score}/10 for {target_capacity}× {bare_type} in {region}. "
                "Moderate capacity signal — some ICE risk exists for larger fleets. "
                "Consider ODCR or phased deployment."
            )
        elif primary_score >= 3:
            capacity_signal = "weak"
            interpretation = (
                f"Spot Placement Score {primary_score}/10 for {target_capacity}× {bare_type} in {region}. "
                "Weak capacity signal — significant ICE risk. "
                "ODCR strongly recommended before launch. Do not deploy ad-hoc."
            )
        else:
            capacity_signal = "critical"
            interpretation = (
                f"Spot Placement Score {primary_score}/10 for {target_capacity}× {bare_type} in {region}. "
                "Critical — very high ICE risk. Region likely has minimal available capacity for this type. "
                "Consider alternative regions or instance families."
            )

        # Build AZ-level detail
        az_detail = [
            {
                "az": s.get("AvailabilityZoneId", "region-wide"),
                "region": s.get("Region", region),
                "score": s.get("Score", 0),
            }
            for s in scores
        ]

        return {
            "instance_type": bare_type,
            "region": region,
            "target_capacity": target_capacity,
            "score": primary_score,
            "scores_by_az": az_detail,
            "capacity_signal": capacity_signal,
            "interpretation": interpretation,
            "source": "ec2.GetSpotPlacementScores",
        }

    except Exception as exc:  # noqa: BLE001
        error_type = type(exc).__name__
        return {
            "instance_type": bare_type,
            "region": region,
            "target_capacity": target_capacity,
            "score": None,
            "scores_by_az": [],
            "capacity_signal": "unknown",
            "interpretation": (
                f"Unable to retrieve Spot Placement Score: {error_type}: {exc}. "
                "This may indicate insufficient IAM permissions (ec2:GetSpotPlacementScores) "
                "or the instance type is not eligible for Spot placement scoring."
            ),
            "source": "ec2.GetSpotPlacementScores",
        }


TOOL_SPEC = {
    "name": "get_spot_placement_score",
    "description": (
        "Get Spot Placement Score for an instance type in a region — this is a "
        "PROXY SIGNAL for real capacity availability. Scores range from 1 (very low "
        "capacity, high ICE risk) to 10 (excellent capacity, very low ICE risk). "
        "Use this AFTER confirming instance availability to assess whether a fleet "
        "of the requested size is likely to launch successfully. A score below 5 "
        "for a large fleet indicates ODCR or Capacity Blocks should be recommended. "
        "The target_capacity parameter should match the customer's requested fleet size."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g., 'g5.2xlarge', 'p4d.24xlarge'). "
                    "Do not include ml. prefix."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region code (e.g., 'us-east-1', 'sa-east-1')",
            },
            "target_capacity": {
                "type": "integer",
                "description": (
                    "Number of instances to assess capacity for. Use the customer's "
                    "requested fleet size (e.g., 22 for '22 instances'). Default 1."
                ),
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_spot_placement_score,
}
