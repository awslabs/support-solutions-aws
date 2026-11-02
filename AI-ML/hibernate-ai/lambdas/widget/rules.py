# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Custom per-service rules for Hibernate AI — user-defined protection policies."""
import boto3
import json
import os
import html
from datetime import datetime, timedelta


def esc(v):
    """HTML-escape externally-sourced values (user-supplied rule fields) before
    interpolating into HTML — prevents stored XSS via rule name/service/condition."""
    return html.escape("" if v is None else str(v), quote=True)

STATE_TABLE = os.environ.get("STATE_TABLE", "HibernateAIState")

# Default built-in rules
DEFAULT_RULES = [
    {"id": "builtin-prod-tag", "name": "Protect production resources", "service": "*", "condition": "tag:Environment in [production, prod, prd]", "action": "skip", "enabled": True, "builtin": True},
    {"id": "builtin-protect-tag", "name": "Protect explicitly tagged", "service": "*", "condition": "tag:CostSaverProtect in [true, yes]", "action": "skip", "enabled": True, "builtin": True},
]


def get_rules():
    """Load all rules (built-in + custom) from DynamoDB."""
    table = boto3.resource("dynamodb").Table(STATE_TABLE)
    try:
        r = table.get_item(Key={"project_name": "__rules__"})
        custom = r.get("Item", {}).get("state", {}).get("rules", [])
    except Exception:
        custom = []
    return DEFAULT_RULES + custom


def save_rules(custom_rules):
    """Save custom rules to DynamoDB."""
    table = boto3.resource("dynamodb").Table(STATE_TABLE)
    table.put_item(Item={"project_name": "__rules__", "state": {"rules": custom_rules, "updated": datetime.utcnow().isoformat()}})


def add_rule(rule):
    """Add a new custom rule."""
    rules = get_rules()
    custom = [r for r in rules if not r.get("builtin")]
    rule["id"] = f"custom-{len(custom)+1}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    rule["enabled"] = True
    rule["builtin"] = False
    custom.append(rule)
    save_rules(custom)
    return rule


def remove_rule(rule_id):
    """Remove a custom rule by ID."""
    rules = get_rules()
    custom = [r for r in rules if not r.get("builtin") and r["id"] != rule_id]
    save_rules(custom)


def evaluate_resource(resource, resource_type, metrics=None):
    """Evaluate all rules against a resource. Returns (allow_hibernate: bool, blocking_rule: str|None)."""
    rules = get_rules()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        if rule["service"] != "*" and rule["service"].lower() != resource_type.lower():
            continue
        if _matches_condition(rule["condition"], resource, resource_type, metrics):
            if rule["action"] == "skip":
                return False, rule["name"]
    return True, None


def _matches_condition(condition, resource, resource_type, metrics):
    """Evaluate a rule condition against a resource."""
    condition = condition.strip()

    # Tag-based: "tag:Key in [val1, val2]"
    if condition.startswith("tag:"):
        parts = condition[4:].split(" in ")
        if len(parts) == 2:
            tag_key = parts[0].strip()
            values = [v.strip().strip("[]").strip() for v in parts[1].strip("[]").split(",")]
            tags = resource.get("Tags", resource.get("tags", []))
            for t in tags:
                if t.get("Key", t.get("key", "")) == tag_key and t.get("Value", t.get("value", "")).lower() in [v.lower() for v in values]:
                    return True

    # Metric-based: "metric:DatabaseConnections > 5"
    elif condition.startswith("metric:"):
        if not metrics:
            return False
        parts = condition[7:].split()
        if len(parts) >= 3:
            metric_name, operator, threshold = parts[0], parts[1], float(parts[2])
            value = metrics.get(metric_name, {}).get("avg", 0)
            if operator == ">" and value > threshold: return True
            if operator == "<" and value < threshold: return True
            if operator == ">=" and value >= threshold: return True
            if operator == "<=" and value <= threshold: return True

    # State-based: "state:running"
    elif condition.startswith("state:"):
        expected = condition[6:].strip()
        state = resource.get("State", {}).get("Name", resource.get("DBInstanceStatus", resource.get("status", "")))
        if state.lower() == expected.lower():
            return True

    # Age-based: "age:<7d" (don't hibernate resources less than 7 days old)
    elif condition.startswith("age:<"):
        days = int(condition[5:].rstrip("d"))
        launch_time = resource.get("LaunchTime")
        if launch_time:
            age = (datetime.utcnow() - launch_time.replace(tzinfo=None)).days
            if age < days:
                return True

    return False


def render_rules_html(self_arn):
    """Render the rules management UI."""
    rules = get_rules()
    rows = ""
    for r in rules:
        badge = '<span style="font-size:9px;padding:1px 4px;background:#4f8ff7;border-radius:2px">built-in</span>' if r.get("builtin") else ""
        enabled = "✅" if r.get("enabled") else "❌"
        rows += f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #2a2d3a;font-size:12px"><div>{enabled} <b>{esc(r["name"])}</b> {badge}<div style="color:#888;font-size:10px">{esc(r["service"])} — {esc(r["condition"])}</div></div>'
        if not r.get("builtin"):
            rm_params = json.dumps({"action": "remove_rule", "rule_id": r["id"]})
            rows += f'<div><a class="btn" style="background:#f87171;color:#fff;padding:2px 6px;font-size:10px;border-radius:3px;text-decoration:none">Remove</a><cwdb-action action="call" endpoint="{self_arn}" display="widget">{rm_params}</cwdb-action></div>'
        rows += '</div>'

    return f"""
<div style="background:#16213e;border-radius:6px;padding:12px;margin-top:8px">
  <div style="font-size:13px;font-weight:600;color:#fb923c;margin-bottom:8px">📋 Hibernate Rules</div>
  {rows}
  <div style="margin-top:8px;font-size:11px;color:#888">
    <b>Add rules via:</b> "add rule: skip RDS when metric:DatabaseConnections > 5"
  </div>
</div>"""
