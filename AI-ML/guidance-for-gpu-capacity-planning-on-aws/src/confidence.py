# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deterministic confidence scoring for capacity risk assessment.

Computes a numeric confidence score (0.0-1.0) from tool results using
weighted signals. The LLM reports this pre-computed score — it doesn't
invent one.

Signal weights:
- Instance availability:  0.25 (binary: offered or not)
- Pricing/GA status:      0.15 (binary: priced or not)
- Quota sufficiency:      0.25 (ratio: quota/requested, capped at 1.0)
- Spot Placement Score:   0.25 (normalized: score/10)
- Region maturity:        0.10 (categorical: established=1.0, moderate=0.6, limited=0.3)

Side signals (reported for transparency, modify spot_placement):
- ICE history (customer's own CloudTrail). Real ICE events are ground truth;
  they cap the spot_placement signal regardless of what SPS says.
- ODCR coverage (customer's reserved capacity). Reserved capacity is
  guaranteed, so it lifts the spot_placement signal to at least the coverage
  ratio. Applied AFTER the ICE cap so an active ODCR still rescues the
  reserved portion even when the shared pool is showing severe ICE.
- Capacity Blocks for ML availability (Tier 1 mitigation). Public inventory
  of pre-reserved ML slots. Weaker than ODCR — the capacity exists in the
  market but the customer still has to purchase matching blocks and have the
  launch window align. When available, lifts the spot_placement signal to a
  modest floor (0.4) so a weak shared-pool SPS doesn't dominate the verdict
  when a Tier 1 mitigation PATH exists. Does NOT override ICE severity caps.
- Flexible Training Plans availability (Tier 2 mitigation). Same shape as
  Capacity Blocks but for medium-term (14-182 day) training-focused
  reservations. Same modest 0.4 floor; same no-override behavior on ICE caps.
- Heterogeneous-endpoint fallback availability (SageMaker inference
  architectural mitigation). Signal from ``evaluate_heterogeneous_endpoint_fallback``
  that a valid primary+fallback instance pairing exists for a capacity-aware
  SageMaker endpoint. The fallback pool only consumes cost when SageMaker
  ScaleOut actually provisions it, which makes this a no-upfront-cost
  mitigation path. Same 0.4 floor and same no-override-of-ICE-caps as the
  CB / FTP signals; the shared ``_MITIGATION_PATH_FLOOR`` constant pins all
  three.
"""

from __future__ import annotations

from typing import Any

try:
    from region_model import get_region_score
except ImportError:
    from src.region_model import get_region_score
# Everything else is considered limited-capacity


def compute_confidence(
    *,
    instance_available: bool | None = None,
    pricing_available: bool | None = None,
    quota_limit: int | None = None,
    requested_count: int = 1,
    spot_placement_score: int | None = None,
    odcr_available_capacity: int | None = None,
    customer_ice_events: int | None = None,
    customer_ice_capacity_signal: str | None = None,
    customer_ice_deployment_target: str | None = None,
    capacity_blocks_available: bool | None = None,
    training_plans_available: bool | None = None,
    heterogeneous_fallback_available: bool | None = None,
    region: str = "",
    gpu_family: str | None = None,
) -> dict[str, Any]:
    """Compute a deterministic confidence score from tool results.

    Args:
        instance_available: True if instance is offered in region, None if unknown
        pricing_available: True if SageMaker pricing exists (confirms GA)
        quota_limit: Current SageMaker endpoint quota for this instance type
        requested_count: Number of instances the customer wants to deploy
        spot_placement_score: Spot Placement Score (1-10), None if unavailable
        odcr_available_capacity: Customer's available (unconsumed) capacity from
            active/scheduled On-Demand Capacity Reservations matching this
            instance type and region. None when no ODCR lookup ran.
        customer_ice_events: Count of Insufficient Capacity Errors observed in
            the customer's CloudTrail for this instance type / region in the
            lookback window. None when no ICE-history lookup ran. Real ICE
            events cap the spot_placement signal — ground truth beats the SPS
            proxy.
        capacity_blocks_available: True if the ``get_capacity_blocks_offerings``
            tool returned at least one matching offering for this instance
            type / region (Tier 1 mitigation path exists in public inventory).
            False when the tool ran and returned zero offerings (ML family not
            eligible or no matching inventory). None when the tool didn't run.
            Weaker signal than ODCR — the capacity exists in the market but
            the customer still has to purchase blocks that match the launch
            window.
        training_plans_available: Same shape as ``capacity_blocks_available``
            but for SageMaker Flexible Training Plans (Tier 2 mitigation,
            14-182 day training reservations).
        heterogeneous_fallback_available: True when the
            ``evaluate_heterogeneous_endpoint_fallback`` tool found at
            least one valid fallback instance for a SageMaker
            capacity-aware endpoint pairing (same GPU vendor, offered
            in region, SageMaker-GA). False when every candidate was
            disqualified. None when the tool didn't run or returned
            ``capacity_signal="unknown"``. SageMaker-inference-only
            architectural mitigation — the fallback pool only costs
            money if SageMaker ScaleOut actually provisions it.
        region: AWS region code
        gpu_family: GPU family prefix (e.g., "g5", "p4d") for family-specific region scoring

    Returns:
        dict with:
            score: float (0.0-1.0) — final score, after caps/lifts.
            label: str (HIGH/MODERATE/MODERATE-LOW/VERY LOW)
            verdict: str (DEPLOYABLE/DEPLOYABLE WITH PREPARATION/HIGH RISK/NOT FEASIBLE)
            signals: dict of the FINAL (post-adjustment) signal values used
                to compute the weighted base score. Multiply by ``weights``
                to reproduce the pre-cap ``base_score``.
            weights: dict of signal → weight. Sums to 1.0.
            base_score: float — the weighted sum of ``signals × weights``
                BEFORE the hard-gate caps (unavailable / not-priced / severe
                ICE without ODCR cover) are applied. This is the number a
                consumer would compute if they only had ``signals`` and
                ``weights``. Publishing it makes the score auditable.
            applied_caps: list of {name, description, from_value, to_value}
                records for every cap or lift the scorer applied — the
                categorical unavailable gate, the pricing gate, the ICE
                severity ceilings, and the ODCR coverage lifts. Empty when
                no adjustment fired. Reading this list explains WHY the
                final ``score`` differs from ``base_score``.
    """
    signals = {}
    weights = {
        "availability": 0.25,
        "pricing": 0.15,
        "quota": 0.25,
        "spot_placement": 0.25,
        "region_maturity": 0.10,
    }
    applied_caps: list[dict[str, Any]] = []

    # Signal 1: Instance availability (binary)
    if instance_available is True:
        signals["availability"] = 1.0
    elif instance_available is False:
        signals["availability"] = 0.0
    else:
        signals["availability"] = 0.5  # Unknown — neutral

    # Signal 2: Pricing/GA status (binary)
    if pricing_available is True:
        signals["pricing"] = 1.0
    elif pricing_available is False:
        signals["pricing"] = 0.0
    else:
        signals["pricing"] = 0.5  # Unknown

    # Signal 3: Quota sufficiency (ratio)
    if quota_limit is not None and requested_count > 0:
        ratio = quota_limit / requested_count
        signals["quota"] = min(ratio, 1.0)  # Cap at 1.0
    else:
        signals["quota"] = 0.5  # Unknown

    # Signal 4: Spot Placement Score (normalized)
    if spot_placement_score is not None:
        signals["spot_placement"] = spot_placement_score / 10.0
    else:
        signals["spot_placement"] = 0.5  # Unknown

    # Signal 5: Region maturity (from region model, per-GPU-family aware)
    signals["region_maturity"] = get_region_score(region, gpu_family)

    # Side signal: customer ICE history. Real capacity errors observed in the
    # customer's account override the SPS proxy. Cap spot_placement downward
    # so a strong SPS reading can't overrule lived experience.
    if customer_ice_events is not None and customer_ice_events >= 0:
        if customer_ice_events == 0:
            signals["ice_history_signal"] = 1.0
        elif customer_ice_events <= 3:
            signals["ice_history_signal"] = 0.7
            _cap_signal(
                signals,
                applied_caps,
                signal_name="spot_placement",
                cap_to=0.6,
                cap_kind="ice_history_isolated",
                description=(
                    f"ICE history: {customer_ice_events} isolated event(s) "
                    "in lookback window capped spot_placement at 0.6"
                ),
            )
        elif customer_ice_events <= 10:
            signals["ice_history_signal"] = 0.3
            _cap_signal(
                signals,
                applied_caps,
                signal_name="spot_placement",
                cap_to=0.3,
                cap_kind="ice_history_recurring",
                description=(
                    f"ICE history: {customer_ice_events} recurring events "
                    "in lookback window capped spot_placement at 0.3"
                ),
            )
        else:
            signals["ice_history_signal"] = 0.0
            _cap_signal(
                signals,
                applied_caps,
                signal_name="spot_placement",
                cap_to=0.1,
                cap_kind="ice_history_severe",
                description=(
                    f"ICE history: {customer_ice_events} severe events "
                    "in lookback window capped spot_placement at 0.1"
                ),
            )
    else:
        signals["ice_history_signal"] = None  # not queried

    # Side signal: ODCR coverage. Reserved capacity is guaranteed regardless of
    # pool depth or observed ICE on the shared pool — lift the spot_placement
    # signal to at least the coverage ratio. Applied AFTER the ICE cap so an
    # active ODCR can still rescue the reserved-capacity portion.
    if odcr_available_capacity is not None and requested_count > 0:
        coverage = min(odcr_available_capacity / requested_count, 1.0)
        signals["odcr_coverage"] = round(coverage, 2)
        _lift_signal(
            signals,
            applied_caps,
            signal_name="spot_placement",
            lift_to=coverage,
            cap_kind="odcr_coverage_lift",
            description=(
                f"ODCR covers {odcr_available_capacity}/{requested_count} = "
                f"{round(coverage, 2) * 100:.0f}% of the ask; spot_placement "
                f"floored at coverage ratio"
            ),
        )
    else:
        signals["odcr_coverage"] = 0.0

    # Side signals: Capacity Blocks for ML (Tier 1), SageMaker Flexible
    # Training Plans (Tier 2), and heterogeneous-endpoint fallback
    # (architectural, SageMaker inference only). All three are WEAKER lifts
    # than ODCR because they are *mitigation paths* rather than reserved
    # capacity — the customer still has to purchase (CBs / FTP) or configure
    # (fallback pool) and have the launch window align. Floor the
    # spot_placement signal at 0.4 so a weak shared-pool reading doesn't
    # drive the verdict when a mitigation path is confirmed available.
    # Applied AFTER ODCR so an actual ODCR coverage still wins. Does NOT
    # override the ICE severity final-score caps below — those enforce
    # "real failures beat available-but-not-reserved capacity".
    _MITIGATION_PATH_FLOOR = 0.4
    _CB_FTP_FLOOR = _MITIGATION_PATH_FLOOR  # backwards-compat alias; the
    # name is referenced in a few comments pre-dating the heterogeneous
    # fallback lift. Both names resolve to the same floor; keep the alias
    # until the next scoring refactor so a grep of "CB_FTP_FLOOR" still
    # finds the control.
    if capacity_blocks_available is True:
        signals["capacity_blocks_signal"] = 1.0
        _lift_signal(
            signals,
            applied_caps,
            signal_name="spot_placement",
            lift_to=_CB_FTP_FLOOR,
            cap_kind="capacity_blocks_available_lift",
            description=(
                "Capacity Blocks for ML inventory confirmed available for "
                "this instance type / region (Tier 1 mitigation path); "
                f"spot_placement floored at {_CB_FTP_FLOOR}"
            ),
        )
    elif capacity_blocks_available is False:
        signals["capacity_blocks_signal"] = 0.0
    else:
        signals["capacity_blocks_signal"] = None  # not queried

    if training_plans_available is True:
        signals["training_plans_signal"] = 1.0
        _lift_signal(
            signals,
            applied_caps,
            signal_name="spot_placement",
            lift_to=_CB_FTP_FLOOR,
            cap_kind="training_plans_available_lift",
            description=(
                "SageMaker Flexible Training Plans inventory confirmed "
                "available for this instance type / region (Tier 2 "
                f"mitigation path); spot_placement floored at {_CB_FTP_FLOOR}"
            ),
        )
    elif training_plans_available is False:
        signals["training_plans_signal"] = 0.0
    else:
        signals["training_plans_signal"] = None  # not queried

    if heterogeneous_fallback_available is True:
        signals["heterogeneous_fallback_signal"] = 1.0
        _lift_signal(
            signals,
            applied_caps,
            signal_name="spot_placement",
            lift_to=_MITIGATION_PATH_FLOOR,
            cap_kind="heterogeneous_fallback_available_lift",
            description=(
                "Heterogeneous-endpoint fallback candidate confirmed "
                "available (SageMaker capacity-aware endpoint pairing — "
                "primary + fallback instance pools, SageMaker ScaleOut "
                "provisions whichever has launchable capacity); "
                f"spot_placement floored at {_MITIGATION_PATH_FLOOR}"
            ),
        )
    elif heterogeneous_fallback_available is False:
        signals["heterogeneous_fallback_signal"] = 0.0
    else:
        signals["heterogeneous_fallback_signal"] = None  # not queried

    # Compute weighted base score from post-adjustment signals. This is the
    # number a consumer would reproduce from ``signals × weights`` alone.
    base_score = sum(signals[key] * weights[key] for key in weights)
    base_score = round(base_score, 4)  # avoid float-noise across platforms

    score = base_score

    # Hard gates: if instance not available or not GA, cap score
    if instance_available is False:
        score = _cap_final_score(
            score,
            applied_caps,
            cap_to=0.1,
            cap_kind="hard_gate_unavailable",
            description=(
                "Instance type not offered in region — categorical block; "
                "final score capped at 0.1 regardless of other signals"
            ),
        )
    if pricing_available is False and instance_available is not False:
        score = _cap_final_score(
            score,
            applied_caps,
            cap_to=0.2,
            cap_kind="hard_gate_not_ga",
            description=(
                "No SageMaker pricing for this instance/region — likely "
                "not GA; final score capped at 0.2"
            ),
        )

    # Hard cap on ICE history: real customer-side capacity errors are ground
    # truth. Even if every other signal is green, recurring or severe ICE
    # means the deploy is at material risk. A fully-covering ODCR is the only
    # legitimate escape hatch — reserved capacity is unaffected by shared-pool
    # ICE, so when ODCR alone covers the ask we let the score reflect that.
    odcr_fully_covers = (
        odcr_available_capacity is not None
        and requested_count > 0
        and odcr_available_capacity >= requested_count
    )
    if customer_ice_events is not None and customer_ice_events > 3:
        if not odcr_fully_covers:
            if customer_ice_events > 10:
                score = _cap_final_score(
                    score,
                    applied_caps,
                    cap_to=0.4,
                    cap_kind="ice_ceiling_severe",
                    description=(
                        f"Severe ICE history ({customer_ice_events} events) "
                        "without full ODCR coverage; final score capped at 0.4 "
                        "(HIGH RISK ceiling)"
                    ),
                )
            else:
                score = _cap_final_score(
                    score,
                    applied_caps,
                    cap_to=0.65,
                    cap_kind="ice_ceiling_recurring",
                    description=(
                        f"Recurring ICE history ({customer_ice_events} events) "
                        "without full ODCR coverage; final score capped at 0.65 "
                        "(DEPLOYABLE WITH PREPARATION ceiling)"
                    ),
                )

    # Hard cap on a critical Spot Placement Score. A score of 3/10 or
    # below is what the SPS tool itself classifies as "weak" or "critical"
    # capacity — but with a weight of only 0.25 in the base score, a single
    # 2/10 reading barely moves the needle. A fully-covering ODCR overrides
    # this cap the same way it overrides ICE severity.
    if (
        spot_placement_score is not None
        and spot_placement_score <= 3
        and not odcr_fully_covers
    ):
        score = _cap_final_score(
            score,
            applied_caps,
            cap_to=0.4,
            cap_kind="sps_ceiling_critical",
            description=(
                f"Spot Placement Score {spot_placement_score}/10 is at or "
                "below the critical pool-depth threshold and no ODCR fully "
                "covers the ask; final score capped at 0.4 (HIGH RISK "
                "ceiling)"
            ),
        )

    # When the ICE signal was never observed (tool didn't run, IAM
    # error, no CloudTrail trail, or the account-level rate limiter refused
    # the call), we CANNOT claim DEPLOYABLE. "Not queried" is not "clean".
    # Cap at 0.65 (DEPLOYABLE WITH PREPARATION ceiling) so the customer is
    # nudged toward mitigation until the signal is obtained.
    ice_signal_unavailable = customer_ice_events is None or (
        isinstance(customer_ice_capacity_signal, str)
        and customer_ice_capacity_signal
        in (
            "unknown",
            "rate_limited",
            "unresolved",
            "metric_filter_not_deployed",
        )
    )
    if ice_signal_unavailable and not odcr_fully_covers:
        reason = "not queried"
        if customer_ice_capacity_signal == "rate_limited":
            reason = "CloudTrail rate limit refused the lookup"
        elif customer_ice_capacity_signal == "unknown":
            reason = "CloudTrail lookup failed (IAM / trail config)"
        elif customer_ice_capacity_signal == "unresolved":
            # SageMaker capacity events were found but couldn't be
            # attributed to an instance type — typically missing
            # sagemaker:DescribeEndpointConfig or a deleted endpoint
            # config. See Follow-up A in the code review.
            reason = (
                "SageMaker capacity events found but not attributable "
                "to an instance type (missing "
                "sagemaker:DescribeEndpointConfig or deleted endpoint "
                "config)"
            )
        elif customer_ice_capacity_signal == "metric_filter_not_deployed":
            # The precise CloudWatch metric-filter path (Follow-up B)
            # produced no datapoints, most likely because the customer
            # hasn't deployed the companion CFN template. The agent
            # should have already fallen back to get_ice_history; if
            # THAT also produced no signal, both readings landed on
            # this branch and the scorer legitimately doesn't know the
            # customer's ICE history.
            reason = (
                "customer has not deployed the "
                "docs/samples/customer-side-ice-metric-filter.yaml "
                "companion stack and the CloudTrail LookupEvents "
                "fallback also produced no usable signal"
            )
        score = _cap_final_score(
            score,
            applied_caps,
            cap_to=0.65,
            cap_kind="ice_signal_unavailable",
            description=(
                f"Customer-side ICE-history signal is unavailable "
                f"({reason}) and no ODCR fully covers the ask; final score "
                "capped at 0.65 (DEPLOYABLE WITH PREPARATION ceiling) — "
                "'not queried' is not the same as 'clean'."
            ),
        )

    # Clamp to [0, 1] and round for stable output.
    score = max(0.0, min(1.0, round(score, 2)))

    # Single source of truth for label + verdict. Bucket boundaries
    # now align. A score's ``label`` and ``verdict`` come from the same
    # bucket, so downstream reports can't disagree.
    #
    # Bucket table (score >= boundary, top-down):
    #   0.80 -> HIGH   / DEPLOYABLE
    #   0.65 -> MODERATE / DEPLOYABLE WITH PREPARATION (upper)
    #   0.50 -> MODERATE / DEPLOYABLE WITH PREPARATION (lower)
    #   0.30 -> MODERATE-LOW / HIGH RISK
    #    else -> VERY LOW / NOT FEASIBLE
    if score >= 0.80:
        label, verdict = "HIGH", "DEPLOYABLE"
    elif score >= 0.65:
        label, verdict = "MODERATE", "DEPLOYABLE WITH PREPARATION"
    elif score >= 0.50:
        label, verdict = "MODERATE", "DEPLOYABLE WITH PREPARATION"
    elif score >= 0.30:
        label, verdict = "MODERATE-LOW", "HIGH RISK"
    else:
        label, verdict = "VERY LOW", "NOT FEASIBLE"

    # Categorical NOT-FEASIBLE overrides for hard gates. The score itself is
    # already capped by _cap_final_score, but the verdict must be explicit
    # so downstream code doesn't rely on numeric boundaries.
    if instance_available is False or pricing_available is False:
        verdict = "NOT FEASIBLE"

    return {
        "score": score,
        "label": label,
        "verdict": verdict,
        "signals": signals,
        "weights": weights,
        "base_score": base_score,
        "applied_caps": applied_caps,
    }


def _cap_signal(
    signals: dict[str, Any],
    applied_caps: list[dict[str, Any]],
    *,
    signal_name: str,
    cap_to: float,
    cap_kind: str,
    description: str,
) -> None:
    """Cap ``signals[signal_name]`` downward to ``cap_to``, recording the
    change in ``applied_caps`` when the cap actually fires.
    """
    current = signals.get(signal_name)
    if not isinstance(current, (int, float)):
        return
    if current <= cap_to:
        return
    applied_caps.append(
        {
            "kind": cap_kind,
            "target": f"signals.{signal_name}",
            "from_value": round(float(current), 4),
            "to_value": round(float(cap_to), 4),
            "description": description,
        }
    )
    signals[signal_name] = cap_to


def _lift_signal(
    signals: dict[str, Any],
    applied_caps: list[dict[str, Any]],
    *,
    signal_name: str,
    lift_to: float,
    cap_kind: str,
    description: str,
) -> None:
    """Lift ``signals[signal_name]`` upward to at least ``lift_to``, recording
    the change in ``applied_caps`` when the lift actually fires.
    """
    current = signals.get(signal_name)
    if not isinstance(current, (int, float)):
        return
    if current >= lift_to:
        return
    applied_caps.append(
        {
            "kind": cap_kind,
            "target": f"signals.{signal_name}",
            "from_value": round(float(current), 4),
            "to_value": round(float(lift_to), 4),
            "description": description,
        }
    )
    signals[signal_name] = lift_to


def _cap_final_score(
    current: float,
    applied_caps: list[dict[str, Any]],
    *,
    cap_to: float,
    cap_kind: str,
    description: str,
) -> float:
    """Cap the final score downward to ``cap_to``, recording the change in
    ``applied_caps`` when the cap actually fires. Returns the new value.
    """
    if current <= cap_to:
        return current
    applied_caps.append(
        {
            "kind": cap_kind,
            "target": "score",
            "from_value": round(float(current), 4),
            "to_value": round(float(cap_to), 4),
            "description": description,
        }
    )
    return cap_to
