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
# server's list_readiness_checks tool and the CLI.
#
# tests/test_check_catalog.py asserts this catalog matches the check IDs the
# pillar modules actually emit, so it cannot silently go stale.
CHECK_CATALOG = {
    "observability": [
        ("OBS-01", "Model Invocation Logging"),
        ("OBS-02", "GenAI Dashboard"),
        ("OBS-03", "Alarm: Error"),
        ("OBS-04", "Alarm: Latency"),
        ("OBS-05", "Alarm: Throttle"),
        ("OBS-06", "Alarm: Cost"),
        ("OBS-07", "Transaction Search (X-Ray)"),
        ("OBS-08", "Bedrock Log Groups"),
        ("OBS-09", "AgentCore Observability Logs"),
        ("OBS-10", "Agent Session Tracing"),
        ("OBS-11", "KB Sync Monitoring"),
        ("OBS-12", "Model Evaluation Runs"),
        ("OBS-13", "AgentCore Evaluations"),
    ],
    "architecture": [
        ("ARCH-01", "Cross-Region Inference"),
        ("ARCH-02", "Model Fallback Strategy"),
        ("ARCH-03", "Retry with Exponential Backoff"),
        ("ARCH-04", "Async/Queue Pattern"),
        ("ARCH-05", "Model Diversity"),
        ("ARCH-06", "AgentCore Runtime"),
        ("ARCH-07", "AgentCore Gateway"),
        ("ARCH-08", "AgentCore Memory"),
        ("ARCH-09", "KB Sync Health"),
        ("ARCH-10", "Flow Error Handling"),
    ],
    "quota_capacity": [
        ("QC-01", "Quota Sufficient for Production"),
        ("QC-02", "Throttle Events (14 days)"),
        ("QC-03", "Growth Trajectory"),
        ("QC-04", "Quota Alarm"),
        ("QC-05", "Provisioned Throughput"),
        ("QC-06", "Cross-Region Capacity"),
        ("QC-07", "Model Access Enabled"),
        ("QC-08", "Quota Increase Requested"),
        ("QC-09", "Batch Inference for Bulk"),
        ("QC-10", "Agent Concurrent Sessions"),
        ("QC-11", "Cross-Region Inference (CRIS) Usage"),
    ],
    "cost_optimization": [
        ("COST-01", "Token Usage Tracking"),
        ("COST-02", "Model Right-Sizing"),
        ("COST-03", "Prompt Routing"),
        ("COST-04", "Batch Inference for Bulk"),
        ("COST-05", "Provisioned Throughput Utilization"),
        ("COST-06", "Cost Alarm"),
        ("COST-07", "KB Embedding Cost"),
        ("COST-08", "Batch-Eligible Workloads On-Demand"),
        ("COST-09", "Bedrock Spend Growth Trend"),
    ],
    "model_fitness": [
        ("MF-01", "Model Diversity (Single-Model Dependency)"),
        ("MF-02", "Premium Model Usage Share"),
        ("MF-03", "Legacy Model Usage"),
        ("MF-04", "Cross-Region Inference Adoption"),
    ],
    "security": [
        ("SEC-01", "Broad Managed Policies on Bedrock Roles"),
        ("SEC-02", "Wildcard Action/Resource on Bedrock Roles"),
        ("SEC-03", "Bedrock Role Trust Policy Scoping"),
        ("SEC-04", "CloudTrail Coverage for Bedrock"),
    ],
    "guardrails": [
        ("GR-01", "Guardrail Configured"),
        ("GR-02", "Account-Level Guardrail Enforcement"),
        ("GR-03", "Sensitive-Information Filtering"),
        ("GR-04", "Guardrail Versioning"),
    ],
    "data_governance": [
        ("DG-01", "Account Data Retention Mode"),
        ("DG-02", "Invocation Log Bucket Encryption"),
        ("DG-03", "Invocation Log Bucket Public Access"),
        ("DG-04", "Invocation Log Group Encryption"),
    ],
}


def total_check_count() -> int:
    return sum(len(v) for v in CHECK_CATALOG.values())
