# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guards for the CloudTrail rate limiter.

The rate limiter in ``src/aws_session.py`` sits between the ICE-history
tool and the raw boto3 CloudTrail client. It refuses calls that would
exceed a conservative local budget so a single warm Lambda container
can't drain the shared ~2 req/s account-wide budget for other tenants /
other queries.

The critical downstream contract: when the rate limiter refuses a call,
the ICE-history tool must return ``capacity_signal="rate_limited"``, and
the deterministic scorer must NOT return DEPLOYABLE off that response —
that is what stops the false-DEPLOYABLE regression the review flagged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.aws_session import (
    CloudTrailRateLimitedError,
    _RateLimitedCloudTrailClient,
    _TokenBucket,
    get_client,
)
from src.confidence import compute_confidence
from src.tools.ice_history import get_ice_history


class TestTokenBucket:
    def test_starts_full(self):
        bucket = _TokenBucket(rate_per_second=1.0, capacity=5.0)
        for _ in range(5):
            assert bucket.try_acquire() is True

    def test_refuses_when_empty(self):
        bucket = _TokenBucket(rate_per_second=1.0, capacity=2.0)
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False

    def test_refills_over_time(self):
        import time as _time

        bucket = _TokenBucket(rate_per_second=100.0, capacity=1.0)
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False
        _time.sleep(0.05)  # 100 tokens/s × 0.05 = 5 tokens available
        assert bucket.try_acquire() is True

    def test_rejects_invalid_construction(self):
        with pytest.raises(ValueError):
            _TokenBucket(rate_per_second=0, capacity=1)
        with pytest.raises(ValueError):
            _TokenBucket(rate_per_second=1, capacity=0)


class TestRateLimitedCloudTrailClient:
    def test_lookup_events_gated_by_bucket(self):
        inner = MagicMock()
        inner.lookup_events.return_value = {"Events": []}
        wrapper = _RateLimitedCloudTrailClient(inner)

        # Point the wrapper at a tiny bucket so we hit the wall immediately.
        with patch(
            "src.aws_session._CLOUDTRAIL_BUCKET",
            _TokenBucket(rate_per_second=1.0, capacity=1.0),
        ):
            assert wrapper.lookup_events()["Events"] == []
            with pytest.raises(CloudTrailRateLimitedError):
                wrapper.lookup_events()

    def test_other_methods_proxy_through(self):
        inner = MagicMock()
        inner.get_paginator.return_value = "paginator-sentinel"
        wrapper = _RateLimitedCloudTrailClient(inner)
        # get_paginator must not be gated by the bucket — only lookup_events is.
        assert wrapper.get_paginator("lookup_events") == "paginator-sentinel"

    def test_get_client_returns_wrapper_for_cloudtrail(self):
        with patch("src.aws_session.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            client = get_client("cloudtrail", "us-east-1")
        assert isinstance(client, _RateLimitedCloudTrailClient)

    def test_get_client_does_not_wrap_other_services(self):
        with patch("src.aws_session.boto3.client") as mock_boto:
            raw = MagicMock()
            mock_boto.return_value = raw
            ec2 = get_client("ec2", "us-east-1")
        assert ec2 is raw


class TestIceHistoryHandlesRateLimit:
    """When the rate limiter fires, the ICE tool surfaces
    ``capacity_signal="rate_limited"`` and the scorer refuses DEPLOYABLE.
    """

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_rate_limit_returns_rate_limited_signal(self, mock_factory):
        raising_client = MagicMock()
        raising_client.lookup_events.side_effect = CloudTrailRateLimitedError(
            "test bucket refused"
        )
        mock_factory.return_value = raising_client

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] is None
        assert result["capacity_signal"] == "rate_limited"
        assert "rate limit" in result["interpretation"].lower()

    def test_scorer_refuses_deployable_on_rate_limited_signal(self):
        """End-to-end contract: rate_limited → scorer caps at DWP."""
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            customer_ice_events=None,
            customer_ice_capacity_signal="rate_limited",
            region="us-east-1",
        )
        assert result["verdict"] == "DEPLOYABLE WITH PREPARATION"
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_signal_unavailable" in kinds
