# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Python API for Bedrock Readiness (readiness-only).

    from bedrock_readiness import assess, is_production_ready

    result = assess(region="us-east-1")
    if not is_production_ready(region="us-east-1"):
        sys.exit(1)

Three assessment sources are available, all read-only:

    assess()          -- live account scan (runtime evidence)
    review_diagram()  -- architecture diagram review (intended design)
    assess_platform() -- both, plus a design-vs-reality reconciliation

None of them create, modify, or delete any AWS resource, and none generate
deployable remediation templates -- including for Security, Guardrails, and
Data Governance, assessed here on the same terms as every other pillar.
"""

from typing import Callable, Optional

from .core.models import Config, AssessmentResult
from .core.scanner import AccountScanner
from .core.scorer import compute_assessment, summarize_by_severity
from .core import diagram_review
from .config import auto_detect_mode, auto_detect_workload_type
from .modules import PILLAR_MODULES


def assess(
    region: str = "us-east-1",
    workload_type: str = "general",
    mode: str = "auto",
    profile: Optional[str] = None,
    role_arn: Optional[str] = None,
    production_estimate: Optional[dict] = None,
) -> AssessmentResult:
    """Run a readiness-only assessment and return the AssessmentResult."""
    config = Config(
        mode=mode,
        workload_type=workload_type,
        regions=[region],
        profile=profile,
        role_arn=role_arn,
        production_estimate=production_estimate or {},
    )

    scanner = AccountScanner(config)
    scan_data = scanner.scan_all()

    if config.mode == "auto":
        config.mode = auto_detect_mode(scan_data)
    if config.workload_type == "general":
        detected = auto_detect_workload_type(scan_data)
        if detected != "general":
            config.workload_type = detected

    pillar_results = [module.assess(scanner, scan_data, config) for module in PILLAR_MODULES.values()]

    return compute_assessment(
        pillar_results=pillar_results,
        config=config,
        account_id=scanner.account_id,
        region=scanner.region,
        api_calls=scanner.api_call_count,
    )


def assess_pillar(pillar_id: str, region: str = "us-east-1", **kwargs):
    """Run a single pillar's checks."""
    config = Config(regions=[region], **kwargs)
    if pillar_id not in PILLAR_MODULES:
        raise ValueError(
            f"Pillar {pillar_id!r} does not exist in this package. "
            f"Available pillars: {sorted(PILLAR_MODULES)}."
        )
    scanner = AccountScanner(config)
    scan_data = scanner.scan_all()
    return PILLAR_MODULES[pillar_id].assess(scanner, scan_data, config)


def is_production_ready(threshold: int = 70, **kwargs) -> bool:
    """Convenience wrapper for CI/CD gates."""
    result = assess(**kwargs)
    return result.overall_score >= threshold


# --- Design-source assessment (architecture diagram review) ------------------


def review_diagram(
    diagram: str,
    region: str = "us-east-1",
    profile: Optional[str] = None,
    role_arn: Optional[str] = None,
    workload_type: str = "general",
    model_id: Optional[str] = None,
    invoke_fn: Optional[Callable] = None,
) -> AssessmentResult:
    """Review an architecture diagram for readiness, without scanning an account.

    Useful before infrastructure exists, or to sanity-check a proposed design.
    Reads the diagram (local file or `s3:GetObject`) and analyses it with a
    Bedrock multimodal model via `bedrock:InvokeModel`. Nothing is written.

    The returned AssessmentResult has `score_applicable=False`: a diagram is
    not evidence about a live account, so no readiness score is produced.

    Args:
        diagram: local path or `s3://bucket/key` to a PNG/JPEG/GIF/WEBP diagram
        region: region whose Bedrock runtime performs the analysis
        profile: AWS CLI profile name
        role_arn: optional cross-account role to assume (read-only)
        workload_type: weighting/context hint; inferred from the diagram if general
        model_id: override the multimodal model
        invoke_fn: test seam -- supply a stub to run offline with no model call
    """
    config = Config(
        workload_type=workload_type,
        regions=[region],
        profile=profile,
        role_arn=role_arn,
        diagram=diagram,
        diagram_model_id=model_id,
        include_account_scan=False,
    )

    scanner, session, account_id = _session_for(config, invoke_fn=invoke_fn)

    dr = diagram_review.review(
        diagram=diagram,
        session=session,
        region=region,
        workload_type=workload_type,
        model_id=model_id,
        invoke_fn=invoke_fn,
        scan_data=_model_inventory(scanner),
    )

    design_pillars = dr.as_pillar_results()
    return AssessmentResult(
        account_id=account_id,
        region=region,
        mode="design-review",
        workload_type=workload_type,
        pillar_results=[],                 # No runtime evidence gathered
        overall_score=0,
        score_applicable=False,
        priority_actions=[],               # Design findings are ranked by risk instead
        severity_summary=summarize_by_severity(design_pillars),
        design_summary=dr.design_summary,
        design_findings=dr.findings,
        out_of_scope_note_count=dr.out_of_scope_note_count,
        dropped_finding_count=dr.dropped_finding_count,
        diagram_source=dr.diagram_source,
        diagram_model_id=dr.model_id,
        diagram_model_via=dr.model_via,
    )


def assess_platform(
    region: str = "us-east-1",
    profile: Optional[str] = None,
    role_arn: Optional[str] = None,
    workload_type: str = "general",
    mode: str = "auto",
    production_estimate: Optional[dict] = None,
    diagram: Optional[str] = None,
    include_account_scan: bool = True,
    model_id: Optional[str] = None,
    invoke_fn: Optional[Callable] = None,
) -> AssessmentResult:
    """Unified readiness assessment: account scan and/or diagram review.

    With both sources, adds a design-vs-reality reconciliation showing which
    readiness capabilities the diagram promises but the account does not yet
    have (and vice versa).

    The readiness score always comes from the runtime scan alone. Design
    findings are reported alongside it but never move the score -- a model's
    reading of a picture is not evidence about a live account.
    """
    if not include_account_scan and not diagram:
        raise ValueError(
            "Nothing to assess: enable include_account_scan, provide a diagram, or both."
        )

    if not include_account_scan:
        return review_diagram(
            diagram=diagram, region=region, profile=profile, role_arn=role_arn,
            workload_type=workload_type, model_id=model_id, invoke_fn=invoke_fn,
        )

    result = assess(
        region=region,
        workload_type=workload_type,
        mode=mode,
        profile=profile,
        role_arn=role_arn,
        production_estimate=production_estimate,
    )

    if not diagram:
        return result

    config = Config(regions=[region], profile=profile, role_arn=role_arn)
    scanner, session, _ = _session_for(config, invoke_fn=invoke_fn)

    # A failed or unavailable diagram review must never cost the caller their
    # scan results, so it is skipped with a recorded reason rather than raised.
    try:
        dr = diagram_review.review(
            diagram=diagram,
            session=session,
            region=region,
            workload_type=result.workload_type,
            model_id=model_id,
            invoke_fn=invoke_fn,
            scan_data=_model_inventory(scanner),
        )
    except (diagram_review.DiagramReviewSkipped, diagram_review.DiagramReviewError) as e:
        result.diagram_source = diagram
        result.diagram_skipped_reason = str(e)
        return result

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
    return result


def _session_for(config: Config, invoke_fn: Optional[Callable] = None):
    """Build a scanner and session (and resolve the account id) without scanning.

    Instantiating AccountScanner sets up credentials and makes exactly one
    read-only call (`sts:GetCallerIdentity`); `scan_all()` is not invoked here.

    Returns (scanner, session, account_id). When a stub `invoke_fn` is supplied
    and the diagram is a local file, no AWS session is needed at all, so
    failures are tolerated to keep the pipeline testable offline.
    """
    try:
        scanner = AccountScanner(config)
        return scanner, scanner.session, scanner.account_id
    except Exception:
        if invoke_fn is not None:
            return None, None, "offline-stub"
        raise


def _model_inventory(scanner) -> Optional[dict]:
    """The two read-only lists diagram-model selection needs.

    Shaped like `scan_data` so `select_diagram_model` takes one input form
    everywhere. Returns None when the lists cannot be read, which selection
    treats as "no model available" and therefore a skip.

    Two extra read-only calls on the diagram-only path. The `assess --diagram`
    path passes its existing scan_data instead and makes none.
    """
    if scanner is None:
        return None
    foundation_models = scanner.list_foundation_models()
    inference_profiles = scanner.list_inference_profiles()
    return {
        "bedrock": {
            "foundation_models": foundation_models if isinstance(foundation_models, list) else [],
            "inference_profiles": inference_profiles if isinstance(inference_profiles, list) else [],
        }
    }
