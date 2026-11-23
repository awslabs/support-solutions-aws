# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the instance_availability tool module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools.instance_availability import check_instance_availability


@pytest.fixture
def mock_ec2_client():
    """Create a mock EC2 client and patch the lru_cache factory."""
    client = MagicMock()
    with patch("src.tools.instance_availability._ec2_client", return_value=client):
        yield client


class TestHappyPath:
    """Test instance type found with AZ offerings."""

    def test_instance_available_with_azs(self, mock_ec2_client):
        """Instance offered in region returns available=True with AZ list."""
        mock_ec2_client.describe_instance_type_offerings.side_effect = [
            # Region-level check: instance is offered
            {
                "InstanceTypeOfferings": [
                    {"InstanceType": "g6e.2xlarge", "Location": "us-east-1"}
                ]
            },
            # AZ-level check: available in 3 AZs
            {
                "InstanceTypeOfferings": [
                    {"InstanceType": "g6e.2xlarge", "Location": "us-east-1a"},
                    {"InstanceType": "g6e.2xlarge", "Location": "us-east-1b"},
                    {"InstanceType": "g6e.2xlarge", "Location": "us-east-1c"},
                ]
            },
        ]

        result = check_instance_availability("g6e.2xlarge", "us-east-1")

        assert result["instance_type"] == "g6e.2xlarge"
        assert result["region"] == "us-east-1"
        assert result["available"] is True
        assert result["azs"] == ["us-east-1a", "us-east-1b", "us-east-1c"]
        assert result["az_count"] == 3
        assert "source" in result
        assert "interpretation" in result

    def test_ml_prefix_stripped(self, mock_ec2_client):
        """The ml. prefix is stripped before querying EC2."""
        mock_ec2_client.describe_instance_type_offerings.side_effect = [
            {
                "InstanceTypeOfferings": [
                    {"InstanceType": "g6e.2xlarge", "Location": "us-west-2"}
                ]
            },
            {
                "InstanceTypeOfferings": [
                    {"InstanceType": "g6e.2xlarge", "Location": "us-west-2a"},
                ]
            },
        ]

        result = check_instance_availability("ml.g6e.2xlarge", "us-west-2")

        assert result["instance_type"] == "g6e.2xlarge"
        assert result["available"] is True


class TestNotFoundPath:
    """Test instance type not offered in region."""

    def test_instance_not_offered_in_region(self, mock_ec2_client):
        """Empty offerings returns available=False with empty AZ list."""
        mock_ec2_client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": []
        }

        result = check_instance_availability("p5.48xlarge", "sa-east-1")

        assert result["instance_type"] == "p5.48xlarge"
        assert result["region"] == "sa-east-1"
        assert result["available"] is False
        assert result["azs"] == []
        assert "NOT offered" in result["interpretation"]
        assert "source" in result


class TestAPIErrorPath:
    """Test boto3 exception returns error dict gracefully."""

    def test_client_error_returns_error_dict(self, mock_ec2_client):
        """ClientError from boto3 is caught and returned as error dict."""
        from botocore.exceptions import ClientError

        mock_ec2_client.describe_instance_type_offerings.side_effect = ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterValue",
                    "Message": "Invalid instance type",
                }
            },
            "DescribeInstanceTypeOfferings",
        )

        result = check_instance_availability("invalid.type", "us-east-1")

        assert result["instance_type"] == "invalid.type"
        assert result["region"] == "us-east-1"
        assert result["available"] is None
        assert result["azs"] == []
        assert "API error" in result["interpretation"]
        assert "ClientError" in result["interpretation"]
        assert "source" in result

    def test_generic_exception_returns_error_dict(self, mock_ec2_client):
        """Any generic exception is caught and returned as error dict."""
        mock_ec2_client.describe_instance_type_offerings.side_effect = RuntimeError(
            "Connection timeout"
        )

        result = check_instance_availability("g6e.2xlarge", "us-east-1")

        assert result["instance_type"] == "g6e.2xlarge"
        assert result["region"] == "us-east-1"
        assert result["available"] is None
        assert result["azs"] == []
        assert "API error" in result["interpretation"]
        assert "RuntimeError" in result["interpretation"]
        assert "Connection timeout" in result["interpretation"]
        assert "source" in result
