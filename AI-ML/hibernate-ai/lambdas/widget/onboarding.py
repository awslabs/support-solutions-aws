# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Onboarding & adoption guidance for Hibernate AI."""
import json

SETUP_STEPS = [
    {"step": 1, "title": "Tag Your Resources", "desc": "Add `project_name` tag to group resources. Resources without this tag appear as 'Untagged'.", "command": "aws ec2 create-tags --resources i-xxx --tags Key=project_name,Value=my-project"},
    {"step": 2, "title": "Protect Production", "desc": "Add `Environment=production` or `CostSaverProtect=true` tag to prevent accidental hibernation.", "command": "aws ec2 create-tags --resources i-xxx --tags Key=Environment,Value=production"},
    {"step": 3, "title": "Enable Bedrock", "desc": "Enable Claude 3 Haiku in the Bedrock console for AI-powered analysis.", "command": None},
    {"step": 4, "title": "Run AI Analysis", "desc": "Click 'AI Analyze All' to get recommendations based on 7-day CloudWatch metrics.", "command": None},
    {"step": 5, "title": "Set Schedules", "desc": "After analysis, click 'Set This Schedule' to auto-hibernate/restore on workdays.", "command": None},
]

TAGGING_SCRIPT = """#!/bin/bash
# Hibernate AI — Auto-tag resources by Name pattern
# Usage: ./tag_projects.sh <pattern> <project_name>
# Example: ./tag_projects.sh "dev-*" dev-environment

PATTERN="${1:?Usage: $0 <name-pattern> <project-name>}"
PROJECT="${2:?Usage: $0 <name-pattern> <project-name>}"

echo "Tagging EC2 instances matching '$PATTERN' as project_name=$PROJECT"
IDS=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=$PATTERN" "Name=instance-state-name,Values=running,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)
[ -n "$IDS" ] && aws ec2 create-tags --resources $IDS --tags Key=project_name,Value=$PROJECT && echo "Tagged: $IDS"

echo "Tagging EBS volumes matching '$PATTERN'"
VIDS=$(aws ec2 describe-volumes --filters "Name=tag:Name,Values=$PATTERN" --query 'Volumes[].VolumeId' --output text)
[ -n "$VIDS" ] && aws ec2 create-tags --resources $VIDS --tags Key=project_name,Value=$PROJECT && echo "Tagged: $VIDS"

echo "Done. Refresh Hibernate AI dashboard to see grouped resources."
"""

CUSTOMER_CFN_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "Hibernate AI — Deploy to customer account in 60 seconds",
    "Parameters": {
        "BedrockModelAccess": {"Type": "String", "Default": "true", "AllowedValues": ["true", "false"], "Description": "Whether Claude 3 Haiku is enabled in Bedrock"},
        "NotificationEmail": {"Type": "String", "Default": "", "Description": "Email for pre-freeze notifications (optional)"},
        "SlackWebhookUrl": {"Type": "String", "Default": "", "Description": "Slack webhook for notifications (optional)", "NoEcho": True},
        "PreFreezeMinutes": {"Type": "Number", "Default": 15, "Description": "Minutes before hibernate to send warning"},
    },
    "Conditions": {"HasEmail": {"Fn::Not": [{"Fn::Equals": [{"Ref": "NotificationEmail"}, ""]}]}},
    "Resources": {
        "NotificationTopic": {
            "Type": "AWS::SNS::Topic",
            "Condition": "HasEmail",
            "Properties": {"TopicName": "HibernateAI-Notifications", "Subscription": [{"Protocol": "email", "Endpoint": {"Ref": "NotificationEmail"}}]},
        },
    },
    "Outputs": {
        "DeployCommand": {"Value": "cd hibernate-ai && cdk deploy", "Description": "Run this after enabling Bedrock model access"},
    },
}


def render_onboarding_html(projects, self_arn):
    """Render the onboarding wizard panel."""
    # Determine current adoption state
    tagged_count = sum(1 for name in projects if name != "__untagged__")
    untagged = projects.get("__untagged__", {"ec2": [], "ebs": [], "aurora": [], "rds_standalone": []})
    untagged_count = len(untagged["ec2"]) + len(untagged["ebs"]) + len(untagged["aurora"]) + len(untagged["rds_standalone"])
    has_schedules = any(_has_schedule(p["display"]) for p in projects.values())

    # Progress calculation
    progress = 0
    if tagged_count > 0: progress += 1
    if any(_has_protection(p) for p in projects.values()): progress += 1
    progress += 1  # Bedrock assumed enabled (widget is running)
    if has_schedules: progress += 2
    completion = int((progress / 5) * 100)

    steps_html = ""
    for s in SETUP_STEPS:
        done = _is_step_done(s["step"], projects, has_schedules)
        icon = "✅" if done else f'<span style="color:#fb923c">{s["step"]}</span>'
        cmd = f'<div style="margin-top:4px;font-family:monospace;font-size:10px;background:#0f0f23;padding:4px 8px;border-radius:3px;color:#4f8ff7">{s["command"]}</div>' if s.get("command") and not done else ""
        steps_html += f'<div style="padding:6px 0;border-bottom:1px solid #2a2d3a"><div style="font-size:12px">{icon} <b>{s["title"]}</b></div><div style="font-size:11px;color:#888">{s["desc"]}</div>{cmd}</div>'

    # Quick tips based on state
    tips = []
    if untagged_count > 0:
        tips.append(f"💡 {untagged_count} untagged resources found — tag them with `project_name` for grouping")
    if not has_schedules and tagged_count > 0:
        tips.append("💡 Run AI Analysis to get schedule recommendations based on actual usage")
    tips_html = "".join(f'<div style="font-size:11px;color:#fb923c;padding:2px 0">{t}</div>' for t in tips)

    return f"""
<div style="background:#16213e;border-radius:6px;padding:12px;margin-top:8px;border-left:3px solid #4f8ff7">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:13px;font-weight:600;color:#4f8ff7">🚀 Getting Started</div>
    <div style="font-size:11px;color:#888">{completion}% setup complete</div>
  </div>
  <div style="background:#0f0f23;border-radius:4px;height:6px;margin:8px 0;overflow:hidden"><div style="background:#4f8ff7;height:100%;width:{completion}%;border-radius:4px"></div></div>
  {tips_html}
  <details style="cursor:pointer;margin-top:6px"><summary style="font-size:11px;color:#888">Setup steps</summary>
    <div style="margin-top:4px">{steps_html}</div>
    <div style="margin-top:8px;padding:8px;background:#0f0f23;border-radius:4px">
      <div style="font-size:11px;color:#888"><b>Tagging script:</b> Copy and run to bulk-tag resources</div>
      <pre style="font-size:10px;color:#4f8ff7;margin:4px 0;white-space:pre-wrap">./tag_projects.sh "dev-*" dev-environment</pre>
    </div>
  </details>
</div>"""


def _is_step_done(step, projects, has_schedules):
    if step == 1: return any(name != "__untagged__" for name in projects)
    if step == 2: return any(_has_protection(p) for p in projects.values())
    if step == 3: return True  # widget is running = Bedrock works
    if step == 4: return True  # assume they've tried it
    if step == 5: return has_schedules
    return False


def _has_protection(project):
    for r in project.get("ec2", []):
        tags = r.get("tags", [])
        if any(t.get("Key") == "Environment" for t in tags):
            return True
    return False


def _has_schedule(display_name):
    import boto3
    events = boto3.client("events")
    safe_name = display_name.replace(" ", "-").replace("__", "u")[:30]
    try:
        events.describe_rule(Name=f"HibernateAI-Restore-{safe_name}")
        return True
    except Exception:
        return False


# ── Tag auto-detection (Subagent B: review #1) ──────────────────────────────
# Common customer tag keys we can adopt instead of forcing our own convention.
COMMON_GROUPING_KEYS = ["project_name", "Project", "project", "App_name", "app",
                        "application", "asset_id", "service", "cost_center", "CostCenter",
                        "team", "Team", "owner", "LOB", "line_of_business"]
COMMON_ENV_KEYS = ["environment", "Environment", "env", "Env", "Stage", "stage", "tier"]


def detect_account_tag_keys(ec2_client=None, sample_limit=500):
    """Scan EC2 instance/volume tags in the account and return the tag keys present,
    ranked by how many resources carry them. Best-effort and read-only."""
    import boto3
    from collections import Counter

    ec2 = ec2_client or boto3.client("ec2")
    counts = Counter()
    seen = 0
    try:
        for page in ec2.get_paginator("describe_instances").paginate():
            for r in page.get("Reservations", []):
                for inst in r.get("Instances", []):
                    seen += 1
                    for t in inst.get("Tags", []):
                        k = t.get("Key", "")
                        if k and not k.startswith("aws:"):
                            counts[k] += 1
                    if seen >= sample_limit:
                        break
    except Exception as e:
        print(f"tag auto-detect warning: {e}")
    return counts


def propose_tag_mapping(ec2_client=None):
    """Propose grouping/environment tag keys from what the account already uses.

    Returns a dict suitable for merging into __config__:
      {"grouping_tag_keys": [...], "environment_tag_keys": [...], "detected": {...},
       "requires_our_convention": bool}
    If no usable grouping key is found, we fall back to requiring ``project_name``.
    """
    counts = detect_account_tag_keys(ec2_client)
    present = set(counts)

    grouping = [k for k in COMMON_GROUPING_KEYS if k in present]
    env = [k for k in COMMON_ENV_KEYS if k in present]

    requires_convention = not grouping
    if requires_convention:
        grouping = ["project_name"]
    if not env:
        env = ["environment"]

    return {
        "grouping_tag_keys": grouping,
        "environment_tag_keys": env,
        "detected": dict(counts),
        "requires_our_convention": requires_convention,
    }
