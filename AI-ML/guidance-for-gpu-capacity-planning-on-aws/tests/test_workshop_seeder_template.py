# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Template-level regression guards for the workshop ICE seeder split.

The workshop pre-provisions THREE separate stacks (before the attendee
deploys their own agent stack), and this file guards the contract each
one exposes.

Log-group ownership was moved out of ``workshop/ice-seeder.yaml`` and
into ``workshop/ice-log-group.yaml`` on 2026-07-17 so Workshop Studio
can guarantee the deploy order (log-group → metric-filter → seeder)
that lets the metric filter attach BEFORE any event arrives. Prior to
that change, all 30 seeded events pre-dated the filter and the metric
stayed at zero datapoints (observed during workshop testing on
2026-07-17).

Two outputs Workshop Studio and workshop facilitators depend on:

    - ``LogGroupName`` (on ice-log-group.yaml) — the metric-filter
      stack hardcodes the same log-group path. If ice-log-group.yaml's
      path changes without a matching update to the metric-filter
      template, the two stacks silently diverge.
    - ``EventsSeeded`` (on ice-seeder.yaml) — surfaced to facilitators
      so they can sanity check the seeder ran correctly for each
      attendee sandbox before the workshop starts.

Defensive assertions guard against future regressions:

    - The seeder MUST NOT re-own the log group (would re-create the
      pre-2026-07-17 datapoint bug).
    - The seeder MUST NOT own a shared SNS alarm topic (an earlier
      2026-07-14 design that relied on Workshop Studio resolving
      ${STACK.OUTPUT} placeholders — unsupported; failed
      EarlyValidation 2026-07-15).

The tests use the same permissive PyYAML CFN loader as
``test_template.py`` so intrinsic tags (``!Ref``, ``!Sub``, etc.) are
preserved as inspectable dicts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


WORKSHOP_DIR = Path(__file__).resolve().parent.parent / "workshop"
SEEDER_TEMPLATE_PATH = WORKSHOP_DIR / "ice-seeder.yaml"
LOG_GROUP_TEMPLATE_PATH = WORKSHOP_DIR / "ice-log-group.yaml"

# The workshop/ directory is not shipped in every distribution of this
# guidance; skip these template guards when it is absent.
pytestmark = pytest.mark.skipif(
    not WORKSHOP_DIR.is_dir(), reason="workshop/ directory not present"
)


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
def seeder_template() -> dict:
    with SEEDER_TEMPLATE_PATH.open() as f:
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506 - _CfnLoader subclasses yaml.SafeLoader; parses trusted local CloudFormation templates


@pytest.fixture(scope="module")
def log_group_template() -> dict:
    with LOG_GROUP_TEMPLATE_PATH.open() as f:
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506 - _CfnLoader subclasses yaml.SafeLoader; parses trusted local CloudFormation templates


class TestLogGroupOutputContract:
    """regression guard: outputs the metric-filter stack depends on."""

    def test_log_group_name_output_exists(self, log_group_template: dict):
        outputs = log_group_template.get("Outputs", {})
        assert "LogGroupName" in outputs, (
            "LogGroupName output is required — the workshop metric-filter "
            "stack hardcodes the same log-group path as its default. If the "
            "log-group path changes without a matching update to the "
            "metric-filter template's hardcoded value, the two stacks "
            "silently diverge and the metric filter never receives events."
        )


class TestSeederOutputContract:
    """regression guard: outputs facilitators depend on."""

    def test_events_seeded_output_exists(self, seeder_template: dict):
        outputs = seeder_template.get("Outputs", {})
        assert "EventsSeeded" in outputs, (
            "EventsSeeded output is required — surfaced to facilitators so "
            "they can sanity-check the seeder ran correctly for each "
            "attendee sandbox before the workshop starts."
        )


class TestSeederDoesNotOwnLogGroup:
    """regression guard: the seeder does NOT create the log group.

    Log-group creation was moved to ``workshop/ice-log-group.yaml`` on
    2026-07-17 so Workshop Studio could enforce the deploy order
    (log-group → metric-filter → seeder). If the seeder re-owns the log
    group, WS may deploy the seeder before the metric filter attaches
    and the pre-2026-07-17 datapoint bug returns.
    """

    def test_seeder_does_not_declare_log_group_resource(self, seeder_template: dict):
        resources = seeder_template.get("Resources", {})
        for logical_id, resource in resources.items():
            resource_type = resource.get("Type") if isinstance(resource, dict) else None
            assert resource_type != "AWS::Logs::LogGroup", (
                f"Resource '{logical_id}' (Type: {resource_type}) must not "
                "exist in the seeder template — log-group creation belongs "
                "in workshop/ice-log-group.yaml, which is deployed before "
                "the metric-filter stack. Re-adding it here would recreate "
                "the datapoint bug fixed on 2026-07-17."
            )

    def test_seeder_does_not_expose_log_group_name_output(self, seeder_template: dict):
        outputs = seeder_template.get("Outputs", {})
        assert "LogGroupName" not in outputs, (
            "LogGroupName output must not exist in the seeder — it was "
            "moved to workshop/ice-log-group.yaml on 2026-07-17. If both "
            "templates expose it, callers may cross-wire the wrong one."
        )

    def test_seeder_does_not_expose_log_group_arn_output(self, seeder_template: dict):
        outputs = seeder_template.get("Outputs", {})
        assert "LogGroupArn" not in outputs, (
            "LogGroupArn output must not exist in the seeder — it was "
            "moved to workshop/ice-log-group.yaml on 2026-07-17. See "
            "TestSeederDoesNotOwnLogGroup docstring for context."
        )


class TestSeederHasNoSnsTopic:
    """regression guard: the seeder does NOT own the alarm SNS topic.

    Deliberately assert the absence so a future well-meaning refactor
    doesn't reintroduce the cross-stack-ref pattern that failed
    Workshop Studio validation on 2026-07-15. If the workshop
    needs an SNS topic in the seeder for some future purpose, remove
    this test and update the seeder + agent stack together.
    """

    def test_seeder_does_not_declare_workshop_alarm_topic(self, seeder_template: dict):
        resources = seeder_template.get("Resources", {})
        assert "WorkshopAlarmTopic" not in resources, (
            "WorkshopAlarmTopic must not be reintroduced in the seeder. "
            "The agent stack (template.yaml) self-provisions its own SNS "
            "topic via the HasNoExternalAlarmTopic condition. Workshop "
            "Studio does not resolve ${STACK.OUTPUT} placeholders, so a "
            "seeder-owned topic cannot be referenced by the agent stack "
            "via contentspec.yaml."
        )

    def test_seeder_does_not_expose_sns_topic_arn_output(self, seeder_template: dict):
        outputs = seeder_template.get("Outputs", {})
        assert "SnsTopicArn" not in outputs, (
            "SnsTopicArn output must not be reintroduced. See "
            "TestSeederHasNoSnsTopic docstring for context."
        )
