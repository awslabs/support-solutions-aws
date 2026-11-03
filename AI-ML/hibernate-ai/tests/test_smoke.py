# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Smoke tests — prove the test harness works and anchor current baseline behavior.

These are intentionally minimal; focused tests cover each change.
The backward-compat assertions here are the anchor the review work must not regress:
with no config, discovery groups by ``project_name`` exactly like today.
"""
import pytest


@pytest.mark.unit
def test_widget_modules_import():
    """All Lambda modules import by bare name (matches the Lambda runtime sys.path)."""
    import index
    import rules
    import advisor
    import multi_account
    import onboarding

    assert callable(index.handler)
    assert callable(index.discover_projects)
    assert callable(index.handle_analyze)


@pytest.mark.unit
def test_read_only_actions_perform_no_destructive_work():
    """Read-only (advisory) v1 contract: no destructive action succeeds, and the
    handler carries no hibernate/restore/schedule implementation.

    Two gates enforce this, both left intact:
      * Privileged actions (save/restore/set_schedule/remove_schedule/approve) are
        blocked fail-closed by the authorization gate (no approver allowlist) and
        return success == False.
      * Non-privileged destructive actions (prefreeze/snooze) fall through to the
        read-only gate, which returns read_only == true.
    Either way nothing is acted on, and the handlers that used to do the work are gone.
    """
    import index
    import json

    # The destructive handlers must no longer exist on the module.
    for removed in ("handle_save", "handle_restore", "handle_prefreeze",
                    "handle_set_schedule", "handle_remove_schedule", "handle_approve"):
        assert not hasattr(index, removed), f"{removed} should have been removed"

    for action in ("save", "restore", "set_schedule", "remove_schedule",
                   "prefreeze", "snooze", "approve"):
        payload = json.loads(index.handler({"action": action}, None))
        assert payload.get("success") is False, f"{action} must not succeed"

    # prefreeze/snooze are not approver-gated, so they reach the read-only gate and
    # explicitly advertise the read-only contract.
    for action in ("prefreeze", "snooze"):
        payload = json.loads(index.handler({"action": action}, None))
        assert payload.get("read_only") is True, f"{action} should report read_only"


@pytest.mark.unit
def test_is_protected_matches_prod_tags():
    import index

    assert index._is_protected([{"Key": "Environment", "Value": "production"}]) is True
    assert index._is_protected([{"Key": "Environment", "Value": "PROD"}]) is True
    assert index._is_protected([{"Key": "CostSaverProtect", "Value": "true"}]) is True
    assert index._is_protected([{"Key": "Environment", "Value": "dev"}]) is False
    assert index._is_protected([]) is False
    assert index._is_protected(None) is False


@pytest.mark.unit
def test_get_tag_resolves_and_defaults():
    import index

    tags = [{"Key": "project_name", "Value": "alpha"}, {"Key": "Name", "Value": "web"}]
    assert index.get_tag(tags, "project_name") == "alpha"
    assert index.get_tag(tags, "missing") is None
    assert index.get_tag(None, "project_name") is None


@pytest.mark.unit
def test_discover_groups_by_project_name_baseline(ec2, rds, state_table, default_vpc):
    """Baseline / backward-compat anchor: EC2 tagged project_name lands in that group."""
    import index

    images = ec2.describe_images(Owners=["amazon"])["Images"]
    ami = images[0]["ImageId"] if images else "ami-12345678"
    ec2.run_instances(
        ImageId=ami,
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.micro",
        SubnetId=default_vpc["subnet_id"],
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": "project_name", "Value": "alpha"}]}
        ],
    )

    groups = index.discover_projects()

    assert "alpha" in groups
    assert len(groups["alpha"]["ec2"]) == 1
    assert groups["alpha"]["ec2"][0]["type"] == "t3.micro"


@pytest.mark.unit
def test_list_saved_projects_excludes_reserved_keys(state_table):
    """Reserved __key__ control records must not surface as phantom projects.

    Regression test: __snooze__/__config__/__rules__/__approval__/__retry__ rows in the
    overloaded state table were rendering as '0 resources saved' cards in the dashboard.
    """
    import index

    # Real projects
    state_table.put_item(Item={"project_name": "HAI Demo", "state": {"ec2": []}})
    state_table.put_item(Item={"project_name": "Testing", "state": {"ec2": []}})
    # Reserved control records that must be filtered out
    for k in ("__config__", "__rules__", "__snooze__abc123", "__approval__def456",
              "__retry__HAI Demo", "__untagged__"):
        state_table.put_item(Item={"project_name": k, "state": {}})

    saved = index._list_saved_projects()

    assert set(saved) == {"HAI Demo", "Testing"}
    assert not any(p.startswith("__") for p in saved)


@pytest.mark.unit
def test_list_saved_projects_paginates(state_table, monkeypatch):
    """_list_saved_projects must follow LastEvaluatedKey, not drop items past page 1."""
    import index

    pages = [
        {"Items": [{"project_name": "P1"}, {"project_name": "__config__"}],
         "LastEvaluatedKey": {"project_name": "P1"}},
        {"Items": [{"project_name": "P2"}]},  # no LastEvaluatedKey => last page
    ]
    calls = {"n": 0}

    class _FakeTable:
        def scan(self, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            # second call must carry the ExclusiveStartKey from page 1
            if i == 1:
                assert kwargs.get("ExclusiveStartKey") == {"project_name": "P1"}
            return pages[i]

    monkeypatch.setattr(index, "_get_table", lambda: _FakeTable())
    saved = index._list_saved_projects()
    assert set(saved) == {"P1", "P2"}  # both pages aggregated, reserved key filtered
    assert calls["n"] == 2
