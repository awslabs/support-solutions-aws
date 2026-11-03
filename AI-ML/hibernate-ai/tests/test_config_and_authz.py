# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Safety-gate and authorization tests for HibernateAI's read-only (advisory) v1.

The destructive save/restore handlers were removed from this read-only branch, so the
end-to-end handle_save gate tests no longer apply. What remains here still protects the
advisory contract:
  * config accessor defaults/merge behavior (hib_config)
  * authorization: privileged (destructive) actions fail closed with no approver allowlist
"""
import pytest

import config as hib_config


@pytest.mark.unit
class TestConfigAccessor:
    def test_empty_config_returns_defaults(self, state_table):
        cfg = hib_config.load_config()
        assert cfg["grouping_tag_keys"] == ["project_name"]
        assert cfg["cost_threshold_monthly_usd"] == 0.0
        assert cfg["deny_list"]["account_ids"] == []
        assert cfg["iac_managed_policy"] == "stop_start"

    def test_saved_config_merges_over_defaults(self, state_table):
        hib_config.save_config({"cost_threshold_monthly_usd": 250})
        cfg = hib_config.load_config()
        assert cfg["cost_threshold_monthly_usd"] == 250
        assert cfg["grouping_tag_keys"] == ["project_name"]  # default still present


@pytest.mark.unit
class TestAuthz:
    def test_analyze_is_read_only(self):
        import approvals

        assert not approvals.is_privileged("analyze")
        assert approvals.is_privileged("save")
        assert approvals.is_privileged("restore")

    def test_authorize_denies_when_no_allowlist(self):
        # Fail-closed by default — no allowlist means privileged denied.
        import approvals

        allowed, _ = approvals.authorize("save", {}, config={"approvers": []})
        assert not allowed

    def test_authorize_unconfigured_always_fails_closed(self):
        # The demo fall-open switch was removed. No allowlist =>
        # privileged actions are DENIED, with no opt-in to relax it.
        import approvals, os

        os.environ.pop("ALLOW_UNCONFIGURED_PRIVILEGED", None)
        allowed, _ = approvals.authorize(
            "save", {}, config={"approvers": [], "allow_unconfigured_privileged": True})
        assert not allowed  # the demo opt-in no longer has any effect

    def test_authorize_blocks_unknown_caller_with_allowlist(self):
        import approvals

        allowed, _ = approvals.authorize("save", {}, config={"approvers": ["admin-group"]})
        assert not allowed
