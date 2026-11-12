# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AWS Lambda handler for the Capacity Planning Agent."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from agent import CapacityDecisionAgent
from aws_session import assume_role, use_session
from metrics import ToolMetricsEmitter
from secrets_manager import SecretsRetrievalError, get_secrets
from tool_cache import ToolCache
from tools import ALL_TOOLS

# ``config`` is a first-party module in ``src/`` — bare import works in the
# Lambda runtime (which runs from ``src/`` as the working directory). Tests
# import via ``from src.lambda_handler import handler``, so fall back to the
# ``src.config`` path when the bare import fails.
try:
    from config import (  # type: ignore[no-redef]
        ALLOWED_BEDROCK_MODEL_IDS,
        BEDROCK_MODEL_ID,
        resolve_model_id,
    )
except ImportError:  # pragma: no cover - only fires in test/dev contexts
    from src.config import (  # type: ignore[no-redef]
        ALLOWED_BEDROCK_MODEL_IDS,
        BEDROCK_MODEL_ID,
        resolve_model_id,
    )

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_BODY_BYTES = 4096
MAX_QUERY_LENGTH = 2000
BLOCKED_PATTERNS = [
    # Direct instruction overrides. Pre-filter only; the proper defense
    # is BEDROCK_GUARDRAIL_ID. Low false-positive risk for a capacity-
    # planning agent: these phrases almost never appear in a genuine
    # GPU-capacity question.
    "ignore previous",
    "ignore all instructions",
    "ignore the above",
    "disregard the above",
    "disregard previous",
    "forget your instructions",
    "forget all instructions",
    "new instructions:",
    "you are now",
    "pretend you are",
    "act as if you",
    # Role / channel confusion.
    "system:",
    "assistant:",
    "\n\nsystem",
    "```system",
    "###system",
    "<|system|>",
]

# IAM role ARN format. Matches every AWS partition (commercial,
# GovCloud, China, ISO) with a 12-digit account and a role path bounded
# to IAM's actual 64-character limit. The previous regex hardcoded
# `arn:aws:` which silently broke every cross-account request from
# GovCloud (arn:aws-us-gov:) or China
# (arn:aws-cn:) deployments.
_ROLE_ARN_PATTERN = re.compile(
    r"^arn:aws(?:-us-gov|-cn|-iso|-iso-b)?:iam::"
    r"(?P<account_id>\d{12}):role/[\w+=,.@\-_/]{1,64}$"
)

# Session-name format accepted by STS AssumeRole. Letters, digits, =, ,, ., @, -, _.
_SESSION_NAME_PATTERN = re.compile(r"^[\w+=,.@\-]{2,64}$")

# Cold-start secrets retrieval.
# Secrets are fetched once at module load and cached for the lifetime of the
# execution environment. If retrieval fails, the error is stored and the handler
# returns 503 on every invocation until the environment is recycled.
#
# The stack ships the secret as an empty JSON object (``{}``) by default —
# the Lambda code path does not read any specific keys from it today, so
# empty-is-fine. As an emergency stop, operators can set the payload to
# ``{"__unconfigured__": true}`` via ``aws secretsmanager update-secret``;
# the sentinel check below detects that and returns 503 on every invocation
# until the sentinel is cleared and the execution environment is recycled.
# Useful during scheduled maintenance or credential rotation windows.
_secrets_init_error: Optional[SecretsRetrievalError] = None
_secrets_unconfigured: bool = False

_SECRET_ARN = os.environ.get("SECRET_ARN", "")
if _SECRET_ARN:
    try:
        _fetched_secrets = get_secrets(_SECRET_ARN)
        if (
            isinstance(_fetched_secrets, dict)
            and _fetched_secrets.get("__unconfigured__") is True
        ):
            _secrets_unconfigured = True
            logger.error(
                json.dumps(
                    {
                        "event": "secrets_unconfigured",
                        "secret_id": _SECRET_ARN,
                        "message": (
                            "Stack-managed secret still holds the sentinel "
                            "value; operator must replace it via "
                            "'aws secretsmanager update-secret --secret-id <arn> "
                            "--secret-string <json>' before the Lambda can serve traffic."
                        ),
                    }
                )
            )
    except SecretsRetrievalError as exc:
        _secrets_init_error = exc
        logger.error(
            json.dumps(
                {
                    "event": "secrets_retrieval_failed",
                    "secret_id": exc.secret_id,
                    "reason": exc.reason,
                    "message": "Failed to retrieve secrets at cold start after retries",
                }
            )
        )


def _parse_positive_int(raw: Optional[str], *, default: int, max_value: int) -> int:
    """Parse a positive int env-var value, falling back to ``default`` on
    invalid / missing / out-of-range inputs. Values above ``max_value`` are
    clamped down rather than falling back — a caller who wrote "MAX_ITERATIONS=1000"
    almost certainly meant "give me as many as you'll allow" and returning
    the ceiling is more useful than silently reverting to the default.
    """
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (ValueError, TypeError):
        return default
    if parsed < 1:
        return default
    return min(parsed, max_value)


def _require_external_id() -> bool:
    """Return True when the stack's ``REQUIRE_EXTERNAL_ID`` env var is set
    to any truthy string. Defaults to True — the safe posture.

    Any value other than a case-insensitive ``"false"``, ``"0"``, or
    empty string is treated as True. This tolerates SAM's habit of
    surfacing the raw parameter string.
    """
    raw = os.environ.get("REQUIRE_EXTERNAL_ID", "true")
    return raw.strip().lower() not in {"false", "0", ""}


def _is_cors_preflight(event: dict) -> bool:
    """Detect a CORS preflight OPTIONS request across both event shapes.

    API Gateway v1 (REST) surfaces the method as ``event['httpMethod']``.
    Function URL / API Gateway v2 (HTTP) uses
    ``event['requestContext']['http']['method']``. Return True for either
    shape when the method is OPTIONS.
    """
    if not isinstance(event, dict):
        return False
    method = event.get("httpMethod")
    if not method:
        ctx = event.get("requestContext") or {}
        method = (ctx.get("http") or {}).get("method")
    return isinstance(method, str) and method.upper() == "OPTIONS"


def _is_function_url_event(event: dict) -> bool:
    """Detect if the event is from a Lambda Function URL (payload format 2.0)."""
    request_context = event.get("requestContext", {})
    # Function URL events have requestContext.http and no httpMethod at top level
    return "http" in request_context and "httpMethod" not in event


def _extract_caller_id(event: dict) -> str:
    """Extract caller ID: IAM ARN > API Key ID > Cognito sub > 'anonymous'.

    Handles both API Gateway (v1) and Function URL (v2) event formats.

    This function returns the RAW caller identifier. Callers writing to
    persistent logs / metrics MUST hash it via ``_stable_caller_hash``
    to avoid leaking IAM ARNs and API key IDs to anyone with log-read
    access.
    """
    ctx = event.get("requestContext", {})

    # Function URL with IAM auth (payload format 2.0)
    iam_context = ctx.get("authorizer", {}).get("iam", {})
    if iam_context:
        arn = iam_context.get("userArn")
        if arn:
            return arn

    # API Gateway IAM auth (payload format 1.0)
    iam_arn = ctx.get("identity", {}).get("userArn")
    if iam_arn:
        return iam_arn
    # API Key
    api_key_id = ctx.get("identity", {}).get("apiKeyId")
    if api_key_id:
        return api_key_id
    # Cognito
    claims = ctx.get("authorizer", {}).get("claims", {})
    cognito_sub = claims.get("sub")
    if cognito_sub:
        return cognito_sub
    return "anonymous"


def _extract_caller_type(event: dict) -> str:
    """Return a coarse-grained caller type: iam | api_key | cognito | anonymous.

    Used alongside ``_stable_caller_hash`` so operators can group usage
    logs by authentication mode without needing the raw caller identifier.
    """
    ctx = event.get("requestContext", {})
    # Function URL with IAM auth (payload format 2.0)
    if ctx.get("authorizer", {}).get("iam", {}).get("userArn"):
        return "iam"
    # API Gateway IAM auth (payload format 1.0)
    if ctx.get("identity", {}).get("userArn"):
        return "iam"
    if ctx.get("identity", {}).get("apiKeyId"):
        return "api_key"
    if ctx.get("authorizer", {}).get("claims", {}).get("sub"):
        return "cognito"
    return "anonymous"


def _stable_caller_hash(caller_id: str) -> str:
    """Return a stable, non-reversible identifier for a raw caller ID.

    The full SHA-256 is truncated to 16 hex chars (64 bits) — enough to
    keep collision risk negligible at the workload volumes this agent
    sees (millions of unique callers), while keeping log lines short.

    ``"anonymous"`` passes through unchanged: it carries no PII and
    keeping it human-readable helps triage.
    """
    if caller_id == "anonymous":
        return "anonymous"
    return hashlib.sha256(caller_id.encode("utf-8")).hexdigest()[:16]


def _is_safe_input(query: str) -> bool:
    """Check query against known prompt injection patterns (case-insensitive)."""
    lower = query.lower()
    return not any(pattern in lower for pattern in BLOCKED_PATTERNS)


def _parse_customer_context(
    body: dict, request_id: str
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Pull and validate the optional customer_context block from the request body.

    Returns a tuple of (parsed_context, error_response). Exactly one is non-None.
    When parsed_context is None and error_response is None, no customer_context
    was supplied and the agent should run against the deploy account.

    A well-formed parsed_context dict has:
        role_arn       : validated against _ROLE_ARN_PATTERN
        account_id     : extracted from the role ARN, for logging
        external_id    : optional shared secret (passed through to STS)
        session_name   : caller-supplied or defaulted to "agent-<request_id>"
    """
    raw = body.get("customer_context")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, _error_response(
            400, "customer_context must be an object", request_id
        )

    role_arn = raw.get("role_arn")
    if not isinstance(role_arn, str) or not role_arn:
        return None, _error_response(
            400, "customer_context.role_arn is required", request_id
        )

    match = _ROLE_ARN_PATTERN.match(role_arn)
    if not match:
        return None, _error_response(
            400, "customer_context.role_arn is not a valid IAM role ARN", request_id
        )
    account_id = match.group("account_id")

    external_id = raw.get("external_id")
    if external_id is not None and not isinstance(external_id, str):
        return None, _error_response(
            400, "customer_context.external_id must be a string", request_id
        )

    # Enforce ExternalId when REQUIRE_EXTERNAL_ID=true (stack default).
    # This closes the confused-deputy hole where a compromised deploy-account
    # principal could assume any customer role whose trust policy names the
    # Lambda role, without the shared secret handshake.
    if _require_external_id() and not (isinstance(external_id, str) and external_id):
        return None, _error_response(
            400,
            "customer_context.external_id is required (stack policy)",
            request_id,
        )

    session_name = raw.get("session_name") or f"agent-{request_id[:32]}"
    if not isinstance(session_name, str) or not _SESSION_NAME_PATTERN.match(
        session_name
    ):
        return None, _error_response(
            400,
            "customer_context.session_name must match [a-zA-Z0-9+=,.@_-]{2,64}",
            request_id,
        )

    return (
        {
            "role_arn": role_arn,
            "account_id": account_id,
            "external_id": external_id,
            "session_name": session_name,
        },
        None,
    )


def _assume_customer_role(parsed_context: dict[str, Any], request_id: str):
    """Assume the customer-supplied role. Returns (session, error_response)."""
    try:
        session = assume_role(
            role_arn=parsed_context["role_arn"],
            session_name=parsed_context["session_name"],
            external_id=parsed_context.get("external_id"),
        )
        return session, None
    except Exception as exc:  # noqa: BLE001
        # Avoid leaking STS detail to callers; log specifics for operators.
        error_code = ""
        try:
            error_code = exc.response.get("Error", {}).get("Code", "")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            error_code = type(exc).__name__

        logger.warning(
            json.dumps(
                {
                    "event": "assume_role_failed",
                    "request_id": request_id,
                    "customer_account_id": parsed_context.get("account_id"),
                    "error_code": error_code,
                }
            )
        )

        if error_code in ("AccessDenied", "AccessDeniedException"):
            return None, _error_response(
                403,
                "Cross-account access denied; check the customer role's trust policy",
                request_id,
            )
        if error_code in (
            "ValidationError",
            "MalformedPolicyDocument",
            "InvalidParameterValue",
        ):
            return None, _error_response(
                400, "Cross-account role configuration is invalid", request_id
            )
        return None, _error_response(
            502, "Cross-account session could not be established", request_id
        )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point. Accepts a JSON body with a 'query' field."""
    request_id = getattr(context, "aws_request_id", "unknown")
    start_time = time.time()

    # CORS preflight (OPTIONS) short-circuit. Browsers send this
    # before every cross-origin POST when custom headers are present
    # (e.g. Authorization, x-api-key). The old handler had no OPTIONS
    # path and would fall through to body validation → 400, breaking
    # any browser-facing use. Return 204 with the CORS headers instead.
    if _is_cors_preflight(event):
        return {
            "statusCode": 204,
            "headers": _response_headers(),
            "body": "",
        }

    # Initialize these up here so the ``except Exception`` block below
    # can reference them even if the failure fires before they're
    # assigned inside the try. Keeps the usage_tracking_failed log line
    # emittable regardless of where the exception landed.
    resolved_model_id = ""
    customer_account_id: Optional[str] = None

    # If secrets retrieval failed at cold start, return 503 immediately
    if _secrets_init_error is not None:
        return _error_response(
            503, "Service unavailable: secrets retrieval failed", request_id
        )

    # If the stack-managed secret still holds the sentinel value, refuse to
    # serve traffic — the operator hasn't rotated the placeholder yet. Log
    # the correlation ID but keep the client-facing message generic.
    if _secrets_unconfigured:
        logger.warning(
            json.dumps(
                {
                    "event": "unconfigured_secret_request_refused",
                    "request_id": request_id,
                }
            )
        )
        return _error_response(
            503,
            "Service unavailable: operator configuration incomplete",
            request_id,
        )

    try:
        # Body size validation — Function URL events may base64-encode the body
        raw_body = event.get("body", "") or ""
        if event.get("isBase64Encoded", False):
            try:
                raw_body = base64.b64decode(raw_body).decode("utf-8")
            except Exception:
                return _error_response(400, "Invalid base64-encoded body", request_id)
        if len(raw_body.encode("utf-8")) > MAX_BODY_BYTES:
            return _error_response(400, "Request body exceeds 4KB limit", request_id)

        # JSON parsing
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return _error_response(400, "Invalid JSON body", request_id)

        # Query extraction + validation
        raw_query = body.get("query", "")
        if not isinstance(raw_query, str):
            return _error_response(
                400, "Missing 'query' field in request body", request_id
            )

        # Prompt injection check (before strip to catch patterns with leading whitespace)
        if not _is_safe_input(raw_query):
            return _error_response(400, "Query contains blocked content", request_id)

        query = raw_query.strip()
        if not query:
            return _error_response(
                400, "Missing 'query' field in request body", request_id
            )
        if len(query) > MAX_QUERY_LENGTH:
            return _error_response(
                400, "Query exceeds 2000 character limit", request_id
            )

        # Optional per-request model override.
        # Callers can supply ``"model_id": "..."`` in the request body to
        # escalate a hard query to a stronger model (e.g. Sonnet 4.5) or
        # switch families (e.g. Nova Pro for a sandbox that blocks
        # Anthropic Marketplace subscriptions). Requests omitting
        # ``model_id`` get the stack default (Haiku 4.5). Requests with a
        # ``model_id`` NOT on the ``ALLOWED_BEDROCK_MODEL_IDS`` allowlist
        # are rejected with 400 so IAM boundary can't be broadened via
        # the request path.
        requested_model_id = body.get("model_id")
        resolved_model_id, model_err = resolve_model_id(requested_model_id)
        if model_err is not None:
            return _error_response(400, model_err, request_id)

        # Optional cross-account customer context.
        # When supplied, the agent's AWS API calls run against the customer's
        # account via an assumed role. When absent, behavior is unchanged and
        # all calls use the Lambda execution role.
        parsed_customer_ctx, parse_err = _parse_customer_context(body, request_id)
        if parse_err is not None:
            return parse_err

        customer_session = None
        customer_account_id: Optional[str] = None
        if parsed_customer_ctx is not None:
            customer_session, assume_err = _assume_customer_role(
                parsed_customer_ctx, request_id
            )
            if assume_err is not None:
                return assume_err
            customer_account_id = parsed_customer_ctx["account_id"]

        # Agent execution. use_session(None) is a safe no-op when no role was
        # supplied, so both paths share a single code path.
        with use_session(customer_session):
            result = _run_agent(
                query,
                context,
                tenant_scope=customer_account_id,
                model_id=resolved_model_id,
            )

        # Structured logging
        duration_ms = int((time.time() - start_time) * 1000)
        _log_invocation(
            request_id,
            query,
            result,
            duration_ms,
            resolved_model_id,
            customer_account_id,
        )

        # Emit an EMF metric dimensioned by endpoint. Enables split p99
        # latency alarms — API Gateway has a 29s ceiling, but the Function
        # URL path can legitimately run up to 180s. A single alarm on
        # AWS/Lambda Duration would trip on every long FURL call.
        endpoint_dimension = (
            "FunctionUrl" if _is_function_url_event(event) else "ApiGateway"
        )
        try:
            _emitter_for_invocation_metric().emit_invocation_metric(
                duration_ms=float(duration_ms),
                endpoint=endpoint_dimension,
            )
        except Exception:  # noqa: BLE001  # nosec B110 - best-effort metric emission must never break the response
            # Metric emission is best-effort — never break the response.
            pass

        # Usage tracking log for per-caller chargeback. The raw caller
        # identifier (IAM ARN / API key ID / Cognito sub) is hashed
        # before writing to logs so anyone with log-read access can't
        # enumerate every caller who has used the endpoint. The paired
        # ``caller_type`` field preserves group-by capability for
        # chargeback dashboards.
        #
        # ``model_id`` records the RESOLVED model that actually served
        # the request (either the stack default or the caller-supplied
        # override), so downstream cost-attribution queries can bucket
        # spend by model without re-parsing the request body.
        caller_id = _extract_caller_id(event)
        caller_type = _extract_caller_type(event)
        request_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _emit_usage_tracking_log(
            caller_id,
            caller_type,
            result,
            resolved_model_id,
            request_timestamp,
            customer_account_id,
        )

        # Success response
        return _success_response(result, request_id, resolved_model_id)

    except Exception as exc:
        logger.exception("Unhandled error in capacity agent handler")

        # Classify recognized Bedrock errors so callers get an actionable
        # 4xx (with the Bedrock reason + concrete fallback suggestions)
        # instead of a generic 500 "Internal server error" that forces
        # them to grep Lambda logs. Falls through to 500 for truly
        # unknown exceptions.
        classified = _classify_bedrock_error(exc)
        if classified is not None:
            status_code, error_message, error_code = classified
        else:
            status_code = 500
            error_message = "Internal server error"
            error_code = type(exc).__name__

        # Emit usage_tracking_failed so operators can attribute failures
        # the same way they attribute successes (per-caller, per-model).
        # Best-effort — never let logging failure cascade into a double
        # error. If caller-id extraction itself raises, we swallow it
        # and still return the main error response.
        try:
            caller_id = _extract_caller_id(event)
            caller_type = _extract_caller_type(event)
            request_timestamp = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            _emit_usage_tracking_failed_log(
                caller_id,
                caller_type,
                resolved_model_id,
                error_code,
                request_timestamp,
                customer_account_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to emit usage_tracking_failed log")

        return _error_response(status_code, error_message, request_id)


def _run_agent(
    query: str,
    context: Any = None,
    tenant_scope: Optional[str] = None,
    model_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create and run the capacity decision agent.

    Initializes ToolCache (DynamoDB-backed) and ToolMetricsEmitter (EMF),
    then passes them to the agent for cache-aware, metrics-instrumented execution.

    Args:
        query: User query to run.
        context: Lambda context (for timeout headroom checks).
        tenant_scope: AWS account ID whose data the tools will read. Used as
            the cache tenant scope so cross-account calls never collide with
            deploy-account calls or with a different customer's calls. When
            None, the deploy account is assumed.
        model_id: Bedrock model ID / inference-profile ID to invoke. When
            None, falls back to the stack default (``BEDROCK_MODEL_ID`` env
            var, then the ``config.BEDROCK_MODEL_ID`` module constant).
            The lambda handler resolves this against the allowlist BEFORE
            calling ``_run_agent``, so the value here is always trusted.
    """
    if model_id is None:
        model_id = os.environ.get("BEDROCK_MODEL_ID", BEDROCK_MODEL_ID)
    region = os.environ.get(
        "AWS_REGION_BEDROCK", os.environ.get("AWS_REGION", "us-west-2")
    )

    # Initialize tool cache with DynamoDB table name from environment. The
    # tenant scope isolates cached results between the deploy account and any
    # assumed customer accounts to prevent cross-tenant data leaks in warm
    # containers.
    cache_table_name = os.environ.get("CACHE_TABLE_NAME", "")
    tool_cache: ToolCache | None = None
    if cache_table_name:
        tool_cache = ToolCache(
            table_name=cache_table_name,
            tenant_scope=tenant_scope,
        )

    # Initialize metrics emitter with standard namespace
    metrics_emitter = ToolMetricsEmitter(namespace="CapacityAgent/Tools")

    # Optional Bedrock Guardrails wiring. When BEDROCK_GUARDRAIL_ID is set,
    # every Converse call runs through the guardrail server-side for
    # prompt-injection / content / sensitive-information filtering. This is
    # the supported prompt-injection defense; the coarse blocklist in this
    # module is only a pre-filter for obvious attempts.
    guardrail_identifier = os.environ.get("BEDROCK_GUARDRAIL_ID") or None
    guardrail_version = os.environ.get("BEDROCK_GUARDRAIL_VERSION") or None

    # Single source of truth for max_iterations — env var with a
    # 15-iteration default that matches both the class default in
    # agent.py and the documented Solutions Library baseline. Callers
    # can raise it via MAX_ITERATIONS for adversarial multi-region asks
    # without touching code.
    max_iterations = _parse_positive_int(
        os.environ.get("MAX_ITERATIONS"), default=15, max_value=50
    )

    agent = CapacityDecisionAgent(
        model_id=model_id,
        region=region,
        max_iterations=max_iterations,
        max_tokens=4096,
        temperature=0.0,
        lambda_context=context,
        tool_cache=tool_cache,
        metrics_emitter=metrics_emitter,
        guardrail_identifier=guardrail_identifier,
        guardrail_version=guardrail_version,
    )
    for tool_spec in ALL_TOOLS:
        agent.register_tool(**tool_spec)

    return agent.run(query)


def _emitter_for_invocation_metric() -> ToolMetricsEmitter:
    """Return an EMF emitter scoped to the invocation-metrics namespace.

    Kept as a module-level helper so tests can patch it.
    """
    return ToolMetricsEmitter(namespace="CapacityAgent/Invocation")


def _log_invocation(
    request_id: str,
    query: str,
    result: dict,
    duration_ms: int,
    model_id: str,
    customer_account_id: Optional[str] = None,
) -> None:
    """Emit structured JSON log for the invocation.

    ``model_id`` is the RESOLVED model that served the request — either the
    stack default (``BEDROCK_MODEL_ID`` env var) or the caller's per-request
    override. The lambda handler validates the override against the
    allowlist BEFORE calling this, so the value is trusted.
    """
    entry = {
        "event": "agent_invocation",
        "request_id": request_id,
        "query_length": len(query),
        "tool_calls": len(result["tool_calls"]),
        "iterations": result["iterations"],
        "input_tokens": result["usage"]["input_tokens"],
        "output_tokens": result["usage"]["output_tokens"],
        "stop_reason": result["stop_reason"],
        "duration_ms": duration_ms,
        "model_id": model_id,
    }
    if customer_account_id:
        entry["customer_account_id"] = customer_account_id
    logger.info(json.dumps(entry))


def _emit_usage_tracking_log(
    caller_id: str,
    caller_type: str,
    result: dict,
    model_id: str,
    request_timestamp: str,
    customer_account_id: Optional[str] = None,
) -> None:
    """Emit structured JSON log entry for per-caller usage tracking.

    Schema (do not RENAME or REMOVE fields without updating Insights
    queries in template.yaml § OperationalDashboard and downstream
    consumers — adding new optional fields is backward-compatible):

        event               "usage_tracking"
        caller_id_hash      SHA-256 (first 16 hex chars) of the raw caller ID.
                            "anonymous" passes through unchanged.
        caller_type         iam | api_key | cognito | anonymous
        input_tokens        int
        output_tokens       int
        total_tokens        int (input + output)
        cache_read_tokens   int — Converse cacheReadInputTokens summed
                            across every Bedrock turn in the agent
                            loop. 0 when prompt caching is disabled or
                            when the request paid the write premium
                            instead of a read hit. Not counted in
                            ``input_tokens`` or ``total_tokens`` —
                            those remain the agent's existing
                            "billed-at-full-rate" aggregates.
        cache_write_tokens  int — Converse cacheWriteInputTokens summed
                            across every Bedrock turn. 0 when caching
                            is disabled.
        tool_call_count     int
        model_id            str
        request_timestamp   ISO 8601 UTC
        customer_account_id str (only present under cross-account access)

    The raw caller identifier (IAM ARN / API key ID / Cognito sub) is
    hashed before logging so anyone with log-read access cannot
    enumerate individual callers. Operators
    needing to de-anonymize for a specific investigation can compute
    the same hash locally from a known caller ID.
    """
    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = input_tokens + output_tokens
    cache_read_tokens = usage.get("cache_read_tokens", 0)
    cache_write_tokens = usage.get("cache_write_tokens", 0)
    tool_call_count = len(result.get("tool_calls", []))

    log_entry = {
        "event": "usage_tracking",
        "caller_id_hash": _stable_caller_hash(caller_id),
        "caller_type": caller_type,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "tool_call_count": tool_call_count,
        "model_id": model_id,
        "request_timestamp": request_timestamp,
    }
    if customer_account_id:
        log_entry["customer_account_id"] = customer_account_id
    logger.info(json.dumps(log_entry))


def _emit_usage_tracking_failed_log(
    caller_id: str,
    caller_type: str,
    model_id: str,
    error_code: str,
    request_timestamp: str,
    customer_account_id: Optional[str] = None,
) -> None:
    """Emit structured JSON log entry for FAILED invocations.

    Kept as a separate ``event`` type from ``usage_tracking`` so existing
    dashboard Insights queries that filter on ``event == "usage_tracking"``
    (chargeback reporting, cost attribution) keep working unchanged.
    Operators can query failures explicitly via
    ``event == "usage_tracking_failed"``, or combine both via
    ``event in ("usage_tracking", "usage_tracking_failed")``.

    Schema (fixed, like ``usage_tracking`` — do not add fields without
    updating any dashboard/Insights query that reads them):

        event               "usage_tracking_failed"
        caller_id_hash      SHA-256 (first 16 hex chars). "anonymous" passes through.
        caller_type         iam | api_key | cognito | anonymous
        model_id            resolved Bedrock model ID that was targeted.
                            May be an empty string if the failure happened
                            BEFORE allowlist resolution (e.g. body parse).
        error_code          AccessDeniedException | ThrottlingException |
                            ValidationException | Python-class-name
                            (for unrecognized exceptions). Operators grep
                            for this to see which Bedrock failures a
                            given caller is hitting without needing to
                            page through Lambda logs.
        request_timestamp   ISO 8601 UTC
        customer_account_id present only under cross-account access
    """
    log_entry = {
        "event": "usage_tracking_failed",
        "caller_id_hash": _stable_caller_hash(caller_id),
        "caller_type": caller_type,
        "model_id": model_id,
        "error_code": error_code,
        "request_timestamp": request_timestamp,
    }
    if customer_account_id:
        log_entry["customer_account_id"] = customer_account_id
    logger.info(json.dumps(log_entry))


def _classify_bedrock_error(exc: Exception) -> Optional[tuple[int, str, str]]:
    """Map recognized Bedrock exceptions to (http_status, user_message, error_code).

    Returns ``None`` if the exception isn't a Bedrock error we have a
    specific handler for — caller falls back to generic 500.

    Why: the agent Lambda's ``_run_agent`` ultimately calls
    ``bedrock.converse(...)`` via the boto3 Converse API, which raises
    ``botocore`` client errors on access/throttle/validation failures.
    Returning a generic 500 "Internal server error" to callers in those
    cases is actively unhelpful — they can't tell "my model_id is
    Marketplace-blocked" from "the agent crashed." Classify known
    error codes and surface the Bedrock error message plus actionable
    guidance; the generic 500 is now the LAST resort for truly unknown
    failures.

    Specifically for ``AccessDeniedException`` with a Marketplace reason,
    enumerate the Amazon-family (first-party, non-Marketplace) models
    on the current allowlist so callers get a concrete fallback list
    without needing to grep logs or read documentation. This failure
    mode is common in Workshop Studio-vended sandboxes where
    Marketplace subscriptions are SCP-blocked.
    """
    err_response = getattr(exc, "response", None)
    if not isinstance(err_response, dict):
        return None
    error_meta = err_response.get("Error", {})
    if not isinstance(error_meta, dict):
        return None
    code = error_meta.get("Code", "")
    message = error_meta.get("Message", "")

    if code == "AccessDeniedException":
        lowered = message.lower()
        if "aws-marketplace" in lowered or "marketplace subscription" in lowered:
            # Pull first-party Amazon alternatives from the live allowlist so
            # the fallback list stays accurate even if the operator customizes
            # ``AllowedBedrockModelIds``.
            amazon_models = [m for m in ALLOWED_BEDROCK_MODEL_IDS if ".amazon." in m]
            fallback = (
                ", ".join(amazon_models)
                if amazon_models
                else "an Amazon Nova model via the US cross-region inference profile"
            )
            user_message = (
                f"Bedrock model access denied. {message} "
                f"Likely cause: this model requires an AWS Marketplace "
                f"subscription and the account doesn't permit it (common "
                f"in Workshop Studio-vended sandboxes with SCP-level "
                f"Marketplace restrictions). Try an Amazon-family model "
                f"from the allowlist instead: {fallback}."
            )
            return 403, user_message, code
        return 403, f"Bedrock model access denied: {message}", code

    if code == "ThrottlingException":
        return (
            429,
            f"Bedrock throttled this request: {message} "
            "Retry with exponential backoff, or request a model throughput "
            "quota increase.",
            code,
        )

    if code == "ValidationException":
        return 400, f"Bedrock validation error: {message}", code

    return None


# Advisory disclaimer emitted on EVERY response body (success or
# error). The verdict labels (DEPLOYABLE / DEPLOYABLE WITH PREPARATION /
# HIGH RISK / NOT FEASIBLE) are actionable-sounding. This disclaimer is
# the machine-readable place downstream automations must check before
# treating a "DEPLOYABLE" verdict as a capacity commitment (it is not).
# See also the human-readable "Known Limitations & When NOT to Use"
# section in README.md.
_ADVISORY_DISCLAIMER = (
    "This response provides best-effort signals from public AWS APIs and, "
    "when cross-account access is configured, the customer's own CloudTrail "
    "and Service Quotas. It is NOT a capacity commitment from AWS. Verdicts "
    "such as 'DEPLOYABLE' are advisory. For strategic deployments (>= 100 "
    "instances, limited-capacity regions, tight timelines, or regulated "
    "workloads), confirm capacity with AWS Support and your account team "
    "before acting on the recommendation."
)


def _response_headers() -> dict[str, str]:
    """Assemble the common response headers.

    Includes:
    - Content-Type.
    - CORS headers. ``Access-Control-Allow-Origin`` is emitted only when
      ``ALLOWED_CORS_ORIGIN`` is set to a non-empty value. The stack
      ships the parameter blank, which suppresses the header entirely
      (secure default for a public reference implementation). Browsers
      will reject cross-origin requests; same-origin and SigV4-signed
      calls still succeed. Operators who need cross-origin browser
      access set the parameter to a specific origin
      (e.g. ``https://console.example.com``) or to literal ``*`` for
      intentional public read access.
    - Security headers: HSTS to force TLS on future visits,
      ``X-Content-Type-Options: nosniff`` to prevent MIME sniffing, and
      ``Referrer-Policy: no-referrer`` to stop the browser from leaking
      the API URL to third-party sites.
    """
    allowed_origin = os.environ.get("ALLOWED_CORS_ORIGIN", "").strip()
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Headers": (
            "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"
        ),
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        # HSTS: 1 year, subdomains, preload-eligible. Only meaningful over
        # HTTPS, which API Gateway and Function URL both enforce.
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if allowed_origin:
        headers["Access-Control-Allow-Origin"] = allowed_origin
    return headers


def _success_response(
    result: dict, request_id: str, model_id: str = ""
) -> dict[str, Any]:
    """Build a 200 response with agent results and correlation ID.

    ``model_id`` is echoed back in the response body so callers can
    confirm which model actually served the request (helpful when the
    caller supplied a ``model_id`` override — they get to see exactly
    the model the Lambda resolved to). Defaults to an empty string for
    backward compatibility with callers that don't pass it.
    """
    stop_reason = result.get("stop_reason", "end_turn")
    incomplete = stop_reason != "end_turn"

    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = input_tokens + output_tokens
    budget_limit = result.get("token_budget_limit", 20000)

    tool_outcomes = result.get("tool_outcomes", {"succeeded": [], "failed": []})

    return {
        "statusCode": 200,
        "headers": _response_headers(),
        "body": json.dumps(
            {
                "request_id": request_id,
                "answer": result["final_text"],
                "model_id": model_id,
                "tool_calls": len(result["tool_calls"]),
                "iterations": result["iterations"],
                "incomplete": incomplete,
                "stop_reason": stop_reason,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "budget_limit": budget_limit,
                },
                "tool_outcomes": tool_outcomes,
                "confidence_assessments": result.get("confidence_assessments", []),
                "disclaimer": _ADVISORY_DISCLAIMER,
            }
        ),
    }


def _error_response(status_code: int, message: str, request_id: str) -> dict[str, Any]:
    """Build an error response with correlation ID."""
    return {
        "statusCode": status_code,
        "headers": _response_headers(),
        "body": json.dumps(
            {
                "request_id": request_id,
                "error": message,
                "disclaimer": _ADVISORY_DISCLAIMER,
            }
        ),
    }
