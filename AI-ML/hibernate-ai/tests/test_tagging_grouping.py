# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for flexible tags + dynamic grouping (review #1, #2)."""
import pytest

import config as hib_config


def _run(ec2, subnet_id, tags):
    imgs = ec2.describe_images(Owners=["amazon"])["Images"]
    ami = imgs[0]["ImageId"] if imgs else "ami-12345678"
    return ec2.run_instances(
        ImageId=ami, MinCount=1, MaxCount=1, InstanceType="t3.micro", SubnetId=subnet_id,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )["Instances"][0]["InstanceId"]


@pytest.mark.unit
class TestResolveGroupKey:
    def test_empty_config_uses_project_name(self):
        assert hib_config.resolve_group_key([{"Key": "project_name", "Value": "alpha"}], {}) == "alpha"

    def test_untagged_when_no_key_present(self):
        assert hib_config.resolve_group_key([{"Key": "Name", "Value": "x"}], {}) == "__untagged__"

    def test_fallback_order(self):
        cfg = {"grouping_tag_keys": ["project_name", "App_name", "asset_id"]}
        # project_name absent, App_name present -> App_name wins
        tags = [{"Key": "App_name", "Value": "billing"}, {"Key": "asset_id", "Value": "A1"}]
        assert hib_config.resolve_group_key(tags, cfg) == "billing"

    def test_grouping_dimension_overrides(self):
        cfg = {"grouping_dimension": "cost_center", "grouping_tag_keys": ["project_name"]}
        tags = [{"Key": "project_name", "Value": "alpha"}, {"Key": "cost_center", "Value": "CC-9"}]
        assert hib_config.resolve_group_key(tags, cfg) == "CC-9"

    def test_dict_form_tags(self):
        cfg = {"grouping_tag_keys": ["project_name"]}
        assert hib_config.resolve_group_key({"project_name": "alpha"}, cfg) == "alpha"


@pytest.mark.unit
class TestResolveEnvironment:
    def test_fallback_keys(self):
        cfg = {"environment_tag_keys": ["environment", "Stage"]}
        assert hib_config.resolve_environment([{"Key": "Stage", "Value": "prod"}], cfg) == "prod"

    def test_none_when_absent(self):
        assert hib_config.resolve_environment([{"Key": "Name", "Value": "x"}], {}) is None


@pytest.mark.unit
class TestEffectiveGroupingKey:
    def test_default(self):
        assert hib_config.effective_grouping_key({}) == "project_name"

    def test_dimension(self):
        assert hib_config.effective_grouping_key({"grouping_dimension": "team"}) == "team"

    def test_first_of_keys(self):
        assert hib_config.effective_grouping_key({"grouping_tag_keys": ["App_name", "x"]}) == "App_name"


@pytest.mark.unit
class TestDiscoveryGrouping:
    def test_empty_config_groups_by_project_name(self, ec2, rds, state_table, default_vpc):
        import index

        _run(ec2, default_vpc["subnet_id"], [{"Key": "project_name", "Value": "alpha"}])
        groups = index.discover_projects()
        assert "alpha" in groups

    def test_groups_by_alternate_dimension(self, ec2, rds, state_table, default_vpc):
        import index

        hib_config.save_config({"grouping_dimension": "cost_center",
                                "grouping_tag_keys": ["cost_center", "project_name"]})
        _run(ec2, default_vpc["subnet_id"],
             [{"Key": "project_name", "Value": "alpha"}, {"Key": "cost_center", "Value": "CC-9"}])
        groups = index.discover_projects()
        assert "CC-9" in groups
        assert "alpha" not in groups  # grouped by cost_center, not project_name


@pytest.mark.unit
class TestSavePathFilter:
    def test_discovery_uses_configured_key(self, ec2, rds, state_table, default_vpc):
        # Read-only v1: the save-path discovery helper (index._find_ec2) was removed along
        # with the destructive save/restore flow. The configured-grouping-key behavior it
        # covered is still exercised by the advisory discovery path (discover_projects).
        import index

        hib_config.save_config({"grouping_dimension": "cost_center"})
        _run(ec2, default_vpc["subnet_id"],
             [{"Key": "cost_center", "Value": "CC-9"}, {"Key": "environment", "Value": "dev"}])
        groups = index.discover_projects()
        assert "CC-9" in groups
        assert len(groups["CC-9"]["ec2"]) == 1


@pytest.mark.unit
class TestConfigActions:
    def test_set_config_merges(self, state_table):
        import index, json

        out = json.loads(index.handle_set_config({"config": {"grouping_dimension": "team"}}))
        assert out["success"]
        assert hib_config.load_config()["grouping_dimension"] == "team"


@pytest.mark.unit
class TestOnboardingProposal:
    def test_proposes_detected_key(self, ec2, default_vpc):
        import onboarding

        _run(ec2, default_vpc["subnet_id"], [{"Key": "App_name", "Value": "billing"}])
        proposal = onboarding.propose_tag_mapping(ec2_client=ec2)
        assert "App_name" in proposal["grouping_tag_keys"]
        assert proposal["requires_our_convention"] is False

    def test_falls_back_to_convention_when_nothing_usable(self, ec2, default_vpc):
        import onboarding

        _run(ec2, default_vpc["subnet_id"], [{"Key": "Name", "Value": "x"}])
        proposal = onboarding.propose_tag_mapping(ec2_client=ec2)
        assert proposal["grouping_tag_keys"] == ["project_name"]
        assert proposal["requires_our_convention"] is True
