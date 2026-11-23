# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared pytest fixtures for the Capacity Decision Agent test suite."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_bedrock_client() -> MagicMock:
    """A MagicMock of boto3 bedrock-runtime client with a converse() method.

    Usage in tests:
        mock_bedrock_client.converse.return_value = { ... }
    """
    client = MagicMock()
    client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Mock response"}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 50},
    }
    return client


@pytest.fixture
def mock_lambda_event():
    """Factory fixture that creates API Gateway proxy events with configurable body.

    Usage in tests:
        event = mock_lambda_event({"query": "What GPU instances are available?"})
        event = mock_lambda_event(body_string='invalid json')
        event = mock_lambda_event()  # empty body
    """

    def _make_event(
        body: dict[str, Any] | None = None,
        body_string: str | None = None,
    ) -> dict[str, Any]:
        """Create an API Gateway proxy event.

        Args:
            body: Dict to be JSON-encoded as the event body.
            body_string: Raw string body (overrides body dict if provided).

        Returns:
            API Gateway proxy event dict.
        """
        if body_string is not None:
            encoded_body = body_string
        elif body is not None:
            encoded_body = json.dumps(body)
        else:
            encoded_body = "{}"

        return {
            "httpMethod": "POST",
            "path": "/query",
            "headers": {
                "Content-Type": "application/json",
            },
            "body": encoded_body,
            "isBase64Encoded": False,
            "requestContext": {
                "requestId": "test-request-id-12345",
                "stage": "prod",
            },
        }

    return _make_event


@pytest.fixture
def mock_lambda_context() -> MagicMock:
    """A mock Lambda context object with standard attributes.

    Provides:
        - aws_request_id: a test correlation ID
        - function_name: test function name
        - get_remaining_time_in_millis(): returns 30000 (30 seconds)
    """
    context = MagicMock()
    context.aws_request_id = "test-request-id-abc123"
    context.function_name = "CapacityAgentFunction"
    context.memory_limit_in_mb = 256
    context.invoked_function_arn = (
        "arn:aws:lambda:us-west-2:123456789012:function:CapacityAgentFunction"
    )
    context.get_remaining_time_in_millis.return_value = 30000
    return context
