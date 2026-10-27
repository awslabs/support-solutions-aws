# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Plain Lambda serving UI events and the weekly schedule (issue #139).

Two invocation shapes:
- API Gateway HTTP API (payload v2, Cognito JWT authorizer already enforced):
    POST /actions          body {action, ...}     -> actions.dispatch
    POST /refresh          body {mode?, services?} -> start (or adopt) a durable
                                                     pipeline execution
    GET  /refresh/{arn}                           -> condensed execution status
- EventBridge Scheduler (direct invoke):
    {"action": "start_refresh", "refresh_origin": "Auto"} -> weekly pipeline run
  Any other router payload -> actions.dispatch

The browser holds no IAM permissions: everything reaches AWS through this
function, so its role carries the permissions the UI used to need.

The HTTP routes accept only what the web UI sends:
the router actions in HTTP_ACTIONS, run parameters validated against known
values, execution names and the run origin decided here, and status reads of
this pipeline's executions only. Extraction and scans requested from the web
always go through POST /refresh and the pipeline. Direct invocation (IAM
principals of the account: scheduler, CLI, tests) keeps the full router.
The caller's Cognito identity is passed to the router so action plans record
who changed them.
"""
import json
import os
import re
import time
from typing import Optional, Tuple

import boto3

from account_discovery import MAX_SCAN_REGIONS
from actions import dispatch

PIPELINE_FUNCTION_ARN = os.environ.get("PIPELINE_FUNCTION_ARN")  # qualified (alias) ARN
_lambda = None


def _lambda_client():
    global _lambda
    if _lambda is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        _lambda = boto3.client("lambda", region_name=region)
    return _lambda


# ---------------------------------------------------------------------------
# Refresh pipeline control
# ---------------------------------------------------------------------------

def _unqualified_function_arn() -> str:
    """Strip the alias qualifier: arn:...:function:name:live -> arn:...:function:name."""
    parts = PIPELINE_FUNCTION_ARN.split(":")
    return ":".join(parts[:7]) if len(parts) > 7 else PIPELINE_FUNCTION_ARN


def _running_execution() -> Optional[dict]:
    """The currently RUNNING pipeline execution, if any (across all versions).

    The list API cannot filter by alias, so query the function itself.
    """
    resp = _lambda_client().list_durable_executions_by_function(
        FunctionName=_unqualified_function_arn(), Statuses=["RUNNING"], MaxItems=1)
    executions = resp.get("DurableExecutions", [])
    return executions[0] if executions else None


def start_refresh(body: dict) -> dict:
    """Start a pipeline execution, or adopt the one already running.

    Execution names are unique per function: a duplicate name is the SAME
    execution (AWS-enforced), which is what makes the weekly schedule
    idempotent. Manual starts get a timestamped name.
    """
    if not PIPELINE_FUNCTION_ARN:
        return {"error": "PIPELINE_FUNCTION_ARN not configured"}, 500

    running = _running_execution()
    if running:
        return {"executionArn": running["DurableExecutionArn"], "alreadyRunning": True}, 200

    payload = {
        "mode": body.get("mode", "full"),
        "refresh_origin": body.get("refresh_origin", "manual"),
    }
    if body.get("services"):
        payload["services"] = body["services"]
    if body.get("regions"):
        payload["regions"] = body["regions"]

    # Scheduled runs get a date-based name (one weekly run per day at most,
    # duplicates collapse onto the same execution); manual runs get a
    # timestamp. Allowed charset is [a-zA-Z0-9-_], max 64.
    if body.get("execution_name"):
        name = body["execution_name"]
    elif payload["refresh_origin"] == "Auto":
        name = f"refresh-weekly-{time.strftime('%Y-%m-%d', time.gmtime())}"
    else:
        name = f"refresh-manual-{int(time.time())}"
    resp = _lambda_client().invoke(
        FunctionName=PIPELINE_FUNCTION_ARN,
        InvocationType="Event",
        DurableExecutionName=name,
        Payload=json.dumps(payload).encode(),
    )
    return {"executionArn": resp.get("DurableExecutionArn"), "executionName": name,
            "alreadyRunning": False}, 202


def refresh_status(execution_arn: str) -> dict:
    """Condensed view of a pipeline execution for UI polling."""
    client = _lambda_client()
    desc = client.get_durable_execution(DurableExecutionArn=execution_arn)
    status = desc.get("Status")
    out = {
        "executionArn": execution_arn,
        "status": status,
        "startDate": desc.get("StartTimestamp").isoformat() if desc.get("StartTimestamp") else None,
        "stopDate": desc.get("EndTimestamp").isoformat() if desc.get("EndTimestamp") else None,
    }
    if status == "SUCCEEDED" and desc.get("Result"):
        try:
            out["summary"] = json.loads(desc["Result"])
        except (TypeError, ValueError):
            out["summary"] = None
    if status in ("FAILED", "TIMED_OUT", "STOPPED"):
        out["error"] = desc.get("Error")

    out["progress"] = _progress_from_history(client, execution_arn, status)
    return out, 200


def _decode_step_result(payload: str) -> dict:
    """The durable SDK stores step results in a typed envelope ({"t":"m","v":{...}}); flatten it."""
    def _dec(node):
        t, v = node.get("t"), node.get("v")
        if t == "m":
            return {k: _dec(val) for k, val in v.items()}
        if t == "l":
            return [_dec(x) for x in v]
        return v
    try:
        return _dec(json.loads(payload))
    except Exception:
        return {}


def _progress_from_history(client, execution_arn: str, status: str) -> dict:
    """Phases of a run, read from the execution history (best effort, never raises).

    Returns the legacy counters plus `phases`: one entry per pipeline phase with
    status (pending | in-progress | success | warning | error), done/failed/total
    and the step names still running, for the Steps component (#150 item 6).
    """
    steps = {}          # name -> "started" | "succeeded" | "failed"
    totals = {"extract_total": None, "scan_total": None}
    try:
        paginator = client.get_paginator("get_durable_execution_history")
        for page in paginator.paginate(DurableExecutionArn=execution_arn, IncludeExecutionData=True):
            for ev in page.get("Events", []):
                et, name = ev.get("EventType"), ev.get("Name") or ""
                if et == "StepStarted":
                    steps.setdefault(name, "started")
                elif et == "StepSucceeded":
                    steps[name] = "succeeded"
                    if name == "start-run":
                        run = _decode_step_result(((ev.get("StepSucceededDetails") or {}).get("Result") or {}).get("Payload") or "")
                        totals["extract_total"] = run.get("extract_total", len(run.get("services") or []) or None)
                        totals["scan_total"] = run.get("scan_total")
                elif et == "StepFailed":
                    steps[name] = "failed"  # a later StepStarted/StepSucceeded (retry) overrides
    except Exception:
        pass

    def phase(label: str, prefix: str, total, single: bool = False):
        names = [n for n in steps if (n == prefix if single else n.startswith(prefix))]
        done = sum(1 for n in names if steps[n] == "succeeded")
        failed = sum(1 for n in names if steps[n] == "failed")
        running = [n[len(prefix):] if not single else n for n in names if steps[n] == "started"]
        total = total if total is not None else (len(names) or None)
        if not names:
            st = "pending"
        elif running or (total and done + failed < total):
            st = "in-progress"
        elif failed:
            st = "error" if done == 0 else "warning"
        else:
            st = "success"
        return {"label": label, "status": st, "done": done, "failed": failed, "total": total, "running": running[:5]}

    phases = [
        phase("Prepare the run", "start-run", 1, single=True),
        phase("Update the catalog from the AWS documentation", "extract-", totals["extract_total"]),
        phase("Scan the accounts", "scan-", totals["scan_total"]),
        phase("Cross-check with AWS Health and price Extended Support", "reconcile-inventory", 1, single=True),
        phase("Summarize and notify", "summarize-and-notify", 1, single=True),
    ]
    # A phase that never started while the run is over is 'skipped' (mode extract/scan), not pending
    if status != "RUNNING":
        for ph in phases:
            if ph["status"] == "pending":
                ph["status"] = "stopped"
    return {
        "extract_done": phases[1]["done"], "scan_done": phases[2]["done"],
        "phases": phases,
    }


# ---------------------------------------------------------------------------
# HTTP request validation
# ---------------------------------------------------------------------------

# Router actions the web UI sends (frontend/src/api.ts). The router knows more
# (discover_account, list_enabled_service_names, and bodies without an action
# that run an extraction inside this function); those stay reachable by direct
# invocation only.
HTTP_ACTIONS = frozenset({
    'list_services', 'list_deprecations', 'get_metrics', 'list_scanners', 'list_regions',
    'save_scan_targets', 'update_service',
    'list_action_plans', 'get_action_plan', 'create_action_plan', 'update_action_plan', 'delete_action_plan',
})
# Actions that change data: logged with the caller's Cognito sub.
_MUTATING_HTTP_ACTIONS = frozenset({
    'save_scan_targets', 'update_service', 'create_action_plan', 'update_action_plan', 'delete_action_plan',
})
REFRESH_MODES = ("full", "extract", "scan")
_REGION_CODE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def caller_from_event(event: dict) -> Optional[dict]:
    """The Cognito user behind an HTTP API request, from the JWT authorizer claims.

    API Gateway has already validated the token; the claims are trusted here.
    Returns {"sub", "name"} (name: email, else username, else sub) or None.
    """
    claims = ((((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {})
              .get("claims") or {})
    sub = claims.get("sub")
    if not sub:
        return None
    return {"sub": str(sub), "name": str(claims.get("email") or claims.get("cognito:username") or sub)}


def _audit(event_name: str, caller: Optional[dict], **fields) -> None:
    """One log line per change made through the web: who (Cognito sub) and what (ids, never content)."""
    print(json.dumps({"audit": event_name, "user_sub": (caller or {}).get("sub"), **fields}, default=str))


def _configured_services() -> set:
    """Service keys present in the configuration table (enabled or not)."""
    from database_reads import config_table
    names, kwargs = set(), {"ProjectionExpression": "service_name"}
    while True:
        page = config_table.scan(**kwargs)
        names.update(item["service_name"] for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return names
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def refresh_request_from_http(body) -> Tuple[Optional[dict], Optional[str]]:
    """Validate a POST /refresh body. Returns (start_refresh payload, None) or (None, error).

    The run origin is always "manual" and the execution name is always chosen
    by start_refresh: a web caller cannot take the weekly schedule's name (which
    would make the scheduled run adopt theirs) or label a run as scheduled.
    """
    if not isinstance(body, dict):
        return None, "Body must be a JSON object"
    mode = str(body.get("mode") or "full").lower()
    if mode not in REFRESH_MODES:
        return None, f"mode must be one of {', '.join(REFRESH_MODES)}"
    request = {"mode": mode, "refresh_origin": "manual"}

    services = body.get("services")
    if services:
        if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
            return None, "services must be a list of service names"
        unknown = sorted(set(services) - _configured_services())
        if unknown:
            return None, f"Unknown service(s): {', '.join(unknown)}"
        request["services"] = list(dict.fromkeys(services))

    regions = body.get("regions")
    if regions:
        if not isinstance(regions, list) or not all(isinstance(r, str) and _REGION_CODE.match(r) for r in regions):
            return None, "regions must be a list of region codes"
        regions = list(dict.fromkeys(regions))
        if len(regions) > MAX_SCAN_REGIONS:
            return None, f"up to {MAX_SCAN_REGIONS} regions allowed"
        request["regions"] = regions
    return request, None


def validate_http_action(body) -> Optional[str]:
    """Error message for a POST /actions body the web UI would never send, else None."""
    if not isinstance(body, dict):
        return "Body must be a JSON object"
    action = body.get("action")
    if action not in HTTP_ACTIONS:
        return "Unknown or missing action"
    if action == "update_service":
        service_name, updates = body.get("service_name"), body.get("updates")
        if not isinstance(service_name, str) or not service_name:
            return "service_name is required"
        if not isinstance(updates, dict) or set(updates) != {"enabled"} or not isinstance(updates["enabled"], bool):
            return "updates must be {\"enabled\": true|false}"
    return None


def is_pipeline_execution_arn(arn: str) -> bool:
    """True for a durable execution of this deployment's pipeline function (any version)."""
    if not PIPELINE_FUNCTION_ARN or not isinstance(arn, str):
        return False
    return arn.startswith(_unqualified_function_arn() + ":") and "/durable-execution/" in arn


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _http(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def handler(event, context):
    # API Gateway HTTP API (payload format 2.0)
    if isinstance(event, dict) and "requestContext" in event and "http" in event["requestContext"]:
        method = event["requestContext"]["http"]["method"]
        path = event.get("rawPath", "")
        body = {}
        if event.get("body"):
            try:
                body = json.loads(event["body"])
            except ValueError:
                return _http(400, {"error": "Body must be JSON"})

        caller = caller_from_event(event)
        if method == "POST" and path.endswith("/actions"):
            error = validate_http_action(body)
            if error:
                return _http(400, {"error": error})
            if body["action"] in _MUTATING_HTTP_ACTIONS:
                _audit(body["action"], caller, service_name=body.get("service_name"), plan_id=body.get("plan_id"))
            return _http(200, dispatch(body, caller=caller))
        if method == "POST" and path.endswith("/refresh"):
            request, error = refresh_request_from_http(body)
            if error:
                return _http(400, {"error": error})
            result, status = start_refresh(request)
            if status in (200, 202):
                _audit("start_refresh", caller, mode=request["mode"], services=request.get("services"),
                       execution=result.get("executionArn"), adopted=result.get("alreadyRunning"))
            return _http(status, result)
        if method == "GET" and "/refresh/" in path:
            arn = (event.get("pathParameters") or {}).get("arn") or path.rsplit("/refresh/", 1)[1]
            if not is_pipeline_execution_arn(arn):
                return _http(400, {"error": "Not an execution of this pipeline"})
            result, status = refresh_status(arn)
            return _http(status, result)
        return _http(404, {"error": f"No route for {method} {path}"})

    # Direct invocation (EventBridge Scheduler, CLI, tests). The weekly
    # schedule goes through here too ({"action": "start_refresh",
    # "refresh_origin": "Auto"}) so scheduled and manual runs share the
    # naming and adopt-running logic above.
    if isinstance(event, dict) and event.get("action") == "start_refresh":
        result, _ = start_refresh(event)
        return result
    return dispatch(event)
