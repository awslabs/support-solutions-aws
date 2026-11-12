# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the SageMaker pricing tool module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.tools.pricing import get_sagemaker_pricing


@pytest.fixture
def mock_pricing_client():
    """Create a mock boto3 Pricing client."""
    client = MagicMock()
    return client


def _make_price_list_entry(hourly_rate: float = 1.234) -> str:
    """Build a realistic Pricing API PriceList JSON entry."""
    entry = {
        "product": {
            "productFamily": "Machine Learning",
            "attributes": {
                "instanceName": "ml.g5.xlarge",
                "location": "US East (N. Virginia)",
            },
        },
        "terms": {
            "OnDemand": {
                "SKU123.JRTCKXETXF": {
                    "priceDimensions": {
                        "SKU123.JRTCKXETXF.6YS6EN2CT7": {
                            "unit": "Hrs",
                            "pricePerUnit": {"USD": str(hourly_rate)},
                        }
                    }
                }
            }
        },
    }
    return json.dumps(entry)


class TestPricingHappyPath:
    """Test pricing data found — hourly_usd returned."""

    @patch("src.tools.pricing._pricing_client")
    def test_returns_hourly_usd_for_known_instance(self, mock_client_factory):
        """When a matching SKU is found, hourly_usd is populated."""
        mock_client = MagicMock()
        mock_client.get_products.return_value = {
            "PriceList": [_make_price_list_entry(1.515)],
        }
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing("ml.g5.xlarge", "us-east-1")

        assert result["hourly_usd"] == 1.515
        assert result["available"] is True
        assert result["instance_type"] == "ml.g5.xlarge"
        assert result["region"] == "us-east-1"
        assert result["component"] == "Hosting"
        assert result["source"] == "AWS Pricing API"
        assert result["_notes"] is None

    @patch("src.tools.pricing._pricing_client")
    def test_normalizes_instance_type_prefix(self, mock_client_factory):
        """Instance type without ml. prefix gets normalized."""
        mock_client = MagicMock()
        mock_client.get_products.return_value = {
            "PriceList": [_make_price_list_entry(2.0)],
        }
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing("g5.xlarge", "us-west-2")

        assert result["instance_type"] == "ml.g5.xlarge"
        assert result["hourly_usd"] == 2.0
        assert result["available"] is True

    @patch("src.tools.pricing._pricing_client")
    def test_custom_component_passed_to_filters(self, mock_client_factory):
        """Component parameter is included in the API call."""
        mock_client = MagicMock()
        mock_client.get_products.return_value = {
            "PriceList": [_make_price_list_entry(3.5)],
        }
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing(
            "ml.p4d.24xlarge", "eu-west-1", component="Training"
        )

        assert result["component"] == "Training"
        assert result["hourly_usd"] == 3.5
        # Verify the filter was passed correctly
        call_kwargs = mock_client.get_products.call_args[1]
        filters = call_kwargs["Filters"]
        component_filter = next(f for f in filters if f["Field"] == "platoinstancetype")
        assert component_filter["Value"] == "Training"


class TestPricingNotFound:
    """Test not-found path — no matching SKU/product."""

    @patch("src.tools.pricing._pricing_client")
    def test_empty_price_list_returns_not_available(self, mock_client_factory):
        """When no SKU matches, available is False and hourly_usd is None."""
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": []}
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing("ml.g7e.2xlarge", "sa-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert result["instance_type"] == "ml.g7e.2xlarge"
        assert result["region"] == "sa-east-1"
        assert (
            "not yet GA" in result["_notes"].lower()
            or "No SageMaker" in result["_notes"]
        )

    @patch("src.tools.pricing._pricing_client")
    def test_sku_found_but_no_extractable_rate(self, mock_client_factory):
        """When SKU records exist but no hourly rate can be extracted."""
        # Entry with no OnDemand terms
        entry_no_rate = json.dumps(
            {
                "product": {"attributes": {"instanceName": "ml.g5.xlarge"}},
                "terms": {"OnDemand": {}},
            }
        )
        mock_client = MagicMock()
        mock_client.get_products.return_value = {"PriceList": [entry_no_rate]}
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing("ml.g5.xlarge", "us-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert (
            "no extractable hourly rate" in result["_notes"].lower()
            or "Found SKU" in result["_notes"]
        )


class TestPricingUnknownRegion:
    """Test unknown region handling."""

    def test_unknown_region_returns_error_without_api_call(self):
        """Unknown region code returns immediately without calling AWS."""
        result = get_sagemaker_pricing("ml.g5.xlarge", "xx-fake-99")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert result["region"] == "xx-fake-99"
        assert "Unknown region code" in result["_notes"]

    def test_unknown_region_still_normalizes_instance_type(self):
        """Instance type is normalized even for unknown regions."""
        result = get_sagemaker_pricing("p4d.24xlarge", "invalid-region")

        assert result["instance_type"] == "ml.p4d.24xlarge"
        assert result["available"] is False
        assert "Unknown region code" in result["_notes"]


class TestPricingApiError:
    """Test API error path — boto3 raises an exception."""

    @patch("src.tools.pricing._pricing_client")
    def test_client_exception_returns_error_dict(self, mock_client_factory):
        """When get_products raises, handler returns error dict gracefully."""
        mock_client = MagicMock()
        mock_client.get_products.side_effect = Exception("Throttling: Rate exceeded")
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing("ml.g5.xlarge", "us-east-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert "Pricing API error" in result["_notes"]
        assert "Throttling" in result["_notes"]
        assert result["source"] == "AWS Pricing API"

    @patch("src.tools.pricing._pricing_client")
    def test_botocore_client_error_returns_error_dict(self, mock_client_factory):
        """Simulates a botocore ClientError being raised."""
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_client.get_products.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameterException", "Message": "Bad filter"}},
            "GetProducts",
        )
        mock_client_factory.return_value = mock_client

        result = get_sagemaker_pricing("ml.g5.xlarge", "eu-central-1")

        assert result["hourly_usd"] is None
        assert result["available"] is False
        assert "Pricing API error" in result["_notes"]
        assert "ClientError" in result["_notes"]
