# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for tool failure error result structure.

Feature: production-observability-resilience
Property 8: Tool Failure Error Result Structure

**Validates: Requirements 6.2**

For any NON-RETRYABLE tool failure (any tool name, any non-retryable
exception type), the error result passed to Bedrock SHALL contain the tool
name, the error type (exception class name), and a human-readable error
description.

C2 note: retryable exceptions (throttling, 5xx, connection/timeout errors)
now propagate out of ``_execute_tool`` so the outer ``with_retry`` wrapper
can back off. The final structured error dict is built inside ``agent.run``
after retries are exhausted. Non-retryable exceptions are still wrapped
inside ``_execute_tool``, preserving the model-facing contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for tool names: printable strings of reasonable length
tool_name_st = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P"), whitelist_characters="_-"
    ),
    min_size=1,
    max_size=50,
).filter(lambda s: s.strip() != "")

# Strategy for exception messages
exception_message_st = st.text(min_size=0, max_size=200)

# Strategy for NON-retryable exception types. ConnectionError and TimeoutError
# are retryable (see src/retry_handler.py::_is_retryable) and now propagate
# out of _execute_tool, so they're excluded from the wrapping property.
# Retryable exceptions are validated separately in
# test_agent.py::TestAgentRetryIntegration.
exception_type_st = st.sampled_from(
    [
        ValueError,
        RuntimeError,
        TypeError,
        KeyError,
        IOError,
        OSError,
        AttributeError,
        IndexError,
        ZeroDivisionError,
        PermissionError,
        FileNotFoundError,
        NotImplementedError,
    ]
)


# ---------------------------------------------------------------------------
# Property test: Tool Failure Error Result Structure
# ---------------------------------------------------------------------------


class TestToolFailureErrorResultStructure:
    """Property 8: Tool Failure Error Result Structure.

    Feature: production-observability-resilience
    Property 8: Tool Failure Error Result Structure

    **Validates: Requirements 6.2**

    For any tool failure (any tool name, any exception type), the error result
    passed to Bedrock SHALL contain the tool name, the error type (exception
    class name), and a human-readable error description.
    """

    @given(
        tool_name=tool_name_st,
        exc_type=exception_type_st,
        exc_message=exception_message_st,
    )
    @settings(max_examples=100)
    def test_error_result_contains_required_fields(
        self,
        tool_name: str,
        exc_type: type,
        exc_message: str,
    ):
        """**Validates: Requirements 6.2**

        When a tool raises an exception, the error result dict returned by
        _execute_tool SHALL contain:
        - "ok": False
        - "error_type": the exception class name
        - "error": a human-readable description including the exception message
        - "tool_name": the name of the tool that failed
        """
        # Create agent with mocked boto3 client
        mock_client = MagicMock()
        with patch("src.agent.boto3.client", return_value=mock_client):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=5,
            )

        # Register a tool that raises the generated exception
        def failing_handler(**kwargs):
            raise exc_type(exc_message)

        agent.register_tool(
            name=tool_name,
            description=f"Test tool that raises {exc_type.__name__}",
            input_schema={"type": "object", "properties": {}},
            handler=failing_handler,
        )

        # Execute the tool
        result = agent._execute_tool(tool_name, {})

        # Verify the error result structure
        assert result["ok"] is False, (
            f"Expected 'ok' to be False for a failed tool, got {result['ok']}"
        )

        assert "error_type" in result, "Error result must contain 'error_type' field"
        assert result["error_type"] == exc_type.__name__, (
            f"Expected error_type '{exc_type.__name__}', got '{result['error_type']}'"
        )

        assert "error" in result, (
            "Error result must contain 'error' field with human-readable description"
        )
        # The error field should be a human-readable string
        assert isinstance(result["error"], str), (
            f"Expected 'error' to be a string, got {type(result['error'])}"
        )
        # The error description should include the exception class name
        assert exc_type.__name__ in result["error"], (
            f"Error description should include exception class name "
            f"'{exc_type.__name__}', got: '{result['error']}'"
        )
        # The error description should include the exception message content.
        # Note: Some exceptions like KeyError use repr() for their str() output,
        # so we check that the string representation of the raised exception
        # appears in the error field (which uses f"{type(exc).__name__}: {exc}").
        if exc_message:
            expected_exc_str = str(exc_type(exc_message))
            assert expected_exc_str in result["error"], (
                f"Error description should include exception string representation "
                f"'{expected_exc_str}', got: '{result['error']}'"
            )

        assert "tool_name" in result, "Error result must contain 'tool_name' field"
        assert result["tool_name"] == tool_name, (
            f"Expected tool_name '{tool_name}', got '{result['tool_name']}'"
        )

    @given(
        tool_name=tool_name_st,
        exc_type=exception_type_st,
        exc_message=exception_message_st,
    )
    @settings(max_examples=100)
    def test_error_result_does_not_propagate_exception(
        self,
        tool_name: str,
        exc_type: type,
        exc_message: str,
    ):
        """**Validates: Requirements 6.2**

        When a tool raises any exception, _execute_tool SHALL catch it and
        return a structured error dict rather than propagating the exception.
        """
        # Create agent with mocked boto3 client
        mock_client = MagicMock()
        with patch("src.agent.boto3.client", return_value=mock_client):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=5,
            )

        # Register a tool that raises the generated exception
        def failing_handler(**kwargs):
            raise exc_type(exc_message)

        agent.register_tool(
            name=tool_name,
            description=f"Test tool that raises {exc_type.__name__}",
            input_schema={"type": "object", "properties": {}},
            handler=failing_handler,
        )

        # Execute the tool — should NOT raise
        try:
            result = agent._execute_tool(tool_name, {})
        except Exception as e:
            raise AssertionError(
                f"_execute_tool should not propagate exceptions, "
                f"but raised {type(e).__name__}: {e}"
            )

        # Result should be a dict with error info
        assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
        assert result["ok"] is False

    @given(
        tool_name=tool_name_st,
    )
    @settings(max_examples=100)
    def test_unknown_tool_error_result_structure(
        self,
        tool_name: str,
    ):
        """**Validates: Requirements 6.2**

        When a tool name is not registered, _execute_tool SHALL return an
        error result with the tool name, error type "UnknownToolError", and
        a human-readable description.
        """
        # Create agent with mocked boto3 client (no tools registered)
        mock_client = MagicMock()
        with patch("src.agent.boto3.client", return_value=mock_client):
            agent = CapacityDecisionAgent(
                model_id="test-model",
                region="us-west-2",
                max_iterations=5,
            )

        # Execute a tool that doesn't exist
        result = agent._execute_tool(tool_name, {})

        # Verify the error result structure
        assert result["ok"] is False, (
            f"Expected 'ok' to be False for unknown tool, got {result['ok']}"
        )
        assert result["error_type"] == "UnknownToolError", (
            f"Expected error_type 'UnknownToolError', got '{result['error_type']}'"
        )
        assert "error" in result, "Error result must contain 'error' field"
        assert isinstance(result["error"], str), (
            f"Expected 'error' to be a string, got {type(result['error'])}"
        )
        assert tool_name in result["error"], (
            f"Error description should mention the tool name '{tool_name}'"
        )
        assert result["tool_name"] == tool_name, (
            f"Expected tool_name '{tool_name}', got '{result['tool_name']}'"
        )
