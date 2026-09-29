# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Report generator -- severity-based priorities.

No maturity levels. The score is a tracking metric only. Findings are framed
as severity-based priorities (CRITICAL -> HIGH -> MEDIUM -> LOW). There is no
remediation-template section anywhere in this file -- this package never
generates CloudFormation/Terraform, so there is nothing to render.
"""

from datetime import datetime
from .models import AssessmentResult, CheckStatus


def generate_html_report(result: AssessmentResult) -> str:
    score_color = _score_color(result.overall_score)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    all_findings = []
    for pr in result.pillar_results:
        for f in pr.findings:
            if f.status in (CheckStatus.FAIL, CheckStatus.WARN):
                all_findings.append((pr.pillar_name, f))

    # A design-only review has no runtime findings. Drive the severity cards
    # from the design findings in that case, so the report doesn't read as
    # "all checks passed" when in fact nothing was scanned.
    if not result.score_applicable and not all_findings:
        all_findings = [("Design review", f) for f in result.design_findings]

    critical = [(p, f) for p, f in all_findings if f.risk_score >= 12]
    high = [(p, f) for p, f in all_findings if 6 <= f.risk_score < 12]
    medium = [(p, f) for p, f in all_findings if 3 <= f.risk_score < 6]
    low = [(p, f) for p, f in all_findings if f.risk_score < 3]

    critical_effort = sum(f.effort_minutes for _, f in critical)
    high_effort = sum(f.effort_minutes for _, f in high)

    pillar_rows = ""
    for pr in result.pillar_results:
        weight = _get_weight(pr.pillar_id, result.workload_type)
        contribution = round(pr.score * weight, 1)
        max_possible = round(100 * weight, 1)
        gap = round(max_possible - contribution, 1)
        color = _score_color(pr.score)
        pillar_rows += f"""
        <tr>
          <td>{pr.pillar_name}</td>
          <td style="color:{color};font-weight:600">{pr.score}/100</td>
          <td>{int(weight*100)}%</td>
          <td>{contribution}</td>
          <td>{max_possible}</td>
          <td style="color:{'#dc2626' if gap > 10 else '#d97706' if gap > 5 else '#16a34a'}">{gap}</td>
        </tr>"""

    priority_html = _render_priority_actions(result, all_findings)

    pillar_sections = ""
    for pr in result.pillar_results:
        findings_html = _render_findings(pr)
        icon = "PASS" if pr.score >= 70 else "WARN" if pr.score >= 50 else "FAIL"
        pillar_sections += f"""
    <details class="pillar" id="pillar-{pr.pillar_id}" {'open' if pr.score < 70 else ''}>
      <summary>[{icon}] {pr.pillar_name} -- {pr.score}/100 ({pr.failed_checks} to resolve)</summary>
      <div class="pillar-content">
        {findings_html}
      </div>
    </details>"""

    reconciliation_html = _render_reconciliation(result)
    design_html = _render_design_review(result)
    skipped_html = ""
    if result.diagram_was_skipped:
        skipped_html = f"""
<section id="diagram-skipped">
  <h2>Diagram review not performed</h2>
  <p class="section-intro">A diagram was supplied but the review did not run. The
  account assessment below is complete and unaffected.</p>
  <p class="scope-note"><strong>Diagram:</strong> <code>{result.diagram_source}</code><br>
  <strong>Reason:</strong> {result.diagram_skipped_reason}</p>
</section>"""

    if result.score_applicable:
        score_html = f'<div class="score-badge" style="background:{score_color}">{result.overall_score}/100</div>'
        header_meta = (
            f'Account: <code>{result.account_id}</code> | '
            f'Region: <code>{result.region}</code> | '
            f'Mode: <code>{result.mode}</code> | '
            f'Workload: <code>{result.workload_type}</code> | '
            f'Date: {timestamp}'
        )
    else:
        score_html = '<div class="score-badge unscored">Design review -- not scored</div>'
        header_meta = (
            f'Source: <code>diagram review</code> | '
            f'Diagram: <code>{result.diagram_source}</code> | '
            f'Region: <code>{result.region}</code> | '
            f'Workload: <code>{result.workload_type}</code> | '
            f'Date: {timestamp}'
        )

    # Sidebar navigation, built from the sections actually rendered so a
    # design-only report does not advertise a Pillar Breakdown it lacks.
    nav_items: list[tuple[str, str, list]] = [
        ("status", "Status summary", []),
        ("priority", "Priority Actions", []),
    ]
    if result.reconciliation:
        nav_items.append(("reconciliation", "Design vs. Reality", []))
    if result.has_design_review:
        nav_items.append(("design-review", "Design Review", []))
    if result.diagram_was_skipped:
        nav_items.append(("diagram-skipped", "Diagram Review Skipped", []))
    if result.pillar_results:
        nav_items.append(("summary", "Pillar Breakdown", []))
        nav_items.append((
            "pillars", "Detailed Findings",
            [(f"pillar-{pr.pillar_id}", pr.pillar_name, pr.score)
             for pr in result.pillar_results],
        ))
    nav_html = _render_nav(nav_items, result, critical, high)

    summary_section = ""
    if result.pillar_results:
        summary_section = f"""
<section id="summary">
  <h2>Pillar Breakdown</h2>
  <p class="section-intro">Score per domain. Weight reflects importance for your workload type ({result.workload_type}).</p>
  <table>
    <thead><tr>
      <th>Pillar</th><th>Score</th><th>Weight</th><th>Contribution</th><th>Max</th><th>Gap</th>
    </tr></thead>
    <tbody>{pillar_rows}</tbody>
  </table>
</section>"""

    pillars_section = ""
    if pillar_sections:
        pillars_section = f"""
<section id="pillars">
  <h2>Detailed Findings</h2>
  {pillar_sections}
</section>"""

    if result.score_applicable:
        footer_scope = (
            f"This assessment made {result.total_api_calls} read-only API calls in "
            f"{result.scan_duration_seconds:.1f}s."
        )
    else:
        footer_scope = (
            "This report reviewed an architecture diagram only. No account was scanned, "
            "so no readiness score was produced."
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bedrock Readiness Report (Readiness-Only)</title>
<style>
{_get_css()}
</style>
</head>
<body>
<header>
  <h1>Bedrock Readiness Report</h1>
  <div class="scope-badge">Observability &middot; Architecture &amp; Resilience &middot; Quota &amp; Capacity &middot; Cost Optimization &middot; Model Fitness</div>
  <div class="meta">
    {header_meta}
  </div>
  {score_html}
</header>

<div class="note-banner">
  <span class="note-label">Note</span>
  This assessment covers operational readiness for production, across all eight
  pillars below. Every recommendation is plain-text guidance pointing at public
  AWS documentation -- never a deployable policy, guardrail configuration, or
  template to apply as-is.
</div>

<div class="layout">
{nav_html}
<main>

<section id="status" class="status-section">
  <div class="status-grid">
    <div class="status-card {'critical-card' if critical else 'ok-card'}">
      <div class="status-count">{len(critical)}</div>
      <div class="status-label">Critical</div>
      <div class="status-desc">{'Fix before production' if critical else 'None -- no production blockers'}</div>
    </div>
    <div class="status-card {'high-card' if high else 'ok-card'}">
      <div class="status-count">{len(high)}</div>
      <div class="status-label">High</div>
      <div class="status-desc">{'Address within first week' if high else 'All clear'}</div>
    </div>
    <div class="status-card medium-card">
      <div class="status-count">{len(medium)}</div>
      <div class="status-label">Medium</div>
      <div class="status-desc">Plan within 30 days</div>
    </div>
    <div class="status-card low-card">
      <div class="status-count">{len(low)}</div>
      <div class="status-label">Low</div>
      <div class="status-desc">Best practices for later</div>
    </div>
  </div>
</section>

<section id="effort-summary">
  <p class="effort-line">
    <strong>Effort to fix all Critical + High findings:</strong>
    ~{critical_effort + high_effort} minutes ({(critical_effort + high_effort) // 60}h {(critical_effort + high_effort) % 60}m)
  </p>
</section>

<section id="priority">
  <h2>Priority Actions</h2>
  <p class="section-intro">Ordered by risk (highest first).</p>
  {priority_html}
</section>
{reconciliation_html}
{design_html}
{skipped_html}
{summary_section}
{pillars_section}
</main>
</div>

<footer>
  <p>{footer_scope}
  All operations were read-only -- nothing was created, modified, or deleted, and no data
  was sent outside your account. Recommendations are guidance to apply yourself; this
  report does not generate deployable templates.</p>
  <p>Scope: observability, architecture and resilience, capacity, cost, model fitness,
  security, guardrails, and data governance. Where a finding describes an IAM policy or
  guardrail configuration, it reports what that configuration grants in prose --
  it never reproduces or corrects the configuration itself.</p>
  <p><strong>Sample content -- not for production use without additional security testing.</strong>
  Provided as is, with no warranty of workmanship or fitness for purpose, and no claim of
  compliance with any regulation or standard.</p>
  <p><em>Generated by Bedrock Readiness (readiness-only) -- {timestamp}</em></p>
</footer>
</body>
</html>"""
    return html


def _render_priority_actions(result: AssessmentResult, all_findings: list) -> str:
    if not all_findings:
        if not result.score_applicable:
            return ("<p class='all-clear'>No in-scope readiness findings from the diagram.</p>")
        return "<p class='all-clear'>No findings to address -- all checks passed!</p>"

    all_findings.sort(key=lambda x: x[1].risk_score, reverse=True)

    # Score impact is only meaningful when a live account was scored. In a
    # design-only review there is no score for a fix to move.
    if not result.score_applicable:
        html = ("<table class='priority-table'><thead><tr>"
                "<th>#</th><th>Severity</th><th>Finding</th><th>Source</th>"
                "<th>Type</th><th>Effort</th></tr></thead><tbody>")
        for i, (label, f) in enumerate(all_findings[:15], 1):
            sev_class = f.severity_label.lower()
            html += f"""<tr class='{sev_class}-row'>
              <td>{i}</td>
              <td><span class='severity-badge {sev_class}'>{f.severity_label}</span></td>
              <td><strong>{f.check_name}</strong><br><span class='finding-msg'>{f.message[:80]}</span></td>
              <td>{label}</td>
              <td><span class='badge {f.fix_type.value}'>{f.fix_type.value}</span></td>
              <td>{f.effort_minutes} min</td>
            </tr>"""
        html += "</tbody></table>"
        return html

    html = "<table class='priority-table'><thead><tr>"
    html += "<th>#</th><th>Severity</th><th>Finding</th><th>Pillar</th><th>Type</th><th>Effort</th><th>Score Impact</th>"
    html += "</tr></thead><tbody>"

    weight_map = {}
    for pr in result.pillar_results:
        w = _get_weight(pr.pillar_id, result.workload_type)
        scoreable = [f for f in pr.findings if f.status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.WARN)]
        total_risk = sum(f.risk_score for f in scoreable)
        weight_map[pr.pillar_name] = (w, total_risk)

    for i, (pillar_name, f) in enumerate(all_findings[:15], 1):
        sev = f.severity_label
        sev_class = sev.lower()
        w, total_risk = weight_map.get(pillar_name, (0.15, 1))
        impact = round((f.risk_score / max(total_risk, 1)) * 100 * w, 1) if total_risk > 0 else 0

        html += f"""<tr class='{sev_class}-row'>
          <td>{i}</td>
          <td><span class='severity-badge {sev_class}'>{sev}</span></td>
          <td><strong>{f.check_name}</strong><br><span class='finding-msg'>{f.message[:80]}</span></td>
          <td>{pillar_name}</td>
          <td><span class='badge {f.fix_type.value}'>{f.fix_type.value}</span></td>
          <td>{f.effort_minutes} min</td>
          <td>+{impact} pts</td>
        </tr>"""

    html += "</tbody></table>"
    return html


def _render_nav(nav_items: list, result: AssessmentResult,
                critical: list, high: list) -> str:
    """Sticky sidebar of in-page links.

    Deliberately JavaScript-free: the report is often archived, emailed, or
    opened from a file path, and a no-script document behaves identically
    everywhere. Anchors land on each section's heading; a collapsed pillar
    still shows its summary line, so the target is always visible.
    """
    items_html = ""
    for anchor, label, children in nav_items:
        sub = ""
        if children:
            sub_items = "".join(
                f'<li><a href="#{cid}">{cname}</a>'
                f'<span class="nav-score {_score_class(cscore)}">{cscore}</span></li>'
                for cid, cname, cscore in children
            )
            sub = f"<ul class='nav-sub'>{sub_items}</ul>"
        items_html += f'<li><a href="#{anchor}">{label}</a>{sub}</li>'

    if result.score_applicable:
        badge = (
            f'<div class="nav-score-badge" style="background:{_score_color(result.overall_score)}">'
            f'{result.overall_score}<span>/100</span></div>'
        )
    else:
        badge = '<div class="nav-score-badge unscored">Not scored</div>'

    counts = ""
    if critical or high:
        parts = []
        if critical:
            parts.append(f'<span class="nav-count critical">{len(critical)} critical</span>')
        if high:
            parts.append(f'<span class="nav-count high">{len(high)} high</span>')
        counts = f'<div class="nav-counts">{"".join(parts)}</div>'

    return f"""<nav class="toc" aria-label="Report sections">
  {badge}
  {counts}
  <div class="toc-title">On this page</div>
  <ul>{items_html}</ul>
</nav>"""


def _score_class(score: int) -> str:
    if score >= 85:
        return "ok"
    if score >= 70:
        return "fair"
    if score >= 50:
        return "warn"
    return "bad"


def _render_references(check_id: str) -> str:
    """Curated AWS documentation links for a check.

    Links come from the static map in core/references.py, looked up by a check
    ID this code generated. No link originates from a model, and `Finding` has
    no URL field for one to be smuggled through.
    """
    from .references import for_check

    refs = for_check(check_id)
    if not refs:
        return ""
    items = "".join(
        f'<li><a href="{r.url}" target="_blank" rel="noopener noreferrer">{r.title}</a></li>'
        for r in refs
    )
    return f'<div class="refs"><strong>AWS documentation:</strong><ul>{items}</ul></div>'


def _render_reconciliation(result: AssessmentResult) -> str:
    """Design-vs-reality table. Rendered only when both sources are present."""
    if not result.reconciliation:
        return ""

    from .models import RECONCILIATION_VERDICTS

    drift = [r for r in result.reconciliation if r["verdict"] == "DESIGN_NOT_IMPLEMENTED"]
    lead = (
        f"<p class='drift-callout'><strong>{len(drift)} capability(ies) shown in the diagram "
        f"are not present in the account.</strong> These are the rows worth acting on first.</p>"
        if drift else
        "<p class='section-intro'>No capability in the diagram is missing from the account.</p>"
    )

    rows = ""
    for r in result.reconciliation:
        verdict_class = r["verdict"].lower().replace("_", "-")
        sev_class = r["severity"].lower()
        rows += f"""
        <tr class="verdict-{verdict_class}">
          <td>{r['label']}</td>
          <td>{r['pillar']}</td>
          <td class="status-cell">{r['design_status']}</td>
          <td class="status-cell">{r['runtime_status']}</td>
          <td><span class="verdict-badge {verdict_class}">{r['verdict']}</span></td>
          <td><span class="severity-badge {sev_class}">{r['severity']}</span></td>
        </tr>"""

    legend = "".join(
        f"<li><code>{verdict}</code> -- {meaning}</li>"
        for verdict, meaning in RECONCILIATION_VERDICTS.items()
    )

    return f"""
<section id="reconciliation">
  <h2>Design vs. Reality</h2>
  <p class="section-intro">What the diagram promises, compared against what the read-only
  scan actually found. The runtime column comes entirely from real check results.</p>
  {lead}
  <table>
    <thead><tr>
      <th>Capability</th><th>Pillar</th><th>Diagram</th><th>Account</th><th>Verdict</th><th>Severity</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <details class="legend">
    <summary>What the verdicts mean</summary>
    <ul>{legend}</ul>
  </details>
</section>"""


def _render_design_review(result: AssessmentResult) -> str:
    """Findings from the diagram, kept visually separate from runtime evidence."""
    if not result.has_design_review:
        return ""

    summary_html = (
        f"<p class='design-summary'><strong>What the diagram appears to show:</strong> "
        f"{result.design_summary}</p>"
        if result.design_summary else ""
    )

    if result.design_findings:
        rows = ""
        for f in sorted(result.design_findings, key=lambda x: -x.risk_score):
            sev_class = f.severity_label.lower()
            rec = (
                f"<div class='fix-detail'><strong>Consider</strong> ({f.fix_type.value}, "
                f"~{f.effort_minutes} min): {f.recommendation}"
                f"{_render_references(f.check_id)}</div>"
                if f.recommendation else ""
            )
            rows += f"""
            <tr class="{sev_class}-row">
              <td><span class="severity-badge {sev_class}">{f.severity_label}</span></td>
              <td><code>{f.check_id}</code></td>
              <td><strong>{f.check_name}</strong><br><span class="finding-msg">{f.message}</span>{rec}</td>
              <td>{f.risk_score}</td>
            </tr>"""
        findings_html = f"""
  <table>
    <thead><tr><th>Severity</th><th>ID</th><th>Finding</th><th>Risk</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>"""
    else:
        findings_html = "<p class='all-clear'>No in-scope readiness findings from the diagram.</p>"

    filter_note = ""
    total_set_aside = result.dropped_finding_count + result.out_of_scope_note_count
    if total_set_aside:
        kept = len(result.design_findings)
        filter_note = (
            f"<p class='scope-note'>This review reports the {kept} observation(s) that fall "
            f"within the five readiness pillars above. A further {total_set_aside} "
            f"observation(s) sat outside that focus and are not included here.</p>"
        )

    via_label = {
        "global-cris": "global cross-region inference profile",
        "geo-cris": "geographic cross-region inference profile",
        "direct": "direct invocation",
        "override": "explicitly requested",
    }.get(result.diagram_model_via, result.diagram_model_via)
    via_suffix = f" via {via_label}" if via_label else ""
    model_note = (
        f"<p class='section-intro'>Diagram <code>{result.diagram_source}</code> analysed by "
        f"<code>{result.diagram_model_id}</code>{via_suffix}. Selected from the models "
        f"available to this account.</p>"
        if result.diagram_model_id else ""
    )

    return f"""
<section id="design-review">
  <h2>Design Review (from the diagram)</h2>
  {model_note}
  {summary_html}
  {findings_html}
  {filter_note}
  <p class='scope-note'>Design findings are advisory and never affect the readiness score.
  A diagram omitting something is not proof the deployment lacks it -- treat these as
  questions to confirm, not defects.</p>
</section>"""


def _render_findings(pr) -> str:
    html = "<table class='findings'><thead><tr><th>Status</th><th>Check</th><th>Risk</th><th>Message</th></tr></thead><tbody>"
    for f in pr.findings:
        label = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "ERROR": "ERROR", "SKIPPED": "SKIP"}.get(f.status.value, "?")
        risk_class = "critical" if f.risk_score >= 12 else "high" if f.risk_score >= 6 else "medium"
        html += f"<tr class='{f.status.value.lower()}'>"
        html += f"<td>{label}</td><td><strong>{f.check_name}</strong></td>"
        html += f"<td class='{risk_class}'>{f.risk_score}</td>"
        html += f"<td>{f.message}</td></tr>"

        if f.status in (CheckStatus.FAIL, CheckStatus.WARN) and f.recommendation:
            html += f"""<tr class='detail'><td colspan='4'>
              <details><summary>How to resolve ({f.fix_type.value}, ~{f.effort_minutes} min)</summary>
              <div class='fix-detail'>
                <p><strong>Recommendation:</strong> {f.recommendation}</p>
                {'<p><strong>Affected resources:</strong> ' + ', '.join(f.resource_ids) + '</p>' if f.resource_ids else ''}
                {'<p><strong>Fix first:</strong> ' + ', '.join(f.depends_on) + '</p>' if f.depends_on else ''}
                {_render_references(f.check_id)}
              </div></details>
            </td></tr>"""
    html += "</tbody></table>"
    return html


def _get_weight(pillar_id: str, workload_type: str) -> float:
    from .models import DEFAULT_WEIGHTS
    weights = DEFAULT_WEIGHTS.get(workload_type, DEFAULT_WEIGHTS["general"])
    return weights.get(pillar_id, 0.15)


def _score_color(score: int) -> str:
    if score >= 85:
        return "#16a34a"
    if score >= 70:
        return "#22c55e"
    if score >= 50:
        return "#d97706"
    return "#dc2626"


def _get_css() -> str:
    return """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; font-size: 15px; line-height: 1.7; color: #1f2937; max-width: 1360px; margin: 0 auto; padding: 24px; background: #f9fafb; }
    header { text-align: center; margin-bottom: 16px; padding: 28px 24px; background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }

    /* --- Out-of-scope note, stated once --- */
    .note-banner { display: flex; align-items: baseline; gap: 10px; margin-bottom: 24px; padding: 12px 18px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px; color: #1e40af; font-size: 0.88em; }
    .note-label { flex: 0 0 auto; padding: 1px 9px; border-radius: 4px; background: #1d4ed8; color: white; font-size: 0.82em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }

    /* --- Two-column layout with a sticky in-page nav --- */
    .layout { display: flex; gap: 28px; align-items: flex-start; }
    main { flex: 1 1 auto; min-width: 0; }
    .toc { position: sticky; top: 24px; flex: 0 0 232px; padding: 20px 18px; background: white; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); font-size: 0.87em; max-height: calc(100vh - 48px); overflow-y: auto; }
    .toc-title { margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #e5e7eb; color: #6b7280; font-size: 0.85em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
    .toc ul { list-style: none; margin: 0; padding: 0; }
    .toc li { margin-bottom: 2px; }
    .toc a { display: block; padding: 5px 9px; border-radius: 6px; color: #374151; text-decoration: none; border-left: 2px solid transparent; }
    .toc a:hover { background: #f3f4f6; color: #111827; border-left-color: #1d4ed8; }
    .toc ul.nav-sub { margin: 2px 0 6px 10px; }
    .toc ul.nav-sub li { display: flex; align-items: center; gap: 6px; }
    .toc ul.nav-sub a { flex: 1 1 auto; padding: 3px 8px; color: #6b7280; font-size: 0.94em; }
    .nav-score { flex: 0 0 auto; min-width: 26px; padding: 1px 5px; border-radius: 4px; font-size: 0.86em; font-weight: 700; text-align: center; }
    .nav-score.ok { background: #dcfce7; color: #166534; }
    .nav-score.fair { background: #ecfdf5; color: #15803d; }
    .nav-score.warn { background: #fef3c7; color: #92400e; }
    .nav-score.bad { background: #fee2e2; color: #b91c1c; }
    .nav-score-badge { padding: 10px; border-radius: 8px; color: white; font-size: 1.5em; font-weight: 700; text-align: center; line-height: 1.2; }
    .nav-score-badge span { font-size: 0.5em; font-weight: 500; opacity: 0.85; }
    .nav-score-badge.unscored { background: #6b7280; font-size: 0.95em; font-weight: 600; padding: 12px 10px; }
    .nav-counts { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; margin-bottom: 4px; }
    .nav-count { flex: 1 1 auto; padding: 3px 7px; border-radius: 5px; font-size: 0.82em; font-weight: 600; text-align: center; white-space: nowrap; }
    .nav-count.critical { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
    .nav-count.high { background: #fffbeb; color: #d97706; border: 1px solid #fed7aa; }
    section[id], details.pillar[id] { scroll-margin-top: 20px; }
    @media (max-width: 1024px) {
      .layout { flex-direction: column; }
      .toc { position: static; flex: 1 1 auto; width: 100%; max-height: none; }
      .toc ul { display: flex; flex-wrap: wrap; gap: 4px; }
      .toc ul.nav-sub { display: none; }
    }
    @media print { .toc { display: none; } .layout { display: block; } }
    h1 { font-size: 1.6em; font-weight: 700; color: #111827; margin-bottom: 10px; }
    h2 { font-size: 1.25em; font-weight: 600; color: #111827; margin-bottom: 14px; }
    .scope-badge { display: inline-block; padding: 6px 16px; border-radius: 20px; background: #eef2ff; color: #3730a3; border: 1px solid #c7d2fe; font-size: 0.82em; font-weight: 600; margin-bottom: 10px; letter-spacing: 0.01em; }
    .meta { color: #6b7280; font-size: 0.87em; margin-bottom: 16px; }
    .meta code { background: #f3f4f6; color: #374151; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
    .score-badge { display: inline-block; padding: 12px 24px; border-radius: 10px; color: white; font-size: 1.6em; font-weight: 700; margin-top: 6px; }
    .section-intro { color: #6b7280; font-size: 0.9em; margin-bottom: 14px; }
    .status-section { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    .status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
    .status-card { padding: 18px; border-radius: 10px; text-align: center; }
    .status-count { font-size: 2em; font-weight: 700; }
    .status-label { font-size: 0.9em; font-weight: 600; margin-top: 4px; }
    .status-desc { font-size: 0.78em; color: #6b7280; margin-top: 6px; }
    .critical-card { background: #fef2f2; border: 1px solid #fecaca; } .critical-card .status-count { color: #dc2626; } .critical-card .status-label { color: #991b1b; }
    .high-card { background: #fffbeb; border: 1px solid #fed7aa; } .high-card .status-count { color: #d97706; } .high-card .status-label { color: #92400e; }
    .medium-card { background: #fefce8; border: 1px solid #fef08a; } .medium-card .status-count { color: #ca8a04; } .medium-card .status-label { color: #854d0e; }
    .low-card, .ok-card { background: #f0fdf4; border: 1px solid #bbf7d0; } .low-card .status-count, .ok-card .status-count { color: #16a34a; } .low-card .status-label, .ok-card .status-label { color: #166534; }
    #effort-summary { background: white; border-radius: 12px; padding: 16px 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    section, details#methodology { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    table { width: 100%; border-collapse: separate; border-spacing: 0; margin: 10px 0; font-size: 0.9em; }
    th { padding: 10px 12px; text-align: left; background: #f9fafb; font-weight: 600; color: #374151; border-bottom: 2px solid #e5e7eb; }
    td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; color: #374151; }
    .priority-table tr.critical-row td { border-left: 3px solid #dc2626; } .priority-table tr.high-row td { border-left: 3px solid #d97706; } .priority-table tr.medium-row td { border-left: 3px solid #ca8a04; } .priority-table tr.low-row td { border-left: 3px solid #16a34a; }
    .finding-msg { font-size: 0.85em; color: #6b7280; }
    .severity-badge { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.75em; font-weight: 700; text-transform: uppercase; }
    .severity-badge.critical { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; } .severity-badge.high { background: #fffbeb; color: #d97706; border: 1px solid #fed7aa; } .severity-badge.medium { background: #fefce8; color: #ca8a04; border: 1px solid #fef08a; } .severity-badge.low { background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }
    .findings tr.fail td { background: #fef2f2; } .findings tr.warn td { background: #fffbeb; } .findings tr.pass td { background: #f0fdf4; } .findings tr.detail td { padding: 4px 12px; background: #fafafa; }
    .critical { color: #dc2626; font-weight: 600; } .high { color: #d97706; font-weight: 600; } .medium { color: #ca8a04; font-weight: 500; } .low { color: #16a34a; font-weight: 500; }
    details.pillar { margin-bottom: 10px; border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; }
    details.pillar summary { padding: 14px 18px; cursor: pointer; font-size: 1.02em; font-weight: 500; color: #1f2937; background: #f9fafb; }
    details.pillar[open] summary { border-bottom: 1px solid #e5e7eb; background: #f3f4f6; }
    .pillar-content { padding: 16px; }
    .fix-detail { padding: 12px; background: #f9fafb; border-radius: 6px; margin-top: 8px; border-left: 3px solid #3b82f6; font-size: 0.9em; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.75em; font-weight: 600; text-transform: uppercase; }
    .badge.config { background: #dcfce7; color: #166534; } .badge.deploy { background: #dbeafe; color: #1e40af; } .badge.architecture { background: #fef3c7; color: #92400e; } .badge.code { background: #fce7f3; color: #9d174d; }
    .all-clear { color: #16a34a; font-weight: 600; font-size: 1.1em; text-align: center; padding: 20px; }
    footer { text-align: center; color: #9ca3af; font-size: 0.8em; padding: 20px; margin-top: 12px; }
    .score-badge.unscored { background: #6b7280; font-size: 1.05em; font-weight: 600; }
    .status-cell { text-align: center; font-size: 0.85em; font-weight: 600; color: #4b5563; }
    .verdict-badge { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.72em; font-weight: 700; }
    .verdict-badge.design-not-implemented { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
    .verdict-badge.missing-in-both { background: #fffbeb; color: #d97706; border: 1px solid #fed7aa; }
    .verdict-badge.absent-in-account { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
    tr.verdict-absent-in-account td { border-left: 3px solid #f59e0b; }
    .verdict-badge.undocumented-in-design { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
    .verdict-badge.aligned { background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }
    .verdict-badge.inconclusive { background: #f3f4f6; color: #6b7280; border: 1px solid #e5e7eb; }
    tr.verdict-design-not-implemented td { border-left: 3px solid #dc2626; background: #fffafa; }
    tr.verdict-missing-in-both td { border-left: 3px solid #d97706; }
    .drift-callout { padding: 12px 14px; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; color: #991b1b; margin-bottom: 12px; font-size: 0.92em; }
    .design-summary { padding: 12px 14px; background: #f9fafb; border-left: 3px solid #2563eb; border-radius: 6px; margin-bottom: 14px; font-size: 0.92em; }
    .scope-note { margin-top: 12px; padding: 10px 14px; background: #f9fafb; border: 1px dashed #d1d5db; border-radius: 6px; color: #6b7280; font-size: 0.85em; }
    .refs { margin-top: 10px; padding-top: 8px; border-top: 1px solid #e5e7eb; font-size: 0.88em; }
    .refs strong { color: #374151; }
    .refs ul { margin: 6px 0 0 18px; }
    .refs li { margin-bottom: 3px; }
    .refs a { color: #1d4ed8; text-decoration: none; }
    .refs a:hover { text-decoration: underline; }
    details.legend { margin-top: 12px; }
    details.legend summary { cursor: pointer; color: #4b5563; font-size: 0.88em; font-weight: 500; }
    details.legend ul { margin: 10px 0 0 20px; color: #6b7280; font-size: 0.85em; }
    details.legend li { margin-bottom: 4px; }
    @media (max-width: 768px) { .status-grid { grid-template-columns: repeat(2, 1fr); } }
    """
