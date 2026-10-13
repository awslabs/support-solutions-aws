# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reliability pillar validator for SageMaker resources."""

from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    PILLAR_RELIABILITY,
    RESOURCE_ENDPOINT,
    RESOURCE_MODEL,
    RESOURCE_TRAINING_JOB,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)
from botocore.exceptions import ClientError
from loguru import logger


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
    """Create a reliability finding."""
    return {
        'pillar': PILLAR_RELIABILITY,
        'severity': severity,
        'resource': resource,
        'check': check,
        'detail': detail,
        'recommendation': recommendation,
    }


def _extract_s3_buckets_for_model(resource_info: dict) -> list[str]:
    """Extract S3 bucket names from model artifacts."""
    buckets: set[str] = set()

    def _bucket_from_uri(uri: str | None) -> str | None:
        if uri and uri.startswith('s3://'):
            parts = uri.replace('s3://', '').split('/')
            return parts[0] if parts else None
        return None

    # Training job output
    output_uri = resource_info.get('OutputDataConfig', {}).get('S3OutputPath')
    b = _bucket_from_uri(output_uri)
    if b:
        buckets.add(b)

    # Model artifacts
    primary = resource_info.get('PrimaryContainer', {})
    b = _bucket_from_uri(primary.get('ModelDataUrl'))
    if b:
        buckets.add(b)
    for c in resource_info.get('Containers', []):
        b = _bucket_from_uri(c.get('ModelDataUrl'))
        if b:
            buckets.add(b)

    return list(buckets)


def validate_reliability(
    resource_type: str, resource_info: dict, region_name: str | None = None
) -> list[dict]:
    """Validate a SageMaker resource against Reliability pillar best practices.

    Checks (7):
    27. Multi-AZ deployment for endpoints
    28. 2+ instances for redundancy
    29. Auto-scaling configured for endpoints
    30. Target-tracking scaling policies
    31. Service quotas review
    32. S3 versioning for model artifacts
    33. Cross-region replication for critical data

    Also checks: training timeout, checkpointing, retry strategy.

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        region_name: AWS region name

    Returns:
        List of reliability findings
    """
    findings: list[dict] = []
    name = _get_resource_name(resource_info)

    if resource_type == RESOURCE_ENDPOINT:
        config_name = resource_info.get('EndpointConfigName')
        if config_name:
            try:
                sm = AwsHelper.create_boto3_client('sagemaker', region_name=region_name)
                config = sm.describe_endpoint_config(EndpointConfigName=config_name)
                variants = config.get('ProductionVariants', [])

                for v in variants:
                    count = v.get('InitialInstanceCount', 1)

                    # Check 28: 2+ instances for redundancy
                    if count < 2:
                        findings.append(
                            _finding(
                                SEVERITY_HIGH,
                                name,
                                'single-instance-endpoint',
                                f"Production variant '{v.get('VariantName')}' has only {count} instance(s).",
                                'Use at least 2 instances for redundancy and fault tolerance.',
                            )
                        )

                # Check 27: Multi-AZ deployment
                # SageMaker automatically distributes instances across AZs when count >= 2,
                # but we check if the endpoint config has routing config for multi-AZ awareness
                total_instances = sum(v.get('InitialInstanceCount', 1) for v in variants)
                if total_instances < 2:
                    findings.append(
                        _finding(
                            SEVERITY_HIGH,
                            name,
                            'no-multi-az',
                            'Endpoint does not have instances across multiple Availability Zones.',
                            'Deploy endpoints with 2+ instances to enable multi-AZ high availability.',
                        )
                    )

            except ClientError as e:
                logger.warning(f'Could not describe endpoint config {config_name}: {e}')

        # Check 29: Auto-scaling configured
        try:
            aas = AwsHelper.create_boto3_client('application-autoscaling', region_name=region_name)
            targets = aas.describe_scalable_targets(
                ServiceNamespace='sagemaker',
                ResourceIds=[f'endpoint/{name}/variant/AllTraffic'],
            ).get('ScalableTargets', [])
            if not targets:
                findings.append(
                    _finding(
                        SEVERITY_HIGH,
                        name,
                        'no-autoscaling',
                        'Endpoint does not have auto-scaling configured.',
                        'Configure auto-scaling to handle variable traffic loads and prevent overload.',
                    )
                )
            else:
                # Check 30: Target-tracking scaling policies
                policies = aas.describe_scaling_policies(
                    ServiceNamespace='sagemaker',
                    ResourceId=f'endpoint/{name}/variant/AllTraffic',
                ).get('ScalingPolicies', [])
                target_tracking = [
                    p for p in policies if p.get('PolicyType') == 'TargetTrackingScaling'
                ]
                if not target_tracking:
                    findings.append(
                        _finding(
                            SEVERITY_MEDIUM,
                            name,
                            'no-target-tracking-policy',
                            'Endpoint auto-scaling does not use target-tracking scaling policies.',
                            'Configure target-tracking scaling policies for automatic capacity adjustment.',
                        )
                    )
        except ClientError as e:
            logger.warning(f'Could not check auto-scaling for {name}: {e}')

    # Check 31: Service quotas review
    if resource_type == RESOURCE_ENDPOINT:
        try:
            sq = AwsHelper.create_boto3_client('service-quotas', region_name=region_name)
            # Check endpoint instance quota
            try:
                quota = sq.get_service_quota(
                    ServiceCode='sagemaker',
                    QuotaCode='L-6E869900',  # Number of instances across active endpoints
                )
                quota_value = quota.get('Quota', {}).get('Value', 0)
                if quota_value and quota_value < 10:
                    findings.append(
                        _finding(
                            SEVERITY_MEDIUM,
                            name,
                            'low-endpoint-quota',
                            f'SageMaker endpoint instance quota is {int(quota_value)}.',
                            'Review and request increases for SageMaker service quotas to prevent hitting limits during scale-up.',
                        )
                    )
            except ClientError:
                pass
        except ClientError as e:
            logger.warning(f'Could not check service quotas for {name}: {e}')

    # Check 32: S3 versioning for model artifacts
    s3_buckets = _extract_s3_buckets_for_model(resource_info)
    if s3_buckets and resource_type in (RESOURCE_TRAINING_JOB, RESOURCE_MODEL):
        try:
            s3 = AwsHelper.create_boto3_client('s3', region_name=region_name)
            for bucket in s3_buckets:
                try:
                    versioning = s3.get_bucket_versioning(Bucket=bucket)
                    if versioning.get('Status') != 'Enabled':
                        findings.append(
                            _finding(
                                SEVERITY_MEDIUM,
                                name,
                                's3-no-versioning-artifacts',
                                f"S3 bucket '{bucket}' storing model artifacts does not have versioning enabled.",
                                'Enable S3 versioning for model artifacts to support rollback and recovery.',
                            )
                        )
                except ClientError:
                    pass
        except ClientError as e:
            logger.warning(f'Could not check S3 versioning for {name}: {e}')

    # Check 33: Cross-region replication
    if s3_buckets and resource_type in (RESOURCE_TRAINING_JOB, RESOURCE_MODEL):
        try:
            s3 = AwsHelper.create_boto3_client('s3', region_name=region_name)
            for bucket in s3_buckets:
                try:
                    s3.get_bucket_replication(Bucket=bucket)
                    # If we get here, replication is configured — no finding needed
                except ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code', '')
                    if error_code == 'ReplicationConfigurationNotFoundError':
                        findings.append(
                            _finding(
                                SEVERITY_LOW,
                                name,
                                'no-cross-region-replication',
                                f"S3 bucket '{bucket}' does not have cross-region replication configured.",
                                'Configure cross-region replication for critical data to support disaster recovery.',
                            )
                        )
        except ClientError as e:
            logger.warning(f'Could not check S3 replication for {name}: {e}')

    # Existing: training timeout
    if resource_type == RESOURCE_TRAINING_JOB:
        max_runtime = resource_info.get('StoppingCondition', {}).get('MaxRuntimeInSeconds', 0)
        if max_runtime == 0 or max_runtime > 432000:  # 5 days
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'training-timeout',
                    f'Training job max runtime is {max_runtime}s (or unlimited).',
                    'Set a reasonable MaxRuntimeInSeconds to prevent runaway training jobs.',
                )
            )

        # Existing: checkpointing
        if not resource_info.get('CheckpointConfig'):
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'no-checkpointing',
                    'Training job does not have checkpointing configured.',
                    'Enable checkpointing to allow training to resume from the last checkpoint on failure.',
                )
            )

        # Existing: retry strategy
        if not resource_info.get('RetryStrategy'):
            findings.append(
                _finding(
                    SEVERITY_LOW,
                    name,
                    'no-retry-strategy',
                    'Training job does not have a retry strategy configured.',
                    'Configure RetryStrategy to automatically retry on transient failures.',
                )
            )

    return findings
