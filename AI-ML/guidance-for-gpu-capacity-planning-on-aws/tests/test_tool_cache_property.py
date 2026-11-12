# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for ToolCache.

Feature: production-observability-resilience

Property 14: Cache Key Determinism — For any tool name and input parameters
dict, the generated cache key SHALL be deterministic — calling the key
generation function multiple times with the same inputs SHALL always produce
the same key, regardless of dict insertion order.

Property 18: Cache Unavailability Fallthrough — For any DynamoDB error
(on read or write), the tool cache SHALL fall through to the live API call,
return the live result without error, and the failed cache operation SHALL
not add more than 1 second of additional latency.

**Validates: Requirements 12.1, 12.6**
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
    ConnectTimeoutError,
    NoCredentialsError,
)
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from src.tool_cache import ToolCache


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for DynamoDB exceptions that can occur on read/write
_dynamodb_exceptions_st = st.sampled_from(
    [
        ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Internal error"}},
            "GetItem",
        ),
        ClientError(
            {
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "Rate exceeded",
                }
            },
            "GetItem",
        ),
        ClientError(
            {"Error": {"Code": "RequestLimitExceeded", "Message": "Too many requests"}},
            "PutItem",
        ),
        ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "Service unavailable"}},
            "GetItem",
        ),
        ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "Table not found",
                }
            },
            "GetItem",
        ),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.us-east-1.amazonaws.com"
        ),
        ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com"),
        ConnectTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com"),
        NoCredentialsError(),
        ConnectionError("Connection refused"),
        TimeoutError("Operation timed out"),
        OSError("Network unreachable"),
        RuntimeError("Unexpected DynamoDB failure"),
    ]
)

# Strategy for tool names
_tool_name_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
    min_size=1,
    max_size=50,
)

# Strategy for tool parameters (simple JSON-serializable dicts)
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
    max_size=10,
)

# Strategy for tool results (JSON-serializable dicts)
_result_st = st.dictionaries(
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
    ),
    min_size=1,
    max_size=10,
)


# ---------------------------------------------------------------------------
# Property test: Cache Key Determinism (Property 14)
# ---------------------------------------------------------------------------


class TestCacheKeyDeterminism:
    """Property 14: Cache Key Determinism.

    Feature: production-observability-resilience, Property 14: Cache Key Determinism

    **Validates: Requirements 12.1**

    For any tool name and input parameters dict, the generated cache key
    SHALL be deterministic — calling the key generation function multiple
    times with the same inputs SHALL always produce the same key, regardless
    of dict insertion order.
    """

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
    )
    @settings(max_examples=200)
    def test_same_inputs_always_produce_same_key(self, tool_name: str, params: dict):
        """**Validates: Requirements 12.1**

        Calling _make_key multiple times with the same tool name and params
        always produces the same cache key.
        """
        key1 = ToolCache._make_key(tool_name, params)
        key2 = ToolCache._make_key(tool_name, params)
        key3 = ToolCache._make_key(tool_name, params)

        assert key1 == key2, f"Key mismatch on repeated calls: {key1!r} != {key2!r}"
        assert key2 == key3, f"Key mismatch on repeated calls: {key2!r} != {key3!r}"

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
    )
    @settings(max_examples=200)
    def test_dict_insertion_order_does_not_affect_key(
        self, tool_name: str, params: dict
    ):
        """**Validates: Requirements 12.1**

        Dicts with the same content but different insertion order produce
        the same cache key.
        """
        if len(params) < 2:
            # Need at least 2 keys to test ordering
            params["key_alpha"] = "value_a"
            params["key_beta"] = "value_b"

        # Create a dict with reversed key insertion order
        reversed_params = dict(reversed(list(params.items())))

        key_original = ToolCache._make_key(tool_name, params)
        key_reversed = ToolCache._make_key(tool_name, reversed_params)

        assert key_original == key_reversed, (
            f"Key differs with reversed dict order: "
            f"{key_original!r} != {key_reversed!r} "
            f"for params={params!r} vs reversed={reversed_params!r}"
        )

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
    )
    @settings(max_examples=200)
    def test_key_format_is_tool_name_scope_hash(self, tool_name: str, params: dict):
        """**Validates: Requirements 12.1**

        The cache key follows the format:
        ``{tool_name}#{tenant_scope}#{sha256_hex_digest}``.
        """
        key = ToolCache._make_key(tool_name, params)

        # Key must contain the tool name as prefix followed by '#'
        assert key.startswith(f"{tool_name}#"), (
            f"Key should start with '{tool_name}#', got: {key!r}"
        )

        # Split on '#' — expect exactly 3 parts (tool, scope, hash).
        # Note: tool_name may contain '#' if the alphabet allows Pd (dashes)
        # but never '#' itself. Split from the right to be safe.
        parts = key.rsplit("#", 2)
        assert len(parts) == 3, f"Key should have 3 '#'-delimited parts, got: {key!r}"
        _prefix, scope_part, hash_part = parts
        assert scope_part == "deploy-account", (
            f"Default scope should be 'deploy-account', got: {scope_part!r}"
        )

        # Hash part is a valid SHA-256 hex digest (64 hex chars)
        assert len(hash_part) == 64, (
            f"Hash part should be 64 chars (SHA-256), got {len(hash_part)}"
        )
        assert all(c in "0123456789abcdef" for c in hash_part), (
            f"Hash part should be lowercase hex, got: {hash_part!r}"
        )


# ---------------------------------------------------------------------------
# Property test: Cache Unavailability Fallthrough (Property 18)
# ---------------------------------------------------------------------------


class TestCacheUnavailabilityFallthrough:
    """Property 18: Cache Unavailability Fallthrough.

    Feature: production-observability-resilience, Property 18: Cache Unavailability Fallthrough

    **Validates: Requirements 12.6**

    For any DynamoDB error (on read or write), the tool cache SHALL fall
    through to the live API call without failing the request.
    """

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        exception=_dynamodb_exceptions_st,
    )
    @settings(max_examples=100)
    def test_get_returns_none_on_dynamodb_read_error(
        self,
        tool_name: str,
        params: dict,
        exception: Exception,
    ):
        """**Validates: Requirements 12.6**

        When DynamoDB raises any exception on get_item, the cache get()
        method SHALL return None without raising, allowing the caller to
        proceed with a live API call.
        """
        with patch("src.tool_cache.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            mock_table.get_item.side_effect = exception

            cache = ToolCache("test-table", ttl_seconds=300)
            result = cache.get(tool_name, params)

            # get() must return None (fallthrough) and never raise
            assert result is None

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        tool_result=_result_st,
        exception=_dynamodb_exceptions_st,
    )
    @settings(max_examples=100)
    def test_put_completes_without_error_on_dynamodb_write_error(
        self,
        tool_name: str,
        params: dict,
        tool_result: dict,
        exception: Exception,
    ):
        """**Validates: Requirements 12.6**

        When DynamoDB raises any exception on put_item, the cache put()
        method SHALL complete without raising, allowing the caller to
        return the live result without error.
        """
        with patch("src.tool_cache.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            mock_table.put_item.side_effect = exception

            cache = ToolCache("test-table", ttl_seconds=300)

            # put() must not raise — it should silently swallow the error
            cache.put(tool_name, params, tool_result)

            # If we reach here, the exception was properly caught
            # (no assertion needed — the test passes if no exception propagates)

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        tool_result=_result_st,
        get_exception=_dynamodb_exceptions_st,
        put_exception=_dynamodb_exceptions_st,
    )
    @settings(max_examples=100)
    def test_full_cache_cycle_with_both_read_and_write_errors(
        self,
        tool_name: str,
        params: dict,
        tool_result: dict,
        get_exception: Exception,
        put_exception: Exception,
    ):
        """**Validates: Requirements 12.6**

        When DynamoDB raises exceptions on both get_item and put_item in
        the same invocation, both operations SHALL fail gracefully. The
        get() returns None (triggering live API call) and put() completes
        without raising (result is returned to caller without caching).
        """
        with patch("src.tool_cache.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            mock_table.get_item.side_effect = get_exception
            mock_table.put_item.side_effect = put_exception

            cache = ToolCache("test-table", ttl_seconds=300)

            # Simulate the full cache cycle:
            # 1. Try to get from cache (fails → returns None → live API called)
            cached = cache.get(tool_name, params)
            assert cached is None

            # 2. After live API returns result, try to cache it (fails silently)
            cache.put(tool_name, params, tool_result)

            # Both operations completed without raising — fallthrough works


# ---------------------------------------------------------------------------
# Property test: Cache Write with Size Guard (Property 16)
# ---------------------------------------------------------------------------


class TestCacheWriteSizeGuard:
    """Property 16: Cache Write with Size Guard.

    Feature: production-observability-resilience, Property 16: Cache Write with Size Guard

    **Validates: Requirements 12.3, 12.7**

    For any tool result, the cache SHALL store the result if and only if its
    JSON-serialized size is <= 400KB. Results exceeding 400KB SHALL be returned
    directly without caching and without error.
    """

    @staticmethod
    def _make_result_of_size(target_size: int) -> dict:
        """Create a dict whose json.dumps().encode('utf-8') is approximately target_size bytes.

        The result is {"d": "<padding>"} where padding is sized to hit the target.
        JSON overhead for {"d": ""} is 7 bytes, so padding = target_size - 7.
        If target_size <= overhead, we use an empty string (minimum serialized size).
        """
        import json as _json

        overhead = len(_json.dumps({"d": ""}).encode("utf-8"))  # 7 bytes
        if target_size <= overhead:
            return {"d": ""}
        padding_len = target_size - overhead
        return {"d": "x" * padding_len}

    @given(
        tool_name=_tool_name_st,
        target_size=st.integers(min_value=1, max_value=1024 * 1024),
    )
    @settings(max_examples=100)
    def test_results_within_limit_are_stored_and_oversized_are_skipped(
        self, tool_name: str, target_size: int
    ):
        """**Validates: Requirements 12.3, 12.7**

        Generate random results of varying sizes (1B to 1MB).
        Verify results <= 400KB are stored (put_item called) and
        results > 400KB are skipped without error (put_item NOT called).
        """
        import json as _json

        from src.tool_cache import MAX_ITEM_SIZE_BYTES

        with patch("src.tool_cache.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table

            cache = ToolCache("test-table", ttl_seconds=300)
            result = self._make_result_of_size(target_size)

            # Call put — should never raise regardless of size
            cache.put(tool_name, {"param": "value"}, result)

            # Determine actual serialized size
            serialized_size = len(_json.dumps(result).encode("utf-8"))

            if serialized_size <= MAX_ITEM_SIZE_BYTES:
                # Property: results within limit MUST be stored
                mock_table.put_item.assert_called_once()
                stored_item = mock_table.put_item.call_args[1]["Item"]
                # Verify the stored result matches the original
                assert _json.loads(stored_item["result"]) == result
                assert stored_item["tool_name"] == tool_name
                assert "expires_at" in stored_item
                assert "cache_key" in stored_item
            else:
                # Property: oversized results MUST NOT be stored
                mock_table.put_item.assert_not_called()

    @given(
        tool_name=_tool_name_st,
        target_size=st.integers(min_value=1, max_value=1024 * 1024),
    )
    @settings(max_examples=100)
    def test_oversized_results_never_raise(self, tool_name: str, target_size: int):
        """**Validates: Requirements 12.7**

        For any result exceeding ``MAX_ITEM_SIZE_BYTES``, the put method
        SHALL NOT raise an exception — the result is simply skipped.

        The exact threshold value is intentionally NOT hardcoded here;
        M5 lowered it to 380KB to leave headroom for DynamoDB attribute
        overhead. Any target size above the current threshold must be a
        no-op ``put``.
        """
        from src.tool_cache import MAX_ITEM_SIZE_BYTES

        # Only test oversized results.
        assume(target_size > MAX_ITEM_SIZE_BYTES)

        with patch("src.tool_cache.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table

            cache = ToolCache("test-table", ttl_seconds=300)
            result = self._make_result_of_size(target_size)

            # Must not raise
            cache.put(tool_name, {"param": "value"}, result)

            # Verify put_item was NOT called (oversized)
            mock_table.put_item.assert_not_called()

    @given(
        tool_name=_tool_name_st,
        target_size=st.integers(min_value=1, max_value=1024 * 1024),
    )
    @settings(max_examples=100)
    def test_results_within_limit_are_always_stored(
        self, tool_name: str, target_size: int
    ):
        """**Validates: Requirements 12.3**

        For any result whose serialized size is <= ``MAX_ITEM_SIZE_BYTES``,
        the cache SHALL store the result in DynamoDB.
        """
        import json as _json

        from src.tool_cache import MAX_ITEM_SIZE_BYTES

        # Only test within-limit results.
        assume(target_size <= MAX_ITEM_SIZE_BYTES)

        with patch("src.tool_cache.boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table

            cache = ToolCache("test-table", ttl_seconds=300)
            result = self._make_result_of_size(target_size)

            cache.put(tool_name, {"param": "value"}, result)

            # Verify the actual serialized size is within limit
            serialized_size = len(_json.dumps(result).encode("utf-8"))
            assert serialized_size <= MAX_ITEM_SIZE_BYTES

            # Property: put_item MUST be called for within-limit results
            mock_table.put_item.assert_called_once()


# ---------------------------------------------------------------------------
# Hypothesis strategies for Property 17
# ---------------------------------------------------------------------------

# Strategy for valid TTL values (integers 1-3600 inclusive)
_valid_ttl_st = st.integers(min_value=1, max_value=3600)

# Strategy for out-of-range integers (below 1 or above 3600)
_out_of_range_ttl_st = st.one_of(
    st.integers(max_value=0),
    st.integers(min_value=3601),
)

# Strategy for non-integer strings that cannot be parsed as int
_non_integer_str_st = st.one_of(
    st.just(""),
    st.text(
        min_size=1,
        max_size=50,
        alphabet=st.characters(
            whitelist_categories=("L", "P", "S"),
            blacklist_characters=(
                "0",
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
                "8",
                "9",
                "-",
                "+",
            ),
        ),
    ),
    # Float strings like "3.14", "1.0", "300.5"
    st.floats(
        min_value=-10000.0,
        max_value=10000.0,
        allow_nan=False,
        allow_infinity=False,
    )
    .filter(lambda x: x != int(x) if x == x else True)
    .map(str),
)


# ---------------------------------------------------------------------------
# Property test: Cache TTL Configuration Validation
# ---------------------------------------------------------------------------


class TestCacheTtlConfigurationValidation:
    """Property 17: Cache TTL Configuration Validation.

    Feature: production-observability-resilience, Property 17: Cache TTL Configuration Validation

    **Validates: Requirements 12.4**

    For any CACHE_TTL_SECONDS environment variable value, the cache SHALL use
    the value if it is a valid integer between 1 and 3600 inclusive, and SHALL
    fall back to 300 seconds for any invalid value (None, non-integer, out of range).
    """

    @given(ttl_value=_valid_ttl_st)
    @settings(max_examples=100)
    def test_valid_integers_are_used_as_ttl(self, ttl_value: int):
        """**Validates: Requirements 12.4**

        Any valid integer between 1 and 3600 inclusive SHALL be used as the
        TTL value without modification.
        """
        from src.tool_cache import _parse_ttl_seconds

        result = _parse_ttl_seconds(str(ttl_value))
        assert result == ttl_value, (
            f"Valid TTL {ttl_value} should be used as-is, got {result}"
        )

    @given(ttl_value=_out_of_range_ttl_st)
    @settings(max_examples=100)
    def test_out_of_range_integers_fall_back_to_default(self, ttl_value: int):
        """**Validates: Requirements 12.4**

        Any integer outside the range 1-3600 SHALL fall back to the default
        value of 300 seconds.
        """
        from src.tool_cache import _parse_ttl_seconds

        result = _parse_ttl_seconds(str(ttl_value))
        assert result == 300, (
            f"Out-of-range TTL '{ttl_value}' should fall back to 300, got {result}"
        )

    @given(ttl_value=_non_integer_str_st)
    @settings(max_examples=100)
    def test_non_integer_strings_fall_back_to_default(self, ttl_value: str):
        """**Validates: Requirements 12.4**

        Any non-integer string value (empty string, alphabetic, float strings)
        SHALL fall back to the default value of 300 seconds.
        """
        from src.tool_cache import _parse_ttl_seconds

        result = _parse_ttl_seconds(ttl_value)
        assert result == 300, (
            f"Non-integer TTL '{ttl_value}' should fall back to 300, got {result}"
        )

    @given(data=st.data())
    @settings(max_examples=100)
    def test_none_value_falls_back_to_default(self, data):
        """**Validates: Requirements 12.4**

        When CACHE_TTL_SECONDS is None (env var not set), the cache SHALL
        fall back to the default value of 300 seconds.
        """
        from src.tool_cache import _parse_ttl_seconds

        result = _parse_ttl_seconds(None)
        assert result == 300, f"None TTL should fall back to 300, got {result}"

    @given(
        ttl_value=st.one_of(
            st.just(None),
            _non_integer_str_st,
            _out_of_range_ttl_st.map(str),
            _valid_ttl_st.map(str),
        )
    )
    @settings(max_examples=100)
    def test_return_value_is_always_valid_integer_in_range(self, ttl_value: str | None):
        """**Validates: Requirements 12.4**

        For any input value, _parse_ttl_seconds SHALL always return a positive
        integer in the range [1, 3600]. The return value is always suitable
        for use as a TTL.
        """
        from src.tool_cache import _parse_ttl_seconds

        result = _parse_ttl_seconds(ttl_value)
        assert isinstance(result, int), (
            f"Return value must be an int, got {type(result)}"
        )
        assert 1 <= result <= 3600, (
            f"Return value {result} is not in valid range [1, 3600]"
        )


# ---------------------------------------------------------------------------
# Property test: Tenant-Scoped Cache Key Isolation (L4)
# ---------------------------------------------------------------------------


class TestTenantScopeIsolationProperty:
    """L4 fuzz coverage of C1 cache tenant isolation.

    For any (tool, params) pair, two DIFFERENT tenant scopes MUST produce
    different keys, and the SAME tenant scope MUST always produce the same
    key. This complements the concrete tests in test_tool_cache.py by
    exercising many random tenant/params combinations.
    """

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        tenant_a=st.text(min_size=1, max_size=40),
        tenant_b=st.text(min_size=1, max_size=40),
    )
    @settings(max_examples=200)
    def test_different_tenants_never_share_a_key(
        self, tool_name: str, params: dict, tenant_a: str, tenant_b: str
    ):
        # Only meaningful when the two tenants differ.
        from hypothesis import assume

        assume(tenant_a != tenant_b)

        key_a = ToolCache._make_key(tool_name, params, tenant_a)
        key_b = ToolCache._make_key(tool_name, params, tenant_b)
        assert key_a != key_b, (
            f"Cross-tenant collision: tenants {tenant_a!r} and {tenant_b!r} "
            f"produce identical cache key {key_a!r} for tool={tool_name!r}, "
            f"params={params!r}"
        )

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        tenant=st.text(min_size=1, max_size=40),
    )
    @settings(max_examples=200)
    def test_same_tenant_same_inputs_yields_same_key(
        self, tool_name: str, params: dict, tenant: str
    ):
        key_1 = ToolCache._make_key(tool_name, params, tenant)
        key_2 = ToolCache._make_key(tool_name, params, tenant)
        assert key_1 == key_2

    @given(
        tool_name=_tool_name_st,
        params=_params_st,
        tenant=st.text(min_size=1, max_size=40),
    )
    @settings(max_examples=200)
    def test_tenant_never_collides_with_deploy_account_default(
        self, tool_name: str, params: dict, tenant: str
    ):
        """A random customer account ID must never collide with the
        ``"deploy-account"`` sentinel used when no session is bound.
        """
        from hypothesis import assume

        assume(tenant != "deploy-account")

        deploy_key = ToolCache._make_key(tool_name, params)  # default scope
        tenant_key = ToolCache._make_key(tool_name, params, tenant)
        assert deploy_key != tenant_key
