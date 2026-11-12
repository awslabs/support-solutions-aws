# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ice_history (CloudTrail-backed ICE lookup) tool."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.tools.ice_history import (
    DEFAULT_LOOKBACK_DAYS,
    EC2_CAPACITY_ERROR_PATTERNS,
    MAX_LOOKBACK_DAYS,
    MAX_PAGES,
    PAGE_SIZE,
    TOOL_SPEC,
    _extract_az,
    _extract_instance_type_from_ec2_event,
    _is_ice_error,
    get_ice_history,
)


# Convenience aliases for tests that only exercise the EC2 path.
def _extract_instance_type(payload):
    return _extract_instance_type_from_ec2_event(payload)


def _is_ec2_ice_error(code):
    return _is_ice_error(code, EC2_CAPACITY_ERROR_PATTERNS)


@pytest.fixture(autouse=True)
def clear_cloudtrail_cache():
    """No-op fixture preserved for test discovery (boto3 client caching was
    removed when tools moved to aws_session.get_client())."""
    yield


def _ct_event(
    *,
    event_id: str = "evt-1",
    event_time: datetime | None = None,
    error_code: str | None = "Client.InsufficientInstanceCapacity",
    instance_type: str = "g5.2xlarge",
    az: str | None = None,
    use_instances_set: bool = False,
    raw_override: str | None = None,
) -> dict:
    """Build a CloudTrail event dict shaped like the LookupEvents response."""
    if raw_override is not None:
        raw = raw_override
    else:
        payload: dict = {"errorCode": error_code} if error_code is not None else {}
        req: dict = {}
        if instance_type:
            if use_instances_set:
                req["instancesSet"] = {"items": [{"instanceType": instance_type}]}
            else:
                req["instanceType"] = instance_type
        if az:
            req["placement"] = {"availabilityZone": az}
        if req:
            payload["requestParameters"] = req
        raw = json.dumps(payload)

    return {
        "EventId": event_id,
        "EventTime": event_time or datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        "CloudTrailEvent": raw,
    }


class TestErrorMatching:
    """The ICE pattern matcher accepts the real-world error code shapes."""

    @pytest.mark.parametrize(
        "code",
        [
            "Client.InsufficientInstanceCapacity",
            "Server.InsufficientInstanceCapacity",
            "InsufficientInstanceCapacity",
            "InsufficientHostCapacity",
        ],
    )
    def test_matches_known_ice_codes(self, code):
        assert _is_ec2_ice_error(code) is True

    @pytest.mark.parametrize(
        "code",
        [
            "RequestLimitExceeded",
            "UnauthorizedOperation",
            "InvalidParameterValue",
            "",
            None,
            42,
        ],
    )
    def test_rejects_non_ice(self, code):
        assert _is_ec2_ice_error(code) is False


class TestInstanceTypeExtraction:
    def test_extracts_from_top_level_instance_type(self):
        payload = {"requestParameters": {"instanceType": "g5.2xlarge"}}
        assert _extract_instance_type(payload) == "g5.2xlarge"

    def test_extracts_from_instances_set(self):
        payload = {
            "requestParameters": {
                "instancesSet": {"items": [{"instanceType": "p4d.24xlarge"}]}
            }
        }
        assert _extract_instance_type(payload) == "p4d.24xlarge"

    def test_top_level_wins_over_instances_set(self):
        """If both shapes appear, prefer the top-level field."""
        payload = {
            "requestParameters": {
                "instanceType": "g5.2xlarge",
                "instancesSet": {"items": [{"instanceType": "p4d.24xlarge"}]},
            }
        }
        assert _extract_instance_type(payload) == "g5.2xlarge"

    def test_missing_request_parameters_returns_none(self):
        assert _extract_instance_type({}) is None
        assert _extract_instance_type({"requestParameters": None}) is None

    def test_malformed_instances_set_returns_none(self):
        payload = {"requestParameters": {"instancesSet": "not a dict"}}
        assert _extract_instance_type(payload) is None


class TestAZExtraction:
    def test_extracts_az_from_placement(self):
        payload = {
            "requestParameters": {"placement": {"availabilityZone": "us-east-1a"}}
        }
        assert _extract_az(payload) == "us-east-1a"

    def test_returns_none_when_missing(self):
        assert _extract_az({"requestParameters": {}}) is None
        assert _extract_az({}) is None


class TestCleanHistory:
    """When no ICE events exist, capacity_signal = 'clean'."""

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_empty_response(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 0
        assert result["events_by_az"] == {}
        assert result["sample_events"] == []
        assert result["capacity_signal"] == "clean"
        assert "No capacity errors" in result["interpretation"]
        assert result["source"] == "cloudtrail.LookupEvents"
        assert result["truncated"] is False

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_non_ice_events_filtered_out(self, mock_factory):
        """RunInstances events with non-ICE error codes should be ignored."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [
                _ct_event(error_code="RequestLimitExceeded"),
                _ct_event(error_code="UnauthorizedOperation"),
                _ct_event(error_code=None),  # successful launch
            ]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 0
        assert result["capacity_signal"] == "clean"

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_ice_for_different_instance_type_filtered_out(self, mock_factory):
        """ICE events for a different instance type should NOT count."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [
                _ct_event(instance_type="p4d.24xlarge"),  # different type, ICE
                _ct_event(instance_type="g6.xlarge"),  # different type, ICE
            ]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 0
        assert result["capacity_signal"] == "clean"


class TestCapacitySignalBands:
    """Verify the count → signal mapping at each band."""

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_isolated_band_1_to_3_events(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(event_id=f"evt-{i}") for i in range(2)]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 2
        assert result["capacity_signal"] == "isolated"
        assert "isolated incidents" in result["interpretation"]

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_recurring_band_4_to_10_events(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(event_id=f"evt-{i}") for i in range(7)]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 7
        assert result["capacity_signal"] == "recurring"
        assert "recurring capacity issues" in result["interpretation"]

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_severe_band_above_10_events(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(event_id=f"evt-{i}") for i in range(25)]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 25
        assert result["capacity_signal"] == "severe"
        assert (
            "ODCR" in result["interpretation"]
            or "Capacity Blocks" in result["interpretation"]
        )


class TestAZBreakdown:
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_groups_events_by_az(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [
                _ct_event(event_id="e1", az="us-east-1a"),
                _ct_event(event_id="e2", az="us-east-1a"),
                _ct_event(event_id="e3", az="us-east-1b"),
                _ct_event(event_id="e4"),  # no AZ
            ]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 4
        assert result["events_by_az"] == {
            "us-east-1a": 2,
            "us-east-1b": 1,
            "unknown": 1,
        }


class TestSampleAndDetail:
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_returns_at_most_5_sample_events(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(event_id=f"evt-{i}") for i in range(8)]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 8
        assert len(result["sample_events"]) == 5

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_sample_event_shape(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        ts = datetime(2026, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        mock_client.lookup_events.return_value = {
            "Events": [
                _ct_event(event_id="evt-x", event_time=ts, az="us-east-1a"),
            ]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        sample = result["sample_events"][0]
        assert sample["event_id"] == "evt-x"
        assert sample["event_time"] == "2026-06-15T10:30:00+00:00"
        assert sample["error_code"] == "Client.InsufficientInstanceCapacity"
        assert sample["availability_zone"] == "us-east-1a"


class TestNormalizationAndArgs:
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_strips_ml_prefix(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(instance_type="g5.2xlarge")]
        }

        result = get_ice_history("ml.g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["instance_type"] == "g5.2xlarge"
        assert result["event_count"] == 1

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_matches_event_with_ml_prefix_in_payload(self, mock_factory):
        """Some payloads may have ml. on the requested type — normalize on both sides."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(instance_type="ml.g5.2xlarge")]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 1

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_lookback_clamped_to_max(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", lookback_days=500, deployment_target="ec2"
        )

        assert result["lookback_days"] == MAX_LOOKBACK_DAYS

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_lookback_clamped_to_min(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        result = get_ice_history(
            "g5.2xlarge", "us-east-1", lookback_days=0, deployment_target="ec2"
        )

        assert result["lookback_days"] == 1

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_default_lookback(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["lookback_days"] == DEFAULT_LOOKBACK_DAYS

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_passes_event_name_filter_for_default_auto_target(self, mock_factory):
        """F1: default deployment_target='auto' issues one query per
        event name across BOTH EC2 (RunInstances) and SageMaker
        (CreateEndpoint / UpdateEndpoint / UpdateEndpointWeightsAndCapacities).
        Every call must carry a LookupAttribute keyed on EventName and
        use PAGE_SIZE as the MaxResults.
        """
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        # Deliberately do NOT pass deployment_target — exercise the default.
        get_ice_history("g5.2xlarge", "us-east-1")

        # Collect every EventName the tool asked for.
        seen_event_names = set()
        for call in mock_client.lookup_events.call_args_list:
            kwargs = call[1]
            for attr in kwargs["LookupAttributes"]:
                if attr["AttributeKey"] == "EventName":
                    seen_event_names.add(attr["AttributeValue"])
            assert kwargs["MaxResults"] == PAGE_SIZE

        # Must include RunInstances (EC2) and at least one SageMaker event.
        assert "RunInstances" in seen_event_names
        assert seen_event_names & {
            "CreateEndpoint",
            "UpdateEndpoint",
            "UpdateEndpointWeightsAndCapacities",
        }

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_ec2_target_only_queries_run_instances(self, mock_factory):
        """deployment_target='ec2' must NOT touch the SageMaker namespace."""
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        event_names = {
            attr["AttributeValue"]
            for call in mock_client.lookup_events.call_args_list
            for attr in call[1]["LookupAttributes"]
            if attr["AttributeKey"] == "EventName"
        }
        assert event_names == {"RunInstances"}

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_sagemaker_target_only_queries_sagemaker_events(self, mock_factory):
        """deployment_target='sagemaker' must NOT query RunInstances —
        SageMaker capacity failures don't appear there. This is the P0
        defect the split fixes.
        """
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {"Events": []}

        get_ice_history("g5.2xlarge", "us-east-1", deployment_target="sagemaker")

        event_names = {
            attr["AttributeValue"]
            for call in mock_client.lookup_events.call_args_list
            for attr in call[1]["LookupAttributes"]
            if attr["AttributeKey"] == "EventName"
        }
        assert "RunInstances" not in event_names
        assert event_names.issubset(
            {
                "CreateEndpoint",
                "UpdateEndpoint",
                "UpdateEndpointWeightsAndCapacities",
            }
        )


class TestPagination:
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_aggregates_across_pages(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.side_effect = [
            {
                "Events": [_ct_event(event_id="e1")],
                "NextToken": "page2",
            },
            {
                "Events": [_ct_event(event_id="e2")],
                # no NextToken — done
            },
        ]

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 2
        assert result["truncated"] is False
        # Second call must include the NextToken
        second_call = mock_client.lookup_events.call_args_list[1][1]
        assert second_call.get("NextToken") == "page2"

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_truncation_flag_when_cap_hit(self, mock_factory):
        """If the pagination cap is reached OR the early-exit threshold is
        hit while NextToken is still set, ``truncated`` must be True and
        the interpretation must note the cap.

        H6 update: the tool now stops paginating once the match count
        exceeds ``EARLY_EXIT_MATCH_COUNT`` because the deterministic
        scorer's HIGH-RISK ceiling already fires by then — more precision
        past that point is wasted latency.
        """
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # Every page returns events + NextToken — never stops
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(event_id=f"e{i}") for i in range(PAGE_SIZE)],
            "NextToken": "more",
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["truncated"] is True
        assert result["capacity_signal"] == "severe"  # well over 10 events
        assert "pagination cap" in result["interpretation"]
        # Must have called AT LEAST once and AT MOST MAX_PAGES times.
        # The early-exit optimisation may stop us before page MAX_PAGES.
        assert 1 <= mock_client.lookup_events.call_count <= MAX_PAGES

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_max_pages_bounded_and_documented(self, mock_factory):
        """F1 raised MAX_PAGES from 3 to 5 to accommodate the second
        (SageMaker) event-name query. The cap must still stay bounded
        so a pathologically active account doesn't burn the entire
        Lambda budget on one tool call.
        """
        from src.tools.ice_history import MAX_PAGES

        assert MAX_PAGES <= 5, (
            f"MAX_PAGES was raised to {MAX_PAGES}. CloudTrail LookupEvents "
            f"is throttled at ~2 req/s; more than ~5 pages per event name "
            f"risks pushing the 29s API Gateway timeout."
        )

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_early_exit_after_severe_threshold(self, mock_factory):
        """H6 optimisation: once we've collected enough matches to guarantee
        the deterministic scorer will apply its severe-ICE ceiling, we stop
        paginating. Additional pages don't change the verdict — they just
        add latency and CloudTrail throttle risk.
        """
        from src.tools.ice_history import EARLY_EXIT_MATCH_COUNT

        # Make each page return enough matches to blow past the threshold
        # in one call, so we can verify only ONE call happened even with a
        # NextToken advertising more.
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        events_per_page = EARLY_EXIT_MATCH_COUNT + 5
        mock_client.lookup_events.return_value = {
            "Events": [_ct_event(event_id=f"e{i}") for i in range(events_per_page)],
            "NextToken": "more",
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        # Early exit — only one page fetched even though NextToken was set.
        assert mock_client.lookup_events.call_count == 1
        assert result["truncated"] is True
        # Signal still classified as severe (which is why early exit is safe).
        assert result["capacity_signal"] == "severe"


class TestMalformedPayloads:
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_malformed_cloudtrail_event_json_skipped(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [
                _ct_event(raw_override="not valid json"),
                _ct_event(),  # one good event
            ]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 1

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_missing_cloudtrail_event_field_skipped(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.return_value = {
            "Events": [
                {"EventId": "no-payload"},  # missing CloudTrailEvent
                _ct_event(),
            ]
        }

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] == 1


class TestApiError:
    @patch("src.tools.ice_history._cloudtrail_client")
    def test_unauthorized_returns_unknown_signal(self, mock_factory):
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.side_effect = Exception("UnauthorizedOperation")

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["event_count"] is None
        assert result["capacity_signal"] == "unknown"
        assert "cloudtrail:LookupEvents" in result["interpretation"]
        assert result["source"] == "cloudtrail.LookupEvents"

    @patch("src.tools.ice_history._cloudtrail_client")
    def test_client_error_returns_unknown_signal(self, mock_factory):
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        mock_client.lookup_events.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "LookupEvents",
        )

        result = get_ice_history("g5.2xlarge", "us-east-1", deployment_target="ec2")

        assert result["capacity_signal"] == "unknown"
        assert "ClientError" in result["interpretation"]


class TestToolSpec:
    def test_tool_spec_has_required_fields(self):
        assert TOOL_SPEC["name"] == "get_ice_history"
        assert TOOL_SPEC["handler"] is get_ice_history
        schema = TOOL_SPEC["input_schema"]
        assert "instance_type" in schema["required"]
        assert "region" in schema["required"]
        assert "lookback_days" in schema["properties"]
        assert "lookback_days" not in schema["required"]
