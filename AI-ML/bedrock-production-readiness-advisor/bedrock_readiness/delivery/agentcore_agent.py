# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AgentCore / Strands delivery channel -- readiness-only.

Ported from Sruthi's `sample-bedrock-readiness-agent` agent.py pattern (a
Strands Agent on AgentCore Runtime, calling @tool-decorated functions), with
each tool wrapping a call into this package's pillar engine -- now all eight
pillars, including Security, Guardrails, and Data Governance, generated from
the same PILLAR_MODULES registry as every other tool here.

There is no `generate_remediation` tool registered here -- that capability
does not exist in this codebase at all, not just unregistered, for ANY
pillar. Every finding's `recommendation` is plain-text guidance; a Security
finding that names a wildcard IAM grant, for example, describes what the
policy document grants in prose and never reproduces or corrects the document.
"""

from typing import Optional

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from bedrock_readiness.core.models import Config, CheckStatus
from bedrock_readiness.core.scanner import AccountScanner
from bedrock_readiness.modules import PILLAR_MODULES


def _pillar_result_to_dict(pillar_result) -> dict:
    findings = []
    for f in pillar_result.findings:
        if f.status not in (CheckStatus.FAIL, CheckStatus.WARN):
            continue
        findings.append({
            "severity": f.severity_label,
            "check_id": f.check_id,
            "finding": f"{f.check_name}: {f.message}",
            "recommendation": f.recommendation,
            "resources": f.resource_ids,
        })
    return {"pillar": pillar_result.pillar_name, "score": pillar_result.score, "findings": findings}


def build_agent(region: str = "us-east-1", profile: Optional[str] = None) -> Agent:
    """Build the readiness-only Strands Agent. One tool per pillar in
    PILLAR_MODULES -- currently Observability, Architecture, Quota & Capacity,
    Cost Optimization, Model Fitness, Security, Guardrails, and Data
    Governance. Adding a pillar module automatically adds its tool here."""
    config = Config(regions=[region], profile=profile)
    scanner_holder = {"scanner": None, "scan_data": None}

    def _get_scanner_and_scan():
        if scanner_holder["scanner"] is None:
            scanner_holder["scanner"] = AccountScanner(config)
            scanner_holder["scan_data"] = scanner_holder["scanner"].scan_all()
        return scanner_holder["scanner"], scanner_holder["scan_data"]

    tools = []
    for pillar_id, module in PILLAR_MODULES.items():
        def _make_tool(pillar_id=pillar_id, module=module):
            @tool(name=f"check_{pillar_id}")
            def _tool() -> dict:
                f"""Run the {pillar_id.replace('_', ' ')} pillar's checks and return findings."""
                scanner, scan_data = _get_scanner_and_scan()
                pr = module.assess(scanner, scan_data, config)
                return _pillar_result_to_dict(pr)
            return _tool
        tools.append(_make_tool())

    pillar_lines = "\n".join(f"{i+1}. {pid.replace('_', ' ').title()}" for i, pid in enumerate(PILLAR_MODULES))

    system_prompt = f"""You are the Bedrock Readiness Agent (readiness-only). You assess whether an
AWS account's Bedrock deployment is operationally ready for production, across
eight pillars including Security, Guardrails, and Data Governance.

This agent has no remediation-template generation capability, for ANY pillar.
It cannot produce a CloudFormation/Terraform template, a corrected IAM policy,
or a guardrail configuration meant to be deployed as-is. If a Security finding
names a wildcard IAM grant or similar, describe what the policy document
grants in your own words -- do not invent, complete, or "fix" a policy
document yourself; that is not something this agent's tools return and not
something you should synthesize on top of them.

You evaluate these pillars:
{pillar_lines}

For each pillar, produce severity-rated findings (CRITICAL/HIGH/MEDIUM/LOW)
with plain-text remediation guidance pointing at what to change, never a
generated CloudFormation/Terraform template, IAM policy, or guardrail
configuration.

IMPORTANT RULES:
- Run every available pillar tool exactly ONCE each. Do NOT retry failed checks.
- If a check returns errors in its findings, report those as-is. Do NOT re-run the tool.
- Deliver the report ONCE. Do not repeat or summarize it afterward.
"""

    model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        region_name=region,
    )

    return Agent(model=model, system_prompt=system_prompt, tools=tools)


if __name__ == "__main__":
    import os

    region = os.environ.get("AWS_REGION", "us-east-1")
    agent = build_agent(region=region)
    agent(
        f"Run a Bedrock readiness assessment for this account in {region}. "
        "Run each available check exactly once, then deliver the report once. Do not repeat."
    )
