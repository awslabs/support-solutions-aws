# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression guards for the MEDIUM-tier fixes.

These tests are grouped by MEDIUM-item ID from the original code review so
CI failures point at the specific fix that regressed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# CFN YAML loader (mirrors tests/test_template.py; kept local)
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
# M2 — ODCR paginator cap
# ---------------------------------------------------------------------------


class TestOdcrPaginatorBounded:
    def test_max_pages_bounded(self):
        from src.tools.capacity_reservations import MAX_PAGES

        assert MAX_PAGES <= 5, (
            f"ODCR MAX_PAGES was raised to {MAX_PAGES}; that lets a "
            f"pathological account of tiny ODCRs burn the 29s API Gateway "
            f"timeout. Cap at 5."
        )

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_truncated_flag_set_when_cap_hit(self, mock_factory):
        from src.tools.capacity_reservations import (
            MAX_PAGES,
            PAGE_SIZE,
            check_existing_odcrs,
        )

        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # Every page returns a full batch + a NextToken; the loop hits the cap.
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": [
                {
                    "CapacityReservationId": f"cr-{i}",
                    "State": "active",
                    "AvailabilityZone": "us-east-1a",
                    "TotalInstanceCount": 1,
                    "AvailableInstanceCount": 1,
                }
                for i in range(PAGE_SIZE)
            ],
            "NextToken": "more",
        }

        result = check_existing_odcrs("g5.2xlarge", "us-east-1")

        assert result["truncated"] is True
        assert "pagination cap" in result["interpretation"]
        assert mock_client.describe_capacity_reservations.call_count == MAX_PAGES

    @patch("src.tools.capacity_reservations._ec2_client")
    def test_truncated_flag_false_on_normal_and_error_paths(self, mock_factory):
        from src.tools.capacity_reservations import check_existing_odcrs

        # No ODCRs found → truncated is explicitly False, not missing.
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.describe_capacity_reservations.return_value = {
            "CapacityReservations": []
        }
        result = check_existing_odcrs("g5.2xlarge", "us-east-1")
        assert "truncated" in result and result["truncated"] is False

        # API error → truncated is also explicitly False (no lookup ran).
        mock_client.describe_capacity_reservations.side_effect = RuntimeError("boom")
        result_err = check_existing_odcrs("g5.2xlarge", "us-east-1")
        assert result_err["truncated"] is False


# ---------------------------------------------------------------------------
# M3 — Pricing API uses deploy account regardless of bound customer session
# ---------------------------------------------------------------------------


class TestPricingAlwaysUsesDeployAccount:
    """Pricing API responses are identical across accounts. Forcing the
    customer's assumed role to be used would waste an STS call and require
    ``pricing:GetProducts`` in the customer's trust policy.
    """

    def test_get_client_use_deploy_account_bypasses_bound_session(self):
        """When use_deploy_account=True, the currently-bound customer
        session is ignored and boto3.client is called with the deploy
        credentials.
        """
        import boto3

        from src.aws_session import get_client, use_session

        fake_customer_session = MagicMock(spec=boto3.Session)
        with (
            patch("src.aws_session.boto3.client") as mock_boto3_client,
            use_session(fake_customer_session),
        ):
            mock_boto3_client.return_value = MagicMock()

            get_client("pricing", "us-east-1", use_deploy_account=True)

            # The customer session must NOT be used.
            fake_customer_session.client.assert_not_called()
            # Deploy-account boto3.client must be called instead.
            mock_boto3_client.assert_called_once()

    def test_get_client_default_behavior_unchanged(self):
        """Regression: without use_deploy_account=True, get_client still
        honors the bound customer session.
        """
        import boto3

        from src.aws_session import get_client, use_session

        fake_customer_session = MagicMock(spec=boto3.Session)
        with use_session(fake_customer_session):
            get_client("ec2", "us-east-1")

        fake_customer_session.client.assert_called_once()

    @patch("src.tools.pricing.get_client")
    def test_pricing_tool_calls_get_client_with_use_deploy_account(
        self, mock_get_client
    ):
        """The SageMaker pricing tool must pass use_deploy_account=True
        so it never routes through the customer role.
        """
        from src.tools.pricing import _pricing_client

        _pricing_client()
        mock_get_client.assert_called_once()
        kwargs = mock_get_client.call_args.kwargs
        assert kwargs.get("use_deploy_account") is True

    @patch("src.tools.ec2_pricing.get_client")
    def test_ec2_pricing_tool_calls_get_client_with_use_deploy_account(
        self, mock_get_client
    ):
        from src.tools.ec2_pricing import _pricing_client

        _pricing_client()
        kwargs = mock_get_client.call_args.kwargs
        assert kwargs.get("use_deploy_account") is True


# ---------------------------------------------------------------------------
# M7 — DLQ alarm noise: threshold + separate aged-messages alarm
# ---------------------------------------------------------------------------


class TestDlqAlarmsRefined:
    def test_dlq_message_spike_alarm_uses_15min_window_and_threshold(
        self, template: dict
    ):
        alarm = template["Resources"]["DLQMessagesAlarm"]
        p = alarm["Properties"]
        # Not the old {1-msg, 60s} configuration.
        assert p["Threshold"] >= 5, (
            f"DLQ spike threshold {p['Threshold']} is too aggressive; a "
            f"single transient DLQ enqueue would page. Raise to 5+."
        )
        assert p["Period"] >= 300, (
            f"DLQ spike period {p['Period']}s is too short — 15-min "
            f"window (900s) skips transient spikes."
        )
        assert p["MetricName"] == "ApproximateNumberOfMessagesVisible"

    def test_dlq_aged_messages_alarm_exists(self, template: dict):
        alarm = template["Resources"]["DLQAgedMessagesAlarm"]
        p = alarm["Properties"]
        assert p["MetricName"] == "ApproximateAgeOfOldestMessage"
        assert p["Threshold"] == 3600  # 1 hour, in seconds
        assert p["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


# ---------------------------------------------------------------------------
# M11 — RequireExternalId parameter wiring in the template
# ---------------------------------------------------------------------------


class TestRequireExternalIdParameterWiring:
    def test_parameter_exists_and_defaults_to_true(self, template: dict):
        p = template["Parameters"]["RequireExternalId"]
        assert p["Default"] == "true"
        assert set(p["AllowedValues"]) == {"true", "false"}

    def test_parameter_wired_to_env_var(self, template: dict):
        env = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Environment"
        ]["Variables"]
        assert env["REQUIRE_EXTERNAL_ID"] == {"Fn::Ref": "RequireExternalId"}
