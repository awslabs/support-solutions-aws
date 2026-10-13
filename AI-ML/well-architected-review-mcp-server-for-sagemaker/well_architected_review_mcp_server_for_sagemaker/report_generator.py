# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HTML report generator for Well-Architected validation results."""

import datetime
import html
import os


PILLAR_ORDER = [
    'Security',
    'Reliability',
    'Performance Efficiency',
    'Cost Optimization',
    'Operational Excellence',
    'Sustainability',
]

PILLAR_SHORT = {
    'Security': 'Security',
    'Reliability': 'Reliability',
    'Performance Efficiency': 'Performance',
    'Cost Optimization': 'Cost',
    'Operational Excellence': 'Ops Excellence',
    'Sustainability': 'Sustainability',
}

PILLAR_ANCHORS = {
    'Security': 'security',
    'Reliability': 'reliability',
    'Performance Efficiency': 'performance',
    'Cost Optimization': 'cost',
    'Operational Excellence': 'ops',
    'Sustainability': 'sustainability',
}

PILLAR_CHECK_COUNTS = {
    'Security': 14,
    'Reliability': 10,
    'Performance Efficiency': 6,
    'Cost Optimization': 8,
    'Operational Excellence': 8,
    'Sustainability': 5,
}

PILLAR_ICONS = {
    'Security': '🔒',
    'Reliability': '🛡️',
    'Operational Excellence': '⚙️',
    'Performance Efficiency': '⚡',
    'Cost Optimization': '💰',
    'Sustainability': '🌱',
}


def generate_html_report(
    resource_name: str,
    resource_type: str,
    findings: list[dict],
    summary: dict,
    report_path: str | None = None,
    account_id: str | None = None,
    region: str | None = None,
) -> str:
    """Generate an HTML report for single-resource validation findings.

    Args:
        resource_name: Name of the validated resource
        resource_type: Type of the resource
        findings: List of finding dictionaries
        summary: Summary dict
        report_path: Optional path to write the report.
        account_id: AWS account ID
        region: AWS region name

    Returns:
        Absolute path to the generated HTML report file
    """
    return _build_report(
        resources=[f'{resource_type}/{resource_name}'],
        findings=findings,
        report_path=report_path,
        account_id=account_id,
        region=region,
    )


def generate_batch_html_report(
    resources_validated: list[str],
    findings: list[dict],
    summary: dict,
    report_path: str | None = None,
    account_id: str | None = None,
    region: str | None = None,
) -> str:
    """Generate an HTML report for batch validation findings.

    Args:
        resources_validated: List of validated resource names
        findings: List of all finding dictionaries
        summary: Aggregated summary dict
        report_path: Optional path to write the report.
        account_id: AWS account ID
        region: AWS region name

    Returns:
        Absolute path to the generated HTML report file
    """
    return _build_report(
        resources=resources_validated,
        findings=findings,
        report_path=report_path,
        account_id=account_id,
        region=region,
    )


def _build_report(
    resources: list[str],
    findings: list[dict],
    report_path: str | None = None,
    account_id: str | None = None,
    region: str | None = None,
) -> str:
    """Build the complete HTML report.

    Args:
        resources: List of resource identifiers
        findings: List of finding dicts
        report_path: Output file path
        account_id: AWS account ID
        region: AWS region name

    Returns:
        Absolute path to the generated HTML file
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime('%B %d, %Y')
    region_str = region or 'unknown'
    account_str = account_id or 'unknown'
    res_count = len(resources)

    # Store region for console URL generation
    _report_region = region_str

    total_findings = len(findings)
    high = sum(1 for f in findings if f['severity'] == 'HIGH')
    med = sum(1 for f in findings if f['severity'] == 'MEDIUM')
    low = sum(1 for f in findings if f['severity'] == 'LOW')

    # Calculate passed checks per pillar
    by_pillar: dict[str, list[dict]] = {}
    for f in findings:
        by_pillar.setdefault(f['pillar'], []).append(f)

    total_checks = res_count * sum(PILLAR_CHECK_COUNTS.values())
    passed = total_checks - total_findings

    # Build per-resource findings
    by_resource: dict[str, list[dict]] = {}
    for f in findings:
        by_resource.setdefault(f['resource'], []).append(f)

    parts = [
        _css(),
        _nav(account_str, date_str, region_str),
        '<main>',
        _header(account_str, date_str, region_str, res_count),
        _stats_bar(res_count, total_findings, high, med, low, passed),
        _exec_summary(res_count, total_findings, passed, high, by_pillar),
        _donut_row(by_pillar, res_count),
    ]

    # Pillar sections
    for pillar in PILLAR_ORDER:
        pillar_findings = by_pillar.get(pillar, [])
        parts.append(_pillar_section(pillar, pillar_findings, res_count, region_str))

    # Priority remediation
    parts.append(_priority_section(findings, region_str))

    # Resource details
    parts.append(_resource_section(by_resource))

    parts.append('</main>\n</body>\n</html>')

    content = '\n'.join(parts)

    if report_path is None:
        report_path = 'wa-report.html'

    with open(report_path, 'w', encoding='utf-8') as fh:
        fh.write(content)

    return os.path.abspath(report_path)


def _css() -> str:
    """Generate the full HTML head with CSS."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SageMaker Well-Architected Review</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;min-height:100vh;background:#f5f6fa;color:#2d3436;font-size:14px;-webkit-font-smoothing:antialiased}
nav{width:240px;background:#1e272e;color:#fff;position:fixed;top:0;left:0;height:100vh;overflow-y:auto;z-index:10}
nav .logo{padding:24px 20px;border-bottom:1px solid #485460;font-size:14px}
nav .logo h2{font-size:18px;margin-bottom:4px}
nav .logo span{color:#a4b0be;font-size:12px}
nav ul{list-style:none;padding:12px 0}
nav li a{display:block;padding:10px 20px;color:#d2dae2;text-decoration:none;font-size:13px;border-left:3px solid transparent;transition:.2s;font-weight:400}
nav li a:hover,nav li a.active{background:#485460;border-left-color:#0984e3;color:#fff}
nav li.section-label{padding:16px 20px 6px;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#808e9b}
main{margin-left:240px;flex:1;padding:32px;max-width:1200px}
.stats-bar{display:flex;gap:16px;margin-bottom:28px;flex-wrap:wrap}
.stat-card{background:#fff;border-radius:8px;padding:18px 22px;flex:1;min-width:140px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.stat-card .num{font-size:28px;font-weight:700;letter-spacing:-.5px}
.stat-card .label{font-size:11px;color:#636e72;margin-top:2px;font-weight:500;text-transform:uppercase;letter-spacing:.3px}
.stat-card.high .num{color:#d63031}
.stat-card.med .num{color:#e17055}
.stat-card.low .num{color:#f39c12}
.stat-card.pass .num{color:#00b894}
h1{font-size:24px;margin-bottom:4px;font-weight:700;letter-spacing:-.3px}
h2{font-size:18px;margin:32px 0 16px;padding-bottom:8px;border-bottom:2px solid #dfe6e9;font-weight:600;letter-spacing:-.2px}
h3{font-size:15px;margin:20px 0 10px;font-weight:600}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;color:#fff}
.badge.high{background:#d63031}.badge.medium{background:#e17055}.badge.low{background:#f39c12}.badge.pass{background:#00b894}
table{width:100%;border-collapse:collapse;margin:12px 0 24px;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
th{background:#f8f9fa;text-align:left;padding:10px 14px;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:#636e72;border-bottom:2px solid #dfe6e9}
td{padding:10px 14px;font-size:13px;border-bottom:1px solid #f1f2f6;line-height:1.5}
tr:last-child td{border-bottom:none}
tr:hover{background:#f8f9fa}
.pillar-row{display:flex;gap:16px;flex-wrap:wrap;margin:20px 0 32px}
.pillar-card{background:#fff;border-radius:8px;padding:20px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:150px;flex:1}
.pillar-card .p-icon{font-size:32px;margin-bottom:8px}
.pillar-card .p-name{font-size:12px;font-weight:600;color:#2d3436;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.pillar-card .p-counts{display:flex;justify-content:center;gap:8px}
.pillar-card .cnt{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:50%;font-size:13px;font-weight:700}
.cnt-h{background:#ffeaea;color:#d63031}
.cnt-m{background:#fff3e0;color:#e17055}
.cnt-l{background:#fff8e1;color:#f39c12}
.cnt-p{background:#e8f8f5;color:#00b894}
.cnt-zero{opacity:.3}
details{background:#fff;border-radius:8px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
details summary{padding:14px 18px;cursor:pointer;font-weight:600;font-size:14px;list-style:none;display:flex;align-items:center;gap:8px;letter-spacing:-.1px}
details summary::before{content:'▶';font-size:10px;transition:.2s}
details[open] summary::before{transform:rotate(90deg)}
details .detail-body{padding:0 18px 16px}
.priority-list{counter-reset:pri}
.priority-item{background:#fff;border-radius:8px;padding:16px 18px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.06);display:flex;gap:14px;align-items:flex-start}
.priority-item::before{counter-increment:pri;content:counter(pri);background:#d63031;color:#fff;border-radius:50%;min-width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700}
.meta{font-size:11px;color:#636e72}
.meta li{padding:2px 0}
.meta li code{background:#f1f2f6;padding:1px 6px;border-radius:3px;font-size:11px}
.rec{background:#ffeaa7;border-radius:4px;padding:6px 10px;font-size:12px;margin-top:6px}
@media print{
nav{display:none}main{margin-left:0;padding:20px}
.stat-card,.donut-card,details,table{break-inside:avoid}
body{background:#fff}details[open]{break-inside:avoid}
}
</style>
</head>
<body>"""


def _nav(account: str, date_str: str, region: str) -> str:
    """Generate the sidebar navigation."""
    pillar_links = '\n'.join(
        f'<li><a href="#{PILLAR_ANCHORS[p]}">{p}</a></li>' for p in PILLAR_ORDER
    )
    return f"""<nav>
<div class="logo"><h2>{html.escape(account)}</h2><span>Well-Architected Review<br>{date_str} · {html.escape(region)}</span></div>
<ul>
<li class="section-label">Overview</li>
<li><a href="#exec">Executive Summary</a></li>
<li><a href="#stats">Statistics</a></li>
<li class="section-label">Pillars</li>
{pillar_links}
<li class="section-label">Actions</li>
<li><a href="#priority">Top Priority Remediation</a></li>
<li><a href="#resources">Resource Details</a></li>
</ul>
</nav>"""


def _header(account: str, date_str: str, region: str, res_count: int) -> str:
    """Generate the page header."""
    return f"""<div style="background:linear-gradient(135deg,#232f3e 0%,#37475a 100%);border-radius:10px;padding:32px 36px;margin-bottom:28px;color:#fff">
<h1 style="font-size:26px;font-weight:700;margin-bottom:8px;color:#fff">SageMaker Well-Architected Review</h1>
<div style="display:flex;gap:20px;flex-wrap:wrap;font-size:13px;opacity:.85">
<span>🏢 {html.escape(account)}</span>
<span>🌍 {html.escape(region)}</span>
<span>📅 {date_str}</span>
<span>📦 {res_count} Resources Evaluated</span>
</div>
</div>"""


def _stats_bar(res_count: int, total: int, high: int, med: int, low: int, passed: int) -> str:
    """Generate the top stats bar."""
    return f"""<div id="stats" class="stats-bar">
<div class="stat-card"><div class="num">{res_count}</div><div class="label">Total Resources</div></div>
<div class="stat-card"><div class="num">{total}</div><div class="label">Total Findings</div></div>
<div class="stat-card high"><div class="num">{high}</div><div class="label">High Severity</div></div>
<div class="stat-card med"><div class="num">{med}</div><div class="label">Medium Severity</div></div>
<div class="stat-card low"><div class="num">{low}</div><div class="label">Low Severity</div></div>
</div>"""


def _exec_summary(
    res_count: int, total_findings: int, passed: int, high: int, by_pillar: dict
) -> str:
    """Generate the executive summary paragraph."""
    total_checks = passed + total_findings
    pass_pct = int(passed / total_checks * 100) if total_checks else 0

    # Find pillars with most HIGH findings
    worst = sorted(
        by_pillar.items(),
        key=lambda x: sum(1 for f in x[1] if f['severity'] == 'HIGH'),
        reverse=True,
    )
    worst_names = [p for p, _ in worst[:2] if any(f['severity'] == 'HIGH' for f in _)]

    worst_text = ''
    if worst_names:
        worst_text = f" {' and '.join(worst_names)} {'have' if len(worst_names) > 1 else 'has'} the most critical gaps."

    # Find top HIGH check types
    high_checks: dict[str, int] = {}
    for f in [f for fs in by_pillar.values() for f in fs if f['severity'] == 'HIGH']:
        high_checks[f['check']] = high_checks.get(f['check'], 0) + 1
    top_checks = sorted(high_checks.items(), key=lambda x: x[1], reverse=True)[:3]
    check_text = ', '.join(c.replace('-', ' ').replace('no ', '').replace('no-', '') for c, _ in top_checks)

    return f"""<h2 id="exec">Executive Summary</h2>
<p style="margin-bottom:16px">This report evaluates {res_count} SageMaker resources across all six Well-Architected pillars. Of {total_checks} total checks performed, {passed} passed ({pass_pct}%), while {total_findings} produced findings requiring attention.{worst_text} Immediate action is recommended for the {high} HIGH severity findings{f', particularly around {check_text}' if check_text else ''}.</p>"""


def _donut_row(by_pillar: dict[str, list[dict]], res_count: int) -> str:
    """Generate the pillar overview cards with icon and severity count circles."""
    cards = []
    for pillar in PILLAR_ORDER:
        findings = by_pillar.get(pillar, [])
        check_count = PILLAR_CHECK_COUNTS.get(pillar, 1)
        total_for_pillar = res_count * check_count
        finding_count = len(findings)
        passed_count = max(total_for_pillar - finding_count, 0)

        h = sum(1 for f in findings if f['severity'] == 'HIGH')
        m = sum(1 for f in findings if f['severity'] == 'MEDIUM')
        lo = sum(1 for f in findings if f['severity'] == 'LOW')

        icon = PILLAR_ICONS.get(pillar, '📋')
        short = PILLAR_SHORT.get(pillar, pillar)

        h_cls = ' cnt-zero' if h == 0 else ''
        m_cls = ' cnt-zero' if m == 0 else ''
        l_cls = ' cnt-zero' if lo == 0 else ''
        p_cls = ' cnt-zero' if passed_count == 0 else ''

        cards.append(
            f'<div class="pillar-card">'
            f'<div class="p-icon">{icon}</div>'
            f'<div class="p-name">{html.escape(short)}</div>'
            f'<div class="p-counts">'
            f'<span class="cnt cnt-h{h_cls}" title="High">{h}</span>'
            f'<span class="cnt cnt-m{m_cls}" title="Medium">{m}</span>'
            f'<span class="cnt cnt-l{l_cls}" title="Low">{lo}</span>'
            f'<span class="cnt cnt-p{p_cls}" title="Passed">{passed_count}</span>'
            f'</div>'
            f'</div>'
        )

    legend = (
        '<div style="display:flex;gap:14px;font-size:11px;color:#636e72;margin-bottom:12px">'
        '<span><span class="cnt cnt-h" style="width:18px;height:18px;font-size:9px">H</span> High</span>'
        '<span><span class="cnt cnt-m" style="width:18px;height:18px;font-size:9px">M</span> Medium</span>'
        '<span><span class="cnt cnt-l" style="width:18px;height:18px;font-size:9px">L</span> Low</span>'
        '<span><span class="cnt cnt-p" style="width:18px;height:18px;font-size:9px">✓</span> Passed</span>'
        '</div>'
    )

    return f'{legend}\n<div class="pillar-row">\n{"".join(cards)}\n</div>'


def _resource_type_from_name(name: str) -> tuple[str, str]:
    """Extract resource type and clean name from a resource identifier.

    Args:
        name: Resource name, possibly prefixed with type (e.g., 'endpoint/my-ep')

    Returns:
        Tuple of (type_label, clean_name)
    """
    type_map = {
        'endpoint': ('Endpoint', 'endpoints'),
        'training_job': ('Training Job', 'jobs'),
        'notebook': ('Notebook', 'notebook-instances'),
        'model': ('Model', 'models'),
    }
    for prefix, (label, _) in type_map.items():
        if name.startswith(f'{prefix}/'):
            return label, name[len(prefix) + 1 :]
    return 'Resource', name


def _console_url(resource_name: str, region: str = 'us-east-1') -> str:
    """Build an AWS Console URL for a SageMaker resource.

    Args:
        resource_name: Resource name, possibly prefixed with type
        region: AWS region

    Returns:
        Console URL string
    """
    path_map = {
        'endpoint': 'endpoints',
        'training_job': 'jobs',
        'notebook': 'notebook-instances',
        'model': 'models',
    }
    for prefix, path in path_map.items():
        if resource_name.startswith(f'{prefix}/'):
            clean = resource_name[len(prefix) + 1 :]
            return f'https://{region}.console.aws.amazon.com/sagemaker/home?region={region}#/{path}/{clean}'
    return '#'


def _affected_resources_table(resource_names: list[str], region: str = 'us-east-1') -> str:
    """Build an HTML table of affected resources with type and console link.

    Args:
        resource_names: List of resource identifiers
        region: AWS region for console links

    Returns:
        HTML table string
    """
    rows = []
    for name in resource_names:
        type_label, clean_name = _resource_type_from_name(name)
        url = _console_url(name, region)
        rows.append(
            f'<tr>'
            f'<td><code>{html.escape(clean_name)}</code></td>'
            f'<td><span class="badge pass" style="background:#dfe6e9;color:#2d3436">{type_label}</span></td>'
            f'<td><a href="{url}" target="_blank" style="color:#0984e3;font-size:12px">Open in Console ↗</a></td>'
            f'</tr>'
        )
    return (
        f'<table style="margin-top:10px">'
        f'<thead><tr><th>Resource</th><th>Type</th><th>Console</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _pillar_section(pillar: str, findings: list[dict], res_count: int, region: str = 'us-east-1') -> str:
    """Generate a pillar section with collapsible finding details."""
    anchor = PILLAR_ANCHORS.get(pillar, pillar.lower())
    check_count = PILLAR_CHECK_COUNTS.get(pillar, 1)
    total_for_pillar = res_count * check_count
    finding_count = len(findings)
    passed = total_for_pillar - finding_count
    h = sum(1 for f in findings if f['severity'] == 'HIGH')
    m = sum(1 for f in findings if f['severity'] == 'MEDIUM')
    lo = sum(1 for f in findings if f['severity'] == 'LOW')

    summary_badges = []
    if h:
        summary_badges.append(f'<span class="badge high">{h} High</span>')
    if m:
        summary_badges.append(f'<span class="badge medium">{m} Medium</span>')
    if lo:
        summary_badges.append(f'<span class="badge low">{lo} Low</span>')
    if passed:
        summary_badges.append(f'<span class="badge pass">{passed} Passed</span>')

    # Group findings by check
    by_check: dict[str, list[dict]] = {}
    for f in findings:
        by_check.setdefault(f['check'], []).append(f)

    # Sort checks: HIGH first
    sev_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    sorted_checks = sorted(
        by_check.items(),
        key=lambda x: min(sev_order.get(f['severity'], 3) for f in x[1]),
    )

    check_details = []
    for check_id, check_findings in sorted_checks:
        sev = check_findings[0]['severity']
        sev_lower = sev.lower()
        affected_resources = sorted({f['resource'] for f in check_findings})
        detail_text = check_findings[0]['detail']
        rec_text = check_findings[0]['recommendation']
        count = len(check_findings)

        # Build a clean table of affected resources
        res_table = _affected_resources_table(affected_resources, region)

        check_details.append(
            f'<details><summary>'
            f'<span class="badge {sev_lower}">{sev}</span> '
            f'<code>{html.escape(check_id)}</code> '
            f'<span style="color:#636e72;font-weight:400;font-size:12px">({count} resource{"s" if count != 1 else ""})</span>'
            f'</summary><div class="detail-body">'
            f'<p style="margin-bottom:6px">{html.escape(detail_text)}</p>'
            f'<div class="rec">💡 {html.escape(rec_text)}</div>'
            f'{res_table}'
            f'</div></details>'
        )

    return (
        f'<h2 id="{anchor}">{html.escape(pillar)}</h2>\n'
        f'<p style="margin-bottom:12px">{" ".join(summary_badges)}</p>\n'
        f'{"".join(check_details)}'
    )


def _priority_section(findings: list[dict], region: str = 'us-east-1') -> str:
    """Generate the top priority remediation section."""
    high_findings = [f for f in findings if f['severity'] == 'HIGH']

    # Deduplicate by check — show each check once with count
    by_check: dict[str, dict] = {}
    for f in high_findings:
        check = f['check']
        if check not in by_check:
            by_check[check] = {
                'check': check,
                'pillar': f['pillar'],
                'detail': f['detail'],
                'recommendation': f['recommendation'],
                'count': 0,
                'resources': set(),
            }
        by_check[check]['count'] += 1
        by_check[check]['resources'].add(f['resource'])

    # Sort by count descending
    sorted_items = sorted(by_check.values(), key=lambda x: x['count'], reverse=True)[:10]

    items = []
    for item in sorted_items:
        res_sorted = sorted(item['resources'])
        res_table = _affected_resources_table(res_sorted, region)

        items.append(
            f'<div class="priority-item"><div>'
            f'<strong>{html.escape(item["check"])}</strong> '
            f'<span class="meta">({item["pillar"]} · {item["count"]} resources)</span>'
            f'<p style="font-size:13px;margin-top:4px">{html.escape(item["detail"])}</p>'
            f'<div class="rec">💡 {html.escape(item["recommendation"])}</div>'
            f'{res_table}'
            f'</div></div>'
        )

    if not items:
        items.append('<p style="color:#00b894;font-weight:600">No HIGH severity findings. Nice work!</p>')

    return (
        f'<h2 id="priority">Top Priority Remediation</h2>\n'
        f'<div class="priority-list">{"".join(items)}</div>'
    )


def _resource_section(by_resource: dict[str, list[dict]]) -> str:
    """Generate the resource details table."""
    sorted_res = sorted(
        by_resource.items(),
        key=lambda x: sum(1 for f in x[1] if f['severity'] == 'HIGH'),
        reverse=True,
    )

    rows = []
    for res_name, findings in sorted_res:
        h = sum(1 for f in findings if f['severity'] == 'HIGH')
        m = sum(1 for f in findings if f['severity'] == 'MEDIUM')
        lo = sum(1 for f in findings if f['severity'] == 'LOW')
        total = len(findings)

        badges = []
        if h:
            badges.append(f'<span class="badge high">{h}</span>')
        if m:
            badges.append(f'<span class="badge medium">{m}</span>')
        if lo:
            badges.append(f'<span class="badge low">{lo}</span>')

        rows.append(
            f'<tr>'
            f'<td><code>{html.escape(res_name)}</code></td>'
            f'<td>{total}</td>'
            f'<td>{" ".join(badges)}</td>'
            f'</tr>'
        )

    return (
        f'<h2 id="resources">Resource Details</h2>\n'
        f'<table><thead><tr><th>Resource</th><th>Findings</th><th>Severity Breakdown</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )
