# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Cross-account / per-invocation boto3 session resolution.

For strategic customer use, the agent often needs to read from a customer's
AWS account — their quotas, their existing ODCRs, their CloudTrail — not the
deploy account where the Lambda lives. To support this without rewriting each
tool's client wiring, every tool fetches its boto3 client through
``get_client()`` from this module.

Default behavior (no session bound): ``get_client()`` returns a client backed
by the Lambda execution role / default credential chain — same as before this
module existed.

Cross-account behavior: the Lambda handler calls ``assume_role()`` against a
caller-supplied role ARN, then wraps ``agent.run()`` in
``with use_session(role_session):``. All tool boto3 calls inside the block
use the assumed-role credentials. Outside the block, the agent is back to
the deploy account.

Session binding is held in a ``ContextVar`` so it's per-invocation safe (no
cross-talk between concurrent Lambda invocations sharing the same warm
container's threads).

This module intentionally does NOT cache the boto3 clients. Caching is a
foot-gun across cross-account boundaries: a warm Lambda container would
otherwise hand the next invocation a client carrying the previous customer's
credentials. boto3's own internal service-model cache keeps per-call client
construction in the low milliseconds.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

import boto3
from botocore.config import Config

_current_session: ContextVar[Optional[boto3.Session]] = ContextVar(
    "aws_session_current", default=None
)


# --------------------------------------------------------------------------
# CloudTrail rate limiter (F9)
#
# CloudTrail LookupEvents is throttled to ~2 requests/second per account.
# On a Lambda that runs many capacity queries concurrently we need to
# refuse excess calls LOCALLY rather than let them fire and eat the
# service-side throttle (which returns a ThrottlingException the ICE
# tool would then log as a failure and treat as ``capacity_signal =
# 'rate_limited'``, correctly refusing DEPLOYABLE — but at the cost of
# a wasted round-trip and a wasted retry).
#
# The token bucket below is process-local: one bucket per warm Lambda
# container. In practice the account-wide budget is shared across all
# containers, so this bucket sizes conservatively (well under the
# service's 2 req/s) to keep any single container from consuming the
# whole account budget on its own.
# --------------------------------------------------------------------------


class _TokenBucket:
    """Simple thread-safe token bucket. ``try_acquire`` returns True if a
    token was available and consumed, False otherwise — the caller decides
    whether to sleep-and-retry, fail fast, or skip the operation.
    """

    def __init__(self, rate_per_second: float, capacity: float):
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("rate_per_second and capacity must be positive")
        self._rate = float(rate_per_second)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Attempt to acquire ``tokens`` in a non-blocking way. Returns
        True on success (tokens consumed), False when the bucket is
        empty.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._last_refill = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class CloudTrailRateLimitedError(RuntimeError):
    """Raised when the local CloudTrail token bucket refuses a call. The
    ICE-history tool catches this and returns
    ``capacity_signal="rate_limited"`` in its response; the
    deterministic scorer then refuses to return DEPLOYABLE (see
    ``ice_signal_unavailable`` cap in ``src/confidence.py``).
    """


# Conservative default — 1 req/s sustained with a burst of 2. The
# service-side limit is ~2 req/s per account; a single warm container
# consuming 1 leaves headroom for other tenants and other containers.
_CLOUDTRAIL_BUCKET = _TokenBucket(rate_per_second=1.0, capacity=2.0)


class _RateLimitedCloudTrailClient:
    """Thin wrapper around a boto3 CloudTrail client that gates
    ``lookup_events`` calls through the local token bucket.

    Every other method proxies through unchanged.
    """

    def __init__(self, inner):
        self._inner = inner

    def lookup_events(self, **kwargs):
        if not _CLOUDTRAIL_BUCKET.try_acquire():
            raise CloudTrailRateLimitedError(
                "CloudTrail LookupEvents rate limit refused this call to "
                "protect the account-wide 2 req/s budget. Retry with lower "
                "concurrency; do NOT treat this as a clean-history signal."
            )
        return self._inner.lookup_events(**kwargs)

    def __getattr__(self, name):
        # Proxy every other attribute (paginator, meta, etc.) to the
        # underlying client so downstream code that uses features other
        # than lookup_events keeps working.
        return getattr(self._inner, name)


# Retry configuration applied to every boto3 client returned by get_client().
#
# We use ``adaptive`` mode so botocore respects service-side rate-limit hints
# (throttling with retry-after) and backs off adaptively rather than fighting
# the throttle. ``max_attempts=4`` = 1 initial + 3 retries, chosen to keep the
# worst-case tool latency bounded (roughly 8s of backoff) so we don't blow the
# 29s API Gateway timeout on a single tool call.
#
# This is the fix for the previously-dead outer retry: the tool handlers
# catch broad exceptions and return structured error dicts, so exceptions
# never reached the agent-level ``with_retry``. Configuring retries here
# puts them at the layer where they actually get invoked — the boto3 API
# call itself.
_BOTOCORE_RETRY_CONFIG = Config(
    retries={"max_attempts": 4, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=15,
)


def assume_role(
    role_arn: str,
    session_name: str,
    external_id: Optional[str] = None,
    duration_seconds: int = 900,
) -> boto3.Session:
    """Assume an IAM role and return a boto3.Session bound to the temp creds.

    Args:
        role_arn: ARN of the role to assume. Must be in a format STS accepts.
        session_name: A caller-controlled name visible in CloudTrail on the
            customer side. Keep it short and identifying (e.g. the IAM user
            or workflow that issued the request).
        external_id: Optional shared secret required by the role's trust
            policy. Strongly recommended for cross-account roles used by
            third parties.
        duration_seconds: Session validity in seconds. Clamped to the STS
            allowed range [900, 43200] (15 minutes to 12 hours). The actual
            ceiling depends on the role's MaxSessionDuration setting.

    Returns:
        A boto3.Session pre-configured with the temporary credentials.

    Raises:
        Whatever STS raises on failure (botocore.exceptions.ClientError for
        AccessDenied, malformed ARN, missing ExternalId, etc.). Callers are
        expected to handle these.
    """
    sts = boto3.client("sts")
    kwargs: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": int(max(900, min(int(duration_seconds), 43200))),
    }
    if external_id:
        kwargs["ExternalId"] = external_id

    resp = sts.assume_role(**kwargs)
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


@contextmanager
def use_session(session: Optional[boto3.Session]) -> Iterator[None]:
    """Bind a session as the current session for the duration of the block.

    Passing ``None`` is allowed and is a no-op-style call (no session bound,
    ``get_client()`` returns default-credential clients). Useful for code
    that wants a single code path whether or not cross-account is in use.
    """
    token = _current_session.set(session)
    try:
        yield
    finally:
        _current_session.reset(token)


def get_client(
    service_name: str,
    region_name: Optional[str] = None,
    *,
    use_deploy_account: bool = False,
):
    """Return a boto3 client honoring the currently-bound session.

    Inside a ``use_session(role_session)`` block, the client uses the assumed
    role's temporary credentials. Otherwise, it uses the default credential
    chain (Lambda execution role in production).

    Every client is constructed with ``_BOTOCORE_RETRY_CONFIG`` so transient
    errors (throttling, 5xx, connection timeouts) are retried at the boto3
    layer. Tool handlers catch the residual exception after retries have been
    exhausted and return a structured error dict.

    Args:
        service_name: AWS service name (e.g. ``"ec2"``, ``"pricing"``).
        region_name: Optional region override.
        use_deploy_account: When True, ignore the currently-bound customer
            session and always return a client for the Lambda's own
            execution role. Used by tools that read data which is NOT
            customer-scoped — e.g. the AWS Pricing API, whose response is
            identical regardless of which account calls it, so routing it
            through the customer's role wastes an STS call and forces the
            customer to grant ``pricing:GetProducts`` in their trust policy.

    No client caching — see module docstring for rationale.
    """
    session = _current_session.get() if not use_deploy_account else None
    if session is not None:
        if region_name is not None:
            client = session.client(
                service_name, region_name=region_name, config=_BOTOCORE_RETRY_CONFIG
            )
        else:
            client = session.client(service_name, config=_BOTOCORE_RETRY_CONFIG)
    elif region_name is not None:
        client = boto3.client(
            service_name, region_name=region_name, config=_BOTOCORE_RETRY_CONFIG
        )
    else:
        client = boto3.client(service_name, config=_BOTOCORE_RETRY_CONFIG)

    # Wrap CloudTrail clients so ``lookup_events`` is gated by the
    # local token bucket. Refusals surface as CloudTrailRateLimitedError,
    # which ``src/tools/ice_history.py`` catches and returns as
    # ``capacity_signal="rate_limited"``.
    if service_name == "cloudtrail":
        return _RateLimitedCloudTrailClient(client)
    return client


def current_session() -> Optional[boto3.Session]:
    """Return the currently-bound session, or None if no session is active.

    Exposed primarily for diagnostics and for tests that need to assert on
    the binding state without poking the private ContextVar.
    """
    return _current_session.get()
