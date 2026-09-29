#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Bedrock Readiness (readiness-only) -- CLI Entry Point

Standalone solution. Assesses eight pillars: Observability, Architecture &
Resilience, Quota & Capacity, Cost Optimization, Model Fitness, Security,
Guardrails, and Data Governance. Never generates a deployable remediation
artifact (IAM policy, guardrail configuration, CloudFormation, Terraform) for
any of them -- every recommendation is plain-text guidance. See ../README.md.

All operations are read-only: describe/list/get against AWS, plus
`bedrock:InvokeModel` (and `s3:GetObject` for an s3:// diagram) when reviewing
an architecture diagram. Nothing is created, modified, or deleted.

Usage:
  bedrock-readiness assess --region us-east-1
  bedrock-readiness assess --config bedrock-readiness.yaml
  bedrock-readiness assess --dry-run
  bedrock-readiness assess --diagram <your-diagram>.png
  bedrock-readiness review-diagram --diagram s3://<bucket>/<key>.png
  bedrock-readiness checks
  bedrock-readiness mcp
"""

import argparse
import sys
import time

from .config import load_config, auto_detect_mode, auto_detect_workload_type
from .core.scanner import (
    AccountScanner,
    READ_ONLY_OPERATIONS,
    CONDITIONAL_OPERATIONS,
    NEVER_CALLED_OPERATIONS,
    api_name,
)
from .core.scorer import compute_assessment
from .core.models import Config, RECONCILIATION_VERDICTS
from .core import diagram_review
from .core.references import for_check
from .modules import PILLAR_MODULES, PILLAR_NAMES, CHECK_CATALOG, total_check_count


def main():
    parser = argparse.ArgumentParser(
        prog="bedrock-readiness",
        description="Bedrock Readiness (readiness-only) -- Observability, Architecture, "
                     "Quota & Capacity, Cost Optimization, Model Fitness, Security, "
                     "Guardrails, and Data Governance assessment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  bedrock-readiness assess --region us-east-1\n"
            "  bedrock-readiness assess --config bedrock-readiness.yaml\n"
            "  bedrock-readiness assess --dry-run\n"
            "  bedrock-readiness assess --diagram <your-diagram>.png\n"
            "  bedrock-readiness review-diagram --diagram s3://<bucket>/<key>.png\n"
            "  bedrock-readiness checks --pillar quota_capacity\n"
            "  bedrock-readiness mcp\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    assess_p = sub.add_parser("assess", help="Run readiness assessment")
    assess_p.add_argument("--config", "-c", default=None, help="Path to bedrock-readiness.yaml")
    assess_p.add_argument("--region", "-r", default=None, help="AWS region (comma-separated)")
    assess_p.add_argument("--profile", "-p", default=None, help="AWS CLI profile name")
    assess_p.add_argument("--role-arn", default=None, help="IAM role ARN for cross-account access")
    assess_p.add_argument("--output", "-o", default=None, help="Output file path")
    assess_p.add_argument("--format", "-f", default=None, choices=["html", "markdown", "json"],
                           help="Output format")
    assess_p.add_argument("--previous", default=None, help="Previous state JSON for comparison")
    assess_p.add_argument("--diagram", default=None,
                           help="Optional architecture diagram (local path or s3://bucket/key) to "
                                "review alongside the scan, adding a design-vs-reality comparison")
    assess_p.add_argument("--diagram-model-id", default=None,
                           help="Override the Bedrock multimodal model used for diagram review")
    assess_p.add_argument("--dry-run", action="store_true", help="Show API calls without executing")
    assess_p.add_argument("--verbose", "-v", action="store_true", help="Print each API call")

    diagram_p = sub.add_parser(
        "review-diagram",
        help="Review an architecture diagram only -- no account scan, no score",
    )
    diagram_p.add_argument("--diagram", "-d", required=True,
                            help="Local path or s3://bucket/key to a PNG/JPEG/GIF/WEBP diagram")
    diagram_p.add_argument("--region", "-r", default="us-east-1",
                            help="Region whose Bedrock runtime analyses the diagram")
    diagram_p.add_argument("--profile", "-p", default=None, help="AWS CLI profile name")
    diagram_p.add_argument("--role-arn", default=None, help="IAM role ARN for cross-account access")
    diagram_p.add_argument("--workload-type", default="general",
                            help="Context hint; inferred from the diagram when 'general'")
    diagram_p.add_argument("--diagram-model-id", default=None,
                            help="Override the Bedrock multimodal model")
    diagram_p.add_argument("--output", "-o", default=None, help="Output file path")
    diagram_p.add_argument("--format", "-f", default="markdown",
                            choices=["html", "markdown", "json"], help="Output format")
    diagram_p.add_argument("--verbose", "-v", action="store_true", help="Print each API call")

    checks_p = sub.add_parser("checks", help="List every check this tool implements")
    checks_p.add_argument("--pillar", default=None, choices=sorted(CHECK_CATALOG),
                           help="Narrow to a single pillar")

    sub.add_parser("mcp", help="Run the MCP server (stdio) for Kiro/Claude Code/Cursor/Q")
    sub.add_parser("init", help="Generate a starter config file")

    args = parser.parse_args()

    if args.command == "assess":
        _run_assess(args)
    elif args.command == "review-diagram":
        _run_review_diagram(args)
    elif args.command == "checks":
        _run_checks(args)
    elif args.command == "mcp":
        _run_mcp()
    elif args.command == "init":
        _run_init()
    else:
        parser.print_help()
        sys.exit(1)


def _run_assess(args):
    config = load_config(
        config_path=args.config,
        region=args.region,
        profile=args.profile,
        role_arn=args.role_arn,
        output_format=args.format,
        output_file=args.output,
        dry_run=args.dry_run,
        verbose=args.verbose,
        previous=args.previous,
    )
    # CLI flags override the YAML value, but must not erase it when absent.
    if getattr(args, "diagram", None):
        config.diagram = args.diagram
    if getattr(args, "diagram_model_id", None):
        config.diagram_model_id = args.diagram_model_id

    if config.dry_run:
        _print_dry_run(config)
        return

    print("\nBedrock Readiness (readiness-only)")
    print("   Scope: Observability, Architecture, Quota & Capacity, Cost Optimization,")
    print("          Model Fitness, Security, Guardrails, Data Governance")
    print(f"   Region: {config.regions[0]}")
    print(f"   Profile: {config.profile or 'default'}")
    if config.diagram:
        print(f"   Diagram: {config.diagram} (design-vs-reality comparison enabled)")
    print()

    start_time = time.time()
    try:
        scanner = AccountScanner(config)
    except Exception as e:
        print(f"Failed to connect to AWS: {e}")
        print("   Check credentials, region, and permissions.")
        sys.exit(1)

    print(f"   Account: {scanner.account_id}")
    print("   Scanning...\n")

    scan_data = scanner.scan_all()
    scan_duration = time.time() - start_time

    if config.mode == "auto":
        config.mode = auto_detect_mode(scan_data)
        print(f"   Auto-detected mode: {config.mode}")

    if config.workload_type == "general":
        detected = auto_detect_workload_type(scan_data)
        if detected != "general":
            config.workload_type = detected
            print(f"   Auto-detected workload: {config.workload_type}")

    print()

    pillar_results = []
    for pillar_id, module in PILLAR_MODULES.items():
        print(f"   {pillar_id.replace('_', ' ').title()}...", end=" ")
        pr = module.assess(scanner, scan_data, config)
        pillar_results.append(pr)
        status_label = "OK" if pr.score >= 70 else "WARN" if pr.score >= 50 else "FAIL"
        print(f"[{status_label}] {pr.score}/100 ({pr.passed_checks}/{pr.total_checks} passed)")

    print()

    result = compute_assessment(
        pillar_results=pillar_results,
        config=config,
        account_id=scanner.account_id,
        region=scanner.region,
        scan_duration=scan_duration,
        api_calls=scanner.api_call_count,
    )

    if config.diagram:
        print("   Reviewing architecture diagram...", end=" ")
        try:
            # scan_data already holds the account's foundation models and
            # inference profiles, so model selection costs no extra API calls.
            dr = diagram_review.review(
                diagram=config.diagram,
                session=scanner.session,
                region=scanner.region,
                workload_type=config.workload_type,
                model_id=config.diagram_model_id,
                scan_data=scan_data,
            )
        except (diagram_review.DiagramReviewSkipped,
                diagram_review.DiagramReviewError) as e:
            # Skipped, not retried. The scan results stand on their own.
            result.diagram_source = config.diagram
            result.diagram_skipped_reason = str(e)
            print("[SKIPPED]")
            print(f"      Reason: {e}")
            print("      The readiness assessment below is unaffected.")
        else:
            result.design_summary = dr.design_summary
            result.design_findings = dr.findings
            result.out_of_scope_note_count = dr.out_of_scope_note_count
            result.dropped_finding_count = dr.dropped_finding_count
            result.diagram_source = dr.diagram_source
            result.diagram_model_id = dr.model_id
            result.diagram_model_via = dr.model_via
            result.reconciliation = diagram_review.reconcile(
                dr.capability_statuses, result.pillar_results
            )
            drift = [r for r in result.reconciliation
                     if r["verdict"] == "DESIGN_NOT_IMPLEMENTED"]
            print(f"[OK] {len(dr.findings)} design finding(s), "
                  f"{len(drift)} capability(ies) in the design but not in the account")
            print(f"      Model: {dr.model_id} ({dr.model_via})")
        print()

    counts = result.severity_summary.get("counts", {})
    effort = result.severity_summary.get("effort", {})
    crit_high_effort = effort.get("CRITICAL", 0) + effort.get("HIGH", 0)
    print("   " + "-" * 48)
    print(f"   Overall Score: {result.overall_score}/100")
    print(f"   Critical: {counts.get('CRITICAL', 0)}  |  High: {counts.get('HIGH', 0)}  |  "
          f"Medium: {counts.get('MEDIUM', 0)}  |  Low: {counts.get('LOW', 0)}")
    print("   " + "-" * 48)
    print()

    if counts.get("CRITICAL", 0) > 0:
        print(f"   {counts['CRITICAL']} critical finding(s) -- fix before production "
              f"(~{crit_high_effort} min for all critical + high)")
        print()

    output_file = config.output_file or f"readiness-report.{config.output_format}"
    if config.output_format == "json":
        _write_json_report(result, output_file)
    elif config.output_format == "markdown":
        _write_markdown_report(result, output_file)
    else:
        _write_html_report(result, output_file)

    print(f"   Report saved: {output_file}")
    print(f"   Completed in {scan_duration:.1f}s ({scanner.api_call_count} API calls, all read-only)")

    if scanner.errors:
        print(f"   {len(scanner.errors)} API call(s) failed (permission denied or service unavailable)")

    print()
    sys.exit(0 if result.overall_score >= 70 else 1)


def _run_review_diagram(args):
    """Diagram-only review: no account scan, and therefore no readiness score."""
    from .api import review_diagram

    print("\nBedrock Readiness -- architecture diagram review (readiness-only)")
    print(f"   Diagram: {args.diagram}")
    print(f"   Region: {args.region} (Bedrock runtime for the analysis)")
    print("   Scope: Observability, Architecture, Quota & Capacity, Cost Optimization,")
    print("          Model Fitness -- narrower than `assess`, which also covers Security,")
    print("          Guardrails, and Data Governance. See README.md for why.")
    print("   Read-only: reads the diagram and calls bedrock:InvokeModel. Nothing is written.")
    if args.diagram_model_id:
        print(f"   Model: {args.diagram_model_id} (explicitly requested)")
    else:
        print("   Model: auto-selected -- cheapest image-capable model this account has")
    print()

    try:
        result = review_diagram(
            diagram=args.diagram,
            region=args.region,
            profile=args.profile,
            role_arn=args.role_arn,
            workload_type=args.workload_type,
            model_id=args.diagram_model_id,
        )
    except diagram_review.DiagramReviewSkipped as e:
        # Nothing else to produce -- the diagram is the whole point of this
        # command -- so report the reason and stop. Not retried.
        print(f"   Diagram review skipped: {e}")
        print()
        print("   To assess the account instead (no model required):")
        print("     bedrock-readiness assess --region " + args.region)
        print()
        sys.exit(1)
    except diagram_review.DiagramReviewError as e:
        print(f"   Diagram could not be read: {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"   Diagram review failed: {e}")
        print("   Check credentials, model access, and the diagram path.\n")
        sys.exit(1)

    counts = result.severity_summary.get("counts", {})
    print(f"   Design findings: {len(result.design_findings)}")
    print(f"   Critical: {counts.get('CRITICAL', 0)}  |  High: {counts.get('HIGH', 0)}  |  "
          f"Medium: {counts.get('MEDIUM', 0)}  |  Low: {counts.get('LOW', 0)}")
    if result.dropped_finding_count or result.out_of_scope_note_count:
        print(f"   Scope filter dropped {result.dropped_finding_count} finding(s) and discarded "
              f"{result.out_of_scope_note_count} out-of-scope observation(s)")
    print("   No readiness score: a diagram is not evidence about a running account.")
    print()

    output_file = args.output or f"diagram-review.{'md' if args.format == 'markdown' else args.format}"
    if args.format == "json":
        _write_json_report(result, output_file)
    elif args.format == "html":
        _write_html_report(result, output_file)
    else:
        _write_markdown_report(result, output_file)

    print(f"   Report saved: {output_file}\n")


def _run_checks(args):
    """Print the declarative check catalog."""
    selected = [args.pillar] if args.pillar else list(CHECK_CATALOG)
    shown = sum(len(CHECK_CATALOG[p]) for p in selected)

    print(f"\nBedrock Readiness (readiness-only) -- {shown} of {total_check_count()} checks\n")
    for pid in selected:
        print(f"  {PILLAR_NAMES[pid]}")
        for check_id, name in CHECK_CATALOG[pid]:
            print(f"    {check_id:<9} {name}")
        print()
    print("  Applicability varies: not every check runs for every workload type,")
    print("  and checks are skipped when the resources they inspect don't exist.")
    print("  Every recommendation, including SEC-*/GR-*/DG-*, is plain-text")
    print("  guidance -- never a deployable policy, guardrail, or template.\n")


def _run_mcp():
    """Launch the MCP server over stdio."""
    try:
        from .delivery.mcp_server import mcp
    except ImportError as e:
        print(f"MCP server unavailable: {e}")
        print("Install the optional dependency:  pip install 'mcp>=1.2.0'")
        sys.exit(1)
    mcp.run()


def _print_dry_run(config: Config):
    """Report the declared API surface without performing any call.

    Rendered from the declarations in core/scanner.py, beside the code that
    makes the calls, so this output cannot drift from actual behaviour.
    tests/test_readonly_surface.py checks the declarations against source.
    """
    print("\nDRY RUN -- no AWS call is made. The scan would perform:\n")
    for service, method, note in READ_ONLY_OPERATIONS:
        suffix = f"  -- {note}" if note else ""
        print(f"  - {api_name(service, method)}{suffix}")

    diagram_ops = {"s3": bool(config.diagram) and str(config.diagram).startswith("s3://"),
                   "runtime": bool(config.diagram)}
    if config.diagram:
        print("\n  Diagram review is enabled, adding:")
        if not diagram_ops["s3"]:
            print(f"  - local filesystem read of {config.diagram} (no AWS call)")
        for service, method, note in CONDITIONAL_OPERATIONS:
            if service == "s3" and not diagram_ops["s3"]:
                continue
            if service == "sts" and not config.role_arn:
                continue
            print(f"  - {api_name(service, method)}  -- {note}")
        if config.diagram_model_id:
            print(f"      Model: {config.diagram_model_id} (explicitly requested)")
        else:
            print("      Model: selected from this account's image-capable models,")
            print("      cheapest first, through a cross-region profile when one exists.")
            print("      No default is assumed. Skipped without retry if none is available.")
    elif config.role_arn:
        print(f"\n  - {api_name('sts', 'assume_role')}  -- --role-arn was supplied")

    print("\n  Every operation above is describe/list/get, plus inference for the")
    print("  optional diagram review. Nothing is created, modified, or deleted, no")
    print("  deployable remediation template is generated, and no data leaves your")
    print("  account.")
    print("\n  Not called by this package, and absent from its code:")
    for service, method, capability in NEVER_CALLED_OPERATIONS:
        print(f"  - {api_name(service, method)}  -- would expose {capability}")
    print()


def _write_json_report(result, output_file: str):
    import json

    def _finding_dict(f):
        return {
            "check_id": f.check_id,
            "check_name": f.check_name,
            "status": f.status.value,
            "impact": f.impact,
            "likelihood": f.likelihood,
            "risk_score": f.risk_score,
            "severity": f.severity_label,
            "message": f.message,
            "recommendation": f.recommendation,
            "resource_ids": f.resource_ids,
            "fix_type": f.fix_type.value,
            "effort_minutes": f.effort_minutes,
            "source": f.source.value,
            # Curated AWS docs from core/references.py, attached at render time.
            # Never model-generated -- Finding has no URL field.
            "references": [{"title": r.title, "url": r.url} for r in for_check(f.check_id)],
        }

    data = {
        "scope": "readiness-only",
        "assessed_pillars": list(PILLAR_MODULES),
        "account_id": result.account_id,
        "region": result.region,
        "mode": result.mode,
        "workload_type": result.workload_type,
        "overall_score": result.overall_score,
        "score_applicable": result.score_applicable,
        "severity_summary": result.severity_summary,
        "priority_actions": result.priority_actions,
        "pillars": [],
    }
    for pr in result.pillar_results:
        data["pillars"].append({
            "id": pr.pillar_id,
            "name": pr.pillar_name,
            "score": pr.score,
            "findings": [_finding_dict(f) for f in pr.findings],
        })

    if result.diagram_was_skipped:
        data["diagram_review_skipped"] = {
            "diagram_source": result.diagram_source,
            "reason": result.diagram_skipped_reason,
            "retried": False,
        }

    if result.has_design_review:
        data["design_review"] = {
            "diagram_source": result.diagram_source,
            "model_id": result.diagram_model_id,
            "model_via": result.diagram_model_via,
            "summary": result.design_summary,
            "findings": [_finding_dict(f) for f in result.design_findings],
            "dropped_finding_count": result.dropped_finding_count,
            "out_of_scope_note_count": result.out_of_scope_note_count,
            "reconciliation": result.reconciliation,
        }

    with open(output_file, "w") as fp:
        json.dump(data, fp, indent=2)


def _write_markdown_report(result, output_file: str):
    lines = ["# Bedrock Readiness Report\n"]
    lines.append("Observability | Architecture & Resilience | Quota & Capacity | "
                 "Cost Optimization | Model Fitness | Security | Guardrails | "
                 "Data Governance\n")
    lines.append("> **Note** This assessment covers operational readiness for production.")
    lines.append("> Every finding is plain-text guidance pointing at public AWS")
    lines.append("> documentation -- never a deployable policy, guardrail configuration,")
    lines.append("> or template to apply as-is.")
    lines.append(">")
    lines.append("> Sample content -- not for production use without additional testing.\n")

    if result.score_applicable:
        lines.append(f"**Account:** {result.account_id} | **Region:** {result.region}")
        lines.append(f"**Mode:** {result.mode} | **Workload:** {result.workload_type}\n")
        lines.append(f"## Overall Score: {result.overall_score}/100\n")
    else:
        lines.append(f"**Source:** design review of `{result.diagram_source}`")
        lines.append(f"**Region:** {result.region} | **Workload:** {result.workload_type}\n")
        lines.append("## No score -- design review only\n")
        lines.append("A diagram is not evidence about a running account, so no readiness")
        lines.append("score is produced. Run `assess` against an account to score it.\n")

    counts = result.severity_summary.get("counts", {})
    lines.append(f"**Critical:** {counts.get('CRITICAL', 0)} | **High:** {counts.get('HIGH', 0)} | "
                 f"**Medium:** {counts.get('MEDIUM', 0)} | **Low:** {counts.get('LOW', 0)}\n")

    for pr in result.pillar_results:
        lines.append(f"### {pr.pillar_name} -- {pr.score}/100\n")
        for f in pr.findings:
            icon = "[PASS]" if f.status.value == "PASS" else "[FAIL]" if f.status.value == "FAIL" else "[WARN]"
            lines.append(f"- {icon} **{f.check_name}** -- {f.message}")
            if f.recommendation:
                lines.append(f"  - Fix: {f.recommendation}")
                for ref in for_check(f.check_id):
                    lines.append(f"  - Docs: [{ref.title}]({ref.url})")
        lines.append("")

    if result.diagram_was_skipped:
        lines.append("## Diagram Review Not Performed\n")
        lines.append("A diagram was supplied but the review did not run. The account")
        lines.append("assessment above is complete and unaffected.\n")
        lines.append(f"- **Diagram:** `{result.diagram_source}`")
        lines.append(f"- **Reason:** {result.diagram_skipped_reason}\n")

    if result.reconciliation:
        drift = [r for r in result.reconciliation if r["verdict"] == "DESIGN_NOT_IMPLEMENTED"]
        lines.append("## Design vs. Reality\n")
        if drift:
            lines.append(f"**{len(drift)} capability(ies) appear in the diagram but not in "
                         f"the account.**\n")
        lines.append("| Capability | Pillar | Diagram | Account | Verdict | Severity |")
        lines.append("|------------|--------|:-------:|:-------:|---------|:--------:|")
        for r in result.reconciliation:
            lines.append(f"| {r['label']} | {r['pillar']} | {r['design_status']} | "
                         f"{r['runtime_status']} | {r['verdict']} | {r['severity']} |")
        lines.append("")
        for verdict, meaning in RECONCILIATION_VERDICTS.items():
            lines.append(f"- `{verdict}` -- {meaning}")
        lines.append("")

    if result.has_design_review:
        lines.append("## Design Review (from the diagram)\n")
        if result.design_summary:
            lines.append(f"**What the diagram appears to show:** {result.design_summary}\n")
        if result.diagram_model_id:
            via = f" ({result.diagram_model_via})" if result.diagram_model_via else ""
            lines.append(f"_Diagram `{result.diagram_source}` analysed by "
                         f"`{result.diagram_model_id}`{via}, selected from the models "
                         f"available to this account._\n")
        for f in sorted(result.design_findings, key=lambda x: -x.risk_score):
            lines.append(f"- [{f.severity_label}] **{f.check_name}** (`{f.check_id}`) -- {f.message}")
            if f.recommendation:
                lines.append(f"  - Consider ({f.fix_type.value}, ~{f.effort_minutes} min): "
                             f"{f.recommendation}")
                for ref in for_check(f.check_id):
                    lines.append(f"  - Docs: [{ref.title}]({ref.url})")
        if not result.design_findings:
            lines.append("_No readiness findings from the diagram._")
        lines.append("")
        set_aside = result.dropped_finding_count + result.out_of_scope_note_count
        if set_aside:
            lines.append(f"_This review reports the {len(result.design_findings)} observation(s) "
                         f"that fall within the five readiness pillars above. A further "
                         f"{set_aside} observation(s) sat outside that focus and are not "
                         f"included here._\n")
        lines.append("_Design findings are advisory and never affect the readiness score._\n")

    with open(output_file, "w") as fp:
        fp.write("\n".join(lines))


def _write_html_report(result, output_file: str):
    from .core.reporter import generate_html_report
    html = generate_html_report(result)
    with open(output_file, "w") as fp:
        fp.write(html)


def _run_init():
    template = """# Bedrock Readiness (Readiness-Only) -- Configuration
version: "readiness-only-1.0"

mode: "auto"
workload_type: "general"

regions:
  - us-east-1

output:
  format: "html"
  file: "readiness-report.html"
  include_fix_templates: true
"""
    filename = "bedrock-readiness.yaml"
    with open(filename, "w") as f:
        f.write(template)
    print(f"Generated: {filename}")
    print(f"   Edit it, then run: bedrock-readiness assess --config {filename}")


if __name__ == "__main__":
    main()
