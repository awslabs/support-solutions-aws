# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sustainability pillar validator for SageMaker resources."""

from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    PILLAR_SUSTAINABILITY,
    RESOURCE_ENDPOINT,
    RESOURCE_NOTEBOOK,
    RESOURCE_TRAINING_JOB,
    SEVERITY_LOW,
)
from botocore.exceptions import ClientError


GRAVITON_INSTANCE_PREFIXES = ['ml.m6g', 'ml.c6g', 'ml.m7g', 'ml.c7g', 'ml.r6g', 'ml.r7g']
X86_INSTANCE_PREFIXES = ['ml.m5.', 'ml.c5.', 'ml.m4.', 'ml.c4.']
LARGE_NOTEBOOK_SIZES = [
    '2xlarge',
    '4xlarge',
    '8xlarge',
    '12xlarge',
    '16xlarge',
    '24xlarge',
]

LIFECYCLE_TAGS = {
    'CreatedDate',
    'created-date',
    'ExpiryDate',
    'expiry-date',
    'TTL',
    'ttl',
    'Expiration',
    'expiration',
}


def _get_resource_name(resource_info: dict) -> str:
    """Extract resource name from resource info."""
    return (
        resource_info.get('EndpointName')
        or resource_info.get('TrainingJobName')
        or resource_info.get('NotebookInstanceName')
        or resource_info.get('ModelName')
        or 'unknown'
    )


def _finding(severity: str, resource: str, check: str, detail: str, recommendation: str) -> dict:
    """Create a sustainability finding."""
    return {
        'pillar': PILLAR_SUSTAINABILITY,
        'severity': severity,
        'resource': resource,
        'check': check,
        'detail': detail,
        'recommendation': recommendation,
    }


def validate_sustainability(
    resource_type: str,
    resource_info: dict,
    tags: list[dict],
    region_name: str | None = None,
) -> list[dict]:
    """Validate a SageMaker resource against Sustainability pillar best practices.

    Checks (4):
    44. Graviton instances for energy efficiency
    45. List active endpoints for cleanup review
    46. Notebook instances review for potential deletion
    47. Lifecycle tags (CreatedDate, ExpiryDate) for automated cleanup

    Also checks: spot training for sustainability, oversized notebooks.

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        tags: Resource tags
        region_name: AWS region name

    Returns:
        List of sustainability findings
    """
    findings: list[dict] = []
    name = _get_resource_name(resource_info)

    # Check 44: Graviton instances for energy efficiency
    if resource_type == RESOURCE_ENDPOINT:
        config_name = resource_info.get('EndpointConfigName')
        if config_name:
            try:
                sm = AwsHelper.create_boto3_client('sagemaker', region_name=region_name)
                config = sm.describe_endpoint_config(EndpointConfigName=config_name)
                for v in config.get('ProductionVariants', []):
                    itype = v.get('InstanceType', '')
                    is_graviton = any(itype.startswith(p) for p in GRAVITON_INSTANCE_PREFIXES)
                    is_x86 = any(prefix in itype for prefix in X86_INSTANCE_PREFIXES)
                    if is_x86 and not is_graviton:
                        findings.append(
                            _finding(
                                SEVERITY_LOW,
                                name,
                                'consider-graviton-endpoint',
                                f"Endpoint uses x86 instance '{itype}'.",
                                'Consider AWS Graviton instances for up to 60% less energy consumption.',
                            )
                        )
            except ClientError:
                pass

    if resource_type == RESOURCE_TRAINING_JOB:
        itype = resource_info.get('ResourceConfig', {}).get('InstanceType', '')
        is_graviton = any(itype.startswith(p) for p in GRAVITON_INSTANCE_PREFIXES)
        is_x86 = any(prefix in itype for prefix in X86_INSTANCE_PREFIXES)
        if is_x86 and not is_graviton:
            findings.append(
                _finding(
                    SEVERITY_LOW,
                    name,
                    'consider-graviton',
                    f"Training job uses x86 instance type '{itype}'.",
                    'Consider Graviton-based instances (ml.m6g, ml.c6g) for better energy efficiency where compatible.',
                )
            )

        # Existing: spot training for sustainability — removed (duplicate of cost check)

    # Check 45: Active endpoints for cleanup review
    if resource_type == RESOURCE_ENDPOINT:
        status = resource_info.get('EndpointStatus', '')
        if status == 'InService':
            creation_time = resource_info.get('CreationTime')
            if creation_time:
                import datetime

                now = datetime.datetime.now(datetime.timezone.utc)
                if hasattr(creation_time, 'tzinfo'):
                    age_days = (now - creation_time).days
                else:
                    age_days = 0
                if age_days > 90:
                    findings.append(
                        _finding(
                            SEVERITY_LOW,
                            name,
                            'long-running-endpoint',
                            f'Endpoint has been running for {age_days} days.',
                            'Review long-running endpoints for potential cleanup of unused resources.',
                        )
                    )

    # Check 46: Notebook instances review for deletion
    if resource_type == RESOURCE_NOTEBOOK:
        status = resource_info.get('NotebookInstanceStatus', '')
        if status == 'Stopped':
            last_modified = resource_info.get('LastModifiedTime')
            if last_modified:
                import datetime

                now = datetime.datetime.now(datetime.timezone.utc)
                if hasattr(last_modified, 'tzinfo'):
                    idle_days = (now - last_modified).days
                else:
                    idle_days = 0
                if idle_days > 30:
                    findings.append(
                        _finding(
                            SEVERITY_LOW,
                            name,
                            'stale-notebook',
                            f'Notebook instance has been stopped for {idle_days} days.',
                            'Review stopped notebook instances for potential deletion to reduce resource footprint.',
                        )
                    )

        # Existing: oversized notebook
        itype = resource_info.get('InstanceType', '')
        if any(size in itype for size in LARGE_NOTEBOOK_SIZES):
            findings.append(
                _finding(
                    SEVERITY_LOW,
                    name,
                    'oversized-notebook',
                    f"Notebook instance type '{itype}' may be larger than needed.",
                    'Right-size notebook instances. Use smaller instances for development and scale up only when needed.',
                )
            )

    # Check 47 removed: lifecycle tags (generic governance)

    return findings
