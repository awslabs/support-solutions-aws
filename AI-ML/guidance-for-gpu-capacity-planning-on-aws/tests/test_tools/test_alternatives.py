# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the alternatives tool module.

Tests cover:
- Happy path: alternatives returned and ranked by similarity
- Not-found path: no GPU types available in region
- API error path: boto3 raises an exception, handler returns error dict gracefully

Requirements: 1.4
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools.alternatives import find_alternative_instances


@pytest.fixture(autouse=True)
def clear_ec2_client_cache():
    """No-op fixture preserved for test discovery (boto3 client caching was
    removed when tools moved to aws_session.get_client())."""
    yield


def _make_instance_type_response(
    instance_type: str,
    vcpus: int = 8,
    memory_mib: int = 32768,
    gpu_count: int = 1,
    gpu_memory_mib: int = 24576,
    gpu_manufacturer: str = "NVIDIA",
) -> dict:
    """Helper to build a DescribeInstanceTypes response for a single type."""
    return {
        "InstanceTypes": [
            {
                "InstanceType": instance_type,
                "VCpuInfo": {"DefaultVCpus": vcpus},
                "MemoryInfo": {"SizeInMiB": memory_mib},
                "GpuInfo": {
                    "Gpus": [
                        {
                            "Count": gpu_count,
                            "MemoryInfo": {"SizeInMiB": gpu_memory_mib},
                            "Manufacturer": gpu_manufacturer,
                        }
                    ]
                },
            }
        ]
    }


class TestAlternativesHappyPath:
    """Test that alternatives are returned and ranked by similarity."""

    @patch("src.tools.alternatives._ec2_client")
    def test_returns_ranked_alternatives(self, mock_client_factory):
        """Alternatives are returned sorted by similarity score (descending)."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Reference instance specs (g5.2xlarge)
        ref_response = _make_instance_type_response(
            "g5.2xlarge", vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
        )

        # Candidate specs: one very similar, one less similar
        similar_response = _make_instance_type_response(
            "g6.2xlarge", vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
        )
        less_similar_response = _make_instance_type_response(
            "p4d.24xlarge",
            vcpus=96,
            memory_mib=1179648,
            gpu_count=8,
            gpu_memory_mib=40960,
        )

        # describe_instance_types returns different results based on input
        def describe_side_effect(InstanceTypes=None, **kwargs):
            type_name = InstanceTypes[0]
            responses = {
                "g5.2xlarge": ref_response,
                "g6.2xlarge": similar_response,
                "p4d.24xlarge": less_similar_response,
            }
            return responses.get(type_name, {"InstanceTypes": []})

        mock_client.describe_instance_types.side_effect = describe_side_effect

        # Paginator returns GPU types offered in region
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "InstanceTypeOfferings": [
                    {"InstanceType": "g5.2xlarge"},
                    {"InstanceType": "g6.2xlarge"},
                    {"InstanceType": "p4d.24xlarge"},
                ]
            }
        ]
        mock_client.get_paginator.return_value = mock_paginator

        result = find_alternative_instances("g5.2xlarge", "us-west-2", max_results=5)

        assert result["reference"] == "g5.2xlarge"
        assert result["region"] == "us-west-2"
        assert len(result["alternatives"]) == 2  # excludes the reference itself
        # g6.2xlarge should rank higher (identical specs)
        assert result["alternatives"][0]["instance_type"] == "g6.2xlarge"
        assert result["alternatives"][1]["instance_type"] == "p4d.24xlarge"
        # Scores should be descending
        assert (
            result["alternatives"][0]["similarity_score"]
            >= result["alternatives"][1]["similarity_score"]
        )
        assert "source" in result

    @patch("src.tools.alternatives._ec2_client")
    def test_strips_ml_prefix(self, mock_client_factory):
        """Instance types with ml. prefix are handled correctly."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        ref_response = _make_instance_type_response(
            "g5.xlarge", vcpus=4, memory_mib=16384, gpu_count=1, gpu_memory_mib=24576
        )
        mock_client.describe_instance_types.return_value = ref_response

        # No GPU types offered
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"InstanceTypeOfferings": []}]
        mock_client.get_paginator.return_value = mock_paginator

        result = find_alternative_instances("ml.g5.xlarge", "us-east-1")

        # Should strip ml. prefix
        assert result["reference"] == "g5.xlarge"

    @patch("src.tools.alternatives._ec2_client")
    def test_max_results_limits_output(self, mock_client_factory):
        """Only max_results alternatives are returned."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        ref_response = _make_instance_type_response(
            "g5.2xlarge", vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
        )

        # Create multiple candidates with same specs
        def describe_side_effect(InstanceTypes=None, **kwargs):
            type_name = InstanceTypes[0]
            if type_name == "g5.2xlarge":
                return ref_response
            return _make_instance_type_response(
                type_name, vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
            )

        mock_client.describe_instance_types.side_effect = describe_side_effect

        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "InstanceTypeOfferings": [
                    {"InstanceType": "g5.2xlarge"},
                    {"InstanceType": "g6.xlarge"},
                    {"InstanceType": "g6.2xlarge"},
                    {"InstanceType": "g6.4xlarge"},
                ]
            }
        ]
        mock_client.get_paginator.return_value = mock_paginator

        result = find_alternative_instances("g5.2xlarge", "us-west-2", max_results=2)

        assert len(result["alternatives"]) <= 2


class TestAlternativesNotFound:
    """Test when no GPU types are available in the region."""

    @patch("src.tools.alternatives._ec2_client")
    def test_no_gpu_types_in_region(self, mock_client_factory):
        """Returns empty alternatives when no GPU instances are offered in region."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Reference instance specs found
        ref_response = _make_instance_type_response(
            "g5.2xlarge", vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
        )
        mock_client.describe_instance_types.return_value = ref_response

        # No GPU types offered in region
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"InstanceTypeOfferings": []}]
        mock_client.get_paginator.return_value = mock_paginator

        result = find_alternative_instances("g5.2xlarge", "af-south-1")

        assert result["reference"] == "g5.2xlarge"
        assert result["region"] == "af-south-1"
        assert result["alternatives"] == []
        assert "No GPU instances found" in result["interpretation"]
        assert "source" in result

    @patch("src.tools.alternatives._ec2_client")
    def test_reference_instance_not_found(self, mock_client_factory):
        """Returns empty alternatives when reference instance specs cannot be found."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Instance type not found in any region
        mock_client.describe_instance_types.return_value = {"InstanceTypes": []}

        result = find_alternative_instances("g99.nonexistent", "us-west-2")

        assert result["reference"] == "g99.nonexistent"
        assert result["alternatives"] == []
        assert "Cannot find specs" in result["interpretation"]


class TestAlternativesAPIError:
    """Test that API errors are handled gracefully."""

    @patch("src.tools.alternatives._ec2_client")
    def test_boto3_exception_returns_error_dict(self, mock_client_factory):
        """When boto3 raises an exception in the paginator, handler returns error dict."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        from botocore.exceptions import ClientError

        # Reference specs succeed so we get past the first check
        ref_response = _make_instance_type_response(
            "g5.2xlarge", vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
        )
        mock_client.describe_instance_types.return_value = ref_response

        # Paginator raises a ClientError (simulating API failure)
        error_response = {
            "Error": {"Code": "UnauthorizedOperation", "Message": "Access denied"}
        }
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = ClientError(
            error_response, "DescribeInstanceTypeOfferings"
        )
        mock_client.get_paginator.return_value = mock_paginator

        result = find_alternative_instances("g5.2xlarge", "us-west-2")

        assert result["reference"] == "g5.2xlarge"
        assert result["region"] == "us-west-2"
        assert result["alternatives"] == []
        assert "API error" in result["interpretation"]
        assert "ClientError" in result["interpretation"]
        assert "source" in result

    @patch("src.tools.alternatives._ec2_client")
    def test_generic_exception_returns_error_dict(self, mock_client_factory):
        """When an unexpected exception occurs in paginator, handler returns error dict."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Reference specs succeed
        ref_response = _make_instance_type_response(
            "g5.2xlarge", vcpus=8, memory_mib=32768, gpu_count=1, gpu_memory_mib=24576
        )
        mock_client.describe_instance_types.return_value = ref_response

        # Paginator raises a generic RuntimeError
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = RuntimeError("Unexpected failure")
        mock_client.get_paginator.return_value = mock_paginator

        result = find_alternative_instances("g5.2xlarge", "eu-west-1")

        assert result["reference"] == "g5.2xlarge"
        assert result["region"] == "eu-west-1"
        assert result["alternatives"] == []
        assert "API error" in result["interpretation"]
        assert "RuntimeError" in result["interpretation"]
        assert "source" in result

    @patch("src.tools.alternatives._ec2_client")
    def test_describe_instance_types_error_returns_not_found(self, mock_client_factory):
        """When describe_instance_types fails, specs lookup returns None gracefully."""
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Simulate describe_instance_types raising (caught by _get_instance_specs)
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "InvalidInstanceType", "Message": "Not found"}
        }
        mock_client.describe_instance_types.side_effect = ClientError(
            error_response, "DescribeInstanceTypes"
        )

        result = find_alternative_instances("g5.2xlarge", "us-west-2")

        # _get_instance_specs catches the error and returns None → "Cannot find specs"
        assert result["reference"] == "g5.2xlarge"
        assert result["alternatives"] == []
        assert "Cannot find specs" in result["interpretation"]
