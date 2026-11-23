# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for src/retry_handler.py.

Feature: production-observability-resilience

Properties tested:
- Property 3: Retry Behavior for Retryable Errors
- Property 4: Retry Delay Calculation Bounds
- Property 5: Non-Retryable Errors Bypass Retry
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from botocore.exceptions import ClientError, ParamValidationError

from src.retry_handler import with_retry, _compute_delay


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for retryable errors
retryable_error_st = st.sampled_from(
    [
        lambda: ClientError(
            {
                "Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"},
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            "TestOperation",
        ),
        lambda: ClientError(
            {
                "Error": {
                    "Code": "TooManyRequestsException",
                    "Message": "Too many requests",
                },
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            "TestOperation",
        ),
        lambda: ClientError(
            {
                "Error": {"Code": "SomeCode", "Message": "HTTP 429"},
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            "TestOperation",
        ),
        lambda: TimeoutError("connection timed out"),
        lambda: ConnectionError("connection reset"),
    ]
)

# Strategy for success result values
success_result_st = st.one_of(
    st.text(min_size=1, max_size=50),
    st.integers(),
    st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), max_size=5),
    st.lists(st.integers(), max_size=5),
)

# 4xx status codes excluding 429 (which is retryable)
non_retryable_4xx_status_st = st.integers(min_value=400, max_value=499).filter(
    lambda x: x != 429
)

# Error codes commonly associated with 4xx client errors
client_error_codes_st = st.sampled_from(
    [
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "InvalidParameterException",
        "InvalidRequestException",
        "ConflictException",
        "ServiceQuotaExceededException",
        "UnauthorizedException",
        "ForbiddenException",
        "MethodNotAllowedException",
        "BadRequestException",
        "MalformedQueryString",
        "InvalidAction",
        "MissingParameter",
    ]
)


# 4xx status codes excluding 429 (which is retryable)
non_retryable_4xx_status_st = st.integers(min_value=400, max_value=499).filter(
    lambda x: x != 429
)

# Error codes commonly associated with 4xx client errors
client_error_codes_st = st.sampled_from(
    [
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "InvalidParameterException",
        "InvalidRequestException",
        "ConflictException",
        "ServiceQuotaExceededException",
        "UnauthorizedException",
        "ForbiddenException",
        "MethodNotAllowedException",
        "BadRequestException",
        "MalformedQueryString",
        "InvalidAction",
        "MissingParameter",
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, http_status: int) -> ClientError:
    """Create a botocore ClientError with given code and HTTP status."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"Test error: {code}"},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        },
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# Property 3: Retry Behavior for Retryable Errors
# ---------------------------------------------------------------------------


class TestRetryBehaviorForRetryableErrors:
    """Property 3: Retry Behavior for Retryable Errors.

    Feature: production-observability-resilience
    Property 3: Retry Behavior for Retryable Errors

    *For any* retryable error (ThrottlingException, HTTP 429, TimeoutError,
    ConnectionError) occurring during tool execution, the retry handler SHALL
    make at most 3 total attempts (1 initial + 2 retries), and if any attempt
    succeeds, that result SHALL be returned.

    **Validates: Requirements 4.1, 4.2**
    """

    @given(
        error_factory=retryable_error_st,
        num_errors=st.integers(min_value=0, max_value=2),
        result_value=success_result_st,
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_returns_success_if_any_attempt_succeeds_within_max(
        self,
        mock_sleep: MagicMock,
        error_factory,
        num_errors: int,
        result_value,
    ):
        """**Validates: Requirements 4.1, 4.2**

        When a retryable error occurs fewer than max_attempts times followed
        by a success, the successful result is returned.
        """
        # Build side effects: N errors followed by success
        errors = [error_factory() for _ in range(num_errors)]
        side_effects = errors + [result_value]

        func = MagicMock(side_effect=side_effects)
        result = with_retry(func)

        # The successful result should be returned
        assert result == result_value
        # Total attempts = num_errors + 1 (the successful one)
        assert func.call_count == num_errors + 1

    @given(
        error_factory=retryable_error_st,
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_at_most_3_total_attempts_default(
        self,
        mock_sleep: MagicMock,
        error_factory,
    ):
        """**Validates: Requirements 4.1, 4.2**

        With default max_attempts=3, the retry handler makes at most 3 total
        attempts (1 initial + 2 retries) before raising the last exception.
        """
        # All attempts fail with retryable error
        error = error_factory()
        func = MagicMock(side_effect=error)

        try:
            with_retry(func)
            assert False, "Should have raised an exception"
        except Exception:
            pass

        # Exactly 3 attempts (default max_attempts)
        assert func.call_count == 3

    @given(
        error_factory=retryable_error_st,
        max_attempts=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_at_most_max_attempts_total(
        self,
        mock_sleep: MagicMock,
        error_factory,
        max_attempts: int,
    ):
        """**Validates: Requirements 4.1, 4.2**

        The retry handler makes at most max_attempts total attempts before
        raising the last exception, for any configurable max_attempts value.
        """
        # All attempts fail with retryable error
        error = error_factory()
        func = MagicMock(side_effect=error)

        try:
            with_retry(func, max_attempts=max_attempts)
            assert False, "Should have raised an exception"
        except Exception:
            pass

        # Exactly max_attempts total calls
        assert func.call_count == max_attempts

    @given(
        error_factory=retryable_error_st,
        num_errors=st.integers(min_value=3, max_value=10),
        result_value=success_result_st,
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_raises_when_success_comes_after_max_attempts(
        self,
        mock_sleep: MagicMock,
        error_factory,
        num_errors: int,
        result_value,
    ):
        """**Validates: Requirements 4.1, 4.2**

        When the number of errors before success exceeds max_attempts (default 3),
        the handler raises the last exception without reaching the success.
        """
        # Build side effects: more errors than max_attempts, then success
        errors = [error_factory() for _ in range(num_errors)]
        side_effects = errors + [result_value]

        func = MagicMock(side_effect=side_effects)

        try:
            with_retry(func)
            assert False, "Should have raised an exception"
        except Exception:
            pass

        # Only 3 attempts made (default), not reaching the success
        assert func.call_count == 3

    @given(
        error_factory=retryable_error_st,
        success_at=st.integers(min_value=0, max_value=2),
        result_value=success_result_st,
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_success_at_any_attempt_returns_result(
        self,
        mock_sleep: MagicMock,
        error_factory,
        success_at: int,
        result_value,
    ):
        """**Validates: Requirements 4.1, 4.2**

        If any of the 3 allowed attempts succeeds (attempt 0, 1, or 2),
        that result is returned regardless of which attempt it was.
        """
        # Build side effects: errors until success_at, then success
        errors = [error_factory() for _ in range(success_at)]
        side_effects = errors + [result_value]

        func = MagicMock(side_effect=side_effects)
        result = with_retry(func)

        # The result from the successful attempt is returned
        assert result == result_value
        # The function was called success_at + 1 times
        assert func.call_count == success_at + 1


# ---------------------------------------------------------------------------
# Property 5: Non-Retryable Errors Bypass Retry
# ---------------------------------------------------------------------------


class TestNonRetryableErrorsBypassRetry:
    """Property 5: Non-Retryable Errors Bypass Retry.

    Feature: production-observability-resilience
    Property 5: Non-Retryable Errors Bypass Retry

    *For any* non-retryable error (HTTP 4xx other than 429, ValidationError,
    or other client errors), the retry handler SHALL return the error
    immediately without any retry attempts.

    **Validates: Requirements 4.4**
    """

    @given(
        status_code=non_retryable_4xx_status_st,
        error_code=client_error_codes_st,
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_4xx_client_errors_not_retried(
        self, mock_sleep, status_code: int, error_code: str
    ):
        """**Validates: Requirements 4.4**

        For any 4xx status code (excluding 429), the error is raised
        immediately on the first attempt with no retries and no sleep calls.
        """
        error = _make_client_error(error_code, status_code)
        func = MagicMock(side_effect=error)

        with pytest.raises(ClientError) as exc_info:
            with_retry(func)

        # Error raised immediately — only 1 call attempt
        assert func.call_count == 1, (
            f"Expected 1 call for non-retryable {status_code} error, "
            f"got {func.call_count}"
        )

        # time.sleep should never be called for non-retryable errors
        mock_sleep.assert_not_called()

        # The raised error should be the same one we injected
        assert exc_info.value is error

    @given(
        error_message=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_param_validation_errors_not_retried(self, mock_sleep, error_message: str):
        """**Validates: Requirements 4.4**

        ParamValidationError is raised immediately without retry attempts.
        """
        error = ParamValidationError(report=error_message)
        func = MagicMock(side_effect=error)

        with pytest.raises(ParamValidationError) as exc_info:
            with_retry(func)

        # Error raised immediately — only 1 call attempt
        assert func.call_count == 1, (
            f"Expected 1 call for ParamValidationError, got {func.call_count}"
        )

        # time.sleep should never be called
        mock_sleep.assert_not_called()

        # The raised error should be the same one we injected
        assert exc_info.value is error

    @given(
        error_message=st.text(min_size=1, max_size=100),
    )
    @settings(max_examples=100)
    @patch("src.retry_handler.time.sleep")
    def test_value_errors_not_retried(self, mock_sleep, error_message: str):
        """**Validates: Requirements 4.4**

        ValueError is raised immediately without retry attempts.
        """
        error = ValueError(error_message)
        func = MagicMock(side_effect=error)

        with pytest.raises(ValueError) as exc_info:
            with_retry(func)

        # Error raised immediately — only 1 call attempt
        assert func.call_count == 1, (
            f"Expected 1 call for ValueError, got {func.call_count}"
        )

        # time.sleep should never be called
        mock_sleep.assert_not_called()

        # The raised error should be the same one we injected
        assert exc_info.value is error


# ---------------------------------------------------------------------------
# Property 4: Retry Delay Calculation Bounds
# ---------------------------------------------------------------------------


class TestRetryDelayCalculationBounds:
    """Property 4: Retry Delay Calculation Bounds.

    Feature: production-observability-resilience
    Property 4: Retry Delay Calculation Bounds

    *For any* retry attempt number n (0-indexed), the computed delay SHALL be
    within the range [base_delay * 2^n, base_delay * 2^n + jitter_max] seconds
    and SHALL never exceed max_delay (8 seconds).

    **Validates: Requirements 4.3**
    """

    @given(attempt=st.integers(min_value=0, max_value=10))
    @settings(max_examples=200)
    def test_delay_within_expected_bounds(self, attempt: int):
        """**Validates: Requirements 4.3**

        For any attempt number n, the delay is within
        [base_delay * 2^n, base_delay * 2^n + jitter_max] capped at max_delay.
        """
        base_delay = 1.0
        max_delay = 8.0
        jitter_max = 0.5

        delay = _compute_delay(
            attempt, base_delay=base_delay, max_delay=max_delay, jitter_max=jitter_max
        )

        # The exponential component without jitter
        exponential = base_delay * (2**attempt)

        # Expected lower bound: the exponential part (jitter >= 0), capped at max_delay
        expected_lower = min(exponential, max_delay)

        # Expected upper bound: exponential + max jitter, capped at max_delay
        expected_upper = min(exponential + jitter_max, max_delay)

        assert delay >= expected_lower, (
            f"Attempt {attempt}: delay {delay} < expected lower bound {expected_lower}"
        )
        assert delay <= expected_upper, (
            f"Attempt {attempt}: delay {delay} > expected upper bound {expected_upper}"
        )

    @given(attempt=st.integers(min_value=0, max_value=10))
    @settings(max_examples=200)
    def test_delay_never_exceeds_max_delay(self, attempt: int):
        """**Validates: Requirements 4.3**

        For any attempt number, the delay never exceeds max_delay (8 seconds).
        """
        base_delay = 1.0
        max_delay = 8.0
        jitter_max = 0.5

        delay = _compute_delay(
            attempt, base_delay=base_delay, max_delay=max_delay, jitter_max=jitter_max
        )

        assert delay <= max_delay, (
            f"Attempt {attempt}: delay {delay} exceeds max_delay {max_delay}"
        )

    @given(attempt=st.integers(min_value=0, max_value=10))
    @settings(max_examples=200)
    def test_delay_is_non_negative(self, attempt: int):
        """**Validates: Requirements 4.3**

        For any attempt number, the delay is always non-negative.
        """
        base_delay = 1.0
        max_delay = 8.0
        jitter_max = 0.5

        delay = _compute_delay(
            attempt, base_delay=base_delay, max_delay=max_delay, jitter_max=jitter_max
        )

        assert delay >= 0, f"Attempt {attempt}: delay {delay} is negative"
