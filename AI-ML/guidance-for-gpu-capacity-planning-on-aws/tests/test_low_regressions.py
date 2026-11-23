# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guards for a set of hardening fixes applied late in
development. Tests are grouped by concern (MAX_ITERATIONS env parsing,
CORS allowlist, OPTIONS preflight, security response headers) so a CI
failure points at the specific fix that regressed.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext as _nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# lambda_handler uses bare imports ("from agent import ...") that only
# resolve in the Lambda runtime, not under pytest. Register stubs before
# import — same pattern used in tests/test_lambda_handler.py.
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
_mock_aws_session_module = MagicMock()
_mock_aws_session_module.use_session = lambda *_a, **_kw: _nullcontext()

sys.modules.setdefault("agent", _mock_agent_module)
sys.modules.setdefault("tools", _mock_tools_module)
sys.modules.setdefault("secrets_manager", _mock_secrets_module)
sys.modules.setdefault("metrics", _mock_metrics_module)
sys.modules.setdefault("tool_cache", _mock_tool_cache_module)
sys.modules.setdefault("aws_session", _mock_aws_session_module)

from src.lambda_handler import (  # noqa: E402
    _is_cors_preflight,
    _parse_positive_int,
    _response_headers,
    handler,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# CFN YAML loader (mirrors tests/test_template.py; kept local)
# ---------------------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_multi_constructor(loader, tag_suffix, node):
    tag = f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {tag: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag: loader.construct_sequence(node, deep=True)}
    return {tag: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


@pytest.fixture(scope="module")
def template() -> dict:
    with (REPO_ROOT / "template.yaml").open() as f:
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506 - _CfnLoader subclasses yaml.SafeLoader; parses trusted local CloudFormation templates


def _lambda_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.aws_request_id = "req-l-test"
    ctx.function_name = "CapacityAgentFunction"
    ctx.get_remaining_time_in_millis.return_value = 30000
    return ctx


def _rest_event(body: dict, method: str = "POST") -> dict:
    return {
        "httpMethod": method,
        "path": "/plan",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "requestContext": {"requestId": "req-l-test", "stage": "prod"},
    }


# ---------------------------------------------------------------------------
# MAX_ITERATIONS env-var parsing
# ---------------------------------------------------------------------------


class TestMaxIterationsEnvVar:
    def test_parse_positive_int_defaults_when_missing(self):
        assert _parse_positive_int(None, default=15, max_value=50) == 15

    def test_parse_positive_int_defaults_when_non_integer(self):
        assert _parse_positive_int("banana", default=15, max_value=50) == 15
        assert _parse_positive_int("", default=15, max_value=50) == 15

    def test_parse_positive_int_defaults_when_below_min(self):
        assert _parse_positive_int("0", default=15, max_value=50) == 15
        assert _parse_positive_int("-5", default=15, max_value=50) == 15

    def test_parse_positive_int_clamps_above_max(self):
        # Rather than falling back to the default (which would confuse
        # a caller who set a very large value on purpose), clamp to the
        # ceiling.
        assert _parse_positive_int("999", default=15, max_value=50) == 50

    def test_parse_positive_int_valid_value(self):
        assert _parse_positive_int("25", default=15, max_value=50) == 25

    def test_handler_env_override_reaches_agent(self):
        """MAX_ITERATIONS in the environment must reach the constructed
        agent's ``max_iterations`` attribute.
        """
        os.environ["MAX_ITERATIONS"] = "12"
        try:
            with patch("src.lambda_handler.CapacityDecisionAgent") as mock_agent_cls:
                mock_agent = MagicMock()
                mock_agent.run.return_value = {
                    "final_text": "ok",
                    "tool_calls": [],
                    "iterations": 1,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "stop_reason": "end_turn",
                    "confidence_assessments": [],
                }
                mock_agent_cls.return_value = mock_agent
                handler(_rest_event({"query": "hi"}), _lambda_ctx())

                kwargs = mock_agent_cls.call_args.kwargs
                assert kwargs["max_iterations"] == 12
        finally:
            os.environ.pop("MAX_ITERATIONS", None)

    def test_agent_class_default_matches_handler_default(self):
        """The agent class default (10) previously disagreed with the
        handler override (15). Both must now agree on 15 so there's a
        single source of truth.
        """
        from src.agent import CapacityDecisionAgent

        with patch("src.agent.boto3.client"):
            agent = CapacityDecisionAgent(model_id="test", region="us-west-2")
        assert agent.max_iterations == 15


# ---------------------------------------------------------------------------
# CORS allowlist via ALLOWED_CORS_ORIGIN env var
# ---------------------------------------------------------------------------


class TestCorsOriginAllowlist:
    """CORS allowlist behavior under the security hardening.

    Before: default was ``"*"`` and empty string was coalesced back to ``"*"``.
    After: default is empty, which SUPPRESSES the ``Access-Control-Allow-Origin``
    header entirely. Browsers reject cross-origin requests; same-origin and
    SigV4-signed calls still succeed. Operators who need cross-origin browser
    access set the env var to a specific origin or literal ``"*"``.
    """

    def test_default_unset_suppresses_cors_header(self):
        os.environ.pop("ALLOWED_CORS_ORIGIN", None)
        headers = _response_headers()
        assert "Access-Control-Allow-Origin" not in headers

    def test_custom_origin_honored(self):
        os.environ["ALLOWED_CORS_ORIGIN"] = "https://console.example.com"
        try:
            assert (
                _response_headers()["Access-Control-Allow-Origin"]
                == "https://console.example.com"
            )
        finally:
            os.environ.pop("ALLOWED_CORS_ORIGIN", None)

    def test_explicit_wildcard_still_honored_when_set(self):
        os.environ["ALLOWED_CORS_ORIGIN"] = "*"
        try:
            # Setting literal "*" explicitly is still supported for operators
            # who want public read access. The default is secure (suppressed),
            # but explicit opt-in to wildcard works.
            assert _response_headers()["Access-Control-Allow-Origin"] == "*"
        finally:
            os.environ.pop("ALLOWED_CORS_ORIGIN", None)

    def test_empty_string_env_suppresses_cors_header(self):
        os.environ["ALLOWED_CORS_ORIGIN"] = ""
        try:
            # Empty string matches the stack's default and suppresses the
            # CORS header. This is the secure posture for a public reference
            # implementation.
            headers = _response_headers()
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            os.environ.pop("ALLOWED_CORS_ORIGIN", None)

    def test_whitespace_only_env_suppresses_cors_header(self):
        os.environ["ALLOWED_CORS_ORIGIN"] = "   "
        try:
            # Whitespace is stripped before the empty check so a trailing
            # space in a parameter override cannot accidentally re-enable
            # cross-origin access.
            headers = _response_headers()
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            os.environ.pop("ALLOWED_CORS_ORIGIN", None)

    def test_template_exposes_cors_parameter(self, template: dict):
        params = template["Parameters"]
        assert "AllowedCorsOrigin" in params
        # Security hardening: default is empty, which suppresses
        # the Access-Control-Allow-Origin header entirely. Operators must
        # explicitly opt in to cross-origin access with a specific origin
        # (recommended) or literal "*" (public read access).
        assert params["AllowedCorsOrigin"]["Default"] == ""
        env = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Environment"
        ]["Variables"]
        assert env["ALLOWED_CORS_ORIGIN"] == {"Fn::Ref": "AllowedCorsOrigin"}


# ---------------------------------------------------------------------------
# OPTIONS preflight short-circuit
# ---------------------------------------------------------------------------


class TestCorsPreflight:
    def test_is_cors_preflight_detects_rest_event(self):
        assert _is_cors_preflight({"httpMethod": "OPTIONS"}) is True
        assert _is_cors_preflight({"httpMethod": "options"}) is True
        assert _is_cors_preflight({"httpMethod": "POST"}) is False

    def test_is_cors_preflight_detects_v2_event(self):
        assert (
            _is_cors_preflight({"requestContext": {"http": {"method": "OPTIONS"}}})
            is True
        )
        assert (
            _is_cors_preflight({"requestContext": {"http": {"method": "GET"}}}) is False
        )

    def test_is_cors_preflight_safe_on_malformed(self):
        assert _is_cors_preflight({}) is False
        assert _is_cors_preflight({"httpMethod": None}) is False
        assert _is_cors_preflight("not a dict") is False  # type: ignore[arg-type]

    def test_handler_returns_204_on_options(self):
        resp = handler(_rest_event({}, method="OPTIONS"), _lambda_ctx())
        assert resp["statusCode"] == 204
        assert resp["body"] == ""
        # Preflight returns the CORS method/header advertisement regardless
        # of ALLOWED_CORS_ORIGIN. The Access-Control-Allow-Origin header is
        # only emitted when ALLOWED_CORS_ORIGIN is set (security
        # hardening). Browsers interpret a missing Allow-Origin as "no
        # cross-origin access", which is the secure default.
        headers = resp["headers"]
        assert "Access-Control-Allow-Methods" in headers

    def test_handler_204_emits_cors_origin_when_configured(self):
        """When ALLOWED_CORS_ORIGIN is set, the OPTIONS preflight response
        includes the Access-Control-Allow-Origin header so cross-origin
        browser calls work for the configured origin."""
        os.environ["ALLOWED_CORS_ORIGIN"] = "https://console.example.com"
        try:
            resp = handler(_rest_event({}, method="OPTIONS"), _lambda_ctx())
            assert resp["statusCode"] == 204
            headers = resp["headers"]
            assert (
                headers["Access-Control-Allow-Origin"] == "https://console.example.com"
            )
            assert "Access-Control-Allow-Methods" in headers
        finally:
            os.environ.pop("ALLOWED_CORS_ORIGIN", None)


# ---------------------------------------------------------------------------
# Security response headers on every response
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    _EXPECTED = {
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    def test_response_headers_include_all_security_headers(self):
        headers = _response_headers()
        for name, value in self._EXPECTED.items():
            assert headers.get(name) == value

    def test_success_response_carries_security_headers(self):
        mock_result = {
            "final_text": "ok",
            "tool_calls": [],
            "iterations": 1,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
            "confidence_assessments": [],
        }
        with patch("src.lambda_handler._run_agent", return_value=mock_result):
            resp = handler(_rest_event({"query": "hi"}), _lambda_ctx())

        assert resp["statusCode"] == 200
        headers = resp["headers"]
        for name, value in self._EXPECTED.items():
            assert headers[name] == value, (
                f"success response missing security header {name!r}"
            )

    def test_error_response_carries_security_headers(self):
        # Missing query → 400
        resp = handler(_rest_event({}), _lambda_ctx())
        assert resp["statusCode"] == 400
        for name, value in self._EXPECTED.items():
            assert resp["headers"][name] == value, (
                f"error response missing security header {name!r}"
            )

    def test_preflight_response_carries_security_headers(self):
        resp = handler(_rest_event({}, method="OPTIONS"), _lambda_ctx())
        for name, value in self._EXPECTED.items():
            assert resp["headers"][name] == value
