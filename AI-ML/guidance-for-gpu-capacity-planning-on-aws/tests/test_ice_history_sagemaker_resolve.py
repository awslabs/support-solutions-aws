# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Follow-up A regression guards: SageMaker EndpointConfig resolution.

These tests pin the behavior that closes the last remaining defect the
review flagged on the SageMaker ICE path — inline extraction from
CloudTrail rarely gets the instance type, so the tool now calls
``sagemaker:DescribeEndpointConfig`` on the referenced config name to
resolve authoritatively. Failures degrade to
``capacity_signal="unresolved"``, which the deterministic scorer treats
as "not queried" (via the ``ice_signal_unavailable`` cap).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.confidence import compute_confidence
from src.tools.ice_history import (
    _extract_endpoint_config_name,
    _resolve_instance_types_via_describe,
    get_ice_history,
)


def _events_only_on_create_endpoint(events: list):
    """Return a ``side_effect`` function suitable for
    ``mock_client.lookup_events`` that yields ``events`` when the caller
    asked for ``CreateEndpoint`` and yields empty pages for the other
    SageMaker event names (UpdateEndpoint, UpdateEndpointWeightsAndCapacities).
    This mirrors real accounts where the vast majority of endpoint
    capacity failures come from CreateEndpoint.
    """

    def _side_effect(**kwargs):
        attrs = kwargs.get("LookupAttributes") or []
        event_name = None
        for a in attrs:
            if a.get("AttributeKey") == "EventName":
                event_name = a.get("AttributeValue")
                break
        if event_name == "CreateEndpoint":
            return {"Events": events}
        return {"Events": []}

    return _side_effect


# --------------------------------------------------------------------------
# Helper — build a CloudTrail SageMaker capacity-error event
# --------------------------------------------------------------------------


def _sagemaker_event(
    *,
    event_name: str = "CreateEndpoint",
    endpoint_config_name: str | None = "cfg-example",
    inline_instance_type: str | None = None,
    error_code: str = "ResourceLimitExceeded",
    event_id: str = "evt-sm-1",
    event_source: str = "sagemaker.amazonaws.com",
) -> dict:
    """Build a CloudTrail Event dict shaped like a SageMaker capacity failure."""
    payload: dict = {
        "eventSource": event_source,
        "errorCode": error_code,
        "requestParameters": {},
    }
    if endpoint_config_name is not None:
        payload["requestParameters"]["endpointConfigName"] = endpoint_config_name
    if inline_instance_type is not None:
        # If someone attaches production variants inline (rare in real life
        # but possible), the extractor should short-circuit.
        payload["requestParameters"]["productionVariants"] = [
            {"instanceType": inline_instance_type}
        ]
    return {
        "EventId": event_id,
        "EventTime": datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
        "EventName": event_name,
        "CloudTrailEvent": json.dumps(payload),
    }


# --------------------------------------------------------------------------
# _extract_endpoint_config_name unit tests
# --------------------------------------------------------------------------


class TestExtractEndpointConfigName:
    def test_extracts_when_present(self):
        payload = {"requestParameters": {"endpointConfigName": "my-cfg"}}
        assert _extract_endpoint_config_name(payload) == "my-cfg"

    def test_returns_none_when_missing(self):
        assert _extract_endpoint_config_name({}) is None
        assert _extract_endpoint_config_name({"requestParameters": {}}) is None

    def test_returns_none_when_malformed(self):
        assert _extract_endpoint_config_name({"requestParameters": None}) is None
        assert (
            _extract_endpoint_config_name(
                {"requestParameters": {"endpointConfigName": ""}}
            )
            is None
        )


# --------------------------------------------------------------------------
# _resolve_instance_types_via_describe unit tests
# --------------------------------------------------------------------------


class TestResolveViaDescribe:
    def test_returns_types_from_production_variants(self):
        client = MagicMock()
        client.describe_endpoint_config.return_value = {
            "ProductionVariants": [
                {"InstanceType": "ml.g5.2xlarge"},
                {"InstanceType": "ml.g5.4xlarge"},
            ],
            "ShadowProductionVariants": [],
        }
        cache: dict = {}
        assert _resolve_instance_types_via_describe(client, "cfg-x", cache) == [
            "ml.g5.2xlarge",
            "ml.g5.4xlarge",
        ]
        # Cached for the second call — no additional API call.
        assert _resolve_instance_types_via_describe(client, "cfg-x", cache) == [
            "ml.g5.2xlarge",
            "ml.g5.4xlarge",
        ]
        assert client.describe_endpoint_config.call_count == 1

    def test_includes_shadow_variants(self):
        client = MagicMock()
        client.describe_endpoint_config.return_value = {
            "ProductionVariants": [{"InstanceType": "ml.g5.2xlarge"}],
            "ShadowProductionVariants": [{"InstanceType": "ml.g5.4xlarge"}],
        }
        result = _resolve_instance_types_via_describe(client, "cfg-y", {})
        assert set(result) == {"ml.g5.2xlarge", "ml.g5.4xlarge"}

    def test_failure_returns_empty_and_caches_miss(self):
        client = MagicMock()
        client.describe_endpoint_config.side_effect = RuntimeError("AccessDenied")
        cache: dict = {}
        assert _resolve_instance_types_via_describe(client, "cfg-z", cache) == []
        # Cache should record the miss so subsequent calls short-circuit.
        assert cache["cfg-z"] is None
        # Second call must NOT hit the API again.
        assert _resolve_instance_types_via_describe(client, "cfg-z", cache) == []
        assert client.describe_endpoint_config.call_count == 1


# --------------------------------------------------------------------------
# End-to-end get_ice_history: SageMaker path happy path + unresolved
# --------------------------------------------------------------------------


class TestSageMakerHappyPath:
    @patch("src.tools.ice_history._sagemaker_client")
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_describe_endpoint_config_resolves_matching_type(
        self, mock_ct_client_factory, mock_sm_client_factory
    ):
        """Follow-up A happy path: an inline-empty CreateEndpoint failure
        resolves to the target instance type via DescribeEndpointConfig
        and counts toward ``event_count``.
        """
        ct_client = MagicMock()
        ct_client.lookup_events.side_effect = _events_only_on_create_endpoint(
            [_sagemaker_event(endpoint_config_name="cfg-happy")]
        )
        mock_ct_client_factory.return_value = ct_client

        sm_client = MagicMock()
        sm_client.describe_endpoint_config.return_value = {
            "ProductionVariants": [{"InstanceType": "ml.g5.2xlarge"}],
            "ShadowProductionVariants": [],
        }
        mock_sm_client_factory.return_value = sm_client

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", deployment_target="sagemaker"
        )

        assert result["event_count"] == 1
        assert result["unresolved_event_count"] == 0
        assert result["capacity_signal"] == "isolated"
        # The sample event must record the resolution path so a consumer
        # (or a downstream test) can see it came from DescribeEndpointConfig.
        assert (
            result["sample_events"][0]["instance_type_source"]
            == "describe_endpoint_config"
        )

    @patch("src.tools.ice_history._sagemaker_client")
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_describe_cached_across_repeat_events(
        self, mock_ct_client_factory, mock_sm_client_factory
    ):
        """The same endpoint config appearing in multiple events must only
        cost one DescribeEndpointConfig call (per-invocation memo).
        """
        ct_client = MagicMock()
        ct_client.lookup_events.side_effect = _events_only_on_create_endpoint(
            [
                _sagemaker_event(endpoint_config_name="cfg-shared", event_id=f"e{i}")
                for i in range(5)
            ]
        )
        mock_ct_client_factory.return_value = ct_client

        sm_client = MagicMock()
        sm_client.describe_endpoint_config.return_value = {
            "ProductionVariants": [{"InstanceType": "ml.g5.2xlarge"}],
            "ShadowProductionVariants": [],
        }
        mock_sm_client_factory.return_value = sm_client

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", deployment_target="sagemaker"
        )

        assert result["event_count"] == 5
        # 5 events, 1 shared endpoint config, 1 DescribeEndpointConfig call.
        assert sm_client.describe_endpoint_config.call_count == 1

    @patch("src.tools.ice_history._sagemaker_client")
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_inline_extraction_skips_describe(
        self, mock_ct_client_factory, mock_sm_client_factory
    ):
        """When CloudTrail already carries the instance type inline (rare
        but supported), DescribeEndpointConfig must NOT be called."""
        ct_client = MagicMock()
        ct_client.lookup_events.side_effect = _events_only_on_create_endpoint(
            [
                _sagemaker_event(
                    endpoint_config_name="cfg-inline",
                    inline_instance_type="ml.g5.2xlarge",
                )
            ]
        )
        mock_ct_client_factory.return_value = ct_client

        sm_client = MagicMock()
        mock_sm_client_factory.return_value = sm_client

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", deployment_target="sagemaker"
        )

        assert result["event_count"] == 1
        sm_client.describe_endpoint_config.assert_not_called()
        assert result["sample_events"][0]["instance_type_source"] == "cloudtrail_inline"


class TestSageMakerUnresolvedPath:
    @patch("src.tools.ice_history._sagemaker_client")
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_describe_denied_produces_unresolved_signal(
        self, mock_ct_client_factory, mock_sm_client_factory
    ):
        """Follow-up A safety net: when DescribeEndpointConfig fails
        (AccessDenied, deleted config, throttled), the event contributes
        to ``unresolved_event_count`` not ``event_count``, and the
        capacity_signal becomes ``"unresolved"`` when there are no
        successfully-resolved events. The scorer's ice_signal_unavailable
        cap then refuses DEPLOYABLE.
        """
        ct_client = MagicMock()
        ct_client.lookup_events.side_effect = _events_only_on_create_endpoint(
            [
                _sagemaker_event(endpoint_config_name="cfg-denied", event_id="e1"),
                _sagemaker_event(endpoint_config_name="cfg-denied", event_id="e2"),
            ]
        )
        mock_ct_client_factory.return_value = ct_client

        sm_client = MagicMock()
        sm_client.describe_endpoint_config.side_effect = RuntimeError("AccessDenied")
        mock_sm_client_factory.return_value = sm_client

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", deployment_target="sagemaker"
        )

        assert result["event_count"] == 0
        assert result["unresolved_event_count"] == 2
        assert result["capacity_signal"] == "unresolved"
        assert (
            "sagemaker:DescribeEndpointConfig" in result["interpretation"]
            or "deleted endpoint config" in result["interpretation"]
        )

    @patch("src.tools.ice_history._sagemaker_client")
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_mixed_resolved_and_unresolved(
        self, mock_ct_client_factory, mock_sm_client_factory
    ):
        """When some events resolve and others don't, ``event_count``
        reflects the resolved subset and ``unresolved_event_count``
        captures the rest. capacity_signal is derived from the resolved
        count only.
        """
        ct_client = MagicMock()
        ct_client.lookup_events.side_effect = _events_only_on_create_endpoint(
            [
                _sagemaker_event(endpoint_config_name="cfg-ok", event_id="e1"),
                _sagemaker_event(endpoint_config_name="cfg-bad", event_id="e2"),
                _sagemaker_event(endpoint_config_name="cfg-bad", event_id="e3"),
            ]
        )
        mock_ct_client_factory.return_value = ct_client

        sm_client = MagicMock()

        def _describe(EndpointConfigName):
            if EndpointConfigName == "cfg-ok":
                return {
                    "ProductionVariants": [{"InstanceType": "ml.g5.2xlarge"}],
                    "ShadowProductionVariants": [],
                }
            raise RuntimeError("AccessDenied")

        sm_client.describe_endpoint_config.side_effect = _describe
        mock_sm_client_factory.return_value = sm_client

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", deployment_target="sagemaker"
        )

        assert result["event_count"] == 1
        assert result["unresolved_event_count"] == 2
        # 1 matched event → "isolated" band per the classifier.
        assert result["capacity_signal"] == "isolated"


class TestScorerHandlesUnresolvedSignal:
    def test_unresolved_caps_verdict_at_deployable_with_preparation(self):
        """End-to-end: the scorer must not return DEPLOYABLE off an
        ``unresolved`` capacity signal, just like it doesn't for
        ``unknown`` or ``rate_limited``.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            customer_ice_events=None,
            customer_ice_capacity_signal="unresolved",
            region="us-east-1",
        )
        assert result["verdict"] == "DEPLOYABLE WITH PREPARATION"
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_signal_unavailable" in kinds
        # The description must explain the SageMaker DescribeEndpointConfig
        # cause so operators can act on it.
        cap = next(
            c for c in result["applied_caps"] if c["kind"] == "ice_signal_unavailable"
        )
        assert "DescribeEndpointConfig" in cap["description"]

    def test_odcr_full_coverage_still_defeats_unresolved(self):
        """An ODCR that fully covers the ask still defeats the cap —
        reserved capacity doesn't care about shared-pool history at all.
        """
        result = compute_confidence(
            instance_available=True,
            pricing_available=True,
            quota_limit=100,
            requested_count=5,
            spot_placement_score=10,
            customer_ice_events=None,
            customer_ice_capacity_signal="unresolved",
            odcr_available_capacity=5,
            region="us-east-1",
        )
        kinds = {c["kind"] for c in result["applied_caps"]}
        assert "ice_signal_unavailable" not in kinds


# --------------------------------------------------------------------------
# Template regression — the Lambda role has the new Sid.
# --------------------------------------------------------------------------


class TestSageMakerDescribeIamGrant:
    def test_lambda_role_has_sagemaker_describe_endpoint_config(self):
        """Follow-up A regression guard: the Lambda role MUST include
        sagemaker:DescribeEndpointConfig, otherwise the SageMaker branch
        of get_ice_history silently returns capacity_signal='unresolved'
        for every SageMaker customer and every verdict caps at
        DEPLOYABLE WITH PREPARATION.
        """
        from pathlib import Path

        import yaml

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

        template_path = Path(__file__).parent.parent / "template.yaml"
        with template_path.open() as f:
            template = yaml.load(f, Loader=_CfnLoader)  # nosec B506 - _CfnLoader subclasses yaml.SafeLoader; parses trusted local CloudFormation templates

        policies = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Policies"
        ]
        statements = policies[0]["Statement"]

        # Sid could be a plain statement OR wrapped in Fn::If.
        found = False
        for stmt in statements:
            if not isinstance(stmt, dict):
                continue
            inner = stmt
            if "Fn::If" in stmt:
                # Only the true-branch counts as an active grant.
                inner = stmt["Fn::If"][1]
            if not isinstance(inner, dict):
                continue
            if inner.get("Sid") == "SageMakerDescribeRead":
                actions = inner.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                assert "sagemaker:DescribeEndpointConfig" in actions
                found = True
                break

        assert found, (
            "template.yaml must include the SageMakerDescribeRead IAM "
            "statement granting sagemaker:DescribeEndpointConfig. See "
            "src/tools/ice_history.py::_resolve_instance_types_via_describe "
            "for why."
        )
