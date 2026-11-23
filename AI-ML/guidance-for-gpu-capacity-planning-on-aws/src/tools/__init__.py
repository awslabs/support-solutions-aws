# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tools the agent can call to gather facts — all use public AWS APIs."""

try:
    from tools.instance_availability import TOOL_SPEC as INSTANCE_AVAILABILITY_SPEC  # noqa: F401
    from tools.pricing import TOOL_SPEC as PRICING_SPEC  # noqa: F401
    from tools.ec2_pricing import TOOL_SPEC as EC2_PRICING_SPEC  # noqa: F401
    from tools.service_quotas import TOOL_SPEC as SERVICE_QUOTAS_SPEC  # noqa: F401
    from tools.alternatives import TOOL_SPEC as ALTERNATIVES_SPEC  # noqa: F401
    from tools.spot_placement_score import TOOL_SPEC as SPOT_PLACEMENT_SCORE_SPEC  # noqa: F401
    from tools.capacity_reservations import TOOL_SPEC as CAPACITY_RESERVATIONS_SPEC  # noqa: F401
    from tools.capacity_blocks import TOOL_SPEC as CAPACITY_BLOCKS_SPEC  # noqa: F401
    from tools.heterogeneous_endpoint import TOOL_SPEC as HETEROGENEOUS_ENDPOINT_SPEC  # noqa: F401
    from tools.ice_history import TOOL_SPEC as ICE_HISTORY_SPEC  # noqa: F401
    from tools.ice_metrics import TOOL_SPEC as ICE_METRICS_SPEC  # noqa: F401
    from tools.reference_data import TOOL_SPEC as REFERENCE_DATA_SPEC  # noqa: F401
except ImportError:
    from src.tools.instance_availability import TOOL_SPEC as INSTANCE_AVAILABILITY_SPEC  # noqa: F401
    from src.tools.pricing import TOOL_SPEC as PRICING_SPEC  # noqa: F401
    from src.tools.ec2_pricing import TOOL_SPEC as EC2_PRICING_SPEC  # noqa: F401
    from src.tools.service_quotas import TOOL_SPEC as SERVICE_QUOTAS_SPEC  # noqa: F401
    from src.tools.alternatives import TOOL_SPEC as ALTERNATIVES_SPEC  # noqa: F401
    from src.tools.spot_placement_score import TOOL_SPEC as SPOT_PLACEMENT_SCORE_SPEC  # noqa: F401
    from src.tools.capacity_reservations import TOOL_SPEC as CAPACITY_RESERVATIONS_SPEC  # noqa: F401
    from src.tools.capacity_blocks import TOOL_SPEC as CAPACITY_BLOCKS_SPEC  # noqa: F401
    from src.tools.heterogeneous_endpoint import (
        TOOL_SPEC as HETEROGENEOUS_ENDPOINT_SPEC,
    )  # noqa: F401
    from src.tools.ice_history import TOOL_SPEC as ICE_HISTORY_SPEC  # noqa: F401
    from src.tools.ice_metrics import TOOL_SPEC as ICE_METRICS_SPEC  # noqa: F401
    from src.tools.reference_data import TOOL_SPEC as REFERENCE_DATA_SPEC  # noqa: F401

ALL_TOOLS = [
    INSTANCE_AVAILABILITY_SPEC,
    PRICING_SPEC,
    EC2_PRICING_SPEC,
    SERVICE_QUOTAS_SPEC,
    ALTERNATIVES_SPEC,
    SPOT_PLACEMENT_SCORE_SPEC,
    CAPACITY_RESERVATIONS_SPEC,
    CAPACITY_BLOCKS_SPEC,
    HETEROGENEOUS_ENDPOINT_SPEC,
    ICE_METRICS_SPEC,
    ICE_HISTORY_SPEC,
    REFERENCE_DATA_SPEC,
]
