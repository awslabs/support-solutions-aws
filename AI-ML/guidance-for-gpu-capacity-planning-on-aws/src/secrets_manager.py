# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Secrets management module for retrieving and caching secrets from AWS Secrets Manager."""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Module-level cache for cold-start optimization.
# Once secrets are fetched successfully, they are cached here for the lifetime
# of the Lambda execution environment.
_secrets_cache: Optional[Dict[str, str]] = None


class SecretsRetrievalError(Exception):
    """Raised when secrets cannot be retrieved after all retry attempts.

    The error message contains the secret identifier but never exposes secret values.
    """

    def __init__(self, secret_id: str, reason: str):
        self.secret_id = secret_id
        self.reason = reason
        super().__init__(f"Failed to retrieve secret '{secret_id}': {reason}")


def get_secrets(secret_arn: str, max_retries: int = 2) -> Dict[str, str]:
    """Retrieve secrets from AWS Secrets Manager with caching and retry.

    On first call, fetches the secret value from Secrets Manager and caches it
    in a module-level variable. Subsequent calls return the cached value without
    making API calls.

    Args:
        secret_arn: The ARN of the secret to retrieve.
        max_retries: Maximum number of retry attempts on failure (default 2).

    Returns:
        A dictionary of secret key-value pairs parsed from the secret string.

    Raises:
        SecretsRetrievalError: If the secret cannot be retrieved after all retry
            attempts. The error includes the secret identifier but never exposes
            secret values.
    """
    global _secrets_cache

    if _secrets_cache is not None:
        return _secrets_cache

    client = boto3.client("secretsmanager")
    last_error: Optional[Exception] = None

    for attempt in range(1 + max_retries):
        try:
            response = client.get_secret_value(SecretId=secret_arn)
            secret_string = response["SecretString"]
            secrets = json.loads(secret_string)
            _secrets_cache = secrets
            return secrets
        except (ClientError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            logger.warning(
                "Secret retrieval attempt %d/%d failed for '%s': %s",
                attempt + 1,
                1 + max_retries,
                secret_arn,
                type(exc).__name__,
            )
            if attempt < max_retries:
                # Brief backoff between retries
                time.sleep(0.5 * (attempt + 1))

    # All attempts exhausted — raise without exposing secret values
    error_reason = type(last_error).__name__ if last_error else "Unknown error"
    raise SecretsRetrievalError(secret_id=secret_arn, reason=error_reason)


def clear_cache() -> None:
    """Clear the secrets cache. Primarily used for testing."""
    global _secrets_cache
    _secrets_cache = None
