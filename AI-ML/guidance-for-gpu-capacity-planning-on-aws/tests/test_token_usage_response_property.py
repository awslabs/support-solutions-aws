# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for token usage in response body.

Feature: production-observability-resilience
Property 11: Token Usage in Response Body

**Validates: Requirements 10.5**

For any successful invocation, the response body SHALL contain the token budget
limit and actual cumulative usage (input_tokens, output_tokens, total_tokens)
as integer values.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

# The lambda_handler uses bare imports (from agent import ..., from tools import ...)
# which work in Lambda runtime but not in test context. We mock these modules
# in sys.modules before importing the handler.
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

from src.lambda_handler import _success_response  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for token counts (non-negative integers representing token usage)
token_count_st = st.integers(min_value=0, max_value=500000)

# Strategy for budget limits (positive integers)
budget_limit_st = st.integers(min_value=1, max_value=1000000)

# Strategy for stop reasons
stop_reason_st = st.sampled_from(
    [
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "token_budget_exceeded",
        "timeout_safety",
    ]
)

# Strategy for tool call lists
tool_calls_st = st.lists(
    st.fixed_dictionaries(
        {"name": st.text(min_size=1, max_size=30), "input": st.just({})}
    ),
    min_size=0,
    max_size=10,
)

# Strategy for iteration count
iterations_st = st.integers(min_value=1, max_value=20)

# Strategy for final text (non-empty strings)
final_text_st = st.text(min_size=0, max_size=500)

# Strategy for tool outcomes
tool_outcomes_st = st.fixed_dictionaries(
    {
        "succeeded": st.lists(st.text(min_size=1, max_size=30), min_size=0, max_size=5),
        "failed": st.lists(
            st.fixed_dictionaries(
                {
                    "tool": st.text(min_size=1, max_size=30),
                    "error": st.text(min_size=1, max_size=100),
                }
            ),
            min_size=0,
            max_size=5,
        ),
    }
)


# ---------------------------------------------------------------------------
# Property test: Token Usage in Response Body
# ---------------------------------------------------------------------------


class TestTokenUsageInResponseBody:
    """Property 11: Token Usage in Response Body.

    Feature: production-observability-resilience
    Property 11: Token Usage in Response Body

    **Validates: Requirements 10.5**

    For any successful invocation, the response body SHALL contain the token
    budget limit and actual cumulative usage (input_tokens, output_tokens,
    total_tokens) as integer values.
    """

    @given(
        input_tokens=token_count_st,
        output_tokens=token_count_st,
        budget_limit=budget_limit_st,
        stop_reason=stop_reason_st,
        tool_calls=tool_calls_st,
        iterations=iterations_st,
        final_text=final_text_st,
        tool_outcomes=tool_outcomes_st,
    )
    @settings(max_examples=100)
    def test_response_contains_token_usage_as_integers(
        self,
        input_tokens: int,
        output_tokens: int,
        budget_limit: int,
        stop_reason: str,
        tool_calls: list,
        iterations: int,
        final_text: str,
        tool_outcomes: dict,
    ):
        """**Validates: Requirements 10.5**

        For any invocation result with varying token counts, the response body
        SHALL contain usage.budget_limit, usage.input_tokens, usage.output_tokens,
        and usage.total_tokens as integer values.
        """
        # Build a mock agent result with the generated token counts
        result = {
            "final_text": final_text,
            "tool_calls": tool_calls,
            "iterations": iterations,
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "token_budget_limit": budget_limit,
            "tool_outcomes": tool_outcomes,
        }

        request_id = "test-request-id-prop11"

        # Call _success_response
        response = _success_response(result, request_id)

        # Parse the response body
        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        # Verify usage section exists
        assert "usage" in body, "Response body must contain 'usage' field"

        usage = body["usage"]

        # Verify budget_limit is present and is an integer
        assert "budget_limit" in usage, "usage must contain 'budget_limit'"
        assert isinstance(usage["budget_limit"], int), (
            f"budget_limit must be an integer, got {type(usage['budget_limit']).__name__}"
        )
        assert usage["budget_limit"] == budget_limit

        # Verify input_tokens is present and is an integer
        assert "input_tokens" in usage, "usage must contain 'input_tokens'"
        assert isinstance(usage["input_tokens"], int), (
            f"input_tokens must be an integer, got {type(usage['input_tokens']).__name__}"
        )
        assert usage["input_tokens"] == input_tokens

        # Verify output_tokens is present and is an integer
        assert "output_tokens" in usage, "usage must contain 'output_tokens'"
        assert isinstance(usage["output_tokens"], int), (
            f"output_tokens must be an integer, got {type(usage['output_tokens']).__name__}"
        )
        assert usage["output_tokens"] == output_tokens

        # Verify total_tokens is present and is an integer
        assert "total_tokens" in usage, "usage must contain 'total_tokens'"
        assert isinstance(usage["total_tokens"], int), (
            f"total_tokens must be an integer, got {type(usage['total_tokens']).__name__}"
        )
        # total_tokens should be the sum of input and output tokens
        assert usage["total_tokens"] == input_tokens + output_tokens

    @given(
        input_tokens=token_count_st,
        output_tokens=token_count_st,
        budget_limit=budget_limit_st,
    )
    @settings(max_examples=100)
    def test_total_tokens_equals_sum_of_input_and_output(
        self,
        input_tokens: int,
        output_tokens: int,
        budget_limit: int,
    ):
        """**Validates: Requirements 10.5**

        For any token counts, total_tokens in the response SHALL always equal
        the sum of input_tokens and output_tokens.
        """
        result = {
            "final_text": "test response",
            "tool_calls": [],
            "iterations": 1,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "token_budget_limit": budget_limit,
            "tool_outcomes": {"succeeded": [], "failed": []},
        }

        response = _success_response(result, "test-req-id")
        body = json.loads(response["body"])
        usage = body["usage"]

        expected_total = input_tokens + output_tokens
        assert usage["total_tokens"] == expected_total, (
            f"total_tokens ({usage['total_tokens']}) must equal "
            f"input_tokens ({input_tokens}) + output_tokens ({output_tokens}) = {expected_total}"
        )

    @given(
        budget_limit=budget_limit_st,
    )
    @settings(max_examples=100)
    def test_budget_limit_reflects_configured_value(
        self,
        budget_limit: int,
    ):
        """**Validates: Requirements 10.5**

        For any configured budget limit, the response body SHALL contain
        that exact budget limit value as an integer.
        """
        result = {
            "final_text": "test",
            "tool_calls": [],
            "iterations": 1,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
            },
            "token_budget_limit": budget_limit,
            "tool_outcomes": {"succeeded": [], "failed": []},
        }

        response = _success_response(result, "test-req-id")
        body = json.loads(response["body"])

        assert body["usage"]["budget_limit"] == budget_limit
        assert isinstance(body["usage"]["budget_limit"], int)
