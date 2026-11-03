# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Least-privilege IAM posture guards (cross-cutting sweep).

Synthesizes the CDK stack and asserts the security-critical statements are present, so a
future refactor can't silently remove the protective Deny or re-broaden PassRole.
"""
import json

import pytest

pytest.importorskip("aws_cdk")

import aws_cdk as cdk
from aws_cdk import assertions

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stacks import HibernateAIStack  # noqa: E402


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = HibernateAIStack(app, "HibernateAI",
                             env=cdk.Environment(account="123456789012", region="us-east-1"))
    return assertions.Template.from_stack(stack).to_json()


def _all_statements(template_json):
    stmts = []
    for res in template_json.get("Resources", {}).values():
        if res.get("Type") != "AWS::IAM::Policy":
            continue
        doc = res.get("Properties", {}).get("PolicyDocument", {})
        stmts.extend(doc.get("Statement", []))
    return stmts


@pytest.mark.unit
class TestLeastPrivilege:
    def test_protective_deny_exists(self, template_json):
        # Read-only v1: no destructive actions are granted, so there are no protective Deny
        # statements. Assert the stronger invariant instead — no destructive/mutating action
        # appears in any Allow statement in the stack.
        allows = [s for s in _all_statements(template_json) if s.get("Effect") == "Allow"]
        blob = json.dumps(allows)
        for action in ("ec2:TerminateInstances", "ec2:StopInstances", "ec2:DeleteVolume",
                       "rds:DeleteDBInstance", "rds:DeleteDBCluster", "rds:ModifyDBInstance",
                       "ec2:RunInstances", "rds:CreateDBInstance"):
            assert action not in blob, f"read-only v1 must not grant {action}"

    def test_passrole_is_not_wildcard(self, template_json):
        for s in _all_statements(template_json):
            actions = s.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "iam:PassRole" in actions and s.get("Effect") == "Allow":
                # must carry a condition (PassedToService) — not an unconditional * grant
                assert "Condition" in s, "iam:PassRole must be conditioned, not unconditional"
                assert "iam:PassedToService" in json.dumps(s["Condition"])

    def test_bedrock_not_wildcard(self, template_json):
        for s in _all_statements(template_json):
            actions = s.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "bedrock:InvokeModel" in actions:
                assert s.get("Resource") != "*", "bedrock:InvokeModel must be an ARN allowlist, not *"

    def test_destructive_actions_gated_by_enrollment_tag(self, template_json):
        """Path A: any Allow granting a destructive action (Terminate/Delete/Stop) on
        Resource:* MUST be conditioned on the HibernateAI enrollment tag. Guards against a
        future refactor silently re-broadening to an unconditioned destructive wildcard."""
        DESTRUCTIVE = (
            "ec2:TerminateInstances", "ec2:StopInstances", "ec2:DeleteVolume", "ec2:DeleteSnapshot",
            "ec2:DeleteNatGateway",
            "rds:DeleteDBInstance", "rds:DeleteDBCluster", "rds:DeleteDBSnapshot",
            "rds:DeleteDBClusterSnapshot",
            "redshift:DeleteCluster", "es:DeleteDomain", "elasticache:DeleteCacheCluster",
            "sagemaker:DeleteEndpoint", "elasticmapreduce:TerminateJobFlows", "eks:DeleteNodegroup",
            "elasticloadbalancing:DeleteLoadBalancer", "fsx:DeleteFileSystem",
            "workspaces:TerminateWorkspaces", "appstream:DeleteFleet",
        )
        for s in _all_statements(template_json):
            if s.get("Effect") != "Allow":
                continue
            actions = s.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if any(a in DESTRUCTIVE for a in actions):
                cond = json.dumps(s.get("Condition", {}))
                assert "aws:ResourceTag/HibernateAI" in cond, (
                    f"destructive Allow must be gated by aws:ResourceTag/HibernateAI; "
                    f"offending actions: {[a for a in actions if a in DESTRUCTIVE]}")


    def test_role_cannot_tag_existing_resources(self, template_json):
        """External-enrollment: the role must NOT be able to tag pre-existing
        resources (which would let it self-enroll HibernateAI=true and then destroy them).
        Any ec2:CreateTags Allow must be gated by ec2:CreateAction (create-time only), and
        there must be NO rds:AddTagsToResource Allow at all."""
        for s in _all_statements(template_json):
            if s.get("Effect") != "Allow":
                continue
            actions = s.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "ec2:CreateTags" in actions:
                cond = json.dumps(s.get("Condition", {}))
                assert "ec2:CreateAction" in cond, \
                    "ec2:CreateTags must be gated by ec2:CreateAction (create-time only), " \
                    "never a standalone grant that could tag existing resources"
            assert "rds:AddTagsToResource" not in actions, \
                "role must not be able to tag existing RDS resources (self-enrollment risk)"
