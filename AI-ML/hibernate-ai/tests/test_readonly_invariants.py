# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read-only v1 invariants — regression guards.

Guards the properties that make v1 safe and correct:
  #1 extended-service runtime safety gate (tag normalization + filter semantics)
  #2 the stack grants NO destructive/mutating action on customer resources (read-only)
  #3 Bedrock fallback no longer masks AccessDenied/Validation (real bugs surface)
  #4 approver gate supports fail-closed when unconfigured
  #6 metrics lookback window is configurable
"""
import importlib
import json
import os

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# #2 — the template grants NO destructive/mutating action on customer resources (read-only)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestExtendedServiceDeny:
    """Read-only v1: the stack grants NO destructive/mutating actions on customer resources,
    so there are no protective Deny statements to assert anymore. Instead we assert the
    stronger invariant: no destructive action appears in any Allow statement in the template."""
    @pytest.fixture(scope="class")
    def all_actions_blob(self):
        cdk = pytest.importorskip("aws_cdk")
        from aws_cdk import assertions
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from stacks import HibernateAIStack

        app = cdk.App()
        stack = HibernateAIStack(app, "HibernateAI",
                                 env=cdk.Environment(account="123456789012", region="us-east-1"))
        tmpl = assertions.Template.from_stack(stack).to_json()
        allows = []
        for res in tmpl.get("Resources", {}).values():
            if res.get("Type") not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                continue
            for s in res.get("Properties", {}).get("PolicyDocument", {}).get("Statement", []):
                if s.get("Effect") == "Allow":
                    allows.append(s)
        return json.dumps(allows)

    @pytest.mark.parametrize("action", [
        "ec2:TerminateInstances", "ec2:StopInstances", "ec2:DeleteVolume",
        "rds:DeleteDBInstance", "rds:DeleteDBCluster", "rds:ModifyDBInstance",
        "ec2:DeleteNatGateway", "elasticloadbalancing:DeleteLoadBalancer",
        "eks:DeleteNodegroup", "workspaces:TerminateWorkspaces", "appstream:DeleteFleet",
        "ec2:RunInstances", "rds:CreateDBInstance", "rds:RestoreDBInstanceFromDBSnapshot",
    ])
    def test_no_destructive_action_granted(self, all_actions_blob, action):
        assert action not in all_actions_blob, \
            f"read-only v1 must not grant {action} anywhere in the stack"


# ─────────────────────────────────────────────────────────────────────────────
# #3 — Bedrock fallback error classification
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestBedrockErrorClassification:
    def test_access_denied_not_in_fallback(self):
        import advisor
        assert "AccessDeniedException" not in advisor._FALLBACK_ERRORS
        assert "ValidationException" not in advisor._FALLBACK_ERRORS

    def test_transient_errors_still_fallback(self):
        import advisor
        for code in ("ThrottlingException", "ModelNotReadyException",
                     "ServiceUnavailableException", "ModelTimeoutException",
                     "ResourceNotFoundException"):
            assert code in advisor._FALLBACK_ERRORS

    def test_access_denied_surfaces_immediately(self, monkeypatch):
        """A non-fallback error should return an error dict, not try every model."""
        import advisor

        calls = {"n": 0}

        def _boom(model_id, prompt):
            calls["n"] += 1
            raise Exception("AccessDeniedException: not authorized")

        monkeypatch.setattr(advisor, "_invoke_one", _boom)
        monkeypatch.setattr(advisor, "_error_code", lambda e: "AccessDeniedException")
        monkeypatch.setattr(advisor, "get_model_chain",
                            lambda: ["m1", "m2", "m3"])
        result = advisor._call_bedrock("prompt")
        assert "error" in result
        # must NOT have iterated the whole chain — surfaces on the first model
        assert calls["n"] == 1, "AccessDenied should surface immediately, not fall through models"


# ─────────────────────────────────────────────────────────────────────────────
# #6 — configurable metrics lookback
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestMetricsLookback:
    def test_default_is_15(self, monkeypatch):
        monkeypatch.delenv("METRICS_LOOKBACK_DAYS", raising=False)
        import advisor
        importlib.reload(advisor)
        assert advisor.METRICS_LOOKBACK_DAYS == 15

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("METRICS_LOOKBACK_DAYS", "30")
        import advisor
        importlib.reload(advisor)
        assert advisor.METRICS_LOOKBACK_DAYS == 30

    def test_clamped_and_safe(self, monkeypatch):
        import advisor
        monkeypatch.setenv("METRICS_LOOKBACK_DAYS", "9999")
        importlib.reload(advisor)
        assert advisor.METRICS_LOOKBACK_DAYS == 90  # upper clamp
        monkeypatch.setenv("METRICS_LOOKBACK_DAYS", "garbage")
        importlib.reload(advisor)
        assert advisor.METRICS_LOOKBACK_DAYS == 15  # bad value -> default
        monkeypatch.setenv("METRICS_LOOKBACK_DAYS", "0")
        importlib.reload(advisor)
        assert advisor.METRICS_LOOKBACK_DAYS == 1  # lower clamp

    def teardown_method(self):
        # restore the module to env-default state for other tests
        os.environ.pop("METRICS_LOOKBACK_DAYS", None)
        import advisor
        importlib.reload(advisor)


# ─────────────────────────────────────────────────────────────────────────────
# #4 — approver gate fail-closed option
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestApproverGate:
    def test_read_only_always_allowed(self):
        import approvals
        allowed, _ = approvals.authorize("analyze", {}, config={})
        assert allowed is True

    def test_unconfigured_default_fails_closed(self):
        # CSR remediation: the SECURE default is fail-closed — privileged actions are
        # DENIED when no approver allowlist is configured (previously fell open).
        import approvals
        allowed, reason = approvals.authorize("save", {}, config={})
        assert allowed is False
        assert "fail-closed" in reason

    def test_unconfigured_demo_fallopen_removed_via_config(self):
        # Read-only v1 / CSR LOW: the demo fall-open switch was removed entirely.
        # Setting it in config has no effect — privileged stays fail-closed.
        import approvals
        allowed, reason = approvals.authorize(
            "save", {}, config={"allow_unconfigured_privileged": True})
        assert allowed is False
        assert "fail-closed" in reason

    def test_unconfigured_demo_fallopen_removed_via_env(self, monkeypatch):
        import approvals
        monkeypatch.setenv("ALLOW_UNCONFIGURED_PRIVILEGED", "true")
        allowed, _ = approvals.authorize("save", {}, config={})
        assert allowed is False  # env switch no longer relaxes the fail-closed default

    def test_allowlist_denies_unknown_caller(self):
        import approvals
        allowed, _ = approvals.authorize(
            "save", {}, config={"approvers": ["arn:aws:iam::1:role/Approver"]})
        assert allowed is False  # no forwarded principal => denied

    def test_allowlist_allows_matching_principal(self):
        import approvals
        event = {"principal": "arn:aws:iam::1:role/Approver"}
        allowed, _ = approvals.authorize(
            "save", event, config={"approvers": ["arn:aws:iam::1:role/Approver"]})
        assert allowed is True


# ─────────────────────────────────────────────────────────────────────────────
# #1 — extended-service tag normalization (safety gate inputs)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestExtendedTagNormalization:
    def test_safety_protects_dict_tagged_prod_resource(self):
        """A dict-style-tagged (EKS/AppStream) prod resource must be classified protected
        once its tags are normalized to list form (the shape safety.is_protected_failsafe
        expects)."""
        import safety

        # dict-style tags like EKS/AppStream, normalized to list-of-{Key,Value}
        raw = {"Environment": "production", "project_name": "p"}
        normalized = [{"Key": k, "Value": v} for k, v in raw.items()]
        protected, reason = safety.is_protected_failsafe(normalized, config={})
        assert protected is True

    def test_safety_untagged_is_failsafe_protected(self):
        import safety
        protected, _ = safety.is_protected_failsafe([], config={})
        assert protected is True  # unknown classification => protected
