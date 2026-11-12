# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for src/retry_handler.py.

Tests the with_retry function covering:
- Successful execution on first attempt
- Retry on retryable errors (ThrottlingException, ConnectionError, TimeoutError)
- Immediate raise on non-retryable errors (4xx ClientError, ValidationError)
- Delay calculation bounds
- Max attempts exhaustion
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    ReadTimeoutError,
    ParamValidationError,
)

from src.retry_handler import with_retry, _is_retryable, _compute_delay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, http_status: int = 400) -> ClientError:
    """Create a botocore ClientError with given code and HTTP status."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"Test error: {code}"},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        },
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# Tests: Successful execution
# ---------------------------------------------------------------------------


class TestWithRetrySuccess:
    """Tests for successful function execution."""

    def test_returns_result_on_first_success(self):
        """Function succeeds on first call — no retries needed."""
        func = MagicMock(return_value="success")
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 1

    def test_passes_args_and_kwargs(self):
        """Positional and keyword arguments are forwarded to the callable."""
        func = MagicMock(return_value="ok")
        result = with_retry(func, "arg1", "arg2", key="value")
        func.assert_called_once_with("arg1", "arg2", key="value")
        assert result == "ok"

    @patch("src.retry_handler.time.sleep")
    def test_returns_result_after_transient_failure(self, mock_sleep):
        """Function fails once with retryable error, then succeeds."""
        throttle_error = _make_client_error("ThrottlingException", 429)
        func = MagicMock(side_effect=[throttle_error, "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2
        mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Retryable errors
# ---------------------------------------------------------------------------


class TestRetryableErrors:
    """Tests for errors that should trigger retries."""

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_throttling_exception(self, mock_sleep):
        """ThrottlingException triggers retry."""
        error = _make_client_error("ThrottlingException", 429)
        func = MagicMock(side_effect=[error, error, "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 3

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_too_many_requests(self, mock_sleep):
        """TooManyRequestsException triggers retry."""
        error = _make_client_error("TooManyRequestsException", 429)
        func = MagicMock(side_effect=[error, "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_http_429(self, mock_sleep):
        """HTTP 429 status code triggers retry regardless of error code."""
        error = _make_client_error("SomeOtherCode", 429)
        func = MagicMock(side_effect=[error, "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_connection_error(self, mock_sleep):
        """Built-in ConnectionError triggers retry."""
        func = MagicMock(side_effect=[ConnectionError("reset"), "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_timeout_error(self, mock_sleep):
        """Built-in TimeoutError triggers retry."""
        func = MagicMock(side_effect=[TimeoutError("timed out"), "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_read_timeout_error(self, mock_sleep):
        """Botocore ReadTimeoutError triggers retry."""
        error = ReadTimeoutError(endpoint_url="https://example.com")
        func = MagicMock(side_effect=[error, "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2

    @patch("src.retry_handler.time.sleep")
    def test_retries_on_server_error_5xx(self, mock_sleep):
        """HTTP 500 server error triggers retry."""
        error = _make_client_error("InternalServerError", 500)
        func = MagicMock(side_effect=[error, "success"])
        result = with_retry(func)
        assert result == "success"
        assert func.call_count == 2


# ---------------------------------------------------------------------------
# Tests: Non-retryable errors
# ---------------------------------------------------------------------------


class TestNonRetryableErrors:
    """Tests for errors that should NOT trigger retries."""

    def test_no_retry_on_client_error_400(self):
        """HTTP 400 Bad Request is not retried."""
        error = _make_client_error("ValidationException", 400)
        func = MagicMock(side_effect=error)
        with pytest.raises(ClientError):
            with_retry(func)
        assert func.call_count == 1

    def test_no_retry_on_client_error_403(self):
        """HTTP 403 Forbidden is not retried."""
        error = _make_client_error("AccessDeniedException", 403)
        func = MagicMock(side_effect=error)
        with pytest.raises(ClientError):
            with_retry(func)
        assert func.call_count == 1

    def test_no_retry_on_client_error_404(self):
        """HTTP 404 Not Found is not retried."""
        error = _make_client_error("ResourceNotFoundException", 404)
        func = MagicMock(side_effect=error)
        with pytest.raises(ClientError):
            with_retry(func)
        assert func.call_count == 1

    def test_no_retry_on_validation_error(self):
        """ParamValidationError is not retried."""
        error = ParamValidationError(report="Invalid parameter")
        func = MagicMock(side_effect=error)
        with pytest.raises(ParamValidationError):
            with_retry(func)
        assert func.call_count == 1

    def test_no_retry_on_value_error(self):
        """ValueError is not retried."""
        func = MagicMock(side_effect=ValueError("bad value"))
        with pytest.raises(ValueError):
            with_retry(func)
        assert func.call_count == 1


# ---------------------------------------------------------------------------
# Tests: Max attempts exhaustion
# ---------------------------------------------------------------------------


class TestMaxAttemptsExhaustion:
    """Tests for behavior when all retry attempts are exhausted."""

    @patch("src.retry_handler.time.sleep")
    def test_raises_last_exception_after_max_attempts(self, mock_sleep):
        """After 3 attempts (default), the last exception is raised."""
        error = _make_client_error("ThrottlingException", 429)
        func = MagicMock(side_effect=error)
        with pytest.raises(ClientError) as exc_info:
            with_retry(func)
        assert "ThrottlingException" in str(exc_info.value)
        assert func.call_count == 3

    @patch("src.retry_handler.time.sleep")
    def test_custom_max_attempts(self, mock_sleep):
        """Custom max_attempts is respected."""
        error = _make_client_error("ThrottlingException", 429)
        func = MagicMock(side_effect=error)
        with pytest.raises(ClientError):
            with_retry(func, max_attempts=5)
        assert func.call_count == 5

    @patch("src.retry_handler.time.sleep")
    def test_max_attempts_1_means_no_retry(self, mock_sleep):
        """max_attempts=1 means only one attempt, no retries."""
        error = _make_client_error("ThrottlingException", 429)
        func = MagicMock(side_effect=error)
        with pytest.raises(ClientError):
            with_retry(func, max_attempts=1)
        assert func.call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Delay calculation
# ---------------------------------------------------------------------------


class TestDelayCalculation:
    """Tests for the _compute_delay function."""

    def test_attempt_0_delay_bounds(self):
        """Attempt 0: delay in [1.0, 1.5] (base_delay * 2^0 + jitter)."""
        for _ in range(100):
            delay = _compute_delay(0, base_delay=1.0, max_delay=8.0, jitter_max=0.5)
            assert 1.0 <= delay <= 1.5

    def test_attempt_1_delay_bounds(self):
        """Attempt 1: delay in [2.0, 2.5] (base_delay * 2^1 + jitter)."""
        for _ in range(100):
            delay = _compute_delay(1, base_delay=1.0, max_delay=8.0, jitter_max=0.5)
            assert 2.0 <= delay <= 2.5

    def test_attempt_2_delay_bounds(self):
        """Attempt 2: delay in [4.0, 4.5] (base_delay * 2^2 + jitter)."""
        for _ in range(100):
            delay = _compute_delay(2, base_delay=1.0, max_delay=8.0, jitter_max=0.5)
            assert 4.0 <= delay <= 4.5

    def test_delay_capped_at_max(self):
        """High attempt numbers are capped at max_delay."""
        for _ in range(100):
            delay = _compute_delay(10, base_delay=1.0, max_delay=8.0, jitter_max=0.5)
            assert delay <= 8.0

    def test_delay_never_negative(self):
        """Delay is always non-negative."""
        for attempt in range(10):
            delay = _compute_delay(
                attempt, base_delay=1.0, max_delay=8.0, jitter_max=0.5
            )
            assert delay >= 0

    @patch("src.retry_handler.time.sleep")
    def test_sleep_called_with_correct_delay_range(self, mock_sleep):
        """Verify time.sleep is called with delay in expected range."""
        error = _make_client_error("ThrottlingException", 429)
        func = MagicMock(side_effect=[error, "success"])
        with_retry(func)
        # First retry (attempt 0): delay should be in [1.0, 1.5]
        sleep_arg = mock_sleep.call_args[0][0]
        assert 1.0 <= sleep_arg <= 1.5


# ---------------------------------------------------------------------------
# Tests: _is_retryable helper
# ---------------------------------------------------------------------------


class TestIsRetryable:
    """Tests for the _is_retryable classification function."""

    def test_connection_error_is_retryable(self):
        assert _is_retryable(ConnectionError("reset")) is True

    def test_timeout_error_is_retryable(self):
        assert _is_retryable(TimeoutError("timed out")) is True

    def test_throttling_client_error_is_retryable(self):
        error = _make_client_error("ThrottlingException", 429)
        assert _is_retryable(error) is True

    def test_too_many_requests_is_retryable(self):
        error = _make_client_error("TooManyRequestsException", 429)
        assert _is_retryable(error) is True

    def test_http_429_is_retryable(self):
        error = _make_client_error("UnknownCode", 429)
        assert _is_retryable(error) is True

    def test_http_500_is_retryable(self):
        error = _make_client_error("InternalServerError", 500)
        assert _is_retryable(error) is True

    def test_http_400_is_not_retryable(self):
        error = _make_client_error("ValidationException", 400)
        assert _is_retryable(error) is False

    def test_http_403_is_not_retryable(self):
        error = _make_client_error("AccessDeniedException", 403)
        assert _is_retryable(error) is False

    def test_value_error_is_not_retryable(self):
        assert _is_retryable(ValueError("bad")) is False

    def test_param_validation_error_is_not_retryable(self):
        error = ParamValidationError(report="Invalid")
        assert _is_retryable(error) is False
