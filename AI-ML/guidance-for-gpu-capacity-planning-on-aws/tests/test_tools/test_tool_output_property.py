# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Property-based test for tool output structure invariant.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

Property 1: Tool output structure invariant — For any tool module
(instance_availability, pricing, service_quotas, alternatives) and for any
mocked AWS API response (success or error), the tool output SHALL always be
a dict containing consistent structural fields, and the agent's tool execution
wrapper SHALL always produce a dict with `ok` (bool), and either `data` (on
success) or `error` (on failure).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.tools.instance_availability import check_instance_availability
from src.tools.pricing import get_sagemaker_pricing
from src.tools.service_quotas import get_sagemaker_quota
from src.tools.alternatives import find_alternative_instances
from src.agent import CapacityDecisionAgent


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating varied AWS API responses
# ---------------------------------------------------------------------------

# Strategy for instance types
instance_type_st = st.sampled_from(
    [
        "g5.xlarge",
        "g5.2xlarge",
        "g6.2xlarge",
        "g6e.2xlarge",
        "g7e.2xlarge",
        "p4d.24xlarge",
        "p5.48xlarge",
        "inf2.xlarge",
        "trn1.32xlarge",
        "ml.g5.xlarge",
        "ml.p4d.24xlarge",
        "nonexistent.type",
        "x99.mega",
    ]
)

# Strategy for AWS regions
region_st = st.sampled_from(
    [
        "us-east-1",
        "us-west-2",
        "eu-west-1",
        "ap-southeast-1",
        "sa-east-1",
        "af-south-1",
        "xx-fake-99",
        "invalid-region",
    ]
)

# Strategy for generating mock EC2 DescribeInstanceTypeOfferings responses
ec2_offerings_response_st = st.one_of(
    # Empty offerings (not found)
    st.just({"InstanceTypeOfferings": []}),
    # Single offering
    st.builds(
        lambda itype, loc: {
            "InstanceTypeOfferings": [{"InstanceType": itype, "Location": loc}]
        },
        itype=st.sampled_from(["g5.xlarge", "g6.2xlarge", "p4d.24xlarge"]),
        loc=st.sampled_from(["us-east-1", "us-east-1a", "us-west-2a"]),
    ),
    # Multiple offerings
    st.builds(
        lambda itypes: {
            "InstanceTypeOfferings": [
                {"InstanceType": t, "Location": f"us-east-1{c}"}
                for t, c in zip(itypes, "abcdef")
            ]
        },
        itypes=st.lists(
            st.sampled_from(["g5.xlarge", "g6.2xlarge", "g7e.2xlarge"]),
            min_size=1,
            max_size=4,
        ),
    ),
)

# Strategy for generating mock NON-retryable exceptions.
#
# ``TimeoutError`` and ``ConnectionError`` are retryable per
# ``src/retry_handler.py::_is_retryable`` and now propagate out of
# ``_execute_tool`` (retry fix from the code review) so the outer
# ``with_retry`` wrapper can back
# off. This strategy is used by properties that assert the exception-to-
# error-dict wrapping behavior, which only holds for non-retryable errors.
exception_st = st.sampled_from(
    [
        RuntimeError("Simulated failure"),
        ValueError("Invalid parameter"),
        OSError("Network unreachable"),
    ]
)

# Strategy for pricing API responses
pricing_response_st = st.one_of(
    # Empty price list
    st.just({"PriceList": []}),
    # Valid price list with extractable rate
    st.builds(
        lambda rate: {
            "PriceList": [
                '{"terms": {"OnDemand": {"SKU.TERM": {"priceDimensions": {"SKU.TERM.DIM": {"pricePerUnit": {"USD": "'
                + str(rate)
                + '"}}}}}}}'
            ]
        },
        rate=st.floats(
            min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False
        ),
    ),
    # Price list with no extractable rate
    st.just({"PriceList": ['{"terms": {"OnDemand": {}}}']}),
)

# Strategy for service quotas responses
quotas_page_st = st.one_of(
    # No quotas
    st.just([{"Quotas": []}]),
    # Quotas that won't match (different instance type)
    st.just(
        [
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-99999",
                        "QuotaName": "ml.m5.xlarge for endpoint usage",
                        "Value": 10.0,
                        "Adjustable": True,
                        "Unit": "None",
                    }
                ]
            }
        ]
    ),
    # Quotas that will match g5
    st.just(
        [
            {
                "Quotas": [
                    {
                        "QuotaCode": "L-12345",
                        "QuotaName": "ml.g5.xlarge for endpoint usage",
                        "Value": 4.0,
                        "Adjustable": True,
                        "Unit": "None",
                    }
                ]
            }
        ]
    ),
)


# ---------------------------------------------------------------------------
# Property test: Tool output is always a dict (never raises)
# ---------------------------------------------------------------------------


class TestToolOutputStructureInvariant:
    """Property 1: Tool output structure invariant.

    For any tool module and for any mocked AWS API response (success or error),
    the tool output SHALL always be a dict.
    """

    @given(
        instance_type=instance_type_st,
        region=region_st,
        response=ec2_offerings_response_st,
    )
    @settings(max_examples=50)
    def test_instance_availability_always_returns_dict(
        self, instance_type, region, response
    ):
        """**Validates: Requirements 1.1**

        check_instance_availability always returns a dict regardless of
        the mocked AWS response.
        """
        with patch("src.tools.instance_availability._ec2_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_client.describe_instance_type_offerings.return_value = response

            result = check_instance_availability(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "instance_type" in result
            assert "region" in result
            assert "source" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
        exc=exception_st,
    )
    @settings(max_examples=50)
    def test_instance_availability_handles_exceptions(self, instance_type, region, exc):
        """**Validates: Requirements 1.1**

        check_instance_availability returns a dict even when AWS API raises.
        """
        with patch("src.tools.instance_availability._ec2_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_client.describe_instance_type_offerings.side_effect = exc

            result = check_instance_availability(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "instance_type" in result
            assert "region" in result
            assert "interpretation" in result
            assert "source" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
        response=pricing_response_st,
    )
    @settings(max_examples=50)
    def test_pricing_always_returns_dict(self, instance_type, region, response):
        """**Validates: Requirements 1.2**

        get_sagemaker_pricing always returns a dict regardless of the
        mocked AWS response.
        """
        with patch("src.tools.pricing._pricing_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_client.get_products.return_value = response

            result = get_sagemaker_pricing(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "instance_type" in result
            assert "region" in result
            assert "source" in result
            assert "hourly_usd" in result
            assert "available" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
        exc=exception_st,
    )
    @settings(max_examples=50)
    def test_pricing_handles_exceptions(self, instance_type, region, exc):
        """**Validates: Requirements 1.2**

        get_sagemaker_pricing returns a dict even when AWS API raises.
        """
        with patch("src.tools.pricing._pricing_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_client.get_products.side_effect = exc

            result = get_sagemaker_pricing(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "instance_type" in result
            assert "region" in result
            assert "source" in result
            assert result["available"] is False

    @given(
        instance_type=instance_type_st,
        region=region_st,
        pages=quotas_page_st,
    )
    @settings(max_examples=50)
    def test_service_quotas_always_returns_dict(self, instance_type, region, pages):
        """**Validates: Requirements 1.3**

        get_sagemaker_quota always returns a dict regardless of the
        mocked AWS response.
        """
        with patch("src.tools.service_quotas._sq_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_paginator = MagicMock()
            mock_client.get_paginator.return_value = mock_paginator
            mock_paginator.paginate.return_value = pages

            result = get_sagemaker_quota(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "instance_type" in result
            assert "region" in result
            assert "found" in result
            assert "source" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
        exc=exception_st,
    )
    @settings(max_examples=50)
    def test_service_quotas_handles_exceptions(self, instance_type, region, exc):
        """**Validates: Requirements 1.3**

        get_sagemaker_quota returns a dict even when AWS API raises.
        """
        with patch("src.tools.service_quotas._sq_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_client.get_paginator.side_effect = exc

            result = get_sagemaker_quota(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "instance_type" in result
            assert "region" in result
            assert result["found"] is False
            assert "source" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
        exc=exception_st,
    )
    @settings(max_examples=50)
    def test_alternatives_handles_exceptions(self, instance_type, region, exc):
        """**Validates: Requirements 1.4**

        find_alternative_instances returns a dict even when AWS API raises.
        """
        with patch("src.tools.alternatives._ec2_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client
            mock_client.describe_instance_types.side_effect = exc

            result = find_alternative_instances(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "reference" in result
            assert "region" in result
            assert "alternatives" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
    )
    @settings(max_examples=50)
    def test_alternatives_always_returns_dict(self, instance_type, region):
        """**Validates: Requirements 1.4**

        find_alternative_instances always returns a dict regardless of
        the mocked AWS response.
        """
        with patch("src.tools.alternatives._ec2_client") as mock_factory:
            mock_client = MagicMock()
            mock_factory.return_value = mock_client

            # Mock describe_instance_types to return valid specs
            mock_client.describe_instance_types.return_value = {
                "InstanceTypes": [
                    {
                        "InstanceType": instance_type.removeprefix("ml."),
                        "VCpuInfo": {"DefaultVCpus": 8},
                        "MemoryInfo": {"SizeInMiB": 32768},
                        "GpuInfo": {
                            "Gpus": [
                                {
                                    "Count": 1,
                                    "MemoryInfo": {"SizeInMiB": 24576},
                                    "Manufacturer": "NVIDIA",
                                }
                            ]
                        },
                    }
                ]
            }

            # Mock paginator to return some offerings
            mock_paginator = MagicMock()
            mock_client.get_paginator.return_value = mock_paginator
            mock_paginator.paginate.return_value = [
                {"InstanceTypeOfferings": [{"InstanceType": "g5.xlarge"}]}
            ]

            result = find_alternative_instances(instance_type, region)

            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "reference" in result
            assert "region" in result

    @given(
        instance_type=instance_type_st,
        region=region_st,
    )
    @settings(max_examples=50)
    def test_execute_tool_wrapper_structure(self, instance_type, region):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

        The agent's _execute_tool wrapper always produces a dict with
        `ok` (bool) and either `data` (when ok=True) or `error` (when ok=False).
        """
        agent = CapacityDecisionAgent.__new__(CapacityDecisionAgent)
        agent.tools = {}
        agent.verbose = False

        # Register a tool that returns a dict (simulating success)
        def mock_handler(**kwargs):
            return {"instance_type": instance_type, "region": region, "available": True}

        agent.tools["test_tool"] = {
            "description": "test",
            "input_schema": {},
            "handler": mock_handler,
        }

        result = agent._execute_tool(
            "test_tool", {"instance_type": instance_type, "region": region}
        )

        assert isinstance(result, dict)
        assert "ok" in result
        assert isinstance(result["ok"], bool)
        if result["ok"]:
            assert "data" in result
        else:
            assert "error" in result

    @given(exc=exception_st)
    @settings(max_examples=20)
    def test_execute_tool_wrapper_handles_exceptions(self, exc):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

        The agent's _execute_tool wrapper catches exceptions and returns
        a dict with ok=False and an error message.
        """
        agent = CapacityDecisionAgent.__new__(CapacityDecisionAgent)
        agent.tools = {}
        agent.verbose = False

        def raising_handler(**kwargs):
            raise exc

        agent.tools["failing_tool"] = {
            "description": "test",
            "input_schema": {},
            "handler": raising_handler,
        }

        result = agent._execute_tool("failing_tool", {})

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert "error" in result
        assert isinstance(result["error"], str)

    @settings(max_examples=20)
    @given(
        tool_name=st.text(min_size=1, max_size=20).filter(
            lambda x: (
                x
                not in [
                    "check_instance_availability",
                    "get_sagemaker_pricing",
                    "get_sagemaker_quota",
                    "find_alternative_instances",
                ]
            )
        )
    )
    def test_execute_tool_unknown_tool_returns_error(self, tool_name):
        """**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

        The agent's _execute_tool returns an error dict for unknown tool names.
        """
        agent = CapacityDecisionAgent.__new__(CapacityDecisionAgent)
        agent.tools = {}
        agent.verbose = False

        result = agent._execute_tool(tool_name, {})

        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str)
