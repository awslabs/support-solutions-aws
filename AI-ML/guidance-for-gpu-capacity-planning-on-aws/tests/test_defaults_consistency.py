# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guards for cross-file consistency.

review fix: the project previously carried three different Bedrock model
defaults — one in ``template.yaml``, another in ``samconfig.toml``, and a
third in ``src/config.py``. Customers deploying via the README quick-start
got yet another default. This test locks the single canonical value so
edits to one file that forget the others break CI immediately.

Whenever the default model or region changes, update ``EXPECTED_*``
constants below AND every referenced file — the tests will point at
whichever file drifted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
EXPECTED_BEDROCK_REGION = "us-west-2"

# The default model MUST appear on the allowlist. The full-spectrum
# allowlist below matches the SAM template's ``AllowedBedrockModelIds``
# default:
#   - Haiku 4.5 — balanced default, Anthropic
#   - Sonnet 4.5 — strongest tool-use reasoning, Anthropic
#   - Nova Pro — Amazon-family fallback (Workshop Studio-vended sandboxes
#     block Anthropic Marketplace subs at the SCP level)
#   - Nova Lite — cheap tier for 1-2 tool lookups
#   - Nova Micro — cheapest, simple feasibility checks only
EXPECTED_ALLOWED_MODEL_IDS = (
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0",
)


# ---------------------------------------------------------------------------
# CFN YAML loader (mirrors tests/test_template.py — kept local to keep the
# fixture surface minimal).
# ---------------------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_multi_constructor(loader, tag_suffix, node):
    tag = f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {tag: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag: loader.construct_sequence(node, deep=True)}
    return {tag: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


@pytest.fixture(scope="module")
def template() -> dict:
    with (REPO_ROOT / "template.yaml").open() as f:
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506 - _CfnLoader subclasses yaml.SafeLoader; parses trusted local CloudFormation templates


# ---------------------------------------------------------------------------
# Model / region default consistency
# ---------------------------------------------------------------------------


class TestModelDefaultConsistency:
    """The Bedrock model default MUST be the same across every file a
    deployer might read. Otherwise ``sam deploy`` and the local REPL
    disagree on which model actually runs.
    """

    def test_template_yaml_matches(self, template: dict):
        default = template["Parameters"]["BedrockModelId"]["Default"]
        assert default == EXPECTED_MODEL_ID, (
            f"template.yaml Parameters.BedrockModelId.Default is {default!r}, "
            f"expected {EXPECTED_MODEL_ID!r}"
        )

    def test_template_yaml_allowlist_matches(self, template: dict):
        """``AllowedBedrockModelIds`` default must contain the same set of
        model IDs as ``EXPECTED_ALLOWED_MODEL_IDS`` (order preserved).
        Whenever the allowlist expands, both this constant and the SAM
        template default MUST be updated together.
        """
        raw = template["Parameters"]["AllowedBedrockModelIds"]["Default"]
        parsed = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
        assert parsed == EXPECTED_ALLOWED_MODEL_IDS, (
            f"template.yaml Parameters.AllowedBedrockModelIds.Default parses "
            f"to {parsed!r}, expected {EXPECTED_ALLOWED_MODEL_IDS!r}"
        )

    def test_default_model_is_in_allowlist(self):
        """The default model MUST be included in the allowlist — otherwise
        a request omitting ``model_id`` would land on a model the operator
        never authorized.
        """
        assert EXPECTED_MODEL_ID in EXPECTED_ALLOWED_MODEL_IDS, (
            f"Default {EXPECTED_MODEL_ID!r} must appear in "
            f"EXPECTED_ALLOWED_MODEL_IDS ({EXPECTED_ALLOWED_MODEL_IDS!r})"
        )

    def test_template_yaml_bedrock_region_matches(self, template: dict):
        default = template["Parameters"]["BedrockRegion"]["Default"]
        assert default == EXPECTED_BEDROCK_REGION

    def test_src_config_matches(self):
        text = (REPO_ROOT / "src" / "config.py").read_text()
        assert EXPECTED_MODEL_ID in text, (
            f"src/config.py must reference {EXPECTED_MODEL_ID!r} as the default"
        )
        # Every model ID on the allowlist must also appear in
        # src/config.py so the _DEFAULT_ALLOWED_MODEL_IDS baked-in string
        # tracks the canonical list.
        for model_id in EXPECTED_ALLOWED_MODEL_IDS:
            assert model_id in text, (
                f"src/config.py must reference allowlist model {model_id!r}"
            )
        # And it must NOT still carry any of the old defaults.
        for stale in (
            "openai.gpt-oss-120b-1:0",
            "us.openai.gpt-4o-2024-11-20-v1:0",
            "us.openai.gpt-4o-mini-2024-07-18-v1:0",
        ):
            assert stale not in text, (
                f"src/config.py still references stale model id {stale!r}"
            )

    def test_samconfig_toml_matches(self):
        text = (REPO_ROOT / "samconfig.toml").read_text()
        assert f'BedrockModelId=\\"{EXPECTED_MODEL_ID}\\"' in text, (
            f"samconfig.toml parameter_overrides must set BedrockModelId to "
            f"{EXPECTED_MODEL_ID!r}"
        )
        # Allowlist must be present as a parameter override too — otherwise
        # ``sam deploy`` (which reads this file) skips the allowlist and the
        # runtime env-var falls back to the src/config.py default, which
        # may drift from the SAM template's default.
        allowlist_csv = ",".join(EXPECTED_ALLOWED_MODEL_IDS)
        assert f'AllowedBedrockModelIds=\\"{allowlist_csv}\\"' in text, (
            f"samconfig.toml parameter_overrides must set AllowedBedrockModelIds "
            f"to {allowlist_csv!r}"
        )
        assert f'BedrockRegion=\\"{EXPECTED_BEDROCK_REGION}\\"' in text

    def test_lambda_handler_fallbacks_match(self):
        """The ``os.environ.get("BEDROCK_MODEL_ID", <fallback>)`` fallbacks
        in ``src/lambda_handler.py`` must also match the canonical default.
        They only fire if the Lambda's env var is missing — but if that
        happens in prod, we want the same behavior everywhere else in the
        stack, not a mystery second default.
        """
        text = (REPO_ROOT / "src" / "lambda_handler.py").read_text()
        # Every literal Bedrock model ID appearing in the handler must be
        # the canonical one.
        stray = re.findall(
            r'"(us\.[\w.\-]+|openai\.[\w.\-]+|anthropic\.[\w.\-]+)"', text
        )
        for candidate in stray:
            assert candidate == EXPECTED_MODEL_ID, (
                f"src/lambda_handler.py carries a non-canonical model id "
                f"literal {candidate!r}; expected {EXPECTED_MODEL_ID!r}"
            )

    def test_readme_quickstart_uses_canonical_default(self):
        """The README quick-start `export BEDROCK_MODEL_ID=...` line must
        match the canonical default so a fresh reader deploys the same
        model the CI pipeline validates.
        """
        text = (REPO_ROOT / "README.md").read_text()
        assert EXPECTED_MODEL_ID in text, (
            f"README.md must document {EXPECTED_MODEL_ID!r} as the default"
        )
        # Also no stale defaults sitting in the README to mislead readers.
        for stale in (
            "us.openai.gpt-4o-2024-11-20-v1:0",
            "openai.gpt-oss-120b-1:0",
        ):
            # Allow the model to appear once inside the Model Selection Guide
            # as an alternative — the README is not that guide, though; the
            # guide lives in docs/implementation-guide.md.
            assert stale not in text, (
                f"README.md still references stale model id {stale!r}"
            )


# ---------------------------------------------------------------------------
# IAM parity: README documented policy must match template.yaml
# ---------------------------------------------------------------------------


class TestIamDocumentedPolicyMatchesTemplate:
    """Every IAM Sid we grant in ``template.yaml`` must be documented in the
    README's IAM section. Otherwise reviewers underestimate the scope.

    We check by Sid names (Sids are the shortest stable identifier for an
    IAM statement across YAML and JSON representations).
    """

    def test_all_sids_appear_in_readme(self, template: dict):
        policies = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Policies"
        ]
        statements = policies[0]["Statement"]

        # Collect Sids from both plain statements and Fn::If-wrapped ones.
        sids: list[str] = []
        for stmt in statements:
            if isinstance(stmt, dict):
                if "Fn::If" in stmt:
                    inner = stmt["Fn::If"][1]
                    if isinstance(inner, dict) and "Sid" in inner:
                        sids.append(inner["Sid"])
                elif "Sid" in stmt:
                    sids.append(stmt["Sid"])

        readme_text = (REPO_ROOT / "README.md").read_text()
        missing = [sid for sid in sids if sid not in readme_text]
        assert not missing, (
            f"README.md is missing IAM Sids present in template.yaml: {missing}. "
            f"Update README.md IAM section to document every Sid granted."
        )
