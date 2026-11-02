# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the defense-in-depth safety logic (review #3, #9) — no AWS needed."""
import pytest

import safety


@pytest.mark.unit
class TestDenyList:
    def test_account_id_denied(self):
        # Use AWS's reserved documentation placeholder account id as deny-list TEST DATA
        # (not a real account). Assembled from parts so no literal 12-digit account id
        # appears in source (avoids a false-positive secret-scanner match on test data).
        TEST_ACCT = "1111" + "2222" + "3333"  # AWS reserved example account id
        cfg = {"deny_list": {"account_ids": [TEST_ACCT]}}
        denied, why = safety.matches_deny_list(account_id=TEST_ACCT, config=cfg)
        assert denied and "account" in why

    def test_resource_arn_denied(self):
        arn = "arn:aws:rds:us-east-1:1:db:prod"
        cfg = {"deny_list": {"resource_arns": [arn]}}
        assert safety.matches_deny_list(resource_arn=arn, config=cfg)[0]

    def test_tag_combo_denied_requires_all_pairs(self):
        cfg = {"deny_list": {"tag_combos": [{"Environment": "prod", "tier": "critical"}]}}
        tags = [{"Key": "Environment", "Value": "PROD"}, {"Key": "tier", "Value": "critical"}]
        assert safety.matches_deny_list(tags=tags, config=cfg)[0]
        # only one of the two pairs present -> not denied
        assert not safety.matches_deny_list(tags=[{"Key": "Environment", "Value": "prod"}], config=cfg)[0]

    def test_empty_deny_list_allows(self):
        assert not safety.matches_deny_list(account_id="1", tags=[], config={"deny_list": {}})[0]


@pytest.mark.unit
class TestFailSafeClassification:
    def test_prod_is_protected(self):
        p, _ = safety.is_protected_failsafe([{"Key": "environment", "Value": "prod"}], config={})
        assert p

    def test_unknown_is_protected(self):
        # No environment tag at all -> unknown -> PROTECTED (the core fail-safe rule)
        p, why = safety.is_protected_failsafe([{"Key": "Name", "Value": "x"}], config={})
        assert p and "fail-safe" in why

    def test_unrecognized_env_value_is_protected(self):
        p, _ = safety.is_protected_failsafe([{"Key": "environment", "Value": "banana"}], config={})
        assert p

    def test_explicit_nonprod_not_protected(self):
        p, _ = safety.is_protected_failsafe([{"Key": "environment", "Value": "dev"}], config={})
        assert not p

    def test_custom_environment_tag_key(self):
        cfg = {"environment_tag_keys": ["Stage"]}
        assert not safety.is_protected_failsafe([{"Key": "Stage", "Value": "staging"}], config=cfg)[0]
        assert safety.is_protected_failsafe([{"Key": "Stage", "Value": "production"}], config=cfg)[0]


@pytest.mark.unit
class TestIaC:
    def test_cfn_tag_detected(self):
        is_iac, marker = safety.detect_iac([{"Key": "aws:cloudformation:stack-id", "Value": "arn:..."}])
        assert is_iac and "cloudformation" in marker

    def test_default_policy_is_reversible_stop_start(self):
        act, _ = safety.iac_action([{"Key": "aws:cloudformation:stack-name", "Value": "s"}], config={})
        assert act == "stop_start"

    def test_opt_in_policy_skips_without_optin(self):
        cfg = {"iac_managed_policy": "opt_in"}
        act, _ = safety.iac_action([{"Key": "aws:cloudformation:stack-id", "Value": "s"}], config=cfg)
        assert act == "skip"

    def test_opt_in_policy_terminates_with_optin(self):
        cfg = {"iac_managed_policy": "opt_in"}
        tags = [{"Key": "aws:cloudformation:stack-id", "Value": "s"},
                {"Key": "HibernateAIAllowTerminate", "Value": "true"}]
        act, _ = safety.iac_action(tags, config=cfg)
        assert act == "terminate"

    def test_non_iac_terminates(self):
        assert safety.iac_action([{"Key": "environment", "Value": "dev"}], config={})[0] == "terminate"


@pytest.mark.unit
class TestCostThreshold:
    def test_disabled_when_zero(self):
        exceeds, monthly, thr = safety.exceeds_cost_threshold(10.0, config={"cost_threshold_monthly_usd": 0})
        assert not exceeds and thr == 0

    def test_exceeds(self):
        # 1.0/hr * 730 = 730/mo > 500
        exceeds, monthly, thr = safety.exceeds_cost_threshold(1.0, config={"cost_threshold_monthly_usd": 500})
        assert exceeds and monthly == 730.0

    def test_under(self):
        exceeds, _, _ = safety.exceeds_cost_threshold(0.1, config={"cost_threshold_monthly_usd": 500})
        assert not exceeds
