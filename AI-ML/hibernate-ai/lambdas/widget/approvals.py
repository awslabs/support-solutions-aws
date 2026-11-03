# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Approval workflow + role separation for HibernateAI.

- Read vs privileged action classification: ``analyze`` is read-only; ``save``/``restore``
  are privileged (destructive). Privileged actions can be gated on caller identity and,
  above the cost threshold, on a second-person approval.
- Pending-approval records live in DynamoDB under ``__approval__<token>`` (mirroring the
  existing ``__snooze__<token>`` pattern in notifications.py) and are consumed once.
"""
import hashlib
import json
import os
from datetime import datetime

import boto3

# prefreeze triggers an SNS notification and snooze/feedback mutate state, so NONE of them
# are read-only. They are gated below (every state-changing/side-effecting action must
# pass authorize()).
READ_ONLY_ACTIONS = {"analyze", "nlp", "list_accounts", "detect_tags"}
# Every STATE-CHANGING action is privileged and must pass authorize(). This includes not
# just the directly-destructive save/restore, but also actions that can REWRITE the safety
# controls themselves (set_config can change the approver allowlist/deny-list/thresholds),
# schedule/rule/account mutations, and `approve` (which executes the destructive hibernate).
# Omitting any of these lets an unauthenticated caller bypass the approval model.
PRIVILEGED_ACTIONS = {
    "save", "restore", "deploy_stackset", "approve",
    "set_config", "set_schedule", "remove_schedule",
    "add_rule", "remove_rule", "set_accounts",
}
# Side-effecting actions that must NOT be approver-gated (that would break their legitimate
# automated/token-driven sources) but also must NOT be treated as unauthenticated read-only:
#   - prefreeze: invoked by an EventBridge SCHEDULE (no human principal) -> require the event
#     to originate from EventBridge ("source": "aws.events" / a detail-type), reject direct calls.
#   - snooze: carries a single-use snooze TOKEN from the notification -> self-authorizing by token.
#   - feedback: low-risk write; its real risk is free-text reaching the LLM prompt, mitigated by
#     escaping + validation at the sink, not by an approver gate.
SOURCE_OR_TOKEN_ACTIONS = {"prefreeze", "snooze", "feedback"}
# Changing WHO can approve (the allowlist) or the safety thresholds is the highest-privilege
# operation — it should be restricted to a dedicated admin principal, not any approver.
ADMIN_ACTIONS = {"set_config", "set_accounts"}


def _table():
    return boto3.resource("dynamodb").Table(os.environ.get("STATE_TABLE", "HibernateAIState"))


def is_privileged(action):
    return action in PRIVILEGED_ACTIONS


def is_eventbridge_sourced(event):
    """True only if the invocation came from EventBridge (a scheduled rule), not a direct
    user/widget call. EventBridge events carry a top-level "source" (e.g. "aws.events") and
    a "detail-type". Used to restrict `prefreeze` (which sends an SNS alert) to its scheduler.
    """
    if not isinstance(event, dict):
        return False
    return bool(event.get("source")) and bool(event.get("detail-type"))


def caller_identity(event):
    """Extract the invoking principal from the event, if the widget/API passed it.

    CloudWatch custom-widget events don't carry IAM identity by default, so this is
    best-effort: we look for an explicitly forwarded principal. Returns None when the
    caller is unknown (the authz gate treats unknown as unauthorized for privileged
    actions when an allowlist is configured).
    """
    for path in (("requestContext", "identity", "userArn"), ("principal",), ("userArn",)):
        node = event
        ok = True
        for k in path:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                ok = False
                break
        if ok and isinstance(node, str) and node:
            return node
    return None


def authorize(action, event, *, config=None):
    """Return (allowed, reason). Read-only actions are always allowed.

    Privileged actions require the caller to be in the ``approvers`` allowlist, matched by
    EXACT principal ARN. Admin actions (changing the allowlist/thresholds) additionally
    require the caller to be in the ``admins`` allowlist.

    Honest limitations made explicit:

    1. **Transport / identity-trust limitation:** CloudWatch custom-widget events do NOT
       natively carry a verified IAM identity. ``caller_identity`` reads a principal from
       caller-supplied event fields, which can be SPOOFED by whoever invokes the function.
       This gate is therefore defence-in-depth only. ROBUST enforcement REQUIRES a trusted
       front door that carries a verified identity (API Gateway with IAM/Cognito authorizer,
       or a Step Functions approval) AND resource-/identity-based IAM limiting who may invoke
       this function. Do not rely on this gate alone for production.

    2. **Unconfigured behavior — ALWAYS FAIL CLOSED:** when NO approver allowlist is set,
       privileged actions are DENIED. There is no opt-in to skip this — a deployment with no
       ``approvers`` list simply cannot perform privileged actions. (The previous demo
       fall-open switch, ``ALLOW_UNCONFIGURED_PRIVILEGED``, was removed: shipping a one-flag
       way to disable authorization on destructive actions is a needless footgun.)
    """
    if action in READ_ONLY_ACTIONS:
        return True, "read-only"
    from config import load_config

    cfg = config if config is not None else load_config()
    approvers = cfg.get("approvers") or []
    if not approvers:
        # SECURE DEFAULT (no opt-out): deny every privileged action until an 'approvers'
        # allowlist is configured. No environment/config switch can relax this.
        return False, ("no approver allowlist configured (fail-closed): privileged action "
                       "denied until an 'approvers' allowlist is set")
    principal = caller_identity(event)
    if principal is None:
        return False, "caller identity unknown; privileged action requires an approver"
    # EXACT match only — a suffix/endswith match (previous behavior) would let
    # an attacker-chosen principal whose ARN merely ENDS WITH an allowlisted string pass.
    if principal not in approvers:
        return False, f"principal {principal} is not in the approver allowlist"
    # Admin actions (rewriting the approver/deny lists or thresholds) need the stricter
    # admins allowlist — and FAIL CLOSED: if no admins list is configured, admin actions are
    # DENIED (previously any approver passed). A deployment may opt into letting
    # approvers double as admins via allow_approver_as_admin, for demo/simple setups.
    if action in ADMIN_ACTIONS:
        admins = cfg.get("admins") or []
        if admins:
            if principal not in admins:
                return False, f"principal {principal} is not an admin (required for {action})"
        else:
            allow_approver_admin = bool(cfg.get("allow_approver_as_admin")) or \
                os.environ.get("ALLOW_APPROVER_AS_ADMIN", "").strip().lower() in ("1", "true", "yes")
            if not allow_approver_admin:
                return False, (f"{action} is an admin action but no 'admins' allowlist is "
                               "configured (fail-closed); set 'admins' or opt in with "
                               "allow_approver_as_admin for demo use")
    return True, f"principal {principal} is authorized"


def verify_approver_distinct(approver_principal, requested_by):
    """Second-person approval: the approver MUST differ from the requester.

    Returns (ok, reason). An approver who is also the requester (or an unknown approver
    identity) fails the check, so a caller cannot approve their own pending request.
    """
    if not approver_principal:
        return False, "approver identity unknown; cannot verify second-person approval"
    if requested_by and approver_principal == requested_by:
        return False, "approver is the same principal as the requester (self-approval denied)"
    return True, "approver is distinct from requester"


def create_pending_approval(project_name, plan, requested_by=None):
    """Persist a pending-approval record and return an approve token."""
    from decimal import Decimal

    token = hashlib.sha256(
        f"approval-{project_name}-{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:16]
    _table().put_item(
        Item={
            "project_name": f"__approval__{token}",
            "state": {
                "project": project_name,
                "plan": json.loads(json.dumps(plan, default=str), parse_float=Decimal),
                "requested_by": requested_by,
                "created": datetime.utcnow().isoformat(),
                "status": "pending",
            },
        }
    )
    return token


def get_pending_approval(token):
    r = _table().get_item(Key={"project_name": f"__approval__{token}"})
    return r.get("Item", {}).get("state")


def consume_approval(token, *, approve=True, decided_by=None):
    """Mark an approval decided and remove the token (single-use). Returns the record
    or None if the token was invalid/expired."""
    rec = get_pending_approval(token)
    if not rec or rec.get("status") != "pending":
        return None
    rec["status"] = "approved" if approve else "rejected"
    rec["decided_by"] = decided_by
    rec["decided_at"] = datetime.utcnow().isoformat()
    # Single-use: delete the pending token so it cannot be replayed.
    _table().delete_item(Key={"project_name": f"__approval__{token}"})
    return rec


def notify_approver(project_name, token, plan, *, monthly_cost=None):
    """SNS an approver that a destructive action needs approval. Best-effort."""
    topic = os.environ.get("NOTIFICATION_TOPIC_ARN", "")
    if not topic:
        return False
    try:
        boto3.client("sns").publish(
            TopicArn=topic,
            Subject=f"[Hibernate AI] Approval needed: {project_name}",
            Message=json.dumps(
                {
                    "project": project_name,
                    "approve_token": token,
                    "estimated_monthly_cost_usd": monthly_cost,
                    "resource_count": len(plan) if isinstance(plan, list) else None,
                    "action": "Reply/POST with this token to approve the hibernate.",
                },
                indent=2,
                default=str,
            ),
        )
        return True
    except Exception as e:
        print(f"approver SNS warning: {e}")
        return False
