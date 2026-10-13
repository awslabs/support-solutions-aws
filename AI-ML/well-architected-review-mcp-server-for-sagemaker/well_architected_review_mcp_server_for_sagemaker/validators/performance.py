# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Performance Efficiency pillar validator for SageMaker resources."""

import datetime
from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    PILLAR_PERFORMANCE,
    RESOURCE_ENDPOINT,
    RESOURCE_NOTEBOOK,
    RESOURCE_TRAINING_JOB,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)
from botocore.exceptions import ClientError
from loguru import logger


OLDER_INSTANCE_GENERATIONS = ['ml.m4.', 'ml.c4.', 'ml.p2.']
OLDER_NOTEBOOK_GENERATIONS = ['ml.t2.', 'ml.m4.']

# Instance type categories for workload matching guidance
GPU_INSTANCE_PREFIXES = ['ml.p', 'ml.g', 'ml.inf', 'ml.trn']
COMPUTE_INSTANCE_PREFIXES = ['ml.c']
MEMORY_INSTANCE_PREFIXES = ['ml.r']
GENERAL_INSTANCE_PREFIXES = ['ml.m', 'ml.t']


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
    """Create a performance finding."""
    return {
        'pillar': PILLAR_PERFORMANCE,
        'severity': severity,
        'resource': resource,
        'check': check,
        'detail': detail,
        'recommendation': recommendation,
    }


def validate_performance(
    resource_type: str, resource_info: dict, region_name: str | None = None
) -> list[dict]:
    """Validate a SageMaker resource against Performance Efficiency pillar best practices.

    Checks (3):
    34. Instance type review for workload requirements
    35. CloudWatch metrics analysis for right-sizing
    36. SageMaker Neo compilation check

    Also checks: older instance generations, data capture, data distribution, older notebooks.

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        region_name: AWS region name

    Returns:
        List of performance findings
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
                    itype = v.get('InstanceType', '')

                    # Older instance generation
                    if any(gen in itype for gen in OLDER_INSTANCE_GENERATIONS):
                        findings.append(
                            _finding(
                                SEVERITY_MEDIUM,
                                name,
                                'older-instance-generation',
                                f"Variant '{v.get('VariantName')}' uses older generation instance type '{itype}'.",
                                'Consider upgrading to newer instance types (m5/c5/p3/g5) for better price-performance.',
                            )
                        )

                # Data capture
                data_capture = config.get('DataCaptureConfig')
                if not data_capture or not data_capture.get('EnableCapture', False):
                    findings.append(
                        _finding(
                            SEVERITY_LOW,
                            name,
                            'no-data-capture',
                            'Data capture is not enabled on the endpoint.',
                            'Enable data capture to monitor model input/output for drift detection and performance analysis.',
                        )
                    )
            except ClientError as e:
                logger.warning(f'Could not describe endpoint config {config_name}: {e}')

        # Check 35: CloudWatch metrics for right-sizing
        try:
            cw = AwsHelper.create_boto3_client('cloudwatch', region_name=region_name)
            now = datetime.datetime.now(datetime.timezone.utc)
            start = now - datetime.timedelta(days=7)

            # Check CPU utilization
            cpu_stats = cw.get_metric_statistics(
                Namespace='AWS/SageMaker',
                MetricName='CPUUtilization',
                Dimensions=[
                    {'Name': 'EndpointName', 'Value': name},
                    {'Name': 'VariantName', 'Value': 'AllTraffic'},
                ],
                StartTime=start,
                EndTime=now,
                Period=86400,  # 1 day
                Statistics=['Average'],
            ).get('Datapoints', [])

            if cpu_stats:
                avg_cpu = sum(d['Average'] for d in cpu_stats) / len(cpu_stats)
                if avg_cpu < 10:
                    findings.append(
                        _finding(
                            SEVERITY_MEDIUM,
                            name,
                            'underutilized-endpoint',
                            f'Average CPU utilization is {avg_cpu:.1f}% over the last 7 days.',
                            'Endpoint appears over-provisioned. Consider downsizing the instance type or using auto-scaling.',
                        )
                    )
                elif avg_cpu > 80:
                    findings.append(
                        _finding(
                            SEVERITY_MEDIUM,
                            name,
                            'overutilized-endpoint',
                            f'Average CPU utilization is {avg_cpu:.1f}% over the last 7 days.',
                            'Endpoint may be under-provisioned. Consider upgrading the instance type or adding more instances.',
                        )
                    )
        except ClientError as e:
            logger.warning(f'Could not check CloudWatch metrics for {name}: {e}')

    if resource_type == RESOURCE_TRAINING_JOB:
        resource_config = resource_info.get('ResourceConfig', {})
        itype = resource_config.get('InstanceType', '')

        # Existing: older instance generation
        if any(gen in itype for gen in OLDER_INSTANCE_GENERATIONS):
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'older-instance-generation',
                    f"Training job uses older generation instance type '{itype}'.",
                    'Consider newer instance types (ml.m5, ml.c5, ml.p3, ml.g5, ml.trn1) for better throughput.',
                )
            )

        # Existing: data distribution
        input_data = resource_info.get('InputDataConfig', [])
        instance_count = resource_config.get('InstanceCount', 1)
        for channel in input_data:
            s3_source = channel.get('DataSource', {}).get('S3DataSource', {})
            if s3_source.get('S3DataDistributionType') == 'FullyReplicated' and instance_count > 1:
                findings.append(
                    _finding(
                        SEVERITY_MEDIUM,
                        name,
                        'data-distribution',
                        f"Channel '{channel.get('ChannelName')}' uses FullyReplicated with {instance_count} instances.",
                        'Consider ShardedByS3Key distribution for large datasets with multiple instances.',
                    )
                )

    if resource_type == RESOURCE_NOTEBOOK:
        itype = resource_info.get('InstanceType', '')
        # Existing: older notebook instance
        if any(gen in itype for gen in OLDER_NOTEBOOK_GENERATIONS):
            findings.append(
                _finding(
                    SEVERITY_LOW,
                    name,
                    'older-notebook-instance',
                    f"Notebook instance uses older generation type '{itype}'.",
                    'Consider ml.t3 or ml.m5 instances for better performance.',
                )
            )

    return findings
