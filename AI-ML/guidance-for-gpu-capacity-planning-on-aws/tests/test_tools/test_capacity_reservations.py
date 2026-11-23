# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the capacity_reservations (ODCR lookup) tool module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.tools.capacity_reservations import (
    HONORED_STATES,
    TOOL_SPEC,
    check_existing_odcrs,
)


@pytest.fixture(autouse=True)
def clear_ec2_client_cache():
    """No-op fixture preserved for test discovery (boto3 client caching was
    removed when tools moved to aws_session.get_client())."""
    yield


def _odcr(
    *,
    reservation_id: str = "cr-0123456789abcdef0",
    state: str = "active",
    az: str = "us-east-1a",
    total: int = 10,
    available: int = 10,
    end_date_type: str = "unlimited",
    end_date: datetime | None = None,
    tenancy: str = "default",
    instance_platform: str = "Linux/UNIX",
) -> dict:
    """Build a CapacityReservation dict shaped like the EC2 API response."""
    return {
        "CapacityReservationId": reservation_id,
        "State": state,
        "AvailabilityZone": az,
        "TotalInstanceCount": total,
        "AvailableInstanceCount": available,
        "EndDateType": end_date_type,
        "EndDate": end_date,
        "Tenancy": tenancy,
        "InstancePlatform": instance_platform,
    }


class TestNoReservations:
    """When the customer has no ODCRs, the tool reports 'none' cleanly."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_empty_reservations_returns_none_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": []
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["reservation_count"] == 0
        assert result["total_capacity"] == 0
        assert result["available_capacity"] == 0
        assert result["reservations"] == []
        assert result["capacity_signal"] == "none"
        assert "No active On-Demand Capacity Reservations" in result["interpretation"]
        assert result["source"] == "ec2.DescribeCapacityReservations"
        assert result["instance_type"] == "g5.2xlarge"
        assert result["region"] == "us-east-1"


class TestCoveredReservations:
    """When all reserved capacity is free, capacity_signal = 'covered'."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_single_active_odcr_fully_available(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [
                _odcr(total=20, available=20),
            ]
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["reservation_count"] == 1
        assert result["total_capacity"] == 20
        assert result["available_capacity"] == 20
        assert result["capacity_signal"] == "covered"
        assert "guaranteed capacity" in result["interpretation"]
        assert len(result["reservations"]) == 1
        first = result["reservations"][0]
        assert first["reservation_id"] == "cr-0123456789abcdef0"
        assert first["state"] == "active"
        assert first["instance_count"] == 20
        assert first["available_count"] == 20

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_multiple_odcrs_aggregate_capacity(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [
                _odcr(reservation_id="cr-aaa", total=10, available=10),
                _odcr(reservation_id="cr-bbb", total=15, available=15, az="us-east-1b"),
            ]
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["reservation_count"] == 2
        assert result["total_capacity"] == 25
        assert result["available_capacity"] == 25
        assert result["capacity_signal"] == "covered"


class TestPartialReservations:
    """When some reserved capacity is in use, capacity_signal = 'partial'."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_partially_consumed_odcr(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [
                _odcr(total=10, available=3),
            ]
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["total_capacity"] == 10
        assert result["available_capacity"] == 3
        assert result["capacity_signal"] == "partial"
        assert "Some reserved capacity is in use" in result["interpretation"]


class TestFullyConsumed:
    """When 0 capacity is free, capacity_signal = 'fully_consumed'."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_zero_available_capacity(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [
                _odcr(total=15, available=0),
            ]
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["capacity_signal"] == "fully_consumed"
        assert "currently consumed" in result["interpretation"]


class TestFiltersAndNormalization:
    """Test ml. prefix stripping, AZ filter pass-through, and state filter."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_strips_ml_prefix_before_filter(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [_odcr()]
        }

        result = check_existing_odcrs("ml.g5.2xlarge", "us-east-1")

        # Result reports the bare instance type
        assert result["instance_type"] == "g5.2xlarge"

        # The boto3 call uses the bare type
        call_kwargs = mock_client.describe_capacity_reservations.call_args[1]
        instance_filter = next(
            f for f in call_kwargs["Filters"] if f["Name"] == "instance-type"
        )
        assert instance_filter["Values"] == ["g5.2xlarge"]

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_az_filter_is_passed_through(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": []
        }

        check_existing_odcrs("g5.2xlarge", "us-east-1", availability_zone="us-east-1a")

        call_kwargs = mock_client.describe_capacity_reservations.call_args[1]
        az_filter = next(
            (f for f in call_kwargs["Filters"] if f["Name"] == "availability-zone"),
            None,
        )
        assert az_filter is not None
        assert az_filter["Values"] == ["us-east-1a"]

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_state_filter_includes_only_honored_states(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": []
        }

        check_existing_odcrs("g5.2xlarge", "us-east-1")

        call_kwargs = mock_client.describe_capacity_reservations.call_args[1]
        state_filter = next(f for f in call_kwargs["Filters"] if f["Name"] == "state")
        # Should match exactly the honored set (order-insensitive)
        assert set(state_filter["Values"]) == HONORED_STATES


class TestPagination:
    """Multi-page responses are aggregated."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_aggregates_multiple_pages(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # First page returns NextToken, second page does not
        mock_client.describe_capacity_reservations.side_effect = [
            {
                "CapacityReservations": [
                    _odcr(reservation_id="cr-1", total=5, available=5)
                ],
                "NextToken": "page2",
            },
            {
                "CapacityReservations": [
                    _odcr(reservation_id="cr-2", total=7, available=2)
                ],
            },
        ]

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["reservation_count"] == 2
        assert result["total_capacity"] == 12
        assert result["available_capacity"] == 7
        assert result["capacity_signal"] == "partial"
        # Second call should have included the NextToken
        second_call_kwargs = mock_client.describe_capacity_reservations.call_args_list[
            1
        ][1]
        assert second_call_kwargs.get("NextToken") == "page2"


class TestEndDateSerialization:
    """EndDate is converted to an ISO 8601 string for JSON safety."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_end_date_serialized_to_iso(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        end = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [
                _odcr(end_date_type="limited", end_date=end),
            ]
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        first = result["reservations"][0]
        assert first["end_date_type"] == "limited"
        assert first["end_date"] == "2026-12-31T23:59:59+00:00"

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_missing_end_date_returns_none(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [_odcr(end_date=None)]
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["reservations"][0]["end_date"] is None


class TestApiError:
    """boto3 exceptions are caught and returned as structured errors."""

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_unauthorized_error_returns_unknown_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.side_effect = Exception(
            "UnauthorizedOperation"
        )

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["reservation_count"] is None
        assert result["capacity_signal"] == "unknown"
        assert "Unable to query ODCRs" in result["interpretation"]
        assert "ec2:DescribeCapacityReservations" in result["interpretation"]
        assert result["source"] == "ec2.DescribeCapacityReservations"

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_client_error_returns_unknown_signal(self, mock_factory):
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "DescribeCapacityReservations",
        )

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["capacity_signal"] == "unknown"
        assert "ClientError" in result["interpretation"]


class TestToolSpec:
    """Test the TOOL_SPEC registration metadata."""

    def test_tool_spec_has_required_fields(self):
        assert TOOL_SPEC["name"] == "check_existing_odcrs"
        assert TOOL_SPEC["handler"] is check_existing_odcrs
        assert "description" in TOOL_SPEC
        schema = TOOL_SPEC["input_schema"]
        assert "instance_type" in schema["required"]
        assert "region" in schema["required"]
        # availability_zone is optional
        assert "availability_zone" not in schema["required"]
        assert "availability_zone" in schema["properties"]
