# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the aws_session module (cross-account session resolution)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import boto3
import pytest

from src.aws_session import (
    _BOTOCORE_RETRY_CONFIG,
    assume_role,
    current_session,
    get_client,
    use_session,
)


class TestDefaultBehavior:
    """Without a bound session, get_client() falls back to default credentials."""

    def test_current_session_is_none_by_default(self):
        assert current_session() is None

    @patch("src.aws_session.boto3.client")
    def test_get_client_uses_boto3_default(self, mock_boto3_client):
        mock_client = MagicMock()
        mock_boto3_client.return_value = mock_client

        client = get_client("ec2", "us-east-1")

        assert client is mock_client
        mock_boto3_client.assert_called_once_with(
            "ec2", region_name="us-east-1", config=_BOTOCORE_RETRY_CONFIG
        )

    @patch("src.aws_session.boto3.client")
    def test_get_client_without_region(self, mock_boto3_client):
        get_client("sts")

        # Region not passed → boto3.client called without region_name but
        # still with the retry config attached.
        mock_boto3_client.assert_called_once_with("sts", config=_BOTOCORE_RETRY_CONFIG)


class TestUseSession:
    """use_session binds a session for the duration of the block."""

    def test_session_bound_inside_block(self):
        fake_session = MagicMock(spec=boto3.Session)
        assert current_session() is None

        with use_session(fake_session):
            assert current_session() is fake_session

        assert current_session() is None

    def test_session_is_none_means_no_binding(self):
        with use_session(None):
            assert current_session() is None

    def test_get_client_uses_bound_session(self):
        fake_session = MagicMock(spec=boto3.Session)
        fake_client = MagicMock()
        fake_session.client.return_value = fake_client

        with use_session(fake_session):
            client = get_client("ec2", "us-west-2")

        assert client is fake_client
        fake_session.client.assert_called_once_with(
            "ec2", region_name="us-west-2", config=_BOTOCORE_RETRY_CONFIG
        )

    def test_get_client_uses_bound_session_without_region(self):
        fake_session = MagicMock(spec=boto3.Session)
        fake_session.client.return_value = MagicMock()

        with use_session(fake_session):
            get_client("pricing")

        # No region_name kwarg when not provided, but retry config is still applied.
        fake_session.client.assert_called_once_with(
            "pricing", config=_BOTOCORE_RETRY_CONFIG
        )

    def test_nested_use_session_restores_outer(self):
        outer = MagicMock(spec=boto3.Session)
        inner = MagicMock(spec=boto3.Session)

        with use_session(outer):
            assert current_session() is outer
            with use_session(inner):
                assert current_session() is inner
            assert current_session() is outer
        assert current_session() is None

    def test_exception_in_block_still_resets_session(self):
        fake_session = MagicMock(spec=boto3.Session)
        with pytest.raises(RuntimeError):
            with use_session(fake_session):
                assert current_session() is fake_session
                raise RuntimeError("boom")
        assert current_session() is None

    def test_concurrent_invocations_do_not_leak(self):
        """A new ContextVar binding via _current_session.set() is not visible
        outside the scope it was set in. Reset restores the prior state."""
        from src.aws_session import _current_session, current_session

        outer = MagicMock(spec=boto3.Session)
        with use_session(outer):
            assert current_session() is outer

            # Simulate a second invocation's worker rebinding to None inside
            # a different scope. Reset must restore the outer binding.
            token = _current_session.set(None)
            try:
                assert current_session() is None
            finally:
                _current_session.reset(token)
            assert current_session() is outer
        assert current_session() is None


class TestAssumeRole:
    """assume_role wires STS correctly and returns a session with temp creds."""

    @patch("src.aws_session.boto3.Session")
    @patch("src.aws_session.boto3.client")
    def test_assume_role_minimal(self, mock_boto3_client, mock_session_ctor):
        mock_sts = MagicMock()
        mock_boto3_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secretvalue",
                "SessionToken": "tokenvalue",
            }
        }
        mock_session_ctor.return_value = "session-instance"

        result = assume_role(
            "arn:aws:iam::111122223333:role/CustomerAgentRole",
            "planner-session",
        )

        # STS was called with the right arguments
        mock_boto3_client.assert_called_once_with("sts")
        mock_sts.assume_role.assert_called_once()
        call_kwargs = mock_sts.assume_role.call_args[1]
        assert (
            call_kwargs["RoleArn"] == "arn:aws:iam::111122223333:role/CustomerAgentRole"
        )
        assert call_kwargs["RoleSessionName"] == "planner-session"
        assert call_kwargs["DurationSeconds"] == 900
        assert "ExternalId" not in call_kwargs

        # boto3.Session was constructed with the temp creds
        mock_session_ctor.assert_called_once_with(
            aws_access_key_id="AKIAEXAMPLE",
            aws_secret_access_key="secretvalue",
            aws_session_token="tokenvalue",
        )
        assert result == "session-instance"

    @patch("src.aws_session.boto3.Session")
    @patch("src.aws_session.boto3.client")
    def test_assume_role_passes_external_id(self, mock_boto3_client, _mock_session):
        mock_sts = MagicMock()
        mock_boto3_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIA",
                "SecretAccessKey": "s",
                "SessionToken": "t",
            }
        }

        assume_role(
            "arn:aws:iam::111122223333:role/CustomerAgentRole",
            "planner-session",
            external_id="shared-secret-abc",
        )

        call_kwargs = mock_sts.assume_role.call_args[1]
        assert call_kwargs["ExternalId"] == "shared-secret-abc"

    @patch("src.aws_session.boto3.Session")
    @patch("src.aws_session.boto3.client")
    def test_assume_role_clamps_duration_to_min(self, mock_boto3_client, _mock_session):
        mock_sts = MagicMock()
        mock_boto3_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "a",
                "SecretAccessKey": "b",
                "SessionToken": "c",
            }
        }

        assume_role("arn:aws:iam::111122223333:role/X", "n", duration_seconds=60)

        call_kwargs = mock_sts.assume_role.call_args[1]
        # Minimum allowed by STS is 900
        assert call_kwargs["DurationSeconds"] == 900

    @patch("src.aws_session.boto3.Session")
    @patch("src.aws_session.boto3.client")
    def test_assume_role_clamps_duration_to_max(self, mock_boto3_client, _mock_session):
        mock_sts = MagicMock()
        mock_boto3_client.return_value = mock_sts
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "a",
                "SecretAccessKey": "b",
                "SessionToken": "c",
            }
        }

        assume_role("arn:aws:iam::111122223333:role/X", "n", duration_seconds=100000)

        call_kwargs = mock_sts.assume_role.call_args[1]
        # Maximum allowed by STS is 43200
        assert call_kwargs["DurationSeconds"] == 43200

    @patch("src.aws_session.boto3.client")
    def test_assume_role_propagates_client_error(self, mock_boto3_client):
        from botocore.exceptions import ClientError

        mock_sts = MagicMock()
        mock_boto3_client.return_value = mock_sts
        mock_sts.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no thanks"}},
            "AssumeRole",
        )

        with pytest.raises(ClientError):
            assume_role("arn:aws:iam::111122223333:role/X", "n")


class TestBotocoreRetryConfig:
    """regression guard: every boto3 client MUST carry a retry Config.

    Previously the outer ``with_retry`` wrapper around tool calls was dead code
    because every tool handler catches broad exceptions and returns a structured
    error dict — the retry logic never saw the original exception. Retries now
    live at the boto3 client layer where they actually fire.
    """

    def test_retry_config_has_adaptive_mode_and_bounded_attempts(self):
        cfg = _BOTOCORE_RETRY_CONFIG
        assert cfg.retries is not None
        assert cfg.retries["mode"] == "adaptive"
        # Retries are bounded so a single tool call can't consume the entire
        # Lambda budget backing off.
        assert cfg.retries["max_attempts"] >= 2
        assert cfg.retries["max_attempts"] <= 6

    def test_retry_config_bounds_read_timeout(self):
        cfg = _BOTOCORE_RETRY_CONFIG
        # Guard against unbounded reads on customer-side APIs
        # (CloudTrail LookupEvents in particular).
        assert cfg.read_timeout is not None
        assert cfg.read_timeout <= 60

    @patch("src.aws_session.boto3.client")
    def test_default_path_attaches_retry_config(self, mock_boto3_client):
        get_client("ec2", "us-east-1")
        kwargs = mock_boto3_client.call_args[1]
        assert "config" in kwargs
        assert kwargs["config"] is _BOTOCORE_RETRY_CONFIG

    def test_bound_session_path_attaches_retry_config(self):
        fake_session = MagicMock(spec=boto3.Session)
        fake_session.client.return_value = MagicMock()
        with use_session(fake_session):
            get_client("ec2", "us-east-1")
        kwargs = fake_session.client.call_args[1]
        assert "config" in kwargs
        assert kwargs["config"] is _BOTOCORE_RETRY_CONFIG


class TestCachingDeliberatelyAbsent:
    """get_client must NOT cache across invocations — that would cross account boundaries."""

    @patch("src.aws_session.boto3.client")
    def test_default_returns_a_fresh_client_each_call(self, mock_boto3_client):
        mock_boto3_client.side_effect = [MagicMock(name="c1"), MagicMock(name="c2")]

        c1 = get_client("ec2", "us-east-1")
        c2 = get_client("ec2", "us-east-1")

        # Two separate calls → two factory invocations, never cached
        assert mock_boto3_client.call_count == 2
        assert c1 is not c2

    def test_bound_session_calls_session_client_each_time(self):
        fake_session = MagicMock(spec=boto3.Session)
        fake_session.client.side_effect = [MagicMock(name="a"), MagicMock(name="b")]

        with use_session(fake_session):
            get_client("ec2", "us-east-1")
            get_client("ec2", "us-east-1")

        assert fake_session.client.call_count == 2
