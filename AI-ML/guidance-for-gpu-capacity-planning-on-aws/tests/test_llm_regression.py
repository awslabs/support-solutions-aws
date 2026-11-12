# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""LLM-behavior regression suite.

Runs 8 canned scenarios through the real Bedrock Haiku 4.5 model with
every tool mocked out to return deterministic, scenario-specific
responses. Asserts on:

- The deterministic verdict label (`confidence_assessments[0].confidence.verdict`)
- Case-insensitive narrative concept matches in the markdown `answer`
- Absence of forbidden phrases that would indicate model drift

**Not run by default.** Opt in via ``make llm-regression`` or
``RUN_LLM_REGRESSION=1 pytest -m llm_regression``.

**Cost**: each run hits real Bedrock with ~6K tokens of input + ~1K
tokens of output per scenario. At the Haiku 4.5 cross-region rate
(\$1.10 / \$5.50 per M), the full 8-scenario run costs ~\$0.10. The
suite is pytest-collected only when the ``RUN_LLM_REGRESSION`` env var
is ``"1"``.

**Why not test the LLM directly?** The deterministic scorer already
covers the signal-to-verdict mapping (see ``tests/test_confidence.py``
and ``tests/test_mitigation_signals.py``). What those tests DON'T cover
is whether the LLM, given the system prompt + 12 tool specs + a user
query + the computed verdict, produces a narrative that:
  (a) uses the correct verdict label from the scorer, not a hallucinated one;
  (b) mentions the mitigation strategy that fits the scenario; and
  (c) stays within the four canonical verdict labels defined in the
      system prompt.

Those are LLM behavior properties, not scorer properties, so they
need an actual Bedrock call to regress against.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.agent import CapacityDecisionAgent
from src.tools import ALL_TOOLS


# ---------- Opt-in gating ----------

RUN_LLM = os.environ.get("RUN_LLM_REGRESSION") == "1"

pytestmark = [
    pytest.mark.llm_regression,
    pytest.mark.skipif(
        not RUN_LLM,
        reason=(
            "LLM regression tests not run by default (hit real Bedrock, "
            "cost ~\\$0.10/run). Run with `make llm-regression` or "
            "`RUN_LLM_REGRESSION=1 pytest -m llm_regression`."
        ),
    ),
]


# ---------- Scenario definition ----------


@dataclass
class Scenario:
    """A canned LLM regression scenario.

    tool_stubs: tool_name -> dict return value. Every call to that tool
        (regardless of args) returns the same dict. If a tool is not
        in the dict, the tool call will raise — scenarios should mock
        every tool the LLM might reasonably call for the query.

    expected_verdict: one of DEPLOYABLE / DEPLOYABLE WITH PREPARATION /
        HIGH RISK / NOT FEASIBLE.

    required_concepts: list of concept groups. Each inner list is a set
        of acceptable phrasings for one concept (e.g., ["ODCR",
        "capacity reservation", "reserved capacity"]). For each group,
        at least one phrasing must appear in the markdown ``answer``
        (case-insensitive substring).

    forbidden_phrases: anti-regression. If any appears in the answer,
        the test fails. Use for verdict labels the scorer wouldn't emit
        (e.g., "MODERATE RISK" which the system prompt forbids).
    """

    name: str
    query: str
    tool_stubs: dict[str, dict[str, Any]]
    expected_verdict: str
    required_concepts: list[list[str]]
    forbidden_phrases: list[str] = field(default_factory=list)


# ---------- Mock-output helpers ----------


def _mock_availability(available: bool, instance_type: str, region: str) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "available": available,
        "offered_zones": ["use1-az1", "use1-az2"] if available else [],
        "source": "ec2.DescribeInstanceTypeOfferings",
    }


def _mock_pricing(
    available: bool, hourly: float, instance_type: str, region: str
) -> dict:
    return {
        "instance_type": f"ml.{instance_type.removeprefix('ml.')}",
        "region": region,
        "component": "Hosting",
        "hourly_usd": hourly if available else None,
        "available": available,
        "source": "AWS Pricing API",
    }


def _mock_quota(limit: int, instance_type: str) -> dict:
    return {
        "instance_type": instance_type,
        "found": True,
        "current_limit": limit,
        "source": "service-quotas.ListServiceQuotas",
    }


def _mock_spot_score(score: int, target: int, instance_type: str, region: str) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "target_capacity": target,
        "score": score,
        "scores_by_az": [{"az": "region-wide", "region": region, "score": score}],
        "capacity_signal": (
            "strong"
            if score >= 8
            else "moderate"
            if score >= 5
            else "weak"
            if score >= 3
            else "critical"
        ),
        "interpretation": f"Spot Placement Score {score}/10 for {target}× {instance_type}",
        "source": "ec2.GetSpotPlacementScores",
    }


def _mock_odcrs(available_capacity: int, instance_type: str, region: str) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "available_capacity": available_capacity,
        "reservation_count": 1 if available_capacity > 0 else 0,
        "source": "ec2.DescribeCapacityReservations",
    }


def _mock_capacity_blocks(
    signal: str, instance_type: str, region: str, offerings: int = 0
) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "offerings_count": offerings,
        "offerings": [
            {"capacity_block_offering_id": f"cbo-{i}"} for i in range(offerings)
        ],
        "cheapest_offering": {"upfront_fee": "12600.00"} if offerings > 0 else None,
        "capacity_signal": signal,
        "interpretation": (
            f"{offerings} Capacity Block offering(s) match this request"
            if signal == "available"
            else "No Capacity Block offerings match this request"
        ),
        "source": "ec2.DescribeCapacityBlockOfferings",
    }


def _mock_training_plans(
    signal: str, instance_type: str, region: str, offerings: int = 0
) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "offerings_count": offerings,
        "offerings": [
            {"training_plan_offering_id": f"tpo-{i}"} for i in range(offerings)
        ],
        "capacity_signal": signal,
        "interpretation": (
            f"{offerings} Flexible Training Plan offering(s) match this request"
            if signal == "available"
            else "No Flexible Training Plan offerings match this request"
        ),
        "source": "sagemaker.SearchTrainingPlanOfferings",
    }


def _mock_heterogeneous(signal: str, primary: str, fallback: str, region: str) -> dict:
    return {
        "primary": {
            "instance_type": primary,
            "sagemaker_instance_type": f"ml.{primary}",
            "specs": {
                "gpu_count": 1,
                "gpu_memory_gb_per_gpu": 48,
                "gpu_total_memory_gb": 48,
                "gpu_manufacturer": "NVIDIA",
                "gpu_name": "L40S",
            },
            "available_in_region": True,
            "hourly_usd": 1.861,
        },
        "region": region,
        "fallback_candidates": (
            [
                {
                    "instance_type": fallback,
                    "sagemaker_instance_type": f"ml.{fallback}",
                    "specs": {
                        "gpu_count": 4,
                        "gpu_memory_gb_per_gpu": 32,
                        "gpu_total_memory_gb": 128,
                        "gpu_manufacturer": "NVIDIA",
                        "gpu_name": "RTX PRO 4500 Blackwell",
                    },
                    "available_in_region": True,
                    "hourly_usd": 7.44,
                    "cost_delta_pct_vs_primary": 300.0,
                    "pairing_score": 0.95,
                    "tp_guidance": {
                        "fits": True,
                        "recommended_tp": 1,
                        "note": "7B at FP16 ≈ 18 GB; fits at TP=1",
                    },
                }
            ]
            if signal == "available"
            else []
        ),
        "deployment_recommendation": (
            {
                "pattern": "capacity_aware_instance_fallback",
                "example_production_variants": [
                    {"VariantName": "primary", "InstanceType": f"ml.{primary}"},
                    {"VariantName": "fallback", "InstanceType": f"ml.{fallback}"},
                ],
            }
            if signal == "available"
            else None
        ),
        "capacity_signal": signal,
        "interpretation": (
            f"Fallback candidate ml.{fallback} available"
            if signal == "available"
            else "No valid heterogeneous-endpoint fallback found"
        ),
        "source": "ec2.DescribeInstanceTypes + ec2.DescribeInstanceTypeOfferings + pricing.GetProducts",
    }


def _mock_ice_metrics(
    signal: str, events: int, instance_type: str, region: str
) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "event_count": events,
        "capacity_signal": signal,
        "deployment_target": "sagemaker",
        "interpretation": f"{events} ICE events in last 30 days",
        "source": "cloudwatch.GetMetricStatistics on GPUCapacityAgent/CustomerICE",
    }


def _mock_ice_history(
    signal: str, events: int, instance_type: str, region: str
) -> dict:
    return {
        "instance_type": instance_type,
        "region": region,
        "event_count": events,
        "capacity_signal": signal,
        "interpretation": f"{events} CloudTrail capacity errors in lookback",
        "source": "cloudtrail.LookupEvents",
    }


def _mock_alternatives(reference: str, region: str, alternatives: list[dict]) -> dict:
    return {
        "reference": reference,
        "region": region,
        "alternatives": alternatives,
        "interpretation": (
            f"Alternative GPU instances for {reference} in {region}: "
            + ", ".join(a["instance_type"] for a in alternatives)
            if alternatives
            else f"No alternatives found in {region}"
        ),
        "source": "ec2.DescribeInstanceTypes + DescribeInstanceTypeOfferings",
    }


def _mock_reference(
    category: str = "gpu_chips", query: str = "", data: Any = None
) -> dict:
    return {
        "category": category,
        "query": query,
        "data": data or {},
        "source": "local reference data (src/tools/reference_data.py)",
    }


def _mock_ec2_pricing(
    available: bool, hourly: float, instance_type: str, region: str
) -> dict:
    return {
        "instance_type": instance_type.removeprefix("ml."),
        "region": region,
        "hourly_usd": hourly if available else None,
        "available": available,
        "source": "AWS Pricing API (EC2)",
    }


# ---------- Scenarios ----------


SCENARIOS: list[Scenario] = [
    Scenario(
        name="deployable_simple",
        query=(
            "I need to deploy 20 ml.g5.2xlarge instances for real-time inference "
            "in us-east-1. Assess the capacity risk."
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "g5.2xlarge", "us-east-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 1.408, "g5.2xlarge", "us-east-1"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(
                True, 1.212, "g5.2xlarge", "us-east-1"
            ),
            "get_sagemaker_quota": _mock_quota(500, "g5.2xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                9, 20, "g5.2xlarge", "us-east-1"
            ),
            "check_existing_odcrs": _mock_odcrs(0, "g5.2xlarge", "us-east-1"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "g5.2xlarge", "us-east-1"
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "g5.2xlarge", "us-east-1"
            ),
            "get_ice_metrics": _mock_ice_metrics("clean", 0, "g5.2xlarge", "us-east-1"),
            "get_ice_history": _mock_ice_history("clean", 0, "g5.2xlarge", "us-east-1"),
            "find_alternative_instances": _mock_alternatives(
                "g5.2xlarge", "us-east-1", []
            ),
            "get_reference_data": _mock_reference("gpu_chips", "g5"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "g5.2xlarge", "", "us-east-1"
            ),
        },
        expected_verdict="DEPLOYABLE",
        required_concepts=[
            ["DEPLOYABLE"],
            ["g5.2xlarge"],
            ["us-east-1"],
        ],
        forbidden_phrases=["MODERATE RISK", "DEPLOYABLE WITH CAUTION"],
    ),
    Scenario(
        name="not_feasible_not_ga",
        query=(
            "22 g6 instances in af-south-1 for prod inference launching in "
            "2 weeks. What's my risk?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                False, "g6", "af-south-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(False, 0.0, "g6", "af-south-1"),
            "get_ec2_pricing": _mock_ec2_pricing(False, 0.0, "g6", "af-south-1"),
            "get_sagemaker_quota": _mock_quota(0, "g6"),
            "get_spot_placement_score": _mock_spot_score(1, 22, "g6", "af-south-1"),
            "check_existing_odcrs": _mock_odcrs(0, "g6", "af-south-1"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "g6", "af-south-1"
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "g6", "af-south-1"
            ),
            "get_ice_metrics": _mock_ice_metrics(
                "metric_filter_not_deployed", 0, "g6", "af-south-1"
            ),
            "get_ice_history": _mock_ice_history("clean", 0, "g6", "af-south-1"),
            "find_alternative_instances": _mock_alternatives(
                "g6",
                "af-south-1",
                [{"instance_type": "g5.2xlarge", "similarity_score": 0.85, "gpus": 1}],
            ),
            "get_reference_data": _mock_reference("gpu_chips", "g6"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "g6", "", "af-south-1"
            ),
        },
        expected_verdict="NOT FEASIBLE",
        required_concepts=[
            ["NOT FEASIBLE"],
            ["g6"],
            ["af-south-1"],
            ["g5", "alternative", "not offered", "not available"],
        ],
    ),
    Scenario(
        name="deployable_with_prep_quota_gap",
        query=(
            "100 ml.g5.xlarge for inference in us-west-2. Current quota is 20. "
            "What's the risk?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "g5.xlarge", "us-west-2"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 0.84, "g5.xlarge", "us-west-2"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(True, 0.766, "g5.xlarge", "us-west-2"),
            "get_sagemaker_quota": _mock_quota(20, "g5.xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                8, 100, "g5.xlarge", "us-west-2"
            ),
            "check_existing_odcrs": _mock_odcrs(0, "g5.xlarge", "us-west-2"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "g5.xlarge", "us-west-2"
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "g5.xlarge", "us-west-2"
            ),
            "get_ice_metrics": _mock_ice_metrics("clean", 0, "g5.xlarge", "us-west-2"),
            "get_ice_history": _mock_ice_history("clean", 0, "g5.xlarge", "us-west-2"),
            "find_alternative_instances": _mock_alternatives(
                "g5.xlarge", "us-west-2", []
            ),
            "get_reference_data": _mock_reference("gpu_chips", "g5"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "g5.xlarge", "", "us-west-2"
            ),
        },
        expected_verdict="DEPLOYABLE WITH PREPARATION",
        required_concepts=[
            ["DEPLOYABLE WITH PREPARATION"],
            ["quota"],
            ["100", "100 instances"],
        ],
    ),
    Scenario(
        name="high_risk_severe_ice_no_odcr",
        query=(
            "I want to deploy 30 ml.g5.4xlarge in us-east-1 for prod inference. "
            "Is it safe?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "g5.4xlarge", "us-east-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 2.03, "g5.4xlarge", "us-east-1"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(
                True, 1.624, "g5.4xlarge", "us-east-1"
            ),
            "get_sagemaker_quota": _mock_quota(500, "g5.4xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                8, 30, "g5.4xlarge", "us-east-1"
            ),
            "check_existing_odcrs": _mock_odcrs(0, "g5.4xlarge", "us-east-1"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "g5.4xlarge", "us-east-1"
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "g5.4xlarge", "us-east-1"
            ),
            "get_ice_metrics": _mock_ice_metrics(
                "severe", 25, "g5.4xlarge", "us-east-1"
            ),
            "get_ice_history": _mock_ice_history("clean", 0, "g5.4xlarge", "us-east-1"),
            "find_alternative_instances": _mock_alternatives(
                "g5.4xlarge", "us-east-1", []
            ),
            "get_reference_data": _mock_reference("gpu_chips", "g5"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "g5.4xlarge", "", "us-east-1"
            ),
        },
        expected_verdict="HIGH RISK",
        required_concepts=[
            ["HIGH RISK"],
            ["ICE", "insufficient capacity", "capacity error"],
            ["ODCR", "capacity reservation", "reserved capacity"],
        ],
        forbidden_phrases=["MODERATE RISK", "DEPLOYABLE WITH CAUTION"],
    ),
    Scenario(
        name="odcr_rescues_severe_ice",
        query=(
            "I have a fully-reserved ODCR for 10 ml.g5.4xlarge in us-east-1 and "
            "want to deploy an inference endpoint. Risk?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "g5.4xlarge", "us-east-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 2.03, "g5.4xlarge", "us-east-1"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(
                True, 1.624, "g5.4xlarge", "us-east-1"
            ),
            "get_sagemaker_quota": _mock_quota(500, "g5.4xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                2, 10, "g5.4xlarge", "us-east-1"
            ),
            # 10 ODCR capacity, 10 requested → fully covers
            "check_existing_odcrs": _mock_odcrs(10, "g5.4xlarge", "us-east-1"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "g5.4xlarge", "us-east-1"
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "g5.4xlarge", "us-east-1"
            ),
            "get_ice_metrics": _mock_ice_metrics(
                "severe", 25, "g5.4xlarge", "us-east-1"
            ),
            "get_ice_history": _mock_ice_history("clean", 0, "g5.4xlarge", "us-east-1"),
            "find_alternative_instances": _mock_alternatives(
                "g5.4xlarge", "us-east-1", []
            ),
            "get_reference_data": _mock_reference("gpu_chips", "g5"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "g5.4xlarge", "", "us-east-1"
            ),
        },
        expected_verdict="DEPLOYABLE",
        required_concepts=[
            ["DEPLOYABLE"],
            ["ODCR", "capacity reservation", "reserved capacity"],
        ],
    ),
    Scenario(
        name="capacity_blocks_recommended",
        query=(
            "I need 8 ml.p5.48xlarge in us-east-1 for a 1-week fine-tuning run "
            "starting next month. What's the right way to get predictable capacity?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "p5.48xlarge", "us-east-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 103.14, "p5.48xlarge", "us-east-1"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(
                True, 98.32, "p5.48xlarge", "us-east-1"
            ),
            "get_sagemaker_quota": _mock_quota(32, "p5.48xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                2, 8, "p5.48xlarge", "us-east-1"
            ),
            "check_existing_odcrs": _mock_odcrs(0, "p5.48xlarge", "us-east-1"),
            # Capacity Blocks available — the mitigation path exists
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "available", "p5.48xlarge", "us-east-1", offerings=3
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "p5.48xlarge", "us-east-1"
            ),
            "get_ice_metrics": _mock_ice_metrics(
                "isolated", 2, "p5.48xlarge", "us-east-1"
            ),
            "get_ice_history": _mock_ice_history(
                "clean", 0, "p5.48xlarge", "us-east-1"
            ),
            "find_alternative_instances": _mock_alternatives(
                "p5.48xlarge", "us-east-1", []
            ),
            "get_reference_data": _mock_reference("gpu_chips", "p5"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "p5.48xlarge", "", "us-east-1"
            ),
        },
        # Weak SPS (2/10) triggers sps_ceiling_critical → final capped at 0.4
        expected_verdict="HIGH RISK",
        required_concepts=[
            ["HIGH RISK"],
            ["Capacity Block", "capacity blocks"],
            ["p5.48xlarge"],
        ],
    ),
    Scenario(
        name="training_plans_recommended",
        query=(
            "I need 16 ml.p5en.48xlarge in us-east-1 for a 60-day foundation-model "
            "pre-training run starting next month. What's the right way to lock in "
            "predictable capacity?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "p5en.48xlarge", "us-east-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 115.56, "p5en.48xlarge", "us-east-1"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(
                True, 109.78, "p5en.48xlarge", "us-east-1"
            ),
            "get_sagemaker_quota": _mock_quota(64, "p5en.48xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                2, 16, "p5en.48xlarge", "us-east-1"
            ),
            "check_existing_odcrs": _mock_odcrs(0, "p5en.48xlarge", "us-east-1"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "p5en.48xlarge", "us-east-1"
            ),
            # Flexible Training Plans available for the multi-month ask
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "available", "p5en.48xlarge", "us-east-1", offerings=4
            ),
            "get_ice_metrics": _mock_ice_metrics(
                "isolated", 2, "p5en.48xlarge", "us-east-1"
            ),
            "get_ice_history": _mock_ice_history(
                "clean", 0, "p5en.48xlarge", "us-east-1"
            ),
            "find_alternative_instances": _mock_alternatives(
                "p5en.48xlarge", "us-east-1", []
            ),
            "get_reference_data": _mock_reference("gpu_chips", "p5en"),
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "unavailable", "p5en.48xlarge", "", "us-east-1"
            ),
        },
        expected_verdict="HIGH RISK",
        required_concepts=[
            ["HIGH RISK"],
            ["Flexible Training Plan", "Training Plan", "training plan"],
            ["p5en.48xlarge"],
        ],
    ),
    Scenario(
        name="heterogeneous_endpoint_fallback_recommended",
        query=(
            "I want to deploy a 7B model on ml.g6e.xlarge for real-time inference "
            "in us-east-1. On-demand capacity has been thin for this instance type "
            "in this region. What should I do?"
        ),
        tool_stubs={
            "check_instance_availability": _mock_availability(
                True, "g6e.xlarge", "us-east-1"
            ),
            "get_sagemaker_pricing": _mock_pricing(
                True, 1.861, "g6e.xlarge", "us-east-1"
            ),
            "get_ec2_pricing": _mock_ec2_pricing(
                True, 1.767, "g6e.xlarge", "us-east-1"
            ),
            "get_sagemaker_quota": _mock_quota(50, "g6e.xlarge"),
            "get_spot_placement_score": _mock_spot_score(
                3, 4, "g6e.xlarge", "us-east-1"
            ),
            "check_existing_odcrs": _mock_odcrs(0, "g6e.xlarge", "us-east-1"),
            "get_capacity_blocks_offerings": _mock_capacity_blocks(
                "unavailable", "g6e.xlarge", "us-east-1"
            ),
            "get_sagemaker_training_plan_offerings": _mock_training_plans(
                "unavailable", "g6e.xlarge", "us-east-1"
            ),
            # The star of this scenario: heterogeneous fallback IS available
            "evaluate_heterogeneous_endpoint_fallback": _mock_heterogeneous(
                "available", "g6e.xlarge", "g7.12xlarge", "us-east-1"
            ),
            "get_ice_metrics": _mock_ice_metrics(
                "isolated", 2, "g6e.xlarge", "us-east-1"
            ),
            "get_ice_history": _mock_ice_history("clean", 0, "g6e.xlarge", "us-east-1"),
            "find_alternative_instances": _mock_alternatives(
                "g6e.xlarge",
                "us-east-1",
                [{"instance_type": "g7.12xlarge", "similarity_score": 0.92, "gpus": 4}],
            ),
            "get_reference_data": _mock_reference("gpu_chips", "g6e"),
        },
        expected_verdict="HIGH RISK",
        required_concepts=[
            ["HIGH RISK"],
            ["g6e.xlarge"],
            [
                "capacity-aware endpoint",
                "capacity aware endpoint",
                "heterogeneous endpoint",
                "instance pool",
                "fallback",
            ],
            ["g7.12xlarge"],
        ],
    ),
]


# ---------- Helpers ----------


def _make_handler(canned_output: dict):
    """Return a closure that returns ``canned_output`` regardless of args.

    The agent calls ``handler(**args)``, so the handler must accept
    arbitrary keyword arguments; we discard them because scenario-level
    determinism is the whole point.
    """

    def _stub(**_kwargs):
        return canned_output

    return _stub


def _assert_concept_present(answer: str, synonyms: list[str]) -> None:
    """At least one of ``synonyms`` must appear in ``answer`` (case-
    insensitive substring)."""
    answer_lc = answer.lower()
    for phrase in synonyms:
        if phrase.lower() in answer_lc:
            return
    raise AssertionError(
        f"Expected at least one of {synonyms} in response answer; "
        f"got (first 400 chars): {answer[:400]}"
    )


def _assert_phrase_absent(answer: str, forbidden: str) -> None:
    assert forbidden.lower() not in answer.lower(), (
        f"Response answer should NOT contain {forbidden!r}; "
        f"got (first 400 chars): {answer[:400]}"
    )


def _run_scenario(scenario: Scenario) -> dict:
    """Build an agent with scenario-specific tool stubs and run the query.

    Uses the real Bedrock client — no mocking of boto3 — so this is a
    live LLM call. The agent's deterministic scorer runs over the mocked
    tool outputs and produces ``confidence_assessments[]`` the way the
    production path would.
    """
    agent = CapacityDecisionAgent(
        max_iterations=10,
        max_tokens=4096,
        temperature=0.0,
        verbose=False,
    )
    # Register every real tool first (so TOOL_SPEC schemas + descriptions
    # match production). Then overwrite handlers with the scenario stubs.
    for spec in ALL_TOOLS:
        agent.register_tool(**spec)
    for tool_name, canned in scenario.tool_stubs.items():
        if tool_name not in agent.tools:
            raise AssertionError(
                f"Scenario {scenario.name!r} mocks tool {tool_name!r} that "
                "is not registered — did a tool get renamed?"
            )
        agent.tools[tool_name]["handler"] = _make_handler(canned)
    return agent.run(scenario.query)


# ---------- Parametrized test ----------


@pytest.mark.parametrize(
    "scenario",
    SCENARIOS,
    ids=[s.name for s in SCENARIOS],
)
def test_llm_regression_scenario(scenario: Scenario) -> None:
    """Each scenario: real Bedrock call + stubbed tools + assertions.

    Assertions:
      1. ``confidence_assessments[0].confidence.verdict`` matches
         ``scenario.expected_verdict``. This is the deterministic
         scorer output, independent of the LLM narrative. Regressing
         here means the signal extractor or scorer semantics drifted.
      2. For each required concept group, at least one synonym appears
         in the markdown ``answer``. Regressing here usually means the
         LLM dropped a mitigation recommendation or verdict label from
         its narration.
      3. No forbidden phrase appears. Regressing here means the LLM
         started inventing verdict categories outside the four
         canonical labels — a system-prompt-drift signal.
    """
    result = _run_scenario(scenario)

    # Assertion 1 — deterministic verdict.
    assessments = result.get("confidence_assessments") or []
    assert assessments, (
        f"[{scenario.name}] No confidence_assessments produced. "
        f"stop_reason={result.get('stop_reason')!r}, "
        f"tool_calls={len(result.get('tool_calls') or [])}"
    )
    verdict = assessments[0]["confidence"]["verdict"]
    assert verdict == scenario.expected_verdict, (
        f"[{scenario.name}] Expected verdict {scenario.expected_verdict!r}, "
        f"got {verdict!r}. Full first assessment: {assessments[0]}"
    )

    # Assertion 2 — required narrative concepts.
    answer = result.get("final_text") or ""
    for synonyms in scenario.required_concepts:
        _assert_concept_present(answer, synonyms)

    # Assertion 3 — forbidden phrases absent.
    for forbidden in scenario.forbidden_phrases:
        _assert_phrase_absent(answer, forbidden)
