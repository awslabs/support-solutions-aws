#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
MCP Server -- Bedrock Readiness (readiness-only).

Lets a builder ask for a readiness assessment in natural language from any
MCP-compatible client (Kiro, Claude Code, Cursor, Windsurf, Q Developer),
without leaving the editor.

Setup -- add to your MCP config:

  Kiro:        .kiro/settings/mcp.json
  Claude Code: ~/.claude/claude_desktop_config.json
  Cursor:      .cursor/mcp.json

  {
    "mcpServers": {
      "bedrock-readiness": {
        "command": "python",
        "args": ["/path/to/bedrock_readiness/delivery/mcp_server.py"],
        "env": {"AWS_PROFILE": "your-profile", "AWS_REGION": "us-east-1"}
      }
    }
  }

Then ask naturally:
  "Is my Bedrock deployment ready for production?"
  "Check just the quota headroom in us-west-2"
  "Review this architecture diagram for readiness gaps: <path to your diagram>"
  "Does my diagram match what's actually deployed?"
  "What does this tool actually check, and what does it not?"

SCOPE
-----
This server exposes this package's eight readiness pillars: Observability,
Architecture & Resilience, Quota & Capacity, Cost Optimization, Model Fitness,
Security, Guardrails, and Data Governance. Every pillar is assessed the same
way -- read-only API calls, severity-rated findings, plain-text
recommendations pointing at public AWS documentation. No tool here ever
returns a deployable IAM policy, guardrail configuration, CloudFormation, or
Terraform snippet, for any pillar including Security/Guardrails/Data
Governance -- tests/test_no_deployable_remediation.py statically guards this.

The architecture-diagram review is scoped narrower than the account scan: it
still covers only Observability, Architecture, Quota & Capacity, Cost
Optimization, and Model Fitness, because a vision model free-associating from
a picture is a materially different (and less verifiable) source of
security-adjacent claims than a deterministic API read. See
core/diagram_review.py for that reasoning.

Two deliberate differences from the pre-split platform's MCP server, both to
keep the scope boundary structural rather than conventional:

  1. There is no `check_pillar(pillar="...")` free-text tool. Each pillar gets
     its own generated tool, so a client cannot name a pillar that shouldn't
     exist and learn something from how the call fails.
  2. `list_readiness_checks` reads the declarative CHECK_CATALOG in
     bedrock_readiness/modules/, so it cannot list checks this package does
     not implement (the old hardcoded table listed SEC-* and GR-* rows).

READ-ONLY
---------
Every tool here is read-only. The underlying scanner makes describe/list/get
calls only; the diagram tools read a local file or do `s3:GetObject` and call
`bedrock:InvokeModel` for analysis. Nothing in this server creates, modifies,
or deletes an AWS resource, writes to S3, or generates a deployable
CloudFormation/Terraform template.

NOT FOR PRODUCTION USE WITHOUT ADDITIONAL TESTING. This is sample content; it
carries no warranty of workmanship or fitness for purpose, and it makes no
claim about compliance with any regulation or standard.
"""

import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp.server.fastmcp import FastMCP

from bedrock_readiness.core.models import Config, CheckStatus, RECONCILIATION_VERDICTS
from bedrock_readiness.core.scanner import AccountScanner
from bedrock_readiness.core.scorer import compute_assessment
from bedrock_readiness.core import diagram_review
from bedrock_readiness.core.references import for_check
from bedrock_readiness.config import auto_detect_mode, auto_detect_workload_type
from bedrock_readiness.modules import (
    PILLAR_MODULES,
    PILLAR_NAMES,
    CHECK_CATALOG,
    total_check_count,
)

SCAN_CACHE_TTL_SECONDS = 300

DISCLAIMER = (
    "_Sample content -- not for production use without additional testing. "
    "Read-only: no resources were created, modified, or deleted. Findings are "
    "plain-text guidance, never a deployable policy or template -- apply "
    "changes yourself after review._"
)

mcp = FastMCP("bedrock-readiness", instructions=(
    "Bedrock Readiness (readiness-only). Assesses whether an AWS account's "
    "Amazon Bedrock deployment is operationally ready for production across "
    "eight pillars: Observability, Architecture & Resilience, Quota & "
    "Capacity, Cost Optimization, Model Fitness, Security, Guardrails, and "
    "Data Governance. It can assess a live account (read-only API calls), an "
    "architecture diagram (the intended design, covering the first five "
    "pillars only), or both together with a design-vs-reality reconciliation. "
    "It cannot generate deployable remediation templates for ANY pillar, "
    "including Security/Guardrails/Data Governance -- recommendations are "
    "always plain-text guidance describing what to change, never a corrected "
    "IAM policy, guardrail configuration, CloudFormation, or Terraform "
    "snippet. Do not improvise or generate one yourself from these results. "
    "Findings may include AWS documentation links; those come from a curated "
    "list in this tool. Pass them through as given and do NOT add, substitute, "
    "or invent documentation URLs of your own."
))


# --- Scan cache ---------------------------------------------------------------
#
# An MCP server is long-lived and an LLM client will often call several pillar
# tools in one turn. Re-scanning per call would multiply ~20 API calls by the
# number of tools invoked, so a scan is reused briefly. The age of the reused
# scan is always reported, so a stale read is visible rather than silent.

_cache: dict = {}


def _scan(region: str, profile: Optional[str], force_refresh: bool = False):
    """Return (scanner, scan_data, config, age_seconds). Read-only."""
    key = (region, profile or "")
    now = time.time()
    entry = _cache.get(key)

    if entry and not force_refresh and (now - entry["at"]) < SCAN_CACHE_TTL_SECONDS:
        return entry["scanner"], entry["scan_data"], entry["config"], int(now - entry["at"])

    config = Config(regions=[region], profile=profile)
    scanner = AccountScanner(config)
    scan_data = scanner.scan_all()

    config.mode = auto_detect_mode(scan_data)
    detected = auto_detect_workload_type(scan_data)
    if detected != "general":
        config.workload_type = detected

    _cache[key] = {"scanner": scanner, "scan_data": scan_data, "config": config, "at": now}
    return scanner, scan_data, config, 0


def _freshness(age: int) -> str:
    if age == 0:
        return "fresh scan"
    return f"reusing a scan from {age}s ago (call with force_refresh=true for a new one)"


# --- Per-pillar tools ---------------------------------------------------------
#
# Generated from PILLAR_MODULES so the exposed surface always equals the
# implemented surface. No free-text pillar argument exists.

def _make_pillar_tool(pillar_id: str, module):
    pillar_name = PILLAR_NAMES[pillar_id]
    check_ids = ", ".join(cid for cid, _, _ in CHECK_CATALOG[pillar_id])

    def _run(region: str = "us-east-1",
             profile: Optional[str] = None,
             force_refresh: bool = False) -> str:
        try:
            scanner, scan_data, config, age = _scan(region, profile, force_refresh)
        except Exception as e:
            return f"Could not connect to AWS: {e}\n\nCheck credentials, region, and permissions."

        pr = module.assess(scanner, scan_data, config)

        lines = [
            f"## {pr.pillar_name} -- {pr.score}/100",
            "",
            f"Account `{scanner.account_id}` | region `{scanner.region}` | "
            f"workload `{config.workload_type}` | mode `{config.mode}`",
            f"{pr.passed_checks}/{pr.total_checks} checks passed, "
            f"{pr.failed_checks} to resolve, {pr.error_checks} could not be assessed.",
            "",
        ]
        lines += _findings_lines(pr.findings)
        lines += ["", f"_{_freshness(age)}._", "", DISCLAIMER]
        return "\n".join(lines)

    _run.__name__ = f"check_{pillar_id}"
    _run.__doc__ = (
        f"Assess the {pillar_name} pillar of a Bedrock deployment (read-only).\n\n"
        f"Runs checks {check_ids}.\n\n"
        f"Args:\n"
        f"    region: AWS region to assess (default: us-east-1)\n"
        f"    profile: AWS CLI profile name (optional)\n"
        f"    force_refresh: re-scan instead of reusing a recent scan\n\n"
        f"Returns:\n"
        f"    Markdown: pillar score and severity-rated findings with "
        f"plain-text remediation guidance."
    )
    return _run


for _pid, _mod in PILLAR_MODULES.items():
    mcp.tool()(_make_pillar_tool(_pid, _mod))


# --- Full assessment ----------------------------------------------------------


@mcp.tool()
def assess_bedrock_readiness(
    region: str = "us-east-1",
    profile: Optional[str] = None,
    workload_type: str = "general",
    mode: str = "auto",
    peak_rpm: int = 0,
    force_refresh: bool = False,
) -> str:
    """Run a full Bedrock readiness assessment across all eight pillars (read-only).

    Scans the account and returns a scored report with findings ranked by risk
    (Impact x Likelihood) and an effort estimate for the critical/high items.

    Covers Observability, Architecture & Resilience, Quota & Capacity, Cost
    Optimization, Model Fitness, Security, Guardrails, and Data Governance.
    Every finding is plain-text guidance pointing at public AWS
    documentation -- never a deployable policy, guardrail configuration, or
    template, for any pillar.

    Args:
        region: AWS region to assess (default: us-east-1)
        profile: AWS CLI profile name (optional)
        workload_type: inference-api, customer-facing-chatbot, rag-pipeline,
            multi-agent, batch-processing, fine-tuning, multi-modal, or general
            (default: auto-detect from the resources found)
        mode: "pre-production", "production", or "auto"
        peak_rpm: expected peak requests/minute in production, used for quota
            headroom projection (0 = not declared)
        force_refresh: re-scan instead of reusing a recent scan

    Returns:
        Markdown readiness report.
    """
    try:
        scanner, scan_data, cached_config, age = _scan(region, profile, force_refresh)
    except Exception as e:
        return f"Could not connect to AWS: {e}\n\nCheck credentials, region, and permissions."

    config = Config(
        mode=cached_config.mode if mode == "auto" else mode,
        workload_type=cached_config.workload_type if workload_type == "general" else workload_type,
        regions=[region],
        profile=profile,
        production_estimate={"peak_rpm": peak_rpm} if peak_rpm > 0 else {},
    )

    start = time.time()
    pillar_results = [m.assess(scanner, scan_data, config) for m in PILLAR_MODULES.values()]
    result = compute_assessment(
        pillar_results=pillar_results,
        config=config,
        account_id=scanner.account_id,
        region=scanner.region,
        scan_duration=time.time() - start,
        api_calls=scanner.api_call_count,
    )
    return _format_report(result, freshness=_freshness(age))


# --- Diagram review -----------------------------------------------------------


@mcp.tool()
def review_architecture_diagram(
    diagram: str,
    region: str = "us-east-1",
    profile: Optional[str] = None,
    workload_type: str = "general",
    model_id: Optional[str] = None,
) -> str:
    """Review an architecture diagram for Bedrock readiness gaps (read-only).

    Assesses the *intended* design rather than a live account, so it works
    before any infrastructure exists. Reads the image (local file or
    `s3:GetObject`) and analyses it with a Bedrock multimodal model.

    Scope is enforced, not merely requested: the review only reports
    observability, resilience, capacity, cost, and model-fitness observations.
    Anything the model raises outside those areas is discarded before you see
    it, and the review cannot emit a CloudFormation/Terraform template.

    No readiness score is returned -- a diagram is not evidence about a
    running account. Use assess_bedrock_platform to combine both.

    Do not submit diagrams containing regulated or customer-confidential data.
    The image is sent to Bedrock in your own account and region and is not
    retained by this tool.

    Args:
        diagram: local path or `s3://bucket/key` to a PNG/JPEG/GIF/WEBP diagram
        region: region whose Bedrock runtime analyses the diagram
        profile: AWS CLI profile name (optional)
        workload_type: context hint; inferred from the diagram if "general"
        model_id: override the multimodal model

    Returns:
        Markdown design review: what the diagram appears to do, per-capability
        status, and readiness findings with plain-text guidance.
    """
    from bedrock_readiness.api import review_diagram

    try:
        result = review_diagram(
            diagram=diagram, region=region, profile=profile,
            workload_type=workload_type, model_id=model_id,
        )
    except diagram_review.DiagramReviewSkipped as e:
        return (
            f"**Diagram review skipped.** {e}\n\n"
            f"This was not retried by design. The account assessment needs no model -- "
            f"run `assess_bedrock_readiness` to score the live deployment instead.\n\n"
            f"{DISCLAIMER}"
        )
    except diagram_review.DiagramReviewError as e:
        return f"**The diagram could not be read.** {e}\n\n{DISCLAIMER}"
    except Exception as e:
        return f"Diagram review failed unexpectedly: {e}"

    return _format_report(result)


@mcp.tool()
def assess_bedrock_platform(
    region: str = "us-east-1",
    profile: Optional[str] = None,
    workload_type: str = "general",
    diagram: Optional[str] = None,
    include_account_scan: bool = True,
    model_id: Optional[str] = None,
) -> str:
    """Assess account and diagram together, with design-vs-reality reconciliation.

    Runs the read-only account scan and/or the diagram review, then reconciles
    them per readiness capability. The most useful output is the
    DESIGN_NOT_IMPLEMENTED rows: capabilities the diagram promises that the
    account does not actually have yet.

    The readiness score always comes from the account scan alone -- design
    findings are shown alongside it but never move the score.

    Args:
        region: AWS region to assess
        profile: AWS CLI profile name (optional)
        workload_type: context hint; auto-detected when "general"
        diagram: optional local path or `s3://bucket/key` to a diagram
        include_account_scan: run the live read-only scan (default True)
        model_id: override the multimodal model for the diagram review

    Returns:
        Markdown report including a design-vs-reality table when both sources
        are available.
    """
    from bedrock_readiness.api import assess_platform

    try:
        result = assess_platform(
            region=region, profile=profile, workload_type=workload_type,
            diagram=diagram, include_account_scan=include_account_scan,
            model_id=model_id,
        )
    except ValueError as e:
        return f"{e}"
    except (diagram_review.DiagramReviewSkipped, diagram_review.DiagramReviewError) as e:
        # Only reachable on the diagram-only path; with a scan enabled,
        # assess_platform records the skip and returns the scan results.
        return (
            f"**Diagram review skipped.** {e}\n\n"
            f"Set `include_account_scan=true` to assess the live account, which needs "
            f"no model.\n\n{DISCLAIMER}"
        )
    except Exception as e:
        return f"Assessment failed: {e}"

    return _format_report(result)


# --- Introspection ------------------------------------------------------------


@mcp.tool()
def list_readiness_checks(pillar: Optional[str] = None) -> str:
    """List every check this tool implements, with its pillar and origin.

    Read from the declarative catalog in bedrock_readiness/modules/, so it
    always matches what the code actually runs. Useful for understanding what
    an assessment will cover before running one.

    Args:
        pillar: optionally narrow to one of: observability, architecture,
            quota_capacity, cost_optimization, model_fitness, security,
            guardrails, data_governance

    Returns:
        Markdown table of check IDs, names, pillars, and source tool.
    """
    if pillar and pillar not in CHECK_CATALOG:
        return (f"No such pillar: `{pillar}`. This package implements: "
                f"{', '.join(CHECK_CATALOG)}.")

    selected = [pillar] if pillar else list(CHECK_CATALOG)
    shown = sum(len(CHECK_CATALOG[p]) for p in selected)

    lines = [
        f"# Readiness checks ({shown} of {total_check_count()})",
        "",
        "| Check | Name | Pillar | From |",
        "|-------|------|--------|------|",
    ]
    origin_label = {
        "platform": "bedrock-readiness-platform",
        "agent": "sample-bedrock-readiness-agent",
    }
    for pid in selected:
        for check_id, name, origin in CHECK_CATALOG[pid]:
            lines.append(
                f"| `{check_id}` | {name} | {PILLAR_NAMES[pid]} | "
                f"{origin_label.get(origin, origin)} |"
            )

    lines += [
        "",
        "Not every check runs on every assessment -- applicability depends on "
        "workload type and which resources exist in the account.",
        "",
        "Every check's recommendation is plain-text guidance pointing at public "
        "AWS documentation -- including SEC-*, GR-*, and DG-* -- never a "
        "deployable policy, guardrail configuration, or template to apply as-is.",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


@mcp.tool()
def describe_scope() -> str:
    """Explain what this tool assesses, what it deliberately does not, and why.

    Call this when the user asks what the tool covers, whether it checks
    security, what permissions it needs, or whether it can change anything.

    Returns:
        Markdown scope statement.
    """
    pillar_rows = "\n".join(
        f"| {PILLAR_NAMES[pid]} | {len(CHECK_CATALOG[pid])} | "
        f"{CHECK_CATALOG[pid][0][0].split('-')[0]}-* |"
        for pid in CHECK_CATALOG
    )
    return f"""# Bedrock Readiness (readiness-only) -- scope

## What it assesses ({total_check_count()} checks across 8 pillars)

| Pillar | Checks | IDs |
|--------|:------:|-----|
{pillar_rows}

Two assessment sources, usable separately or together:

- **Account scan** -- read-only API calls against a live account (runtime
  evidence). Covers all 8 pillars, including Security, Guardrails, and Data
  Governance.
- **Diagram review** -- a Bedrock multimodal model reads an architecture
  diagram (intended design), for use before infrastructure exists. Covers
  only Observability, Architecture, Quota & Capacity, Cost Optimization, and
  Model Fitness -- narrower than the account scan on purpose (see below).
- **Both** -- adds a design-vs-reality reconciliation per capability, for the
  five pillars the diagram review covers.

## Why the diagram review covers fewer pillars than the account scan

The account scan reaches its Security/Guardrails/Data-Governance findings
through deterministic read-only API calls -- the same kind of call as every
other pillar. The diagram review works differently: a multimodal model looks
at a picture and describes what it sees, which is a fundamentally less
verifiable source for security-adjacent claims. That boundary was left in
place deliberately rather than widened along with the account scan. Three
independent layers enforce it: the model's prompt is scoped to five
categories, an `out_of_scope_notes` field exists for anything else and is
discarded (only its count is reported), and a deterministic filter drops any
finding whose text matches a security/guardrails/data-governance/compliance
marker list regardless of the category the model claimed.

## What no check here can do, for any of the 8 pillars

- Create, modify, or delete any AWS resource
- Write to S3 or anywhere else
- Generate a deployable CloudFormation/Terraform/CDK template, corrected IAM
  policy, or guardrail configuration meant to be applied as-is
- Claim compliance with any regulation or standard

Recommendations are always plain-text guidance describing what to change,
paired with a link to public AWS documentation. Where a Security check reads
an IAM policy DOCUMENT (to check for a wildcard grant, for example), it
reports what the document grants in prose -- it never reproduces the document
or hands back a corrected one.

## Permissions it needs (all read-only)

`sts:GetCallerIdentity`, `bedrock:List*`,
`bedrock:GetModelInvocationLoggingConfiguration`/`GetGuardrail`/
`ListEnforcedGuardrailsConfiguration`/`GetAccountDataRetention`,
`bedrock-agent:List*`, `bedrock-agentcore-control:List*`,
`cloudwatch:GetMetricData`/`GetMetricStatistics`/`ListMetrics`/
`DescribeAlarms`/`ListDashboards`, `logs:DescribeLogGroups`,
`ec2:DescribeVpcEndpoints`, `servicequotas:ListServiceQuotas` (+ change
history), `ce:GetCostAndUsage`, `xray:GetTraceSegmentDestination`,
`iam:ListRoles`/`ListAttachedRolePolicies`/`ListRolePolicies`/
`GetRolePolicy`/`GetPolicy`/`GetPolicyVersion` (policy document reads are
scoped to roles already identified as Bedrock-trusted, never every role in
the account), `cloudtrail:DescribeTrails`/`GetTrailStatus`/
`GetEventSelectors`, `s3:GetBucketEncryption`/`GetPublicAccessBlock` (on the
one bucket configured for Bedrock invocation logging, if any).

One conditional, non-read-only-verb permission: `sts:AssumeRole`, needed ONLY
when a `role_arn` is configured for cross-account assessment. The assumed role
still needs nothing beyond the read-only set above. Without `role_arn` the tool
uses the caller's own credential chain and never calls `AssumeRole`.

Diagram review additionally needs `bedrock:InvokeModel` and, for `s3://`
diagrams, `s3:GetObject` on that object only.

{DISCLAIMER}
"""


# --- Formatting ---------------------------------------------------------------


def _findings_lines(findings: list) -> list[str]:
    if not findings:
        return ["_No findings._"]
    marker = {
        CheckStatus.PASS: "PASS",
        CheckStatus.FAIL: "FAIL",
        CheckStatus.WARN: "WARN",
        CheckStatus.ERROR: "ERROR",
        CheckStatus.SKIPPED: "SKIP",
    }
    ordered = sorted(
        findings,
        key=lambda f: (f.status == CheckStatus.PASS, -f.risk_score),
    )
    lines = []
    for f in ordered:
        if f.status == CheckStatus.SKIPPED:
            continue
        lines.append(
            f"- **[{marker.get(f.status, '?')}]** `{f.check_id}` {f.check_name} "
            f"(risk {f.risk_score}, {f.severity_label}) -- {f.message}"
        )
        if f.recommendation and f.status in (CheckStatus.FAIL, CheckStatus.WARN):
            lines.append(
                f"  - Fix ({f.fix_type.value}, ~{f.effort_minutes} min): {f.recommendation}"
            )
            # Curated AWS docs, looked up by check ID from core/references.py.
            # The model consuming this output must not invent additional links.
            for ref in for_check(f.check_id):
                lines.append(f"    - [{ref.title}]({ref.url})")
    return lines


def _format_report(result, freshness: str = "") -> str:
    counts = result.severity_summary.get("counts", {})
    effort = result.severity_summary.get("effort", {})
    crit_high = effort.get("CRITICAL", 0) + effort.get("HIGH", 0)

    lines = ["# Bedrock Readiness Report", ""]

    if result.score_applicable:
        lines.append(
            f"**Account** `{result.account_id}` | **Region** `{result.region}` | "
            f"**Mode** {result.mode} | **Workload** {result.workload_type}"
        )
        lines += ["", f"## Overall score: {result.overall_score}/100", ""]
    else:
        lines.append(
            f"**Source** design review of `{result.diagram_source}` | "
            f"**Region** `{result.region}` | **Workload** {result.workload_type}"
        )
        lines += [
            "",
            "## No score -- design review only",
            "",
            "A diagram is not evidence about a running account, so no readiness "
            "score is produced. Run `assess_bedrock_platform` with "
            "`include_account_scan=true` to score a live deployment.",
            "",
        ]

    lines += [
        "| Severity | Count | Action |",
        "|----------|:-----:|--------|",
        f"| CRITICAL | {counts.get('CRITICAL', 0)} | Fix before production |",
        f"| HIGH | {counts.get('HIGH', 0)} | Address within the first week |",
        f"| MEDIUM | {counts.get('MEDIUM', 0)} | Plan within 30 days |",
        f"| LOW | {counts.get('LOW', 0)} | Best practice for later |",
        "",
        f"**Effort for CRITICAL + HIGH:** ~{crit_high} min "
        f"({crit_high // 60}h {crit_high % 60}m)",
        "",
    ]

    if result.pillar_results:
        lines += ["## Pillar breakdown", "",
                  "| Pillar | Score | Passed | To resolve |",
                  "|--------|:-----:|:------:|:----------:|"]
        for pr in result.pillar_results:
            lines.append(
                f"| {pr.pillar_name} | {pr.score}/100 | "
                f"{pr.passed_checks}/{pr.total_checks} | {pr.failed_checks} |"
            )
        lines.append("")

    if result.priority_actions:
        lines += ["## Priority actions (ranked by risk)", ""]
        for i, fix in enumerate(result.priority_actions[:10], 1):
            lines.append(
                f"{i}. **[{fix['severity']}]** {fix['check_name']} "
                f"-- {fix['pillar']} ({fix['fix_type']}, ~{fix['effort_minutes']} min)"
            )
        lines.append("")

    # A requested diagram review that did not run must be stated, not omitted.
    # Silence reads as "the diagram was reviewed and looked fine", which is the
    # opposite of what happened. The CLI and HTML reporters already disclose
    # this; this channel must match them.
    if result.diagram_was_skipped:
        lines += [
            "## Diagram review not performed",
            "",
            "A diagram was supplied but the review did not run. The account "
            "assessment above is complete and unaffected.",
            "",
            f"- **Diagram:** `{result.diagram_source}`",
            f"- **Reason:** {result.diagram_skipped_reason}",
            "- **Retried:** no, by design.",
            "",
        ]

    if result.reconciliation:
        lines += _reconciliation_lines(result.reconciliation)

    if result.has_design_review:
        lines += _design_lines(result)

    if result.pillar_results:
        lines += ["## Findings by pillar", ""]
        for pr in result.pillar_results:
            lines += [f"### {pr.pillar_name} -- {pr.score}/100", ""]
            lines += _findings_lines(pr.findings)
            lines.append("")

    footer = []
    if result.score_applicable:
        footer.append(
            f"_Scanned in {result.scan_duration_seconds:.1f}s, "
            f"{result.total_api_calls} read-only API calls._"
        )
    if freshness:
        footer.append(f"_{freshness}._")
    footer.append(DISCLAIMER)
    lines += ["", *footer]
    return "\n".join(lines)


def _reconciliation_lines(rows: list) -> list[str]:
    drift = [r for r in rows if r["verdict"] == "DESIGN_NOT_IMPLEMENTED"]
    lines = ["## Design vs. reality", ""]
    if drift:
        lines.append(
            f"**{len(drift)} capability(ies) shown in the diagram are not present "
            f"in the account.** These are the highest-value rows below."
        )
        lines.append("")
    lines += [
        "| Capability | Pillar | Diagram | Account | Verdict | Severity |",
        "|------------|--------|:-------:|:-------:|---------|:--------:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['pillar']} | {r['design_status']} | "
            f"{r['runtime_status']} | {r['verdict']} | {r['severity']} |"
        )
    lines += ["", "Verdicts:", ""]
    for verdict, meaning in RECONCILIATION_VERDICTS.items():
        lines.append(f"- `{verdict}` -- {meaning}")
    lines.append("")
    return lines


def _design_lines(result) -> list[str]:
    lines = ["## Design review (from the diagram)", ""]
    if result.design_summary:
        lines += [f"**What the diagram appears to show:** {result.design_summary}", ""]
    if result.diagram_model_id:
        lines.append(f"_Analysed by `{result.diagram_model_id}`._")
        lines.append("")

    if result.design_findings:
        lines += _findings_lines(result.design_findings)
    else:
        lines.append("_No in-scope readiness findings from the diagram._")
    lines.append("")

    notes = []
    if result.dropped_finding_count:
        notes.append(
            f"{result.dropped_finding_count} model finding(s) were dropped by the "
            f"scope filter (outside the five readiness pillars)"
        )
    if result.out_of_scope_note_count:
        notes.append(
            f"{result.out_of_scope_note_count} out-of-scope observation(s) were "
            f"discarded unread (security, guardrails, or data governance)"
        )
    if notes:
        lines += [f"_Scope filter: {'; '.join(notes)}._", ""]
        lines += [
            "_This diagram review does not cover security, guardrails, or data "
            "governance -- a picture is a less verifiable source for those than "
            "a live check. Run `assess_bedrock_readiness` (or the Security/"
            "Guardrails/Data Governance pillar tools) against the account "
            "itself for those findings; do not infer them from this diagram._",
            "",
        ]

    lines += [
        "_Design findings are advisory and never affect the readiness score: a "
        "diagram omitting something is not proof the deployment lacks it._", "",
    ]
    return lines


if __name__ == "__main__":
    mcp.run()
