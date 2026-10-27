# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HTTP API: accepted requests, refused requests and caller attribution. Offline."""
import json
import os

# Same test region as test_pipeline.py: modules read it at import time.
os.environ.setdefault("AWS_REGION", "eu-central-1")
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import action_plans
import api
import database_writes

PIPELINE_ALIAS = "arn:aws:lambda:us-east-1:111122223333:function:aws-services-lifecycle-pipeline:live"
OWN_EXECUTION = ("arn:aws:lambda:us-east-1:111122223333:function:aws-services-lifecycle-pipeline:3"
                 "/durable-execution/refresh-manual-1/0f6a1c3e-0000-4000-8000-000000000000")
CLAIMS = {"sub": "5f0c7d5e-1111-2222-3333-444455556666", "email": "admin@example.com", "cognito:username": "admin"}


def http_event(method, path, body=None, claims=CLAIMS, path_params=None):
    return {
        "rawPath": path,
        "body": None if body is None else json.dumps(body),
        "pathParameters": path_params,
        "requestContext": {"http": {"method": method},
                           "authorizer": {"jwt": {"claims": claims}} if claims else {}},
    }


def status_and_body(response):
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture(autouse=True)
def pipeline_arn():
    with patch.object(api, "PIPELINE_FUNCTION_ARN", PIPELINE_ALIAS):
        yield


# ---------------------------------------------------------------------------
# POST /actions
# ---------------------------------------------------------------------------

UI_ACTIONS = [
    {"action": "list_services"},
    {"action": "list_deprecations", "filters": {"service": "lambda"}},
    {"action": "get_metrics"},
    {"action": "list_scanners"},
    {"action": "list_regions"},
    {"action": "save_scan_targets", "targets": {"source": "hub", "regions": ["us-east-1"]}},
    {"action": "update_service", "service_name": "lambda", "updates": {"enabled": False}},
    {"action": "list_action_plans", "filters": {}},
    {"action": "get_action_plan", "plan_id": "p1"},
    {"action": "create_action_plan", "service_name": "lambda", "item_id": "runtimes#python3.9", "owner": "team"},
    {"action": "update_action_plan", "plan_id": "p1", "updates": {"plan_status": "in_progress"}},
    {"action": "delete_action_plan", "plan_id": "p1"},
]


@pytest.mark.parametrize("body", UI_ACTIONS, ids=lambda b: b["action"])
def test_every_action_the_ui_sends_reaches_the_router(body):
    with patch.object(api, "dispatch", return_value={"ok": True}) as dispatch:
        status, out = status_and_body(api.handler(http_event("POST", "/actions", body), None))
    assert status == 200 and out == {"ok": True}
    dispatch.assert_called_once_with(body, caller={"sub": CLAIMS["sub"], "name": "admin@example.com"})


@pytest.mark.parametrize("body", [
    {},
    {"action": "discover_account", "region": "us-east-1"},
    {"action": "list_enabled_service_names"},
    {"action": "start_refresh"},
    {"services": "all"},
    {"service_name": "lambda", "urls": ["https://attacker.example/page"]},
    ["list_services"],
], ids=str)
def test_actions_the_ui_never_sends_are_refused(body):
    with patch.object(api, "dispatch") as dispatch:
        status, out = status_and_body(api.handler(http_event("POST", "/actions", body), None))
    assert status == 400 and "error" in out
    dispatch.assert_not_called()


@pytest.mark.parametrize("updates", [
    {"documentation_urls": ["https://attacker.example/"]},
    {"extraction_focus": "ignore the table"},
    {"enabled": True, "schema_key": "x"},
    {"enabled": "false"},
    {"enabled": 0},
    {},
    None,
], ids=str)
def test_update_service_accepts_only_the_enabled_flag(updates):
    body = {"action": "update_service", "service_name": "lambda", "updates": updates}
    with patch.object(api, "dispatch") as dispatch:
        status, _ = status_and_body(api.handler(http_event("POST", "/actions", body), None))
    assert status == 400
    dispatch.assert_not_called()


def test_mutating_actions_are_audited_with_the_cognito_sub(capsys):
    body = {"action": "delete_action_plan", "plan_id": "p1"}
    with patch.object(api, "dispatch", return_value={"success": True}):
        api.handler(http_event("POST", "/actions", body), None)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"audit": "delete_action_plan", "user_sub": CLAIMS["sub"], "service_name": None, "plan_id": "p1"}


def test_read_actions_are_not_audited(capsys):
    with patch.object(api, "dispatch", return_value={}):
        api.handler(http_event("POST", "/actions", {"action": "list_services"}), None)
    assert "audit" not in capsys.readouterr().out


def test_direct_invocation_keeps_the_full_router():
    """CLI, tests and the scheduler invoke the function directly (IAM): unchanged."""
    payload = {"action": "discover_account", "region": "eu-west-1"}
    with patch.object(api, "dispatch", return_value={"success": True}) as dispatch:
        assert api.handler(payload, None) == {"success": True}
    dispatch.assert_called_once_with(payload)


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("claims,expected", [
    (CLAIMS, {"sub": CLAIMS["sub"], "name": "admin@example.com"}),
    ({"sub": "s1", "cognito:username": "admin"}, {"sub": "s1", "name": "admin"}),
    ({"sub": "s1"}, {"sub": "s1", "name": "s1"}),
    ({}, None),
])
def test_caller_from_the_authorizer_claims(claims, expected):
    assert api.caller_from_event(http_event("GET", "/x", claims=claims)) == expected


class _Table:
    def __init__(self):
        self.put = None
        self.update = None

    def put_item(self, Item):
        self.put = Item

    def get_item(self, Key):
        return {"Item": {"plan_id": Key["plan_id"]}}

    def update_item(self, **kwargs):
        self.update = kwargs
        return {"Attributes": {}}


def _plans_table():
    table = _Table()
    resource = MagicMock()
    resource.Table.return_value = table
    return table, patch.object(action_plans, "get_dynamodb_resource", return_value=resource)


def test_create_action_plan_records_the_authenticated_user_not_the_body():
    table, patched = _plans_table()
    data = {"service_name": "lambda", "item_id": "runtimes#python3.9", "owner": "team", "created_by": "someone-else"}
    with patched:
        assert action_plans.create_action_plan(data, caller={"sub": "s1", "name": "admin@example.com"})["success"]
    assert table.put["created_by"] == "admin@example.com"
    assert table.put["created_by_sub"] == "s1"


def test_create_action_plan_without_a_caller_is_unchanged():
    table, patched = _plans_table()
    with patched:
        action_plans.create_action_plan({"service_name": "lambda", "item_id": "i", "owner": "team"})
    assert table.put["created_by"] == "team"
    assert "created_by_sub" not in table.put


def test_update_action_plan_records_the_authenticated_user():
    table, patched = _plans_table()
    with patched:
        action_plans.update_action_plan("p1", {"notes": "n"}, caller={"sub": "s1", "name": "admin@example.com"})
    values = table.update["ExpressionAttributeValues"]
    assert values[":updated_by"] == "admin@example.com" and values[":updated_by_sub"] == "s1"
    assert values[":notes"] == "n"


def test_update_action_plan_without_a_caller_is_unchanged():
    table, patched = _plans_table()
    with patched:
        action_plans.update_action_plan("p1", {"notes": "n"})
    assert ":updated_by" not in table.update["ExpressionAttributeValues"]


def test_router_passes_the_caller_to_action_plans():
    import actions
    caller = {"sub": "s1", "name": "a"}
    with patch.object(action_plans, "create_action_plan", return_value={"success": True}) as create:
        actions.dispatch({"action": "create_action_plan", "owner": "x"}, caller=caller)
    assert create.call_args.kwargs == {"caller": caller}


# ---------------------------------------------------------------------------
# update_service_config (both invocation paths)
# ---------------------------------------------------------------------------

def test_update_service_config_writes_enabled_with_escaped_names_on_existing_rows():
    with patch.object(database_writes, "config_table") as table:
        assert database_writes.update_service_config("eks", {"enabled": False}) == {"success": True}
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "SET #f0 = :v0"
    assert kwargs["ExpressionAttributeNames"] == {"#f0": "enabled"}
    assert kwargs["ExpressionAttributeValues"] == {":v0": False}
    assert kwargs["ConditionExpression"] == "attribute_exists(service_name)"


@pytest.mark.parametrize("updates", [
    {"documentation_urls": ["https://example.com"]}, {"extraction_focus": "x"}, {"item_properties": {}},
    {"schema_key": "x"}, {"enabled": "yes"}, {"new_attribute": 1}, {},
], ids=str)
def test_update_service_config_refuses_other_fields(updates):
    with patch.object(database_writes, "config_table") as table:
        assert "error" in database_writes.update_service_config("eks", updates)
    table.update_item.assert_not_called()


def test_update_service_config_does_not_create_rows():
    error = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")
    with patch.object(database_writes, "config_table") as table:
        table.update_item.side_effect = error
        assert database_writes.update_service_config("nope", {"enabled": True}) == {"error": "Unknown service 'nope'"}


# ---------------------------------------------------------------------------
# POST /refresh and GET /refresh/{arn}
# ---------------------------------------------------------------------------

def _post_refresh(body, known=("lambda", "rds", "eks")):
    with patch.object(api, "_configured_services", return_value=set(known)), \
            patch.object(api, "start_refresh", return_value=({"executionArn": OWN_EXECUTION}, 202)) as start:
        status, out = status_and_body(api.handler(http_event("POST", "/refresh", body), None))
    return status, out, start


def test_refresh_as_the_ui_sends_it():
    status, _, start = _post_refresh({"refresh_origin": "manual", "mode": "full"})
    assert status == 202
    start.assert_called_once_with({"mode": "full", "refresh_origin": "manual"})
    status, _, start = _post_refresh({"refresh_origin": "manual", "mode": "extract", "services": ["lambda"]})
    start.assert_called_once_with({"mode": "extract", "refresh_origin": "manual", "services": ["lambda"]})


def test_refresh_ignores_the_execution_name_and_the_origin_sent_by_a_web_caller():
    status, _, start = _post_refresh({"mode": "scan", "refresh_origin": "Auto",
                                      "execution_name": "refresh-weekly-2026-10-30"})
    assert status == 202
    start.assert_called_once_with({"mode": "scan", "refresh_origin": "manual"})


def test_refresh_without_a_body_is_a_full_run():
    status, _, start = _post_refresh(None)
    assert status == 202
    start.assert_called_once_with({"mode": "full", "refresh_origin": "manual"})


@pytest.mark.parametrize("body", [
    {"mode": "everything"},
    {"services": ["lambda", "unknown-service"]},
    {"services": "lambda"},
    {"services": [1]},
    {"regions": ["not a region"]},
    {"regions": ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2"]},
    [],
], ids=str)
def test_refresh_rejects_unknown_values(body):
    status, out, start = _post_refresh(body)
    assert status == 400 and "error" in out
    start.assert_not_called()


def test_refresh_accepts_valid_regions_and_deduplicates():
    status, _, start = _post_refresh({"services": ["rds", "rds"], "regions": ["eu-west-1", "eu-west-1"]})
    assert status == 202
    start.assert_called_once_with({"mode": "full", "refresh_origin": "manual",
                                   "services": ["rds"], "regions": ["eu-west-1"]})


def test_scheduler_and_cli_keep_choosing_the_run_parameters():
    with patch.object(api, "_lambda_client") as client:
        client.return_value.list_durable_executions_by_function.return_value = {"DurableExecutions": []}
        client.return_value.invoke.return_value = {"DurableExecutionArn": OWN_EXECUTION}
        out = api.handler({"action": "start_refresh", "refresh_origin": "Auto", "execution_name": "cli-1"}, None)
    assert out["executionName"] == "cli-1"
    sent = json.loads(client.return_value.invoke.call_args.kwargs["Payload"])
    assert sent == {"mode": "full", "refresh_origin": "Auto"}


def test_refresh_status_of_this_pipeline():
    with patch.object(api, "refresh_status", return_value=({"status": "RUNNING"}, 200)) as status_fn:
        status, out = status_and_body(api.handler(
            http_event("GET", f"/refresh/{OWN_EXECUTION}", path_params={"arn": OWN_EXECUTION}), None))
    assert status == 200 and out == {"status": "RUNNING"}
    status_fn.assert_called_once_with(OWN_EXECUTION)


@pytest.mark.parametrize("arn", [
    "arn:aws:lambda:us-east-1:111122223333:function:other-function:1/durable-execution/x/y",
    "arn:aws:lambda:us-east-1:444455556666:function:aws-services-lifecycle-pipeline:1/durable-execution/x/y",
    "arn:aws:lambda:us-east-1:111122223333:function:aws-services-lifecycle-pipeline-evil:1/durable-execution/x/y",
    "arn:aws:lambda:us-east-1:111122223333:function:aws-services-lifecycle-pipeline:1",
])
def test_refresh_status_refuses_other_executions(arn):
    with patch.object(api, "refresh_status") as status_fn:
        status, _ = status_and_body(api.handler(
            http_event("GET", f"/refresh/{arn}", path_params={"arn": arn}), None))
    assert status == 400
    status_fn.assert_not_called()
