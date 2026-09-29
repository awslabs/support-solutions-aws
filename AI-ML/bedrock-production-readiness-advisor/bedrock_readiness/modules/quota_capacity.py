# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Quota & Capacity -- Can your deployment handle production traffic?

Eleven checks (QC-01 through QC-11) covering quota sufficiency, throttle
events, growth trajectory, quota alarms, provisioned throughput,
cross-region capacity, model access, quota-increase pipeline, batch usage,
agent session limits, and Cross-Region Inference (CRIS) usage. All findings
here are operational capacity-resilience signals rather than security
findings, so this pillar runs on both tracks.
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError

CRIS_PREFIXES = ("us.", "eu.", "apac.", "global.")

PILLAR_ID = "quota_capacity"
PILLAR_NAME = "Quota & Capacity"


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)
    wt = config.workload_type
    mode = config.mode

    result.findings.append(_check_quota_sufficient(scan_data, config))
    result.findings.append(_check_throttle_events(scanner, mode))

    if mode == "production":
        result.findings.append(_check_growth_trajectory(scanner, scan_data))

    result.findings.append(_check_quota_alarm(scan_data))

    if wt in ("customer-facing-chatbot", "batch-processing", "inference-api"):
        result.findings.append(_check_provisioned_throughput(scan_data))

    if wt in ("customer-facing-chatbot", "multi-agent"):
        result.findings.append(_check_cross_region_capacity(config))

    result.findings.append(_check_model_access(scan_data))

    if mode == "pre-production":
        result.findings.append(_check_quota_increase_submitted(scan_data, config))

    if wt == "batch-processing":
        result.findings.append(_check_batch_usage(scan_data))

    if wt == "multi-agent":
        runtimes = scan_data.get("agentcore", {}).get("runtimes", [])
        if not isinstance(runtimes, ScanError) and runtimes:
            result.findings.append(_check_agent_session_limits(scan_data))

    # QC-11: Cross-Region Inference (CRIS) usage detection
    result.findings.append(_check_cris_usage(scanner))

    return result


def _check_quota_sufficient(scan_data: dict, config: Config) -> Finding:
    quotas = scan_data.get("quotas", {}).get("service_quotas", [])
    if isinstance(quotas, ScanError):
        return Finding(
            check_id="QC-01", check_name="Quota Sufficient for Production",
            status=CheckStatus.ERROR, impact=4, likelihood=4,
            message=f"Could not check quotas: {quotas.error}",
            recommendation="Grant servicequotas:ListServiceQuotas permission",
        )

    estimate = config.production_estimate
    peak_rpm = estimate.get("peak_rpm", 0)

    if not peak_rpm:
        return Finding(
            check_id="QC-01", check_name="Quota Sufficient for Production",
            status=CheckStatus.WARN, impact=4, likelihood=4,
            message="No production traffic estimate provided -- cannot verify quota sufficiency",
            recommendation="Declare production_estimate.peak_rpm in config for accurate quota assessment",
        )

    rpm_quotas = [q for q in quotas if "request" in q.get("QuotaName", "").lower()
                  or "invocation" in q.get("QuotaName", "").lower()]

    if not rpm_quotas:
        return Finding(
            check_id="QC-01", check_name="Quota Sufficient for Production",
            status=CheckStatus.WARN, impact=4, likelihood=4,
            message="Could not identify RPM quota limits -- verify manually in Service Quotas console",
        )

    min_quota = min(q.get("Value", float("inf")) for q in rpm_quotas)
    safety_margin = peak_rpm * 1.5

    if min_quota >= safety_margin:
        return Finding(
            check_id="QC-01", check_name="Quota Sufficient for Production",
            status=CheckStatus.PASS, impact=4, likelihood=4,
            message=f"Quota ({min_quota} RPM) sufficient for estimated peak ({peak_rpm} RPM) with safety margin",
        )
    elif min_quota >= peak_rpm:
        return Finding(
            check_id="QC-01", check_name="Quota Sufficient for Production",
            status=CheckStatus.WARN, impact=4, likelihood=4,
            message=f"Quota ({min_quota} RPM) covers peak ({peak_rpm} RPM) but no safety margin for spikes",
            recommendation=f"Request quota increase to {int(safety_margin)} RPM (1.5x your estimated peak)",
            fix_type=FixType.CONFIG, effort_minutes=10,
        )
    return Finding(
        check_id="QC-01", check_name="Quota Sufficient for Production",
        status=CheckStatus.FAIL, impact=4, likelihood=4,
        message=f"Quota ({min_quota} RPM) INSUFFICIENT for estimated peak ({peak_rpm} RPM) -- will throttle at launch",
        recommendation=f"Request quota increase to at least {int(safety_margin)} RPM IMMEDIATELY (takes 3-5 business days)",
        fix_type=FixType.CONFIG, effort_minutes=10,
    )


def _check_throttle_events(scanner: AccountScanner, mode: str) -> Finding:
    metrics = scanner.get_metric_statistics(
        namespace="AWS/Bedrock", metric_name="InvocationThrottles",
        dimensions=[], period=86400, hours=336,
    )
    if isinstance(metrics, ScanError):
        return Finding(
            check_id="QC-02", check_name="Throttle Events (14 days)",
            status=CheckStatus.ERROR, impact=3, likelihood=4,
            message=f"Could not check throttle metrics: {metrics.error}",
        )
    datapoints = metrics.get("Datapoints", [])
    total_throttles = sum(d.get("Sum", 0) for d in datapoints)

    if total_throttles == 0:
        return Finding(
            check_id="QC-02", check_name="Throttle Events (14 days)",
            status=CheckStatus.PASS, impact=3, likelihood=4,
            message="Zero throttle events in past 14 days",
        )
    impact = 3 if mode == "production" else 2
    likelihood = 4 if mode == "production" else 2
    return Finding(
        check_id="QC-02", check_name="Throttle Events (14 days)",
        status=CheckStatus.FAIL, impact=impact, likelihood=likelihood,
        message=f"{int(total_throttles)} throttle events in past 14 days -- requests are being dropped",
        recommendation="Request quota increase or implement rate limiting/queuing to stay within limits",
        fix_type=FixType.CONFIG, effort_minutes=10,
    )


def _check_growth_trajectory(scanner: AccountScanner, scan_data: dict) -> Finding:
    metrics = scanner.get_metric_statistics(
        namespace="AWS/Bedrock", metric_name="Invocations",
        dimensions=[], period=604800, hours=672,
    )
    if isinstance(metrics, ScanError):
        return Finding(
            check_id="QC-03", check_name="Growth Trajectory",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message="Could not check invocation growth metrics",
        )
    datapoints = sorted(metrics.get("Datapoints", []), key=lambda d: d.get("Timestamp", ""))
    if len(datapoints) < 2:
        return Finding(
            check_id="QC-03", check_name="Growth Trajectory",
            status=CheckStatus.WARN, impact=3, likelihood=3,
            message="Insufficient data for growth projection (need 2+ weeks of metrics)",
        )
    first_week = datapoints[0].get("Sum", 0)
    last_week = datapoints[-1].get("Sum", 0)
    if first_week > 0:
        growth_rate = (last_week - first_week) / first_week
        if growth_rate > 0.5:
            return Finding(
                check_id="QC-03", check_name="Growth Trajectory",
                status=CheckStatus.WARN, impact=3, likelihood=3,
                message=f"Traffic growing at {int(growth_rate*100)}% -- monitor quota headroom closely",
                recommendation="Project time-to-exhaustion and request quota increase proactively",
                fix_type=FixType.CONFIG, effort_minutes=15,
            )
    return Finding(
        check_id="QC-03", check_name="Growth Trajectory",
        status=CheckStatus.PASS, impact=3, likelihood=3,
        message="Traffic growth is manageable within current quota headroom",
    )


def _check_quota_alarm(scan_data: dict) -> Finding:
    alarms = scan_data.get("cloudwatch", {}).get("alarms", [])
    if isinstance(alarms, ScanError):
        return Finding(
            check_id="QC-04", check_name="Quota Alarm",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message="Could not check alarms",
        )
    quota_alarms = [a for a in alarms if any(k in a.get("AlarmName", "").lower()
                    for k in ["quota", "limit", "capacity", "utilization"])]
    if quota_alarms:
        return Finding(
            check_id="QC-04", check_name="Quota Alarm",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"Quota/capacity alarm found: {quota_alarms[0].get('AlarmName', '')}",
        )
    return Finding(
        check_id="QC-04", check_name="Quota Alarm",
        status=CheckStatus.FAIL, impact=3, likelihood=3,
        message="No alarm for approaching quota limits -- throttling will be the first signal",
        recommendation="Create alarms at 70% and 85% of quota limit for proactive capacity management",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_provisioned_throughput(scan_data: dict) -> Finding:
    pts = scan_data.get("bedrock", {}).get("provisioned_throughputs", [])
    if isinstance(pts, ScanError):
        return Finding(
            check_id="QC-05", check_name="Provisioned Throughput",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message=f"Could not check: {pts.error}",
        )
    if pts:
        return Finding(
            check_id="QC-05", check_name="Provisioned Throughput",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"{len(pts)} provisioned throughput(s) -- guaranteed dedicated capacity",
        )
    return Finding(
        check_id="QC-05", check_name="Provisioned Throughput",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="No provisioned throughput -- relying entirely on on-demand (shared pool, noisy neighbor risk)",
        recommendation="Consider provisioned throughput for predictable high-volume workloads",
        fix_type=FixType.DEPLOY, effort_minutes=30,
    )


def _check_cross_region_capacity(config: Config) -> Finding:
    if len(config.regions) >= 2:
        return Finding(
            check_id="QC-06", check_name="Cross-Region Capacity",
            status=CheckStatus.PASS, impact=3, likelihood=2,
            message=f"Multi-region configured ({len(config.regions)} regions) -- failover capacity available",
        )
    return Finding(
        check_id="QC-06", check_name="Cross-Region Capacity",
        status=CheckStatus.WARN, impact=3, likelihood=2,
        message="Single region -- no failover capacity if primary region is exhausted or degraded",
        recommendation="Pre-approve quota in a secondary region for capacity failover",
        fix_type=FixType.ARCHITECTURE, effort_minutes=120,
    )


def _check_model_access(scan_data: dict) -> Finding:
    models = scan_data.get("bedrock", {}).get("foundation_models", [])
    if isinstance(models, ScanError):
        return Finding(
            check_id="QC-07", check_name="Model Access Enabled",
            status=CheckStatus.ERROR, impact=4, likelihood=4,
            message=f"Could not check model access: {models.error}",
        )
    if models:
        return Finding(
            check_id="QC-07", check_name="Model Access Enabled",
            status=CheckStatus.PASS, impact=4, likelihood=4,
            message=f"{len(models)} foundation model(s) available in this account/region",
        )
    return Finding(
        check_id="QC-07", check_name="Model Access Enabled",
        status=CheckStatus.FAIL, impact=4, likelihood=4,
        message="No foundation models accessible -- model access has not been requested/granted",
        recommendation="Request access to required models in the Bedrock console (Model access page)",
        fix_type=FixType.CONFIG, effort_minutes=5,
    )


def _check_quota_increase_submitted(scan_data: dict, config: Config) -> Finding:
    history = scan_data.get("quotas", {}).get("change_history", [])
    if isinstance(history, ScanError):
        return Finding(
            check_id="QC-08", check_name="Quota Increase Requested",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message="Could not check quota change history",
        )
    recent = [r for r in history if r.get("Status") in ("PENDING", "APPROVED", "CASE_OPENED")]
    if recent:
        return Finding(
            check_id="QC-08", check_name="Quota Increase Requested",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"Quota increase request found (status: {recent[0].get('Status', 'unknown')})",
        )
    estimate = config.production_estimate.get("peak_rpm", 0)
    if estimate > 0:
        return Finding(
            check_id="QC-08", check_name="Quota Increase Requested",
            status=CheckStatus.WARN, impact=3, likelihood=3,
            message="No quota increase request submitted -- default quotas may be insufficient for production",
            recommendation="Submit quota increase request NOW -- approval takes 3-5 business days",
            fix_type=FixType.CONFIG, effort_minutes=10,
        )
    return Finding(
        check_id="QC-08", check_name="Quota Increase Requested",
        status=CheckStatus.PASS, impact=3, likelihood=3,
        message="No production estimate declared -- cannot assess if increase is needed",
    )


def _check_batch_usage(scan_data: dict) -> Finding:
    return Finding(
        check_id="QC-09", check_name="Batch Inference for Bulk",
        status=CheckStatus.WARN, impact=2, likelihood=3,
        message="Verify bulk workloads use batch inference (50% cheaper) instead of real-time invocations",
        recommendation="Use Bedrock batch inference for non-latency-sensitive bulk processing",
        fix_type=FixType.CODE, effort_minutes=120,
    )


def _check_agent_session_limits(scan_data: dict) -> Finding:
    return Finding(
        check_id="QC-10", check_name="Agent Concurrent Sessions",
        status=CheckStatus.WARN, impact=3, likelihood=3,
        message="Verify agent concurrent session limits can handle production traffic",
        recommendation="Check AgentCore session limits and request increase if needed for peak concurrent users",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_cris_usage(scanner: AccountScanner) -> Finding:
    """QC-11: Cross-region inference (CRIS) usage detection.

    CRIS is a capacity resilience signal -- distributing inference across
    regions reduces the chance a single region's quota exhaustion causes a
    full outage.
    """
    metrics = scanner.list_metrics("AWS/Bedrock", "Invocations")
    if isinstance(metrics, ScanError):
        return Finding(
            check_id="QC-11", check_name="Cross-Region Inference (CRIS) Usage",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not check CRIS usage: {metrics.error}",
        )
    cris_models = [
        m for m in metrics
        for d in m.get("Dimensions", [])
        if d.get("Name") == "ModelId" and any(d.get("Value", "").startswith(p) for p in CRIS_PREFIXES)
    ]
    if cris_models:
        return Finding(
            check_id="QC-11", check_name="Cross-Region Inference (CRIS) Usage",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"CRIS active on {len(cris_models)} model invocation metric(s) -- good for capacity resilience",
        )
    return Finding(
        check_id="QC-11", check_name="Cross-Region Inference (CRIS) Usage",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="No cross-region inference (CRIS) detected -- all inference is pinned to a single region's capacity pool",
        recommendation="Enable geographic (us./eu./apac.) or global CRIS inference profiles to distribute load across regions",
        fix_type=FixType.CONFIG, effort_minutes=20,
    )
