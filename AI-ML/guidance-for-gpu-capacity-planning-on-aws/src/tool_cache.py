# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB-backed tool result cache with configurable TTL.

Provides caching for tool results to reduce redundant AWS API calls.
All DynamoDB operations are wrapped in try/except so that cache failures
never propagate to the caller — the request falls through to the live API.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.6, 12.7
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)

# Maximum DynamoDB item size is 400 KB total, and that includes all
# attribute names + values + DynamoDB's internal per-item overhead. We
# guard against the ``result`` JSON payload alone by sizing 20 KB below
# the hard limit, leaving room for the sibling attributes
# (``cache_key``, ``expires_at``, ``tool_name``, ``tenant_scope``,
# ``created_at``) plus DynamoDB's ~100 bytes of overhead. Anything larger
# would silently trigger a ValidationException at put_item time, which
# the broad except in ``put()`` would swallow.
MAX_ITEM_SIZE_BYTES = 380 * 1024


def _parse_ttl_seconds(value: str | None) -> int:
    """Parse CACHE_TTL_SECONDS env var.

    Valid range: 1-3600 inclusive. Returns 300 for invalid/missing values.
    """
    if value is None:
        return 300
    try:
        parsed = int(value)
        if 1 <= parsed <= 3600:
            return parsed
        return 300
    except (ValueError, TypeError):
        return 300


class ToolCache:
    """DynamoDB-backed cache for tool results.

    Cache key format:
        {tool_name}#{tenant_scope}#{sha256(json.dumps(params, sort_keys=True))}

    ``tenant_scope`` is the AWS account whose data the tool call reads —
    the assumed customer account under cross-account access, or the deploy
    account otherwise. Without this scoping, a warm Lambda container would
    serve tenant A's tool results to tenant B when their (tool, params) match,
    which is the exact cross-tenant leak ``aws_session.py`` prevents on the
    boto3 client layer.

    DynamoDB table schema:
        - Partition key: cache_key (String)
        - TTL attribute: expires_at (Number — Unix epoch seconds)
        - result attribute: JSON-serialized tool result
    """

    def __init__(
        self,
        table_name: str,
        ttl_seconds: int | None = None,
        tenant_scope: str | None = None,
    ):
        """Initialize the tool cache.

        Args:
            table_name: DynamoDB table name for caching.
            ttl_seconds: Override TTL in seconds. If None, reads from
                CACHE_TTL_SECONDS env var (default 300).
            tenant_scope: Identifier for the AWS account whose data this
                cache instance serves — typically the assumed customer
                account ID under cross-account access, or the deploy
                account ID otherwise. Two ToolCache instances with the
                same table but different tenant_scope values MUST return
                different keys for identical (tool, params).
        """
        if ttl_seconds is not None:
            self.ttl_seconds = ttl_seconds
        else:
            self.ttl_seconds = _parse_ttl_seconds(os.environ.get("CACHE_TTL_SECONDS"))
        self.table_name = table_name
        self.tenant_scope = tenant_scope or "deploy-account"
        self._table = boto3.resource("dynamodb").Table(table_name)

    def get(self, tool_name: str, params: dict) -> dict | None:
        """Return cached result or None. Never raises.

        Args:
            tool_name: Name of the tool being invoked.
            params: Input parameters for the tool call.

        Returns:
            Cached result dict if found and not expired, otherwise None.
        """
        try:
            cache_key = self._make_key(tool_name, params, self.tenant_scope)
            response = self._table.get_item(Key={"cache_key": cache_key})
            item = response.get("Item")
            if item is None:
                return None

            # Check TTL expiration (DynamoDB TTL cleanup is eventual)
            expires_at = item.get("expires_at", 0)
            if time.time() > expires_at:
                return None

            result_str = item.get("result")
            if result_str is None:
                return None

            return json.loads(result_str)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Cache get failed for tool=%s, falling through to live API",
                tool_name,
                exc_info=True,
            )
            return None

    def put(self, tool_name: str, params: dict, result: dict) -> None:
        """Store result if serialized size <= 400KB. Never raises.

        Args:
            tool_name: Name of the tool that produced the result.
            params: Input parameters for the tool call.
            result: Tool result to cache.
        """
        try:
            result_json = json.dumps(result)

            # Skip caching if serialized size exceeds 400KB
            if len(result_json.encode("utf-8")) > MAX_ITEM_SIZE_BYTES:
                logger.debug(
                    "Skipping cache put for tool=%s: result size %d exceeds 400KB",
                    tool_name,
                    len(result_json.encode("utf-8")),
                )
                return

            cache_key = self._make_key(tool_name, params, self.tenant_scope)
            expires_at = int(time.time()) + self.ttl_seconds

            self._table.put_item(
                Item={
                    "cache_key": cache_key,
                    "result": result_json,
                    "expires_at": expires_at,
                    "tool_name": tool_name,
                    "tenant_scope": self.tenant_scope,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Cache put failed for tool=%s, continuing without caching",
                tool_name,
                exc_info=True,
            )

    @staticmethod
    def _make_key(
        tool_name: str,
        params: dict,
        tenant_scope: str = "deploy-account",
    ) -> str:
        """Generate a deterministic, tenant-scoped cache key.

        Format: {tool_name}#{tenant_scope}#{sha256(json.dumps(params, sort_keys=True))}

        The ``tenant_scope`` component prevents cross-tenant cache leaks in a
        warm Lambda container: two invocations with identical (tool, params)
        but different assumed customer accounts MUST produce different keys.

        Args:
            tool_name: Name of the tool.
            params: Input parameters dict.
            tenant_scope: Identifier for the AWS account whose data this
                call reads. Defaults to ``"deploy-account"`` (no cross-
                account role assumed).

        Returns:
            Deterministic cache key string.
        """
        canonical = json.dumps(params, sort_keys=True)
        param_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{tool_name}#{tenant_scope}#{param_hash}"
