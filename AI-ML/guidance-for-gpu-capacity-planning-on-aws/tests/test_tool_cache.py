# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ToolCache class.

Tests cover:
- Cache key determinism (_make_key)
- TTL parsing (_parse_ttl_seconds)
- get() behavior (cache hit, miss, expired, DynamoDB errors)
- put() behavior (normal, oversized, DynamoDB errors)
"""

from __future__ import annotations

import hashlib
import json
import time
from unittest.mock import MagicMock, patch


from src.tool_cache import ToolCache, _parse_ttl_seconds, MAX_ITEM_SIZE_BYTES


# ---------------------------------------------------------------------------
# Tests for _parse_ttl_seconds
# ---------------------------------------------------------------------------


class TestParseTtlSeconds:
    """Tests for CACHE_TTL_SECONDS env var parsing."""

    def test_none_returns_default(self):
        assert _parse_ttl_seconds(None) == 300

    def test_valid_integer_in_range(self):
        assert _parse_ttl_seconds("60") == 60
        assert _parse_ttl_seconds("1") == 1
        assert _parse_ttl_seconds("3600") == 3600

    def test_below_range_returns_default(self):
        assert _parse_ttl_seconds("0") == 300
        assert _parse_ttl_seconds("-1") == 300

    def test_above_range_returns_default(self):
        assert _parse_ttl_seconds("3601") == 300
        assert _parse_ttl_seconds("99999") == 300

    def test_non_integer_returns_default(self):
        assert _parse_ttl_seconds("abc") == 300
        assert _parse_ttl_seconds("3.14") == 300
        assert _parse_ttl_seconds("") == 300


# ---------------------------------------------------------------------------
# Tests for ToolCache._make_key
# ---------------------------------------------------------------------------


class TestMakeKey:
    """Tests for deterministic cache key generation."""

    def test_basic_key_format(self):
        key = ToolCache._make_key("my_tool", {"a": 1, "b": 2})
        canonical = json.dumps({"a": 1, "b": 2}, sort_keys=True)
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert key == f"my_tool#deploy-account#{expected_hash}"

    def test_key_deterministic_regardless_of_dict_order(self):
        params_a = {"z": 3, "a": 1, "m": 2}
        params_b = {"a": 1, "m": 2, "z": 3}
        key_a = ToolCache._make_key("tool", params_a)
        key_b = ToolCache._make_key("tool", params_b)
        assert key_a == key_b

    def test_different_tool_names_produce_different_keys(self):
        params = {"x": 1}
        key_a = ToolCache._make_key("tool_a", params)
        key_b = ToolCache._make_key("tool_b", params)
        assert key_a != key_b

    def test_different_params_produce_different_keys(self):
        key_a = ToolCache._make_key("tool", {"x": 1})
        key_b = ToolCache._make_key("tool", {"x": 2})
        assert key_a != key_b

    def test_empty_params(self):
        key = ToolCache._make_key("tool", {})
        canonical = json.dumps({}, sort_keys=True)
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert key == f"tool#deploy-account#{expected_hash}"


# ---------------------------------------------------------------------------
# Tests for cross-tenant isolation in the cache key (regression guard)
# ---------------------------------------------------------------------------


class TestTenantScopedCacheKey:
    """Guards against cross-tenant cache leaks in warm Lambda containers.

    Two invocations with identical (tool, params) but different assumed
    customer accounts MUST produce different cache keys. Otherwise a warm
    container will serve tenant A's results to tenant B.
    """

    def test_different_customer_accounts_produce_different_keys(self):
        params = {"instance_type": "g5.2xlarge", "region": "us-east-1"}
        key_a = ToolCache._make_key("get_ice_history", params, "111111111111")
        key_b = ToolCache._make_key("get_ice_history", params, "222222222222")
        assert key_a != key_b

    def test_deploy_account_and_customer_account_isolated(self):
        params = {"instance_type": "g5.2xlarge", "region": "us-east-1"}
        deploy = ToolCache._make_key("get_ice_history", params, "deploy-account")
        customer = ToolCache._make_key("get_ice_history", params, "111111111111")
        assert deploy != customer

    def test_same_scope_same_params_same_key(self):
        params = {"instance_type": "g5.2xlarge", "region": "us-east-1"}
        first = ToolCache._make_key("get_ice_history", params, "111111111111")
        second = ToolCache._make_key("get_ice_history", params, "111111111111")
        assert first == second

    def test_scope_component_appears_in_key(self):
        key = ToolCache._make_key("get_ice_history", {}, "111111111111")
        # Format: {tool_name}#{tenant_scope}#{hash}
        parts = key.split("#")
        assert len(parts) == 3
        assert parts[0] == "get_ice_history"
        assert parts[1] == "111111111111"

    @patch("src.tool_cache.boto3.resource")
    def test_cache_does_not_leak_between_customers_end_to_end(self, mock_resource):
        """Full flow: put by customer A, get by customer B → miss."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        # Track PUT items and simulate DynamoDB by key
        storage: dict = {}

        def fake_put(**kwargs):
            item = kwargs["Item"]
            storage[item["cache_key"]] = item
            return {}

        def fake_get(**kwargs):
            key = kwargs["Key"]["cache_key"]
            if key in storage:
                return {"Item": storage[key]}
            return {}

        mock_table.put_item.side_effect = fake_put
        mock_table.get_item.side_effect = fake_get

        cache_a = ToolCache("t", ttl_seconds=300, tenant_scope="111111111111")
        cache_b = ToolCache("t", ttl_seconds=300, tenant_scope="222222222222")

        params = {"instance_type": "g5.2xlarge", "region": "us-east-1"}
        cache_a.put("get_ice_history", params, {"event_count": 42})

        # Tenant B must NOT see tenant A's result.
        assert cache_b.get("get_ice_history", params) is None
        # Tenant A still sees its own result.
        assert cache_a.get("get_ice_history", params) == {"event_count": 42}

    @patch("src.tool_cache.boto3.resource")
    def test_default_tenant_scope_is_deploy_account(self, mock_resource):
        mock_resource.return_value.Table.return_value = MagicMock()
        cache = ToolCache("t", ttl_seconds=300)
        assert cache.tenant_scope == "deploy-account"

    @patch("src.tool_cache.boto3.resource")
    def test_tenant_scope_stored_in_dynamodb_item(self, mock_resource):
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        cache = ToolCache("t", ttl_seconds=300, tenant_scope="123456789012")
        cache.put("tool", {"x": 1}, {"data": "ok"})

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["tenant_scope"] == "123456789012"


# ---------------------------------------------------------------------------
# Tests for ToolCache.get
# ---------------------------------------------------------------------------


class TestToolCacheGet:
    """Tests for cache get behavior."""

    @patch("src.tool_cache.boto3.resource")
    def test_cache_hit_returns_result(self, mock_resource):
        """Valid cached item is returned."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        cached_result = {"data": "cached_value"}
        mock_table.get_item.return_value = {
            "Item": {
                "cache_key": "tool#abc",
                "result": json.dumps(cached_result),
                "expires_at": int(time.time()) + 600,
            }
        }

        cache = ToolCache("test-table", ttl_seconds=300)
        result = cache.get("tool", {"param": "value"})
        assert result == cached_result

    @patch("src.tool_cache.boto3.resource")
    def test_cache_miss_returns_none(self, mock_resource):
        """Missing item returns None."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {}

        cache = ToolCache("test-table", ttl_seconds=300)
        result = cache.get("tool", {"param": "value"})
        assert result is None

    @patch("src.tool_cache.boto3.resource")
    def test_expired_item_returns_none(self, mock_resource):
        """Expired item (TTL passed) returns None."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        mock_table.get_item.return_value = {
            "Item": {
                "cache_key": "tool#abc",
                "result": json.dumps({"data": "old"}),
                "expires_at": int(time.time()) - 100,  # expired
            }
        }

        cache = ToolCache("test-table", ttl_seconds=300)
        result = cache.get("tool", {"param": "value"})
        assert result is None

    @patch("src.tool_cache.boto3.resource")
    def test_dynamodb_error_returns_none(self, mock_resource):
        """DynamoDB error on get returns None without raising."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.get_item.side_effect = Exception("DynamoDB unavailable")

        cache = ToolCache("test-table", ttl_seconds=300)
        result = cache.get("tool", {"param": "value"})
        assert result is None


# ---------------------------------------------------------------------------
# Tests for ToolCache.put
# ---------------------------------------------------------------------------


class TestToolCachePut:
    """Tests for cache put behavior."""

    @patch("src.tool_cache.boto3.resource")
    def test_put_stores_result(self, mock_resource):
        """Normal-sized result is stored in DynamoDB."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        cache = ToolCache("test-table", ttl_seconds=300)
        cache.put("tool", {"param": "value"}, {"data": "result"})

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["tool_name"] == "tool"
        assert json.loads(item["result"]) == {"data": "result"}
        assert "expires_at" in item
        assert "cache_key" in item

    @patch("src.tool_cache.boto3.resource")
    def test_put_skips_oversized_result(self, mock_resource):
        """Result exceeding 400KB is not stored."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        cache = ToolCache("test-table", ttl_seconds=300)
        # Create a result larger than 400KB
        large_result = {"data": "x" * (MAX_ITEM_SIZE_BYTES + 1)}
        cache.put("tool", {"param": "value"}, large_result)

        mock_table.put_item.assert_not_called()

    @patch("src.tool_cache.boto3.resource")
    def test_put_dynamodb_error_does_not_raise(self, mock_resource):
        """DynamoDB error on put does not propagate."""
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_table.put_item.side_effect = Exception("DynamoDB unavailable")

        cache = ToolCache("test-table", ttl_seconds=300)
        # Should not raise
        cache.put("tool", {"param": "value"}, {"data": "result"})

    @patch("src.tool_cache.boto3.resource")
    def test_put_exactly_at_size_boundary_is_stored(self, mock_resource):
        """Result exactly at the MAX_ITEM_SIZE_BYTES boundary is stored.

        The threshold is set below DynamoDB's 400KB hard cap to leave room
        for attribute overhead (M5); this test uses the current threshold
        constant so it tracks the value.
        """
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table

        cache = ToolCache("test-table", ttl_seconds=300)
        # Create a result that serializes to exactly MAX_ITEM_SIZE_BYTES
        # We need to account for JSON overhead (braces, quotes, key)
        overhead = len(json.dumps({"d": ""}).encode("utf-8"))
        padding = "x" * (MAX_ITEM_SIZE_BYTES - overhead)
        result = {"d": padding}
        serialized = json.dumps(result).encode("utf-8")
        assert len(serialized) == MAX_ITEM_SIZE_BYTES

        cache.put("tool", {"param": "value"}, result)
        mock_table.put_item.assert_called_once()

    def test_size_guard_leaves_headroom_for_attribute_overhead(self):
        """regression guard: MAX_ITEM_SIZE_BYTES must sit strictly below
        DynamoDB's 400KB hard cap. The sibling attributes (``cache_key``,
        ``expires_at``, ``tool_name``, ``tenant_scope``, ``created_at``)
        plus DynamoDB's per-item overhead consume ~200 bytes, and item
        size is measured including all of that. If a future refactor
        raises MAX_ITEM_SIZE_BYTES back to 400*1024, put_item will start
        raising ValidationException on legitimate near-limit results.
        """
        dynamo_hard_limit = 400 * 1024
        # Leave at least 4KB of headroom.
        assert MAX_ITEM_SIZE_BYTES <= dynamo_hard_limit - 4 * 1024, (
            f"MAX_ITEM_SIZE_BYTES={MAX_ITEM_SIZE_BYTES} does not leave "
            f"enough headroom below DynamoDB's 400KB cap for sibling "
            f"attributes and per-item overhead."
        )


# ---------------------------------------------------------------------------
# Tests for ToolCache TTL env var integration
# ---------------------------------------------------------------------------


class TestToolCacheTtlConfig:
    """Tests for TTL configuration via env var."""

    @patch("src.tool_cache.boto3.resource")
    @patch.dict("os.environ", {"CACHE_TTL_SECONDS": "120"})
    def test_reads_ttl_from_env(self, mock_resource):
        mock_resource.return_value.Table.return_value = MagicMock()
        cache = ToolCache("test-table")
        assert cache.ttl_seconds == 120

    @patch("src.tool_cache.boto3.resource")
    @patch.dict("os.environ", {}, clear=True)
    def test_default_ttl_when_env_not_set(self, mock_resource):
        mock_resource.return_value.Table.return_value = MagicMock()
        cache = ToolCache("test-table")
        assert cache.ttl_seconds == 300

    @patch("src.tool_cache.boto3.resource")
    def test_explicit_ttl_overrides_env(self, mock_resource):
        mock_resource.return_value.Table.return_value = MagicMock()
        cache = ToolCache("test-table", ttl_seconds=600)
        assert cache.ttl_seconds == 600
