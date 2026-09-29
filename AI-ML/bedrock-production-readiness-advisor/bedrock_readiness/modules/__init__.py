# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar module registry -- readiness-only.

Registers the eight readiness pillars with the assessment pipeline. All eight
run on both the readiness and platform tracks; there is no pillar-level
security/non-security split any more, because Security, Guardrails, and Data
Governance are now assessed here using read-only API calls on the same terms
as every other pillar.
"""

from importlib import import_module

PILLAR_ORDER = [
    "observability",
    "architecture",
    "quota_capacity",
    "cost_optimization",
    "model_fitness",
    "security",
    "guardrails",
    "data_governance",
]

PILLAR_MODULES = {
    "observability": import_module("bedrock_readiness.modules.observability"),
    "architecture": import_module("bedrock_readiness.modules.architecture"),
    "quota_capacity": import_module("bedrock_readiness.modules.quota_capacity"),
    "cost_optimization": import_module("bedrock_readiness.modules.cost_optimization"),
    "model_fitness": import_module("bedrock_readiness.modules.model_fitness"),
    "security": import_module("bedrock_readiness.modules.security"),
    "guardrails": import_module("bedrock_readiness.modules.guardrails"),
    "data_governance": import_module("bedrock_readiness.modules.data_governance"),
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
