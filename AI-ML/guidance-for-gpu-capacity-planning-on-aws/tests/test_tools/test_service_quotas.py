# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the service_quotas tool module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.tools.service_quotas import get_sagemaker_quota


@pytest.fixture
def mock_sq_client():
    """Create a mock Service Quotas client with a paginator."""
    client = MagicMock()
    return client


class TestGetSagemakerQuotaFound:
    """Tests for the happy path — quota found with value returned."""

    @patch("src.tools.service_quotas._sq_client")
    def test_quota_found_returns_value(self, mock_client_factory):
        """When a matching quota exists, return found=True with quota details."""
        client = MagicMock()
        mock_client_factory.return_value = client

        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-12345678",
                        "QuotaName": "ml.g5.2xlarge for endpoint usage",
                        "Value": 4.0,
                        "Adjustable": True,
                        "Unit": "None",
                    }
                ]
            }
        ]

        result = get_sagemaker_quota("ml.g5.2xlarge", "us-east-1")

        assert result["found"] is True
        assert result["instance_type"] == "ml.g5.2xlarge"
        assert result["region"] == "us-east-1"
        assert result["current_limit"] == 4.0
        assert result["quota_code"] == "L-12345678"
        assert result["quota_name"] == "ml.g5.2xlarge for endpoint usage"
        assert result["adjustable"] is True
        assert result["source"] == "service-quotas.ListServiceQuotas"

    @patch("src.tools.service_quotas._sq_client")
    def test_ml_prefix_added_when_missing(self, mock_client_factory):
        """When instance_type lacks ml. prefix, it is added automatically."""
        client = MagicMock()
        mock_client_factory.return_value = client

        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-ABCDEF01",
                        "QuotaName": "ml.g7e.2xlarge for endpoint usage",
                        "Value": 2.0,
                        "Adjustable": False,
                        "Unit": "None",
                    }
                ]
            }
        ]

        result = get_sagemaker_quota("g7e.2xlarge", "us-west-2")

        assert result["instance_type"] == "ml.g7e.2xlarge"
        assert result["found"] is True
        assert result["current_limit"] == 2.0

    @patch("src.tools.service_quotas._sq_client")
    def test_canonical_variant_wins_over_sibling(self, mock_client_factory):
        """regression guard: when multiple quotas mention the same
        instance type, the tool matches on the CANONICAL name string for
        the requested endpoint_variant (default 'realtime') and surfaces
        siblings only in ``candidates``. Prior version would non-
        deterministically pick the first-returned quota, so a customer
        requesting the realtime quota could receive the async-inference
        quota's value.
        """
        client = MagicMock()
        mock_client_factory.return_value = client

        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Quotas": [
                    # The async quota is listed FIRST — under the old
                    # substring-match logic this would be returned as
                    # ``current_limit`` for a realtime query. Under the
                    # new logic it lands in ``candidates``.
                    {
                        "QuotaCode": "L-ASYNC",
                        "QuotaName": "ml.p4d.24xlarge for async inference endpoint usage",
                        "Value": 8.0,
                        "Adjustable": True,
                        "Unit": "None",
                    },
                    {
                        "QuotaCode": "L-REALTIME",
                        "QuotaName": "ml.p4d.24xlarge for endpoint usage",
                        "Value": 1.0,
                        "Adjustable": True,
                        "Unit": "None",
                    },
                ]
            }
        ]

        result = get_sagemaker_quota("ml.p4d.24xlarge", "us-east-1")

        assert result["found"] is True
        assert result["endpoint_variant"] == "realtime"
        # Matched the realtime canonical name, NOT the async sibling.
        assert result["quota_code"] == "L-REALTIME"
        assert result["current_limit"] == 1.0
        # The async sibling is surfaced in candidates for observability.
        candidate_codes = {c["quota_code"] for c in result["candidates"]}
        assert "L-ASYNC" in candidate_codes

    @patch("src.tools.service_quotas._sq_client")
    def test_async_variant_returns_async_quota(self, mock_client_factory):
        """Same fixture as above but caller explicitly requests the async
        variant → gets the async quota back, not realtime.
        """
        client = MagicMock()
        mock_client_factory.return_value = client
        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-REALTIME",
                        "QuotaName": "ml.p4d.24xlarge for endpoint usage",
                        "Value": 1.0,
                        "Adjustable": True,
                        "Unit": "None",
                    },
                    {
                        "QuotaCode": "L-ASYNC",
                        "QuotaName": "ml.p4d.24xlarge for async inference endpoint usage",
                        "Value": 8.0,
                        "Adjustable": True,
                        "Unit": "None",
                    },
                ]
            }
        ]

        result = get_sagemaker_quota(
            "ml.p4d.24xlarge", "us-east-1", endpoint_variant="async"
        )

        assert result["found"] is True
        assert result["endpoint_variant"] == "async"
        assert result["quota_code"] == "L-ASYNC"
        assert result["current_limit"] == 8.0

    @patch("src.tools.service_quotas._sq_client")
    def test_serverless_variant_returns_helpful_note(self, mock_client_factory):
        """Serverless inference has no per-instance-type quota — the tool
        returns a helpful message rather than pretending to look one up.
        """
        client = MagicMock()
        mock_client_factory.return_value = client

        result = get_sagemaker_quota(
            "ml.g5.2xlarge", "us-east-1", endpoint_variant="serverless"
        )

        assert result["found"] is False
        assert result["endpoint_variant"] == "serverless"
        assert "Serverless" in result["interpretation"]
        # Must NOT have made an API call — no need to.
        client.get_paginator.assert_not_called()

    @patch("src.tools.service_quotas._sq_client")
    def test_invalid_variant_rejected(self, mock_client_factory):
        client = MagicMock()
        mock_client_factory.return_value = client

        result = get_sagemaker_quota(
            "ml.g5.2xlarge", "us-east-1", endpoint_variant="not-a-real-variant"
        )
        assert result["found"] is False
        assert "Invalid endpoint_variant" in result["interpretation"]
        client.get_paginator.assert_not_called()


class TestGetSagemakerQuotaNotFound:
    """Tests for the not-found path — no matching quota in the response."""

    @patch("src.tools.service_quotas._sq_client")
    def test_no_matching_quota_returns_not_found(self, mock_client_factory):
        """When no quotas match the instance type, return found=False."""
        client = MagicMock()
        mock_client_factory.return_value = client

        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-99999999",
                        "QuotaName": "ml.m5.xlarge for endpoint usage",
                        "Value": 10.0,
                        "Adjustable": True,
                        "Unit": "None",
                    }
                ]
            }
        ]

        result = get_sagemaker_quota("ml.g5.48xlarge", "eu-west-1")

        assert result["found"] is False
        assert result["instance_type"] == "ml.g5.48xlarge"
        assert result["region"] == "eu-west-1"
        # New API surfaces siblings under ``candidates`` (empty here).
        assert result["candidates"] == []
        assert "No Service Quotas entry" in result["interpretation"]
        assert result["source"] == "service-quotas.ListServiceQuotas"

    @patch("src.tools.service_quotas._sq_client")
    def test_empty_quotas_list_returns_not_found(self, mock_client_factory):
        """When the API returns no quotas at all, return found=False."""
        client = MagicMock()
        mock_client_factory.return_value = client

        paginator = MagicMock()
        client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{"Quotas": []}]

        result = get_sagemaker_quota("ml.trn1.32xlarge", "us-east-1")

        assert result["found"] is False
        assert result["candidates"] == []


class TestGetSagemakerQuotaApiError:
    """Tests for the API error path — boto3 raises an exception."""

    @patch("src.tools.service_quotas._sq_client")
    def test_client_error_returns_error_dict(self, mock_client_factory):
        """When boto3 raises a ClientError, return found=False with error info."""
        from botocore.exceptions import ClientError

        client = MagicMock()
        mock_client_factory.return_value = client

        client.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
            "ListServiceQuotas",
        )

        result = get_sagemaker_quota("ml.g5.2xlarge", "us-east-1")

        assert result["found"] is False
        assert result["instance_type"] == "ml.g5.2xlarge"
        assert result["region"] == "us-east-1"
        assert "API error" in result["interpretation"]
        assert "ClientError" in result["interpretation"]
        assert result["source"] == "service-quotas.ListServiceQuotas"

    @patch("src.tools.service_quotas._sq_client")
    def test_generic_exception_returns_error_dict(self, mock_client_factory):
        """When an unexpected exception occurs, return found=False gracefully."""
        client = MagicMock()
        mock_client_factory.return_value = client

        client.get_paginator.side_effect = RuntimeError("Connection timeout")

        result = get_sagemaker_quota("ml.p5.48xlarge", "ap-southeast-1")

        assert result["found"] is False
        assert "API error" in result["interpretation"]
        assert "RuntimeError" in result["interpretation"]
        assert "Connection timeout" in result["interpretation"]
        assert result["source"] == "service-quotas.ListServiceQuotas"
