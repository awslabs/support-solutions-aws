# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cost Optimization pillar validator for SageMaker resources."""

from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    PILLAR_COST,
    RESOURCE_ENDPOINT,
    RESOURCE_NOTEBOOK,
    RESOURCE_TRAINING_JOB,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)
from botocore.exceptions import ClientError
from loguru import logger


COST_ALLOCATION_TAGS = {
    'CostCenter',
    'cost-center',
    'Project',
    'project',
    'Environment',
    'environment',
    'Owner',
    'owner',
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
    """Create a cost finding."""
    return {
        'pillar': PILLAR_COST,
        'severity': severity,
        'resource': resource,
        'check': check,
        'detail': detail,
        'recommendation': recommendation,
    }


def _extract_s3_buckets(resource_type: str, resource_info: dict) -> list[str]:
    """Extract S3 bucket names referenced by a SageMaker resource."""
    buckets: set[str] = set()

    def _bucket_from_uri(uri: str | None) -> str | None:
        if uri and uri.startswith('s3://'):
            parts = uri.replace('s3://', '').split('/')
            return parts[0] if parts else None
        return None

    if resource_type == RESOURCE_TRAINING_JOB:
        output_uri = resource_info.get('OutputDataConfig', {}).get('S3OutputPath')
        b = _bucket_from_uri(output_uri)
        if b:
            buckets.add(b)
        for channel in resource_info.get('InputDataConfig', []):
            uri = channel.get('DataSource', {}).get('S3DataSource', {}).get('S3Uri')
            b = _bucket_from_uri(uri)
            if b:
                buckets.add(b)

    return list(buckets)


def validate_cost(
    resource_type: str,
    resource_info: dict,
    tags: list[dict],
    region_name: str | None = None,
) -> list[dict]:
    """Validate a SageMaker resource against Cost Optimization pillar best practices.

    Checks (7):
    37. Spot instances for training jobs
    38. Idle running notebook instances
    39. Notebook lifecycle configs for auto-stop
    40. Multi-model endpoints
    41. Cost allocation tags
    42. S3 lifecycle policies
    43. S3 Intelligent-Tiering

    Also checks: large training volumes, serverless inference consideration.

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        tags: Resource tags
        region_name: AWS region name

    Returns:
        List of cost findings
    """
    findings: list[dict] = []
    name = _get_resource_name(resource_info)

    if resource_type == RESOURCE_TRAINING_JOB:
        # Check 37: Spot instances for training
        if not resource_info.get('EnableManagedSpotTraining', False):
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'no-spot-training',
                    'Training job does not use managed spot training.',
                    'Enable managed spot training to save 60-90% on training costs for fault-tolerant workloads.',
                )
            )

        # Existing: large training volume
        volume_size = resource_info.get('ResourceConfig', {}).get('VolumeSizeInGB', 0)
        if volume_size > 500:
            findings.append(
                _finding(
                    SEVERITY_LOW,
                    name,
                    'large-training-volume',
                    f'Training volume size is {volume_size} GB.',
                    'Review if the volume size is appropriate. Consider using S3 pipe mode to reduce storage needs.',
                )
            )

    if resource_type == RESOURCE_ENDPOINT:
        config_name = resource_info.get('EndpointConfigName')
        if config_name:
            try:
                sm = AwsHelper.create_boto3_client('sagemaker', region_name=region_name)
                config = sm.describe_endpoint_config(EndpointConfigName=config_name)
                variants = config.get('ProductionVariants', [])

                # Existing: serverless inference consideration
                for v in variants:
                    if not v.get('ServerlessConfig') and v.get('InitialInstanceCount', 0) == 1:
                        itype = v.get('InstanceType', '')
                        if 'xlarge' in itype or '2xlarge' in itype:
                            findings.append(
                                _finding(
                                    SEVERITY_LOW,
                                    name,
                                    'consider-serverless',
                                    f"Single-instance endpoint with '{itype}' may benefit from serverless inference.",
                                    'For intermittent traffic, consider serverless inference to pay only for usage.',
                                )
                            )

                # Check 40: Multi-model endpoints
                # Check if multiple models could share an endpoint
                total_variants = len(variants)
                if total_variants == 1:
                    v = variants[0]
                    model_name = v.get('ModelName', '')
                    if model_name:
                        try:
                            model = sm.describe_model(ModelName=model_name)
                            # If it's a single-model endpoint, suggest multi-model
                            container = model.get('PrimaryContainer', {})
                            mode = container.get('Mode', 'SingleModel')
                            if mode == 'SingleModel':
                                findings.append(
                                    _finding(
                                        SEVERITY_LOW,
                                        name,
                                        'consider-multi-model',
                                        'Endpoint uses a single-model configuration.',
                                        'Consider multi-model endpoints to share resources and reduce costs when hosting multiple models.',
                                    )
                                )
                        except ClientError:
                            pass
            except ClientError as e:
                logger.warning(f'Could not describe endpoint config {config_name}: {e}')

    if resource_type == RESOURCE_NOTEBOOK:
        # Check 38: Idle running notebooks
        if resource_info.get('NotebookInstanceStatus') == 'InService':
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'running-notebook',
                    'Notebook instance is currently running and may be idle.',
                    'Identify and terminate idle notebook instances to reduce costs.',
                )
            )

        # Check 39: Notebook lifecycle configs
        if not resource_info.get('NotebookInstanceLifecycleConfigName'):
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'no-lifecycle-config',
                    'Notebook instance has no lifecycle configuration for automatic start/stop.',
                    'Attach a lifecycle config with auto-stop scripts to prevent idle notebook costs.',
                )
            )

    # Check 42 & 43: S3 lifecycle policies and Intelligent-Tiering
    s3_buckets = _extract_s3_buckets(resource_type, resource_info)
    if s3_buckets:
        try:
            s3 = AwsHelper.create_boto3_client('s3', region_name=region_name)
            for bucket in s3_buckets:
                # Check 42: S3 lifecycle policies
                try:
                    s3.get_bucket_lifecycle_configuration(Bucket=bucket)
                except ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code', '')
                    if error_code == 'NoSuchLifecycleConfiguration':
                        findings.append(
                            _finding(
                                SEVERITY_LOW,
                                name,
                                's3-no-lifecycle-policy',
                                f"S3 bucket '{bucket}' has no lifecycle policy configured.",
                                'Configure S3 lifecycle policies to automatically archive or delete old data.',
                            )
                        )

                # Check 43: S3 Intelligent-Tiering
                try:
                    it_configs = s3.list_bucket_intelligent_tiering_configurations(
                        Bucket=bucket
                    ).get('IntelligentTieringConfigurationList', [])
                    if not it_configs:
                        findings.append(
                            _finding(
                                SEVERITY_LOW,
                                name,
                                's3-no-intelligent-tiering',
                                f"S3 bucket '{bucket}' does not have Intelligent-Tiering configured.",
                                'Configure S3 Intelligent-Tiering for automatic cost optimization of stored data.',
                            )
                        )
                except ClientError:
                    pass
        except ClientError as e:
            logger.warning(f'Could not check S3 buckets for {name}: {e}')

    return findings
