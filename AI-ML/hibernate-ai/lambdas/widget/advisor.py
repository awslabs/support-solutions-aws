# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AI Advisor — pulls CloudWatch metrics and uses Bedrock for analysis."""
import json
import os
import time
import boto3
from datetime import datetime, timedelta

_cw = boto3.client("cloudwatch")
_bedrock = boto3.client("bedrock-runtime")

# Review round-2 #6: metrics lookback window is configurable (was hardcoded 7).
# Short windows can skew idle detection on bursty workloads; 15 days is a safer
# default. Override via METRICS_LOOKBACK_DAYS env var. Clamped to a sane 1..90 range.
def _metrics_lookback_days():
    try:
        d = int(os.environ.get("METRICS_LOOKBACK_DAYS", "15"))
    except (TypeError, ValueError):
        d = 15
    return max(1, min(d, 90))

METRICS_LOOKBACK_DAYS = _metrics_lookback_days()

# #7 Model abstraction: primary + ordered fallback, driven by env/config, NOT a literal.
# Default preserves today's behavior (Claude 3 Haiku primary) with Sonnet as fallback.
_DEFAULT_PRIMARY = "anthropic.claude-3-haiku-20240307-v1:0"
_DEFAULT_FALLBACKS = [
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
]
# Bedrock error codes that should trigger a fallback to the next model.
# These are TRANSIENT / availability conditions where trying another model is sensible.
# Review round-2 #3: AccessDeniedException and ValidationException are deliberately
# NOT here — they indicate a genuine config/permission bug (missing IAM, malformed
# request/prompt) that every model would hit identically. Falling through would mask
# the real cause and surface a misleading "all models failed". They now return the
# actual error immediately (the else-branch in _call_with_fallback).
_FALLBACK_ERRORS = (
    "ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException",
    "ModelTimeoutException", "ResourceNotFoundException",
)


def get_model_chain():
    """Ordered list of model IDs to try. Sources, in priority order:
      1. DynamoDB __config__ keys ``bedrock_model`` (primary) + ``bedrock_model_fallbacks``
      2. env BEDROCK_MODEL_ID / BEDROCK_MODEL_FALLBACKS (comma-separated)
      3. built-in defaults (Haiku -> Sonnet).
    Duplicates removed, order preserved. Supports inference-profile / cross-region ARNs.
    """
    primary, fallbacks = None, None
    try:
        import config as _hib_config

        cfg = _hib_config.load_config()
        primary = cfg.get("bedrock_model")
        fallbacks = cfg.get("bedrock_model_fallbacks")
    except Exception:
        pass
    primary = primary or os.environ.get("BEDROCK_MODEL_ID") or _DEFAULT_PRIMARY
    if fallbacks is None:
        env_fb = os.environ.get("BEDROCK_MODEL_FALLBACKS")
        fallbacks = [m.strip() for m in env_fb.split(",") if m.strip()] if env_fb else list(_DEFAULT_FALLBACKS)
    chain, seen = [], set()
    for m in [primary] + list(fallbacks):
        if m and m not in seen:
            seen.add(m)
            chain.append(m)
    return chain


# Backward-compat shim: some callers/tests may still read MODEL_ID.
MODEL_ID = _DEFAULT_PRIMARY


def get_hourly_usage_pattern(instance_id, metric_namespace, metric_name, dimension_name, days=7):
    """Get average usage by hour-of-day (0-23 UTC) over the past N days."""
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    try:
        r = _cw.get_metric_statistics(
            Namespace=metric_namespace, MetricName=metric_name,
            Dimensions=[{"Name": dimension_name, "Value": instance_id}],
            StartTime=start, EndTime=end, Period=3600, Statistics=["Average"],
        )
        # Bucket by hour of day
        hourly = {h: [] for h in range(24)}
        for p in r["Datapoints"]:
            hourly[p["Timestamp"].hour].append(p["Average"])
        return {h: round(sum(v)/len(v), 2) if v else 0 for h, v in hourly.items()}
    except Exception:
        return {h: 0 for h in range(24)}


def get_ec2_metrics(instance_id, days=7):
    """Get CPU, network metrics for an EC2 instance."""
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    metrics = {}
    for metric_name, stat in [("CPUUtilization", "Average"), ("NetworkIn", "Sum"), ("NetworkOut", "Sum")]:
        try:
            r = _cw.get_metric_statistics(
                Namespace="AWS/EC2", MetricName=metric_name,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start, EndTime=end, Period=3600, Statistics=[stat],
            )
            points = sorted(r["Datapoints"], key=lambda x: x["Timestamp"])
            values = [p[stat] for p in points]
            metrics[metric_name] = {
                "avg": round(sum(values) / len(values), 2) if values else 0,
                "max": round(max(values), 2) if values else 0,
                "min": round(min(values), 2) if values else 0,
                "points": len(values),
            }
        except Exception:
            metrics[metric_name] = {"avg": 0, "max": 0, "min": 0, "points": 0}
    return metrics


def get_rds_metrics(db_id, days=7):
    """Get CPU, connections metrics for an RDS instance."""
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    metrics = {}
    for metric_name, stat in [("CPUUtilization", "Average"), ("DatabaseConnections", "Average")]:
        try:
            r = _cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName=metric_name,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                StartTime=start, EndTime=end, Period=3600, Statistics=[stat],
            )
            points = sorted(r["Datapoints"], key=lambda x: x["Timestamp"])
            values = [p[stat] for p in points]
            metrics[metric_name] = {
                "avg": round(sum(values) / len(values), 2) if values else 0,
                "max": round(max(values), 2) if values else 0,
                "points": len(values),
            }
        except Exception:
            metrics[metric_name] = {"avg": 0, "max": 0, "points": 0}
    return metrics


def analyze_projects(projects):
    """Send project + metrics data to Bedrock for recommendations."""
    # Collect metrics for all resources
    project_data = []
    for name, p in projects.items():
        entry = {"project": p["display"], "hourly_cost": round(p["hourly_cost"], 4), "resources": []}
        for r in p["ec2"]:
            m = get_ec2_metrics(r["id"], days=METRICS_LOOKBACK_DAYS)
            hp = get_hourly_usage_pattern(r["id"], "AWS/EC2", "CPUUtilization", "InstanceId", days=METRICS_LOOKBACK_DAYS)
            entry["resources"].append({
                "type": "EC2", "id": r["id"], "name": r["name"],
                "instance_type": r["type"], "state": r["state"], "metrics": m, "metrics_window_days": METRICS_LOOKBACK_DAYS,
                "hourly_cpu_pattern_utc": hp,
            })
        for r in p["aurora"]:
            entry["resources"].append({"type": "Aurora", "id": r["id"], "engine": r["engine"], "members": r["members"], "status": r["status"]})
        for r in p["rds_standalone"]:
            m = get_rds_metrics(r["id"], days=METRICS_LOOKBACK_DAYS)
            hp = get_hourly_usage_pattern(r["id"], "AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", days=METRICS_LOOKBACK_DAYS)
            entry["resources"].append({
                "type": "RDS", "id": r["id"], "engine": r["engine"],
                "class": r["cls"], "status": r["status"], "metrics": m, "metrics_window_days": METRICS_LOOKBACK_DAYS,
                "hourly_connections_pattern_utc": hp,
            })
        for r in p["ebs"]:
            entry["resources"].append({"type": "EBS", "id": r["id"], "name": r["name"], "size_gb": r["size"]})
        project_data.append(entry)

    # SD: never dump the full project_data (resource IDs, names, costs, metrics) — not
    # even under DEBUG. Log only non-sensitive structural metadata.
    try:
        _rc = sum(len(p.get("resources", [])) for p in project_data) if isinstance(project_data, list) else 0
        print(f"BEDROCK_INPUT: {len(project_data)} projects, {_rc} resources")
    except Exception:
        print("BEDROCK_INPUT: (summary unavailable)")

    if not project_data or all(len(e["resources"]) == 0 for e in project_data):
        return {"recommendations": [], "summary": "No active resources to analyze. Restore some projects first."}

    project_names = [e["project"] for e in project_data]

    # #8 Closed loop: prepend recent human feedback as few-shot corrections.
    feedback_block = ""
    try:
        import feedback as _fb
        feedback_block = _fb.recent_feedback_examples(limit=10)
    except Exception as e:
        print(f"feedback few-shot warning: {e}")

    prompt = f"""You are an AWS cost optimization advisor. Analyze ONLY the resources and metrics provided below.
{feedback_block}
STRICT RULES:
- The ONLY valid project names are: {json.dumps(project_names)}
- You MUST use these EXACT project names. Do NOT rename, abbreviate, or invent project names.
- If a project name is "__untagged__" or "Untagged Resources", use that exact name.
- Calculate monthly_savings = hourly_cost * 16 * 22 (16 saved hours/day, 22 workdays/month)
- If metrics show 0 data points, state "no metrics available" — do NOT guess.
- Do NOT add any projects that are not in the list above.

Resources by project:
{json.dumps(project_data, indent=2)}

For each project in the data above, provide:
1. risk: safe_to_save (avg CPU < 10% or 0 connections) / review_needed (some activity) / keep_running (high usage)
2. monthly_savings: calculated from hourly_cost * 16 * 22
3. suggestions: based on actual metrics only
4. anomalies: only if data supports it
5. suggested_schedule: analyze hourly_cpu_pattern_utc and hourly_connections_pattern_utc to find active hours.
   - Find first and last UTC hour with avg > 1% CPU or > 0.1 connections. That is the active window.
   - restore_at: 1 hour BEFORE active window starts, converted to US/Eastern (UTC-4 EDT)
   - save_at: end of active window, converted to US/Eastern
   - If no clear pattern or always active, set suggested_schedule to null
   - days: ["mon","tue","wed","thu","fri"] unless weekend activity detected

Respond in this exact JSON format:
{{
  "recommendations": [
    {{
      "project": "EXACT project name from data above",
      "risk": "safe_to_save|review_needed|keep_running",
      "reason": "brief explanation",
      "monthly_savings": 0.00,
      "suggestions": ["suggestion 1"],
      "anomalies": ["anomaly if any"],
      "suggested_schedule": {{
        "restore_at": "08:00",
        "save_at": "18:00",
        "timezone": "US/Eastern",
        "days": ["mon","tue","wed","thu","fri"],
        "reasoning": "Active 13-22 UTC = 9AM-6PM ET. Restore 1hr early at 8AM."
      }}
    }}
  ],
  "summary": "one-line overall summary"
}}"""

    result = _call_bedrock(prompt)
    # Attach the metrics context so the deterministic guardrail can validate (#8).
    if isinstance(result, dict):
        result["_project_data"] = project_data
    return result


def process_natural_language(user_input, projects):
    """Process natural language input and return action or info."""
    project_names = [p["display"] for p in projects.values()]
    project_summary = json.dumps([{
        "name": p["display"],
        "resources": len(p["ec2"]) + len(p["ebs"]) + len(p["aurora"]) + len(p["rds_standalone"]),
        "hourly_cost": round(p["hourly_cost"], 4),
    } for p in projects.values()])

    prompt = f"""You are a cost optimization assistant inside a CloudWatch Dashboard.
Available projects: {project_summary}

User said: "{user_input}"

Determine the user's intent and respond in this exact JSON format:
{{
  "action": "save|restore|analyze|info|schedule",
  "project": "project name or null for all",
  "response": "friendly response to show the user",
  "params": {{}}
}}

Rules:
- "save all dev" or "shut down X" -> action: save
- "bring back X" or "restore X" -> action: restore
- "what's costing me" or "analyze" -> action: analyze
- "schedule X to save at 6pm" -> action: schedule, params: {{"end_time": "18:00"}}
- General questions -> action: info"""

    return _call_bedrock(prompt)


def _invoke_one(model_id, prompt):
    """Invoke a single model and return parsed JSON, or raise."""
    r = _bedrock.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    body = json.loads(r["body"].read())
    text = body["content"][0]["text"]
    # SD: never dump raw model text (it recalls customer resource/cost data) — not even
    # under DEBUG. Log only model id and response length.
    print(f"BEDROCK_RESPONSE[{model_id}]: {len(text)} chars")
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    import re
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)
    return json.loads(text.strip())


def _error_code(exc):
    """Best-effort extraction of a Bedrock/botocore error code."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return resp.get("Error", {}).get("Code", type(exc).__name__)
    return type(exc).__name__


def _call_bedrock(prompt, max_attempts_per_model=2):
    """Call Bedrock with an ordered model fallback chain (#7).

    For each model in get_model_chain(): retry transient throttles with backoff, and on a
    fallback-class error move to the next model. Returns parsed JSON, or a structured
    error listing what was tried (so the widget degrades instead of crashing)."""
    chain = get_model_chain()
    last_err = None
    for model_id in chain:
        for attempt in range(max_attempts_per_model):
            try:
                result = _invoke_one(model_id, prompt)
                if model_id != chain[0]:
                    result.setdefault("_model_used", model_id)
                return result
            except Exception as e:
                code = _error_code(e)
                last_err = f"{model_id}: {code}"
                # SD: log only the extracted error code + model id — NOT the raw exception,
                # which can embed request context (resource ids, metric values, cost data).
                # Consistent with the data-minimization on the success path (lines ~172-177).
                print(f"BEDROCK_ERROR[{model_id}] attempt {attempt+1}: {code}")
                if code == "ThrottlingException" and attempt + 1 < max_attempts_per_model:
                    time.sleep(0.5 * (2 ** attempt))  # backoff then retry same model
                    continue
                if code in _FALLBACK_ERRORS:
                    break  # move to next model in the chain
                # Unknown/non-retryable error: stop trying, surface it.
                return {"error": str(e), "model_tried": model_id}
    return {"error": f"All models failed: {last_err}", "models_tried": chain}
