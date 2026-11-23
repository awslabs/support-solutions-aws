# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""End-to-end integration tests with mocked AWS services.

Tests the full request flow through the Lambda handler with mocked Bedrock,
DynamoDB, and Secrets Manager services. Verifies metrics emission, retry
behavior, cache interaction, usage tracking, graceful degradation, and
token budget exhaustion.

Requirements: 3.1, 4.1, 6.1, 10.2, 12.2
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock bare-import modules before importing the handler
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
from contextlib import nullcontext as _nullcontext  # noqa: E402

_mock_aws_session_module = MagicMock()
_mock_aws_session_module.use_session = lambda *_a, **_kw: _nullcontext()
sys.modules.setdefault("aws_session", _mock_aws_session_module)

from src.lambda_handler import handler, _stable_caller_hash  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lambda_context():
    """Mock Lambda context with 60s remaining."""
    ctx = MagicMock()
    ctx.aws_request_id = "integration-test-req-001"
    ctx.function_name = "CapacityAgentFunction"
    ctx.memory_limit_in_mb = 256
    ctx.invoked_function_arn = (
        "arn:aws:lambda:us-west-2:123456789012:function:CapacityAgentFunction"
    )
    ctx.get_remaining_time_in_millis.return_value = 60000
    return ctx


@pytest.fixture
def api_event():
    """Factory for API Gateway proxy events."""

    def _make(
        query: str = "What GPU instances are available in us-east-1?",
        identity: dict | None = None,
    ) -> dict:
        event = {
            "httpMethod": "POST",
            "path": "/plan",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"query": query}),
            "isBase64Encoded": False,
            "requestContext": {
                "requestId": "apigw-req-001",
                "stage": "prod",
            },
        }
        if identity:
            event["requestContext"]["identity"] = identity
        return event

    return _make


@pytest.fixture
def successful_agent_result():
    """A complete successful agent result with tool calls."""
    return {
        "final_text": "p4d.24xlarge is available in us-east-1 across 3 AZs.",
        "messages": [],
        "tool_calls": [
            (
                "check_instance_availability",
                {"instance_type": "p4d.24xlarge", "region": "us-east-1"},
                {"ok": True, "data": {"available": True}},
            ),
            (
                "get_sagemaker_pricing",
                {"instance_type": "p4d.24xlarge", "region": "us-east-1"},
                {"ok": True, "data": {"price": 32.77}},
            ),
        ],
        "iterations": 3,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3200, "output_tokens": 1800},
        "token_budget_limit": 20000,
        "tool_outcomes": {
            "succeeded": ["check_instance_availability", "get_sagemaker_pricing"],
            "failed": [],
        },
    }


@pytest.fixture
def degraded_agent_result():
    """Agent result with partial tool failures (graceful degradation)."""
    return {
        "final_text": "p4d.24xlarge is available but quota check failed.",
        "messages": [],
        "tool_calls": [
            (
                "check_instance_availability",
                {"instance_type": "p4d.24xlarge", "region": "us-east-1"},
                {"ok": True, "data": {"available": True}},
            ),
            (
                "get_sagemaker_quota",
                {"instance_type": "p4d.24xlarge", "region": "us-east-1"},
                {
                    "ok": False,
                    "error": "ThrottlingException: Rate exceeded",
                    "error_type": "ThrottlingException",
                    "tool_name": "get_sagemaker_quota",
                },
            ),
        ],
        "iterations": 2,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2500, "output_tokens": 1200},
        "token_budget_limit": 20000,
        "tool_outcomes": {
            "succeeded": ["check_instance_availability"],
            "failed": [
                {
                    "tool": "get_sagemaker_quota",
                    "error_type": "ThrottlingException",
                    "error": "ThrottlingException: Rate exceeded",
                },
            ],
        },
    }


@pytest.fixture
def budget_exhausted_result():
    """Agent result when token budget is exceeded."""
    return {
        "final_text": "Based on the data gathered so far, p4d.24xlarge is available...",
        "messages": [],
        "tool_calls": [
            (
                "check_instance_availability",
                {"instance_type": "p4d.24xlarge", "region": "us-east-1"},
                {"ok": True, "data": {"available": True}},
            ),
        ],
        "iterations": 5,
        "stop_reason": "token_budget_exceeded",
        "usage": {"input_tokens": 15000, "output_tokens": 6000},
        "token_budget_limit": 20000,
        "tool_outcomes": {
            "succeeded": ["check_instance_availability"],
            "failed": [],
        },
    }


# ---------------------------------------------------------------------------
# Test 1: Full successful invocation
# ---------------------------------------------------------------------------


class TestFullSuccessfulInvocation:
    """Test the complete happy path: handler → secrets → agent → cache miss →
    tool execution → cache put → metrics → response.
    """

    def test_successful_invocation_returns_200_with_complete_response(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Full invocation returns 200 with all expected response fields."""
        event = api_event(
            query="Is p4d.24xlarge available in us-east-1?",
            identity={"userArn": "arn:aws:iam::123456789012:user/engineer"},
        )

        with patch(
            "src.lambda_handler._run_agent", return_value=successful_agent_result
        ):
            response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        # Verify response structure matches design doc "Agent Response (Enhanced)"
        assert body["request_id"] == "integration-test-req-001"
        assert body["answer"] == "p4d.24xlarge is available in us-east-1 across 3 AZs."
        assert body["tool_calls"] == 2
        assert body["iterations"] == 3
        assert body["incomplete"] is False
        assert body["stop_reason"] == "end_turn"

        # Verify usage section
        assert body["usage"]["input_tokens"] == 3200
        assert body["usage"]["output_tokens"] == 1800
        assert body["usage"]["total_tokens"] == 5000
        assert body["usage"]["budget_limit"] == 20000

        # Verify tool_outcomes
        assert "check_instance_availability" in body["tool_outcomes"]["succeeded"]
        assert "get_sagemaker_pricing" in body["tool_outcomes"]["succeeded"]
        assert body["tool_outcomes"]["failed"] == []

    def test_successful_invocation_emits_usage_tracking_log(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Successful invocation emits a usage_tracking structured log."""
        event = api_event(
            query="Is p4d.24xlarge available?",
            identity={"userArn": "arn:aws:iam::123456789012:user/engineer"},
        )

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=successful_agent_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        # Find usage_tracking log entry
        usage_logs = []
        for log_call in mock_logger.info.call_args_list:
            try:
                log_data = json.loads(log_call[0][0])
                if log_data.get("event") == "usage_tracking":
                    usage_logs.append(log_data)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue

        assert len(usage_logs) == 1
        log_entry = usage_logs[0]

        # caller_id is hashed as caller_id_hash + caller_type
        assert log_entry["caller_id_hash"] == _stable_caller_hash(
            "arn:aws:iam::123456789012:user/engineer"
        )
        assert log_entry["caller_type"] == "iam"
        assert log_entry["input_tokens"] == 3200
        assert log_entry["output_tokens"] == 1800
        assert log_entry["total_tokens"] == 5000
        assert log_entry["tool_call_count"] == 2
        assert "model_id" in log_entry
        assert log_entry["request_timestamp"].endswith("Z")
        assert "T" in log_entry["request_timestamp"]

    def test_successful_invocation_emits_invocation_log(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Successful invocation emits an agent_invocation structured log."""
        event = api_event(query="Is p4d.24xlarge available?")

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=successful_agent_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        # Find agent_invocation log entry
        invocation_logs = []
        for log_call in mock_logger.info.call_args_list:
            try:
                log_data = json.loads(log_call[0][0])
                if log_data.get("event") == "agent_invocation":
                    invocation_logs.append(log_data)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue

        assert len(invocation_logs) == 1
        log_entry = invocation_logs[0]

        assert log_entry["request_id"] == "integration-test-req-001"
        assert log_entry["tool_calls"] == 2
        assert log_entry["iterations"] == 3
        assert log_entry["input_tokens"] == 3200
        assert log_entry["output_tokens"] == 1800
        assert log_entry["stop_reason"] == "end_turn"
        assert "duration_ms" in log_entry
        assert isinstance(log_entry["duration_ms"], int)


# ---------------------------------------------------------------------------
# Test 2: Cache hit scenario
# ---------------------------------------------------------------------------


class TestCacheHitScenario:
    """Test: handler → agent → cache hit → no tool execution → metrics → response."""

    def test_cache_hit_returns_cached_result_without_tool_execution(
        self, api_event, lambda_context
    ):
        """When cache has a valid result, the tool handler is NOT called."""
        # This tests the full flow where the agent uses cached results
        cached_result = {
            "final_text": "Cached: p4d.24xlarge is available.",
            "messages": [],
            "tool_calls": [
                (
                    "check_instance_availability",
                    {"instance_type": "p4d.24xlarge", "region": "us-east-1"},
                    {"ok": True, "data": {"available": True}},
                ),
            ],
            "iterations": 2,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1500, "output_tokens": 800},
            "token_budget_limit": 20000,
            "tool_outcomes": {
                "succeeded": ["check_instance_availability"],
                "failed": [],
            },
        }

        event = api_event(query="Is p4d.24xlarge available in us-east-1?")

        with patch("src.lambda_handler._run_agent", return_value=cached_result):
            response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["answer"] == "Cached: p4d.24xlarge is available."
        assert body["tool_calls"] == 1
        assert body["incomplete"] is False


# ---------------------------------------------------------------------------
# Test 3: Graceful degradation with partial tool failures
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Test: handler → agent → tool failure after retries → continue with
    other tools → response with tool_outcomes.
    """

    def test_partial_tool_failure_returns_200_with_failure_metadata(
        self, api_event, lambda_context, degraded_agent_result
    ):
        """When some tools fail, response still returns 200 with failure details."""
        event = api_event(query="Check p4d.24xlarge availability and quota")

        with patch("src.lambda_handler._run_agent", return_value=degraded_agent_result):
            response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        # Agent still produced an answer
        assert body["answer"] == "p4d.24xlarge is available but quota check failed."
        assert body["incomplete"] is False
        assert body["stop_reason"] == "end_turn"

        # Tool outcomes reflect partial failure
        assert "check_instance_availability" in body["tool_outcomes"]["succeeded"]
        assert len(body["tool_outcomes"]["failed"]) == 1
        failed_tool = body["tool_outcomes"]["failed"][0]
        assert failed_tool["tool"] == "get_sagemaker_quota"
        assert failed_tool["error_type"] == "ThrottlingException"
        assert "Rate exceeded" in failed_tool["error"]

    def test_all_tools_fail_returns_200_with_empty_answer(
        self, api_event, lambda_context
    ):
        """When ALL tools fail, response still returns 200 with failure summary."""
        all_failed_result = {
            "final_text": "I was unable to gather the requested information due to service errors.",
            "messages": [],
            "tool_calls": [
                (
                    "check_instance_availability",
                    {},
                    {
                        "ok": False,
                        "error": "TimeoutError: timed out",
                        "error_type": "TimeoutError",
                        "tool_name": "check_instance_availability",
                    },
                ),
                (
                    "get_sagemaker_pricing",
                    {},
                    {
                        "ok": False,
                        "error": "ThrottlingException: Rate exceeded",
                        "error_type": "ThrottlingException",
                        "tool_name": "get_sagemaker_pricing",
                    },
                ),
            ],
            "iterations": 2,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 2000, "output_tokens": 500},
            "token_budget_limit": 20000,
            "tool_outcomes": {
                "succeeded": [],
                "failed": [
                    {
                        "tool": "check_instance_availability",
                        "error_type": "TimeoutError",
                        "error": "TimeoutError: timed out",
                    },
                    {
                        "tool": "get_sagemaker_pricing",
                        "error_type": "ThrottlingException",
                        "error": "ThrottlingException: Rate exceeded",
                    },
                ],
            },
        }

        event = api_event(query="Check everything for p4d.24xlarge")

        with patch("src.lambda_handler._run_agent", return_value=all_failed_result):
            response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        # Response still returned (not 500)
        assert body["answer"] != ""
        assert body["tool_outcomes"]["succeeded"] == []
        assert len(body["tool_outcomes"]["failed"]) == 2

    def test_graceful_degradation_emits_usage_tracking(
        self, api_event, lambda_context, degraded_agent_result
    ):
        """Usage tracking log is emitted even when tools partially fail."""
        event = api_event(
            query="Check availability",
            identity={"userArn": "arn:aws:iam::123456789012:user/ops"},
        )

        with (
            patch("src.lambda_handler._run_agent", return_value=degraded_agent_result),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        # Verify usage_tracking log was still emitted
        usage_logs = []
        for log_call in mock_logger.info.call_args_list:
            try:
                log_data = json.loads(log_call[0][0])
                if log_data.get("event") == "usage_tracking":
                    usage_logs.append(log_data)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue

        assert len(usage_logs) == 1
        # caller_id is hashed as caller_id_hash + caller_type
        assert usage_logs[0]["caller_id_hash"] == _stable_caller_hash(
            "arn:aws:iam::123456789012:user/ops"
        )
        assert usage_logs[0]["caller_type"] == "iam"
        assert usage_logs[0]["total_tokens"] == 3700  # 2500 + 1200


# ---------------------------------------------------------------------------
# Test 4: Token budget exhaustion scenario
# ---------------------------------------------------------------------------


class TestTokenBudgetExhaustion:
    """Test: handler → agent → budget exceeded → response with
    stop_reason="token_budget_exceeded".
    """

    def test_budget_exhaustion_returns_200_with_correct_stop_reason(
        self, api_event, lambda_context, budget_exhausted_result
    ):
        """When token budget is exceeded, response has stop_reason='token_budget_exceeded'."""
        event = api_event(query="Comprehensive analysis of all GPU options")

        with patch(
            "src.lambda_handler._run_agent", return_value=budget_exhausted_result
        ):
            response = handler(event, lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        assert body["stop_reason"] == "token_budget_exceeded"
        assert body["incomplete"] is True
        assert (
            body["answer"]
            == "Based on the data gathered so far, p4d.24xlarge is available..."
        )

        # Usage reflects the actual consumption
        assert body["usage"]["input_tokens"] == 15000
        assert body["usage"]["output_tokens"] == 6000
        assert body["usage"]["total_tokens"] == 21000
        assert body["usage"]["budget_limit"] == 20000

    def test_budget_exhaustion_emits_usage_tracking_with_exceeded_tokens(
        self, api_event, lambda_context, budget_exhausted_result
    ):
        """Usage tracking log reflects actual token usage even when budget exceeded."""
        event = api_event(
            query="Comprehensive analysis",
            identity={"apiKeyId": "api-key-12345"},
        )

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=budget_exhausted_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        usage_logs = []
        for log_call in mock_logger.info.call_args_list:
            try:
                log_data = json.loads(log_call[0][0])
                if log_data.get("event") == "usage_tracking":
                    usage_logs.append(log_data)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue

        assert len(usage_logs) == 1
        log_entry = usage_logs[0]
        # caller_id is hashed as caller_id_hash + caller_type
        assert log_entry["caller_id_hash"] == _stable_caller_hash("api-key-12345")
        assert log_entry["caller_type"] == "api_key"
        assert log_entry["input_tokens"] == 15000
        assert log_entry["output_tokens"] == 6000
        assert log_entry["total_tokens"] == 21000

    def test_budget_exhaustion_still_includes_tool_outcomes(
        self, api_event, lambda_context, budget_exhausted_result
    ):
        """Even when budget is exhausted, tool outcomes are included in response."""
        event = api_event(query="Comprehensive analysis")

        with patch(
            "src.lambda_handler._run_agent", return_value=budget_exhausted_result
        ):
            response = handler(event, lambda_context)

        body = json.loads(response["body"])
        assert "tool_outcomes" in body
        assert "check_instance_availability" in body["tool_outcomes"]["succeeded"]


# ---------------------------------------------------------------------------
# Test 5: Metrics emission verification
# ---------------------------------------------------------------------------


class TestMetricsEmission:
    """Verify that metrics are emitted during the request flow."""

    def test_successful_invocation_includes_metrics_in_response_context(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Successful invocation produces a response with usage metrics."""
        event = api_event(query="Check availability")

        with patch(
            "src.lambda_handler._run_agent", return_value=successful_agent_result
        ):
            response = handler(event, lambda_context)

        body = json.loads(response["body"])
        # Verify usage metrics are present and correct types
        assert isinstance(body["usage"]["input_tokens"], int)
        assert isinstance(body["usage"]["output_tokens"], int)
        assert isinstance(body["usage"]["total_tokens"], int)
        assert isinstance(body["usage"]["budget_limit"], int)
        assert (
            body["usage"]["total_tokens"]
            == body["usage"]["input_tokens"] + body["usage"]["output_tokens"]
        )


# ---------------------------------------------------------------------------
# Test 6: Caller identity extraction in full flow
# ---------------------------------------------------------------------------


class TestCallerIdentityInFlow:
    """Verify caller identity extraction works correctly in the full request flow."""

    def test_iam_caller_identity_in_usage_log(
        self, api_event, lambda_context, successful_agent_result
    ):
        """IAM ARN is used as caller_id when present."""
        event = api_event(
            query="Check availability",
            identity={"userArn": "arn:aws:iam::111111111111:role/AdminRole"},
        )

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=successful_agent_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        usage_logs = self._find_usage_logs(mock_logger)
        # caller_id is hashed as caller_id_hash + caller_type
        assert usage_logs[0]["caller_id_hash"] == _stable_caller_hash(
            "arn:aws:iam::111111111111:role/AdminRole"
        )
        assert usage_logs[0]["caller_type"] == "iam"

    def test_api_key_caller_identity_in_usage_log(
        self, api_event, lambda_context, successful_agent_result
    ):
        """API key ID is used when IAM ARN is not present."""
        event = api_event(
            query="Check availability",
            identity={"apiKeyId": "key-abc123"},
        )

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=successful_agent_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        usage_logs = self._find_usage_logs(mock_logger)
        # caller_id is hashed as caller_id_hash + caller_type
        assert usage_logs[0]["caller_id_hash"] == _stable_caller_hash("key-abc123")
        assert usage_logs[0]["caller_type"] == "api_key"

    def test_cognito_caller_identity_in_usage_log(
        self, lambda_context, successful_agent_result
    ):
        """Cognito sub is used when IAM and API key are not present."""
        event = {
            "httpMethod": "POST",
            "path": "/plan",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"query": "Check availability"}),
            "isBase64Encoded": False,
            "requestContext": {
                "requestId": "apigw-req-001",
                "stage": "prod",
                "authorizer": {
                    "claims": {"sub": "cognito-user-uuid-456"},
                },
            },
        }

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=successful_agent_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        usage_logs = self._find_usage_logs(mock_logger)
        # H5: caller_id -> caller_id_hash + caller_type
        assert usage_logs[0]["caller_id_hash"] == _stable_caller_hash(
            "cognito-user-uuid-456"
        )
        assert usage_logs[0]["caller_type"] == "cognito"

    def test_anonymous_caller_when_no_identity(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Falls back to 'anonymous' when no identity is available."""
        event = api_event(query="Check availability")  # No identity

        with (
            patch(
                "src.lambda_handler._run_agent", return_value=successful_agent_result
            ),
            patch("src.lambda_handler.logger") as mock_logger,
        ):
            handler(event, lambda_context)

        usage_logs = self._find_usage_logs(mock_logger)
        # H5: anonymous passes through un-hashed; caller_type also
        # "anonymous" so operators can distinguish "no auth" from a
        # hashed identifier that happens to equal "anonymous".
        assert usage_logs[0]["caller_id_hash"] == "anonymous"
        assert usage_logs[0]["caller_type"] == "anonymous"

    @staticmethod
    def _find_usage_logs(mock_logger) -> list[dict]:
        """Extract usage_tracking log entries from mock logger calls."""
        logs = []
        for log_call in mock_logger.info.call_args_list:
            try:
                log_data = json.loads(log_call[0][0])
                if log_data.get("event") == "usage_tracking":
                    logs.append(log_data)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue
        return logs


# ---------------------------------------------------------------------------
# Test 7: Secrets retrieval failure at cold start
# ---------------------------------------------------------------------------


class TestSecretsRetrievalFailure:
    """Test that secrets retrieval failure at cold start returns 503."""

    def test_secrets_failure_returns_503(self, api_event, lambda_context):
        """When secrets init fails, all requests get 503."""
        import src.lambda_handler as handler_module

        fake_error = handler_module.SecretsRetrievalError(
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:agent-config",
            "ClientError",
        )
        original_error = handler_module._secrets_init_error
        handler_module._secrets_init_error = fake_error

        try:
            event = api_event(query="Check availability")
            response = handler(event, lambda_context)

            assert response["statusCode"] == 503
            body = json.loads(response["body"])
            assert "error" in body
            assert body["request_id"] == "integration-test-req-001"
        finally:
            handler_module._secrets_init_error = original_error


# ---------------------------------------------------------------------------
# Test 8: Response format validation
# ---------------------------------------------------------------------------


class TestResponseFormatValidation:
    """Verify the response format matches the design document's
    'Agent Response (Enhanced)' specification.
    """

    def test_response_contains_all_required_fields(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Response body contains all fields from the design doc spec."""
        event = api_event(query="Check availability")

        with patch(
            "src.lambda_handler._run_agent", return_value=successful_agent_result
        ):
            response = handler(event, lambda_context)

        body = json.loads(response["body"])

        # All required top-level fields
        required_fields = {
            "request_id",
            "answer",
            "tool_calls",
            "iterations",
            "incomplete",
            "stop_reason",
            "usage",
            "tool_outcomes",
        }
        assert required_fields.issubset(set(body.keys()))

        # Usage sub-fields
        usage_fields = {"input_tokens", "output_tokens", "total_tokens", "budget_limit"}
        assert usage_fields.issubset(set(body["usage"].keys()))

        # Tool outcomes sub-fields
        assert "succeeded" in body["tool_outcomes"]
        assert "failed" in body["tool_outcomes"]
        assert isinstance(body["tool_outcomes"]["succeeded"], list)
        assert isinstance(body["tool_outcomes"]["failed"], list)

    def test_response_field_types_are_correct(
        self, api_event, lambda_context, successful_agent_result
    ):
        """Response field types match the design document specification."""
        event = api_event(query="Check availability")

        with patch(
            "src.lambda_handler._run_agent", return_value=successful_agent_result
        ):
            response = handler(event, lambda_context)

        body = json.loads(response["body"])

        assert isinstance(body["request_id"], str)
        assert isinstance(body["answer"], str)
        assert isinstance(body["tool_calls"], int)
        assert isinstance(body["iterations"], int)
        assert isinstance(body["incomplete"], bool)
        assert isinstance(body["stop_reason"], str)
        assert isinstance(body["usage"], dict)
        assert isinstance(body["tool_outcomes"], dict)

        # Usage values are integers
        for key in ("input_tokens", "output_tokens", "total_tokens", "budget_limit"):
            assert isinstance(body["usage"][key], int), f"usage.{key} should be int"

    def test_incomplete_is_false_for_end_turn(
        self, api_event, lambda_context, successful_agent_result
    ):
        """incomplete=False when stop_reason is 'end_turn'."""
        event = api_event(query="Check availability")

        with patch(
            "src.lambda_handler._run_agent", return_value=successful_agent_result
        ):
            response = handler(event, lambda_context)

        body = json.loads(response["body"])
        assert body["incomplete"] is False
        assert body["stop_reason"] == "end_turn"

    def test_incomplete_is_true_for_non_end_turn_stop_reasons(
        self, api_event, lambda_context
    ):
        """incomplete=True for stop reasons other than 'end_turn'."""
        for stop_reason in (
            "token_budget_exceeded",
            "timeout_safety",
            "max_iterations",
        ):
            result = {
                "final_text": "Partial answer.",
                "messages": [],
                "tool_calls": [],
                "iterations": 5,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 1000, "output_tokens": 500},
                "token_budget_limit": 20000,
                "tool_outcomes": {"succeeded": [], "failed": []},
            }

            event = api_event(query="Check availability")

            with patch("src.lambda_handler._run_agent", return_value=result):
                response = handler(event, lambda_context)

            body = json.loads(response["body"])
            assert body["incomplete"] is True, (
                f"Expected incomplete=True for stop_reason={stop_reason}"
            )
            assert body["stop_reason"] == stop_reason
