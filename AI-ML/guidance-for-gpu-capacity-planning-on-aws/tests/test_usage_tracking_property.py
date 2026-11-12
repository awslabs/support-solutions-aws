# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for usage tracking log structure.

Feature: production-observability-resilience
Property 13: Usage Tracking Log Structure

**Validates: Requirements 11.2, 11.3**

For any completed invocation, the emitted usage tracking log SHALL be a valid
JSON object with event type "usage_tracking" and SHALL contain all required
fields (caller_id, input_tokens, output_tokens, total_tokens, tool_call_count,
model_id, request_timestamp) with request_timestamp in ISO 8601 UTC format.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
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

from src.lambda_handler import (  # noqa: E402
    _emit_usage_tracking_log,
    _stable_caller_hash,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for caller IDs (IAM ARNs, API key IDs, Cognito subs, or "anonymous")
caller_id_st = st.one_of(
    # IAM ARN format
    st.from_regex(
        r"arn:aws:iam::\d{12}:(user|role)/[a-zA-Z0-9_+=,.@-]+",
        fullmatch=True,
    ),
    # API key ID format
    st.from_regex(r"[a-z0-9]{10,20}", fullmatch=True),
    # Cognito sub (UUID format)
    st.from_regex(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        fullmatch=True,
    ),
    # Anonymous
    st.just("anonymous"),
    # Arbitrary non-empty strings
    st.text(min_size=1, max_size=200).filter(lambda s: s.strip() != ""),
)

# Strategy for token counts (non-negative integers)
token_count_st = st.integers(min_value=0, max_value=1_000_000)

# Strategy for tool call counts
tool_call_count_st = st.integers(min_value=0, max_value=50)

# Strategy for model IDs
model_id_st = st.one_of(
    st.sampled_from(
        [
            "us.openai.gpt-4o-2024-11-20-v1:0",
            "anthropic.claude-3-sonnet-20240229-v1:0",
            "us.meta.llama3-70b-instruct-v1:0",
            "amazon.titan-text-express-v1",
        ]
    ),
    st.text(min_size=1, max_size=100).filter(lambda s: s.strip() != ""),
)

# Strategy for ISO 8601 UTC timestamps
timestamp_st = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
).map(lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

# ISO 8601 UTC pattern for validation
ISO_8601_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Required fields in the usage tracking log.
# A security review replaced the raw ``caller_id`` field
# with ``caller_id_hash`` + ``caller_type`` so log lines no longer leak
# IAM ARNs / API key IDs to anyone with log-read access.
REQUIRED_FIELDS = {
    "event",
    "caller_id_hash",
    "caller_type",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    # Bedrock Converse prompt-cache tokens landed with the prompt
    # caching feature; both default to 0 when caching is off or the
    # model doesn't support it (see src/agent.py). Added here so the
    # fixed-schema guard enforces their presence going forward.
    "cache_read_tokens",
    "cache_write_tokens",
    "tool_call_count",
    "model_id",
    "request_timestamp",
}


# ---------------------------------------------------------------------------
# Property 13: Usage Tracking Log Structure
# ---------------------------------------------------------------------------


class TestUsageTrackingLogStructure:
    """Property 13: Usage Tracking Log Structure.

    Feature: production-observability-resilience
    Property 13: Usage Tracking Log Structure

    **Validates: Requirements 11.2, 11.3**

    For any completed invocation, the emitted usage tracking log SHALL be a
    valid JSON object with event type "usage_tracking" and SHALL contain all
    required fields (caller_id, input_tokens, output_tokens, total_tokens,
    tool_call_count, model_id, request_timestamp) with request_timestamp in
    ISO 8601 UTC format.
    """

    @given(
        caller_id=caller_id_st,
        input_tokens=token_count_st,
        output_tokens=token_count_st,
        tool_call_count=tool_call_count_st,
        model_id=model_id_st,
        request_timestamp=timestamp_st,
    )
    @settings(max_examples=100)
    def test_usage_tracking_log_is_valid_json_with_all_required_fields(
        self,
        caller_id: str,
        input_tokens: int,
        output_tokens: int,
        tool_call_count: int,
        model_id: str,
        request_timestamp: str,
    ):
        """**Validates: Requirements 11.2, 11.3**

        For any random invocation data (caller_id, token counts, tool call
        counts, model_id, timestamps), the emitted usage tracking log is
        valid JSON with event="usage_tracking" and all required fields present.
        """
        # Build mock result matching the structure expected by _emit_usage_tracking_log
        tool_calls_list = [{"name": f"tool_{i}"} for i in range(tool_call_count)]
        mock_result = {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "tool_calls": tool_calls_list,
        }

        # Capture log output
        captured_logs: list[str] = []

        class LogCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_logs.append(record.getMessage())

        log_handler = LogCapture()
        log_handler.setLevel(logging.INFO)

        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            _emit_usage_tracking_log(
                caller_id=caller_id,
                caller_type="iam",  # fixed for schema-level tests; H5
                result=mock_result,
                model_id=model_id,
                request_timestamp=request_timestamp,
            )
        finally:
            root_logger.removeHandler(log_handler)

        # Find the usage_tracking log entry
        usage_log = None
        for log_msg in captured_logs:
            try:
                parsed = json.loads(log_msg)
                if parsed.get("event") == "usage_tracking":
                    usage_log = parsed
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        # Assert: log entry was emitted and is valid JSON
        assert usage_log is not None, (
            f"No usage_tracking log entry found. Captured logs: {captured_logs}"
        )

        # Assert: event type is "usage_tracking"
        assert usage_log["event"] == "usage_tracking", (
            f"Expected event='usage_tracking', got event='{usage_log.get('event')}'"
        )

        # Assert: all required fields are present
        missing_fields = REQUIRED_FIELDS - set(usage_log.keys())
        assert not missing_fields, (
            f"Usage tracking log is missing required fields: {missing_fields}. "
            f"Present keys: {sorted(usage_log.keys())}"
        )

        # Assert: field values match inputs. H5 replaces the raw
        # caller_id field with caller_id_hash + caller_type.
        assert usage_log["caller_id_hash"] == _stable_caller_hash(caller_id)
        assert usage_log["caller_type"] == "iam"
        assert usage_log["input_tokens"] == input_tokens
        assert usage_log["output_tokens"] == output_tokens
        assert usage_log["total_tokens"] == input_tokens + output_tokens
        assert usage_log["tool_call_count"] == tool_call_count
        assert usage_log["model_id"] == model_id
        assert usage_log["request_timestamp"] == request_timestamp

    @given(
        caller_id=caller_id_st,
        input_tokens=token_count_st,
        output_tokens=token_count_st,
        tool_call_count=tool_call_count_st,
        model_id=model_id_st,
        request_timestamp=timestamp_st,
    )
    @settings(max_examples=100)
    def test_request_timestamp_is_iso_8601_utc_format(
        self,
        caller_id: str,
        input_tokens: int,
        output_tokens: int,
        tool_call_count: int,
        model_id: str,
        request_timestamp: str,
    ):
        """**Validates: Requirements 11.2, 11.3**

        For any invocation, the request_timestamp field in the usage tracking
        log SHALL be in ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ).
        """
        tool_calls_list = [{"name": f"tool_{i}"} for i in range(tool_call_count)]
        mock_result = {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "tool_calls": tool_calls_list,
        }

        captured_logs: list[str] = []

        class LogCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_logs.append(record.getMessage())

        log_handler = LogCapture()
        log_handler.setLevel(logging.INFO)

        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            _emit_usage_tracking_log(
                caller_id=caller_id,
                caller_type="iam",  # fixed for schema-level tests; H5
                result=mock_result,
                model_id=model_id,
                request_timestamp=request_timestamp,
            )
        finally:
            root_logger.removeHandler(log_handler)

        # Find the usage_tracking log entry
        usage_log = None
        for log_msg in captured_logs:
            try:
                parsed = json.loads(log_msg)
                if parsed.get("event") == "usage_tracking":
                    usage_log = parsed
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        assert usage_log is not None, "No usage_tracking log entry found"

        # Verify request_timestamp matches ISO 8601 UTC format
        ts = usage_log["request_timestamp"]
        assert ISO_8601_UTC_PATTERN.match(ts), (
            f"request_timestamp '{ts}' does not match ISO 8601 UTC format "
            f"(YYYY-MM-DDTHH:MM:SSZ)"
        )

        # Verify it's a parseable datetime
        parsed_dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        assert parsed_dt is not None

    @given(
        caller_id=caller_id_st,
        input_tokens=token_count_st,
        output_tokens=token_count_st,
        model_id=model_id_st,
        request_timestamp=timestamp_st,
    )
    @settings(max_examples=100)
    def test_total_tokens_equals_sum_of_input_and_output(
        self,
        caller_id: str,
        input_tokens: int,
        output_tokens: int,
        model_id: str,
        request_timestamp: str,
    ):
        """**Validates: Requirements 11.2, 11.3**

        For any invocation, total_tokens SHALL equal input_tokens + output_tokens.
        """
        mock_result = {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "tool_calls": [],
        }

        captured_logs: list[str] = []

        class LogCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_logs.append(record.getMessage())

        log_handler = LogCapture()
        log_handler.setLevel(logging.INFO)

        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            _emit_usage_tracking_log(
                caller_id=caller_id,
                caller_type="iam",  # fixed for schema-level tests; H5
                result=mock_result,
                model_id=model_id,
                request_timestamp=request_timestamp,
            )
        finally:
            root_logger.removeHandler(log_handler)

        # Find the usage_tracking log entry
        usage_log = None
        for log_msg in captured_logs:
            try:
                parsed = json.loads(log_msg)
                if parsed.get("event") == "usage_tracking":
                    usage_log = parsed
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        assert usage_log is not None, "No usage_tracking log entry found"

        # Verify total_tokens is the exact sum
        assert usage_log["total_tokens"] == input_tokens + output_tokens, (
            f"total_tokens ({usage_log['total_tokens']}) != "
            f"input_tokens ({input_tokens}) + output_tokens ({output_tokens})"
        )

    @given(
        caller_id=caller_id_st,
        input_tokens=token_count_st,
        output_tokens=token_count_st,
        tool_call_count=tool_call_count_st,
        model_id=model_id_st,
        request_timestamp=timestamp_st,
    )
    @settings(max_examples=100)
    def test_log_schema_field_names_are_fixed(
        self,
        caller_id: str,
        input_tokens: int,
        output_tokens: int,
        tool_call_count: int,
        model_id: str,
        request_timestamp: str,
    ):
        """**Validates: Requirements 11.3**

        The structured log entry SHALL use a fixed JSON schema with field names
        that remain unchanged across deployments to enable CloudWatch Logs
        Insights queries for aggregation.
        """
        tool_calls_list = [{"name": f"tool_{i}"} for i in range(tool_call_count)]
        mock_result = {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "tool_calls": tool_calls_list,
        }

        captured_logs: list[str] = []

        class LogCapture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_logs.append(record.getMessage())

        log_handler = LogCapture()
        log_handler.setLevel(logging.INFO)

        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            _emit_usage_tracking_log(
                caller_id=caller_id,
                caller_type="iam",  # fixed for schema-level tests; H5
                result=mock_result,
                model_id=model_id,
                request_timestamp=request_timestamp,
            )
        finally:
            root_logger.removeHandler(log_handler)

        # Find the usage_tracking log entry
        usage_log = None
        for log_msg in captured_logs:
            try:
                parsed = json.loads(log_msg)
                if parsed.get("event") == "usage_tracking":
                    usage_log = parsed
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        assert usage_log is not None, "No usage_tracking log entry found"

        # Verify the exact set of field names matches the fixed schema
        assert set(usage_log.keys()) == REQUIRED_FIELDS, (
            f"Log schema fields {sorted(usage_log.keys())} do not match "
            f"expected fixed schema {sorted(REQUIRED_FIELDS)}"
        )
