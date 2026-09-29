# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Scoring engine -- calculates pillar scores, overall score, and prioritized actions.

No maturity levels. The overall score is a tracking metric only. Findings are
ranked by risk (Impact x Likelihood) and grouped by severity for prioritized action.
"""

from .models import Config, PillarResult, AssessmentResult, CheckStatus


def calculate_overall_score(pillar_results: list[PillarResult], config: Config) -> int:
    """Weighted sum of pillar scores. Used as a tracking metric only."""
    weights = config.weights
    total = 0.0
    for pr in pillar_results:
        w = weights.get(pr.pillar_id, 0.15)
        total += pr.score * w
    return int(total)


def calculate_priority_actions(pillar_results: list[PillarResult], config: Config) -> list[dict]:
    """Produce a prioritized list of fixes, ranked by risk (highest first)."""
    weights = config.weights
    fix_candidates = []

    for pr in pillar_results:
        w = weights.get(pr.pillar_id, 0.15)
        scoreable = [f for f in pr.findings
                     if f.status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.WARN)]
        total_risk = sum(f.risk_score for f in scoreable)
        if total_risk == 0:
            continue

        for f in pr.findings:
            if f.status in (CheckStatus.FAIL, CheckStatus.WARN):
                point_impact = (f.risk_score / total_risk) * 100 * w
                fix_candidates.append({
                    "check_id": f.check_id,
                    "check_name": f.check_name,
                    "pillar": pr.pillar_name,
                    "severity": f.severity_label,
                    "risk_score": f.risk_score,
                    "score_impact": round(point_impact, 1),
                    "effort_minutes": f.effort_minutes or 30,
                    "fix_type": f.fix_type.value,
                    "depends_on": f.depends_on,
                })

    fix_candidates.sort(
        key=lambda x: (x["risk_score"], x["score_impact"] / max(x["effort_minutes"], 1)),
        reverse=True,
    )
    return _topological_sort(fix_candidates)


def _topological_sort(candidates: list[dict]) -> list[dict]:
    id_to_idx = {c["check_id"]: i for i, c in enumerate(candidates)}
    result = []
    visited = set()

    def visit(idx):
        if idx in visited:
            return
        visited.add(idx)
        c = candidates[idx]
        for dep_id in c.get("depends_on", []):
            if dep_id in id_to_idx:
                visit(id_to_idx[dep_id])
        result.append(c)

    for i in range(len(candidates)):
        visit(i)
    return result


def summarize_by_severity(pillar_results: list[PillarResult]) -> dict:
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    effort = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for pr in pillar_results:
        for f in pr.findings:
            if f.status in (CheckStatus.FAIL, CheckStatus.WARN):
                sev = f.severity_label
                counts[sev] += 1
                effort[sev] += f.effort_minutes or 30
    return {"counts": counts, "effort": effort}


def compute_assessment(
    pillar_results: list[PillarResult],
    config: Config,
    account_id: str,
    region: str,
    scan_duration: float = 0.0,
    api_calls: int = 0,
) -> AssessmentResult:
    """Full scoring computation -- produces the final AssessmentResult.

    Note there is no `remediation` parameter here at all, for any pillar
    including Security/Guardrails/Data Governance -- this scorer has no
    concept of deployable remediation to attach to a result.
    """
    overall = calculate_overall_score(pillar_results, config)
    priority_actions = calculate_priority_actions(pillar_results, config)
    severity_summary = summarize_by_severity(pillar_results)

    return AssessmentResult(
        account_id=account_id,
        region=region,
        mode=config.mode,
        workload_type=config.workload_type,
        pillar_results=pillar_results,
        overall_score=overall,
        priority_actions=priority_actions,
        severity_summary=severity_summary,
        total_api_calls=api_calls,
        scan_duration_seconds=scan_duration,
    )
