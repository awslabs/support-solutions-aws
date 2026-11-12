# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for region_model.py — region capacity tier model."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.region_model import (
    REGION_CAPACITY_DATA,
    TIER_SCORES,
    _get_data,
    _load_custom_data,
    get_region_assessment,
    get_region_score,
    get_region_tier,
)


class TestGetRegionTier:
    """Test the get_region_tier lookup function."""

    def test_known_region_overall_tier(self):
        assert get_region_tier("us-east-1") == "established"
        assert get_region_tier("eu-central-1") == "moderate"
        assert get_region_tier("af-south-1") == "limited"

    def test_unknown_region_returns_unknown(self):
        assert get_region_tier("xx-fake-99") == "unknown"

    def test_known_region_with_gpu_family(self):
        # us-east-1 has g5 = established
        assert get_region_tier("us-east-1", gpu_family="g5") == "established"
        # sa-east-1 has p5 = none
        assert get_region_tier("sa-east-1", gpu_family="p5") == "none"
        # af-south-1 has g5 = limited
        assert get_region_tier("af-south-1", gpu_family="g5") == "limited"

    def test_gpu_family_falls_back_to_overall_when_missing(self):
        """If a family isn't listed for a region, falls back to _overall."""
        # ca-central-1 has _overall=moderate but no p5 entry → falls back to moderate
        result = get_region_tier("ca-central-1", gpu_family="p5")
        assert result == "moderate"

    def test_gpu_family_strips_size_suffix(self):
        """'g5.2xlarge' should be normalized to 'g5'."""
        assert get_region_tier("us-east-1", gpu_family="g5.2xlarge") == "established"

    def test_gpu_family_strips_ml_prefix(self):
        """'ml.p4d.24xlarge' should be normalized to 'p4d'."""
        assert (
            get_region_tier("us-east-1", gpu_family="ml.p4d.24xlarge") == "established"
        )

    def test_gpu_family_for_unknown_region(self):
        assert get_region_tier("fake-region", gpu_family="g5") == "unknown"

    def test_p5e_in_us_east_1_is_moderate(self):
        # p5e is newer, only moderate even in established us-east-1
        assert get_region_tier("us-east-1", gpu_family="p5e") == "moderate"


class TestGetRegionScore:
    """Test the numeric scoring conversion."""

    def test_established_score_is_1(self):
        assert get_region_score("us-east-1") == 1.0
        assert get_region_score("us-east-1", gpu_family="g5") == 1.0

    def test_moderate_score_is_06(self):
        assert get_region_score("eu-central-1") == 0.6

    def test_limited_score_is_03(self):
        assert get_region_score("af-south-1") == 0.3

    def test_none_tier_score_is_0(self):
        assert get_region_score("af-south-1", gpu_family="g6") == 0.0

    def test_unknown_region_returns_default_05(self):
        """Unknown regions default to 0.5 (neutral)."""
        assert get_region_score("xx-fake-99") == 0.5

    def test_tier_scores_dict_complete(self):
        """All four tiers map to numeric scores."""
        assert TIER_SCORES["established"] == 1.0
        assert TIER_SCORES["moderate"] == 0.6
        assert TIER_SCORES["limited"] == 0.3
        assert TIER_SCORES["none"] == 0.0


class TestGetRegionAssessment:
    """Test the structured assessment output."""

    def test_established_region_no_risk_factors(self):
        result = get_region_assessment("us-east-1", "g5.2xlarge")

        assert result["region"] == "us-east-1"
        assert result["instance_family"] == "g5"
        assert result["overall_tier"] == "established"
        assert result["family_tier"] == "established"
        assert result["overall_score"] == 1.0
        assert result["family_score"] == 1.0
        # Established region, established family — no risk factors
        assert result["risk_factors"] == []
        assert result["recommendations"] == []
        assert "region_capacity_model" in result["source"]

    def test_strips_ml_prefix_from_instance_type(self):
        result = get_region_assessment("us-east-1", "ml.p4d.24xlarge")
        assert result["instance_family"] == "p4d"

    def test_limited_region_adds_risk_factor(self):
        result = get_region_assessment("af-south-1", "g5.2xlarge")

        assert result["overall_tier"] == "limited"
        assert any("newer/limited-capacity" in rf for rf in result["risk_factors"])

    def test_family_not_available_marks_recommendations(self):
        """When family_tier is 'none', strong recommendations are emitted."""
        result = get_region_assessment("af-south-1", "g6.xlarge")

        assert result["family_tier"] == "none"
        assert any("NOT available" in rf for rf in result["risk_factors"])
        assert any("ODCR" in rec for rec in result["recommendations"])
        assert any("planned GA date" in rec for rec in result["recommendations"])

    def test_limited_family_recommends_odcr(self):
        result = get_region_assessment("af-south-1", "g5.xlarge")

        assert result["family_tier"] == "limited"
        assert any("shallow" in rf for rf in result["risk_factors"])
        assert any("Pre-create ODCR" in rec for rec in result["recommendations"])
        assert any("alternative regions" in rec for rec in result["recommendations"])

    def test_moderate_family_adds_monitoring_note(self):
        result = get_region_assessment("eu-central-1", "g5.xlarge")

        # eu-central-1 has g5=established but ca-central-1 has g5=moderate
        # Use eu-central-1 with g6 instead — that's moderate
        result = get_region_assessment("eu-central-1", "g6.xlarge")
        assert result["family_tier"] == "moderate"
        assert any(
            "adequate for moderate fleets" in rf for rf in result["risk_factors"]
        )
        # Moderate alone shouldn't trigger ODCR recommendation
        assert not any("Pre-create ODCR" in rec for rec in result["recommendations"])

    def test_unknown_region_returns_unknown_tiers(self):
        result = get_region_assessment("fake-region-1", "g5.xlarge")

        assert result["overall_tier"] == "unknown"
        assert result["family_tier"] == "unknown"
        assert result["overall_score"] == 0.5
        assert result["family_score"] == 0.5


class TestRegionCapacityDataIntegrity:
    """Sanity checks on the static data dict."""

    def test_all_regions_have_overall_tier(self):
        for region, data in REGION_CAPACITY_DATA.items():
            assert "_overall" in data, f"{region} missing _overall"
            assert data["_overall"] in ("established", "moderate", "limited"), (
                f"{region} has invalid _overall tier"
            )

    def test_all_tiers_are_valid(self):
        valid_tiers = {"established", "moderate", "limited", "none"}
        for region, data in REGION_CAPACITY_DATA.items():
            for family, tier in data.items():
                assert tier in valid_tiers, (
                    f"{region}.{family} has invalid tier '{tier}'"
                )


class TestCustomDataLoading:
    """Test that custom region capacity data can override defaults via JSON file."""

    def test_load_custom_data_returns_none_when_no_file_present(
        self, tmp_path, monkeypatch
    ):
        """When no custom file exists, returns None."""
        monkeypatch.setenv("REGION_CAPACITY_FILE", "")
        with patch("src.region_model.Path") as mock_path:
            # Make all paths return non-existent
            mock_instance = mock_path.return_value
            mock_instance.is_file.return_value = False
            # Path(__file__).parent is also a Path call
            assert _load_custom_data() is None

    def test_load_custom_data_from_env_var(self, tmp_path, monkeypatch):
        """REGION_CAPACITY_FILE env var points to a JSON override file."""
        custom = {
            "us-east-1": {"g5": "moderate", "_overall": "moderate"},
            "fake-region": {"g99": "established", "_overall": "established"},
        }
        custom_file = tmp_path / "custom.json"
        custom_file.write_text(json.dumps(custom))

        monkeypatch.setenv("REGION_CAPACITY_FILE", str(custom_file))

        result = _load_custom_data()
        assert result == custom

    def test_load_custom_data_handles_invalid_json(self, tmp_path, monkeypatch):
        """Invalid JSON in custom file should not crash; falls through to next path."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not: valid json")

        monkeypatch.setenv("REGION_CAPACITY_FILE", str(bad_file))

        # _load_custom_data tries other paths after the env var fails to parse;
        # those paths won't exist in our test → None
        with patch("src.region_model.Path") as mock_path:
            # Make Path() pass through normally, but the fallback paths return non-existent
            from pathlib import Path as RealPath

            def path_factory(p):
                real = RealPath(p)
                if str(p) == str(bad_file):
                    return real
                # Fallback paths (Path(__file__).parent / ...) → non-existent stub
                stub = MagicMock()
                stub.is_file.return_value = False
                return stub

            mock_path.side_effect = path_factory
            result = _load_custom_data()
            assert result is None

    def test_get_data_uses_custom_when_available(self, tmp_path, monkeypatch):
        """_get_data prefers custom data over defaults."""
        custom = {"us-east-1": {"g5": "limited", "_overall": "limited"}}
        custom_file = tmp_path / "custom.json"
        custom_file.write_text(json.dumps(custom))

        monkeypatch.setenv("REGION_CAPACITY_FILE", str(custom_file))

        data = _get_data()
        assert data == custom
        # And get_region_tier should use it
        assert get_region_tier("us-east-1") == "limited"

    def test_get_data_returns_default_when_no_custom(self, monkeypatch):
        """When no custom file is present, returns the hardcoded default."""
        monkeypatch.delenv("REGION_CAPACITY_FILE", raising=False)
        with patch("src.region_model._load_custom_data", return_value=None):
            assert _get_data() is REGION_CAPACITY_DATA
