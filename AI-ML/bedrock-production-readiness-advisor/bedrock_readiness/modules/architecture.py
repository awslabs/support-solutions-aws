# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Architecture & Resilience -- Is your deployment resilient and scalable?

Ported unchanged from the Vipul/Ankur `bedrock-readiness-platform`
(enhanced-version). Not security-sensitive -- runs on both tracks.

Checks: ARCH-01 through ARCH-10
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import ScanError


PILLAR_ID = "architecture"
PILLAR_NAME = "Architecture & Resilience"


def assess(scanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)
    wt = config.workload_type

    if wt in ("customer-facing-chatbot", "multi-agent", "inference-api", "rag-pipeline"):
        result.findings.append(_check_cross_region(scan_data))
    if wt != "batch-processing":
        result.findings.append(_check_model_fallback(scan_data))
    if wt not in ("batch-processing", "fine-tuning"):
        result.findings.append(_check_retry_pattern(scan_data))
    if wt in ("customer-facing-chatbot", "batch-processing", "inference-api"):
        result.findings.append(_check_async_pattern(scan_data))

    result.findings.append(_check_model_diversity(scan_data))

    if wt == "multi-agent":
        result.findings.append(_check_agentcore_runtime(scan_data))
        runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
        if not isinstance(runtimes, ScanError) and runtimes:
            result.findings.append(_check_agentcore_gateway(scan_data))

    if wt in ("multi-agent", "customer-facing-chatbot"):
        runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
        if not isinstance(runtimes, ScanError) and runtimes:
            result.findings.append(_check_agentcore_memory(scan_data))

    if wt == "rag-pipeline":
        kbs = scan_data.get("bedrock", {}).get("knowledge_bases", [])
        if not isinstance(kbs, ScanError) and kbs:
            result.findings.append(_check_kb_health(scan_data))

    flows = scan_data.get("bedrock", {}).get("flows", [])
    if not isinstance(flows, ScanError) and flows:
        result.findings.append(_check_flow_errors(flows))

    return result


def _check_cross_region(scan_data: dict) -> Finding:
    profiles = scan_data.get("bedrock", {}).get("inference_profiles", [])
    if isinstance(profiles, ScanError):
        return Finding(
            check_id="ARCH-01", check_name="Cross-Region Inference",
            status=CheckStatus.ERROR, impact=4, likelihood=2,
            message=f"Could not check: {profiles.error}",
        )
    multi_region = [p for p in profiles if len(p.get("models", [])) > 1]
    if multi_region:
        return Finding(
            check_id="ARCH-01", check_name="Cross-Region Inference",
            status=CheckStatus.PASS, impact=4, likelihood=2,
            message=f"{len(multi_region)} cross-region inference profile(s) configured",
        )
    return Finding(
        check_id="ARCH-01", check_name="Cross-Region Inference",
        status=CheckStatus.FAIL, impact=4, likelihood=2,
        message="No cross-region inference -- single region failure means full service outage",
        recommendation="Configure cross-region inference profiles for high-availability workloads",
        fix_type=FixType.ARCHITECTURE, effort_minutes=120,
    )


def _check_model_fallback(scan_data: dict) -> Finding:
    alarms = scan_data.get("cloudwatch", {}).get("alarms", [])
    if isinstance(alarms, ScanError):
        return Finding(
            check_id="ARCH-02", check_name="Model Fallback Strategy",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message="Could not check model usage diversity",
        )
    model_ids = set()
    for a in alarms:
        for dim in a.get("Dimensions", []):
            if dim.get("Name") == "ModelId":
                model_ids.add(dim["Value"])

    if len(model_ids) >= 2:
        return Finding(
            check_id="ARCH-02", check_name="Model Fallback Strategy",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"Multiple models detected ({len(model_ids)}): {', '.join(list(model_ids)[:3])}",
        )
    return Finding(
        check_id="ARCH-02", check_name="Model Fallback Strategy",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="Only 1 or 0 model IDs detected -- no fallback if primary model is degraded",
        recommendation="Configure a fallback model (e.g., Haiku as fallback for Sonnet) for resilience",
        fix_type=FixType.CODE, effort_minutes=120,
    )


def _check_retry_pattern(scan_data: dict) -> Finding:
    return Finding(
        check_id="ARCH-03", check_name="Retry with Exponential Backoff",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="Verify your application implements retry with exponential backoff for Bedrock calls",
        recommendation="Implement retry with exponential backoff (SDK default or custom) to handle transient errors gracefully",
        fix_type=FixType.CODE, effort_minutes=60,
    )


def _check_async_pattern(scan_data: dict) -> Finding:
    return Finding(
        check_id="ARCH-04", check_name="Async/Queue Pattern",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="Verify burst traffic is buffered (SQS/EventBridge) rather than hitting Bedrock synchronously",
        recommendation="Add an async queue between user requests and Bedrock invocations to smooth traffic bursts",
        fix_type=FixType.ARCHITECTURE, effort_minutes=480,
    )


def _check_model_diversity(scan_data: dict) -> Finding:
    alarms = scan_data.get("cloudwatch", {}).get("alarms", [])
    profiles = scan_data.get("bedrock", {}).get("inference_profiles", [])
    model_ids = set()

    if not isinstance(alarms, ScanError):
        for a in alarms:
            for dim in a.get("Dimensions", []):
                if dim.get("Name") == "ModelId":
                    model_ids.add(dim["Value"])

    if not isinstance(profiles, ScanError):
        for p in profiles:
            for m in p.get("models", []):
                model_ids.add(m.get("modelArn", ""))

    if len(model_ids) >= 2:
        return Finding(
            check_id="ARCH-05", check_name="Model Diversity",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"{len(model_ids)} distinct models in use",
        )
    return Finding(
        check_id="ARCH-05", check_name="Model Diversity",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="Single model dependency detected -- consider using cost-appropriate models for different tasks",
        recommendation="Use smaller/cheaper models (Haiku) for simple tasks, larger models for complex reasoning",
        fix_type=FixType.CODE, effort_minutes=120,
    )


def _check_agentcore_runtime(scan_data: dict) -> Finding:
    runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
    if isinstance(runtimes, ScanError):
        return Finding(
            check_id="ARCH-06", check_name="AgentCore Runtime",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not check: {runtimes.error}",
        )
    if runtimes:
        active = [r for r in runtimes if r.get("status") == "ACTIVE"]
        return Finding(
            check_id="ARCH-06", check_name="AgentCore Runtime",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"{len(runtimes)} runtime(s) ({len(active)} active) -- managed infrastructure",
        )
    return Finding(
        check_id="ARCH-06", check_name="AgentCore Runtime",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="No AgentCore runtimes -- agents may be self-managed on ECS/EKS/Lambda",
        recommendation="Consider AgentCore Runtime for managed deployment with built-in session isolation and scaling",
        fix_type=FixType.ARCHITECTURE, effort_minutes=480,
    )


def _check_agentcore_gateway(scan_data: dict) -> Finding:
    gateways = scan_data.get("agentcore", {}).get("gateways", [])
    if isinstance(gateways, ScanError):
        return Finding(
            check_id="ARCH-07", check_name="AgentCore Gateway",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message=f"Could not check: {gateways.error}",
        )
    if gateways:
        return Finding(
            check_id="ARCH-07", check_name="AgentCore Gateway",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"{len(gateways)} gateway(s) -- centralized API management for agents",
        )
    return Finding(
        check_id="ARCH-07", check_name="AgentCore Gateway",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="No AgentCore Gateway -- each agent manages its own auth and rate limiting",
        recommendation="Deploy AgentCore Gateway for centralized auth, rate limiting, and tool governance",
        fix_type=FixType.DEPLOY, effort_minutes=120,
    )


def _check_agentcore_memory(scan_data: dict) -> Finding:
    memories = scan_data.get("agentcore", {}).get("memories", [])
    if isinstance(memories, ScanError):
        return Finding(
            check_id="ARCH-08", check_name="AgentCore Memory",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not check: {memories.error}",
        )
    if memories:
        return Finding(
            check_id="ARCH-08", check_name="AgentCore Memory",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"{len(memories)} memory store(s) -- agents retain context across sessions",
        )
    return Finding(
        check_id="ARCH-08", check_name="AgentCore Memory",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="No AgentCore Memory configured -- agents lose context between sessions",
        recommendation="Configure Memory for stateful agents that need cross-session context",
        fix_type=FixType.DEPLOY, effort_minutes=60,
    )


def _check_kb_health(scan_data: dict) -> Finding:
    kbs = scan_data.get("bedrock", {}).get("knowledge_bases", [])
    if isinstance(kbs, ScanError):
        return Finding(
            check_id="ARCH-09", check_name="KB Sync Health",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message="Could not check Knowledge Bases",
        )
    return Finding(
        check_id="ARCH-09", check_name="KB Sync Health",
        status=CheckStatus.PASS, impact=3, likelihood=2,
        message=f"{len(kbs)} Knowledge Base(s) found -- verify sync jobs are completing successfully",
    )


def _check_flow_errors(flows: list) -> Finding:
    return Finding(
        check_id="ARCH-10", check_name="Flow Error Handling",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message=f"{len(flows)} Bedrock Flow(s) found -- verify error/catch nodes are configured for failure paths",
        recommendation="Add error handling nodes to Flows for graceful failure management",
        fix_type=FixType.CONFIG, effort_minutes=60,
    )
