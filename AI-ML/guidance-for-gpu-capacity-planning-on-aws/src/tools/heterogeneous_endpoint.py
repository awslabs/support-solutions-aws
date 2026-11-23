# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Heterogeneous endpoint fallback evaluator — scores primary + fallback
instance pairs for use in a SageMaker capacity-aware endpoint.

Problem this tool solves. A capacity planner wants to deploy a model on
``ml.g6e.xlarge`` but that instance type isn't dependably available in
the target region. SageMaker supports a "capacity-aware" deployment
pattern where one endpoint carries multiple instance pools and
SageMaker's control plane picks whichever pool has launchable capacity
at request time:

- https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-heterogeneous.html
- https://aws.amazon.com/blogs/machine-learning/capacity-aware-inference-automatic-instance-fallback-for-sagemaker-ai-endpoints/

Picking a *correct* fallback is non-trivial. The fallback has to:
    1. Be offered in the target region (hard requirement).
    2. Be SageMaker-GA in the target region (hard requirement — if the
       Pricing API has no record, the instance can't carry a SageMaker
       endpoint there).
    3. Match on GPU vendor (hard requirement — model binaries compiled
       for CUDA will not run on AWS Neuron, and vice versa).
    4. Hold the model with reasonable headroom, which usually means
       bumping tensor parallelism (TP) on the fallback. A 7B model
       deployed on a g6e.xlarge at TP=1 may need TP=2 on a g7.12xlarge
       so the model spreads across both of the fallback's GPUs.
    5. Not blow up the cost model — SageMaker only consumes a pool when
       it provisions it, but the customer still has to budget for
       worst-case failover.

This tool wraps those checks behind one agent-callable function. It
calls three public AWS APIs — ``ec2:DescribeInstanceTypes``,
``ec2:DescribeInstanceTypeOfferings``, ``pricing:GetProducts`` — ranks
candidate fallbacks, computes a per-pair pairing score and a TP
recommendation for the fallback, and emits a ready-to-paste
``ProductionVariants`` config block for the SageMaker CreateEndpointConfig
API. Deliberately returns the raw signals too so the agent can narrate
the "why" behind each ranking.

IAM permissions required (all already in the Lambda role for the
sibling tools): ``ec2:DescribeInstanceTypes``,
``ec2:DescribeInstanceTypeOfferings``, ``pricing:GetProducts``.
"""

from __future__ import annotations

from typing import Any

try:
    from aws_session import get_client
    from tools.pricing import get_sagemaker_pricing
except ImportError:
    from src.aws_session import get_client
    from src.tools.pricing import get_sagemaker_pricing


# ---------- GPU inference instance families ----------
# Instances the tool is willing to consider for capacity-aware pairing.
# Non-GPU families and video / specialty families (nova-reel-equivalent
# compute) are excluded. Keep in sync with
# ``src/tools/alternatives.py::GPU_FAMILIES``.
_GPU_FAMILIES: tuple[str, ...] = (
    "g5",
    "g6",
    "g6e",
    "g7",
    "g7e",
    "p4d",
    "p4de",
    "p5",
    "p5e",
    "p5en",
    "p6-b200",
    "p6-b300",
    "inf1",
    "inf2",
    "trn1",
    "trn2",
)


# ---------- Model-memory heuristics (FP16, inference) ----------
# Approximate: 1B params ≈ 2 GB at FP16.
# KV cache + runtime overhead: ~30% on top of model weights for typical
# serving (batch=1-4, context 2K-8K). Larger batches or longer context
# push this higher; the heuristic is a STARTING POINT only — the
# tool's output includes a disclaimer directing the customer to
# validate TP configuration with real traffic.
_FP16_BYTES_PER_PARAM = 2
_KV_CACHE_OVERHEAD_FACTOR = 1.30
_MEMORY_UTILIZATION_TARGET = 0.85  # fill up to 85% of per-GPU VRAM
_TP_SPLITS: tuple[int, ...] = (1, 2, 4, 8, 16, 32)


def _ec2_client(region: str):
    return get_client("ec2", region)


# ---------- Instance spec lookup ----------


def _normalize_bare(instance_type: str) -> str:
    """Strip ``ml.`` and lowercase — the EC2 API expects bare names."""
    return instance_type.removeprefix("ml.").lower()


def _family_of(bare_type: str) -> str:
    return bare_type.split(".")[0]


def _get_instance_specs(bare_type: str, region: str) -> dict[str, Any] | None:
    """Pull vCPU / memory / GPU specs for a single instance type.

    Returns None when the type is unknown in the target region. Falls
    back to us-east-1 for the spec lookup only when the target region
    doesn't offer it (specs are region-independent in practice, but
    DescribeInstanceTypes needs a region to execute in).
    """
    for lookup_region in (region, "us-east-1"):
        try:
            client = _ec2_client(lookup_region)
            resp = client.describe_instance_types(InstanceTypes=[bare_type])
        except Exception:  # noqa: BLE001  # nosec B112 - best-effort per-type lookup; skip on error
            continue
        types = resp.get("InstanceTypes", [])
        if not types:
            continue
        t = types[0]
        gpu_info = t.get("GpuInfo") or {}
        gpus = gpu_info.get("Gpus") or [{}]
        g = gpus[0]
        total_gpu_memory_mib = gpu_info.get("TotalGpuMemoryInMiB")
        if total_gpu_memory_mib is None:
            # Older API shape: sum per-GPU memory * count.
            total_gpu_memory_mib = (g.get("MemoryInfo") or {}).get(
                "SizeInMiB", 0
            ) * g.get("Count", 0)
        return {
            "instance_type": bare_type,
            "vcpus": (t.get("VCpuInfo") or {}).get("DefaultVCpus", 0),
            "memory_gib": round(
                (t.get("MemoryInfo") or {}).get("SizeInMiB", 0) / 1024, 1
            ),
            "gpu_count": g.get("Count", 0),
            "gpu_memory_gb_per_gpu": round(
                ((g.get("MemoryInfo") or {}).get("SizeInMiB", 0) or 0) / 1024, 1
            ),
            "gpu_total_memory_gb": round((total_gpu_memory_mib or 0) / 1024, 1),
            "gpu_manufacturer": g.get("Manufacturer") or "unknown",
            "gpu_name": g.get("Name") or "unknown",
        }
    return None


def _offered_gpu_types_in_region(region: str) -> set[str]:
    """Set of GPU instance type bare names offered in the region."""
    try:
        client = _ec2_client(region)
        paginator = client.get_paginator("describe_instance_type_offerings")
        offered: set[str] = set()
        for page in paginator.paginate(LocationType="region"):
            for o in page.get("InstanceTypeOfferings", []):
                itype = o["InstanceType"]
                if _family_of(itype) in _GPU_FAMILIES:
                    offered.add(itype)
        return offered
    except Exception:  # noqa: BLE001
        return set()


# ---------- Tensor-parallelism recommendation ----------


def _recommend_tp(
    model_size_b: float | None,
    gpu_count: int,
    gpu_memory_gb_per_gpu: float,
) -> dict[str, Any]:
    """Pick the smallest TP that fits the model with KV-cache headroom.

    Returns a structured hint, NOT a prescription — serving-stack
    details (batch size, context length, quantization, speculative
    decoding) can shift the real minimum by a factor of 2 either way.
    The tool's human-readable interpretation makes this explicit.
    """
    if model_size_b is None:
        return {
            "model_size_b": None,
            "fits": None,
            "note": (
                "No model_size_b supplied — TP recommendation skipped. "
                "Call this tool with model_size_b (billions of parameters, "
                "e.g. 7.0 for a 7B model) to get a per-fallback TP hint."
            ),
        }

    model_memory_gb = model_size_b * _FP16_BYTES_PER_PARAM
    total_needed_gb = round(model_memory_gb * _KV_CACHE_OVERHEAD_FACTOR, 2)
    usable_per_gpu_gb = round(gpu_memory_gb_per_gpu * _MEMORY_UTILIZATION_TARGET, 2)

    if gpu_count <= 0 or gpu_memory_gb_per_gpu <= 0:
        return {
            "model_size_b": model_size_b,
            "estimated_memory_gb": total_needed_gb,
            "fits": False,
            "min_tp_to_fit": None,
            "max_tp_available": 0,
            "recommended_tp": None,
            "note": (
                "Instance reports zero GPUs or zero GPU memory — cannot "
                "host a model at any TP level. This is almost certainly a "
                "non-ML instance type reaching this code path."
            ),
        }

    for tp in _TP_SPLITS:
        if tp > gpu_count:
            break
        per_gpu_needed = total_needed_gb / tp
        if per_gpu_needed <= usable_per_gpu_gb:
            return {
                "model_size_b": model_size_b,
                "estimated_memory_gb": total_needed_gb,
                "fits": True,
                "min_tp_to_fit": tp,
                "max_tp_available": gpu_count,
                "recommended_tp": tp,
                "memory_per_gpu_gb_at_recommended_tp": round(per_gpu_needed, 2),
                "memory_utilization_at_recommended_tp": round(
                    per_gpu_needed / gpu_memory_gb_per_gpu, 2
                ),
                "note": (
                    f"{model_size_b}B at FP16 ≈ {total_needed_gb} GB with "
                    f"{int((_KV_CACHE_OVERHEAD_FACTOR - 1) * 100)}% KV-cache overhead. "
                    f"Fits at TP={tp} ({round(per_gpu_needed, 1)} GB per GPU, "
                    f"{int(per_gpu_needed / gpu_memory_gb_per_gpu * 100)}% VRAM used). "
                    "Validate with real traffic — batch size and context "
                    "length can shift the real minimum."
                ),
            }

    return {
        "model_size_b": model_size_b,
        "estimated_memory_gb": total_needed_gb,
        "fits": False,
        "min_tp_to_fit": None,
        "max_tp_available": gpu_count,
        "recommended_tp": None,
        "note": (
            f"{model_size_b}B at FP16 (~{total_needed_gb} GB) does not fit "
            f"at any TP level up to {gpu_count} on this instance "
            f"({gpu_memory_gb_per_gpu} GB per GPU). Options: (a) switch to "
            "a GPU family with more per-GPU VRAM, (b) quantize to INT8 "
            "(~2x smaller) or INT4 (~4x smaller), (c) switch to a bigger "
            "instance in the same family (more GPUs)."
        ),
    }


# ---------- Pairing score ----------


def _pairing_score(
    primary: dict[str, Any],
    fallback_specs: dict[str, Any],
    fallback_price: float | None,
    primary_price: float | None,
    available_in_region: bool,
    sagemaker_priced: bool,
) -> tuple[float, list[str]]:
    """Composite 0-1 score for the pairing, plus concerns the agent
    should surface in its narrative.

    Hard disqualifiers (score = 0):
        - Cross-vendor GPU (NVIDIA vs. AWS Neuron, etc.)
        - Not offered in region
        - Not SageMaker-priced in region (confirms SM-GA)

    Soft scoring (70% memory fit + 30% cost ratio):
        - memory_fit = min(1, fallback_total / primary_total)
        - cost_score = 1 - min(1, (ratio - 1) / 2); 1.0 at same cost or
          cheaper, 0 at 3x primary cost. Linear in between.
    """
    concerns: list[str] = []
    if (
        primary["gpu_manufacturer"].lower()
        != fallback_specs["gpu_manufacturer"].lower()
    ):
        concerns.append(
            f"GPU vendor mismatch: primary is {primary['gpu_manufacturer']}, "
            f"fallback is {fallback_specs['gpu_manufacturer']}. Model binaries "
            "do NOT port across vendors (CUDA ↔ Neuron are incompatible). "
            "Disqualified from capacity-aware pairing."
        )
        return 0.0, concerns
    if not available_in_region:
        concerns.append(
            "Fallback instance type is not offered in the target region. Disqualified."
        )
        return 0.0, concerns
    if not sagemaker_priced:
        concerns.append(
            "AWS Pricing API has no SageMaker Hosting record for this "
            "fallback in the target region. Likely not SageMaker-GA here. "
            "Disqualified."
        )
        return 0.0, concerns

    primary_total = primary["gpu_total_memory_gb"]
    fallback_total = fallback_specs["gpu_total_memory_gb"]
    if primary_total <= 0:
        memory_fit = 0.5
    else:
        memory_fit = min(1.0, fallback_total / primary_total)

    if memory_fit < 0.5:
        concerns.append(
            f"Fallback has {fallback_total} GB total GPU memory vs. "
            f"primary's {primary_total} GB — may require aggressive TP "
            "splitting or model quantization. Validate the model fits "
            "before enabling this pool in production."
        )

    if primary_price is None or fallback_price is None or primary_price <= 0:
        cost_score = 0.5
        concerns.append(
            "Could not compute cost delta — one of the prices was "
            "unavailable from the Pricing API."
        )
    else:
        ratio = fallback_price / primary_price
        cost_score = max(0.0, 1.0 - min(1.0, (ratio - 1.0) / 2.0))
        if ratio > 2.0:
            concerns.append(
                f"Fallback hourly cost is {round(ratio, 2)}x primary. "
                "Keep in mind SageMaker only bills the pool it actually "
                "provisions — if primary is almost always available, the "
                "fallback is cheap insurance."
            )

    score = round(0.70 * memory_fit + 0.30 * cost_score, 3)
    return score, concerns


# ---------- Deployment-config snippet builder ----------


def _build_endpoint_config_snippet(
    primary_bare: str,
    fallback_bare: str,
    target_count: int,
) -> dict[str, Any]:
    """Produce a ready-to-paste CreateEndpointConfig fragment.

    Not a full API call — the customer still owns ModelName,
    EndpointConfigName, etc. This is the shape they'll paste into
    their IaC (CloudFormation, CDK, Terraform).
    """
    return {
        "pattern": "capacity_aware_instance_fallback",
        "docs": [
            {
                "label": "Deploy to multiple instance types with instance pools",
                "url": (
                    "https://docs.aws.amazon.com/sagemaker/latest/dg/"
                    "realtime-endpoints-heterogeneous.html"
                ),
            },
            {
                "label": "Capacity-aware inference automatic fallback (blog)",
                "url": (
                    "https://aws.amazon.com/blogs/machine-learning/"
                    "capacity-aware-inference-automatic-instance-fallback-"
                    "for-sagemaker-ai-endpoints/"
                ),
            },
        ],
        "example_production_variants": [
            {
                "VariantName": "primary",
                "InstanceType": f"ml.{primary_bare}",
                "InitialInstanceCount": target_count,
                "InitialVariantWeight": 1.0,
            },
            {
                "VariantName": "fallback",
                "InstanceType": f"ml.{fallback_bare}",
                "InitialInstanceCount": 0,
                "InitialVariantWeight": 0.0,
                "_note": (
                    "Set InitialInstanceCount=0 and lean on "
                    "ManagedInstanceScaling (MinInstanceCount=0, "
                    "MaxInstanceCount=target_count) so SageMaker only "
                    "provisions this pool when the primary pool cannot "
                    "satisfy capacity. See the blog for the exact IAM + "
                    "scaling policy shape."
                ),
            },
        ],
        "deployment_notes": [
            "Validate TP configuration on BOTH pools before enabling in "
            "production — primary and fallback may require different TP "
            "levels for the same model.",
            "SageMaker ScaleOut's instance-launch failure detection is "
            "what triggers automatic fallback; configure "
            "ManagedInstanceScaling (not legacy AutoScaling) for the "
            "capacity-aware behavior.",
            "Container image must support BOTH instance types' CUDA / "
            "driver versions. For NVIDIA-only pairings this is usually "
            "automatic; keep an eye on it if you span driver generations "
            "(e.g. Ampere + Blackwell).",
        ],
    }


# ---------- Public tool entrypoint ----------


def evaluate_heterogeneous_endpoint_fallback(
    primary_instance_type: str,
    region: str,
    target_count: int = 1,
    model_size_b: float | None = None,
    max_fallbacks: int = 3,
) -> dict[str, Any]:
    """Evaluate SageMaker capacity-aware endpoint fallback candidates.

    Args:
        primary_instance_type: Primary instance type the customer wants
            (e.g. ``"ml.g6e.xlarge"`` or ``"g6e.xlarge"``).
        region: AWS region code where the endpoint will deploy.
        target_count: Number of instances per pool. Clamped to >= 1.
        model_size_b: Model parameter count in billions
            (e.g. ``7.0``, ``70.0``). When supplied, each fallback
            carries a TP recommendation; when ``None``, TP is skipped.
        max_fallbacks: Maximum number of ranked fallback candidates to
            return. Default 3.

    Returns:
        dict with:
            primary: spec + hourly_usd + available_in_region
            fallback_candidates: list sorted by pairing_score desc,
                each with its own TP recommendation and concerns
            deployment_recommendation: pattern + docs + example
                CreateEndpointConfig fragment + deployment notes
            capacity_signal: ``"available"`` when ≥1 valid fallback is
                found, ``"unavailable"`` when zero candidates survive
                the hard disqualifiers, ``"unknown"`` on API error
            interpretation: human-readable one-paragraph summary the
                agent can narrate without further munging
            source: comma-separated list of AWS APIs the tool called
    """
    bare_primary = _normalize_bare(primary_instance_type)
    target_count = max(1, int(target_count))
    max_fallbacks = max(1, min(int(max_fallbacks), 10))

    try:
        primary_specs = _get_instance_specs(bare_primary, region)
        if primary_specs is None:
            return _unknown_result(
                bare_primary,
                region,
                target_count,
                model_size_b,
                f"Could not look up specs for {bare_primary}. Verify the "
                "instance type name is correct and that EC2 "
                "DescribeInstanceTypes is reachable.",
            )

        offered = _offered_gpu_types_in_region(region)
        primary_available = bare_primary in offered
        primary_price_resp = get_sagemaker_pricing(
            instance_type=bare_primary, region=region
        )
        primary_price = (
            primary_price_resp.get("hourly_usd")
            if primary_price_resp.get("available")
            else None
        )

        primary_block = {
            "instance_type": bare_primary,
            "sagemaker_instance_type": f"ml.{bare_primary}",
            "specs": primary_specs,
            "available_in_region": primary_available,
            "sagemaker_priced_in_region": primary_price is not None,
            "hourly_usd": primary_price,
            "tp_guidance": _recommend_tp(
                model_size_b,
                primary_specs["gpu_count"],
                primary_specs["gpu_memory_gb_per_gpu"],
            ),
        }

        candidates = sorted(
            offered - {bare_primary},
            key=lambda t: (_family_of(t) != _family_of(bare_primary), t),
        )

        scored: list[dict[str, Any]] = []
        for candidate in candidates:
            specs = _get_instance_specs(candidate, region)
            if specs is None:
                continue
            if specs["gpu_count"] <= 0:
                continue
            price_resp = get_sagemaker_pricing(instance_type=candidate, region=region)
            available = True  # pulled from the offered set
            sagemaker_priced = bool(price_resp.get("available"))
            price = price_resp.get("hourly_usd") if sagemaker_priced else None

            score, concerns = _pairing_score(
                primary_specs,
                specs,
                price,
                primary_price,
                available_in_region=available,
                sagemaker_priced=sagemaker_priced,
            )
            if score <= 0:
                continue

            cost_delta_pct = (
                round(((price - primary_price) / primary_price) * 100, 1)
                if (
                    primary_price is not None
                    and price is not None
                    and primary_price > 0
                )
                else None
            )

            scored.append(
                {
                    "instance_type": candidate,
                    "sagemaker_instance_type": f"ml.{candidate}",
                    "specs": specs,
                    "available_in_region": available,
                    "sagemaker_priced_in_region": sagemaker_priced,
                    "hourly_usd": price,
                    "cost_delta_pct_vs_primary": cost_delta_pct,
                    "pairing_score": score,
                    "pairing_rationale": _rationale(
                        primary_specs, specs, cost_delta_pct
                    ),
                    "concerns": concerns,
                    "tp_guidance": _recommend_tp(
                        model_size_b,
                        specs["gpu_count"],
                        specs["gpu_memory_gb_per_gpu"],
                    ),
                }
            )

        scored.sort(key=lambda c: c["pairing_score"], reverse=True)
        top = scored[:max_fallbacks]

        if not top:
            return {
                "primary": primary_block,
                "region": region,
                "target_count": target_count,
                "model_size_b": model_size_b,
                "fallback_candidates": [],
                "deployment_recommendation": None,
                "capacity_signal": "unavailable",
                "interpretation": (
                    f"No valid heterogeneous-endpoint fallback found for "
                    f"{bare_primary} in {region}. Every candidate failed at "
                    "least one hard requirement: same GPU vendor, offered "
                    "in region, or SageMaker-priced in region. Consider a "
                    "different region, a Capacity Block reservation, or an "
                    "ODCR via your AWS account team."
                ),
                "source": (
                    "ec2.DescribeInstanceTypes + "
                    "ec2.DescribeInstanceTypeOfferings + pricing.GetProducts"
                ),
            }

        best = top[0]
        deployment = _build_endpoint_config_snippet(
            bare_primary, best["instance_type"], target_count
        )

        return {
            "primary": primary_block,
            "region": region,
            "target_count": target_count,
            "model_size_b": model_size_b,
            "fallback_candidates": top,
            "deployment_recommendation": deployment,
            "capacity_signal": "available",
            "interpretation": (
                f"{len(top)} heterogeneous-endpoint fallback candidate(s) "
                f"found for ml.{bare_primary} in {region}. Top candidate: "
                f"ml.{best['instance_type']} "
                f"(pairing_score {best['pairing_score']}"
                + (
                    f", {best['cost_delta_pct_vs_primary']:+.0f}% cost delta"
                    if best.get("cost_delta_pct_vs_primary") is not None
                    else ""
                )
                + "). Use the ``deployment_recommendation.example_production_variants`` "
                "block to configure a SageMaker capacity-aware endpoint "
                "that provisions the fallback pool automatically if the "
                "primary pool cannot satisfy capacity at launch."
            ),
            "source": (
                "ec2.DescribeInstanceTypes + "
                "ec2.DescribeInstanceTypeOfferings + pricing.GetProducts"
            ),
        }

    except Exception as exc:  # noqa: BLE001
        return _unknown_result(
            bare_primary,
            region,
            target_count,
            model_size_b,
            f"API error during evaluation: {type(exc).__name__}: {exc}. "
            "This may indicate insufficient IAM permissions "
            "(ec2:DescribeInstanceTypes, ec2:DescribeInstanceTypeOfferings, "
            "or pricing:GetProducts) or that the region is unreachable.",
        )


def _rationale(
    primary: dict[str, Any],
    fallback: dict[str, Any],
    cost_delta_pct: float | None,
) -> str:
    """One-line reason explaining why the fallback made the cut."""
    vendor_match = (
        primary["gpu_manufacturer"].lower() == fallback["gpu_manufacturer"].lower()
    )
    primary_mem = primary["gpu_total_memory_gb"]
    fallback_mem = fallback["gpu_total_memory_gb"]
    mem_ratio = round(fallback_mem / primary_mem, 2) if primary_mem > 0 else None
    cost_frag = (
        f"{cost_delta_pct:+.0f}% cost" if cost_delta_pct is not None else "cost unknown"
    )
    return (
        f"{primary['gpu_manufacturer']} ↔ {fallback['gpu_manufacturer']} "
        f"({'same vendor' if vendor_match else 'cross-vendor'}); "
        f"{fallback['gpu_name']} ({fallback['gpu_count']}× "
        f"{fallback['gpu_memory_gb_per_gpu']}GB = {fallback_mem}GB "
        f"total, "
        + (f"{mem_ratio}x primary memory, " if mem_ratio is not None else "")
        + f"{cost_frag}."
    )


def _unknown_result(
    bare_primary: str,
    region: str,
    target_count: int,
    model_size_b: float | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "primary": {
            "instance_type": bare_primary,
            "sagemaker_instance_type": f"ml.{bare_primary}",
            "specs": None,
            "available_in_region": None,
            "sagemaker_priced_in_region": None,
            "hourly_usd": None,
            "tp_guidance": None,
        },
        "region": region,
        "target_count": target_count,
        "model_size_b": model_size_b,
        "fallback_candidates": [],
        "deployment_recommendation": None,
        "capacity_signal": "unknown",
        "interpretation": reason,
        "source": (
            "ec2.DescribeInstanceTypes + "
            "ec2.DescribeInstanceTypeOfferings + pricing.GetProducts"
        ),
    }


TOOL_SPEC = {
    "name": "evaluate_heterogeneous_endpoint_fallback",
    "description": (
        "Evaluate SageMaker capacity-aware endpoint fallback candidates "
        "for a primary GPU instance type that may not be dependably "
        "available in the target region. Returns a ranked list of valid "
        "fallback instances with: compatibility checks (same GPU vendor, "
        "region availability, SageMaker-GA via Pricing API), composite "
        "pairing score, per-fallback tensor-parallelism (TP) hint when "
        "model_size_b is supplied, and a ready-to-paste "
        "ProductionVariants config fragment. USE THIS when a customer "
        "mentions SageMaker inference with capacity concerns, "
        "specifically when they want capacity-aware endpoint fallback / "
        "instance_pools / heterogeneous endpoints. The deployment "
        "pattern references the SageMaker docs at "
        "docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-"
        "heterogeneous.html and the Nov 2024 'Capacity-aware inference "
        "automatic fallback' blog post. Do NOT use for training "
        "workloads — those use SageMaker Flexible Training Plans "
        "(get_sagemaker_training_plan_offerings) instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "primary_instance_type": {
                "type": "string",
                "description": (
                    "The primary instance type the customer wants to "
                    "deploy on. ml. prefix is accepted and stripped "
                    "(e.g. 'ml.g6e.xlarge' or 'g6e.xlarge')."
                ),
            },
            "region": {
                "type": "string",
                "description": (
                    "AWS region code where the SageMaker endpoint will "
                    "deploy (e.g. 'us-east-1', 'sa-east-1')."
                ),
            },
            "target_count": {
                "type": "integer",
                "description": (
                    "Number of instances per pool. Default 1. The tool "
                    "echoes this into the example ProductionVariants "
                    "config so the customer sees the right scaling "
                    "numbers."
                ),
            },
            "model_size_b": {
                "type": "number",
                "description": (
                    "Model parameter count in billions (e.g. 7.0 for a "
                    "7B model, 70.0 for 70B). When supplied, each "
                    "fallback candidate carries a per-pool TP "
                    "recommendation based on FP16 + KV-cache memory "
                    "math. Omit if the customer hasn't named a model."
                ),
            },
            "max_fallbacks": {
                "type": "integer",
                "description": (
                    "Max fallback candidates to return (default 3, cap 10)."
                ),
            },
        },
        "required": ["primary_instance_type", "region"],
    },
    "handler": evaluate_heterogeneous_endpoint_fallback,
}
