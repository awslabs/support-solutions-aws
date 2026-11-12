# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for timeout-aware loop control in src/agent.py.

Feature: production-observability-resilience
Property 6: Timeout-Aware Loop Control

*For any* remaining execution time value, the agent loop SHALL proceed with a
Bedrock API call if and only if at least 15 seconds remain; otherwise it SHALL
stop the loop, and the response SHALL have `incomplete=true` when stopped early
or `incomplete=false` when completed normally.

**Validates: Requirements 5.2, 5.3, 5.4**
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEOUT_THRESHOLD_MS = 15000  # 15 seconds safety margin


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for remaining time values covering the full Lambda timeout range
remaining_time_st = st.integers(min_value=0, max_value=180000)

# Strategy for remaining time values below the threshold (should stop)
below_threshold_st = st.integers(min_value=0, max_value=TIMEOUT_THRESHOLD_MS - 1)

# Strategy for remaining time values at or above the threshold (should proceed)
at_or_above_threshold_st = st.integers(min_value=TIMEOUT_THRESHOLD_MS, max_value=180000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_end_turn_response() -> dict:
    """Create a Bedrock converse response with end_turn stop reason."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Final answer from model."}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 50},
    }


def _make_tool_use_response(iteration_idx: int = 0) -> dict:
    """Create a Bedrock converse response requesting a tool call."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": f"tu-{iteration_idx}",
                            "name": "test_tool",
                            "input": {"param": "value"},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 80, "outputTokens": 40},
    }


def _create_agent_with_mock(
    mock_client: MagicMock, lambda_context: MagicMock
) -> CapacityDecisionAgent:
    """Create a CapacityDecisionAgent with mocked boto3 client and Lambda context."""
    with patch("src.agent.boto3.client", return_value=mock_client):
        agent = CapacityDecisionAgent(
            model_id="test-model",
            region="us-west-2",
            max_iterations=10,
            lambda_context=lambda_context,
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
# Property 6: Timeout-Aware Loop Control
# ---------------------------------------------------------------------------


class TestTimeoutAwareLoopControl:
    """Property 6: Timeout-Aware Loop Control.

    Feature: production-observability-resilience
    Property 6: Timeout-Aware Loop Control

    *For any* remaining execution time value, the agent loop SHALL proceed with
    a Bedrock API call if and only if at least 15 seconds remain; otherwise it
    SHALL stop the loop, and the response SHALL have `incomplete=true` when
    stopped early or `incomplete=false` when completed normally.

    **Validates: Requirements 5.2, 5.3, 5.4**
    """

    @given(remaining_ms=below_threshold_st)
    @settings(max_examples=100)
    def test_loop_stops_when_below_threshold(self, remaining_ms: int):
        """**Validates: Requirements 5.2, 5.3**

        For any remaining time value below 15000ms, the agent loop SHALL NOT
        make a Bedrock API call and SHALL set stop_reason to "timeout_safety".
        The response indicates the loop was stopped early (incomplete=true).
        """
        mock_client = MagicMock()
        mock_client.converse.return_value = _make_end_turn_response()

        lambda_context = MagicMock()
        lambda_context.get_remaining_time_in_millis.return_value = remaining_ms

        agent = _create_agent_with_mock(mock_client, lambda_context)
        result = agent.run("Test query")

        # The loop should NOT have called Bedrock
        mock_client.converse.assert_not_called()

        # stop_reason should be "timeout_safety"
        assert result["stop_reason"] == "timeout_safety", (
            f"Expected stop_reason='timeout_safety' for {remaining_ms}ms remaining, "
            f"got '{result['stop_reason']}'"
        )

        # When stopped early due to timeout, incomplete should be true
        # (The Lambda handler derives incomplete from stop_reason != "end_turn")
        assert result["stop_reason"] != "end_turn", (
            "stop_reason should not be 'end_turn' when stopped due to timeout"
        )

    @given(remaining_ms=at_or_above_threshold_st)
    @settings(max_examples=100)
    def test_loop_proceeds_when_at_or_above_threshold(self, remaining_ms: int):
        """**Validates: Requirements 5.2, 5.4**

        For any remaining time value >= 15000ms, the agent loop SHALL proceed
        with a Bedrock API call. When the model returns end_turn, the response
        has stop_reason "end_turn" (incomplete=false).
        """
        mock_client = MagicMock()
        mock_client.converse.return_value = _make_end_turn_response()

        lambda_context = MagicMock()
        lambda_context.get_remaining_time_in_millis.return_value = remaining_ms

        agent = _create_agent_with_mock(mock_client, lambda_context)
        result = agent.run("Test query")

        # The loop SHOULD have called Bedrock at least once
        mock_client.converse.assert_called()

        # stop_reason should be "end_turn" (completed normally)
        assert result["stop_reason"] == "end_turn", (
            f"Expected stop_reason='end_turn' for {remaining_ms}ms remaining, "
            f"got '{result['stop_reason']}'"
        )

    @given(remaining_ms=remaining_time_st)
    @settings(max_examples=100)
    def test_bedrock_called_iff_sufficient_time_remains(self, remaining_ms: int):
        """**Validates: Requirements 5.2, 5.3, 5.4**

        For any remaining time value, the agent loop proceeds with a Bedrock
        call if and only if >= 15000ms remain. This is the core biconditional
        property: proceed ⟺ remaining >= 15000.
        """
        mock_client = MagicMock()
        mock_client.converse.return_value = _make_end_turn_response()

        lambda_context = MagicMock()
        lambda_context.get_remaining_time_in_millis.return_value = remaining_ms

        agent = _create_agent_with_mock(mock_client, lambda_context)
        result = agent.run("Test query")

        if remaining_ms >= TIMEOUT_THRESHOLD_MS:
            # Should have proceeded with Bedrock call
            assert mock_client.converse.called, (
                f"Bedrock should have been called with {remaining_ms}ms remaining "
                f"(>= {TIMEOUT_THRESHOLD_MS}ms threshold)"
            )
            assert result["stop_reason"] == "end_turn", (
                f"Expected 'end_turn' when sufficient time ({remaining_ms}ms), "
                f"got '{result['stop_reason']}'"
            )
        else:
            # Should NOT have called Bedrock
            assert not mock_client.converse.called, (
                f"Bedrock should NOT have been called with {remaining_ms}ms remaining "
                f"(< {TIMEOUT_THRESHOLD_MS}ms threshold)"
            )
            assert result["stop_reason"] == "timeout_safety", (
                f"Expected 'timeout_safety' when insufficient time ({remaining_ms}ms), "
                f"got '{result['stop_reason']}'"
            )

    @given(remaining_ms=below_threshold_st)
    @settings(max_examples=100)
    def test_incomplete_true_when_stopped_early(self, remaining_ms: int):
        """**Validates: Requirements 5.3**

        When the agent loop stops due to insufficient remaining time, the
        stop_reason is "timeout_safety" which maps to incomplete=true in the
        Lambda handler response.
        """
        mock_client = MagicMock()
        mock_client.converse.return_value = _make_end_turn_response()

        lambda_context = MagicMock()
        lambda_context.get_remaining_time_in_millis.return_value = remaining_ms

        agent = _create_agent_with_mock(mock_client, lambda_context)
        result = agent.run("Test query")

        # stop_reason "timeout_safety" → incomplete=true in handler
        assert result["stop_reason"] == "timeout_safety"

        # The incomplete field is derived by the Lambda handler:
        # incomplete = (stop_reason != "end_turn")
        incomplete = result["stop_reason"] != "end_turn"
        assert incomplete is True, (
            f"Response should be incomplete when stopped early ({remaining_ms}ms remaining)"
        )

    @given(remaining_ms=at_or_above_threshold_st)
    @settings(max_examples=100)
    def test_incomplete_false_when_completed_normally(self, remaining_ms: int):
        """**Validates: Requirements 5.4**

        When the agent loop completes normally (end_turn), the stop_reason is
        "end_turn" which maps to incomplete=false in the Lambda handler response.
        """
        mock_client = MagicMock()
        mock_client.converse.return_value = _make_end_turn_response()

        lambda_context = MagicMock()
        lambda_context.get_remaining_time_in_millis.return_value = remaining_ms

        agent = _create_agent_with_mock(mock_client, lambda_context)
        result = agent.run("Test query")

        # stop_reason "end_turn" → incomplete=false in handler
        assert result["stop_reason"] == "end_turn"

        # The incomplete field is derived by the Lambda handler:
        # incomplete = (stop_reason != "end_turn")
        incomplete = result["stop_reason"] != "end_turn"
        assert incomplete is False, (
            f"Response should NOT be incomplete when completed normally "
            f"({remaining_ms}ms remaining)"
        )

    @given(
        initial_remaining_ms=at_or_above_threshold_st,
        second_remaining_ms=below_threshold_st,
    )
    @settings(max_examples=100)
    def test_mid_loop_timeout_stops_after_tool_execution(
        self, initial_remaining_ms: int, second_remaining_ms: int
    ):
        """**Validates: Requirements 5.2, 5.3**

        When the agent loop starts with sufficient time but remaining time
        drops below threshold mid-loop (after a tool call), the loop stops
        before the next Bedrock call with stop_reason "timeout_safety".
        """
        mock_client = MagicMock()
        # First call returns tool_use, second call would be end_turn (but shouldn't happen)
        mock_client.converse.side_effect = [
            _make_tool_use_response(0),
            _make_end_turn_response(),
        ]

        lambda_context = MagicMock()
        # First check: sufficient time → proceed
        # Second check: insufficient time → stop
        lambda_context.get_remaining_time_in_millis.side_effect = [
            initial_remaining_ms,
            second_remaining_ms,
        ]

        agent = _create_agent_with_mock(mock_client, lambda_context)
        result = agent.run("Test mid-loop timeout")

        # First Bedrock call should have been made (sufficient time initially)
        assert mock_client.converse.call_count == 1, (
            f"Expected exactly 1 Bedrock call (first iteration), "
            f"got {mock_client.converse.call_count}"
        )

        # Loop should have stopped due to timeout on second iteration
        assert result["stop_reason"] == "timeout_safety", (
            f"Expected 'timeout_safety' after mid-loop timeout, "
            f"got '{result['stop_reason']}'"
        )

        # Tool call from first iteration should be recorded
        assert len(result["tool_calls"]) == 1, (
            f"Expected 1 tool call from first iteration, "
            f"got {len(result['tool_calls'])}"
        )

        # Response is incomplete since we stopped early
        incomplete = result["stop_reason"] != "end_turn"
        assert incomplete is True
