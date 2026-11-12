# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for TOKEN_BUDGET parsing and fallback behavior.

Feature: production-observability-resilience
Property 10: Invalid TOKEN_BUDGET Fallback

For any invalid TOKEN_BUDGET environment variable value (None, empty string,
non-integer string, float string, zero, or negative number), the agent loop
SHALL use a default budget of 20000 tokens.

**Validates: Requirements 10.3, 10.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for None values
none_st = st.none()

# Strategy for empty strings
empty_string_st = st.just("")

# Strategy for non-integer strings (text that cannot be parsed as int)
non_integer_text_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "P", "S")),
    min_size=1,
    max_size=50,
).filter(lambda s: not _is_valid_positive_int(s))

# Strategy for float strings (e.g., "3.14", "1.5", "0.001")
float_string_st = (
    st.floats(
        min_value=-1e6,
        max_value=1e6,
        allow_nan=False,
        allow_infinity=False,
    )
    .filter(lambda f: f != int(f) if f == f else True)
    .map(str)
)

# Strategy for zero
zero_st = st.just("0")

# Strategy for negative integers
negative_int_st = st.integers(min_value=-1000000, max_value=-1).map(str)

# Combined strategy for all invalid values
invalid_budget_st = st.one_of(
    none_st,
    empty_string_st,
    non_integer_text_st,
    float_string_st,
    zero_st,
    negative_int_st,
)

# Strategy for valid positive integers
valid_budget_st = st.integers(min_value=1, max_value=10000000).map(str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_positive_int(s: str) -> bool:
    """Check if a string represents a valid positive integer."""
    try:
        return int(s) >= 1
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Property test: Invalid TOKEN_BUDGET Fallback
# ---------------------------------------------------------------------------


class TestInvalidTokenBudgetFallback:
    """Property 10: Invalid TOKEN_BUDGET Fallback.

    Feature: production-observability-resilience
    Property 10: Invalid TOKEN_BUDGET Fallback

    **Validates: Requirements 10.3, 10.4**
    """

    @given(invalid_value=invalid_budget_st)
    @settings(max_examples=100)
    def test_invalid_budget_returns_default_20000(self, invalid_value: str | None):
        """**Validates: Requirements 10.3, 10.4**

        For any invalid TOKEN_BUDGET value (None, empty, non-integer, float,
        zero, negative), _parse_budget returns the default of 20000.
        """
        result = CapacityDecisionAgent._parse_budget(invalid_value)

        assert result == 20000, (
            f"_parse_budget({invalid_value!r}) should return 20000 (default), "
            f"got {result}"
        )

    @given(valid_value=valid_budget_st)
    @settings(max_examples=100)
    def test_valid_positive_integer_returns_that_value(self, valid_value: str):
        """**Validates: Requirements 10.3, 10.4**

        For any valid positive integer string, _parse_budget returns
        that integer value.
        """
        expected = int(valid_value)
        result = CapacityDecisionAgent._parse_budget(valid_value)

        assert result == expected, (
            f"_parse_budget({valid_value!r}) should return {expected}, got {result}"
        )

    @given(negative_value=st.integers(min_value=-1000000, max_value=0).map(str))
    @settings(max_examples=100)
    def test_zero_and_negative_values_return_default(self, negative_value: str):
        """**Validates: Requirements 10.4**

        For any zero or negative integer value, _parse_budget returns
        the default of 20000 since the budget must be >= 1.
        """
        result = CapacityDecisionAgent._parse_budget(negative_value)

        assert result == 20000, (
            f"_parse_budget({negative_value!r}) should return 20000 (default), "
            f"got {result}"
        )
