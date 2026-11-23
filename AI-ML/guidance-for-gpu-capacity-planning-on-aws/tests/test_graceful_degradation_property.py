# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for graceful degradation continuation.

Feature: production-observability-resilience
Property 7: Graceful Degradation Continuation

For any combination of tool successes and failures within a single invocation,
the agent loop SHALL continue executing all requested tools (not abort on first
failure), and the final response metadata SHALL accurately list all succeeded
tool names and all failed tool names with their error descriptions.

**Validates: Requirements 6.1, 6.3**
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for tool names (unique identifiers)
tool_name_st = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"
    ),
    min_size=1,
    max_size=20,
).filter(lambda s: s[0].isalpha())

# Strategy for whether a tool should succeed or fail
tool_outcome_st = st.booleans()  # True = success, False = failure

# Strategy for a list of tools with their outcomes (at least 1 tool, up to 6)
tool_config_st = st.lists(
    st.tuples(tool_name_st, tool_outcome_st),
    min_size=1,
    max_size=6,
    unique_by=lambda x: x[0],  # unique tool names
)

# Strategy for error types
error_type_st = st.sampled_from(
    [
        "ThrottlingException",
        "TimeoutError",
        "ConnectionError",
        "RuntimeError",
        "ValueError",
        "ServiceUnavailableError",
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_use_response(
    tool_configs: list[tuple[str, bool]], iteration_idx: int = 0
) -> dict:
    """Create a Bedrock converse response requesting tool calls for all configured tools."""
    content = [
        {
            "toolUse": {
                "toolUseId": f"tu-{iteration_idx}-{i}",
                "name": name,
                "input": {"param": f"value_{i}"},
            }
        }
        for i, (name, _) in enumerate(tool_configs)
    ]
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": content,
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 50, "outputTokens": 20},
    }


def _make_end_turn_response() -> dict:
    """Create a Bedrock converse response with end_turn stop reason."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Final answer with partial results."}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 60, "outputTokens": 30},
    }


def _create_agent_with_tools(
    mock_client: MagicMock,
    tool_configs: list[tuple[str, bool]],
    error_types: dict[str, str],
) -> CapacityDecisionAgent:
    """Create a CapacityDecisionAgent with tools configured to succeed or fail.

    Args:
        mock_client: Mocked boto3 bedrock-runtime client.
        tool_configs: List of (tool_name, should_succeed) tuples.
        error_types: Mapping of tool_name -> exception class name for failing tools.
    """
    with patch("src.agent.boto3.client", return_value=mock_client):
        agent = CapacityDecisionAgent(
            model_id="test-model",
            region="us-west-2",
            max_iterations=10,
        )

    for name, should_succeed in tool_configs:
        if should_succeed:

            def _make_success_handler(_name=name):
                def handler(**kwargs):
                    return {"result": "ok", "tool": _name}

                return handler

            handler = _make_success_handler()
        else:
            exc_type_name = error_types.get(name, "RuntimeError")

            def _make_failing_handler(_name=name, _exc_type=exc_type_name):
                def handler(**kwargs):
                    raise type(_exc_type, (Exception,), {})(_name + " failed")

                return handler

            handler = _make_failing_handler()

        agent.register_tool(
            name=name,
            description=f"Tool {name}",
            input_schema={
                "type": "object",
                "properties": {"param": {"type": "string"}},
            },
            handler=handler,
        )

    return agent


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestGracefulDegradationContinuation:
    """Property 7: Graceful Degradation Continuation.

    Feature: production-observability-resilience
    Property 7: Graceful Degradation Continuation

    **Validates: Requirements 6.1, 6.3**
    """

    @given(
        tool_configs=tool_config_st,
        error_types=st.dictionaries(
            keys=tool_name_st,
            values=error_type_st,
            min_size=0,
            max_size=6,
        ),
    )
    @settings(max_examples=100)
    def test_all_tools_attempted_regardless_of_failures(
        self,
        tool_configs: list[tuple[str, bool]],
        error_types: dict[str, str],
    ):
        """**Validates: Requirements 6.1, 6.3**

        For any combination of tool successes and failures, all tools are
        attempted (the agent does not abort on first failure) and the response
        metadata accurately lists succeeded and failed tools.
        """
        # Ensure we have at least one tool
        assume(len(tool_configs) >= 1)

        # Build mock client responses: one tool_use response requesting all tools, then end_turn
        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _make_tool_use_response(tool_configs),
            _make_end_turn_response(),
        ]

        # Patch with_retry to call through directly (no actual retries/delays in test)
        with patch(
            "src.agent.with_retry",
            side_effect=lambda func, *args, **kwargs: func(*args),
        ):
            agent = _create_agent_with_tools(mock_client, tool_configs, error_types)
            result = agent.run("Test graceful degradation")

        # --- Property assertions ---

        # 1. All tools were attempted: tool_calls list should have one entry per tool
        assert len(result["tool_calls"]) == len(tool_configs), (
            f"Expected {len(tool_configs)} tool calls, got {len(result['tool_calls'])}. "
            f"Agent may have aborted on first failure."
        )

        # 2. Verify each tool was called in order
        for i, (expected_name, _) in enumerate(tool_configs):
            actual_name = result["tool_calls"][i][0]
            assert actual_name == expected_name, (
                f"Tool call {i}: expected '{expected_name}', got '{actual_name}'"
            )

        # 3. Verify tool_outcomes metadata accurately reflects successes and failures
        expected_succeeded = [
            name for name, should_succeed in tool_configs if should_succeed
        ]
        expected_failed_names = [
            name for name, should_succeed in tool_configs if not should_succeed
        ]

        actual_succeeded = result["tool_outcomes"]["succeeded"]
        actual_failed = result["tool_outcomes"]["failed"]
        actual_failed_names = [f["tool"] for f in actual_failed]

        assert sorted(actual_succeeded) == sorted(expected_succeeded), (
            f"Succeeded tools mismatch.\n"
            f"Expected: {sorted(expected_succeeded)}\n"
            f"Actual: {sorted(actual_succeeded)}"
        )

        assert sorted(actual_failed_names) == sorted(expected_failed_names), (
            f"Failed tools mismatch.\n"
            f"Expected: {sorted(expected_failed_names)}\n"
            f"Actual: {sorted(actual_failed_names)}"
        )

        # 4. Each failed tool entry has an error description
        for failed_entry in actual_failed:
            assert "error" in failed_entry, (
                f"Failed tool '{failed_entry['tool']}' missing 'error' field"
            )
            assert failed_entry["error"], (
                f"Failed tool '{failed_entry['tool']}' has empty error description"
            )

    @given(
        num_tools=st.integers(min_value=2, max_value=5),
        failure_index=st.integers(min_value=0, max_value=4),
    )
    @settings(max_examples=100)
    def test_failure_does_not_abort_remaining_tools(
        self,
        num_tools: int,
        failure_index: int,
    ):
        """**Validates: Requirements 6.1, 6.3**

        When a tool fails at any position in the tool list, all subsequent
        tools are still executed (the loop continues past the failure).
        """
        # Ensure failure_index is within bounds
        assume(failure_index < num_tools)

        # Create tool configs: one tool fails, all others succeed
        tool_configs = []
        for i in range(num_tools):
            name = f"tool_{chr(ord('a') + i)}"
            should_succeed = i != failure_index
            tool_configs.append((name, should_succeed))

        failing_tool_name = tool_configs[failure_index][0]
        error_types = {failing_tool_name: "RuntimeError"}

        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _make_tool_use_response(tool_configs),
            _make_end_turn_response(),
        ]

        with patch(
            "src.agent.with_retry",
            side_effect=lambda func, *args, **kwargs: func(*args),
        ):
            agent = _create_agent_with_tools(mock_client, tool_configs, error_types)
            result = agent.run("Test continuation after failure")

        # All tools must have been attempted
        assert len(result["tool_calls"]) == num_tools, (
            f"Expected {num_tools} tool calls but got {len(result['tool_calls'])}. "
            f"Agent aborted after failure at index {failure_index}."
        )

        # Exactly one tool should have failed
        assert len(result["tool_outcomes"]["failed"]) == 1
        assert result["tool_outcomes"]["failed"][0]["tool"] == failing_tool_name

        # All other tools should have succeeded
        assert len(result["tool_outcomes"]["succeeded"]) == num_tools - 1

        # Tools after the failure should still be in the succeeded list
        for i in range(failure_index + 1, num_tools):
            tool_name = tool_configs[i][0]
            assert tool_name in result["tool_outcomes"]["succeeded"], (
                f"Tool '{tool_name}' (index {i}, after failure at {failure_index}) "
                f"was not in succeeded list — agent may have aborted."
            )

    @given(
        num_tools=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100)
    def test_all_tools_fail_still_returns_response(
        self,
        num_tools: int,
    ):
        """**Validates: Requirements 6.1, 6.3**

        When ALL tools fail, the agent still returns a response with failure
        metadata rather than raising an unhandled exception.
        """
        # All tools fail
        tool_configs = [(f"tool_{chr(ord('a') + i)}", False) for i in range(num_tools)]
        error_types = {name: "RuntimeError" for name, _ in tool_configs}

        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _make_tool_use_response(tool_configs),
            _make_end_turn_response(),
        ]

        with patch(
            "src.agent.with_retry",
            side_effect=lambda func, *args, **kwargs: func(*args),
        ):
            agent = _create_agent_with_tools(mock_client, tool_configs, error_types)
            # Should NOT raise an exception
            result = agent.run("Test all tools fail")

        # All tools attempted
        assert len(result["tool_calls"]) == num_tools

        # No succeeded tools
        assert result["tool_outcomes"]["succeeded"] == []

        # All tools in failed list
        assert len(result["tool_outcomes"]["failed"]) == num_tools
        failed_names = [f["tool"] for f in result["tool_outcomes"]["failed"]]
        expected_names = [name for name, _ in tool_configs]
        assert sorted(failed_names) == sorted(expected_names)

        # Each failed entry has error info
        for entry in result["tool_outcomes"]["failed"]:
            assert entry["error"]
            assert entry["error_type"]
