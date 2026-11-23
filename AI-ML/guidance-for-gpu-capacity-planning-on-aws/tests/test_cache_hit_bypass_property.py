# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for cache hit bypassing live API.

Feature: production-observability-resilience, Property 15: Cache Hit Bypasses Live API

Property 15: Cache Hit Bypasses Live API — For any tool invocation where a
valid (non-expired) cached result exists in DynamoDB, the tool cache SHALL
return the cached result and the underlying tool handler SHALL NOT be called.

**Validates: Requirements 12.2**
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for tool names (alphanumeric with underscores, like real tool names)
_tool_name_st = st.sampled_from(
    [
        "check_instance_availability",
        "get_sagemaker_pricing",
        "get_sagemaker_quota",
        "get_alternatives",
        "tool_alpha",
        "tool_beta",
    ]
)

# Strategy for tool input parameters (JSON-serializable dicts)
_params_st = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")),
        min_size=1,
        max_size=20,
    ),
    values=st.one_of(
        st.text(max_size=100),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
        st.booleans(),
        st.none(),
    ),
    min_size=0,
    max_size=5,
)

# Strategy for cached results (JSON-serializable dicts representing tool output)
_cached_result_st = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")),
        min_size=1,
        max_size=20,
    ),
    values=st.one_of(
        st.text(max_size=200),
        st.integers(min_value=-10000, max_value=10000),
        st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
        st.booleans(),
        st.none(),
        st.lists(st.text(max_size=50), max_size=5),
    ),
    min_size=1,
    max_size=8,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_use_response(tool_name: str, params: dict) -> dict:
    """Create a Bedrock converse response requesting a single tool call."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tu-cache-test-001",
                            "name": tool_name,
                            "input": params,
                        }
                    }
                ],
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
                "content": [{"text": "Here is the cached result."}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 30, "outputTokens": 15},
    }


# ---------------------------------------------------------------------------
# Property test: Cache Hit Bypasses Live API (Property 15)
# ---------------------------------------------------------------------------


class TestCacheHitBypassesLiveAPI:
    """Property 15: Cache Hit Bypasses Live API.

    Feature: production-observability-resilience, Property 15: Cache Hit Bypasses Live API

    **Validates: Requirements 12.2**

    For any tool invocation where a valid (non-expired) cached result exists,
    the tool cache SHALL return the cached result and the underlying tool
    handler SHALL NOT be called.
    """

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        cached_result=_cached_result_st,
    )
    @settings(max_examples=100)
    def test_tool_handler_not_called_when_cache_hit(
        self,
        tool_name: str,
        params: dict,
        cached_result: dict,
    ):
        """**Validates: Requirements 12.2**

        Generate random cached results and verify the tool handler function
        is NOT called when the cache returns a result.
        """
        # Create a mock tool handler that tracks calls
        mock_handler = MagicMock(return_value={"should": "not be called"})

        # Create a mock ToolCache that returns the cached result
        mock_cache = MagicMock()
        mock_cache.get.return_value = cached_result

        # Create a mock Bedrock client
        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _make_tool_use_response(tool_name, params),
            _make_end_turn_response(),
        ]

        with patch("src.agent.boto3.client", return_value=mock_client):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=10,
                tool_cache=mock_cache,
            )

        # Register the tool with the mock handler
        agent.register_tool(
            name=tool_name,
            description=f"Test tool {tool_name}",
            input_schema={"type": "object", "properties": {}},
            handler=mock_handler,
        )

        agent.run("Test query for cached tool")

        # Property: The tool handler SHALL NOT be called when cache hit occurs
        mock_handler.assert_not_called()

        # Property: The cache.get() was called with the correct tool name and params
        mock_cache.get.assert_called_once_with(tool_name, params)

        # Property: cache.put() should NOT be called (no new result to cache)
        mock_cache.put.assert_not_called()

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        cached_result=_cached_result_st,
    )
    @settings(max_examples=100)
    def test_cached_result_used_in_tool_calls_output(
        self,
        tool_name: str,
        params: dict,
        cached_result: dict,
    ):
        """**Validates: Requirements 12.2**

        Verify that the cached result is used in the tool_calls output
        when a cache hit occurs.
        """
        # Create a mock tool handler
        mock_handler = MagicMock(return_value={"should": "not be called"})

        # Create a mock ToolCache that returns the cached result
        mock_cache = MagicMock()
        mock_cache.get.return_value = cached_result

        # Create a mock Bedrock client
        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _make_tool_use_response(tool_name, params),
            _make_end_turn_response(),
        ]

        with patch("src.agent.boto3.client", return_value=mock_client):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=10,
                tool_cache=mock_cache,
            )

        agent.register_tool(
            name=tool_name,
            description=f"Test tool {tool_name}",
            input_schema={"type": "object", "properties": {}},
            handler=mock_handler,
        )

        result = agent.run("Test query for cached tool")

        # Property: The tool_calls output should contain the cached result
        assert len(result["tool_calls"]) == 1
        call_name, call_args, call_result = result["tool_calls"][0]
        assert call_name == tool_name
        assert call_args == params
        # The result should be {"ok": True, "data": cached_result}
        assert call_result["ok"] is True
        assert call_result["data"] == cached_result

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        cached_result=_cached_result_st,
    )
    @settings(max_examples=100)
    def test_cache_hit_records_success_in_tool_outcomes(
        self,
        tool_name: str,
        params: dict,
        cached_result: dict,
    ):
        """**Validates: Requirements 12.2**

        When a cache hit occurs, the tool outcome SHALL be recorded as
        success in the response metadata.
        """
        mock_handler = MagicMock(return_value={"should": "not be called"})

        mock_cache = MagicMock()
        mock_cache.get.return_value = cached_result

        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _make_tool_use_response(tool_name, params),
            _make_end_turn_response(),
        ]

        with patch("src.agent.boto3.client", return_value=mock_client):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=10,
                tool_cache=mock_cache,
            )

        agent.register_tool(
            name=tool_name,
            description=f"Test tool {tool_name}",
            input_schema={"type": "object", "properties": {}},
            handler=mock_handler,
        )

        result = agent.run("Test query for cached tool")

        # Property: Tool outcome should be recorded as success
        assert tool_name in result["tool_outcomes"]["succeeded"]
        assert all(f["tool"] != tool_name for f in result["tool_outcomes"]["failed"])
