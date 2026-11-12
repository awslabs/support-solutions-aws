# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Recordable, offline demo of the GPU Capacity Planning Agent.

This script produces the same experience as ``examples/agent_demo.py``
without needing AWS credentials or a live Bedrock endpoint. Bedrock's
``converse`` API is mocked to script a plausible tool-use loop for each
scenario, and each tool call returns a canned fixture that reflects the
real API's response shape. The **deterministic scorer**
(``src/confidence.py``) runs live over those fixture outputs, so the
``confidence_assessments`` block you see on screen is computed the same
way it would be in production.

Usage
-----
    # Run the full 4-scenario recording (default)
    python examples/demo_recorded.py

    # Run one scenario
    python examples/demo_recorded.py feasible
    python examples/demo_recorded.py blocked
    python examples/demo_recorded.py ice_override
    python examples/demo_recorded.py adversarial

    # Adjust pacing (seconds between events; default 0.6)
    DEMO_SPEED=0 python examples/demo_recorded.py       # instant, for QA
    DEMO_SPEED=1.5 python examples/demo_recorded.py    # slow, for recording

    # Disable ANSI colour (e.g. piping to a file)
    NO_COLOR=1 python examples/demo_recorded.py

The scenarios:

    feasible       Mistral 7B in us-west-2 -> DEPLOYABLE
    blocked        22 g6 in af-south-1     -> NOT FEASIBLE (g6 not GA)
    ice_override   30 g5.4xlarge, ICE hist -> HIGH RISK (ICE beats SPS)
    adversarial    300 mixed in sa-east-1  -> NOT FEASIBLE (quota gap)

Nothing this script does mutates AWS state or requires network access.
It is safe to run repeatedly during a recording take.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent import CapacityDecisionAgent  # noqa: E402
from src.tools import ALL_TOOLS  # noqa: E402


# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

_USE_COLOR = os.environ.get("NO_COLOR") is None and sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


BOLD = lambda s: _c(s, "1")           # noqa: E731
DIM = lambda s: _c(s, "2")            # noqa: E731
GREEN = lambda s: _c(s, "32")         # noqa: E731
YELLOW = lambda s: _c(s, "33")        # noqa: E731
BLUE = lambda s: _c(s, "34")          # noqa: E731
MAGENTA = lambda s: _c(s, "35")       # noqa: E731
CYAN = lambda s: _c(s, "36")          # noqa: E731
RED = lambda s: _c(s, "31")           # noqa: E731


def _speed() -> float:
    """Delay multiplier between demo events. 0 disables pauses entirely."""
    try:
        return float(os.environ.get("DEMO_SPEED", "0.6"))
    except ValueError:
        return 0.6


def _pause(seconds: float = 1.0) -> None:
    delay = seconds * _speed()
    if delay > 0:
        time.sleep(delay)


def _rule(char: str = "━", length: int = 80) -> None:
    print(DIM(char * length))


def _banner(title: str, subtitle: str = "") -> None:
    _rule("═")
    print(BOLD(CYAN(f"  {title}")))
    if subtitle:
        print(DIM(f"  {subtitle}"))
    _rule("═")
    _pause(0.5)


def _typewriter(prefix: str, text: str, per_char: float = 0.008) -> None:
    """Slow-type ``text`` after ``prefix`` for a live-typing feel.

    Bounded well below _speed()-driven pauses so it doesn't dominate the
    recording; skip entirely at DEMO_SPEED=0.
    """
    print(prefix, end="", flush=True)
    if _speed() == 0:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        if ch != " ":
            time.sleep(per_char * _speed())
    print()


# ---------------------------------------------------------------------------
# Bedrock Converse mock — scripts a plausible tool-use loop per scenario
# ---------------------------------------------------------------------------


def _tool_use_turn(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    text_before: str = "",
) -> dict[str, Any]:
    """Compose a Bedrock ``converse`` response that asks for one tool call."""
    content: list[dict[str, Any]] = []
    if text_before:
        content.append({"text": text_before})
    content.append(
        {
            "toolUse": {
                "toolUseId": tool_use_id,
                "name": tool_name,
                "input": tool_input,
            }
        }
    )
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 800, "outputTokens": 120},
    }


def _end_turn(final_text: str) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": final_text}],
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 900, "outputTokens": 380},
    }


# ---------------------------------------------------------------------------
# Tool fixture patcher — replaces each tool's handler with a canned response
# ---------------------------------------------------------------------------


def _patch_tool_handlers(fixture_map: dict[str, Any]) -> dict[str, Any]:
    """Swap in canned handlers on ALL_TOOLS in-place; return the originals so
    the caller can restore.
    """
    originals: dict[str, Any] = {}
    for spec in ALL_TOOLS:
        name = spec["name"]
        originals[name] = spec["handler"]
        fixture = fixture_map.get(name)
        if fixture is None:
            # Sensible default for unused tools — return a minimal shape so
            # the agent doesn't crash if the model calls something we didn't
            # script.
            def _default_handler(fixture_name: str = name, **_kwargs) -> dict[str, Any]:
                return {"note": f"{fixture_name} not scripted for this scenario"}

            spec["handler"] = _default_handler
        else:
            def _make_handler(payload: Any = fixture):
                def _handler(**_kwargs):
                    return payload() if callable(payload) else payload

                return _handler

            spec["handler"] = _make_handler(fixture)
    return originals


def _restore_tool_handlers(originals: dict[str, Any]) -> None:
    for spec in ALL_TOOLS:
        if spec["name"] in originals:
            spec["handler"] = originals[spec["name"]]


# ---------------------------------------------------------------------------
# Scenario library — each entry is (title, user_query, bedrock_script, tool_fixtures)
# ---------------------------------------------------------------------------


def _scenarios() -> list[dict[str, Any]]:
    return [
        {
            "id": "feasible",
            "title": "Scenario 1: DEPLOYABLE — Mistral 7B in us-west-2",
            "subtitle": "Established region, small fleet, all signals green",
            "user_query": (
                "I need to deploy a Mistral 7B model for real-time inference "
                "in us-west-2 on 20 ml.g5.2xlarge instances. What's my risk?"
            ),
            "bedrock_script": [
                _tool_use_turn(
                    "check_instance_availability",
                    {"instance_type": "g5.2xlarge", "region": "us-west-2"},
                    "tu-avail-1",
                    text_before="Let me start by confirming instance availability in us-west-2.",
                ),
                _tool_use_turn(
                    "get_sagemaker_pricing",
                    {"instance_type": "g5.2xlarge", "region": "us-west-2"},
                    "tu-price-1",
                    text_before="Good — g5.2xlarge is offered across 4 AZs. Now checking pricing.",
                ),
                _tool_use_turn(
                    "get_sagemaker_quota",
                    {"instance_type": "g5.2xlarge", "region": "us-west-2"},
                    "tu-quota-1",
                    text_before="SageMaker pricing confirms GA at $1.5152/hr. Now the quota.",
                ),
                _tool_use_turn(
                    "get_spot_placement_score",
                    {"instance_type": "g5.2xlarge", "region": "us-west-2", "target_capacity": 20},
                    "tu-sps-1",
                    text_before="Quota is 100, plenty of headroom for 20. Checking pool depth.",
                ),
                _tool_use_turn(
                    "get_ice_history",
                    {"instance_type": "g5.2xlarge", "region": "us-west-2", "deployment_target": "auto"},
                    "tu-ice-1",
                    text_before="Spot Placement Score 9/10 — deep pool. Now confirming with ground truth (customer CloudTrail).",
                ),
                _tool_use_turn(
                    "check_existing_odcrs",
                    {"instance_type": "g5.2xlarge", "region": "us-west-2"},
                    "tu-odcr-1",
                    text_before="ICE history clean over the last 30 days. Verifying no wasted ODCR recommendation.",
                ),
                _end_turn(
                    "## Capacity Risk Assessment\n\n"
                    "### Signals Gathered\n"
                    "- ✅ Instance offered in us-west-2 across 4 AZs (source: DescribeInstanceTypeOfferings)\n"
                    "- ✅ SageMaker Hosting pricing GA at $1.5152/hr (source: AWS Pricing API)\n"
                    "- ✅ Quota 100 vs. requested 20 — 5× headroom (source: Service Quotas)\n"
                    "- ✅ Spot Placement Score 9/10 — deep capacity pool (source: GetSpotPlacementScores)\n"
                    "- ✅ ICE history: 0 events (last 30 days, both EC2 and SageMaker paths — source: CloudTrail LookupEvents)\n"
                    "- No existing ODCRs; not needed for this workload size (source: DescribeCapacityReservations)\n"
                    "- Region maturity: established (us-west-2, g5 family)\n\n"
                    "### Verdict Narrative\n"
                    "The deterministic scorer will return **DEPLOYABLE**. Every signal is "
                    "green: the instance is GA, quota is 5× the ask, the Spot Placement "
                    "Score confirms deep capacity, and us-west-2 is an established region "
                    "for the g5 family. This is a standard deployment — no ODCR or "
                    "Capacity Blocks needed.\n\n"
                    "## Cost Projection\n"
                    "- On-demand hourly: **$1.5152 × 20 = $30.30/hr**\n"
                    "- Monthly (730 hrs): **$22,122/mo**\n"
                    "- Annual: **$265,464/yr**\n\n"
                    "## Recommendations\n"
                    "1. Deploy with standard SageMaker endpoint configuration.\n"
                    "2. Monitor GPU utilization; consider ml.g6.2xlarge (L4) once available "
                    "in this region — ~20% cheaper for the same 24 GB GPU memory.\n"
                    "3. For fleets of this size, most customers have negotiated pricing "
                    "(PPA/EDP). Contact your AWS account team if you don't have one.\n\n"
                    "📖 Full SageMaker AI pricing details: https://aws.amazon.com/sagemaker/pricing/"
                ),
            ],
            "tool_fixtures": {
                "check_instance_availability": {
                    "instance_type": "g5.2xlarge",
                    "region": "us-west-2",
                    "available": True,
                    "azs": ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"],
                    "az_count": 4,
                    "interpretation": "g5.2xlarge is offered in us-west-2 across 4 AZs.",
                    "source": "ec2.DescribeInstanceTypeOfferings (availability-zone)",
                },
                "get_sagemaker_pricing": {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-west-2",
                    "component": "Hosting",
                    "hourly_usd": 1.5152,
                    "available": True,
                    "source": "AWS Pricing API",
                    "_notes": None,
                },
                "get_sagemaker_quota": {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "us-west-2",
                    "found": True,
                    "quota_code": "L-EXAMPLE",
                    "quota_name": "ml.g5.2xlarge for endpoint usage",
                    "current_limit": 100,
                    "adjustable": True,
                    "interpretation": "ml.g5.2xlarge endpoint quota in us-west-2: 100 instances.",
                    "source": "service-quotas.ListServiceQuotas",
                },
                "get_spot_placement_score": {
                    "instance_type": "g5.2xlarge",
                    "region": "us-west-2",
                    "target_capacity": 20,
                    "score": 9,
                    "scores_by_az": [{"az": "region-wide", "region": "us-west-2", "score": 9}],
                    "capacity_signal": "strong",
                    "interpretation": "Spot Placement Score 9/10 — Strong capacity signal.",
                    "source": "ec2.GetSpotPlacementScores",
                },
                "check_existing_odcrs": {
                    "instance_type": "g5.2xlarge",
                    "region": "us-west-2",
                    "availability_zone": None,
                    "reservation_count": 0,
                    "total_capacity": 0,
                    "available_capacity": 0,
                    "reservations": [],
                    "capacity_signal": "none",
                    "interpretation": "No active On-Demand Capacity Reservations found.",
                    "truncated": False,
                    "source": "ec2.DescribeCapacityReservations",
                },
                "get_ice_history": {
                    "instance_type": "g5.2xlarge",
                    "region": "us-west-2",
                    "deployment_target": "auto",
                    "lookback_days": 30,
                    "event_count": 0,
                    "events_by_target": {"ec2": 0, "sagemaker": 0},
                    "events_by_az": {},
                    "sample_events": [],
                    "capacity_signal": "clean",
                    "interpretation": (
                        "No capacity errors observed for this instance type "
                        "in the lookback window — the customer's own recent "
                        "history shows no capacity issues on the queried "
                        "deployment target(s)."
                    ),
                    "truncated": False,
                    "source": "cloudtrail.LookupEvents",
                },
            },
        },
        {
            "id": "blocked",
            "title": "Scenario 2: NOT FEASIBLE — 22 g6 in af-south-1",
            "subtitle": "g6 not GA in Cape Town; agent recommends g5 fallback",
            "user_query": (
                "I'm planning 22 g6.2xlarge instances in af-south-1 for prod "
                "inference launching in 2 weeks. What's my risk?"
            ),
            "bedrock_script": [
                _tool_use_turn(
                    "check_instance_availability",
                    {"instance_type": "g6.2xlarge", "region": "af-south-1"},
                    "tu-avail-2",
                    text_before="Cape Town is a newer region — availability is my first check.",
                ),
                _tool_use_turn(
                    "get_sagemaker_pricing",
                    {"instance_type": "g6.2xlarge", "region": "af-south-1"},
                    "tu-price-2",
                    text_before="g6 is NOT offered in af-south-1. Confirming with pricing (Pricing API tracks GA).",
                ),
                _tool_use_turn(
                    "find_alternative_instances",
                    {"instance_type": "g6.2xlarge", "region": "af-south-1", "max_results": 3},
                    "tu-alt-2",
                    text_before="No SageMaker pricing either — g6 is not GA in Cape Town. Finding alternatives.",
                ),
                _end_turn(
                    "## Capacity Risk Assessment\n\n"
                    "### Signals Gathered\n"
                    "- ❌ g6.2xlarge is NOT offered in af-south-1 (source: DescribeInstanceTypeOfferings)\n"
                    "- ❌ No SageMaker pricing — confirms g6 is not yet GA in Cape Town (source: AWS Pricing API)\n"
                    "- Region maturity: newer / limited-capacity region\n\n"
                    "### Verdict Narrative\n"
                    "The deterministic scorer will return **NOT FEASIBLE**. This is a "
                    "categorical block — no amount of quota increase or ODCR fixes it "
                    "because the instance type isn't offered in the region. The Pricing "
                    "API confirms GA hasn't happened yet.\n\n"
                    "## Recommendations\n"
                    "1. **Switch to g5 family** — ml.g5.2xlarge is GA in af-south-1 across "
                    "3 AZs. Uses NVIDIA A10G (24 GB); same 24 GB GPU memory as g6.2xlarge (L4).\n"
                    "2. **Pre-create an ODCR** before the 2-week launch — af-south-1 has "
                    "limited g5 capacity depth. Reserved capacity is the only way to "
                    "guarantee 22 instances.\n"
                    "3. **Open an AWS Support case** for capacity assistance now. "
                    "For deployment decisions of this scale in a limited-capacity region, "
                    "engage your AWS account team directly — this agent provides best-effort "
                    "signals, not strategic capacity commitments.\n"
                    "4. **Alternative regions:** if data residency permits, eu-west-1 (Ireland) "
                    "has ~150ms latency to Cape Town and much deeper g5/g6 capacity.\n\n"
                    "📖 Full SageMaker AI pricing details: https://aws.amazon.com/sagemaker/pricing/"
                ),
            ],
            "tool_fixtures": {
                "check_instance_availability": {
                    "instance_type": "g6.2xlarge",
                    "region": "af-south-1",
                    "available": False,
                    "azs": [],
                    "interpretation": "g6.2xlarge is NOT offered in af-south-1.",
                    "source": "ec2.DescribeInstanceTypeOfferings (region)",
                },
                "get_sagemaker_pricing": {
                    "instance_type": "ml.g6.2xlarge",
                    "region": "af-south-1",
                    "component": "Hosting",
                    "hourly_usd": None,
                    "available": False,
                    "source": "AWS Pricing API",
                    "_notes": (
                        "No SageMaker Hosting pricing for ml.g6.2xlarge in af-south-1. "
                        "Likely not yet GA in this region."
                    ),
                },
                "find_alternative_instances": {
                    "reference": "g6.2xlarge",
                    "reference_specs": {
                        "instance_type": "g6.2xlarge",
                        "vcpus": 8,
                        "memory_gib": 32.0,
                        "gpus": 1,
                        "gpu_memory_gib": 24.0,
                        "gpu_manufacturer": "NVIDIA",
                    },
                    "region": "af-south-1",
                    "alternatives": [
                        {
                            "instance_type": "g5.2xlarge",
                            "vcpus": 8,
                            "memory_gib": 32.0,
                            "gpus": 1,
                            "gpu_memory_gib": 24.0,
                            "gpu_manufacturer": "NVIDIA",
                            "similarity_score": 1.0,
                        },
                        {
                            "instance_type": "g5.4xlarge",
                            "vcpus": 16,
                            "memory_gib": 64.0,
                            "gpus": 1,
                            "gpu_memory_gib": 24.0,
                            "gpu_manufacturer": "NVIDIA",
                            "similarity_score": 0.7,
                        },
                    ],
                    "interpretation": (
                        "Top alternatives in af-south-1: g5.2xlarge (1.0), g5.4xlarge (0.7)."
                    ),
                    "source": "ec2.DescribeInstanceTypes + DescribeInstanceTypeOfferings",
                },
            },
        },
        {
            "id": "ice_override",
            "title": "Scenario 3: HIGH RISK — ICE history overrides SPS",
            "subtitle": "Customer's CloudTrail shows real capacity errors; scorer caps the verdict",
            "user_query": (
                "I want to deploy 30 ml.g5.4xlarge in us-east-1 for prod inference. "
                "We've been seeing capacity errors here recently — is it safe?"
            ),
            "bedrock_script": [
                _tool_use_turn(
                    "check_instance_availability",
                    {"instance_type": "g5.4xlarge", "region": "us-east-1"},
                    "tu-avail-3",
                    text_before="us-east-1 is established; running the standard signal set.",
                ),
                _tool_use_turn(
                    "get_sagemaker_pricing",
                    {"instance_type": "g5.4xlarge", "region": "us-east-1"},
                    "tu-price-3",
                ),
                _tool_use_turn(
                    "get_sagemaker_quota",
                    {"instance_type": "g5.4xlarge", "region": "us-east-1"},
                    "tu-quota-3",
                ),
                _tool_use_turn(
                    "get_spot_placement_score",
                    {"instance_type": "g5.4xlarge", "region": "us-east-1", "target_capacity": 30},
                    "tu-sps-3",
                    text_before="Everything looks fine so far — quota 200, plenty of room. Checking pool depth.",
                ),
                _tool_use_turn(
                    "get_ice_history",
                    {"instance_type": "g5.4xlarge", "region": "us-east-1", "lookback_days": 30},
                    "tu-ice-3",
                    text_before=(
                        "Spot Placement Score 8/10 says the pool is healthy — but the user "
                        "mentioned real capacity errors. Pulling their CloudTrail to check."
                    ),
                ),
                _tool_use_turn(
                    "check_existing_odcrs",
                    {"instance_type": "g5.4xlarge", "region": "us-east-1"},
                    "tu-odcr-3",
                    text_before=(
                        "18 ICE events on this exact type/region in the last 30 days. "
                        "The proxy signal lied — ground truth beats it. Checking for ODCR rescue."
                    ),
                ),
                _end_turn(
                    "## Capacity Risk Assessment\n\n"
                    "### Signals Gathered\n"
                    "- ✅ Instance offered in us-east-1 across 6 AZs (source: DescribeInstanceTypeOfferings)\n"
                    "- ✅ SageMaker Hosting pricing GA at $2.03/hr (source: AWS Pricing API)\n"
                    "- ✅ Quota 200 vs. requested 30 (source: Service Quotas)\n"
                    "- ⚠️ Spot Placement Score **8/10** — proxy says the pool is fine\n"
                    "- 🚨 **Customer ICE history: 18 InsufficientInstanceCapacity events "
                    "in the last 30 days** on ml.g5.4xlarge in us-east-1 (source: CloudTrail LookupEvents)\n"
                    "- No existing ODCRs to rescue this deployment\n\n"
                    "### Verdict Narrative\n"
                    "The deterministic scorer will return **HIGH RISK**. Two signals are "
                    "in conflict: Spot Placement Score is a proxy at 8/10, but the customer's "
                    "own CloudTrail shows 18 real ICE events on this exact instance type "
                    "and region in the last 30 days. **Ground truth beats the proxy** — the "
                    "scorer caps `spot_placement` at 0.1 and caps the final score at 0.4 "
                    "(HIGH RISK ceiling) because severe ICE without ODCR cover is a hard "
                    "signal that the shared pool can't reliably fulfill this ask.\n\n"
                    "This is exactly the case the scoring hierarchy was designed for. "
                    "SPS reads pool depth in aggregate; ICE events are what the customer "
                    "actually experienced. When they disagree, ICE wins.\n\n"
                    "## Recommendations (HIGH RISK)\n"
                    "1. **Pre-create an ODCR for 30 × ml.g5.4xlarge in us-east-1** before "
                    "launch. Reserved capacity is unaffected by shared-pool ICE — it's the "
                    "only signal-consistent way to guarantee 30 instances.\n"
                    "2. **Open an AWS Support capacity assistance case** with the CloudTrail "
                    "event IDs attached. AWS has visibility into pool depth this agent "
                    "doesn't.\n"
                    "3. **Do not deploy ad-hoc.** The next 30 launches will hit the same "
                    "ICE the customer has been hitting all month.\n"
                    "4. **Consider phased rollout** (10 → 20 → 30) with monitoring between "
                    "phases if ODCR isn't feasible before the deadline.\n\n"
                    "📖 Full SageMaker AI pricing details: https://aws.amazon.com/sagemaker/pricing/"
                ),
            ],
            "tool_fixtures": {
                "check_instance_availability": {
                    "instance_type": "g5.4xlarge",
                    "region": "us-east-1",
                    "available": True,
                    "azs": ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d", "us-east-1e", "us-east-1f"],
                    "az_count": 6,
                    "interpretation": "g5.4xlarge is offered in us-east-1 across 6 AZs.",
                    "source": "ec2.DescribeInstanceTypeOfferings (availability-zone)",
                },
                "get_sagemaker_pricing": {
                    "instance_type": "ml.g5.4xlarge",
                    "region": "us-east-1",
                    "component": "Hosting",
                    "hourly_usd": 2.03,
                    "available": True,
                    "source": "AWS Pricing API",
                    "_notes": None,
                },
                "get_sagemaker_quota": {
                    "instance_type": "ml.g5.4xlarge",
                    "region": "us-east-1",
                    "found": True,
                    "quota_code": "L-EXAMPLE-4X",
                    "quota_name": "ml.g5.4xlarge for endpoint usage",
                    "current_limit": 200,
                    "adjustable": True,
                    "interpretation": "ml.g5.4xlarge endpoint quota in us-east-1: 200 instances.",
                    "source": "service-quotas.ListServiceQuotas",
                },
                "get_spot_placement_score": {
                    "instance_type": "g5.4xlarge",
                    "region": "us-east-1",
                    "target_capacity": 30,
                    "score": 8,
                    "scores_by_az": [{"az": "region-wide", "region": "us-east-1", "score": 8}],
                    "capacity_signal": "strong",
                    "interpretation": (
                        "Spot Placement Score 8/10 — Strong signal from SPS. NOTE: this is "
                        "a PROXY. The customer's own ICE history overrides when in conflict."
                    ),
                    "source": "ec2.GetSpotPlacementScores",
                },
                "get_ice_history": {
                    "instance_type": "g5.4xlarge",
                    "region": "us-east-1",
                    "lookback_days": 30,
                    "window_start": "2026-06-08T12:00:00+00:00",
                    "window_end": "2026-07-08T12:00:00+00:00",
                    "event_count": 18,
                    "events_by_az": {"us-east-1a": 7, "us-east-1c": 6, "us-east-1f": 5},
                    "sample_events": [
                        {
                            "event_time": "2026-07-06T14:22:41+00:00",
                            "error_code": "Server.InsufficientInstanceCapacity",
                            "availability_zone": "us-east-1a",
                            "event_id": "abcd1234-...",
                        },
                    ],
                    "capacity_signal": "severe",
                    "interpretation": (
                        "18 ICE events observed in the lookback window — severe and "
                        "persistent capacity issues."
                    ),
                    "truncated": False,
                    "source": "cloudtrail.LookupEvents",
                },
                "check_existing_odcrs": {
                    "instance_type": "g5.4xlarge",
                    "region": "us-east-1",
                    "availability_zone": None,
                    "reservation_count": 0,
                    "total_capacity": 0,
                    "available_capacity": 0,
                    "reservations": [],
                    "capacity_signal": "none",
                    "interpretation": "No active ODCRs — launch will draw from the shared pool.",
                    "truncated": False,
                    "source": "ec2.DescribeCapacityReservations",
                },
            },
        },
        {
            "id": "adversarial",
            "title": "Scenario 4: NOT FEASIBLE — 300 mixed instances in sa-east-1 with LGPD",
            "subtitle": "Large fleet, limited region, data residency, quota gap — escalation-level decision",
            "user_query": (
                "A large enterprise customer needs 175 ml.g5.2xlarge + 100 ml.g5.4xlarge + "
                "25 multi-GPU instances in sa-east-1 for production AI expansion. Data "
                "residency (LGPD) prevents cross-region pivot. What are the options?"
            ),
            "bedrock_script": [
                _tool_use_turn(
                    "check_instance_availability",
                    {"instance_type": "g5.2xlarge", "region": "sa-east-1"},
                    "tu-avail-4a",
                    text_before="Three instance types, one limited region, LGPD constraint. Starting with the biggest ask.",
                ),
                _tool_use_turn(
                    "get_sagemaker_quota",
                    {"instance_type": "g5.2xlarge", "region": "sa-east-1"},
                    "tu-quota-4a",
                ),
                _tool_use_turn(
                    "get_spot_placement_score",
                    {"instance_type": "g5.2xlarge", "region": "sa-east-1", "target_capacity": 175},
                    "tu-sps-4a",
                    text_before="Quota is 2 vs. need 175 — that alone is disqualifying. Verifying pool depth.",
                ),
                _tool_use_turn(
                    "check_existing_odcrs",
                    {"instance_type": "g5.2xlarge", "region": "sa-east-1"},
                    "tu-odcr-4a",
                ),
                _end_turn(
                    "## Capacity Risk Assessment\n\n"
                    "### Signals Gathered (g5.2xlarge in sa-east-1)\n"
                    "- ✅ Instance offered across 3 AZs\n"
                    "- 🚨 **Quota 2 vs. requested 175 — 87× gap** (source: Service Quotas)\n"
                    "- 🚨 Spot Placement Score 3/10 — critical pool depth for this fleet size\n"
                    "- No existing ODCRs\n"
                    "- Region maturity: **limited** (sa-east-1)\n"
                    "- Data residency: **LGPD prevents cross-region** — cannot suggest US alternatives\n\n"
                    "### Verdict Narrative\n"
                    "The deterministic scorer will return **HIGH RISK**. The instance is "
                    "available and priced, so it doesn't hit a hard-gate NOT-FEASIBLE — "
                    "but every soft signal is pulling the score down: quota 2 vs. 175 "
                    "(87× gap), Spot Placement Score 3/10, limited region maturity for "
                    "the g5 family in sa-east-1, and no ODCR to guarantee anything.\n\n"
                    "HIGH RISK is the honest verdict when the signals say 'technically "
                    "possible if you throw quota-increase + Capacity Blocks + a support "
                    "case at it, but no responsible account team would greenlight this ad-hoc'. "
                    "LGPD closes the usual escape hatch of falling back to us-east-1. "
                    "For a $15M+ ARR commitment this is escalation-level.\n\n"
                    "The pattern extends to g5.4xlarge (quota 0 vs. 100) and the multi-GPU "
                    "tier — same region, same constraints, same verdict.\n\n"
                    "## Cost Projection (if capacity is secured)\n"
                    "- On-demand hourly: ~$1,800/hr\n"
                    "- Monthly: **~$1.3M/mo**\n"
                    "- Annual: **~$15.8M/yr**\n\n"
                    "## Recommendations (HIGH RISK — escalation-level)\n"
                    "1. **Engage the AWS account team immediately.** This agent provides "
                    "best-effort signals from public APIs, not strategic capacity commitments. "
                    "For deployment decisions of this scale in a limited-capacity region "
                    "with LGPD, the account team + capacity engineering team have visibility "
                    "this tool does not.\n"
                    "2. **Request quota increases across all three types** — the 87× gap for "
                    "g5.2xlarge and 250× gap for g5.4xlarge signal that the customer needs a "
                    "capacity-planning conversation, not a Service Quotas ticket.\n"
                    "3. **Pre-purchase Capacity Blocks for ML** if available in sa-east-1 — "
                    "reserved capacity is the only signal-consistent path to a 300-instance "
                    "commitment in a limited-capacity region.\n"
                    "4. **Phased deployment with monitoring** between phases — even after "
                    "capacity is secured, a big-bang launch of 300 instances in sa-east-1 is "
                    "operationally risky.\n\n"
                    "> Note: for fleets of this size, most customers have negotiated pricing "
                    "(PPA/EDP) — the numbers above are list price.\n\n"
                    "📖 Full SageMaker AI pricing details: https://aws.amazon.com/sagemaker/pricing/"
                ),
            ],
            "tool_fixtures": {
                "check_instance_availability": {
                    "instance_type": "g5.2xlarge",
                    "region": "sa-east-1",
                    "available": True,
                    "azs": ["sa-east-1a", "sa-east-1b", "sa-east-1c"],
                    "az_count": 3,
                    "interpretation": "g5.2xlarge is offered in sa-east-1 across 3 AZs.",
                    "source": "ec2.DescribeInstanceTypeOfferings (availability-zone)",
                },
                "get_sagemaker_quota": {
                    "instance_type": "ml.g5.2xlarge",
                    "region": "sa-east-1",
                    "found": True,
                    "quota_code": "L-EXAMPLE-GRU",
                    "quota_name": "ml.g5.2xlarge for endpoint usage",
                    "current_limit": 2,
                    "adjustable": True,
                    "interpretation": (
                        "ml.g5.2xlarge endpoint quota in sa-east-1: 2 instances. Adjustable."
                    ),
                    "source": "service-quotas.ListServiceQuotas",
                },
                "get_spot_placement_score": {
                    "instance_type": "g5.2xlarge",
                    "region": "sa-east-1",
                    "target_capacity": 175,
                    "score": 3,
                    "scores_by_az": [{"az": "region-wide", "region": "sa-east-1", "score": 3}],
                    "capacity_signal": "weak",
                    "interpretation": (
                        "Spot Placement Score 3/10 — weak. Significant ICE risk for a fleet "
                        "this size."
                    ),
                    "source": "ec2.GetSpotPlacementScores",
                },
                "check_existing_odcrs": {
                    "instance_type": "g5.2xlarge",
                    "region": "sa-east-1",
                    "availability_zone": None,
                    "reservation_count": 0,
                    "total_capacity": 0,
                    "available_capacity": 0,
                    "reservations": [],
                    "capacity_signal": "none",
                    "interpretation": "No active ODCRs.",
                    "truncated": False,
                    "source": "ec2.DescribeCapacityReservations",
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _print_confidence_block(assessments: list[dict[str, Any]]) -> None:
    """Format the deterministic confidence_assessments[] payload for viewing.

    The deterministic scorer runs live over the fixture tool outputs, so the
    numbers here are computed the same way they would be against real AWS
    data.
    """
    if not assessments:
        return
    _rule("─")
    print(BOLD(MAGENTA("  Deterministic Confidence Assessments (from src/confidence.py)")))
    _rule("─")
    for a in assessments:
        conf = a["confidence"]
        verdict_color = {
            "DEPLOYABLE": GREEN,
            "DEPLOYABLE WITH PREPARATION": YELLOW,
            "HIGH RISK": YELLOW,
            "NOT FEASIBLE": RED,
        }.get(conf["verdict"], YELLOW)
        score_str = BOLD(f"{conf['score']:.2f}")
        line = (
            f"  {BOLD(a['instance_type'])} in {BOLD(a['region'])}  "
            f"(requested {a['requested_count']}): "
            f"score={score_str}  "
            f"label={BOLD(conf['label'])}  "
            f"verdict={verdict_color(BOLD(conf['verdict']))}"
        )
        print(line)
        # Emit base_score when it diverges from final (a cap fired).
        if abs(conf["base_score"] - conf["score"]) > 0.01:
            print(
                DIM(
                    f"    base_score before caps: {conf['base_score']:.3f}  "
                    f"final score after {len(conf['applied_caps'])} cap(s): {conf['score']:.2f}"
                )
            )
        if conf.get("applied_caps"):
            print(DIM("    Applied caps (what changed the score):"))
            for cap in conf["applied_caps"]:
                print(
                    DIM(
                        f"      • {cap['kind']}: {cap['target']} "
                        f"{cap['from_value']} → {cap['to_value']}"
                    )
                )
    _rule("─")


def _print_tool_call(name: str, args: dict, output: dict, index: int) -> None:
    ok = output.get("ok")
    marker = GREEN("✔") if ok else RED("✘")
    # Compact args rendering; json.dumps with commas + colons keeps it terse.
    args_str = json.dumps(args, separators=(",", ":"))
    idx_str = f"{index:>2}."
    print(f"  {DIM(idx_str)} {marker} {BOLD(name)}  {DIM(args_str)}")


def _play_scenario(scenario: dict[str, Any]) -> None:
    _banner(scenario["title"], scenario["subtitle"])

    print()
    _typewriter(BOLD("👤 User: "), scenario["user_query"])
    print()
    _pause(1.2)

    # Wire the Bedrock mock and fixture handlers.
    bedrock = MagicMock()
    bedrock.converse.side_effect = list(scenario["bedrock_script"])
    originals = _patch_tool_handlers(scenario["tool_fixtures"])

    try:
        from unittest.mock import patch

        with patch("src.agent.boto3.client", return_value=bedrock):
            agent = CapacityDecisionAgent(
                model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                region="us-west-2",
                verbose=False,
                max_iterations=15,
            )
            for tool_spec in ALL_TOOLS:
                agent.register_tool(**tool_spec)

            print(BOLD("🤖 Agent (model-agnostic Bedrock, Claude Haiku 4.5 default):"))
            print(DIM(f"    Tools registered: {len(agent.tools)}"))
            print()
            _pause(0.6)

            result = agent.run(scenario["user_query"])
    finally:
        _restore_tool_handlers(originals)

    # Show the tool trace.
    _rule()
    print(BOLD("  Tool trace (order of API calls the agent made):"))
    _rule()
    for idx, (name, args, output) in enumerate(result["tool_calls"], start=1):
        _print_tool_call(name, args, output, idx)
        _pause(0.15)
    _pause(0.5)

    # Final answer from the LLM.
    _rule()
    print(BOLD("  Agent answer (LLM narrative):"))
    _rule()
    print()
    print(result["final_text"])
    print()

    # Deterministic scorer output.
    _print_confidence_block(result["confidence_assessments"])

    # Stats footer.
    usage = result["usage"]
    total = usage["input_tokens"] + usage["output_tokens"]
    print()
    print(
        DIM(
            f"  Iterations: {result['iterations']}   "
            f"Tool calls: {len(result['tool_calls'])}   "
            f"Stop: {result['stop_reason']}   "
            f"Tokens (in/out): {usage['input_tokens']:,} / {usage['output_tokens']:,} "
            f"(total {total:,})"
        )
    )
    print()
    _pause(1.5)


def _print_intro() -> None:
    _rule("═")
    print(BOLD(CYAN("  GPU Capacity Planning Agent — Recorded Demo")))
    print(DIM("  Model-agnostic Amazon Bedrock agent · 12 read-only AWS API tools"))
    print(DIM("  Deterministic confidence scoring · Data-residency & GovCloud aware"))
    _rule("═")
    print()
    _typewriter(
        BOLD("  What you'll see: "),
        "Four scenarios that walk the agent from an easy DEPLOYABLE case, "
        "through NOT-FEASIBLE-by-availability, then the ICE-vs-SPS "
        "ground-truth override, and finally an escalation-level fleet request.",
    )
    print()
    print(
        DIM(
            "  This demo runs offline against mocked Bedrock + tool fixtures. "
            "The deterministic scorer (src/confidence.py) runs live so the "
            "confidence_assessments block reflects the real scoring hierarchy."
        )
    )
    print()
    _pause(2.0)


def _print_outro() -> None:
    _rule("═")
    print(BOLD(CYAN("  Demo complete.")))
    print()
    print(
        "  Try it against real AWS APIs: "
        + BOLD("./examples/query.sh \"...your question...\"")
    )
    print(
        "  Explore the code: "
        + BOLD("src/agent.py, src/confidence.py, src/tools/, template.yaml")
    )
    print(
        "  Read the guidance: "
        + BOLD("README.md and docs/implementation-guide.md")
    )
    print()
    _rule("═")


def main() -> int:
    scenarios = _scenarios()
    if len(sys.argv) > 1:
        picked = sys.argv[1]
        matching = [s for s in scenarios if s["id"] == picked]
        if not matching:
            available = ", ".join(s["id"] for s in scenarios)
            print(f"Unknown scenario '{picked}'. Choose from: {available}")
            return 1
        scenarios = matching

    _print_intro()
    for s in scenarios:
        _play_scenario(s)

    _print_outro()
    return 0


if __name__ == "__main__":
    sys.exit(main())
