# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ec2_pricing tool module — bare EC2 pricing for self-managed and EKS."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.tools.ec2_pricing import TOOL_SPEC, get_ec2_pricing


@pytest.fixture(autouse=True)
def clear_pricing_client_cache():
    """No-op fixture preserved for test discovery (boto3 client caching was
    removed when tools moved to aws_session.get_client())."""
    yield


def _make_ec2_price_entry(hourly_rate: float = 1.234) -> str:
    """Build a realistic Pricing API PriceList JSON entry for AmazonEC2."""
    return json.dumps(
        {
            "product": {
                "productFamily": "Compute Instance",
                "attributes": {
                    "instanceType": "g5.2xlarge",
                    "location": "US East (N. Virginia)",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                },
            },
            "terms": {
                "OnDemand": {
                    "ABCDE.JRTCKXETXF": {
                        "priceDimensions": {
                            "ABCDE.JRTCKXETXF.6YS6EN2CT7": {
                                "unit": "Hrs",
                                "pricePerUnit": {"USD": str(hourly_rate)},
                            }
                        }
                    }
                }
            },
        }
    )


class TestEc2PricingHappyPath:
    """Test successful pricing retrieval."""

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_returns_hourly_usd(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.return_value = {
            "PriceList": [_make_ec2_price_entry(1.212)],
        }

        result = get_ec2_pricing("g5.2xlarge", "us-east-1")

        assert result["hourly_usd"] == 1.212
        assert result["available"] is True
        assert result["instance_type"] == "g5.2xlarge"
        assert result["region"] == "us-east-1"
        assert result["operating_system"] == "Linux"
        assert result["deployment_target"] == "EC2/EKS"
        assert result["source"] == "AWS Pricing API (AmazonEC2)"
        assert "EC2 on-demand" in result["interpretation"]

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_strips_ml_prefix(self, mock_factory):
        """Customer pasting 'ml.g5.xlarge' should be normalized to 'g5.xlarge'."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.return_value = {
            "PriceList": [_make_ec2_price_entry(0.50)],
        }

        result = get_ec2_pricing("ml.g5.xlarge", "us-east-1")

        assert result["instance_type"] == "g5.xlarge"
        # Verify filter passed bare type
        filters = mock_client.get_products.call_args[1]["Filters"]
        instance_filter = next(f for f in filters if f["Field"] == "instanceType")
        assert instance_filter["Value"] == "g5.xlarge"

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_default_os_is_linux(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.return_value = {
            "PriceList": [_make_ec2_price_entry(2.0)],
        }

        result = get_ec2_pricing("p4d.24xlarge", "us-west-2")

        assert result["operating_system"] == "Linux"
        filters = mock_client.get_products.call_args[1]["Filters"]
        os_filter = next(f for f in filters if f["Field"] == "operatingSystem")
        assert os_filter["Value"] == "Linux"

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_custom_os_passed_through(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.return_value = {
            "PriceList": [_make_ec2_price_entry(3.5)],
        }

        result = get_ec2_pricing("g5.xlarge", "us-east-1", operating_system="Windows")

        assert result["operating_system"] == "Windows"
        filters = mock_client.get_products.call_args[1]["Filters"]
        os_filter = next(f for f in filters if f["Field"] == "operatingSystem")
        assert os_filter["Value"] == "Windows"

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_filters_include_required_dimensions(self, mock_factory):
        """The Pricing API filter must include tenancy=Shared, capacitystatus=Used, etc."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.return_value = {
            "PriceList": [_make_ec2_price_entry(1.0)],
        }

        get_ec2_pricing("g5.xlarge", "us-east-1")

        filters = mock_client.get_products.call_args[1]["Filters"]
        fields = {f["Field"]: f["Value"] for f in filters}
        assert fields["tenancy"] == "Shared"
        assert fields["preInstalledSw"] == "NA"
        assert fields["capacitystatus"] == "Used"
        # Service code passed as separate kwarg
        assert mock_client.get_products.call_args[1]["ServiceCode"] == "AmazonEC2"


class TestEc2PricingNotFound:
    """Test not-found / no-result paths."""

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_empty_price_list_returns_unavailable(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.return_value = {"PriceList": []}

        result = get_ec2_pricing("zz99.fake", "us-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert "No EC2 on-demand pricing" in result["_notes"]

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_zero_rate_returns_no_extractable_rate(self, mock_factory):
        """Zero or negative pricePerUnit should be treated as not extractable."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # Build entry with USD = 0
        zero_entry = json.dumps(
            {
                "product": {"attributes": {"instanceType": "g5.xlarge"}},
                "terms": {
                    "OnDemand": {
                        "SKU": {
                            "priceDimensions": {"DIM": {"pricePerUnit": {"USD": "0"}}}
                        }
                    }
                },
            }
        )
        mock_client.get_products.return_value = {"PriceList": [zero_entry]}

        result = get_ec2_pricing("g5.xlarge", "us-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert "no extractable hourly rate" in result["_notes"].lower()

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_entry_without_ondemand_returns_unavailable(self, mock_factory):
        """Pricing entry with no OnDemand terms cannot be used."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        no_od = json.dumps(
            {
                "product": {"attributes": {"instanceType": "g5.xlarge"}},
                "terms": {"Reserved": {"SKU": {}}},
            }
        )
        mock_client.get_products.return_value = {"PriceList": [no_od]}

        result = get_ec2_pricing("g5.xlarge", "us-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False


class TestEc2PricingUnknownRegion:
    """Test unknown region handling — short-circuits without API call."""

    def test_unknown_region_short_circuits(self):
        result = get_ec2_pricing("g5.xlarge", "xx-fake-99")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert result["region"] == "xx-fake-99"
        assert "Unknown region code" in result["_notes"]

    def test_unknown_region_normalizes_instance_type(self):
        result = get_ec2_pricing("ml.p4d.24xlarge", "fake-region")

        assert result["instance_type"] == "p4d.24xlarge"
        assert result["available"] is False


class TestEc2PricingApiError:
    """Test API error handling."""

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_generic_exception_returns_error_dict(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.side_effect = Exception("Throttling")

        result = get_ec2_pricing("g5.xlarge", "us-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert "Pricing API error" in result["_notes"]
        assert "Throttling" in result["_notes"]

    @patch("src.tools.ec2_pricing._pricing_client")
    def test_client_error_returns_error_dict(self, mock_factory):
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.get_products.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Bad creds"}},
            "GetProducts",
        )

        result = get_ec2_pricing("g5.xlarge", "us-east-1")

        assert result["available"] is False
        assert "ClientError" in result["_notes"]


class TestToolSpec:
    """Test the TOOL_SPEC registration metadata."""

    def test_tool_spec_has_required_fields(self):
        assert TOOL_SPEC["name"] == "get_ec2_pricing"
        assert TOOL_SPEC["handler"] is get_ec2_pricing
        schema = TOOL_SPEC["input_schema"]
        assert "instance_type" in schema["required"]
        assert "region" in schema["required"]
        # operating_system is optional (has default)
        assert "operating_system" not in schema.get("required", [])
