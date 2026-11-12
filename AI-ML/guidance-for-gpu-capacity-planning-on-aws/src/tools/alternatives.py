# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Instance alternatives tool — finds similar GPU instances available in a region.

Uses public EC2 DescribeInstanceTypes + DescribeInstanceTypeOfferings to find
alternative instance types with similar GPU/memory characteristics that ARE
available in the target region.
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
except ImportError:
    from src.aws_session import get_client


def _ec2_client(region: str):
    return get_client("ec2", region)


# GPU instance families relevant to ML inference
GPU_FAMILIES = [
    "g5",
    "g6",
    "g6e",
    "g7",
    "g7e",
    "p4d",
    "p5",
    "p5en",
    "p6-b200",
    "p6-b300",
    "inf2",
    "trn1",
]


def find_alternative_instances(
    instance_type: str, region: str, max_results: int = 5
) -> dict[str, Any]:
    """Find alternative GPU instances available in a region, ranked by similarity.

    Args:
        instance_type: Reference instance type (e.g., "g7e.2xlarge")
        region: Target AWS region
        max_results: Max alternatives to return (default 5)

    Returns:
        dict with ranked alternatives including specs and availability.
    """
    bare_type = instance_type.removeprefix("ml.")

    try:
        # Get specs of the reference instance (from us-east-1 if not in target region)
        ref_specs = _get_instance_specs(bare_type, region)
        if ref_specs is None:
            # Try us-east-1 as fallback for spec lookup
            ref_specs = _get_instance_specs(bare_type, "us-east-1")
        if ref_specs is None:
            return {
                "reference": bare_type,
                "region": region,
                "alternatives": [],
                "interpretation": f"Cannot find specs for {bare_type}.",
                "source": "ec2.DescribeInstanceTypes",
            }

        # Get all GPU instances offered in the target region
        offered = _get_offered_gpu_types(region)
        if not offered:
            return {
                "reference": bare_type,
                "region": region,
                "alternatives": [],
                "interpretation": f"No GPU instances found in {region}.",
                "source": "ec2.DescribeInstanceTypeOfferings",
            }

        # Get specs for all offered types and score similarity
        candidates = []
        for candidate_type in offered:
            if candidate_type == bare_type:
                continue
            specs = _get_instance_specs(candidate_type, region)
            if specs is None:
                continue
            score = _similarity_score(ref_specs, specs)
            candidates.append(
                {
                    "instance_type": candidate_type,
                    "vcpus": specs["vcpus"],
                    "memory_gib": specs["memory_gib"],
                    "gpus": specs["gpus"],
                    "gpu_memory_gib": specs["gpu_memory_gib"],
                    "gpu_manufacturer": specs["gpu_manufacturer"],
                    "similarity_score": round(score, 2),
                }
            )

        # Sort by similarity (higher = more similar)
        candidates.sort(key=lambda x: x["similarity_score"], reverse=True)
        top = candidates[:max_results]

        return {
            "reference": bare_type,
            "reference_specs": ref_specs,
            "region": region,
            "alternatives": top,
            "interpretation": (
                f"Top {len(top)} alternatives to {bare_type} available in {region}: "
                + ", ".join(
                    f"{a['instance_type']} (score {a['similarity_score']})" for a in top
                )
            ),
            "source": "ec2.DescribeInstanceTypes + DescribeInstanceTypeOfferings",
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "reference": bare_type,
            "region": region,
            "alternatives": [],
            "interpretation": f"API error: {type(exc).__name__}: {exc}",
            "source": "ec2.DescribeInstanceTypes",
        }


def _get_instance_specs(instance_type: str, region: str) -> dict[str, Any] | None:
    """Get vCPU, memory, GPU specs for an instance type."""
    try:
        client = _ec2_client(region)
        resp = client.describe_instance_types(InstanceTypes=[instance_type])
        types = resp.get("InstanceTypes", [])
        if not types:
            return None
        t = types[0]
        gpus = t.get("GpuInfo", {}).get("Gpus", [{}])
        gpu_info = gpus[0] if gpus else {}
        return {
            "instance_type": instance_type,
            "vcpus": t.get("VCpuInfo", {}).get("DefaultVCpus", 0),
            "memory_gib": round(t.get("MemoryInfo", {}).get("SizeInMiB", 0) / 1024, 1),
            "gpus": gpu_info.get("Count", 0),
            "gpu_memory_gib": round(
                (gpu_info.get("MemoryInfo", {}).get("SizeInMiB", 0) or 0) / 1024, 1
            ),
            "gpu_manufacturer": gpu_info.get("Manufacturer", "unknown"),
        }
    except Exception:  # noqa: BLE001
        return None


def _get_offered_gpu_types(region: str) -> list[str]:
    """Get all GPU-family instance types offered in a region."""
    client = _ec2_client(region)
    offered = set()
    paginator = client.get_paginator("describe_instance_type_offerings")
    for page in paginator.paginate(LocationType="region"):
        for o in page.get("InstanceTypeOfferings", []):
            itype = o["InstanceType"]
            family = itype.split(".")[0]
            if family in GPU_FAMILIES:
                offered.add(itype)
    return sorted(offered)


def _similarity_score(ref: dict, candidate: dict) -> float:
    """Score 0-1 based on weighted similarity of GPU specs."""
    scores = []

    # GPU count match (weight 0.3)
    if ref["gpus"] > 0 and candidate["gpus"] > 0:
        scores.append(
            0.3
            * min(ref["gpus"], candidate["gpus"])
            / max(ref["gpus"], candidate["gpus"])
        )
    elif ref["gpus"] == candidate["gpus"]:
        scores.append(0.3)
    else:
        scores.append(0.0)

    # GPU memory match (weight 0.3)
    if ref["gpu_memory_gib"] > 0 and candidate["gpu_memory_gib"] > 0:
        scores.append(
            0.3
            * min(ref["gpu_memory_gib"], candidate["gpu_memory_gib"])
            / max(ref["gpu_memory_gib"], candidate["gpu_memory_gib"])
        )
    else:
        scores.append(0.0)

    # vCPU match (weight 0.2)
    if ref["vcpus"] > 0 and candidate["vcpus"] > 0:
        scores.append(
            0.2
            * min(ref["vcpus"], candidate["vcpus"])
            / max(ref["vcpus"], candidate["vcpus"])
        )
    else:
        scores.append(0.0)

    # Memory match (weight 0.2)
    if ref["memory_gib"] > 0 and candidate["memory_gib"] > 0:
        scores.append(
            0.2
            * min(ref["memory_gib"], candidate["memory_gib"])
            / max(ref["memory_gib"], candidate["memory_gib"])
        )
    else:
        scores.append(0.0)

    return sum(scores)


TOOL_SPEC = {
    "name": "find_alternative_instances",
    "description": (
        "Find alternative GPU instance types available in a target region, "
        "ranked by similarity to a reference instance type. Uses GPU count, "
        "GPU memory, vCPUs, and system memory for scoring. Use this when the "
        "requested instance type is not available in the target region and you "
        "need to suggest alternatives the customer could use instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": "Reference instance type to find alternatives for (e.g., 'g7e.2xlarge')",
            },
            "region": {
                "type": "string",
                "description": "Target AWS region where alternatives must be available",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of alternatives to return (default: 5)",
                "default": 5,
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": find_alternative_instances,
}
