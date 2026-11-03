# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Recommendation validation + human feedback loop.

Three pieces:
  1. Deterministic guardrail  — validate an LLM recommendation against the resource's
     actual metrics BEFORE display; override/flag an inconsistent ``safe_to_save``.
  2. Feedback capture         — thumbs up/down (+ reason) per recommendation, stored under
     the ``__feedback__<id>`` DynamoDB partition.
  3. Closed loop              — surface recent feedback as few-shot corrections for the
     advisor prompt.

The guardrail is pure and unit-testable; capture/read touch DynamoDB.
"""
import hashlib
import json
import os
from datetime import datetime

import boto3

# Thresholds above which a resource is considered ACTIVE (so safe_to_save is wrong).
# Overridable via __config__ keys of the same name.
_DEFAULT_THRESHOLDS = {
    "max_cpu_pct_for_save": 10.0,        # avg CPU at/above this => not safe to save
    "max_db_connections_for_save": 1.0,  # avg DB connections at/above => not safe
}


def _thresholds(config=None):
    try:
        import config as _hib_config

        cfg = config if config is not None else _hib_config.load_config()
    except Exception:
        cfg = {}
    return {
        "max_cpu_pct_for_save": float(cfg.get("max_cpu_pct_for_save",
                                               _DEFAULT_THRESHOLDS["max_cpu_pct_for_save"])),
        "max_db_connections_for_save": float(cfg.get("max_db_connections_for_save",
                                                     _DEFAULT_THRESHOLDS["max_db_connections_for_save"])),
    }


def validate_recommendation(rec, resources, config=None):
    """Deterministic guardrail. Given one recommendation and the project's resource list
    (each with ``metrics_7d``), downgrade an unsupported ``safe_to_save`` to
    ``review_needed`` and attach a guardrail note.

    Returns a (possibly modified) copy of ``rec``. Never UPGRADES risk — only makes a
    recommendation safer, never more aggressive.
    """
    th = _thresholds(config)
    risk = rec.get("risk")
    if risk != "safe_to_save":
        return rec  # only guard the destructive-leaning verdict

    violations = []
    for r in resources or []:
        m = r.get("metrics_7d") or {}
        cpu = (m.get("CPUUtilization") or {}).get("avg")
        conns = (m.get("DatabaseConnections") or {}).get("avg")
        # Only treat as a violation when we actually HAVE data (points > 0); absent
        # metrics are handled by the fail-safe layer, not here.
        cpu_pts = (m.get("CPUUtilization") or {}).get("points", 0)
        conn_pts = (m.get("DatabaseConnections") or {}).get("points", 0)
        if cpu is not None and cpu_pts and cpu >= th["max_cpu_pct_for_save"]:
            violations.append(f"{r.get('id', r.get('type','resource'))}: avg CPU {cpu}% ≥ {th['max_cpu_pct_for_save']}%")
        if conns is not None and conn_pts and conns >= th["max_db_connections_for_save"]:
            violations.append(f"{r.get('id','db')}: avg connections {conns} ≥ {th['max_db_connections_for_save']}")

    if violations:
        out = dict(rec)
        out["risk"] = "review_needed"
        out["guardrail_override"] = True
        note = "Guardrail: metrics contradict 'safe_to_save' — " + "; ".join(violations)
        out["reason"] = (note + ". " + out.get("reason", "")).strip()
        return out
    return rec


def validate_result(result, project_data, config=None):
    """Apply the guardrail across all recommendations in an analyze_projects() result.
    ``project_data`` is the list the advisor was given (project -> resources)."""
    if not isinstance(result, dict) or "recommendations" not in result:
        return result
    by_project = {e.get("project"): e.get("resources", []) for e in (project_data or [])}
    result = dict(result)
    result["recommendations"] = [
        validate_recommendation(rec, by_project.get(rec.get("project"), []), config)
        for rec in result.get("recommendations", [])
    ]
    return result


# ── Feedback capture (thumbs up/down) ───────────────────────────────────────
def _table():
    return boto3.resource("dynamodb").Table(os.environ.get("STATE_TABLE", "HibernateAIState"))


def record_feedback(project, verdict, recommendation=None, user=None, reason=None, outcome=None):
    """Persist a feedback event under __feedback__<id>. verdict: 'up' | 'down'."""
    fid = hashlib.sha256(f"{project}-{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]
    item = {
        "project_name": f"__feedback__{fid}",
        "state": {
            "project": project,
            "verdict": verdict,
            "recommendation": recommendation,
            "user": user,
            "reason": reason,
            "outcome": outcome,
            "created": datetime.utcnow().isoformat(),
        },
    }
    _table().put_item(Item=json.loads(json.dumps(item, default=str)))
    return fid


def list_feedback(limit=50):
    """Return recent feedback events (scan of __feedback__ partitions), newest first."""
    try:
        r = _table().scan()
    except Exception as e:
        print(f"feedback scan warning: {e}")
        return []
    items = [i["state"] for i in r.get("Items", [])
             if str(i.get("project_name", "")).startswith("__feedback__")]
    items.sort(key=lambda s: s.get("created", ""), reverse=True)
    return items[:limit]


def recent_feedback_examples(limit=10):
    """Build a compact few-shot correction block from recent feedback for the advisor
    prompt (closed loop). Returns a string, empty if no feedback yet."""
    fb = list_feedback(limit)
    if not fb:
        return ""
    lines = []
    for f in fb:
        verdict = "CORRECT" if f.get("verdict") == "up" else "WRONG"
        reason = f.get("reason") or ""
        outcome = f.get("outcome") or ""
        lines.append(f"- Project '{f.get('project')}': prior recommendation judged {verdict} by a human. "
                     f"{('Reason: ' + reason) if reason else ''} {('Outcome: ' + outcome) if outcome else ''}".strip())
    return ("\nHUMAN FEEDBACK ON PAST RECOMMENDATIONS (use as corrections; do not repeat "
            "mistakes judged WRONG):\n" + "\n".join(lines) + "\n")
