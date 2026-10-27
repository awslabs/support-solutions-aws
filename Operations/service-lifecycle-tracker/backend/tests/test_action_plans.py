# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Action plans against a table with the deployed key schema (moto). Offline.

The plans table has two GSIs (cdk/lib/data-stack.ts, terraform/data.tf):
owner-index (owner, created_at) and plan-status-index (plan_status, target_date).
DynamoDB refuses an empty string for an index key, so a plan without a target
date must be stored without the attribute.
"""
import os

os.environ.setdefault("AWS_REGION", "eu-central-1")

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

import action_plans  # noqa: E402

UI_CREATE = {  # what PlanOfAction.tsx / MyResources.tsx send when no date is picked
    "service_name": "lambda", "item_id": "runtimes#python3.9", "item_name": "Python 3.9",
    "owner": "team-x", "plan_status": "not_started", "priority": "medium", "target_date": "", "notes": "",
}


@pytest.fixture
def plans_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
        ddb.create_table(
            TableName=action_plans.ACTION_PLAN_TABLE,
            KeySchema=[{"AttributeName": "plan_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("plan_id", "owner", "created_at", "plan_status", "target_date")],
            GlobalSecondaryIndexes=[
                {"IndexName": "owner-index", "Projection": {"ProjectionType": "ALL"},
                 "KeySchema": [{"AttributeName": "owner", "KeyType": "HASH"},
                               {"AttributeName": "created_at", "KeyType": "RANGE"}]},
                {"IndexName": "plan-status-index", "Projection": {"ProjectionType": "ALL"},
                 "KeySchema": [{"AttributeName": "plan_status", "KeyType": "HASH"},
                               {"AttributeName": "target_date", "KeyType": "RANGE"}]},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(action_plans.ACTION_PLAN_TABLE)


def _stored(table, plan_id):
    return table.get_item(Key={"plan_id": plan_id})["Item"]


@pytest.mark.parametrize("date", ["", None])
def test_a_plan_without_a_target_date_can_be_created(plans_table, date):
    out = action_plans.create_action_plan({**UI_CREATE, "target_date": date})
    assert out["success"], out
    assert out["plan"]["target_date"] == ""                       # the UI still gets a string
    assert "target_date" not in _stored(plans_table, out["plan"]["plan_id"])


def test_a_plan_with_no_target_date_key_at_all_can_be_created(plans_table):
    data = {k: v for k, v in UI_CREATE.items() if k != "target_date"}
    assert action_plans.create_action_plan(data)["success"]


def test_a_dated_plan_is_unchanged(plans_table):
    out = action_plans.create_action_plan({**UI_CREATE, "target_date": "2026-12-31"})
    assert out["success"]
    assert _stored(plans_table, out["plan"]["plan_id"])["target_date"] == "2026-12-31"


def test_reads_return_an_empty_target_date_for_undated_plans(plans_table):
    plan_id = action_plans.create_action_plan(UI_CREATE)["plan"]["plan_id"]
    assert action_plans.get_action_plan(plan_id)["plan"]["target_date"] == ""
    listed = action_plans.list_action_plans()["plans"]
    assert [p["target_date"] for p in listed] == [""]


def test_the_date_can_be_set_then_cleared(plans_table):
    plan_id = action_plans.create_action_plan(UI_CREATE)["plan"]["plan_id"]
    out = action_plans.update_action_plan(plan_id, {"target_date": "2027-01-15", "plan_status": "in_progress"})
    assert out["success"] and out["plan"]["target_date"] == "2027-01-15"
    out = action_plans.update_action_plan(plan_id, {"target_date": "", "notes": "date dropped"})
    assert out["success"], out
    assert out["plan"]["target_date"] == "" and out["plan"]["notes"] == "date dropped"
    assert "target_date" not in _stored(plans_table, plan_id)


def test_the_edit_form_payload_for_an_undated_plan(plans_table):
    """The edit modal sends every field back, including the empty date."""
    plan_id = action_plans.create_action_plan(UI_CREATE)["plan"]["plan_id"]
    out = action_plans.update_action_plan(plan_id, {
        "owner": "team-y", "plan_status": "blocked", "priority": "high", "target_date": "", "notes": "waiting"})
    assert out["success"], out
    assert out["plan"]["owner"] == "team-y" and out["plan"]["target_date"] == ""


def test_status_filter_includes_undated_plans(plans_table):
    dated = action_plans.create_action_plan({**UI_CREATE, "plan_status": "blocked", "target_date": "2026-11-30"})
    undated = action_plans.create_action_plan({**UI_CREATE, "plan_status": "blocked"})
    action_plans.create_action_plan({**UI_CREATE, "plan_status": "completed"})
    out = action_plans.list_action_plans({"plan_status": "blocked"})
    assert out["success"]
    assert {p["plan_id"] for p in out["plans"]} == {dated["plan"]["plan_id"], undated["plan"]["plan_id"]}


def test_owner_filter(plans_table):
    mine = action_plans.create_action_plan({**UI_CREATE, "owner": "me"})["plan"]["plan_id"]
    action_plans.create_action_plan({**UI_CREATE, "owner": "someone-else"})
    assert [p["plan_id"] for p in action_plans.list_action_plans({"owner": "me"})["plans"]] == [mine]


@pytest.mark.parametrize("updates", [{"owner": ""}, {"plan_status": ""}, {"target_date": 20261231}])
def test_index_keys_get_a_clear_error_instead_of_a_dynamodb_one(plans_table, updates):
    plan_id = action_plans.create_action_plan(UI_CREATE)["plan"]["plan_id"]
    out = action_plans.update_action_plan(plan_id, updates)
    assert out["success"] is False and "ValidationException" not in out["error"]
    assert _stored(plans_table, plan_id)["owner"] == "team-x"


def test_a_non_string_target_date_is_refused_on_create(plans_table):
    out = action_plans.create_action_plan({**UI_CREATE, "target_date": 20261231})
    assert out["success"] is False and "target_date" in out["error"]
