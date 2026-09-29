# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assessment modules -- one per pillar.

Eight pillars, all assessed the same way: read-only API calls, severity-rated
findings, plain-text recommendations pointing at public AWS documentation.
Security, Guardrails, and Data Governance are assessed under the same
constraint as every other pillar here -- no deployable policy, template, or
configuration is ever generated; see core/scanner.py for exactly which
read-only calls back each pillar and why each is scoped the way it is.
"""

from . import (
    observability,
    architecture,
    quota_capacity,
    cost_optimization,
    model_fitness,
    security,
    guardrails,
    data_governance,
)

PILLAR_MODULES = {
    "observability": observability,
    "architecture": architecture,
    "quota_capacity": quota_capacity,
    "cost_optimization": cost_optimization,
    "model_fitness": model_fitness,
    "security": security,
    "guardrails": guardrails,
    "data_governance": data_governance,
}

PILLAR_NAMES = {
    "observability": "Observability",
    "architecture": "Architecture & Resilience",
    "quota_capacity": "Quota & Capacity",
    "cost_optimization": "Cost Optimization",
    "model_fitness": "Model Fitness",
    "security": "Security",
    "guardrails": "Guardrails",
    "data_governance": "Data Governance",
}

# --- Declarative check catalog -------------------------------------------------
#
# Single source of truth for "what does this tool check?", consumed by the MCP
# server's list_readiness_checks tool and the CLI. Keeping it here rather than
# duplicated per delivery channel is deliberate: the pre-split platform's MCP
# server hardcoded its own table, which drifted out of date (it was missing
# QC-11, COST-08/09 and the whole Model Fitness pillar).
#
# `origin` records which source tool the check came from:
#   "platform" -- Vipul/Ankur `bedrock-readiness-platform`
#   "agent"    -- Sruthi `sample-bedrock-readiness-agent`
#
# tests/test_check_catalog.py asserts this catalog matches the check IDs the
# pillar modules actually emit, so it cannot silently go stale.
CHECK_CATALOG = {
    "observability": [
        ("OBS-01", "Model Invocation Logging", "platform"),
        ("OBS-02", "GenAI Dashboard", "platform"),
        ("OBS-03", "Alarm: Error", "platform"),
        ("OBS-04", "Alarm: Latency", "platform"),
        ("OBS-05", "Alarm: Throttle", "platform"),
        ("OBS-06", "Alarm: Cost", "platform"),
        ("OBS-07", "Transaction Search (X-Ray)", "platform"),
        ("OBS-08", "Bedrock Log Groups", "platform"),
        ("OBS-09", "AgentCore Observability Logs", "platform"),
        ("OBS-10", "Agent Session Tracing", "platform"),
        ("OBS-11", "KB Sync Monitoring", "platform"),
        ("OBS-12", "Model Evaluation Runs", "platform"),
        ("OBS-13", "AgentCore Evaluations", "platform"),
    ],
    "architecture": [
        ("ARCH-01", "Cross-Region Inference", "platform"),
        ("ARCH-02", "Model Fallback Strategy", "platform"),
        ("ARCH-03", "Retry with Exponential Backoff", "platform"),
        ("ARCH-04", "Async/Queue Pattern", "platform"),
        ("ARCH-05", "Model Diversity", "platform"),
        ("ARCH-06", "AgentCore Runtime", "platform"),
        ("ARCH-07", "AgentCore Gateway", "platform"),
        ("ARCH-08", "AgentCore Memory", "platform"),
        ("ARCH-09", "KB Sync Health", "platform"),
        ("ARCH-10", "Flow Error Handling", "platform"),
    ],
    "quota_capacity": [
        ("QC-01", "Quota Sufficient for Production", "platform"),
        ("QC-02", "Throttle Events (14 days)", "platform"),
        ("QC-03", "Growth Trajectory", "platform"),
        ("QC-04", "Quota Alarm", "platform"),
        ("QC-05", "Provisioned Throughput", "platform"),
        ("QC-06", "Cross-Region Capacity", "platform"),
        ("QC-07", "Model Access Enabled", "platform"),
        ("QC-08", "Quota Increase Requested", "platform"),
        ("QC-09", "Batch Inference for Bulk", "platform"),
        ("QC-10", "Agent Concurrent Sessions", "platform"),
        ("QC-11", "Cross-Region Inference (CRIS) Usage", "agent"),
    ],
    "cost_optimization": [
        ("COST-01", "Token Usage Tracking", "platform"),
        ("COST-02", "Model Right-Sizing", "platform"),
        ("COST-03", "Prompt Routing", "platform"),
        ("COST-04", "Batch Inference for Bulk", "platform"),
        ("COST-05", "Provisioned Throughput Utilization", "platform"),
        ("COST-06", "Cost Alarm", "platform"),
        ("COST-07", "KB Embedding Cost", "platform"),
        ("COST-08", "Batch-Eligible Workloads On-Demand", "agent"),
        ("COST-09", "Bedrock Spend Growth Trend", "agent"),
    ],
    "model_fitness": [
        ("MF-01", "Model Diversity (Single-Model Dependency)", "agent"),
        ("MF-02", "Premium Model Usage Share", "agent"),
        ("MF-03", "Legacy Model Usage", "agent"),
        ("MF-04", "Cross-Region Inference Adoption", "agent"),
    ],
    "security": [
        ("SEC-01", "Broad Managed Policies on Bedrock Roles", "platform"),
        ("SEC-02", "Wildcard Action/Resource on Bedrock Roles", "platform"),
        ("SEC-03", "Bedrock Role Trust Policy Scoping", "platform"),
        ("SEC-04", "CloudTrail Coverage for Bedrock", "platform"),
    ],
    "guardrails": [
        ("GR-01", "Guardrail Configured", "platform"),
        ("GR-02", "Account-Level Guardrail Enforcement", "platform"),
        ("GR-03", "Sensitive-Information Filtering", "platform"),
        ("GR-04", "Guardrail Versioning", "platform"),
    ],
    "data_governance": [
        ("DG-01", "Account Data Retention Mode", "platform"),
        ("DG-02", "Invocation Log Bucket Encryption", "platform"),
        ("DG-03", "Invocation Log Bucket Public Access", "platform"),
        ("DG-04", "Invocation Log Group Encryption", "platform"),
    ],
}


def total_check_count() -> int:
    return sum(len(v) for v in CHECK_CATALOG.values())
