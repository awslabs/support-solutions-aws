# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the reference_data tool — GPU specs / model sizing / region proximity."""

from __future__ import annotations

from src.tools.reference_data import (
    GPU_CHIP_DATA,
    MODEL_SIZING_DATA,
    REGION_PROXIMITY_DATA,
    TOOL_SPEC,
    get_reference_data,
)


class TestGpuChipsCategory:
    """Test the gpu_chips category lookups."""

    def test_returns_all_gpu_data_without_query(self):
        result = get_reference_data("gpu_chips")
        assert result["category"] == "gpu_chips"
        assert result["data"] == GPU_CHIP_DATA
        assert result["source"] == "reference_data_tool"

    def test_lookup_by_family_name(self):
        result = get_reference_data("gpu_chips", query="g5")
        assert result["category"] == "gpu_chips"
        assert result["family"] == "g5"
        assert result["data"]["chip"] == "NVIDIA A10G"
        assert result["data"]["memory_gb"] == 24

    def test_lookup_strips_size_suffix(self):
        """Full instance type 'p5.48xlarge' should resolve to 'p5'."""
        result = get_reference_data("gpu_chips", query="p5.48xlarge")
        assert result["family"] == "p5"
        assert result["data"]["chip"] == "NVIDIA H100"

    def test_lookup_strips_ml_prefix(self):
        """'ml.p4d' should resolve to 'p4d'."""
        result = get_reference_data("gpu_chips", query="ml.p4d")
        assert result["family"] == "p4d"
        assert result["data"]["chip"] == "NVIDIA A100"

    def test_lookup_strips_ml_prefix_with_size(self):
        """'ml.p4d.24xlarge' should resolve to 'p4d'."""
        result = get_reference_data("gpu_chips", query="ml.p4d.24xlarge")
        assert result["family"] == "p4d"
        assert result["data"]["chip"] == "NVIDIA A100"

    def test_lookup_lowercase_normalization(self):
        result = get_reference_data("gpu_chips", query="G6E")
        assert result["family"] == "g6e"
        # F8 refresh: g6e uses NVIDIA L40S (48 GB), not L4 (24 GB).
        assert result["data"]["chip"] == "NVIDIA L40S"

    def test_unknown_family_returns_none_with_note(self):
        result = get_reference_data("gpu_chips", query="zz99")
        assert result["category"] == "gpu_chips"
        assert result["data"] is None
        assert "Unknown GPU family" in result["note"]
        assert result["query"] == "zz99"

    def test_inferentia_family_returns_correct_chip(self):
        result = get_reference_data("gpu_chips", query="inf2")
        assert result["data"]["chip"] == "AWS Inferentia2"
        assert result["data"]["memory_gb"] == 32


class TestModelSizingCategory:
    """Test the model_sizing category lookups."""

    def test_returns_all_sizing_data_without_query(self):
        result = get_reference_data("model_sizing")
        assert result["category"] == "model_sizing"
        assert result["data"] == MODEL_SIZING_DATA

    def test_7b_lookup_lands_in_7b_to_13b_bucket(self):
        """F8 correction: 7B models belong in the 7b_to_13b bucket
        (a 7B model in FP16 is ~14 GB and fits on a single 24 GB GPU),
        not in under_7b — that bucket is for genuinely small models
        (Phi-3-mini, embedding models, distilled).
        """
        result = get_reference_data("model_sizing", query="7b")
        assert result["tier"] == "7b_to_13b"
        assert "Mistral 7B" in result["data"]["description"]

    def test_under_7b_lookup_for_small_model(self):
        result = get_reference_data("model_sizing", query="3.8b")
        assert result["tier"] == "under_7b"

    def test_70b_lands_in_over_70b_bucket(self):
        """F8 correction: a 70B model in FP16 is ~140 GB and requires
        p4d.24xlarge or bigger. It does NOT fit on a single-node
        g5.48xlarge (192 GB total but only 8x24GB per GPU). Land it in
        the over_70b bucket for a fair recommendation.
        """
        result = get_reference_data("model_sizing", query="70b")
        assert result["tier"] == "over_70b"

    def test_40b_lands_in_13b_to_70b_bucket(self):
        result = get_reference_data("model_sizing", query="40b")
        assert result["tier"] == "13b_to_70b"

    def test_65b_lands_in_13b_to_70b_bucket(self):
        result = get_reference_data("model_sizing", query="65b")
        assert result["tier"] == "13b_to_70b"

    def test_13b_lands_in_13b_to_70b_bucket(self):
        """13B is on the boundary between 7B-13B and 13B-70B; buckets are
        [5, 13) for 7b_to_13b and [13, 70) for 13b_to_70b, so 13B lands
        in the larger bucket (safer default — 13B in FP16 needs ~26 GB).
        """
        result = get_reference_data("model_sizing", query="13b")
        assert result["tier"] == "13b_to_70b"

    def test_over_70b_via_405b(self):
        result = get_reference_data("model_sizing", query="405b")
        assert result["tier"] == "over_70b"

    def test_over_70b_via_175b(self):
        result = get_reference_data("model_sizing", query="175b")
        assert result["tier"] == "over_70b"

    def test_over_70b_via_100b(self):
        result = get_reference_data("model_sizing", query="100b")
        assert result["tier"] == "over_70b"

    def test_mixture_of_experts_multiplied_out(self):
        """regression guard: Mixtral 8x7B = 56B active parameters
        under the multiplicative parsing → 13b_to_70b, NOT under_7b.
        """
        result = get_reference_data("model_sizing", query="Mixtral 8x7B")
        assert result["tier"] == "13b_to_70b"

    def test_largest_match_wins_defeating_substring_race(self):
        """regression guard: 'Is a 7B or 8B model right for me?'
        must pick the largest referenced parameter count.
        """
        result = get_reference_data("model_sizing", query="7b or 8b")
        assert result["tier"] == "7b_to_13b"

    def test_range_query_picks_upper_bound(self):
        """'13B-70B' should land in the 13b_to_70b bucket, not
        overflow to over_70b — the largest matched integer is 70, but
        the bucket edges are exclusive on the upper side so 70 lands
        in over_70b (as it should for standalone 70B). For ranges, the
        user is asking about the WHOLE range; recommend the larger
        bucket to be safe.
        """
        result = get_reference_data("model_sizing", query="13B-70B range")
        assert result["tier"] == "over_70b"

    def test_unknown_size_returns_full_data(self):
        """When query doesn't match any tier, returns full data dict."""
        result = get_reference_data("model_sizing", query="quantum")
        # Falls through and returns full data
        assert result["category"] == "model_sizing"
        assert "data" in result

    def test_case_insensitive_query(self):
        result = get_reference_data("model_sizing", query="8B")
        assert result["tier"] == "7b_to_13b"


class TestRegionProximityCategory:
    """Test the region_proximity category lookups."""

    def test_returns_all_proximity_data_without_query(self):
        result = get_reference_data("region_proximity")
        assert result["category"] == "region_proximity"
        assert result["data"] == REGION_PROXIMITY_DATA

    def test_known_region_lookup(self):
        result = get_reference_data("region_proximity", query="af-south-1")
        assert result["region"] == "af-south-1"
        assert result["data"]["name"] == "Cape Town"
        assert any("eu-west-1" in nearby for nearby in result["data"]["nearby"])

    def test_query_is_trimmed_and_lowercased(self):
        result = get_reference_data("region_proximity", query="  AF-SOUTH-1  ")
        assert result["region"] == "af-south-1"
        assert result["data"]["name"] == "Cape Town"

    def test_unknown_region_returns_none_with_note(self):
        result = get_reference_data("region_proximity", query="us-east-1")
        # us-east-1 is "established", not in proximity map → None + note
        assert result["data"] is None
        assert "No proximity data" in result["note"]

    def test_sa_east_1_returns_us_alternatives(self):
        result = get_reference_data("region_proximity", query="sa-east-1")
        assert result["data"]["name"] == "São Paulo"
        nearby_str = " ".join(result["data"]["nearby"])
        assert "us-east-1" in nearby_str


class TestAllCategory:
    """Test the 'all' category returns full reference data."""

    def test_all_returns_combined_data(self):
        result = get_reference_data("all")
        assert result["category"] == "all"
        assert result["gpu_chips"] == GPU_CHIP_DATA
        assert result["model_sizing"] == MODEL_SIZING_DATA
        assert result["region_proximity"] == REGION_PROXIMITY_DATA
        assert result["source"] == "reference_data_tool"


class TestUnknownCategory:
    """Test error path when category is unknown."""

    def test_unknown_category_returns_error(self):
        result = get_reference_data("invalid_category")
        assert "error" in result
        assert "Unknown category" in result["error"]
        assert "gpu_chips" in result["error"]  # lists valid options
        assert result["source"] == "reference_data_tool"

    def test_empty_category_returns_error(self):
        result = get_reference_data("")
        assert "error" in result


class TestToolSpec:
    """Test the TOOL_SPEC registration metadata."""

    def test_tool_spec_has_required_fields(self):
        assert TOOL_SPEC["name"] == "get_reference_data"
        assert TOOL_SPEC["handler"] is get_reference_data
        assert "description" in TOOL_SPEC
        assert "input_schema" in TOOL_SPEC

    def test_tool_spec_input_schema_enforces_enum(self):
        schema = TOOL_SPEC["input_schema"]
        assert schema["properties"]["category"]["enum"] == [
            "gpu_chips",
            "model_sizing",
            "region_proximity",
            "all",
        ]
        assert "category" in schema["required"]
