# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for the CapacityDecisionAgent loop.

**Validates: Requirements 2.4**

Property 3: Conversation assembly structure — For any sequence of tool calls
during agent execution, the assembled conversation history SHALL maintain
correctly alternating user/assistant message roles with tool results placed
in the correct position.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for number of tool calls per iteration (1-3 tools per turn)
tools_per_iteration_st = st.integers(min_value=1, max_value=3)

# Strategy for number of tool-use iterations before end_turn (0 means immediate end_turn)
iteration_count_st = st.integers(min_value=0, max_value=5)

# Strategy for tool names
tool_name_st = st.sampled_from(["tool_a", "tool_b", "tool_c"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_use_response(tool_names: list[str], iteration_idx: int) -> dict:
    """Create a Bedrock converse response requesting multiple tool calls."""
    content = [
        {
            "toolUse": {
                "toolUseId": f"tu-{iteration_idx}-{i}",
                "name": name,
                "input": {"param": f"value_{iteration_idx}_{i}"},
            }
        }
        for i, name in enumerate(tool_names)
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
                "content": [{"text": "Final answer."}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 60, "outputTokens": 30},
    }


def _create_agent_with_mock(mock_client: MagicMock) -> CapacityDecisionAgent:
    """Create a CapacityDecisionAgent with a mocked boto3 client."""
    with patch("src.agent.boto3.client", return_value=mock_client):
        agent = CapacityDecisionAgent(
            model_id="test-model",
            region="us-west-2",
            max_iterations=10,
        )
    # Register tools that the agent can dispatch to
    for name in ["tool_a", "tool_b", "tool_c"]:
        agent.register_tool(
            name=name,
            description=f"Test tool {name}",
            input_schema={
                "type": "object",
                "properties": {"param": {"type": "string"}},
            },
            handler=lambda **kwargs: {"result": "ok", "received": kwargs},
        )
    return agent


# ---------------------------------------------------------------------------
# Property test: Conversation assembly structure
# ---------------------------------------------------------------------------


class TestConversationAssemblyStructure:
    """Property 3: Conversation assembly structure.

    **Validates: Requirements 2.4**

    For any sequence of tool calls during agent execution, the assembled
    conversation history SHALL maintain correctly alternating user/assistant
    message roles with tool results placed in the correct position.
    """

    @given(
        tool_iterations=st.lists(
            st.lists(tool_name_st, min_size=1, max_size=3),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(max_examples=50)
    def test_conversation_alternates_roles_correctly(
        self, tool_iterations: list[list[str]]
    ):
        """**Validates: Requirements 2.4**

        The conversation history always alternates user/assistant roles,
        starting with a user message. Tool results appear as user messages
        immediately following the assistant's tool_use message.
        """
        # Build the sequence of mock responses:
        # For each tool iteration, return a tool_use response, then end with end_turn
        mock_client = MagicMock()
        responses = []
        for idx, tools in enumerate(tool_iterations):
            responses.append(_make_tool_use_response(tools, idx))
        responses.append(_make_end_turn_response())
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client)
        result = agent.run("Test query")

        messages = result["messages"]

        # Property 1: Conversation starts with a user message
        assert len(messages) >= 1, "Conversation must have at least one message"
        assert messages[0]["role"] == "user", (
            f"First message must be 'user', got '{messages[0]['role']}'"
        )

        # Property 2: Messages alternate correctly between user and assistant
        for i in range(1, len(messages)):
            prev_role = messages[i - 1]["role"]
            curr_role = messages[i]["role"]
            # After a user message, next must be assistant
            # After an assistant message, next must be user
            if prev_role == "user":
                assert curr_role == "assistant", (
                    f"Message {i}: expected 'assistant' after 'user', got '{curr_role}'"
                )
            elif prev_role == "assistant":
                assert curr_role == "user", (
                    f"Message {i}: expected 'user' after 'assistant', got '{curr_role}'"
                )

        # Property 3: Tool results are placed as user messages following
        # assistant tool_use messages
        for i in range(len(messages)):
            if messages[i]["role"] == "assistant":
                # Check if this assistant message contains tool_use blocks
                has_tool_use = any(
                    "toolUse" in block for block in messages[i].get("content", [])
                )
                if has_tool_use and i + 1 < len(messages):
                    # The next message must be a user message with toolResult blocks
                    next_msg = messages[i + 1]
                    assert next_msg["role"] == "user", (
                        f"Message {i + 1}: tool results must be in a 'user' message, "
                        f"got '{next_msg['role']}'"
                    )
                    # Verify it contains toolResult blocks
                    has_tool_result = any(
                        "toolResult" in block for block in next_msg.get("content", [])
                    )
                    assert has_tool_result, (
                        f"Message {i + 1}: user message after tool_use must contain "
                        f"toolResult blocks"
                    )

    @given(
        num_iterations=st.integers(min_value=1, max_value=5),
        tools_per_turn=st.integers(min_value=1, max_value=3),
    )
    @settings(max_examples=50)
    def test_tool_result_count_matches_tool_use_count(
        self, num_iterations: int, tools_per_turn: int
    ):
        """**Validates: Requirements 2.4**

        For each assistant message with N tool_use blocks, the following
        user message contains exactly N toolResult blocks.
        """
        # Build uniform tool iterations
        tool_names = ["tool_a", "tool_b", "tool_c"][:tools_per_turn]

        mock_client = MagicMock()
        responses = []
        for idx in range(num_iterations):
            responses.append(_make_tool_use_response(tool_names, idx))
        responses.append(_make_end_turn_response())
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client)
        result = agent.run("Test query")

        messages = result["messages"]

        for i in range(len(messages)):
            if messages[i]["role"] == "assistant":
                tool_use_blocks = [
                    block
                    for block in messages[i].get("content", [])
                    if "toolUse" in block
                ]
                if tool_use_blocks and i + 1 < len(messages):
                    tool_result_blocks = [
                        block
                        for block in messages[i + 1].get("content", [])
                        if "toolResult" in block
                    ]
                    assert len(tool_result_blocks) == len(tool_use_blocks), (
                        f"At message {i}: {len(tool_use_blocks)} tool_use blocks "
                        f"but {len(tool_result_blocks)} toolResult blocks in response"
                    )

    @given(
        tool_iterations=st.lists(
            st.lists(tool_name_st, min_size=1, max_size=3),
            min_size=0,
            max_size=5,
        ),
    )
    @settings(max_examples=50)
    def test_conversation_ends_with_assistant_on_end_turn(
        self, tool_iterations: list[list[str]]
    ):
        """**Validates: Requirements 2.4**

        When the agent loop ends with end_turn, the last message in the
        conversation is an assistant message.
        """
        mock_client = MagicMock()
        responses = []
        for idx, tools in enumerate(tool_iterations):
            responses.append(_make_tool_use_response(tools, idx))
        responses.append(_make_end_turn_response())
        mock_client.converse.side_effect = responses

        agent = _create_agent_with_mock(mock_client)
        result = agent.run("Test query")

        messages = result["messages"]

        # When stop_reason is end_turn, last message should be assistant
        assert result["stop_reason"] == "end_turn"
        assert messages[-1]["role"] == "assistant", (
            f"Last message should be 'assistant' on end_turn, got '{messages[-1]['role']}'"
        )
