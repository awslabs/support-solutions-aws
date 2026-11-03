# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for Bedrock model abstraction/fallback and pre-restore network-drift handling.

Read-only v1: there is no save/restore in the shipped code. The network-drift helper is
retained (it would support the future restore add-on) and is unit-tested here alongside the
model-chain and fallback logic."""
import pytest


@pytest.mark.unit
class TestModelChain:
    def test_default_chain(self, monkeypatch):
        import advisor

        monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
        monkeypatch.delenv("BEDROCK_MODEL_FALLBACKS", raising=False)
        # no DynamoDB config -> defaults
        monkeypatch.setattr(advisor, "get_model_chain",
                            advisor.get_model_chain)  # ensure real fn
        chain = advisor.get_model_chain()
        assert chain[0] == "anthropic.claude-3-haiku-20240307-v1:0"
        assert len(chain) >= 2  # has fallbacks

    def test_env_override(self, monkeypatch):
        import advisor

        monkeypatch.setenv("BEDROCK_MODEL_ID", "my.primary")
        monkeypatch.setenv("BEDROCK_MODEL_FALLBACKS", "my.fb1, my.fb2")
        chain = advisor.get_model_chain()
        assert chain[0] == "my.primary"
        assert "my.fb1" in chain and "my.fb2" in chain

    def test_no_duplicates(self, monkeypatch):
        import advisor

        monkeypatch.setenv("BEDROCK_MODEL_ID", "dup")
        monkeypatch.setenv("BEDROCK_MODEL_FALLBACKS", "dup,other")
        chain = advisor.get_model_chain()
        assert chain.count("dup") == 1


class _FakeError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.mark.unit
class TestFallback:
    def test_falls_back_to_second_model_on_throttling(self, monkeypatch):
        import advisor

        monkeypatch.setenv("BEDROCK_MODEL_ID", "primary")
        monkeypatch.setenv("BEDROCK_MODEL_FALLBACKS", "secondary")
        calls = []

        def fake_invoke(model_id, prompt):
            calls.append(model_id)
            if model_id == "primary":
                raise _FakeError("ThrottlingException")
            return {"recommendations": [], "summary": "ok"}

        # retry primary once (throttle) then fall back
        monkeypatch.setattr(advisor, "_invoke_one", fake_invoke)
        monkeypatch.setattr(advisor.time, "sleep", lambda *_: None)
        out = advisor._call_bedrock("prompt")
        assert out["summary"] == "ok"
        assert out.get("_model_used") == "secondary"
        assert calls[0] == "primary" and "secondary" in calls

    def test_all_models_fail_returns_structured_error(self, monkeypatch):
        import advisor

        monkeypatch.setenv("BEDROCK_MODEL_ID", "a")
        monkeypatch.setenv("BEDROCK_MODEL_FALLBACKS", "b")

        def always_fail(model_id, prompt):
            raise _FakeError("ModelNotReadyException")

        monkeypatch.setattr(advisor, "_invoke_one", always_fail)
        out = advisor._call_bedrock("prompt")
        assert "error" in out
        assert out["models_tried"] == ["a", "b"]

    def test_unknown_error_stops_immediately(self, monkeypatch):
        import advisor

        monkeypatch.setenv("BEDROCK_MODEL_ID", "a")
        monkeypatch.setenv("BEDROCK_MODEL_FALLBACKS", "b")
        calls = []

        def fail_unknown(model_id, prompt):
            calls.append(model_id)
            raise _FakeError("SomethingWeird")

        monkeypatch.setattr(advisor, "_invoke_one", fail_unknown)
        out = advisor._call_bedrock("prompt")
        assert "error" in out
        assert calls == ["a"]  # did NOT try b for a non-fallback error


@pytest.mark.unit
class TestNetworkDrift:
    def test_no_drift_when_sg_and_subnet_exist(self, ec2, default_vpc):
        import network

        sg = ec2.create_security_group(GroupName="g1", Description="d", VpcId=default_vpc["vpc_id"])["GroupId"]
        cfg = {"security_group_ids": [sg], "subnet_id": default_vpc["subnet_id"]}
        v = network.validate_restore_network(cfg, ec2_client=ec2)
        assert v["ok"]
        assert v["reconciled"]["SubnetId"] == default_vpc["subnet_id"]
        assert v["reconciled"]["SecurityGroupIds"] == [sg]

    def test_missing_sg_is_dropped(self, ec2, default_vpc):
        import network

        cfg = {"security_group_ids": ["sg-doesnotexist0000"], "subnet_id": default_vpc["subnet_id"]}
        v = network.validate_restore_network(cfg, ec2_client=ec2)
        assert not v["ok"]
        assert any("security group" in d for d in v["drift"])
        assert v["reconciled"]["SecurityGroupIds"] == []

    def test_missing_subnet_drops_private_ip(self, ec2):
        import network

        cfg = {"security_group_ids": [], "subnet_id": "subnet-gone00000000", "private_ip": "10.0.1.5"}
        v = network.validate_restore_network(cfg, ec2_client=ec2)
        assert not v["ok"]
        assert v["reconciled"]["SubnetId"] is None
        assert v["reconciled"]["drop_private_ip"] is True
