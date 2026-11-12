# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guards: the system prompt must NOT teach the model
to invent a competing confidence score.

The deterministic scorer (``src/confidence.py``) is the authoritative
source of the numeric ``score`` and the ``verdict`` label. If the system
prompt reverts to giving the model prose thresholds like
"HIGH (0.8-1.0): ..." the model will start narrating its own number and
that number will drift from the deterministic one — the exact bug the
review flagged.

These tests are string-level guards on ``src/config.py::SYSTEM_PROMPT``.
They are intentionally brittle: any future edit that reintroduces the
old anti-pattern will trip CI and force a review.
"""

from __future__ import annotations


from src.config import SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# The four canonical verdict labels
# ---------------------------------------------------------------------------


CANONICAL_VERDICTS = {
    "DEPLOYABLE",
    "DEPLOYABLE WITH PREPARATION",
    "HIGH RISK",
    "NOT FEASIBLE",
}

# Verdicts a well-intentioned edit might invent that would break the
# LLM-vs-scorer contract.
FORBIDDEN_VERDICTS = {
    "MODERATE RISK",
    "DEPLOYABLE WITH CAUTION",
    "MARGINALLY FEASIBLE",
    "DEPLOYABLE BUT RISKY",
}


class TestPromptDoesNotTeachSelfScoring:
    """The prompt must NOT include the old prose thresholds that told the
    LLM to rate confidence 0.0–1.0 and pick a label from HIGH/MODERATE/
    MODERATE-LOW/VERY LOW.
    """

    def test_no_numeric_confidence_rating_instruction(self):
        # The old prompt contained lines like:
        #   "Rate deployment confidence on a scale:"
        #   "**HIGH (0.8-1.0)**: Instance available, quota sufficient, ..."
        # These MUST NOT reappear.
        bad_phrases = [
            "Rate deployment confidence",
            "confidence on a scale",
            "HIGH (0.8-1.0)",
            "MODERATE (0.5-0.7)",
            "MODERATE-LOW (0.3-0.5)",
            "VERY LOW (<0.3)",
        ]
        for phrase in bad_phrases:
            assert phrase not in SYSTEM_PROMPT, (
                f"System prompt must not reintroduce self-scoring phrase "
                f"{phrase!r}. The deterministic scorer owns the number."
            )

    def test_prompt_states_scorer_is_authoritative(self):
        """The prompt must make it explicit that the deterministic scorer
        computes the numeric verdict, not the LLM.
        """
        text = SYSTEM_PROMPT.lower()
        # Must reference the confidence_assessments field so the model knows
        # where the authoritative score comes from.
        assert "confidence_assessments" in text
        # Must state the LLM does NOT invent the score.
        must_reference_one_of = [
            "do not invent",
            "do not compute your own",
            "deterministic",
            "authoritative",
        ]
        assert any(phrase in text for phrase in must_reference_one_of), (
            "Prompt should tell the model it does not invent the score / "
            "the scorer is deterministic / authoritative."
        )


class TestPromptRestrictsVerdictLabels:
    """The prompt must whitelist only the four canonical verdict labels and
    must not include hedged / novel alternatives that would drift from the
    deterministic scorer's outputs.
    """

    def test_all_canonical_verdicts_documented(self):
        for verdict in CANONICAL_VERDICTS:
            assert verdict in SYSTEM_PROMPT, (
                f"Canonical verdict {verdict!r} must appear in the system prompt "
                f"so the model knows the allowed label set."
            )

    def test_forbidden_verdicts_absent_as_labels(self):
        """Forbidden verdicts must not appear as bolded labels — the
        markdown ``**LABEL**`` form the prompt uses for real verdicts.

        Anti-examples in prose (e.g. \"do not invent new ones like
        'MODERATE RISK'\") are fine and intentionally teach the model
        what NOT to do.
        """
        for verdict in FORBIDDEN_VERDICTS:
            bolded = f"**{verdict}**"
            assert bolded not in SYSTEM_PROMPT, (
                f"Non-canonical verdict label {bolded!r} appears in the system "
                f"prompt as a real verdict. Adding a new verdict category "
                f"also requires updating src/confidence.py::compute_confidence "
                f"— do BOTH or neither."
            )


class TestPromptStillIncludesResidencyAndGovContract:
    """Cross-check: added residency + government constraints. the review's
    prompt rewrite must not have accidentally dropped them.
    """

    def test_data_residency_prompt_kept(self):
        text = SYSTEM_PROMPT.lower()
        assert "data residency" in text or "residency" in text
        assert "lgpd" in text
        assert "gdpr" in text

    def test_government_regulatory_prompt_kept(self):
        text = SYSTEM_PROMPT.lower()
        assert "govcloud" in text
        assert "fedramp" in text or "itar" in text
