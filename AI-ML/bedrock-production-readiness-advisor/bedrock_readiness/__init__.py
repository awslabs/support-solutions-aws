# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock Readiness (readiness-only) solution.

Assesses eight pillars: Observability, Architecture & Resilience, Quota &
Capacity, Cost Optimization, Model Fitness, Security, Guardrails, and Data
Governance. Never generates a deployable remediation artifact for any of them
-- no IAM policy, guardrail configuration, CloudFormation, or Terraform.
Every finding is plain-text guidance pointing at public AWS documentation.
See ../README.md.

Every operation is read-only. Nothing in this package creates, modifies, or
deletes an AWS resource.

Three assessment sources:

    assess()          -- read-only scan of a live account (runtime evidence)
    review_diagram()  -- review an architecture diagram (intended design)
    assess_platform() -- both, plus a design-vs-reality reconciliation

Four delivery channels:

    bedrock-readiness assess ...          # CLI
    from bedrock_readiness import assess  # Python API
    delivery/agentcore_agent.py           # Strands Agent on AgentCore Runtime
    delivery/mcp_server.py                # MCP server (Kiro/Claude Code/Cursor/Q)

Usage:
    # CLI
    bedrock-readiness assess --region us-east-1
    bedrock-readiness assess --diagram <your-diagram>.png
    bedrock-readiness review-diagram --diagram s3://<bucket>/<key>.png
    bedrock-readiness checks
    bedrock-readiness mcp

    # Python API
    from bedrock_readiness import assess, is_production_ready
    result = assess(region="us-east-1")
    print(f"Score: {result.overall_score}/100")
"""

__version__ = "readiness-only-1.2.0"

from .api import (
    assess,
    assess_pillar,
    assess_platform,
    is_production_ready,
    review_diagram,
)

__all__ = [
    "assess",
    "assess_pillar",
    "assess_platform",
    "is_production_ready",
    "review_diagram",
]
