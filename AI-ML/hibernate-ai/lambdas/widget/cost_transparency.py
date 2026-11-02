# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tool cost transparency — show Hibernate AI's own running costs vs savings."""
import boto3
import json
import os
from datetime import datetime, timedelta

STATE_TABLE = os.environ.get("STATE_TABLE", "HibernateAIState")

# Hibernate AI's own cost components (us-east-1 pricing)
LAMBDA_COST_PER_GB_SEC = 0.0000166667
LAMBDA_COST_PER_REQUEST = 0.0000002
DYNAMODB_COST_PER_WCU = 0.00000125  # on-demand
DYNAMODB_COST_PER_RCU = 0.00000025
SNAPSHOT_COST_PER_GB_MONTH = 0.05  # EBS snapshots
RDS_SNAPSHOT_COST_PER_GB_MONTH = 0.095  # RDS snapshots
EVENTBRIDGE_COST_PER_INVOCATION = 0.000001


def calculate_tool_costs():
    """Calculate Hibernate AI's own monthly cost estimate."""
    # Estimate based on typical usage
    state = _get_usage_stats()

    # Lambda costs (widget invocations + scheduled actions)
    lambda_invocations = state.get("invocations_month", 0) or _estimate_lambda_invocations()
    lambda_duration_sec = lambda_invocations * 5  # avg 5s per invocation
    lambda_memory_gb = 0.5  # 512MB
    lambda_cost = (lambda_invocations * LAMBDA_COST_PER_REQUEST) + (lambda_duration_sec * lambda_memory_gb * LAMBDA_COST_PER_GB_SEC)

    # DynamoDB costs
    ddb_writes = state.get("ddb_writes_month", 0) or (lambda_invocations * 2)
    ddb_reads = state.get("ddb_reads_month", 0) or (lambda_invocations * 5)
    ddb_cost = (ddb_writes * DYNAMODB_COST_PER_WCU) + (ddb_reads * DYNAMODB_COST_PER_RCU)

    # Snapshot storage costs (actual from saved state)
    snapshot_gb = _get_total_snapshot_storage()
    snapshot_cost = snapshot_gb * SNAPSHOT_COST_PER_GB_MONTH

    # EventBridge costs
    schedules = state.get("active_schedules", 0) or _count_active_schedules()
    eventbridge_invocations = schedules * 2 * 22  # 2 per day (save+restore) * 22 workdays
    eventbridge_cost = eventbridge_invocations * EVENTBRIDGE_COST_PER_INVOCATION

    # Bedrock costs (Claude 3 Haiku: $0.25/1M input, $1.25/1M output)
    bedrock_calls = state.get("bedrock_calls_month", 0) or (lambda_invocations // 10)
    bedrock_cost = bedrock_calls * 0.003  # ~3000 tokens avg per call

    total_tool_cost = lambda_cost + ddb_cost + snapshot_cost + eventbridge_cost + bedrock_cost
    return {
        "total_monthly": round(total_tool_cost, 2),
        "breakdown": {
            "lambda": round(lambda_cost, 4),
            "dynamodb": round(ddb_cost, 4),
            "snapshots": round(snapshot_cost, 2),
            "eventbridge": round(eventbridge_cost, 4),
            "bedrock": round(bedrock_cost, 3),
        },
        "snapshot_storage_gb": round(snapshot_gb, 1),
        "active_schedules": schedules,
    }


def calculate_savings():
    """Calculate total savings from hibernated resources."""
    table = boto3.resource("dynamodb").Table(STATE_TABLE)
    r = table.get_item(Key={"project_name": "__savings_log__"})
    log = r.get("Item", {}).get("state", {"total_saved": 0, "events": []})
    return log


def log_savings(project_name, hourly_cost, hours_saved):
    """Log a savings event when resources are hibernated."""
    table = boto3.resource("dynamodb").Table(STATE_TABLE)
    try:
        r = table.get_item(Key={"project_name": "__savings_log__"})
        log = r.get("Item", {}).get("state", {"total_saved": 0, "events": []})
    except Exception:
        log = {"total_saved": 0, "events": []}

    saved = hourly_cost * hours_saved
    log["total_saved"] = round(float(log["total_saved"]) + saved, 2)
    log["events"].append({"project": project_name, "saved": round(saved, 2), "date": datetime.utcnow().isoformat()[:10]})
    log["events"] = log["events"][-100:]  # keep last 100
    # DynamoDB's boto3 resource rejects Python float; store numbers as Decimal.
    from decimal import Decimal
    item = json.loads(json.dumps({"project_name": "__savings_log__", "state": log}, default=str),
                      parse_float=Decimal)
    table.put_item(Item=item)


def _get_total_snapshot_storage():
    """Sum up all HibernateAI snapshot sizes."""
    ec2 = boto3.client("ec2")
    total_gb = 0
    try:
        for page in ec2.get_paginator("describe_snapshots").paginate(Filters=[{"Name": "tag:HibernateAI", "Values": ["true"]}], OwnerIds=["self"]):
            for snap in page["Snapshots"]:
                total_gb += snap.get("VolumeSize", 0)
    except Exception:
        pass
    return total_gb


def _count_active_schedules():
    """Count active EventBridge rules for Hibernate AI."""
    events = boto3.client("events")
    try:
        r = events.list_rules(NamePrefix="HibernateAI-")
        return sum(1 for rule in r.get("Rules", []) if rule["State"] == "ENABLED") // 2  # pairs
    except Exception:
        return 0


def _estimate_lambda_invocations():
    """Estimate monthly Lambda invocations from CloudWatch."""
    cw = boto3.client("cloudwatch")
    try:
        r = cw.get_metric_statistics(
            Namespace="AWS/Lambda", MetricName="Invocations",
            Dimensions=[{"Name": "FunctionName", "Value": "Hibernate-AI-Widget"}],
            StartTime=datetime.utcnow() - timedelta(days=7), EndTime=datetime.utcnow(),
            Period=604800, Statistics=["Sum"],
        )
        weekly = r["Datapoints"][0]["Sum"] if r["Datapoints"] else 50
        return int(weekly * 4.3)
    except Exception:
        return 200  # conservative estimate


def _get_usage_stats():
    """Load usage stats from DynamoDB."""
    table = boto3.resource("dynamodb").Table(STATE_TABLE)
    try:
        r = table.get_item(Key={"project_name": "__usage_stats__"})
        return r.get("Item", {}).get("state", {})
    except Exception:
        return {}


def render_cost_transparency_html(projects):
    """Render the cost transparency panel."""
    costs = calculate_tool_costs()
    savings = calculate_savings()

    # Current resource monthly cost if left running 24/7
    total_hourly = sum(p["hourly_cost"] for p in projects.values())
    monthly_resource_cost = total_hourly * 730

    # Estimated cost AFTER full hibernation = snapshot storage only.
    # We approximate hibernated storage from the per-project resource footprint:
    #   EBS volumes -> EBS snapshot @ $0.05/GB-mo ; DB storage -> snapshot @ $0.095/GB-mo.
    # Falls back to a small default when sizes aren't in the project payload.
    hibernated_storage_cost = 0.0
    for p in projects.values():
        for v in p.get("ebs", []):
            hibernated_storage_cost += v.get("size", 8) * SNAPSHOT_COST_PER_GB_MONTH
        # Aurora/RDS: snapshots track actual DB storage; test DBs are tiny, use a modest estimate
        for _ in p.get("aurora", []):
            hibernated_storage_cost += 1 * RDS_SNAPSHOT_COST_PER_GB_MONTH  # ~1GB nominal
        for _ in p.get("rds_standalone", []):
            hibernated_storage_cost += 20 * RDS_SNAPSHOT_COST_PER_GB_MONTH
    hibernated_cost = round(hibernated_storage_cost + costs["total_monthly"], 2)

    # Net savings if everything is hibernated 24/7 (honest: subtracts the snapshot storage
    # you keep paying + the tool's own cost). This replaces the old 16h/day heuristic that
    # ignored snapshot cost entirely.
    potential_monthly_savings = max(monthly_resource_cost - hibernated_cost, 0)

    # Scheduled scenario: Mon-Fri 9AM-6PM (≈195 on-hrs/mo). Off-hours pay only snapshot storage.
    _on_frac = (9 * (52 * 5 / 12)) / 730.0
    scheduled_cost = round(monthly_resource_cost * _on_frac + hibernated_storage_cost * (1 - _on_frac), 2)
    scheduled_savings = max(round(monthly_resource_cost - scheduled_cost, 2), 0)

    roi = (savings.get("total_saved", 0) / max(costs["total_monthly"], 0.01))

    breakdown_items = "".join(
        f'<div style="display:flex;justify-content:space-between;font-size:11px;padding:2px 0"><span style="color:#888">{k.title()}</span><span>${v:.4f}</span></div>'
        for k, v in costs["breakdown"].items()
    )

    return f"""
<div style="background:#16213e;border-radius:6px;padding:12px;margin-top:8px;border-left:3px solid #34d399">
  <div style="font-size:13px;font-weight:600;color:#34d399;margin-bottom:8px">💰 Cost Transparency</div>
  <div style="display:flex;gap:16px;margin-bottom:8px;flex-wrap:wrap">
    <div style="background:#0f2740;border-radius:6px;padding:8px 12px">
      <div style="font-size:10px;color:#888">Scheduled (Mon-Fri 9-6 ET)</div>
      <div style="font-size:16px;font-weight:700;color:#4f8ff7">${scheduled_cost:.0f}/mo</div>
      <div style="font-size:10px;color:#34d399">save ${scheduled_savings:.0f}/mo · practical</div>
    </div>
    <div style="background:#0f2b23;border-radius:6px;padding:8px 12px">
      <div style="font-size:10px;color:#888">Always-Off (24/7 hibernated)</div>
      <div style="font-size:16px;font-weight:700;color:#34d399">${hibernated_cost:.2f}/mo</div>
      <div style="font-size:10px;color:#34d399">save ${potential_monthly_savings:.0f}/mo · max</div>
    </div>
    <div style="background:#2a1f14;border-radius:6px;padding:8px 12px">
      <div style="font-size:10px;color:#888">Running 24/7 (now)</div>
      <div style="font-size:16px;font-weight:700;color:#fb923c">${monthly_resource_cost:.0f}/mo</div>
      <div style="font-size:10px;color:#666">current baseline</div>
    </div>
  </div>
  <div style="display:flex;gap:24px;margin-bottom:8px">
    <div><div style="font-size:14px;font-weight:700;color:#fb923c">${costs['total_monthly']:.2f}</div><div style="font-size:10px;color:#888">Tool Cost/mo</div></div>
    <div><div style="font-size:14px;font-weight:700;color:#a78bfa">${savings.get('total_saved', 0):.0f}</div><div style="font-size:10px;color:#888">Total Saved (lifetime)</div></div>
  </div>
  <details style="cursor:pointer"><summary style="font-size:11px;color:#888">Tool cost breakdown</summary>
    <div style="margin-top:4px;padding:6px;background:#0f0f23;border-radius:4px">{breakdown_items}
      <div style="font-size:10px;color:#888;margin-top:4px">Snapshots: {costs['snapshot_storage_gb']:.1f} GB | Schedules: {costs['active_schedules']}</div>
    </div>
  </details>
</div>"""
