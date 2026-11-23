# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the capacity_blocks tool module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


from src.tools.capacity_blocks import (
    TOOL_SPEC,
    _normalize_duration,
    get_capacity_blocks_offerings,
)


class TestNormalizeDuration:
    """The API only accepts 24 hours or multiples of 168 up to 4368."""

    def test_below_minimum_clamps_to_24(self):
        assert _normalize_duration(1) == 24
        assert _normalize_duration(0) == 24
        assert _normalize_duration(-5) == 24
        assert _normalize_duration(24) == 24

    def test_above_minimum_rounds_to_nearest_week(self):
        # 100 hours is closer to 1 week (168) than to 2 weeks (336).
        assert _normalize_duration(100) == 168
        # 300 is closer to 2 weeks (336) than to 1 week (168).
        assert _normalize_duration(300) == 336

    def test_exact_week_passes_through(self):
        assert _normalize_duration(168) == 168
        assert _normalize_duration(336) == 336
        assert _normalize_duration(4368) == 4368

    def test_above_maximum_clamps_to_26_weeks(self):
        # 10,000 hours is way past 26 weeks; clamp to 4368.
        assert _normalize_duration(10_000) == 4368
        assert _normalize_duration(100_000) == 4368


class TestCapacityBlocksHappyPath:
    """Successful responses with and without offerings."""

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_offerings_available_returns_available_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [
                {
                    "CapacityBlockOfferingId": "cbo-abc123",
                    "InstanceType": "p5.48xlarge",
                    "AvailabilityZone": "us-east-1a",
                    "InstanceCount": 8,
                    "StartDate": start,
                    "EndDate": end,
                    "CapacityBlockDurationHours": 168,
                    "UpfrontFee": "12600.00",
                    "CurrencyCode": "USD",
                    "Tenancy": "default",
                },
            ],
        }

        result = get_capacity_blocks_offerings(
            "p5.48xlarge", "us-east-1", target_count=8, capacity_duration_hours=168
        )

        assert result["capacity_signal"] == "available"
        assert result["offerings_count"] == 1
        assert result["offerings"][0]["capacity_block_offering_id"] == "cbo-abc123"
        assert result["offerings"][0]["start_date"] == start.isoformat()
        assert result["offerings"][0]["end_date"] == end.isoformat()
        assert result["cheapest_offering"]["upfront_fee"] == "12600.00"
        assert result["source"] == "ec2.DescribeCapacityBlockOfferings"
        assert "match this request" in result["interpretation"]

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_no_offerings_returns_unavailable_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [],
        }

        result = get_capacity_blocks_offerings("g5.2xlarge", "us-east-1")

        assert result["capacity_signal"] == "unavailable"
        assert result["offerings_count"] == 0
        assert result["offerings"] == []
        assert result["cheapest_offering"] is None
        assert "Capacity Blocks programme" in result["interpretation"]

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_missing_key_treated_as_empty(self, mock_factory):
        """API may return no key when offerings is empty — guard with ``or []``."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {}

        result = get_capacity_blocks_offerings("p5.48xlarge", "us-east-1")

        assert result["capacity_signal"] == "unavailable"
        assert result["offerings_count"] == 0

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_ml_prefix_is_stripped(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [],
        }

        result = get_capacity_blocks_offerings("ml.p5.48xlarge", "us-east-1")

        assert result["instance_type"] == "p5.48xlarge"
        call_kwargs = mock_client.describe_capacity_block_offerings.call_args[1]
        assert call_kwargs["InstanceType"] == "p5.48xlarge"

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_target_count_clamped_to_minimum_1(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [],
        }

        get_capacity_blocks_offerings("p5.48xlarge", "us-east-1", target_count=0)

        call_kwargs = mock_client.describe_capacity_block_offerings.call_args[1]
        assert call_kwargs["InstanceCount"] == 1

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_duration_normalized_before_call(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [],
        }

        # 100 hours is not a valid API input; must snap to 168 (1 week).
        result = get_capacity_blocks_offerings(
            "p5.48xlarge", "us-east-1", capacity_duration_hours=100
        )

        assert result["capacity_duration_hours"] == 168
        call_kwargs = mock_client.describe_capacity_block_offerings.call_args[1]
        assert call_kwargs["CapacityDurationHours"] == 168

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_cheapest_offering_picks_lowest_fee(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [
                {
                    "CapacityBlockOfferingId": "cbo-expensive",
                    "InstanceType": "p5.48xlarge",
                    "UpfrontFee": "50000.00",
                },
                {
                    "CapacityBlockOfferingId": "cbo-cheap",
                    "InstanceType": "p5.48xlarge",
                    "UpfrontFee": "12600.00",
                },
                {
                    "CapacityBlockOfferingId": "cbo-mid",
                    "InstanceType": "p5.48xlarge",
                    "UpfrontFee": "25000.00",
                },
            ],
        }

        result = get_capacity_blocks_offerings("p5.48xlarge", "us-east-1")

        assert result["cheapest_offering"]["capacity_block_offering_id"] == "cbo-cheap"

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_cheapest_offering_is_none_when_no_upfront_fees(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.return_value = {
            "CapacityBlockOfferings": [
                {
                    "CapacityBlockOfferingId": "cbo-no-price",
                    "InstanceType": "p5.48xlarge",
                },
            ],
        }

        result = get_capacity_blocks_offerings("p5.48xlarge", "us-east-1")

        # Offering present but no UpfrontFee — can't pick a cheapest.
        assert result["cheapest_offering"] is None
        # Still categorized as available since the offering itself exists.
        assert result["capacity_signal"] == "available"


class TestCapacityBlocksApiError:
    """Exceptions from boto3 are caught and returned as structured errors."""

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_access_denied_returns_unknown_signal(self, mock_factory):
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "not authorized"}},
            "DescribeCapacityBlockOfferings",
        )

        result = get_capacity_blocks_offerings("p5.48xlarge", "us-east-1")

        assert result["capacity_signal"] == "unknown"
        assert result["offerings_count"] == 0
        assert result["offerings"] == []
        assert "ec2:DescribeCapacityBlockOfferings" in result["interpretation"]
        assert result["source"] == "ec2.DescribeCapacityBlockOfferings"

    @patch("src.tools.capacity_blocks._ec2_client")
    def test_arbitrary_exception_returns_unknown_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_block_offerings.side_effect = RuntimeError(
            "something unexpected"
        )

        result = get_capacity_blocks_offerings("p5.48xlarge", "us-east-1")

        assert result["capacity_signal"] == "unknown"
        assert "RuntimeError" in result["interpretation"]
        assert "something unexpected" in result["interpretation"]


class TestToolSpec:
    """TOOL_SPEC is the agent registry contract — guard against drift."""

    def test_tool_spec_has_required_fields(self):
        assert TOOL_SPEC["name"] == "get_capacity_blocks_offerings"
        assert TOOL_SPEC["handler"] is get_capacity_blocks_offerings
        assert "description" in TOOL_SPEC
        schema = TOOL_SPEC["input_schema"]
        assert "instance_type" in schema["required"]
        assert "region" in schema["required"]

    def test_tool_spec_schema_lists_all_four_params(self):
        props = TOOL_SPEC["input_schema"]["properties"]
        assert set(props) == {
            "instance_type",
            "region",
            "target_count",
            "capacity_duration_hours",
        }
