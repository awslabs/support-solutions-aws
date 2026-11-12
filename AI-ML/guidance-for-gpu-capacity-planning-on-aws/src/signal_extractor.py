# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Signal extraction for deterministic confidence scoring.

Reads the agent's tool_calls history and groups raw signals by
(instance_type, region) so we can compute one confidence assessment
per investigated target. The LLM is no longer responsible for inventing
a score; it reports the pre-computed one.

Tool result contracts this module relies on:
    check_instance_availability                  → data["available"] (bool|None), data["region"], data["instance_type"]
    get_sagemaker_pricing                        → data["available"] (bool), data["region"], data["instance_type"]
    get_sagemaker_quota                          → data["found"] (bool), data["current_limit"] (int when found)
    get_spot_placement_score                     → data["score"] (1-10|None), data["target_capacity"] (int)
    check_existing_odcrs                         → data["available_capacity"] (int)
    get_capacity_blocks_offerings                → data["capacity_signal"] ("available"|"unavailable"|"unknown")
    get_sagemaker_training_plan_offerings        → data["capacity_signal"] ("available"|"unavailable"|"unknown")
    evaluate_heterogeneous_endpoint_fallback     → data["capacity_signal"] ("available"|"unavailable"|"unknown")
    get_ice_history                              → data["event_count"] (int|None on error), data["capacity_signal"]
    get_ice_metrics                              → data["event_count"] (int|None on error), data["capacity_signal"]

ICE signal precedence: when BOTH ``get_ice_metrics`` and ``get_ice_history``
ran successfully for the same (instance_type, region), the scorer sees the
metrics reading. Metric filters index every matching CloudTrail event
server-side; LookupEvents is paging-limited and can miss events on active
fleets. The precedence is enforced by processing the tool calls in an
order that lets ice_metrics OVERWRITE any ice_history values, EXCEPT when
metrics returned an inconclusive signal (``metric_filter_not_deployed`` /
``unknown`` / ``rate_limited``) — in that case the history reading wins.
See ``extract_signals`` for the implementation.

Tools whose results aren't keyed to a single (instance_type, region) — such
as find_alternative_instances or get_reference_data — are ignored here.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Iterable

try:
    from confidence import compute_confidence
except ImportError:
    from src.confidence import compute_confidence


def _normalize_type(value: Any) -> str | None:
    """Strip ml. prefix and lowercase. Return None for non-strings/empty."""
    if not isinstance(value, str) or not value:
        return None
    return value.lower().removeprefix("ml.")


def _gpu_family(instance_type: str) -> str:
    """g5.2xlarge → g5, p4d.24xlarge → p4d. Falls back to the input if no dot."""
    return instance_type.split(".")[0] if "." in instance_type else instance_type


# ---------- Per-tool extractors ----------
# Each extractor mutates the per-target signal record in-place.
# Extractors are intentionally forgiving: they only set a field when the
# tool returned a usable value. Unknown/None signals are left alone so
# compute_confidence can apply its own neutral defaults.


def _from_availability(record: dict[str, Any], args: dict, data: dict) -> None:
    available = data.get("available")
    if isinstance(available, bool):
        record["instance_available"] = available
    elif available is None and "available" in data:
        # Tool ran but couldn't determine availability (API error path).
        # Leave instance_available unset so confidence treats it as unknown.
        return


def _from_sagemaker_pricing(record: dict[str, Any], args: dict, data: dict) -> None:
    available = data.get("available")
    if isinstance(available, bool):
        record["pricing_available"] = available


def _from_quota(record: dict[str, Any], args: dict, data: dict) -> None:
    if data.get("found") is True:
        limit = data.get("current_limit")
        if isinstance(limit, (int, float)):
            record["quota_limit"] = int(limit)
    elif data.get("found") is False:
        # Tool ran and explicitly reported no quota record — treat as 0.
        # Some accounts genuinely have no quota for newer instance types.
        record["quota_limit"] = 0


def _from_spot(record: dict[str, Any], args: dict, data: dict) -> None:
    score = data.get("score")
    if isinstance(score, (int, float)):
        record["spot_placement_score"] = int(score)

    # target_capacity in the result echoes the customer's fleet size. We
    # keep the largest seen value as the "requested_count" — useful both
    # for the quota ratio and the ODCR coverage ratio.
    target = data.get("target_capacity")
    if not isinstance(target, int):
        # Fall back to the input argument if the tool didn't echo it
        target = args.get("target_capacity") if isinstance(args, dict) else None
    if isinstance(target, int) and target > 0:
        record["requested_count"] = max(record.get("requested_count", 0), target)


def _from_odcr(record: dict[str, Any], args: dict, data: dict) -> None:
    available = data.get("available_capacity")
    if isinstance(available, int) and available >= 0:
        record["odcr_available_capacity"] = available


def _from_capacity_blocks(record: dict[str, Any], args: dict, data: dict) -> None:
    """Extract Tier 1 (EC2 Capacity Blocks for ML) availability signal.

    Only the categorical ``capacity_signal`` matters here — the number of
    offerings and dollar figures are LLM-narration material, not scoring
    inputs. ``"available"`` → True (lift); ``"unavailable"`` → False
    (informational, no lift); ``"unknown"`` → leave unset so the scorer
    treats the tool as not having run.
    """
    signal = data.get("capacity_signal")
    if signal == "available":
        record["capacity_blocks_available"] = True
    elif signal == "unavailable":
        record["capacity_blocks_available"] = False
    # "unknown" / anything else → leave unset


def _from_training_plans(record: dict[str, Any], args: dict, data: dict) -> None:
    """Extract Tier 2 (SageMaker Flexible Training Plans) availability signal.

    Mirror of ``_from_capacity_blocks`` for the sibling tool. The nested
    ``reserved_capacity_offerings`` and ``cheapest_offering`` fields are
    LLM-narration material and do not feed the deterministic scorer.
    """
    signal = data.get("capacity_signal")
    if signal == "available":
        record["training_plans_available"] = True
    elif signal == "unavailable":
        record["training_plans_available"] = False
    # "unknown" / anything else → leave unset


def _from_heterogeneous_endpoint(
    record: dict[str, Any], args: dict, data: dict
) -> None:
    """Extract SageMaker capacity-aware endpoint fallback signal.

    ``evaluate_heterogeneous_endpoint_fallback`` returns
    ``capacity_signal="available"`` when at least one fallback candidate
    survives the hard disqualifiers (same GPU vendor, offered in region,
    SageMaker-GA via Pricing API). The ranked fallback_candidates list,
    deployment_recommendation, and per-pool TP guidance are LLM-narration
    material; the deterministic scorer only needs the binary signal.

    The tool's input key is ``primary_instance_type`` rather than the
    ``instance_type`` the other extractors expect, so this function
    normalizes it onto the record under the standard key before the
    caller's ``_key_for_call`` runs.
    """
    signal = data.get("capacity_signal")
    if signal == "available":
        record["heterogeneous_fallback_available"] = True
    elif signal == "unavailable":
        record["heterogeneous_fallback_available"] = False
    # "unknown" / anything else → leave unset


# ICE-signal precedence when merging get_ice_metrics + get_ice_history
# results. A REAL (non-inconclusive) signal from either tool preempts an
# inconclusive one, and when both are real, ice_metrics wins because the
# metric filter indexes every matching event server-side (LookupEvents is
# paging-limited).
_INCONCLUSIVE_ICE_SIGNALS = {
    "unknown",
    "rate_limited",
    "unresolved",
    "metric_filter_not_deployed",
}


def _merge_ice_signal(record: dict[str, Any], data: dict, *, source: str) -> None:
    """Merge an ICE reading (either ``get_ice_metrics`` or
    ``get_ice_history``) into ``record``, honoring precedence.

    ``source`` is ``"metrics"`` or ``"history"``. Metrics wins ties.

    Rules:
    - If the incoming signal is inconclusive AND the record already has
      a real signal, the incoming is ignored (real beats inconclusive).
    - If the incoming signal is real AND the record has only an
      inconclusive one, the incoming replaces it.
    - If both are real, metrics wins (metric filters see everything;
      LookupEvents can miss events on active fleets).
    - If both are inconclusive, the one that ran later stays — this is
      an edge case; either way the scorer refuses DEPLOYABLE.
    """
    incoming_signal = data.get("capacity_signal")
    if not isinstance(incoming_signal, str):
        return
    incoming_count = data.get("event_count")

    existing_signal = record.get("customer_ice_capacity_signal")
    existing_source = record.get("_ice_signal_source")

    incoming_inconclusive = incoming_signal in _INCONCLUSIVE_ICE_SIGNALS
    existing_inconclusive = (
        existing_signal is None or existing_signal in _INCONCLUSIVE_ICE_SIGNALS
    )

    # Decide whether to overwrite.
    if existing_signal is None:
        overwrite = True
    elif existing_inconclusive and not incoming_inconclusive:
        overwrite = True  # real beats inconclusive
    elif not existing_inconclusive and incoming_inconclusive:
        overwrite = False  # keep the real one
    elif not existing_inconclusive and not incoming_inconclusive:
        # Both real: metrics wins over history.
        overwrite = source == "metrics" and existing_source != "metrics"
    else:
        # Both inconclusive: last writer wins.
        overwrite = True

    if not overwrite:
        return

    record["customer_ice_capacity_signal"] = incoming_signal
    record["_ice_signal_source"] = source
    if isinstance(incoming_count, int) and incoming_count >= 0:
        record["customer_ice_events"] = incoming_count
    elif incoming_inconclusive:
        # Clear a stale count so the scorer doesn't mix an old integer
        # with a fresh inconclusive signal.
        record.pop("customer_ice_events", None)

    target = data.get("deployment_target")
    if isinstance(target, str):
        record["customer_ice_deployment_target"] = target


def _from_ice_history(record: dict[str, Any], args: dict, data: dict) -> None:
    """Extract customer-side ICE event count from get_ice_history results.

    Delegates precedence logic to ``_merge_ice_signal`` so that a
    concurrent ``get_ice_metrics`` reading wins when both are real.
    """
    _merge_ice_signal(record, data, source="history")


def _from_ice_metrics(record: dict[str, Any], args: dict, data: dict) -> None:
    """Extract customer-side ICE event count from get_ice_metrics results.

    Metric filters index every matching CloudTrail event, so this is the
    authoritative source when it returns a real signal. Delegates
    precedence logic to ``_merge_ice_signal``.
    """
    _merge_ice_signal(record, data, source="metrics")


TOOL_EXTRACTORS: dict[str, Callable[[dict[str, Any], dict, dict], None]] = {
    "check_instance_availability": _from_availability,
    "get_sagemaker_pricing": _from_sagemaker_pricing,
    "get_sagemaker_quota": _from_quota,
    "get_spot_placement_score": _from_spot,
    "check_existing_odcrs": _from_odcr,
    "get_capacity_blocks_offerings": _from_capacity_blocks,
    "get_sagemaker_training_plan_offerings": _from_training_plans,
    "evaluate_heterogeneous_endpoint_fallback": _from_heterogeneous_endpoint,
    "get_ice_history": _from_ice_history,
    "get_ice_metrics": _from_ice_metrics,
}


def _key_for_call(args: dict, data: dict) -> tuple[str, str] | None:
    """Resolve (normalized instance_type, region) for a tool call.

    Prefers the tool's echoed values (data) over the requested arguments,
    since the tool typically normalizes them. Falls through to a few
    non-standard keys used by newer tools that take a differently-named
    primary instance (currently just
    ``evaluate_heterogeneous_endpoint_fallback``, which uses
    ``primary_instance_type`` as its input key and nests the normalized
    value under ``data["primary"]["instance_type"]`` in its response).
    Keeping the fallback chain here keeps the per-tool extractor
    functions schema-free — they only have to populate the signal
    record, not negotiate how the key gets resolved.
    """
    nested_primary = data.get("primary")
    nested_inst = (
        nested_primary.get("instance_type")
        if isinstance(nested_primary, dict)
        else None
    )
    inst = _normalize_type(
        data.get("instance_type")
        or args.get("instance_type")
        or args.get("primary_instance_type")
        or nested_inst
    )
    region = data.get("region") or args.get("region")
    if not inst or not isinstance(region, str) or not region:
        return None
    return inst, region


def extract_signals(
    tool_calls: Iterable[tuple[str, dict, dict]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Walk tool_calls and produce {(instance_type, region): kwargs_for_compute_confidence}.

    Each value dict can be splatted into compute_confidence(). Targets with
    no usable signals are omitted.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    for entry in tool_calls or []:
        if not isinstance(entry, (tuple, list)) or len(entry) < 3:
            continue
        name, args, output = entry[0], entry[1], entry[2]
        if not isinstance(output, dict) or not output.get("ok"):
            continue
        data = output.get("data")
        if not isinstance(data, dict):
            continue
        extractor = TOOL_EXTRACTORS.get(name)
        if extractor is None:
            continue
        key = _key_for_call(args if isinstance(args, dict) else {}, data)
        if key is None:
            continue
        extractor(grouped[key], args if isinstance(args, dict) else {}, data)
    return dict(grouped)


def assess_targets(
    tool_calls: Iterable[tuple[str, dict, dict]],
) -> list[dict[str, Any]]:
    """Produce one deterministic confidence assessment per (instance_type, region)
    investigated.

    Each assessment is a dict:
        {
            "instance_type": "g5.2xlarge",
            "region": "us-east-1",
            "requested_count": 20,
            "raw_signals_used": {...},
            "confidence": { "score": 0.78, "label": "MODERATE", "verdict": ..., "signals": {...}, "weights": {...} }
        }

    The list is sorted with the lowest-confidence (riskiest) target first,
    so a caller skimming the response sees the items most worth attention.
    Returns an empty list if no usable signals were extracted.
    """
    grouped = extract_signals(tool_calls)
    assessments: list[dict[str, Any]] = []
    for (inst, region), signals in grouped.items():
        kwargs = dict(signals)
        # Strip internal precedence markers before splatting into
        # compute_confidence — they're bookkeeping, not scoring inputs.
        kwargs.pop("_ice_signal_source", None)
        kwargs.setdefault("requested_count", 1)
        kwargs["region"] = region
        kwargs["gpu_family"] = _gpu_family(inst)
        try:
            verdict = compute_confidence(**kwargs)
        except Exception:  # noqa: BLE001  # nosec B112 - skip unscorable candidate; never let scoring break the request
            continue
        # raw_signals_used reflects what the scorer actually used — strip
        # the internal marker there too so the response payload stays clean.
        exposed_signals = {k: v for k, v in signals.items() if not k.startswith("_")}
        assessments.append(
            {
                "instance_type": inst,
                "region": region,
                "requested_count": kwargs.get("requested_count"),
                "raw_signals_used": exposed_signals,
                "confidence": verdict,
            }
        )
    assessments.sort(key=lambda a: a["confidence"]["score"])
    return assessments
