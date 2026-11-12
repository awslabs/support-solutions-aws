# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Smoke tests for the recordable demo script.

These are safety-net tests — they make sure the demo continues to run after
future refactors of the agent, confidence scorer, or tool signatures. If a
tool's return-shape drifts, the fixture in ``examples/demo_recorded.py``
will still shape-match at the boundary and this test will trip.

The demo is intended to run offline against mocked Bedrock + fixture tool
outputs; the tests just verify the four scenarios each:

1. Import + register all tools without error.
2. Complete their scripted Bedrock loop with ``stop_reason == "end_turn"``.
3. Produce a non-empty ``confidence_assessments`` array with a valid
   verdict label.
4. Report a specific expected verdict — pins the story each scenario is
   telling. If the deterministic scorer changes and the story would
   change, this test forces us to update either the scorer, the demo
   fixtures, or the talk track.
"""

from __future__ import annotations

import importlib
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def demo_module():
    """Import the demo module once, isolated from the rest of the suite."""
    demo_path = str(REPO_ROOT / "examples")
    added = False
    if demo_path not in sys.path:
        sys.path.insert(0, demo_path)
        added = True
    try:
        # Force a re-import so DEMO_SPEED changes in tests take effect.
        if "demo_recorded" in sys.modules:
            del sys.modules["demo_recorded"]
        mod = importlib.import_module("demo_recorded")
        yield mod
    finally:
        if added:
            sys.path.remove(demo_path)


@pytest.fixture(autouse=True)
def _fast_and_no_color(monkeypatch):
    """Every demo test runs at zero pace with colour off — otherwise the
    test suite would take 10+ seconds per scenario just on sleeps.
    """
    monkeypatch.setenv("DEMO_SPEED", "0")
    monkeypatch.setenv("NO_COLOR", "1")


class TestDemoScenariosSmoke:
    """Each of the four scenarios must run end-to-end and land on the
    verdict its talk-track promises. These are the specific pinpoints the
    demo relies on.
    """

    EXPECTED = {
        "feasible": "DEPLOYABLE",
        "blocked": "NOT FEASIBLE",
        "ice_override": "HIGH RISK",
        "adversarial": "HIGH RISK",
    }

    def test_all_expected_scenarios_defined(self, demo_module):
        defined = {s["id"] for s in demo_module._scenarios()}
        assert defined == set(self.EXPECTED.keys()), (
            f"Scenario set drifted: defined={defined}, expected={set(self.EXPECTED.keys())}"
        )

    @pytest.mark.parametrize("scenario_id,expected_verdict", list(EXPECTED.items()))
    def test_scenario_produces_expected_verdict(
        self, demo_module, scenario_id, expected_verdict
    ):
        # Locate the scenario dict.
        scenario = next(s for s in demo_module._scenarios() if s["id"] == scenario_id)

        # Wire up the same mocks the demo uses, but capture the result
        # without going through the ANSI-heavy _play_scenario renderer.
        from unittest.mock import MagicMock, patch

        from src.agent import CapacityDecisionAgent
        from src.tools import ALL_TOOLS

        bedrock = MagicMock()
        bedrock.converse.side_effect = list(scenario["bedrock_script"])
        originals = demo_module._patch_tool_handlers(scenario["tool_fixtures"])
        try:
            with patch("src.agent.boto3.client", return_value=bedrock):
                agent = CapacityDecisionAgent(
                    model_id="test-model",
                    region="us-west-2",
                    verbose=False,
                    max_iterations=15,
                )
                for tool_spec in ALL_TOOLS:
                    agent.register_tool(**tool_spec)
                result = agent.run(scenario["user_query"])
        finally:
            demo_module._restore_tool_handlers(originals)

        # (2) — scenario ran to completion.
        assert result["stop_reason"] == "end_turn", (
            f"{scenario_id!r}: expected end_turn, got {result['stop_reason']!r}"
        )
        assert result["final_text"], f"{scenario_id!r}: agent produced no final text"

        # (3) — confidence_assessments is populated with valid shape.
        assessments = result["confidence_assessments"]
        assert assessments, f"{scenario_id!r}: confidence_assessments is empty"
        for a in assessments:
            for field in ("instance_type", "region", "requested_count", "confidence"):
                assert field in a
            conf = a["confidence"]
            assert conf["verdict"] in {
                "DEPLOYABLE",
                "DEPLOYABLE WITH PREPARATION",
                "HIGH RISK",
                "NOT FEASIBLE",
            }

        # (4) — verdict matches the talk-track's promise.
        verdicts = {a["confidence"]["verdict"] for a in assessments}
        assert expected_verdict in verdicts, (
            f"{scenario_id!r}: expected verdict {expected_verdict!r} in "
            f"confidence_assessments; got {verdicts}"
        )


class TestDemoMainEntrypoint:
    """The demo must run to completion when invoked as a script — this is
    what the shell wrapper (``examples/demo_recorded.sh``) does.
    """

    def test_single_scenario_via_main(self, demo_module, monkeypatch):
        # Swap sys.argv to invoke a single scenario.
        monkeypatch.setattr(sys, "argv", ["demo_recorded.py", "feasible"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = demo_module.main()
        assert rc == 0, "demo main() should exit 0 on success"
        output = buf.getvalue()
        # Talk-track anchors — verify the demo actually printed the
        # sections a viewer expects to see on screen.
        assert "GPU Capacity Planning Agent" in output
        assert "DEPLOYABLE" in output
        assert "Deterministic Confidence Assessments" in output

    def test_unknown_scenario_returns_1(self, demo_module, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["demo_recorded.py", "does-not-exist"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = demo_module.main()
        assert rc == 1
        assert "Unknown scenario" in buf.getvalue()


class TestScenarioTalkTrackAlignment:
    """The demo talk-track promises specific evidence for each scenario —
    e.g., scenario 3 (ice_override) demonstrates applied_caps in action.
    These tests pin the evidence so the recording never has to be re-shot
    due to a silent regression.
    """

    def _run(self, demo_module, scenario_id: str):
        from unittest.mock import MagicMock, patch

        from src.agent import CapacityDecisionAgent
        from src.tools import ALL_TOOLS

        scenario = next(s for s in demo_module._scenarios() if s["id"] == scenario_id)
        bedrock = MagicMock()
        bedrock.converse.side_effect = list(scenario["bedrock_script"])
        originals = demo_module._patch_tool_handlers(scenario["tool_fixtures"])
        try:
            with patch("src.agent.boto3.client", return_value=bedrock):
                agent = CapacityDecisionAgent(
                    model_id="test-model",
                    region="us-west-2",
                    verbose=False,
                    max_iterations=15,
                )
                for tool_spec in ALL_TOOLS:
                    agent.register_tool(**tool_spec)
                return agent.run(scenario["user_query"])
        finally:
            demo_module._restore_tool_handlers(originals)

    def test_blocked_scenario_records_hard_gate_unavailable(self, demo_module):
        result = self._run(demo_module, "blocked")
        assessments = result["confidence_assessments"]
        kinds = {
            cap["kind"] for a in assessments for cap in a["confidence"]["applied_caps"]
        }
        assert "hard_gate_unavailable" in kinds, (
            "Scenario `blocked` is supposed to demonstrate the "
            "hard_gate_unavailable cap firing — the demo's third talk-track "
            "beat depends on this."
        )

    def test_ice_override_scenario_records_severe_ice_caps(self, demo_module):
        result = self._run(demo_module, "ice_override")
        assessments = result["confidence_assessments"]
        kinds = {
            cap["kind"] for a in assessments for cap in a["confidence"]["applied_caps"]
        }
        # Both the signal-level cap and the final-score ceiling must fire.
        assert "ice_history_severe" in kinds
        assert "ice_ceiling_severe" in kinds, (
            "Scenario `ice_override` must demonstrate ice_ceiling_severe "
            "firing — this is the auditable evidence that ground-truth "
            "ICE overrides the SPS proxy signal."
        )
        # Base score must be materially higher than the final score;
        # otherwise the caps didn't visibly change anything.
        conf = assessments[0]["confidence"]
        assert conf["base_score"] > conf["score"] + 0.15, (
            f"Scenario `ice_override` needs a visible score drop from caps; "
            f"base={conf['base_score']}, final={conf['score']}"
        )

    def test_feasible_scenario_has_no_caps(self, demo_module):
        """The DEPLOYABLE case is supposed to be visibly clean —
        applied_caps must be empty so the viewer sees the contrast.
        """
        result = self._run(demo_module, "feasible")
        for a in result["confidence_assessments"]:
            assert a["confidence"]["applied_caps"] == [], (
                f"Scenario `feasible` should have no applied caps to "
                f"contrast against the harder scenarios; got: "
                f"{a['confidence']['applied_caps']}"
            )
