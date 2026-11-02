# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for recommendation validation + feedback loop (review #8)."""
import pytest

import feedback


def _res(rid, cpu=None, conns=None, points=168):
    m = {}
    if cpu is not None:
        m["CPUUtilization"] = {"avg": cpu, "max": cpu, "points": points}
    if conns is not None:
        m["DatabaseConnections"] = {"avg": conns, "points": points}
    return {"id": rid, "type": "EC2" if conns is None else "RDS", "metrics_7d": m}


@pytest.mark.unit
class TestGuardrail:
    def test_overrides_safe_when_cpu_high(self):
        rec = {"project": "p", "risk": "safe_to_save", "reason": "idle"}
        out = feedback.validate_recommendation(rec, [_res("i-1", cpu=45.0)], config={})
        assert out["risk"] == "review_needed"
        assert out["guardrail_override"] is True
        assert "Guardrail" in out["reason"]

    def test_overrides_safe_when_db_connections_high(self):
        rec = {"project": "p", "risk": "safe_to_save"}
        out = feedback.validate_recommendation(rec, [_res("d-1", conns=12.0)], config={})
        assert out["risk"] == "review_needed"

    def test_keeps_safe_when_truly_idle(self):
        rec = {"project": "p", "risk": "safe_to_save", "reason": "idle"}
        out = feedback.validate_recommendation(rec, [_res("i-1", cpu=0.5)], config={})
        assert out["risk"] == "safe_to_save"
        assert not out.get("guardrail_override")

    def test_ignores_metrics_with_no_datapoints(self):
        # No data (points=0) is NOT a violation here (fail-safe layer handles absence).
        rec = {"project": "p", "risk": "safe_to_save"}
        out = feedback.validate_recommendation(rec, [_res("i-1", cpu=99.0, points=0)], config={})
        assert out["risk"] == "safe_to_save"

    def test_never_touches_non_safe_verdicts(self):
        rec = {"project": "p", "risk": "keep_running"}
        out = feedback.validate_recommendation(rec, [_res("i-1", cpu=0.1)], config={})
        assert out["risk"] == "keep_running"  # never upgraded to safe_to_save

    def test_respects_custom_thresholds(self):
        rec = {"project": "p", "risk": "safe_to_save"}
        # raise the CPU threshold so 45% is now "safe"
        out = feedback.validate_recommendation(rec, [_res("i-1", cpu=45.0)],
                                               config={"max_cpu_pct_for_save": 50.0})
        assert out["risk"] == "safe_to_save"

    def test_validate_result_maps_by_project(self):
        result = {"recommendations": [{"project": "a", "risk": "safe_to_save"}],
                  "summary": "s"}
        pdata = [{"project": "a", "resources": [_res("i-1", cpu=80.0)]}]
        out = feedback.validate_result(result, pdata, config={})
        assert out["recommendations"][0]["risk"] == "review_needed"


@pytest.mark.unit
class TestFeedbackPersistence:
    def test_record_and_list(self, state_table):
        feedback.record_feedback("alpha", "down", recommendation="safe_to_save", reason="was active")
        fb = feedback.list_feedback()
        assert len(fb) == 1
        assert fb[0]["project"] == "alpha" and fb[0]["verdict"] == "down"

    def test_recent_examples_block(self, state_table):
        feedback.record_feedback("alpha", "down", reason="terminated a live API")
        block = feedback.recent_feedback_examples()
        assert "WRONG" in block and "alpha" in block

    def test_empty_examples(self, state_table):
        assert feedback.recent_feedback_examples() == ""


@pytest.mark.unit
class TestHandleFeedback:
    def test_handle_feedback_persists(self, state_table):
        import index

        html = index.handle_feedback({"project": "alpha", "verdict": "up", "recommendation": "safe_to_save"})
        assert "recorded" in html
        assert len(feedback.list_feedback()) == 1


@pytest.mark.integration
class TestEvalHarness:
    def test_eval_meets_threshold(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent / "eval_recommendations.py"
        spec = importlib.util.spec_from_file_location("eval_recommendations", path)
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)

        m = ev.evaluate()
        assert m["precision"] >= 1.0  # never green-light an active resource
        assert m["recall"] >= 0.8
        assert m["fp"] == 0
