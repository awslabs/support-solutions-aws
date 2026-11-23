# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the heterogeneous_endpoint tool.

Covers:
- _recommend_tp math (fits/doesn't-fit at various model sizes + GPU
  memory shapes)
- _pairing_score hard disqualifiers (cross-vendor, not offered, not
  SageMaker-priced)
- _pairing_score composite math (memory + cost weighting)
- End-to-end tool happy path: primary + 2 fallbacks, Pricing API
  cooperates, example config emitted
- Hard-disqualifier end-to-end: every candidate fails one gate, tool
  returns capacity_signal='unavailable'
- Unknown-primary error path
- TOOL_SPEC contract (name, required args, handler binding)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.tools.heterogeneous_endpoint import (
    TOOL_SPEC,
    _pairing_score,
    _recommend_tp,
    evaluate_heterogeneous_endpoint_fallback,
)


# ---------- Specs helpers ----------


def _spec(
    bare: str,
    gpu_count: int,
    gpu_mem_gb: float,
    manufacturer: str = "NVIDIA",
    gpu_name: str = "L40S",
    vcpus: int = 4,
    mem_gib: float = 16.0,
) -> dict:
    return {
        "instance_type": bare,
        "vcpus": vcpus,
        "memory_gib": mem_gib,
        "gpu_count": gpu_count,
        "gpu_memory_gb_per_gpu": gpu_mem_gb,
        "gpu_total_memory_gb": round(gpu_count * gpu_mem_gb, 1),
        "gpu_manufacturer": manufacturer,
        "gpu_name": gpu_name,
    }


# ---------- _recommend_tp ----------


class TestRecommendTp:
    def test_none_model_size_skips_recommendation(self):
        result = _recommend_tp(None, gpu_count=4, gpu_memory_gb_per_gpu=32)
        assert result["fits"] is None
        assert "TP recommendation skipped" in result["note"]

    def test_7b_fits_at_tp1_on_48gb_single_gpu(self):
        """7B at FP16 ≈ 18 GB with KV cache; fits comfortably on g6e.xlarge."""
        result = _recommend_tp(model_size_b=7.0, gpu_count=1, gpu_memory_gb_per_gpu=48)
        assert result["fits"] is True
        assert result["recommended_tp"] == 1
        assert result["memory_per_gpu_gb_at_recommended_tp"] < 20

    def test_7b_fits_at_tp1_on_32gb_single_gpu(self):
        """7B at FP16 ≈ 18 GB; fits on a single RTX PRO 4500 Blackwell (32 GB)."""
        result = _recommend_tp(model_size_b=7.0, gpu_count=1, gpu_memory_gb_per_gpu=32)
        assert result["fits"] is True
        assert result["recommended_tp"] == 1

    def test_7b_fits_at_tp1_on_32gb_quad_gpu(self):
        """Same 7B on g7.12xlarge (4x 32 GB). TP=1 still fits with lots of headroom."""
        result = _recommend_tp(model_size_b=7.0, gpu_count=4, gpu_memory_gb_per_gpu=32)
        assert result["fits"] is True
        assert result["recommended_tp"] == 1
        assert result["max_tp_available"] == 4

    def test_70b_requires_tp8_on_80gb_h100(self):
        """70B at FP16 ≈ 182 GB with KV cache. TP=4 on 80GB → 45.5 per GPU,
        fits in 85% utilization (68 GB)."""
        result = _recommend_tp(model_size_b=70.0, gpu_count=8, gpu_memory_gb_per_gpu=80)
        assert result["fits"] is True
        assert result["recommended_tp"] in (4, 8)
        assert result["min_tp_to_fit"] in (4, 8)

    def test_70b_does_not_fit_on_small_instance(self):
        """70B at FP16 does not fit on a single-GPU 24 GB instance."""
        result = _recommend_tp(model_size_b=70.0, gpu_count=1, gpu_memory_gb_per_gpu=24)
        assert result["fits"] is False
        assert result["recommended_tp"] is None
        assert "does not fit" in result["note"]

    def test_zero_gpus_returns_doesnt_fit(self):
        result = _recommend_tp(model_size_b=7.0, gpu_count=0, gpu_memory_gb_per_gpu=0)
        assert result["fits"] is False
        assert "non-ML instance" in result["note"]


# ---------- _pairing_score ----------


class TestPairingScore:
    def test_cross_vendor_disqualified(self):
        primary = _spec("g6e.xlarge", 1, 48, "NVIDIA")
        fallback = _spec("inf2.xlarge", 1, 16, "AWS")
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=1.0,
            primary_price=1.0,
            available_in_region=True,
            sagemaker_priced=True,
        )
        assert score == 0.0
        assert any("vendor mismatch" in c for c in concerns)

    def test_not_available_in_region_disqualified(self):
        primary = _spec("g6e.xlarge", 1, 48)
        fallback = _spec("g7.12xlarge", 4, 32)
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=7.0,
            primary_price=1.8,
            available_in_region=False,
            sagemaker_priced=True,
        )
        assert score == 0.0
        assert any("not offered" in c for c in concerns)

    def test_not_sagemaker_priced_disqualified(self):
        primary = _spec("g6e.xlarge", 1, 48)
        fallback = _spec("g7.12xlarge", 4, 32)
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=None,
            primary_price=1.8,
            available_in_region=True,
            sagemaker_priced=False,
        )
        assert score == 0.0
        assert any("no SageMaker Hosting record" in c for c in concerns)

    def test_perfect_pairing_scores_near_1(self):
        """Same vendor + bigger memory + same cost → 0.7*1.0 + 0.3*1.0 = 1.0."""
        primary = _spec("g6e.xlarge", 1, 48)
        fallback = _spec("g6e.2xlarge", 1, 48)
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=1.8,
            primary_price=1.8,
            available_in_region=True,
            sagemaker_priced=True,
        )
        assert score == 1.0

    def test_cost_delta_pulls_score_down(self):
        """Same vendor + same memory + 3x cost → cost_score=0 →
        total = 0.7 * memory_fit + 0.3 * 0 = 0.7"""
        primary = _spec("g6e.xlarge", 1, 48)
        fallback = _spec("g7.12xlarge", 4, 12)  # 48 GB total — same as primary
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=5.4,
            primary_price=1.8,  # 3x
            available_in_region=True,
            sagemaker_priced=True,
        )
        assert score == pytest.approx(0.7, abs=0.01)
        assert any("3.0x primary" in c for c in concerns)

    def test_smaller_memory_pulls_score_down(self):
        """Same vendor + significantly smaller memory + same cost.
        48 GB primary vs. 16 GB fallback → memory_fit ≈ 0.33 →
        total ≈ 0.7 * 0.33 + 0.3 * 1.0 = 0.53. The 'aggressive TP
        splitting' concern fires because memory_fit < 0.5."""
        primary = _spec("g6e.xlarge", 1, 48)
        fallback = _spec("g5.small", 1, 16)
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=1.8,
            primary_price=1.8,
            available_in_region=True,
            sagemaker_priced=True,
        )
        assert score == pytest.approx(0.53, abs=0.02)
        assert any("may require aggressive TP splitting" in c for c in concerns)

    def test_half_memory_no_tp_concern_but_lower_score(self):
        """48 GB primary vs. 24 GB fallback → memory_fit = 0.5 exactly.
        Threshold is strict < 0.5, so no 'aggressive TP' concern fires,
        but the score still drops to 0.65."""
        primary = _spec("g6e.xlarge", 1, 48)
        fallback = _spec("g5.xlarge", 1, 24)
        score, concerns = _pairing_score(
            primary,
            fallback,
            fallback_price=1.8,
            primary_price=1.8,
            available_in_region=True,
            sagemaker_priced=True,
        )
        assert score == pytest.approx(0.65, abs=0.01)
        assert not any("aggressive TP splitting" in c for c in concerns)


# ---------- End-to-end tool ----------


class TestEvaluateHeterogeneousEndpointFallback:
    @patch("src.tools.heterogeneous_endpoint.get_sagemaker_pricing")
    @patch("src.tools.heterogeneous_endpoint._offered_gpu_types_in_region")
    @patch("src.tools.heterogeneous_endpoint._get_instance_specs")
    def test_happy_path_two_fallbacks_ranked(
        self, mock_specs, mock_offered, mock_pricing
    ):
        """Primary g6e.xlarge; region has g7.12xlarge (good fit) and
        g5.xlarge (smaller memory, lower score)."""
        mock_offered.return_value = {"g6e.xlarge", "g7.12xlarge", "g5.xlarge"}

        specs_by_type = {
            "g6e.xlarge": _spec("g6e.xlarge", 1, 48, gpu_name="L40S"),
            "g7.12xlarge": _spec(
                "g7.12xlarge", 4, 32, gpu_name="RTX PRO 4500 Blackwell"
            ),
            "g5.xlarge": _spec("g5.xlarge", 1, 24, gpu_name="A10G"),
        }
        mock_specs.side_effect = lambda bt, r: specs_by_type.get(bt)

        prices = {
            "g6e.xlarge": 1.861,
            "g7.12xlarge": 7.44,
            "g5.xlarge": 1.408,
        }
        mock_pricing.side_effect = lambda instance_type, region: {
            "hourly_usd": prices.get(instance_type),
            "available": instance_type in prices,
        }

        result = evaluate_heterogeneous_endpoint_fallback(
            primary_instance_type="ml.g6e.xlarge",
            region="us-east-1",
            model_size_b=7.0,
            target_count=2,
        )

        assert result["capacity_signal"] == "available"
        assert result["primary"]["instance_type"] == "g6e.xlarge"
        assert result["primary"]["hourly_usd"] == 1.861
        assert result["primary"]["tp_guidance"]["recommended_tp"] == 1

        assert len(result["fallback_candidates"]) == 2
        # g7.12xlarge has 128 GB total GPU memory (4x32) vs primary's 48 GB.
        # Same vendor, same region, same price feed. Should rank first.
        top = result["fallback_candidates"][0]
        assert top["instance_type"] == "g7.12xlarge"
        assert top["pairing_score"] > 0.6
        assert top["tp_guidance"]["fits"] is True
        assert top["tp_guidance"]["recommended_tp"] in (1, 2)

        # Deployment recommendation uses the top candidate.
        deploy = result["deployment_recommendation"]
        assert deploy["pattern"] == "capacity_aware_instance_fallback"
        variants = deploy["example_production_variants"]
        assert variants[0]["InstanceType"] == "ml.g6e.xlarge"
        assert variants[1]["InstanceType"] == "ml.g7.12xlarge"
        assert variants[0]["InitialInstanceCount"] == 2

    @patch("src.tools.heterogeneous_endpoint.get_sagemaker_pricing")
    @patch("src.tools.heterogeneous_endpoint._offered_gpu_types_in_region")
    @patch("src.tools.heterogeneous_endpoint._get_instance_specs")
    def test_no_valid_fallbacks_returns_unavailable(
        self, mock_specs, mock_offered, mock_pricing
    ):
        """Region only offers a cross-vendor option — no valid pairing."""
        mock_offered.return_value = {"g6e.xlarge", "inf2.xlarge"}
        specs_by_type = {
            "g6e.xlarge": _spec("g6e.xlarge", 1, 48, "NVIDIA"),
            "inf2.xlarge": _spec("inf2.xlarge", 1, 16, "AWS"),
        }
        mock_specs.side_effect = lambda bt, r: specs_by_type.get(bt)
        mock_pricing.side_effect = lambda instance_type, region: {
            "hourly_usd": 1.0,
            "available": True,
        }

        result = evaluate_heterogeneous_endpoint_fallback(
            primary_instance_type="g6e.xlarge", region="us-east-1"
        )
        assert result["capacity_signal"] == "unavailable"
        assert result["fallback_candidates"] == []
        assert result["deployment_recommendation"] is None
        assert "No valid heterogeneous-endpoint fallback" in result["interpretation"]

    @patch("src.tools.heterogeneous_endpoint._get_instance_specs")
    def test_unknown_primary_returns_unknown(self, mock_specs):
        mock_specs.return_value = None
        result = evaluate_heterogeneous_endpoint_fallback(
            primary_instance_type="g99.notreal", region="us-east-1"
        )
        assert result["capacity_signal"] == "unknown"
        assert result["fallback_candidates"] == []
        assert "Could not look up specs" in result["interpretation"]

    @patch("src.tools.heterogeneous_endpoint._get_instance_specs")
    def test_api_error_returns_unknown(self, mock_specs):
        mock_specs.side_effect = RuntimeError("STS unavailable")
        result = evaluate_heterogeneous_endpoint_fallback(
            primary_instance_type="g6e.xlarge", region="us-east-1"
        )
        assert result["capacity_signal"] == "unknown"
        assert "RuntimeError" in result["interpretation"]
        assert "STS unavailable" in result["interpretation"]

    @patch("src.tools.heterogeneous_endpoint.get_sagemaker_pricing")
    @patch("src.tools.heterogeneous_endpoint._offered_gpu_types_in_region")
    @patch("src.tools.heterogeneous_endpoint._get_instance_specs")
    def test_ml_prefix_stripped(self, mock_specs, mock_offered, mock_pricing):
        """ml.g6e.xlarge → g6e.xlarge for the EC2 API lookup."""
        mock_specs.side_effect = lambda bt, r: (
            _spec("g6e.xlarge", 1, 48) if bt == "g6e.xlarge" else None
        )
        mock_offered.return_value = {"g6e.xlarge"}
        mock_pricing.return_value = {"hourly_usd": 1.8, "available": True}

        result = evaluate_heterogeneous_endpoint_fallback(
            primary_instance_type="ml.g6e.xlarge", region="us-east-1"
        )
        assert result["primary"]["instance_type"] == "g6e.xlarge"
        assert result["primary"]["sagemaker_instance_type"] == "ml.g6e.xlarge"


# ---------- TOOL_SPEC ----------


class TestToolSpec:
    def test_tool_spec_name_and_handler(self):
        assert TOOL_SPEC["name"] == "evaluate_heterogeneous_endpoint_fallback"
        assert TOOL_SPEC["handler"] is evaluate_heterogeneous_endpoint_fallback

    def test_tool_spec_required_args(self):
        schema = TOOL_SPEC["input_schema"]
        assert set(schema["required"]) == {"primary_instance_type", "region"}

    def test_tool_spec_optional_args_documented(self):
        props = TOOL_SPEC["input_schema"]["properties"]
        assert set(props) == {
            "primary_instance_type",
            "region",
            "target_count",
            "model_size_b",
            "max_fallbacks",
        }

    def test_tool_spec_description_mentions_blog_and_docs(self):
        """The description must point the LLM at the specific SageMaker
        docs so the agent cites authoritative sources in its narrative."""
        desc = TOOL_SPEC["description"]
        assert "capacity-aware" in desc.lower()
        assert "sagemaker" in desc.lower()
        assert "heterogeneous" in desc.lower()
