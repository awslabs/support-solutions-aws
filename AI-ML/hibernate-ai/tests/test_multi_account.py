# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for multi-account automation."""
import pytest

import config as hib_config
import multi_account


@pytest.mark.unit
class TestAccountConfigNoClobber:
    def test_save_accounts_preserves_other_config(self, state_table):
        # Seed a deny-list + threshold, then save accounts — must NOT wipe them.
        hib_config.save_config({"cost_threshold_monthly_usd": 300,
                                "deny_list": {"account_ids": ["111"]}})
        multi_account.save_account_config([{"id": "222", "name": "dev"}])
        cfg = hib_config.load_config()
        assert cfg["accounts"] == [{"id": "222", "name": "dev"}]
        assert cfg["cost_threshold_monthly_usd"] == 300           # preserved
        assert cfg["deny_list"]["account_ids"] == ["111"]         # preserved

    def test_discover_all_accounts_reads_config(self, state_table):
        multi_account.save_account_config([{"id": "333", "name": "prod"}])
        assert multi_account.discover_all_accounts() == [{"id": "333", "name": "prod"}]


@pytest.mark.unit
class TestStackSetTemplate:
    def test_template_is_valid_shape(self):
        t = multi_account.get_stackset_template()
        assert t["AWSTemplateFormatVersion"] == "2010-09-09"
        assert "HibernateAIRole" in t["Resources"]
        assert t["Resources"]["HibernateAIRole"]["Type"] == "AWS::IAM::Role"
        assert "ManagementAccountId" in t["Parameters"]


@pytest.mark.unit
class TestAggregation:
    def test_aggregates_current_account(self, ec2, rds, state_table, default_vpc):
        import index
        import boto3

        imgs = ec2.describe_images(Owners=["amazon"])["Images"]
        ami = imgs[0]["ImageId"] if imgs else "ami-12345678"
        ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, InstanceType="t3.micro",
                          SubnetId=default_vpc["subnet_id"],
                          TagSpecifications=[{"ResourceType": "instance",
                                              "Tags": [{"Key": "project_name", "Value": "alpha"}]}])
        acct = boto3.client("sts").get_caller_identity()["Account"]
        multi_account.save_account_config([{"id": acct, "name": "current"}])

        agg = multi_account.aggregate_all_accounts(index.discover_projects)
        assert any(k.endswith(":alpha") for k in agg["projects"])
        assert all(v.get("account_id") == acct for v in agg["projects"].values())

    def test_other_account_reports_error_when_not_session_aware(self, state_table):
        def fake_discover():
            return {"alpha": {"ec2": []}}

        multi_account.save_account_config([{"id": "999999999999", "name": "other"}])
        agg = multi_account.aggregate_all_accounts(fake_discover)
        assert any(e["account"] == "999999999999" for e in agg["errors"])


@pytest.mark.unit
class TestDeployStackSet:
    def test_deploy_creates_stackset(self, aws, state_table):
        import boto3

        result = multi_account.deploy_stackset_to_ous(
            ["ou-abcd-1234"], management_account_id="123456789012",
            regions=["us-east-1"], cfn_client=boto3.client("cloudformation", region_name="us-east-1"))
        assert result["stackset"] == "HibernateAI-CrossAccount"
        assert result["ous"] == ["ou-abcd-1234"]


@pytest.mark.unit
class TestDeployStackSetAuthz:
    def test_deploy_stackset_is_privileged(self):
        import approvals

        assert approvals.is_privileged("deploy_stackset")
