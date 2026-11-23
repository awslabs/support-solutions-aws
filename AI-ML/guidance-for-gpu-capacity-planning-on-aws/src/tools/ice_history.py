# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Customer-side capacity-error history via CloudTrail.

The tool is the primary ground-truth signal for whether a fleet is likely
to land in the customer's account. Different deployment targets emit
their capacity failures under different CloudTrail namespaces:

- **EC2-managed fleets** (self-managed EC2, EKS-on-EC2, Karpenter with the
  data-plane role in this account): failures show up as ``RunInstances``
  management events with error codes like ``InsufficientInstanceCapacity``
  / ``InsufficientHostCapacity``.
- **SageMaker-managed endpoints** (real-time, async, serverless, batch):
  SageMaker provisions instances inside its own service account and the
  customer's CloudTrail records the SageMaker API calls, NOT ``RunInstances``.
  Capacity failures surface as ``CreateEndpoint`` / ``UpdateEndpoint`` /
  ``UpdateEndpointWeightsAndCapacities`` events with ``eventSource =
  sagemaker.amazonaws.com`` and error codes such as ``ResourceLimitExceeded``
  or ``CapacityError``.

The prior version of this tool queried ONLY ``RunInstances``. For customers
whose workload is SageMaker (the primary use case documented in the
project README), the tool returned ``event_count=0`` and mislabeled the
capacity signal as "clean" even while the customer was drowning in
``CreateEndpoint`` capacity failures. That defect is why the tool now
requires a ``deployment_target`` and runs the correct query per target.

Data-source caveats this tool acknowledges:

- Default trails retain 90 days of management events. Customers without a
  custom multi-region trail get roughly 90 days of history; older data is
  gone. ``lookback_days`` is clamped to that ceiling.
- CloudTrail LookupEvents does NOT accept ``errorCode`` as a lookup
  attribute (see the AWS API docs). Any error filtering must happen
  client-side after fetching an unfiltered event stream. That means an
  active fleet with high launch volume can drown the first few pages in
  successful launches. See ``aws_session.py`` for the account-level
  CloudTrail rate limiter; the ``truncated`` flag on the response
  surfaces when the paging cap was reached.
- LookupEvents is per-region. A query for a customer's Frankfurt fleet
  cannot see events in Tokyo.
- LookupEvents throughput is limited to ~2 req/s per account. When the
  agent runs many capacity queries concurrently, the shared token bucket
  in ``aws_session.py`` refuses excess calls; the tool surfaces that as
  ``capacity_signal="rate_limited"`` rather than as a false "clean".

IAM permission required: ``cloudtrail:LookupEvents``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from aws_session import CloudTrailRateLimitedError, get_client
except ImportError:
    from src.aws_session import CloudTrailRateLimitedError, get_client

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Error code substrings that indicate EC2 capacity-related failures on
# RunInstances. Real-world variants seen:
#   Client.InsufficientInstanceCapacity
#   Server.InsufficientInstanceCapacity
#   InsufficientInstanceCapacity
#   InsufficientHostCapacity
EC2_CAPACITY_ERROR_PATTERNS = (
    "InsufficientInstanceCapacity",
    "InsufficientHostCapacity",
)

# Error codes SageMaker emits on CreateEndpoint / UpdateEndpoint when the
# service cannot provision the requested capacity. ``ResourceLimitExceeded``
# is the account-quota surface; ``CapacityError`` and ``CapacityNotAvailable``
# are what SageMaker service teams document for pool-depth failures. We match
# on substring so provider variants like ``com.amazonaws.sagemaker#...`` land.
SAGEMAKER_CAPACITY_ERROR_PATTERNS = (
    "ResourceLimitExceeded",
    "CapacityError",
    "CapacityNotAvailable",
    "InsufficientInstance",
)

# CloudTrail EventName values worth querying for each deployment target.
_EC2_EVENT_NAMES = ("RunInstances",)
_SAGEMAKER_EVENT_NAMES = (
    "CreateEndpoint",
    "UpdateEndpoint",
    "UpdateEndpointWeightsAndCapacities",
)

# Valid deployment_target values.
_VALID_TARGETS = ("ec2", "sagemaker", "auto")

# How far back to look by default. CloudTrail default trail retains 90d of
# management events; lookback beyond that silently returns fewer events.
DEFAULT_LOOKBACK_DAYS = 30
MAX_LOOKBACK_DAYS = 90

# Pagination safety net. Each page returns up to 50 events; a cap of 5
# pages keeps the worst-case CloudTrail lookup bounded to ~2.5s at the
# ~2 req/s account throttle. The tool is called per-target, so an
# ``auto`` query burns up to two paging budgets — the rate limiter in
# aws_session.py enforces the ~2 req/s account bound.
MAX_PAGES = 5
PAGE_SIZE = 50

# Early-exit threshold. Once we've collected this many matching capacity
# errors for the target instance type in a query, we stop paginating — the
# deterministic scorer's severe cap already fires and any additional
# precision is wasted latency.
EARLY_EXIT_MATCH_COUNT = 15


# --------------------------------------------------------------------------
# CloudTrail client (rate-limited by aws_session)
# --------------------------------------------------------------------------


def _cloudtrail_client(region: str):
    return get_client("cloudtrail", region)


def _sagemaker_client(region: str):
    """SageMaker client scoped to ``region``.

    Goes through the same session-resolution path as CloudTrail so
    cross-account queries (``customer_context`` on the request) route
    through the assumed customer role automatically. IAM required on
    both sides: ``sagemaker:DescribeEndpointConfig``.
    """
    return get_client("sagemaker", region)


# --------------------------------------------------------------------------
# Payload parsers — CloudTrail record shapes differ by service
# --------------------------------------------------------------------------


def _is_ice_error(error_code: str | None, patterns: tuple[str, ...]) -> bool:
    if not isinstance(error_code, str) or not error_code:
        return False
    return any(pattern in error_code for pattern in patterns)


def _extract_instance_type_from_ec2_event(event_payload: dict) -> str | None:
    """Pull the requested EC2 instance type from a RunInstances event.

    RunInstances payloads carry the requested instance type in one of two
    shapes depending on SDK version. Handle both.
    """
    req = event_payload.get("requestParameters") or {}
    if not isinstance(req, dict):
        return None

    direct = req.get("instanceType")
    if isinstance(direct, str) and direct:
        return direct

    instances_set = req.get("instancesSet") or {}
    if isinstance(instances_set, dict):
        items = instances_set.get("items") or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    t = item.get("instanceType")
                    if isinstance(t, str) and t:
                        return t
    return None


def _extract_instance_types_from_sagemaker_event(
    event_payload: dict,
) -> list[str]:
    """Pull the requested SageMaker instance types from a Create/Update
    endpoint event using ONLY data present inline in the CloudTrail
    payload.

    SageMaker endpoint requests carry a ``productionVariants`` array on
    the referenced ``endpointConfigName``. LookupEvents surfaces the
    ``requestParameters`` for the endpoint call itself, which references
    an endpoint-config by name; the actual instance type lives on the
    endpoint config's ``productionVariants`` and typically is NOT in
    this event. See ``_resolve_instance_types_via_describe`` for the
    authoritative resolution path that calls
    ``sagemaker:DescribeEndpointConfig``.
    """
    types: list[str] = []
    req = event_payload.get("requestParameters") or {}
    if not isinstance(req, dict):
        req = {}

    variants = req.get("productionVariants")
    if isinstance(variants, list):
        for v in variants:
            if isinstance(v, dict):
                t = v.get("instanceType")
                if isinstance(t, str) and t:
                    types.append(t)

    # Async and shadow endpoint variants live under similar keys.
    for variant_key in ("shadowProductionVariants", "asyncInferenceConfig"):
        blob = req.get(variant_key)
        if isinstance(blob, list):
            for v in blob:
                if isinstance(v, dict):
                    t = v.get("instanceType")
                    if isinstance(t, str) and t:
                        types.append(t)

    # SageMaker attaches additional event data on capacity failures.
    aed = event_payload.get("additionalEventData") or {}
    if isinstance(aed, dict):
        t = aed.get("instanceType")
        if isinstance(t, str) and t:
            types.append(t)

    return types


def _extract_endpoint_config_name(event_payload: dict) -> str | None:
    """Pull the ``endpointConfigName`` from a SageMaker CreateEndpoint /
    UpdateEndpoint / UpdateEndpointWeightsAndCapacities payload.

    Both events carry it on ``requestParameters.endpointConfigName``.
    Returns None when the field is missing or malformed.
    """
    req = event_payload.get("requestParameters") or {}
    if not isinstance(req, dict):
        return None
    name = req.get("endpointConfigName")
    if isinstance(name, str) and name:
        return name
    return None


def _resolve_instance_types_via_describe(
    sagemaker_client,
    endpoint_config_name: str,
    _describe_cache: dict[str, list[str] | None],
) -> list[str]:
    """Call ``DescribeEndpointConfig`` on ``endpoint_config_name`` and
    return the list of ``InstanceType`` values found across production +
    shadow variants.

    Results are memoised in ``_describe_cache`` (a per-invocation dict
    the caller owns) so an endpoint config referenced by many
    ``CreateEndpoint`` / ``UpdateEndpoint`` events resolves once.

    Failures (endpoint config deleted, IAM denied, throttled, network)
    are swallowed and cached as ``None`` — the next call to the same
    name short-circuits without re-erroring.

    Follow-up A note: the customer trust policy template in
    ``docs/implementation-guide.md`` and the Lambda role now include
    ``sagemaker:DescribeEndpointConfig``. Deployments upgrading from
    before this change will see ``AccessDenied`` on the DescribeCall
    the first time; the ice-history tool degrades gracefully by
    treating those events as unresolvable (they're then filtered out
    by the ``matched`` check, exactly as before the resolver existed).
    """
    if endpoint_config_name in _describe_cache:
        cached = _describe_cache[endpoint_config_name]
        return list(cached) if cached is not None else []

    types: list[str] = []
    try:
        resp = sagemaker_client.describe_endpoint_config(
            EndpointConfigName=endpoint_config_name
        )
    except Exception:  # noqa: BLE001
        # Any failure (AccessDenied, not-found, throttled, network) is
        # non-fatal — the extractor is best-effort. Cache the miss so
        # subsequent references to the same config don't re-try.
        _describe_cache[endpoint_config_name] = None
        return types

    for variant_key in ("ProductionVariants", "ShadowProductionVariants"):
        variants = resp.get(variant_key) or []
        if isinstance(variants, list):
            for v in variants:
                if isinstance(v, dict):
                    t = v.get("InstanceType")
                    if isinstance(t, str) and t:
                        types.append(t)

    # AsyncInferenceConfig.OutputConfig doesn't carry an InstanceType
    # (async inference reuses the production variant slot); no extra
    # handling needed here.

    _describe_cache[endpoint_config_name] = types if types else None
    return types


def _extract_az(event_payload: dict) -> str | None:
    """Best-effort AZ extraction. RunInstances carries it under
    ``requestParameters.placement.availabilityZone``. SageMaker does NOT
    expose AZ selection in the endpoint API (managed).
    """
    req = event_payload.get("requestParameters") or {}
    if not isinstance(req, dict):
        return None
    placement = req.get("placement") or {}
    if isinstance(placement, dict):
        az = placement.get("availabilityZone")
        if isinstance(az, str) and az:
            return az
    return None


def _classify_signal(event_count: int) -> tuple[str, str]:
    if event_count == 0:
        return (
            "clean",
            "No capacity errors observed for this instance type in the "
            "lookback window — the customer's own recent history shows "
            "no capacity issues on the queried deployment target(s).",
        )
    if event_count <= 3:
        return (
            "isolated",
            f"{event_count} capacity error(s) observed in the lookback "
            "window — isolated incidents, may have been transient. Worth "
            "flagging but not necessarily blocking.",
        )
    if event_count <= 10:
        return (
            "recurring",
            f"{event_count} capacity errors observed in the lookback "
            "window — recurring capacity issues. Pool depth is "
            "demonstrably tight.",
        )
    return (
        "severe",
        f"{event_count} capacity errors observed in the lookback window "
        "— severe and persistent capacity issues. Do not rely on Spot "
        "Placement Score; the customer has direct evidence of "
        "insufficient capacity. ODCR or Capacity Blocks are required "
        "before any new deployment.",
    )


# --------------------------------------------------------------------------
# Per-target CloudTrail scans
# --------------------------------------------------------------------------


def _run_lookup(
    client,
    event_names: tuple[str, ...],
    window_start: datetime,
    window_end: datetime,
    bare_type: str,
    ml_type: str,
    error_patterns: tuple[str, ...],
    instance_type_extractor,
    expected_event_source: str | None,
    sagemaker_client=None,
) -> tuple[list[dict[str, Any]], bool]:
    """Page through CloudTrail LookupEvents for a set of event names and
    return (matched_events, truncated).

    ``instance_type_extractor`` returns EITHER a single instance-type
    string (EC2 path) OR a list of instance-type strings (SageMaker
    path — an endpoint request can have multiple production variants).

    ``sagemaker_client``: when provided AND the inline extractor returned
    no usable instance types AND the payload carries an
    ``endpointConfigName``, a ``DescribeEndpointConfig`` call resolves
    the referenced config to authoritative production/shadow variant
    instance types. Results are memoised per invocation. When
    ``None`` (EC2 path), no resolution is attempted.

    Returns ``(matched_events, truncated, unresolved_count)``. The
    third value is the number of capacity-error events we FOUND in
    CloudTrail but could NOT resolve to an instance type — either
    because inline extraction returned nothing AND either (a) no
    ``endpointConfigName`` was present or (b) ``DescribeEndpointConfig``
    failed. The caller uses it to distinguish "genuinely 0 events"
    from "we saw events but blindfolded" — the latter must NOT be
    treated as a clean signal by the scorer.
    """
    ice_events: list[dict[str, Any]] = []
    truncated = False
    unresolved_count = 0
    # Per-invocation memo for DescribeEndpointConfig — the same endpoint
    # config often appears across many CreateEndpoint / UpdateEndpoint
    # events, so we only pay for one API call per config name per query.
    describe_cache: dict[str, list[str] | None] = {}

    # LookupEvents accepts exactly ONE lookup attribute per call, so we
    # must issue one paging loop per event name. It also does NOT accept
    # ``errorCode`` as a lookup attribute — every error filter is
    # client-side after fetching. On an active fleet doing many launches
    # per day the first pages are dominated by successful RunInstances
    # calls, which is why the ``ice_signal_unavailable`` cap in
    # ``src/confidence.py`` matters: an active fleet that never hits any
    # errors in the paging window must NOT be treated as "clean". See
    # docs/implementation-guide.md → "ICE-history metric filter fallback"
    # for the recommended supplement pattern for high-volume customers.
    for event_name in event_names:
        next_token: str | None = None
        for page_idx in range(MAX_PAGES):
            kwargs: dict[str, Any] = {
                "LookupAttributes": [
                    {"AttributeKey": "EventName", "AttributeValue": event_name},
                ],
                "StartTime": window_start,
                "EndTime": window_end,
                "MaxResults": PAGE_SIZE,
            }
            if next_token:
                kwargs["NextToken"] = next_token

            resp = client.lookup_events(**kwargs)
            for event in resp.get("Events", []):
                raw = event.get("CloudTrailEvent")
                if not isinstance(raw, str):
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                # SageMaker events must originate from sagemaker.amazonaws.com;
                # a same-named event under a different eventSource would be
                # an SDK-level oddity we should ignore.
                if expected_event_source is not None:
                    src = payload.get("eventSource")
                    if src != expected_event_source:
                        continue

                error_code = payload.get("errorCode")
                if not _is_ice_error(error_code, error_patterns):
                    continue

                extracted = instance_type_extractor(payload)
                # Normalise to a list.
                if isinstance(extracted, list):
                    candidates = list(extracted)
                elif isinstance(extracted, str):
                    candidates = [extracted]
                else:
                    candidates = []

                # SageMaker fallback: if inline extraction produced nothing,
                # look up the referenced endpoint-config via
                # DescribeEndpointConfig and merge its instance types.
                resolved_via_describe = False
                inline_had_nothing = not candidates
                if inline_had_nothing and sagemaker_client is not None:
                    cfg_name = _extract_endpoint_config_name(payload)
                    if cfg_name:
                        candidates = _resolve_instance_types_via_describe(
                            sagemaker_client, cfg_name, describe_cache
                        )
                        if candidates:
                            resolved_via_describe = True

                # If we saw a capacity-error event but can't attribute it
                # to any instance type, count it as "unresolved" so the
                # caller can distinguish "genuinely 0 events" from
                # "blindfolded on N events". Only relevant on the
                # SageMaker path — the EC2 extractor always returns the
                # instance type when present.
                if not candidates and sagemaker_client is not None:
                    unresolved_count += 1
                    continue

                # Match either bare EC2 form or ml.-prefixed SageMaker form.
                matched = False
                for c in candidates:
                    if not isinstance(c, str) or not c:
                        continue
                    normalized = c.removeprefix("ml.")
                    if normalized == bare_type or c == ml_type:
                        matched = True
                        break
                if not matched:
                    continue

                ice_events.append(
                    {
                        "event_time": event.get("EventTime").isoformat()
                        if event.get("EventTime")
                        else None,
                        "event_name": event_name,
                        "error_code": error_code,
                        "availability_zone": _extract_az(payload),
                        "event_id": event.get("EventId"),
                        "instance_type_source": (
                            "describe_endpoint_config"
                            if resolved_via_describe
                            else "cloudtrail_inline"
                        ),
                    }
                )

            next_token = resp.get("NextToken")
            if len(ice_events) >= EARLY_EXIT_MATCH_COUNT and next_token:
                truncated = True
                break
            if not next_token:
                break
            if page_idx == MAX_PAGES - 1:
                truncated = True

    return ice_events, truncated, unresolved_count


def _scan_ec2(
    client,
    window_start: datetime,
    window_end: datetime,
    bare_type: str,
    ml_type: str,
) -> tuple[list[dict[str, Any]], bool, int]:
    return _run_lookup(
        client,
        _EC2_EVENT_NAMES,
        window_start,
        window_end,
        bare_type,
        ml_type,
        EC2_CAPACITY_ERROR_PATTERNS,
        _extract_instance_type_from_ec2_event,
        expected_event_source=None,  # EC2 EventName is unambiguous
    )


def _scan_sagemaker(
    client,
    window_start: datetime,
    window_end: datetime,
    bare_type: str,
    ml_type: str,
    sagemaker_client=None,
) -> tuple[list[dict[str, Any]], bool, int]:
    return _run_lookup(
        client,
        _SAGEMAKER_EVENT_NAMES,
        window_start,
        window_end,
        bare_type,
        ml_type,
        SAGEMAKER_CAPACITY_ERROR_PATTERNS,
        _extract_instance_types_from_sagemaker_event,
        expected_event_source="sagemaker.amazonaws.com",
        sagemaker_client=sagemaker_client,
    )


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def get_ice_history(
    instance_type: str,
    region: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    deployment_target: str = "auto",
) -> dict[str, Any]:
    """Return the customer's recent capacity-error history for the target
    instance type and deployment surface.

    Args:
        instance_type: EC2 instance type (e.g. ``"g5.2xlarge"``). ``ml.``
            prefix is accepted and stripped.
        region: AWS region whose CloudTrail to inspect.
        lookback_days: How many days of history to scan. Default 30,
            clamped to [1, 90].
        deployment_target: Which CloudTrail namespace to query:

            - ``"ec2"``: only ``RunInstances`` events (self-managed EC2,
              EKS-on-EC2, Karpenter data-plane fleets in this account).
            - ``"sagemaker"``: only SageMaker endpoint events. SageMaker
              provisions instances inside its own service account, so
              capacity failures do NOT appear as RunInstances in the
              customer's CloudTrail.
            - ``"auto"`` (default): run BOTH queries and merge the
              results. Costs two paging budgets. Prefer this when the
              user's ask doesn't clearly specify the deployment target.

    Returns:
        dict with:
            instance_type: normalized (no ml.)
            region: AWS region
            deployment_target: the target queried
            lookback_days: clamped lookback window
            window_start / window_end: ISO timestamps
            event_count: total capacity errors matched
            events_by_target: breakdown by (ec2, sagemaker) subcounts
            events_by_az: {az: count} for EC2 events only
            sample_events: up to 5 recent events with event_name, error_code,
                event_time, availability_zone (may be None for SageMaker)
            capacity_signal: 'clean' | 'isolated' | 'recurring' | 'severe' | 'unknown' | 'rate_limited'
            interpretation: human-readable summary
            truncated: True if any per-event-name paging cap was reached
            source: API citation

    On IAM / rate-limit / trail-not-configured errors the tool returns
    ``event_count=None`` and ``capacity_signal="unknown"`` (or
    ``"rate_limited"`` when the aws_session token bucket refused the call).
    """
    bare_type = instance_type.removeprefix("ml.")
    ml_type = f"ml.{bare_type}"
    lookback_days = max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS))

    if deployment_target not in _VALID_TARGETS:
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": deployment_target,
            "event_count": None,
            "capacity_signal": "unknown",
            "interpretation": (
                f"Invalid deployment_target={deployment_target!r}. Must be "
                f"one of {_VALID_TARGETS}."
            ),
            "source": "cloudtrail.LookupEvents",
        }

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=lookback_days)

    try:
        client = _cloudtrail_client(region)

        ec2_events: list[dict[str, Any]] = []
        sm_events: list[dict[str, Any]] = []
        ec2_truncated = False
        sm_truncated = False
        ec2_unresolved = 0
        sm_unresolved = 0

        if deployment_target in ("ec2", "auto"):
            ec2_events, ec2_truncated, ec2_unresolved = _scan_ec2(
                client, window_start, window_end, bare_type, ml_type
            )
        if deployment_target in ("sagemaker", "auto"):
            # DescribeEndpointConfig resolves the customer's endpoint-
            # config → instance-type mapping when CloudTrail's inline
            # payload doesn't include it (the common case). Client
            # construction is cheap; the actual DescribeCall is only
            # made when a matching event has no inline instance type.
            sm_client = _sagemaker_client(region)
            sm_events, sm_truncated, sm_unresolved = _scan_sagemaker(
                client,
                window_start,
                window_end,
                bare_type,
                ml_type,
                sagemaker_client=sm_client,
            )

        combined = ec2_events + sm_events
        truncated = ec2_truncated or sm_truncated
        unresolved_total = ec2_unresolved + sm_unresolved

        events_by_az: dict[str, int] = {}
        for evt in combined:
            az = evt.get("availability_zone") or "unknown"
            events_by_az[az] = events_by_az.get(az, 0) + 1

        # Follow-up A: distinguish "truly clean" from "blindfolded". If
        # we saw capacity errors we couldn't attribute to an instance
        # type (typically because DescribeEndpointConfig was denied /
        # the config was deleted), return capacity_signal="unresolved"
        # so the scorer's ice_signal_unavailable cap fires and refuses
        # DEPLOYABLE. This closes the false-CLEAN gap the review
        # identified for the SageMaker path.
        if len(combined) == 0 and unresolved_total > 0:
            capacity_signal = "unresolved"
            interpretation = (
                f"Found {unresolved_total} SageMaker capacity-error "
                "event(s) in CloudTrail but could not attribute any to "
                "a specific instance type. The most likely cause is a "
                "missing ``sagemaker:DescribeEndpointConfig`` permission "
                "or a deleted endpoint config. Do NOT treat this as a "
                "clean signal — grant the permission (see "
                "docs/implementation-guide.md) and re-query."
            )
        else:
            capacity_signal, interpretation = _classify_signal(len(combined))
            if unresolved_total > 0:
                interpretation += (
                    f" Note: {unresolved_total} additional SageMaker "
                    "capacity-error event(s) could not be attributed to "
                    "an instance type (missing "
                    "``sagemaker:DescribeEndpointConfig`` or deleted "
                    "endpoint config) and are excluded from event_count."
                )
        if truncated:
            interpretation += (
                f" Note: per-event-name pagination cap of "
                f"{MAX_PAGES * PAGE_SIZE} events was reached — the true "
                "count may be higher."
            )

        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": deployment_target,
            "lookback_days": lookback_days,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "event_count": len(combined),
            "events_by_target": {
                "ec2": len(ec2_events),
                "sagemaker": len(sm_events),
            },
            "unresolved_event_count": unresolved_total,
            "events_by_az": events_by_az,
            "sample_events": combined[:5],
            "capacity_signal": capacity_signal,
            "interpretation": interpretation,
            "truncated": truncated,
            "source": "cloudtrail.LookupEvents",
        }

    except Exception as exc:  # noqa: BLE001
        # Distinguish rate-limit refusals (from aws_session's token bucket
        # or CloudTrail throttling) from other errors so the scorer can
        # treat them differently — the scorer must NOT accept a
        # rate-limited response as "clean" (that would recreate the
        # false-DEPLOYABLE bug this rewrite fixes).
        exc_name = type(exc).__name__
        # ``isinstance(exc, CloudTrailRateLimitedError)`` only works if
        # ``CloudTrailRateLimitedError`` is a real class — under some
        # tests aws_session is monkey-patched in sys.modules and the
        # symbol is a MagicMock. Guard the check so this branch never
        # raises just because the symbol isn't a real class.
        is_rate_limit_class = isinstance(
            CloudTrailRateLimitedError, type
        ) and isinstance(exc, CloudTrailRateLimitedError)
        rate_limited = (
            is_rate_limit_class
            or "Throttl" in exc_name
            or "RateExceeded" in exc_name
            or "rate limit" in str(exc).lower()
            or "CloudTrailRateLimitedError" in exc_name
        )
        capacity_signal = "rate_limited" if rate_limited else "unknown"
        interpretation = f"Unable to query CloudTrail: {exc_name}: {exc}. " + (
            "The account-level rate limit for cloudtrail:LookupEvents "
            "was hit or refused by the local rate limiter. Retry with "
            "lower concurrency; do NOT treat this as a clean signal."
            if rate_limited
            else "This may indicate insufficient IAM permissions "
            "(cloudtrail:LookupEvents), no trail configured in the "
            "region, or a transient API error. Proceed with capacity "
            "assessment without the ICE-history signal — the scorer "
            "will refuse to return DEPLOYABLE until this signal is "
            "obtained."
        )
        return {
            "instance_type": bare_type,
            "region": region,
            "deployment_target": deployment_target,
            "lookback_days": lookback_days,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "event_count": None,
            "events_by_target": {"ec2": None, "sagemaker": None},
            "events_by_az": {},
            "sample_events": [],
            "capacity_signal": capacity_signal,
            "interpretation": interpretation,
            "truncated": False,
            "source": "cloudtrail.LookupEvents",
        }


TOOL_SPEC = {
    "name": "get_ice_history",
    "description": (
        "Check the customer's recent capacity-error history in CloudTrail. "
        "The tool queries the CORRECT CloudTrail namespace for the "
        "deployment target: RunInstances events for EC2 / EKS / self-managed "
        "fleets, or CreateEndpoint / UpdateEndpoint events under "
        "eventSource=sagemaker.amazonaws.com for SageMaker-managed endpoints. "
        "SageMaker capacity failures do NOT appear as RunInstances in the "
        "customer's CloudTrail because SageMaker provisions instances inside "
        "its own service account. Pick deployment_target based on what the "
        "user's ask targets; when in doubt, use 'auto' to query both. "
        "Returns event_count (matched capacity errors), events_by_target "
        "breakdown, and a capacity_signal in {clean, isolated, recurring, "
        "severe, unknown, rate_limited}. Ground-truth signal — outranks "
        "Spot Placement Score when the two disagree."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instance_type": {
                "type": "string",
                "description": (
                    "EC2 instance type (e.g., 'g5.2xlarge', 'p4d.24xlarge'). "
                    "The ml. prefix is accepted and stripped."
                ),
            },
            "region": {
                "type": "string",
                "description": "AWS region whose CloudTrail to inspect (e.g., 'us-east-1').",
            },
            "lookback_days": {
                "type": "integer",
                "description": (
                    "Days of CloudTrail history to scan. Default 30, clamped to "
                    "[1, 90]. Wider windows take longer and may hit pagination caps."
                ),
            },
            "deployment_target": {
                "type": "string",
                "description": (
                    "'ec2' for self-managed / EKS-on-EC2 / Karpenter data-plane; "
                    "'sagemaker' for SageMaker-managed endpoints (real-time, "
                    "async, serverless); 'auto' (default) runs both and merges. "
                    "SageMaker workloads MUST use 'sagemaker' or 'auto' — "
                    "'ec2' will falsely report clean history."
                ),
                "enum": ["ec2", "sagemaker", "auto"],
            },
        },
        "required": ["instance_type", "region"],
    },
    "handler": get_ice_history,
}
