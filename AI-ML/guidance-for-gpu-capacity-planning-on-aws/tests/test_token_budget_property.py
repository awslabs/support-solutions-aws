# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for token budget tracking and enforcement.

Feature: production-observability-resilience
Property 9: Token Budget Tracking and Enforcement

**Validates: Requirements 10.1, 10.2, 10.6**

For any sequence of Bedrock API calls with varying token usage, the agent loop
SHALL track cumulative tokens (input + output) as the exact sum of all individual
call usages, and SHALL stop the loop when cumulative usage exceeds the configured
budget, returning the last assistant message with stop_reason "token_budget_exceeded".
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for token budget (100-50000)
token_budget_st = st.integers(min_value=100, max_value=50000)

# Strategy for individual call token usage
token_usage_st = st.fixed_dictionaries(
    {
        "inputTokens": st.integers(min_value=1, max_value=5000),
        "outputTokens": st.integers(min_value=1, max_value=5000),
    }
)

# Strategy for a sequence of Bedrock responses with varying token usage (1-10 calls)
token_usage_sequence_st = st.lists(token_usage_st, min_size=1, max_size=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_end_turn_response(input_tokens: int, output_tokens: int) -> dict:
    """Create a Bedrock converse response with end_turn and specified token usage."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Final answer."}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }


def _make_tool_use_response(
    input_tokens: int, output_tokens: int, iteration_idx: int
) -> dict:
    """Create a Bedrock converse response requesting a tool call with specified token usage."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": f"tu-{iteration_idx}",
                            "name": "test_tool",
                            "input": {"param": f"value_{iteration_idx}"},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }


def _create_agent_with_mock(
    mock_client: MagicMock, budget: int
) -> CapacityDecisionAgent:
    """Create a CapacityDecisionAgent with a mocked boto3 client and specified budget."""
    with patch("src.agent.boto3.client", return_value=mock_client):
        with patch.dict(os.environ, {"TOKEN_BUDGET": str(budget)}):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=15,
            )
    # Register a test tool
    agent.register_tool(
        name="test_tool",
        description="A test tool",
        input_schema={"type": "object", "properties": {"param": {"type": "string"}}},
        handler=lambda **kwargs: {"result": "ok"},
    )
    return agent


# ---------------------------------------------------------------------------
# Property test: Token Budget Tracking and Enforcement
# ---------------------------------------------------------------------------


class TestTokenBudgetTrackingAndEnforcement:
    """Property 9: Token Budget Tracking and Enforcement.

    Feature: production-observability-resilience
    Property 9: Token Budget Tracking and Enforcement

    **Validates: Requirements 10.1, 10.2, 10.6**
    """

    @given(
        budget=token_budget_st,
        usage_sequence=token_usage_sequence_st,
    )
    @settings(max_examples=100)
    def test_cumulative_tracking_is_exact_sum(
        self, budget: int, usage_sequence: list[dict[str, int]]
    ):
        """**Validates: Requirements 10.1**

        The cumulative token tracking is the exact sum of all individual
        Bedrock call usages (input + output tokens).
        """
        # Build mock responses: all tool_use calls followed by end_turn
        mock_client = MagicMock()
        responses = []
        for idx, usage in enumerate(usage_sequence):
            responses.append(
                _make_tool_use_response(
                    usage["inputTokens"], usage["outputTokens"], idx
                )
            )
        # Add a final end_turn response with minimal tokens
        responses.append(_make_end_turn_response(10, 10))
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client, budget)

        with patch("src.agent.time.sleep"):
            result = agent.run("Test query")

        # Calculate expected cumulative tokens from all calls that actually
        # ran. Every completed converse call contributes its ``usage`` to
        # ``total_input_tokens`` / ``total_output_tokens`` before the loop
        # checks the budget or the stop reason (see src/agent.py). The
        # response index for call N (1-indexed) is N-1 in ``responses``, and
        # the trailing entry is the end_turn stub.
        actual_calls = mock_client.converse.call_count

        # Slice out the responses that actually ran (0..actual_calls-1).
        # For each, extract the (input, output) tokens.
        def _tokens_for_call(n: int) -> tuple[int, int]:
            if n < len(usage_sequence):
                return usage_sequence[n]["inputTokens"], usage_sequence[n][
                    "outputTokens"
                ]
            # The end_turn stub.
            return 10, 10

        expected_input = sum(_tokens_for_call(i)[0] for i in range(actual_calls))
        expected_output = sum(_tokens_for_call(i)[1] for i in range(actual_calls))

        # Verify cumulative tracking is exact sum
        assert result["usage"]["input_tokens"] == expected_input
        assert result["usage"]["output_tokens"] == expected_output
        assert (
            result["usage"]["input_tokens"] + result["usage"]["output_tokens"]
            == expected_input + expected_output
        )

    @given(
        budget=token_budget_st,
        usage_sequence=token_usage_sequence_st,
    )
    @settings(max_examples=100)
    def test_loop_stops_when_budget_exceeded(
        self, budget: int, usage_sequence: list[dict[str, int]]
    ):
        """**Validates: Requirements 10.2, 10.6**

        The agent loop stops when cumulative token usage exceeds the configured
        budget, and the stop_reason is "token_budget_exceeded".
        """
        # Calculate cumulative totals to determine if budget will be exceeded
        cumulative_totals = []
        running_total = 0
        for usage in usage_sequence:
            running_total += usage["inputTokens"] + usage["outputTokens"]
            cumulative_totals.append(running_total)

        # Only test cases where the budget WILL be exceeded during the sequence
        # (i.e., at least one cumulative total exceeds the budget)
        assume(any(total > budget for total in cumulative_totals))

        # Build mock responses: all tool_use calls followed by end_turn
        mock_client = MagicMock()
        responses = []
        for idx, usage in enumerate(usage_sequence):
            responses.append(
                _make_tool_use_response(
                    usage["inputTokens"], usage["outputTokens"], idx
                )
            )
        # Add end_turn in case budget is not exceeded (shouldn't happen due to assume)
        responses.append(_make_end_turn_response(10, 10))
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client, budget)

        with patch("src.agent.time.sleep"):
            result = agent.run("Test query")

        # The loop must have stopped due to budget exceeded
        assert result["stop_reason"] == "token_budget_exceeded", (
            f"Expected stop_reason 'token_budget_exceeded', got '{result['stop_reason']}'. "
            f"Budget={budget}, cumulative totals={cumulative_totals}"
        )

        # Verify cumulative usage exceeds budget
        total_used = result["usage"]["input_tokens"] + result["usage"]["output_tokens"]
        assert total_used > budget, (
            f"Total used ({total_used}) should exceed budget ({budget})"
        )

    @given(
        budget=token_budget_st,
        usage_sequence=token_usage_sequence_st,
    )
    @settings(max_examples=100)
    def test_loop_continues_when_within_budget(
        self, budget: int, usage_sequence: list[dict[str, int]]
    ):
        """**Validates: Requirements 10.1, 10.2**

        When cumulative token usage stays within budget, the loop continues
        normally and does NOT stop with "token_budget_exceeded".
        """
        # Calculate cumulative totals
        cumulative_totals = []
        running_total = 0
        for usage in usage_sequence:
            running_total += usage["inputTokens"] + usage["outputTokens"]
            cumulative_totals.append(running_total)

        # Only test cases where budget is NOT exceeded during the sequence
        # Also account for the final end_turn response (10 + 10 = 20 tokens)
        total_with_end = running_total + 20
        assume(total_with_end <= budget)

        # Build mock responses: all tool_use calls followed by end_turn
        mock_client = MagicMock()
        responses = []
        for idx, usage in enumerate(usage_sequence):
            responses.append(
                _make_tool_use_response(
                    usage["inputTokens"], usage["outputTokens"], idx
                )
            )
        responses.append(_make_end_turn_response(10, 10))
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client, budget)

        with patch("src.agent.time.sleep"):
            result = agent.run("Test query")

        # The loop should NOT have stopped due to budget
        assert result["stop_reason"] != "token_budget_exceeded", (
            f"Loop should not stop for budget when within limits. "
            f"Budget={budget}, total_with_end={total_with_end}"
        )

    @given(
        budget=token_budget_st,
        usage_sequence=token_usage_sequence_st,
    )
    @settings(max_examples=100)
    def test_budget_exceeded_stops_at_first_exceeding_call(
        self, budget: int, usage_sequence: list[dict[str, int]]
    ):
        """**Validates: Requirements 10.1, 10.2, 10.6**

        When budget is exceeded, the loop stops immediately after the call
        that caused the cumulative total to exceed the budget — no further
        Bedrock calls are made after that point.
        """
        # Calculate which call first exceeds the budget
        cumulative_totals = []
        running_total = 0
        first_exceeding_idx = None
        for i, usage in enumerate(usage_sequence):
            running_total += usage["inputTokens"] + usage["outputTokens"]
            cumulative_totals.append(running_total)
            if running_total > budget and first_exceeding_idx is None:
                first_exceeding_idx = i

        # Only test cases where budget will be exceeded
        assume(first_exceeding_idx is not None)

        # Build mock responses
        mock_client = MagicMock()
        responses = []
        for idx, usage in enumerate(usage_sequence):
            responses.append(
                _make_tool_use_response(
                    usage["inputTokens"], usage["outputTokens"], idx
                )
            )
        responses.append(_make_end_turn_response(10, 10))
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client, budget)

        with patch("src.agent.time.sleep"):
            result = agent.run("Test query")

        # The number of Bedrock calls should be exactly first_exceeding_idx + 1
        # (0-indexed, so the call at index first_exceeding_idx is the one that exceeds)
        expected_calls = first_exceeding_idx + 1
        actual_calls = mock_client.converse.call_count

        assert actual_calls == expected_calls, (
            f"Expected {expected_calls} Bedrock calls (stop at index {first_exceeding_idx}), "
            f"but got {actual_calls}. Budget={budget}, cumulative_totals={cumulative_totals}"
        )

        # Verify stop reason
        assert result["stop_reason"] == "token_budget_exceeded"
