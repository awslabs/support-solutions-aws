# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based tests for caller identity extraction priority.

Feature: production-observability-resilience
Property 12: Caller Identity Extraction Priority

**Validates: Requirements 11.1, 11.4**

For any API Gateway request context containing any combination of IAM ARN,
API key ID, and Cognito sub claim, the extracted caller_id SHALL be the first
available value in priority order (IAM ARN > API key ID > Cognito sub), or
"anonymous" if none are present.
"""

from __future__ import annotations

import sys
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

from src.lambda_handler import _extract_caller_id  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for IAM ARNs (realistic format)
iam_arn_st = st.from_regex(
    r"arn:aws:iam::[0-9]{12}:(user|role)/[a-zA-Z0-9_+=,.@-]{1,64}",
    fullmatch=True,
)

# Strategy for API key IDs (alphanumeric, 10-20 chars)
api_key_id_st = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"), min_codepoint=48, max_codepoint=122
    ),
    min_size=10,
    max_size=20,
).filter(lambda s: s.isalnum())

# Strategy for Cognito sub claims (UUID format)
cognito_sub_st = st.from_regex(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    fullmatch=True,
)

# Strategy for optional (present or absent) identity fields
optional_iam_arn_st = st.one_of(st.none(), iam_arn_st)
optional_api_key_id_st = st.one_of(st.none(), api_key_id_st)
optional_cognito_sub_st = st.one_of(st.none(), cognito_sub_st)


def _build_event(
    iam_arn: str | None = None,
    api_key_id: str | None = None,
    cognito_sub: str | None = None,
) -> dict:
    """Build an API Gateway event with the specified identity fields."""
    identity: dict = {}
    if iam_arn is not None:
        identity["userArn"] = iam_arn
    if api_key_id is not None:
        identity["apiKeyId"] = api_key_id

    authorizer: dict = {}
    if cognito_sub is not None:
        authorizer["claims"] = {"sub": cognito_sub}

    request_context: dict = {}
    if identity:
        request_context["identity"] = identity
    if authorizer:
        request_context["authorizer"] = authorizer

    event: dict = {}
    if request_context:
        event["requestContext"] = request_context

    return event


# ---------------------------------------------------------------------------
# Property test: Caller Identity Extraction Priority
# ---------------------------------------------------------------------------


class TestCallerIdentityExtractionPriority:
    """Property 12: Caller Identity Extraction Priority.

    Feature: production-observability-resilience
    Property 12: Caller Identity Extraction Priority

    **Validates: Requirements 11.1, 11.4**
    """

    @given(
        iam_arn=iam_arn_st,
        api_key_id=optional_api_key_id_st,
        cognito_sub=optional_cognito_sub_st,
    )
    @settings(max_examples=100)
    def test_iam_arn_takes_highest_priority(
        self,
        iam_arn: str,
        api_key_id: str | None,
        cognito_sub: str | None,
    ):
        """**Validates: Requirements 11.1**

        When IAM ARN is present, it is always returned regardless of whether
        API key ID or Cognito sub are also present.
        """
        event = _build_event(
            iam_arn=iam_arn,
            api_key_id=api_key_id,
            cognito_sub=cognito_sub,
        )
        result = _extract_caller_id(event)
        assert result == iam_arn, (
            f"Expected IAM ARN '{iam_arn}' but got '{result}'. "
            f"API key: {api_key_id}, Cognito sub: {cognito_sub}"
        )

    @given(
        api_key_id=api_key_id_st,
        cognito_sub=optional_cognito_sub_st,
    )
    @settings(max_examples=100)
    def test_api_key_id_takes_second_priority(
        self,
        api_key_id: str,
        cognito_sub: str | None,
    ):
        """**Validates: Requirements 11.1**

        When IAM ARN is absent but API key ID is present, the API key ID
        is returned regardless of whether Cognito sub is also present.
        """
        event = _build_event(
            iam_arn=None,
            api_key_id=api_key_id,
            cognito_sub=cognito_sub,
        )
        result = _extract_caller_id(event)
        assert result == api_key_id, (
            f"Expected API key ID '{api_key_id}' but got '{result}'. "
            f"Cognito sub: {cognito_sub}"
        )

    @given(cognito_sub=cognito_sub_st)
    @settings(max_examples=100)
    def test_cognito_sub_takes_third_priority(self, cognito_sub: str):
        """**Validates: Requirements 11.1**

        When both IAM ARN and API key ID are absent but Cognito sub is
        present, the Cognito sub is returned.
        """
        event = _build_event(
            iam_arn=None,
            api_key_id=None,
            cognito_sub=cognito_sub,
        )
        result = _extract_caller_id(event)
        assert result == cognito_sub, (
            f"Expected Cognito sub '{cognito_sub}' but got '{result}'"
        )

    @settings(max_examples=100)
    @given(st.data())
    def test_anonymous_when_no_identity_present(self, data):
        """**Validates: Requirements 11.4**

        When no identity fields (IAM ARN, API key ID, Cognito sub) are
        present in the request context, the caller_id is "anonymous".
        """
        # Generate events with no identity fields — various empty structures
        event_variant = data.draw(
            st.sampled_from(
                [
                    {},
                    {"requestContext": {}},
                    {"requestContext": {"identity": {}}},
                    {"requestContext": {"identity": {}, "authorizer": {}}},
                    {"requestContext": {"identity": {}, "authorizer": {"claims": {}}}},
                    {"requestContext": {"authorizer": {"claims": {}}}},
                    {"other_field": "value"},
                ]
            )
        )
        result = _extract_caller_id(event_variant)
        assert result == "anonymous", (
            f"Expected 'anonymous' but got '{result}' for event: {event_variant}"
        )

    @given(
        iam_arn=optional_iam_arn_st,
        api_key_id=optional_api_key_id_st,
        cognito_sub=optional_cognito_sub_st,
    )
    @settings(max_examples=200)
    def test_priority_order_comprehensive(
        self,
        iam_arn: str | None,
        api_key_id: str | None,
        cognito_sub: str | None,
    ):
        """**Validates: Requirements 11.1, 11.4**

        For any combination of present/absent identity fields, the extraction
        follows strict priority: IAM ARN > API key ID > Cognito sub > "anonymous".
        """
        event = _build_event(
            iam_arn=iam_arn,
            api_key_id=api_key_id,
            cognito_sub=cognito_sub,
        )
        result = _extract_caller_id(event)

        # Determine expected result based on priority
        if iam_arn is not None:
            expected = iam_arn
        elif api_key_id is not None:
            expected = api_key_id
        elif cognito_sub is not None:
            expected = cognito_sub
        else:
            expected = "anonymous"

        assert result == expected, (
            f"Priority violation: expected '{expected}' but got '{result}'. "
            f"IAM ARN: {iam_arn}, API key: {api_key_id}, Cognito sub: {cognito_sub}"
        )
