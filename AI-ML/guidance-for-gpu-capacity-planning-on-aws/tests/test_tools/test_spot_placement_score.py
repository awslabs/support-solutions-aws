# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the spot_placement_score tool module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools.spot_placement_score import TOOL_SPEC, get_spot_placement_score


@pytest.fixture(autouse=True)
def clear_ec2_client_cache():
    """No-op fixture preserved for test discovery (boto3 client caching was
    removed when tools moved to aws_session.get_client())."""
    yield


class TestSpotPlacementScoreHappyPath:
    """Test successful placement score retrieval and interpretation."""

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_strong_signal_score_8_or_above(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "us-east-1", "Score": 9},
            ],
        }

        result = get_spot_placement_score("g5.2xlarge", "us-east-1", target_capacity=10)

        assert result["score"] == 9
        assert result["capacity_signal"] == "strong"
        assert "Strong capacity signal" in result["interpretation"]
        assert result["instance_type"] == "g5.2xlarge"
        assert result["region"] == "us-east-1"
        assert result["target_capacity"] == 10
        assert result["source"] == "ec2.GetSpotPlacementScores"

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_moderate_signal_score_5_to_7(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "us-east-1", "Score": 6},
            ],
        }

        result = get_spot_placement_score("p4d.24xlarge", "us-east-1")

        assert result["score"] == 6
        assert result["capacity_signal"] == "moderate"
        assert "Moderate capacity signal" in result["interpretation"]
        assert "ODCR" in result["interpretation"]

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_weak_signal_score_3_to_4(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "af-south-1", "Score": 3},
            ],
        }

        result = get_spot_placement_score(
            "g5.2xlarge", "af-south-1", target_capacity=22
        )

        assert result["score"] == 3
        assert result["capacity_signal"] == "weak"
        assert "Weak capacity signal" in result["interpretation"]
        assert "ODCR strongly recommended" in result["interpretation"]

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_critical_signal_score_below_3(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "sa-east-1", "Score": 2},
            ],
        }

        result = get_spot_placement_score(
            "p5.48xlarge", "sa-east-1", target_capacity=100
        )

        assert result["score"] == 2
        assert result["capacity_signal"] == "critical"
        assert "Critical" in result["interpretation"]
        assert "very high ICE risk" in result["interpretation"]

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_strips_ml_prefix(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [{"Region": "us-east-1", "Score": 8}],
        }

        result = get_spot_placement_score("ml.g5.2xlarge", "us-east-1")

        # ml. prefix should be stripped before passing to AWS API
        assert result["instance_type"] == "g5.2xlarge"
        call_kwargs = mock_client.get_spot_placement_scores.call_args[1]
        assert call_kwargs["InstanceTypes"] == ["g5.2xlarge"]

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_az_level_scores_averaged_when_no_region_score(self, mock_factory):
        """When only AZ-level scores are returned, primary score is the average."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az1", "Score": 6},
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az2", "Score": 8},
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az3", "Score": 7},
            ],
        }

        result = get_spot_placement_score("g5.2xlarge", "us-east-1")

        # (6 + 8 + 7) / 3 = 7.0 → rounds to 7 (no bias applied).
        assert result["score"] == 7
        assert len(result["scores_by_az"]) == 3
        assert result["capacity_signal"] == "moderate"

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_az_average_rounds_half_up_not_down(self, mock_factory):
        """regression guard: previous implementation used integer
        floor-division so ``[8, 7]`` averaged to ``7`` instead of ``8``
        (round-half-up on 7.5), systematically biasing the score down
        and pushing customers into the ODCR-recommended zone
        unnecessarily.
        """
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az1", "Score": 8},
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az2", "Score": 7},
            ],
        }
        result = get_spot_placement_score("g5.2xlarge", "us-east-1")
        # (8 + 7) / 2 = 7.5 — Python's round-half-to-even gives 8.
        assert result["score"] == 8
        # And the signal is strong (>= 8), not moderate (5-7).
        assert result["capacity_signal"] == "strong"

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_az_detail_in_response(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [
                {"Region": "us-east-1", "Score": 8},
                {"Region": "us-east-1", "AvailabilityZoneId": "use1-az1", "Score": 7},
            ],
        }

        result = get_spot_placement_score("g5.xlarge", "us-east-1")

        # Both region-level and AZ-level should appear in scores_by_az
        assert len(result["scores_by_az"]) == 2
        az_ids = {s["az"] for s in result["scores_by_az"]}
        assert "region-wide" in az_ids
        assert "use1-az1" in az_ids


class TestSpotPlacementScoreEdgeCases:
    """Edge cases — empty responses, capacity clamping, etc."""

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_empty_scores_returns_unknown_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {"SpotPlacementScores": []}

        result = get_spot_placement_score("zz99.fake", "us-east-1")

        assert result["score"] is None
        assert result["capacity_signal"] == "unknown"
        assert "No placement score data available" in result["interpretation"]
        assert result["scores_by_az"] == []

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_target_capacity_clamped_to_minimum_1(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [{"Region": "us-east-1", "Score": 9}],
        }

        get_spot_placement_score("g5.xlarge", "us-east-1", target_capacity=0)

        call_kwargs = mock_client.get_spot_placement_scores.call_args[1]
        assert call_kwargs["TargetCapacity"] == 1  # clamped from 0

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_target_capacity_clamped_to_max_1000(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [{"Region": "us-east-1", "Score": 5}],
        }

        get_spot_placement_score("g5.xlarge", "us-east-1", target_capacity=5000)

        call_kwargs = mock_client.get_spot_placement_scores.call_args[1]
        assert call_kwargs["TargetCapacity"] == 1000  # clamped from 5000

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_default_target_capacity_is_1(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.return_value = {
            "SpotPlacementScores": [{"Region": "us-east-1", "Score": 8}],
        }

        result = get_spot_placement_score("g5.xlarge", "us-east-1")

        assert result["target_capacity"] == 1


class TestSpotPlacementScoreApiError:
    """Test that exceptions from boto3 are caught and returned as structured errors."""

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_unauthorized_error_returns_error_dict(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.side_effect = Exception(
            "UnauthorizedOperation: not authorized"
        )

        result = get_spot_placement_score("g5.xlarge", "us-east-1")

        assert result["score"] is None
        assert result["capacity_signal"] == "unknown"
        assert "Unable to retrieve" in result["interpretation"]
        assert "ec2:GetSpotPlacementScores" in result["interpretation"]
        assert result["source"] == "ec2.GetSpotPlacementScores"

    @patch("src.tools.spot_placement_score._ec2_client")
    def test_client_error_returns_error_dict(self, mock_factory):
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_spot_placement_scores.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "GetSpotPlacementScores",
        )

        result = get_spot_placement_score("g5.xlarge", "us-east-1")

        assert result["score"] is None
        assert result["capacity_signal"] == "unknown"
        assert "ClientError" in result["interpretation"]


class TestToolSpec:
    """Test the TOOL_SPEC registration metadata."""

    def test_tool_spec_has_required_fields(self):
        assert TOOL_SPEC["name"] == "get_spot_placement_score"
        assert TOOL_SPEC["handler"] is get_spot_placement_score
        assert "description" in TOOL_SPEC
        schema = TOOL_SPEC["input_schema"]
        assert "instance_type" in schema["required"]
        assert "region" in schema["required"]
