# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Reference data tool — provides GPU chip specs, model sizing guidance,
and regional proximity on-demand.

Instead of embedding 2000+ tokens of reference data in every system prompt
(which gets re-sent on every Bedrock call), the model queries this tool
only when it needs specific reference information. This cuts per-call
token cost by ~60%.

Categories:
- "gpu_chips": GPU family → chip name, memory, use case
- "model_sizing": Model parameter count → recommended instance types
- "region_proximity": Region → nearby alternative regions
- "all": Returns everything (use sparingly)
"""

from __future__ import annotations

import re
from typing import Any


# Parameter-count regex. Matches "7b", "13B", "70b", "405B",
# "8x7B" (Mixtral-style — treated as 56B effective), "1.5b", "3.8b".
# The captured group is the parameter count as a decimal number of
# billions. Preferring the LARGEST match in the string ensures
# "7b or 8b" doesn't dispatch to the under_7b bucket.
_PARAM_COUNT_RE = re.compile(
    r"(?:(\d+)\s*[x*]\s*)?(\d+(?:\.\d+)?)\s*b\b",
    re.IGNORECASE,
)


def _dispatch_model_sizing_bucket(query: str) -> str | None:
    """Parse a natural-language model reference and return the
    non-overlapping bucket key it belongs to.

    Preferences:
    - Explicit ranges (e.g. "13B-70B") pick the upper bucket.
    - Mixture-of-experts multipliers ("8x7B") multiply out to the
      effective active-parameter count.
    - The LARGEST parameter count in the query determines the bucket
      (defeats the "7b or 8b" ambiguity).

    Returns None when no plausible parameter count is found.
    """
    q = query.lower()
    matches = _PARAM_COUNT_RE.findall(q)
    if not matches:
        return None

    largest = 0.0
    for multiplier_str, number_str in matches:
        try:
            n = float(number_str)
        except ValueError:
            continue
        if multiplier_str:
            try:
                m = int(multiplier_str)
                n = n * m
            except ValueError:
                pass
        if n > largest:
            largest = n

    if largest <= 0:
        return None
    if largest < 5:
        return "under_7b"
    if largest < 13:
        return "7b_to_13b"
    if largest < 70:
        return "13b_to_70b"
    return "over_70b"


# GPU chip reference. ``gpus_per_instance`` is normalized to a string
# range across all entries for consistent LLM narration. Data reflects
# the Bedrock-adjacent GPU family catalog as of publication; see
# ``src/region_model.py`` for regional GA status.
GPU_CHIP_DATA = {
    # Cost-optimized inference (T4 / T4G class)
    "g4dn": {
        "chip": "NVIDIA T4",
        "memory_gb": 16,
        "gpus_per_instance": "1-4",
        "use_case": "Cost-optimized inference (small-to-mid models, batch)",
    },
    "g5g": {
        "chip": "NVIDIA T4G (ARM Graviton2 host)",
        "memory_gb": 16,
        "gpus_per_instance": "1-2",
        "use_case": "ARM-based inference, cost-sensitive",
    },
    # A10G class
    "g5": {
        "chip": "NVIDIA A10G",
        "memory_gb": 24,
        "gpus_per_instance": "1-8",
        "use_case": "Inference (7B-13B models), training (small)",
    },
    # L4 class
    "g6": {
        "chip": "NVIDIA L4",
        "memory_gb": 24,
        "gpus_per_instance": "1-8",
        "use_case": "Inference (7B-13B models), cost-optimized",
    },
    "g6e": {
        "chip": "NVIDIA L40S",
        "memory_gb": 48,
        "gpus_per_instance": "1-8",
        "use_case": "Higher-memory L-class for 30B-class models, EFA networking",
    },
    # RTX PRO Blackwell class (2026 GA; US East (Ohio) + US West (Oregon) at launch)
    "g7": {
        "chip": "NVIDIA RTX PRO 4500 Blackwell",
        "memory_gb": 32,
        "gpus_per_instance": "1-8",
        "use_case": "Cost-efficient Blackwell inference with native FP4 Tensor Cores (us-east-2 + us-west-2 at launch)",
    },
    "g7e": {
        "chip": "NVIDIA RTX PRO 6000 Blackwell",
        "memory_gb": 96,
        "gpus_per_instance": "1-8",
        "use_case": "High-memory Blackwell inference, up to 300B models on 8-GPU, native FP4 and EFAv4 (up to 1600 Gbps)",
    },
    # V100 class (legacy)
    "p3": {
        "chip": "NVIDIA V100",
        "memory_gb": 16,
        "gpus_per_instance": "1-8",
        "use_case": "Legacy training/inference, being replaced by p4d",
    },
    "p3dn": {
        "chip": "NVIDIA V100",
        "memory_gb": 32,
        "gpus_per_instance": "8",
        "use_case": "Legacy training with EFA / larger V100 memory",
    },
    # A100 class
    "p4d": {
        "chip": "NVIDIA A100",
        "memory_gb": 40,
        "gpus_per_instance": "8",
        "use_case": "Large model training/inference (13B-70B)",
    },
    "p4de": {
        "chip": "NVIDIA A100",
        "memory_gb": 80,
        "gpus_per_instance": "8",
        "use_case": "Large model training/inference (70B+)",
    },
    # H100 / H200 class
    "p5": {
        "chip": "NVIDIA H100",
        "memory_gb": 80,
        "gpus_per_instance": "1 or 8",
        "use_case": "Largest models (70B+) at 8-GPU scale; single-GPU p5.4xlarge (GA Aug 2025) for cost-conscious H100 inference",
    },
    "p5e": {
        "chip": "NVIDIA H200",
        "memory_gb": 141,
        "gpus_per_instance": "8",
        "use_case": "Next-gen largest models, maximum memory",
    },
    "p5en": {
        "chip": "NVIDIA H200 with EFA v2",
        "memory_gb": 141,
        "gpus_per_instance": "8",
        "use_case": "H200 with the newer EFA fabric for multi-node training",
    },
    # Blackwell class — EC2 family prefix is p6-b200.* (not plain p6)
    "p6-b200": {
        "chip": "NVIDIA B200 (Blackwell)",
        "memory_gb": 192,
        "gpus_per_instance": "8",
        "use_case": "Frontier models (100B+), highest per-GPU memory available at B200 launch",
    },
    # Blackwell Ultra class (2026 GA)
    "p6-b300": {
        "chip": "NVIDIA B300 (Blackwell Ultra)",
        "memory_gb": 288,
        "gpus_per_instance": "8",
        "use_case": "Trillion-parameter frontier models, Blackwell Ultra memory bump over B200",
    },
    # AWS silicon
    "inf1": {
        "chip": "AWS Inferentia",
        "memory_gb": 8,
        "gpus_per_instance": "1-16",
        "use_case": "Cost-optimized inference (specific model support)",
    },
    "inf2": {
        "chip": "AWS Inferentia2",
        "memory_gb": 32,
        "gpus_per_instance": "1-12",
        "use_case": "Cost-optimized inference (broader model support)",
    },
    "trn1": {
        "chip": "AWS Trainium",
        "memory_gb": 32,
        "gpus_per_instance": "1-16",
        "use_case": "Cost-optimized training",
    },
    "trn2": {
        "chip": "AWS Trainium2",
        "memory_gb": 96,
        "gpus_per_instance": "1-16",
        "use_case": "Next-gen cost-optimized training",
    },
}

# Model-sizing recommendations bucketed by parameter count. Buckets are
# NON-OVERLAPPING — a 7B model belongs in the 7b_to_13b bucket, not in
# "under_7b" (that bucket is for truly small models like Phi-3-mini).
MODEL_SIZING_DATA = {
    "under_7b": {
        "description": "Small models under ~5B parameters (Phi-3-mini, small distilled models, embedding models)",
        "recommended": [
            {
                "instance": "ml.g6.xlarge",
                "chip": "L4",
                "memory": "24 GB",
                "cost_tier": "lowest",
            },
            {
                "instance": "ml.g5.xlarge",
                "chip": "A10G",
                "memory": "24 GB",
                "cost_tier": "low",
            },
            {
                "instance": "ml.inf2.xlarge",
                "chip": "Inferentia2",
                "memory": "32 GB",
                "cost_tier": "lowest (if model supported)",
            },
            {
                "instance": "ml.g4dn.xlarge",
                "chip": "T4",
                "memory": "16 GB",
                "cost_tier": "lowest (legacy T4)",
            },
        ],
    },
    "7b_to_13b": {
        "description": "Mid-size models 5B-13B parameters (Mistral 7B, Llama 3 8B, CodeLlama 13B, Phi-3-medium)",
        "recommended": [
            {
                "instance": "ml.g6.2xlarge",
                "chip": "L4",
                "memory": "24 GB",
                "cost_tier": "low",
            },
            {
                "instance": "ml.g5.2xlarge",
                "chip": "A10G",
                "memory": "24 GB",
                "cost_tier": "low",
            },
            {
                "instance": "ml.g5.4xlarge",
                "chip": "A10G",
                "memory": "24 GB",
                "cost_tier": "moderate",
            },
        ],
    },
    "13b_to_70b": {
        "description": "Large models 13B-70B parameters (Llama 3 70B, Mixtral 8x7B, Falcon 40B, DeepSeek V2)",
        "recommended": [
            {
                "instance": "ml.g5.12xlarge",
                "chip": "4x A10G",
                "memory": "96 GB total",
                "cost_tier": "moderate",
            },
            {
                "instance": "ml.g6e.12xlarge",
                "chip": "4x L40S",
                "memory": "192 GB total",
                "cost_tier": "moderate-high",
            },
            {
                "instance": "ml.g5.48xlarge",
                "chip": "8x A10G",
                "memory": "192 GB total",
                "cost_tier": "high",
            },
            {
                "instance": "ml.p4d.24xlarge",
                "chip": "8x A100 40GB",
                "memory": "320 GB total",
                "cost_tier": "high",
            },
            {
                "instance": "ml.p5.4xlarge",
                "chip": "1x H100",
                "memory": "80 GB",
                "cost_tier": "moderate-high (single-GPU H100, GA Aug 2025 — strong per-request latency for 30B-50B models)",
            },
        ],
    },
    "over_70b": {
        "description": "Frontier models 70B+ parameters (Llama 3.1 405B, DeepSeek V3, Claude Opus class, GPT-4 class)",
        "recommended": [
            {
                "instance": "ml.p4d.24xlarge",
                "chip": "8x A100 40GB",
                "memory": "320 GB total",
                "cost_tier": "high",
            },
            {
                "instance": "ml.p4de.24xlarge",
                "chip": "8x A100 80GB",
                "memory": "640 GB total",
                "cost_tier": "very high",
            },
            {
                "instance": "ml.p5.48xlarge",
                "chip": "8x H100",
                "memory": "640 GB total",
                "cost_tier": "highest",
            },
            {
                "instance": "ml.p5en.48xlarge",
                "chip": "8x H200 + EFA v2",
                "memory": "1128 GB total",
                "cost_tier": "highest",
            },
            {
                "instance": "ml.p6-b200.48xlarge",
                "chip": "8x B200 (Blackwell)",
                "memory": "1536 GB total",
                "cost_tier": "flagship",
            },
            {
                "instance": "ml.p6-b300.48xlarge",
                "chip": "8x B300 (Blackwell Ultra)",
                "memory": "2304 GB total",
                "cost_tier": "flagship (Blackwell Ultra memory bump — trillion-parameter frontier)",
            },
        ],
    },
}

REGION_PROXIMITY_DATA = {
    "af-south-1": {
        "name": "Cape Town",
        "nearby": [
            "eu-west-1 (Ireland, ~150ms)",
            "eu-central-1 (Frankfurt, ~160ms)",
            "me-south-1 (Bahrain, ~120ms)",
        ],
    },
    "sa-east-1": {
        "name": "São Paulo",
        "nearby": [
            "us-east-1 (Virginia, ~120ms)",
            "ca-central-1 (Montreal, ~130ms)",
            "us-east-2 (Ohio, ~125ms)",
        ],
    },
    "ap-south-2": {
        "name": "Hyderabad",
        "nearby": ["ap-south-1 (Mumbai, ~20ms)", "ap-southeast-1 (Singapore, ~60ms)"],
    },
    "me-south-1": {
        "name": "Bahrain",
        "nearby": [
            "eu-central-1 (Frankfurt, ~80ms)",
            "ap-south-1 (Mumbai, ~40ms)",
            "af-south-1 (Cape Town, ~120ms)",
        ],
    },
    "me-central-1": {
        "name": "UAE",
        "nearby": ["eu-central-1 (Frankfurt, ~90ms)", "ap-south-1 (Mumbai, ~35ms)"],
    },
    "eu-south-1": {
        "name": "Milan",
        "nearby": ["eu-central-1 (Frankfurt, ~15ms)", "eu-west-1 (Ireland, ~25ms)"],
    },
    "eu-south-2": {
        "name": "Spain",
        "nearby": ["eu-west-1 (Ireland, ~20ms)", "eu-central-1 (Frankfurt, ~25ms)"],
    },
    "ap-southeast-3": {
        "name": "Jakarta",
        "nearby": [
            "ap-southeast-1 (Singapore, ~20ms)",
            "ap-southeast-2 (Sydney, ~80ms)",
        ],
    },
    "il-central-1": {
        "name": "Tel Aviv",
        "nearby": ["eu-central-1 (Frankfurt, ~50ms)", "me-south-1 (Bahrain, ~40ms)"],
    },
    "ca-west-1": {
        "name": "Calgary",
        "nearby": ["us-west-2 (Oregon, ~30ms)", "ca-central-1 (Montreal, ~50ms)"],
    },
    "ap-northeast-3": {
        "name": "Osaka",
        "nearby": ["ap-northeast-1 (Tokyo, ~5ms)", "ap-southeast-1 (Singapore, ~70ms)"],
    },
    "eu-north-1": {
        "name": "Stockholm",
        "nearby": ["eu-west-1 (Ireland, ~30ms)", "eu-central-1 (Frankfurt, ~20ms)"],
    },
}


def get_reference_data(category: str, query: str = "") -> dict[str, Any]:
    """Retrieve GPU capacity planning reference data on-demand.

    Args:
        category: One of "gpu_chips", "model_sizing", "region_proximity", or "all"
        query: Optional filter — e.g., "g5" for gpu_chips, "70b" for model_sizing,
               "af-south-1" for region_proximity

    Returns:
        Relevant reference data for the requested category.
    """
    if category == "gpu_chips":
        if query:
            family = query.removeprefix("ml.").split(".")[0].lower()
            match = GPU_CHIP_DATA.get(family)
            if match:
                return {
                    "category": "gpu_chips",
                    "family": family,
                    "data": match,
                    "source": "reference_data_tool",
                }
            return {
                "category": "gpu_chips",
                "query": query,
                "data": None,
                "note": f"Unknown GPU family: {query}",
                "source": "reference_data_tool",
            }
        return {
            "category": "gpu_chips",
            "data": GPU_CHIP_DATA,
            "source": "reference_data_tool",
        }

    elif category == "model_sizing":
        if query:
            tier = _dispatch_model_sizing_bucket(query)
            if tier is not None:
                return {
                    "category": "model_sizing",
                    "tier": tier,
                    "data": MODEL_SIZING_DATA[tier],
                    "source": "reference_data_tool",
                }
        return {
            "category": "model_sizing",
            "data": MODEL_SIZING_DATA,
            "source": "reference_data_tool",
        }

    elif category == "region_proximity":
        if query:
            region = query.lower().strip()
            match = REGION_PROXIMITY_DATA.get(region)
            if match:
                return {
                    "category": "region_proximity",
                    "region": region,
                    "data": match,
                    "source": "reference_data_tool",
                }
            return {
                "category": "region_proximity",
                "region": region,
                "data": None,
                "note": f"No proximity data for {region} (likely an established region — no alternatives needed)",
                "source": "reference_data_tool",
            }
        return {
            "category": "region_proximity",
            "data": REGION_PROXIMITY_DATA,
            "source": "reference_data_tool",
        }

    elif category == "all":
        return {
            "category": "all",
            "gpu_chips": GPU_CHIP_DATA,
            "model_sizing": MODEL_SIZING_DATA,
            "region_proximity": REGION_PROXIMITY_DATA,
            "source": "reference_data_tool",
        }

    return {
        "error": f"Unknown category: {category}. Use: gpu_chips, model_sizing, region_proximity, or all",
        "source": "reference_data_tool",
    }


TOOL_SPEC = {
    "name": "get_reference_data",
    "description": (
        "Query GPU capacity planning reference data. Use this to look up:\n"
        "- 'gpu_chips': GPU family specs (chip name, memory, use case). Query with family name like 'g5' or 'p4d'.\n"
        "- 'model_sizing': Recommended instances by model parameter count. Query with size like '7b' or '70b'.\n"
        "- 'region_proximity': Nearby alternative regions with estimated latency. Query with region code like 'af-south-1'.\n"
        "Call this tool INSTEAD of guessing GPU specs or model sizing from memory. "
        "It's cheaper than including all reference data in every prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Data category: 'gpu_chips', 'model_sizing', 'region_proximity', or 'all'",
                "enum": ["gpu_chips", "model_sizing", "region_proximity", "all"],
            },
            "query": {
                "type": "string",
                "description": "Optional filter — e.g., 'g5' for gpu_chips, '70b' for model_sizing, 'af-south-1' for region_proximity",
            },
        },
        "required": ["category"],
    },
    "handler": get_reference_data,
}
