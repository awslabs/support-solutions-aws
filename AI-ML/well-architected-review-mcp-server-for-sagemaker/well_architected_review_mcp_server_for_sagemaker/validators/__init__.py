# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Well-Architected pillar validators."""

from well_architected_review_mcp_server_for_sagemaker.validators.security import validate_security
from well_architected_review_mcp_server_for_sagemaker.validators.reliability import validate_reliability
from well_architected_review_mcp_server_for_sagemaker.validators.performance import validate_performance
from well_architected_review_mcp_server_for_sagemaker.validators.cost import validate_cost
from well_architected_review_mcp_server_for_sagemaker.validators.operational_excellence import (
    validate_operational_excellence,
)
from well_architected_review_mcp_server_for_sagemaker.validators.sustainability import validate_sustainability


def run_all_validators(
    resource_type: str,
    resource_info: dict,
    tags: list[dict],
    region_name: str | None = None,
) -> list[dict]:
    """Run all pillar validators against a resource.

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        tags: Resource tags
        region_name: AWS region name

    Returns:
        List of finding dictionaries
    """
    findings: list[dict] = []
    findings.extend(validate_security(resource_type, resource_info, tags, region_name))
    findings.extend(validate_reliability(resource_type, resource_info, region_name))
    findings.extend(validate_performance(resource_type, resource_info, region_name))
    findings.extend(validate_cost(resource_type, resource_info, tags, region_name))
    findings.extend(
        validate_operational_excellence(resource_type, resource_info, tags, region_name)
    )
    findings.extend(validate_sustainability(resource_type, resource_info, tags, region_name))
    return findings
