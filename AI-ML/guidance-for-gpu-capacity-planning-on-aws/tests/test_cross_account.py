# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the lambda_handler cross-account path (customer_context block).

These tests patch src.lambda_handler.assume_role and src.lambda_handler.use_session
directly because the handler binds those at module load via
`from aws_session import ...`. Patching the module attribute is the reliable way
to override the handler's local references in tests.
"""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

# Mock the bare imports the handler uses at module load. Mirrors the pattern
# in test_lambda_handler.py.
_mock_agent_module = MagicMock()
_mock_tools_module = MagicMock()
_mock_tools_module.ALL_TOOLS = []
_mock_secrets_module = MagicMock()
_mock_secrets_module.SecretsRetrievalError = type(
    "SecretsRetrievalError", (Exception,), {"secret_id": "", "reason": ""}
)
_mock_secrets_module.get_secrets = MagicMock(return_value={})
_mock_metrics_module = MagicMock()
_mock_tool_cache_module = MagicMock()

sys.modules.setdefault("agent", _mock_agent_module)
sys.modules.setdefault("tools", _mock_tools_module)
sys.modules.setdefault("secrets_manager", _mock_secrets_module)
sys.modules.setdefault("metrics", _mock_metrics_module)
sys.modules.setdefault("tool_cache", _mock_tool_cache_module)

_mock_aws_session = MagicMock()
_mock_aws_session.use_session = lambda *_a, **_kw: nullcontext()
sys.modules.setdefault("aws_session", _mock_aws_session)

from src.lambda_handler import handler  # noqa: E402

VALID_ROLE_ARN = "arn:aws:iam::123456789012:role/CapacityAgentReadOnly"


def _build_event(body: dict | str | None) -> dict:
    """API Gateway proxy event with the given body."""
    if body is None:
        encoded = "{}"
    elif isinstance(body, str):
        encoded = body
    else:
        encoded = json.dumps(body)
    return {
        "httpMethod": "POST",
        "path": "/plan",
        "headers": {"Content-Type": "application/json"},
        "body": encoded,
        "isBase64Encoded": False,
        "requestContext": {"requestId": "test-req", "stage": "Prod"},
    }


def _lambda_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id-xc-001"
    ctx.function_name = "CapacityAgentFunction"
    ctx.get_remaining_time_in_millis.return_value = 30000
    return ctx


def _agent_result() -> dict:
    """A minimal JSON-serializable agent result for happy-path tests."""
    return {
        "final_text": "ok",
        "tool_calls": [],
        "iterations": 1,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "token_budget_limit": 80000,
        "tool_outcomes": {"succeeded": [], "failed": []},
        "confidence_assessments": [],
    }


# ---------- validation of customer_context ----------


class TestCustomerContextValidation:
    def test_no_customer_context_falls_back_to_deploy_account(self):
        """A request without customer_context should NOT trigger assume_role."""
        with (
            patch("src.lambda_handler.assume_role") as mock_assume,
            patch("src.lambda_handler._run_agent", return_value=_agent_result()),
        ):
            resp = handler(_build_event({"query": "test"}), _lambda_ctx())

        assert resp["statusCode"] == 200
        mock_assume.assert_not_called()

    def test_customer_context_not_object_returns_400(self):
        resp = handler(
            _build_event({"query": "test", "customer_context": "not an object"}),
            _lambda_ctx(),
        )
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 400
        assert "customer_context" in body["error"].lower()

    def test_role_arn_missing_returns_400(self):
        resp = handler(
            _build_event({"query": "test", "customer_context": {}}),
            _lambda_ctx(),
        )
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 400
        assert "role_arn" in body["error"]

    def test_role_arn_not_string_returns_400(self):
        resp = handler(
            _build_event({"query": "test", "customer_context": {"role_arn": 12345}}),
            _lambda_ctx(),
        )
        assert resp["statusCode"] == 400

    @pytest.mark.parametrize(
        "bad_arn",
        [
            "arn:aws:iam::abc:role/X",
            "arn:aws:iam::123:role/X",
            "arn:aws:iam::123456789012:user/X",
            "arn:aws:s3:::bucket",
            "not-an-arn",
        ],
    )
    def test_role_arn_format_rejected(self, bad_arn):
        resp = handler(
            _build_event({"query": "test", "customer_context": {"role_arn": bad_arn}}),
            _lambda_ctx(),
        )
        assert resp["statusCode"] == 400

    def test_external_id_not_string_returns_400(self):
        resp = handler(
            _build_event(
                {
                    "query": "test",
                    "customer_context": {
                        "role_arn": VALID_ROLE_ARN,
                        "external_id": 999,
                    },
                }
            ),
            _lambda_ctx(),
        )
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 400
        assert "external_id" in body["error"]

    def test_missing_external_id_returns_400_when_required(self):
        """M11 regression guard: REQUIRE_EXTERNAL_ID defaults to true.
        A customer_context that omits external_id must be rejected with
        a 400 — this closes the AWS confused-deputy hole for third-party
        cross-account access.
        """
        # REQUIRE_EXTERNAL_ID is unset by default → helper defaults to True.
        import os

        os.environ.pop("REQUIRE_EXTERNAL_ID", None)
        resp = handler(
            _build_event(
                {
                    "query": "test",
                    "customer_context": {"role_arn": VALID_ROLE_ARN},
                }
            ),
            _lambda_ctx(),
        )
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 400
        assert "external_id" in body["error"].lower()

    def test_missing_external_id_accepted_when_stack_disables_requirement(self):
        """Setting REQUIRE_EXTERNAL_ID=false lets an internal deploy skip
        the shared-secret handshake — used for stacks where the deploy
        account fully controls both sides of the trust boundary.
        """
        import os

        os.environ["REQUIRE_EXTERNAL_ID"] = "false"
        try:
            with (
                patch("src.lambda_handler.assume_role", return_value=MagicMock()),
                patch(
                    "src.lambda_handler.use_session",
                    side_effect=lambda s: nullcontext(),
                ),
                patch("src.lambda_handler._run_agent", return_value=_agent_result()),
            ):
                resp = handler(
                    _build_event(
                        {
                            "query": "test",
                            "customer_context": {"role_arn": VALID_ROLE_ARN},
                        }
                    ),
                    _lambda_ctx(),
                )
            assert resp["statusCode"] == 200
        finally:
            os.environ.pop("REQUIRE_EXTERNAL_ID", None)

    def test_empty_string_external_id_rejected_even_when_present(self):
        """A caller who supplies external_id="" must still be rejected
        under REQUIRE_EXTERNAL_ID=true — an empty string offers zero
        protection against confused-deputy.
        """
        import os

        os.environ.pop("REQUIRE_EXTERNAL_ID", None)
        resp = handler(
            _build_event(
                {
                    "query": "test",
                    "customer_context": {
                        "role_arn": VALID_ROLE_ARN,
                        "external_id": "",
                    },
                }
            ),
            _lambda_ctx(),
        )
        assert resp["statusCode"] == 400

    def test_invalid_session_name_returns_400(self):
        resp = handler(
            _build_event(
                {
                    "query": "test",
                    "customer_context": {
                        "role_arn": VALID_ROLE_ARN,
                        "session_name": "has spaces",
                    },
                }
            ),
            _lambda_ctx(),
        )
        assert resp["statusCode"] == 400


# ---------- happy path ----------


class TestCustomerContextHappyPath:
    def test_role_assumption_then_agent_run_with_session(self):
        """assume_role is called and the agent runs inside use_session()."""
        fake_session = MagicMock(name="customer_session")
        observed: list = []

        def fake_use_session(session):
            observed.append(session)
            return nullcontext()

        with (
            patch(
                "src.lambda_handler.assume_role", return_value=fake_session
            ) as mock_assume,
            patch("src.lambda_handler.use_session", side_effect=fake_use_session),
            patch("src.lambda_handler._run_agent", return_value=_agent_result()),
        ):
            resp = handler(
                _build_event(
                    {
                        "query": "test",
                        "customer_context": {
                            "role_arn": VALID_ROLE_ARN,
                            "external_id": "shared-secret-happy-path",
                        },
                    }
                ),
                _lambda_ctx(),
            )

        assert resp["statusCode"] == 200
        mock_assume.assert_called_once()
        call_kwargs = mock_assume.call_args[1]
        assert call_kwargs["role_arn"] == VALID_ROLE_ARN
        assert call_kwargs["session_name"].startswith("agent-")
        assert call_kwargs.get("external_id") == "shared-secret-happy-path"
        # The agent ran inside the session block
        assert observed == [fake_session]

    def test_external_id_and_session_name_propagated(self):
        with (
            patch(
                "src.lambda_handler.assume_role", return_value=MagicMock()
            ) as mock_assume,
            patch(
                "src.lambda_handler.use_session", side_effect=lambda s: nullcontext()
            ),
            patch("src.lambda_handler._run_agent", return_value=_agent_result()),
        ):
            handler(
                _build_event(
                    {
                        "query": "test",
                        "customer_context": {
                            "role_arn": VALID_ROLE_ARN,
                            "external_id": "shared-secret",
                            "session_name": "planner-session",
                        },
                    }
                ),
                _lambda_ctx(),
            )

        kwargs = mock_assume.call_args[1]
        assert kwargs["external_id"] == "shared-secret"
        assert kwargs["session_name"] == "planner-session"


# ---------- error mapping ----------


class TestAssumeRoleErrors:
    @staticmethod
    def _client_error(code: str):
        from botocore.exceptions import ClientError

        return ClientError(
            {"Error": {"Code": code, "Message": "irrelevant"}}, "AssumeRole"
        )

    def test_access_denied_returns_403(self):
        with patch(
            "src.lambda_handler.assume_role",
            side_effect=self._client_error("AccessDenied"),
        ):
            resp = handler(
                _build_event(
                    {
                        "query": "test",
                        "customer_context": {
                            "role_arn": VALID_ROLE_ARN,
                            "external_id": "test-secret",
                        },
                    }
                ),
                _lambda_ctx(),
            )
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 403
        assert "denied" in body["error"].lower()

    def test_validation_error_returns_400(self):
        with patch(
            "src.lambda_handler.assume_role",
            side_effect=self._client_error("ValidationError"),
        ):
            resp = handler(
                _build_event(
                    {
                        "query": "test",
                        "customer_context": {
                            "role_arn": VALID_ROLE_ARN,
                            "external_id": "test-secret",
                        },
                    }
                ),
                _lambda_ctx(),
            )
        assert resp["statusCode"] == 400

    def test_unknown_sts_error_returns_502(self):
        with patch(
            "src.lambda_handler.assume_role",
            side_effect=RuntimeError("network hiccup"),
        ):
            resp = handler(
                _build_event(
                    {
                        "query": "test",
                        "customer_context": {
                            "role_arn": VALID_ROLE_ARN,
                            "external_id": "test-secret",
                        },
                    }
                ),
                _lambda_ctx(),
            )
        assert resp["statusCode"] == 502

    def test_error_response_does_not_leak_role_arn(self):
        with patch(
            "src.lambda_handler.assume_role",
            side_effect=self._client_error("AccessDenied"),
        ):
            resp = handler(
                _build_event(
                    {
                        "query": "test",
                        "customer_context": {
                            "role_arn": VALID_ROLE_ARN,
                            "external_id": "test-secret",
                        },
                    }
                ),
                _lambda_ctx(),
            )
        body = json.loads(resp["body"])
        assert VALID_ROLE_ARN not in body["error"]


# ---------- logging ----------


class TestLoggingIncludesCustomerAccount:
    def test_usage_tracking_log_includes_customer_account_id(self, caplog):
        with (
            patch("src.lambda_handler.assume_role", return_value=MagicMock()),
            patch(
                "src.lambda_handler.use_session", side_effect=lambda s: nullcontext()
            ),
            patch("src.lambda_handler._run_agent", return_value=_agent_result()),
        ):
            with caplog.at_level("INFO"):
                handler(
                    _build_event(
                        {
                            "query": "test",
                            "customer_context": {
                                "role_arn": VALID_ROLE_ARN,
                                "external_id": "test-secret",
                            },
                        }
                    ),
                    _lambda_ctx(),
                )

        usage_logs = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{")
            and json.loads(r.message).get("event") == "usage_tracking"
        ]
        assert len(usage_logs) == 1
        assert usage_logs[0]["customer_account_id"] == "123456789012"

    def test_no_customer_account_in_log_when_omitted(self, caplog):
        with patch("src.lambda_handler._run_agent", return_value=_agent_result()):
            with caplog.at_level("INFO"):
                handler(_build_event({"query": "test"}), _lambda_ctx())

        usage_logs = [
            json.loads(r.message)
            for r in caplog.records
            if r.message.startswith("{")
            and json.loads(r.message).get("event") == "usage_tracking"
        ]
        assert len(usage_logs) == 1
        assert "customer_account_id" not in usage_logs[0]
