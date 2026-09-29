# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Cost Optimization -- Are you spending efficiently?

Nine checks (COST-01 through COST-09), covering token tracking, model
right-sizing, prompt routing, batch inference, provisioned throughput
utilization, cost alarms, KB embedding cost, batch-eligibility, and
month-over-month spend growth. Cost/spend findings are not treated as
security-sensitive, so this pillar runs on both tracks.
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError


PILLAR_ID = "cost_optimization"
PILLAR_NAME = "Cost Optimization"


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)
    wt = config.workload_type

    result.findings.append(_check_token_tracking(scan_data))

    if wt not in ("batch-processing", "fine-tuning"):
        result.findings.append(_check_model_right_sizing(scan_data))

    if wt in ("inference-api", "customer-facing-chatbot"):
        result.findings.append(_check_prompt_routing(scan_data))

    if wt == "batch-processing":
        result.findings.append(_check_batch_inference(scan_data))

    pts = scan_data.get("bedrock", {}).get("provisioned_throughputs", [])
    if not isinstance(pts, ScanError) and pts:
        result.findings.append(_check_pt_utilization(pts))

    result.findings.append(_check_cost_alarm(scan_data))

    if wt == "rag-pipeline":
        kbs = scan_data.get("bedrock", {}).get("knowledge_bases", [])
        if not isinstance(kbs, ScanError) and kbs:
            result.findings.append(_check_kb_embedding_cost(kbs))

    # COST-08 / COST-09: batch-eligible on-demand usage + spend growth trend
    result.findings.append(_check_batch_eligible_on_demand(scan_data, wt))
    result.findings.append(_check_spend_growth_trend(scan_data))

    return result


def _check_token_tracking(scan_data: dict) -> Finding:
    logging_cfg = scan_data.get("bedrock", {}).get("invocation_logging", {})
    if isinstance(logging_cfg, ScanError):
        return Finding(
            check_id="COST-01", check_name="Token Usage Tracking",
            status=CheckStatus.ERROR, impact=2, likelihood=3,
            message="Could not verify token tracking",
        )
    cfg = logging_cfg.get("loggingConfig", {}) if isinstance(logging_cfg, dict) else {}
    has_logging = bool(cfg.get("cloudWatchConfig", {}).get("logGroupName") or
                        cfg.get("s3Config", {}).get("bucketName"))
    if has_logging:
        return Finding(
            check_id="COST-01", check_name="Token Usage Tracking",
            status=CheckStatus.PASS, impact=2, likelihood=3,
            message="Token consumption tracked via invocation logging",
        )
    return Finding(
        check_id="COST-01", check_name="Token Usage Tracking",
        status=CheckStatus.FAIL, impact=2, likelihood=3,
        message="No token tracking -- can't optimize what you can't measure",
        recommendation="Enable invocation logging to track InputTokenCount and OutputTokenCount per model",
        fix_type=FixType.CONFIG, effort_minutes=10, depends_on=["OBS-01"],
    )


def _check_model_right_sizing(scan_data: dict) -> Finding:
    alarms = scan_data.get("cloudwatch", {}).get("alarms", [])
    model_ids = set()

    if not isinstance(alarms, ScanError):
        for a in alarms:
            for dim in a.get("Dimensions", []):
                if dim.get("Name") == "ModelId":
                    model_ids.add(dim["Value"])

    if len(model_ids) >= 2:
        return Finding(
            check_id="COST-02", check_name="Model Right-Sizing",
            status=CheckStatus.PASS, impact=2, likelihood=3,
            message=f"Multiple model tiers in use ({len(model_ids)}) -- cost-appropriate selection likely",
        )
    return Finding(
        check_id="COST-02", check_name="Model Right-Sizing",
        status=CheckStatus.WARN, impact=2, likelihood=3,
        message="Single model for all tasks -- likely overpaying for simple requests",
        recommendation="Use smaller/cheaper models (Haiku, Nova Lite) for simple tasks, reserving larger models for complex reasoning",
        fix_type=FixType.CODE, effort_minutes=120,
    )


def _check_prompt_routing(scan_data: dict) -> Finding:
    profiles = scan_data.get("bedrock", {}).get("inference_profiles", [])
    if isinstance(profiles, ScanError):
        return Finding(
            check_id="COST-03", check_name="Prompt Routing",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message="Could not check inference profiles",
        )
    if profiles:
        return Finding(
            check_id="COST-03", check_name="Prompt Routing",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"{len(profiles)} inference profile(s) -- prompt routing may be in use",
        )
    return Finding(
        check_id="COST-03", check_name="Prompt Routing",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="No inference profiles -- manual model selection may not be cost-optimal",
        recommendation="Consider prompt routing to automatically select the most cost-effective model per request",
        fix_type=FixType.CONFIG, effort_minutes=30,
    )


def _check_batch_inference(scan_data: dict) -> Finding:
    return Finding(
        check_id="COST-04", check_name="Batch Inference for Bulk",
        status=CheckStatus.WARN, impact=2, likelihood=3,
        message="Verify bulk workloads use batch inference (up to 50% cheaper than real-time)",
        recommendation="Switch non-latency-sensitive bulk processing to Bedrock batch inference",
        fix_type=FixType.CODE, effort_minutes=120,
    )


def _check_pt_utilization(pts: list) -> Finding:
    return Finding(
        check_id="COST-05", check_name="Provisioned Throughput Utilization",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message=f"{len(pts)} provisioned throughput(s) -- verify utilization >50% (avoid paying for idle capacity)",
        recommendation="Check CloudWatch ProvisionedModelUtilization metric -- if <50%, consider reducing or removing",
        fix_type=FixType.CONFIG, effort_minutes=30,
    )


def _check_cost_alarm(scan_data: dict) -> Finding:
    alarms = scan_data.get("cloudwatch", {}).get("alarms", [])
    if isinstance(alarms, ScanError):
        return Finding(
            check_id="COST-06", check_name="Cost Alarm",
            status=CheckStatus.ERROR, impact=2, likelihood=3,
            message="Could not check alarms",
        )
    cost_alarms = [a for a in alarms if any(k in a.get("AlarmName", "").lower()
                   for k in ["cost", "spend", "budget", "billing"])]
    if cost_alarms:
        return Finding(
            check_id="COST-06", check_name="Cost Alarm",
            status=CheckStatus.PASS, impact=2, likelihood=3,
            message=f"Cost alarm configured: {cost_alarms[0].get('AlarmName', '')}",
        )
    return Finding(
        check_id="COST-06", check_name="Cost Alarm",
        status=CheckStatus.FAIL, impact=2, likelihood=3,
        message="No cost alarm -- runaway Bedrock spending won't be detected until the monthly bill",
        recommendation="Create a budget alarm or CloudWatch alarm on token consumption metrics",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_kb_embedding_cost(kbs: list) -> Finding:
    return Finding(
        check_id="COST-07", check_name="KB Embedding Cost",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message=f"{len(kbs)} Knowledge Base(s) -- verify sync frequency matches data change rate (avoid over-syncing)",
        recommendation="Set sync schedule proportional to data update frequency to avoid redundant embedding costs",
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_batch_eligible_on_demand(scan_data: dict, workload_type: str) -> Finding:
    """COST-08: flags on-demand-only accounts with no batch inference jobs
    at all (a stronger, account-wide version of COST-04, which only fires
    for the batch-processing workload type)."""
    if workload_type == "batch-processing":
        # COST-04 already covers this workload type specifically.
        return Finding(
            check_id="COST-08", check_name="Batch-Eligible Workloads On-Demand",
            status=CheckStatus.SKIPPED, impact=2, likelihood=3,
            message="Covered by COST-04 for batch-processing workloads",
        )
    jobs = scan_data.get("bedrock", {}).get("batch_inference_jobs", [])
    if isinstance(jobs, ScanError):
        return Finding(
            check_id="COST-08", check_name="Batch-Eligible Workloads On-Demand",
            status=CheckStatus.ERROR, impact=2, likelihood=3,
            message=f"Could not check batch inference jobs: {jobs.error}",
        )
    if jobs:
        return Finding(
            check_id="COST-08", check_name="Batch-Eligible Workloads On-Demand",
            status=CheckStatus.PASS, impact=2, likelihood=3,
            message=f"{len(jobs)} batch inference job(s) found -- bulk/non-real-time work is using the cheaper batch path",
        )
    return Finding(
        check_id="COST-08", check_name="Batch-Eligible Workloads On-Demand",
        status=CheckStatus.WARN, impact=2, likelihood=3,
        message="No batch inference jobs found -- any batch-eligible workloads are paying on-demand rates (up to 50% premium)",
        recommendation="Evaluate batch inference for non-real-time workloads (summarization, classification, data extraction)",
        fix_type=FixType.CODE, effort_minutes=120,
    )


def _check_spend_growth_trend(scan_data: dict) -> Finding:
    """COST-09: month-over-month Bedrock spend growth from Cost Explorer,
    flagged when growing fast without a commitment strategy in place."""
    cost_data = scan_data.get("cost", {}).get("bedrock_cost_and_usage")
    if isinstance(cost_data, ScanError):
        return Finding(
            check_id="COST-09", check_name="Bedrock Spend Growth Trend",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not retrieve cost data: {cost_data.error}",
            recommendation="Verify ce:GetCostAndUsage permission. Cost Explorer must be enabled on the payer account.",
        )
    if not cost_data:
        return Finding(
            check_id="COST-09", check_name="Bedrock Spend Growth Trend",
            status=CheckStatus.WARN, impact=2, likelihood=2,
            message="No cost data available to assess spend trend",
        )
    monthly_costs = []
    for result in cost_data.get("ResultsByTime", []):
        try:
            monthly_costs.append(float(result["Total"]["UnblendedCost"]["Amount"]))
        except (KeyError, ValueError):
            continue

    if len(monthly_costs) >= 2 and monthly_costs[-2] > 0:
        growth = (monthly_costs[-1] - monthly_costs[-2]) / monthly_costs[-2] * 100
        if growth > 20:
            return Finding(
                check_id="COST-09", check_name="Bedrock Spend Growth Trend",
                status=CheckStatus.WARN, impact=2, likelihood=2,
                message=f"Bedrock spend growing {growth:.0f}% month-over-month without an apparent commitment strategy",
                recommendation="Evaluate Provisioned Throughput or Savings Plans for predictable workloads as spend scales",
                fix_type=FixType.CONFIG, effort_minutes=30,
            )

    if monthly_costs:
        total_spend = sum(monthly_costs)
        return Finding(
            check_id="COST-09", check_name="Bedrock Spend Growth Trend",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"90-day Bedrock spend: ${total_spend:.2f} -- growth rate within a manageable range",
        )
    return Finding(
        check_id="COST-09", check_name="Bedrock Spend Growth Trend",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message="No monthly cost datapoints returned -- likely no Bedrock usage yet in this billing period",
    )
