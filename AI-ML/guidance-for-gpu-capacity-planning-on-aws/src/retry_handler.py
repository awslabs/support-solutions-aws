# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Retry handler with exponential backoff for transient AWS errors.

Provides a standalone `with_retry` function that wraps callable execution
with configurable retry logic. Designed to be used by the agent loop so
that the final exception can be caught for graceful degradation.

Retryable errors:
    - ThrottlingException (botocore ClientError)
    - TooManyRequestsException (botocore ClientError)
    - HTTP 429 (botocore ClientError)
    - ConnectionError
    - TimeoutError
    - ReadTimeoutError (botocore/urllib3)

Non-retryable errors (raised immediately):
    - ClientError with 4xx status (except 429)
    - ValidationError / ParamValidationError
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, TypeVar

try:
    from botocore.exceptions import (
        ClientError,
        ConnectionError as BotocoreConnectionError,
        ReadTimeoutError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ParamValidationError,
    )
except ImportError:
    # Fallback for environments without botocore
    ClientError = Exception  # type: ignore[misc, assignment]
    BotocoreConnectionError = Exception  # type: ignore[misc, assignment]
    ReadTimeoutError = Exception  # type: ignore[misc, assignment]
    ConnectTimeoutError = Exception  # type: ignore[misc, assignment]
    EndpointConnectionError = Exception  # type: ignore[misc, assignment]
    ParamValidationError = Exception  # type: ignore[misc, assignment]

T = TypeVar("T")


def _is_retryable(exc: BaseException) -> bool:
    """Determine if an exception is retryable.

    Returns True for transient errors that may succeed on retry.
    Returns False for client errors that will never succeed.
    """
    # Built-in connection and timeout errors
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    # Botocore-specific timeout/connection errors
    if isinstance(
        exc, (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError)
    ):
        return True

    # BotocoreConnectionError (distinct from built-in ConnectionError)
    if isinstance(exc, BotocoreConnectionError):
        return True

    # botocore ClientError — check HTTP status and error code
    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)

        # Throttling exceptions are retryable
        if error_code in (
            "ThrottlingException",
            "TooManyRequestsException",
            "Throttling",
            "RequestThrottled",
            "ProvisionedThroughputExceededException",
        ):
            return True

        # HTTP 429 is retryable
        if http_status == 429:
            return True

        # Other 4xx errors are NOT retryable
        if 400 <= http_status < 500:
            return False

        # 5xx server errors are retryable
        if http_status >= 500:
            return True

        return False

    # ValidationError / ParamValidationError — never retry
    if isinstance(exc, (ParamValidationError, ValueError)):
        return False

    # Unknown exceptions — don't retry by default
    return False


def _compute_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter_max: float,
) -> float:
    """Compute delay for a given retry attempt.

    Formula: min(base_delay * 2^attempt + random(0, jitter_max), max_delay)
    """
    exponential = base_delay * (2**attempt)
    jitter = random.uniform(0, jitter_max)  # nosec B311 - non-cryptographic jitter for retry backoff
    return min(exponential + jitter, max_delay)


def with_retry(
    func: Callable[..., T],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    jitter_max: float = 0.5,
    **kwargs: Any,
) -> T:
    """Execute func with exponential backoff retry on transient errors.

    Args:
        func: Callable to execute.
        *args: Positional arguments passed to func.
        max_attempts: Maximum total attempts (1 initial + retries). Default 3.
        base_delay: Base delay in seconds for backoff calculation. Default 1.0.
        max_delay: Maximum delay cap in seconds. Default 8.0.
        jitter_max: Maximum random jitter added to delay. Default 0.5.
        **kwargs: Keyword arguments passed to func.

    Returns:
        The return value of func on success.

    Raises:
        The last exception encountered if all attempts are exhausted.
        Non-retryable exceptions are raised immediately without retry.
    """
    last_exception: BaseException | None = None

    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exception = exc

            # If not retryable, raise immediately
            if not _is_retryable(exc):
                raise

            # If this was the last attempt, raise
            if attempt >= max_attempts - 1:
                raise

            # Sleep with exponential backoff before retrying
            delay = _compute_delay(attempt, base_delay, max_delay, jitter_max)
            time.sleep(delay)

    # Should not reach here, but satisfy type checker
    raise last_exception  # type: ignore[misc]
