# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Observability -- Can you see what's happening with your Bedrock workloads?

Ported unchanged from the Vipul/Ankur `bedrock-readiness-platform`
(enhanced-version). Not security-sensitive -- runs on both tracks.

Checks: OBS-01 through OBS-13
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError


PILLAR_ID = "observability"
PILLAR_NAME = "Observability"

APPLICABILITY = {
    "OBS-01": ["all"],
    "OBS-02": ["all"],
    "OBS-03": ["all"],
    "OBS-04": ["inference-api", "customer-facing-chatbot", "rag-pipeline", "multi-agent", "fine-tuning"],
    "OBS-05": ["all"],
    "OBS-06": ["all"],
    "OBS-07": ["inference-api", "customer-facing-chatbot", "rag-pipeline", "multi-agent"],
    "OBS-08": ["all"],
    "OBS-09": ["multi-agent"],
    "OBS-10": ["multi-agent"],
    "OBS-11": ["rag-pipeline"],
    "OBS-12": ["inference-api", "customer-facing-chatbot", "rag-pipeline", "multi-agent", "fine-tuning"],
    "OBS-13": ["multi-agent"],
}


def _is_applicable(check_id: str, workload_type: str) -> bool:
    types = APPLICABILITY.get(check_id, [])
    return "all" in types or workload_type in types


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    """Run all observability checks and return scored findings."""
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)
    wt = config.workload_type

    if _is_applicable("OBS-01", wt):
        result.findings.append(_check_invocation_logging(scan_data))
    if _is_applicable("OBS-02", wt):
        result.findings.append(_check_genai_dashboard(scan_data))
    if _is_applicable("OBS-03", wt):
        result.findings.append(_check_alarm("OBS-03", "Error", ["error", "fault", "5xx", "4xx"],
                                             impact=4, likelihood=3, scan_data=scan_data))
    if _is_applicable("OBS-04", wt):
        result.findings.append(_check_alarm("OBS-04", "Latency", ["latency", "duration", "response"],
                                             impact=3, likelihood=3, scan_data=scan_data))
    if _is_applicable("OBS-05", wt):
        result.findings.append(_check_alarm("OBS-05", "Throttle", ["throttle", "limit", "429"],
                                             impact=3, likelihood=4, scan_data=scan_data))
    if _is_applicable("OBS-06", wt):
        result.findings.append(_check_alarm("OBS-06", "Cost", ["cost", "spend", "token", "price"],
                                             impact=3, likelihood=3, scan_data=scan_data))
    if _is_applicable("OBS-07", wt):
        result.findings.append(_check_xray(scan_data))
    if _is_applicable("OBS-08", wt):
        result.findings.append(_check_bedrock_log_groups(scan_data))
    if _is_applicable("OBS-09", wt):
        runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
        if not isinstance(runtimes, ScanError) and runtimes:
            result.findings.append(_check_agentcore_logs(scan_data))
    if _is_applicable("OBS-10", wt):
        runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
        if not isinstance(runtimes, ScanError) and runtimes:
            result.findings.append(_check_agent_tracing(scan_data))
    if _is_applicable("OBS-11", wt):
        kbs = scan_data.get("bedrock", {}).get("knowledge_bases", [])
        if not isinstance(kbs, ScanError) and kbs:
            result.findings.append(_check_kb_sync_monitoring(scan_data))
    if _is_applicable("OBS-12", wt):
        result.findings.append(_check_evaluation_jobs(scan_data))
    if _is_applicable("OBS-13", wt):
        runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
        if not isinstance(runtimes, ScanError) and runtimes:
            result.findings.append(_check_agentcore_evaluations(scan_data))

    return result


def _check_invocation_logging(scan_data: dict) -> Finding:
    logging_cfg = scan_data["bedrock"]["invocation_logging"]
    if isinstance(logging_cfg, ScanError):
        return Finding(
            check_id="OBS-01", check_name="Model Invocation Logging",
            status=CheckStatus.ERROR, impact=4, likelihood=4,
            message=f"Could not check: {logging_cfg.error}",
            recommendation="Grant bedrock:GetModelInvocationLoggingConfiguration permission",
        )
    cfg = logging_cfg.get("loggingConfig", {})
    has_cw = bool(cfg.get("cloudWatchConfig", {}).get("logGroupName"))
    has_s3 = bool(cfg.get("s3Config", {}).get("bucketName"))

    if has_cw or has_s3:
        destinations = []
        if has_cw:
            destinations.append("CloudWatch")
        if has_s3:
            destinations.append("S3")
        return Finding(
            check_id="OBS-01", check_name="Model Invocation Logging",
            status=CheckStatus.PASS, impact=4, likelihood=4,
            message=f"Enabled -- destinations: {', '.join(destinations)}",
        )
    return Finding(
        check_id="OBS-01", check_name="Model Invocation Logging",
        status=CheckStatus.FAIL, impact=4, likelihood=4,
        message="Model invocation logging is not configured",
        recommendation="Enable model invocation logging to CloudWatch Logs and/or S3 for cost tracking, debugging, and audit",
        fix_type=FixType.CONFIG, effort_minutes=10,
    )


def _check_genai_dashboard(scan_data: dict) -> Finding:
    dashboards = scan_data["cloudwatch"]["dashboards"]
    if isinstance(dashboards, ScanError):
        return Finding(
            check_id="OBS-02", check_name="GenAI Dashboard",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message=f"Could not check: {dashboards.error}",
        )
    keywords = ["bedrock", "genai", "llm", "ai", "foundation", "model"]
    matches = [d for d in dashboards if any(k in d.get("DashboardName", "").lower() for k in keywords)]
    if matches:
        names = [d["DashboardName"] for d in matches[:3]]
        return Finding(
            check_id="OBS-02", check_name="GenAI Dashboard",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"Found {len(matches)} GenAI dashboard(s): {', '.join(names)}",
            resource_ids=names,
        )
    return Finding(
        check_id="OBS-02", check_name="GenAI Dashboard",
        status=CheckStatus.FAIL, impact=3, likelihood=3,
        message="No GenAI-specific CloudWatch dashboard found",
        recommendation="Create a dashboard for model invocations, latency, token usage, and cost metrics",
        fix_type=FixType.CONFIG, effort_minutes=20,
    )


def _check_alarm(check_id: str, category: str, keywords: list,
                  impact: int, likelihood: int, scan_data: dict) -> Finding:
    alarms = scan_data["cloudwatch"]["alarms"]
    if isinstance(alarms, ScanError):
        return Finding(
            check_id=check_id, check_name=f"Alarm: {category}",
            status=CheckStatus.ERROR, impact=impact, likelihood=likelihood,
            message=f"Could not check: {alarms.error}",
        )
    bedrock_alarms = [a for a in alarms if
                       "bedrock" in a.get("Namespace", "").lower() or
                       "bedrock" in a.get("AlarmName", "").lower() or
                       "genai" in a.get("AlarmName", "").lower()]

    found = False
    matched_alarm = None
    for a in bedrock_alarms:
        name_lower = (a.get("AlarmName", "") + a.get("MetricName", "")).lower()
        if any(k in name_lower for k in keywords):
            found = True
            matched_alarm = a.get("AlarmName", "")
            break

    if found:
        return Finding(
            check_id=check_id, check_name=f"Alarm: {category}",
            status=CheckStatus.PASS, impact=impact, likelihood=likelihood,
            message=f"{category} alarm configured: {matched_alarm}",
            resource_ids=[matched_alarm] if matched_alarm else [],
        )
    return Finding(
        check_id=check_id, check_name=f"Alarm: {category}",
        status=CheckStatus.FAIL, impact=impact, likelihood=likelihood,
        message=f"No {category.lower()} alarm for Bedrock workloads",
        recommendation=f"Create a CloudWatch alarm for Bedrock {category.lower()} monitoring",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_xray(scan_data: dict) -> Finding:
    xray = scan_data["xray"]["trace_destination"]
    if isinstance(xray, ScanError):
        return Finding(
            check_id="OBS-07", check_name="Transaction Search (X-Ray)",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not check: {xray.error}",
        )
    if isinstance(xray, dict) and xray.get("Destination") == "CloudWatchLogs":
        return Finding(
            check_id="OBS-07", check_name="Transaction Search (X-Ray)",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message="CloudWatch Transaction Search is active (traces route to CloudWatch Logs)",
        )
    return Finding(
        check_id="OBS-07", check_name="Transaction Search (X-Ray)",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="Transaction Search not enabled or not routing to CloudWatch Logs",
        recommendation="Enable Transaction Search for end-to-end distributed tracing of Bedrock calls",
        fix_type=FixType.CONFIG, effort_minutes=10,
    )


def _check_bedrock_log_groups(scan_data: dict) -> Finding:
    logs = scan_data["cloudwatch"]["bedrock_log_groups"]
    if isinstance(logs, ScanError):
        return Finding(
            check_id="OBS-08", check_name="Bedrock Log Groups",
            status=CheckStatus.ERROR, impact=2, likelihood=4,
            message=f"Could not check: {logs.error}",
        )
    if logs:
        names = [lg.get("logGroupName", "") for lg in logs[:5]]
        return Finding(
            check_id="OBS-08", check_name="Bedrock Log Groups",
            status=CheckStatus.PASS, impact=2, likelihood=4,
            message=f"Found {len(logs)} Bedrock log group(s)",
            resource_ids=names,
        )
    return Finding(
        check_id="OBS-08", check_name="Bedrock Log Groups",
        status=CheckStatus.FAIL, impact=2, likelihood=4,
        message="No /aws/bedrock log groups found",
        recommendation="Enable model invocation logging -- this creates the necessary log groups",
        fix_type=FixType.CONFIG, effort_minutes=5, depends_on=["OBS-01"],
    )


def _check_agentcore_logs(scan_data: dict) -> Finding:
    logs = scan_data["cloudwatch"]["agentcore_log_groups"]
    if isinstance(logs, ScanError):
        return Finding(
            check_id="OBS-09", check_name="AgentCore Observability Logs",
            status=CheckStatus.ERROR, impact=3, likelihood=4,
            message=f"Could not check: {logs.error}",
        )
    if logs:
        return Finding(
            check_id="OBS-09", check_name="AgentCore Observability Logs",
            status=CheckStatus.PASS, impact=3, likelihood=4,
            message=f"Found {len(logs)} AgentCore observability log group(s)",
            resource_ids=[lg.get("logGroupName", "") for lg in logs[:5]],
        )
    return Finding(
        check_id="OBS-09", check_name="AgentCore Observability Logs",
        status=CheckStatus.FAIL, impact=3, likelihood=4,
        message="AgentCore runtimes deployed but no observability log groups found",
        recommendation="Enable AgentCore Observability for session traces, span metrics, and token usage",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_agent_tracing(scan_data: dict) -> Finding:
    logs = scan_data["cloudwatch"].get("agentcore_log_groups", [])
    xray = scan_data["xray"].get("trace_destination", {})

    has_logs = isinstance(logs, list) and len(logs) > 0
    has_xray = isinstance(xray, dict) and xray.get("Destination") == "CloudWatchLogs"

    if has_logs or has_xray:
        return Finding(
            check_id="OBS-10", check_name="Agent Session Tracing",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message="Agent sessions are being traced via logs and/or X-Ray",
        )
    return Finding(
        check_id="OBS-10", check_name="Agent Session Tracing",
        status=CheckStatus.FAIL, impact=3, likelihood=3,
        message="No agent session tracing detected -- can't debug multi-step agent failures",
        recommendation="Enable AgentCore Observability or instrument agents with OTEL SDK for X-Ray traces",
        fix_type=FixType.CONFIG, effort_minutes=30,
    )


def _check_kb_sync_monitoring(scan_data: dict) -> Finding:
    alarms = scan_data["cloudwatch"]["alarms"]
    if isinstance(alarms, ScanError):
        return Finding(
            check_id="OBS-11", check_name="KB Sync Monitoring",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message="Could not check alarms",
        )
    kb_alarms = [a for a in alarms if any(k in a.get("AlarmName", "").lower()
                 for k in ["knowledge", "kb", "sync", "ingestion", "rag"])]
    if kb_alarms:
        return Finding(
            check_id="OBS-11", check_name="KB Sync Monitoring",
            status=CheckStatus.PASS, impact=3, likelihood=2,
            message=f"Knowledge Base sync monitoring alarm found: {kb_alarms[0].get('AlarmName', '')}",
        )
    return Finding(
        check_id="OBS-11", check_name="KB Sync Monitoring",
        status=CheckStatus.FAIL, impact=3, likelihood=2,
        message="No alarm monitoring Knowledge Base sync failures -- stale data could be served without alerting",
        recommendation="Create an alarm on KB ingestion errors to detect sync failures",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_evaluation_jobs(scan_data: dict) -> Finding:
    jobs = scan_data["bedrock"].get("evaluation_jobs", [])
    if isinstance(jobs, ScanError):
        return Finding(
            check_id="OBS-12", check_name="Model Evaluation Runs",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not check: {jobs.error}",
        )
    if jobs:
        return Finding(
            check_id="OBS-12", check_name="Model Evaluation Runs",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"{len(jobs)} evaluation job(s) found -- model quality is being measured",
        )
    return Finding(
        check_id="OBS-12", check_name="Model Evaluation Runs",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="No model evaluation jobs found -- quality changes from model updates will go unnoticed",
        recommendation="Run Bedrock Evaluations to measure model quality before/after changes",
        fix_type=FixType.CONFIG, effort_minutes=60,
    )


def _check_agentcore_evaluations(scan_data: dict) -> Finding:
    return Finding(
        check_id="OBS-13", check_name="AgentCore Evaluations",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="AgentCore Evaluations check -- verify agent quality is measured before deployments",
        recommendation="Use AgentCore Evaluations to test agent behavior before production changes",
        fix_type=FixType.CONFIG, effort_minutes=120,
    )
