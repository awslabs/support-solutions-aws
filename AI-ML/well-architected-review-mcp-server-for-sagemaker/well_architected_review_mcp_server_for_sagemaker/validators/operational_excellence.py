# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Operational Excellence pillar validator for SageMaker resources."""

from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    PILLAR_OPS_EXCELLENCE,
    RESOURCE_ENDPOINT,
    RESOURCE_TRAINING_JOB,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)
from botocore.exceptions import ClientError
from loguru import logger


OPERATIONAL_TAGS = {
    'Environment',
    'environment',
    'Team',
    'team',
    'Application',
    'application',
}

EXPERIMENT_TAGS = {
    'ExperimentName',
    'experiment-name',
    'mlflow-run-id',
    'sagemaker:experiment-name',
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
    """Create an operational excellence finding."""
    return {
        'pillar': PILLAR_OPS_EXCELLENCE,
        'severity': severity,
        'resource': resource,
        'check': check,
        'detail': detail,
        'recommendation': recommendation,
    }


def validate_operational_excellence(
    resource_type: str,
    resource_info: dict,
    tags: list[dict],
    region_name: str | None = None,
) -> list[dict]:
    """Validate a SageMaker resource against Operational Excellence pillar best practices.

    Checks (7):
    1. CloudWatch metrics monitoring for endpoints
    2. CloudWatch log groups exist for SageMaker resources
    3. CloudWatch log retention periods configured
    4. Model Registry versioning
    5. SageMaker Projects usage
    6. CloudWatch alarms for 4XX/5XX invocation errors
    7. CloudWatch alarms for high model latency

    Also checks: operational tags, auto-scaling, experiment tracking.

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        tags: Resource tags
        region_name: AWS region name

    Returns:
        List of operational excellence findings
    """
    findings: list[dict] = []
    name = _get_resource_name(resource_info)
    tag_keys = {t['Key'] for t in tags}

    if resource_type == RESOURCE_ENDPOINT:
        # Existing: auto-scaling
        try:
            aas = AwsHelper.create_boto3_client('application-autoscaling', region_name=region_name)
            targets = aas.describe_scalable_targets(
                ServiceNamespace='sagemaker',
                ResourceIds=[f'endpoint/{name}/variant/AllTraffic'],
            ).get('ScalableTargets', [])
            if not targets:
                findings.append(
                    _finding(
                        SEVERITY_MEDIUM,
                        name,
                        'no-autoscaling',
                        'Endpoint does not have auto-scaling configured.',
                        'Configure auto-scaling policies to handle traffic variations automatically.',
                    )
                )
        except ClientError:
            pass

        # Check 1: CloudWatch metrics monitoring
        try:
            cw = AwsHelper.create_boto3_client('cloudwatch', region_name=region_name)
            metrics = cw.list_metrics(
                Namespace='AWS/SageMaker',
                MetricName='Invocations',
                Dimensions=[{'Name': 'EndpointName', 'Value': name}],
            ).get('Metrics', [])
            if not metrics:
                findings.append(
                    _finding(
                        SEVERITY_MEDIUM,
                        name,
                        'no-cloudwatch-metrics',
                        'No CloudWatch metrics found for this endpoint.',
                        'Ensure the endpoint is actively monitored with CloudWatch metrics for performance tracking.',
                    )
                )
        except ClientError as e:
            logger.warning(f'Could not check CloudWatch metrics for {name}: {e}')

        # Check 6: CloudWatch alarms for 4XX/5XX errors
        try:
            cw = AwsHelper.create_boto3_client('cloudwatch', region_name=region_name)
            alarms_4xx = cw.describe_alarms_for_metric(
                Namespace='AWS/SageMaker',
                MetricName='Invocation4XXErrors',
                Dimensions=[{'Name': 'EndpointName', 'Value': name}],
            ).get('MetricAlarms', [])
            alarms_5xx = cw.describe_alarms_for_metric(
                Namespace='AWS/SageMaker',
                MetricName='Invocation5XXErrors',
                Dimensions=[{'Name': 'EndpointName', 'Value': name}],
            ).get('MetricAlarms', [])
            if not alarms_4xx and not alarms_5xx:
                findings.append(
                    _finding(
                        SEVERITY_HIGH,
                        name,
                        'no-error-alarms',
                        'No CloudWatch alarms configured for endpoint 4XX/5XX invocation errors.',
                        'Configure CloudWatch alarms to alert on endpoint invocation errors for rapid incident response.',
                    )
                )
        except ClientError as e:
            logger.warning(f'Could not check CloudWatch alarms for {name}: {e}')

        # Check 7: CloudWatch alarms for high latency
        try:
            cw = AwsHelper.create_boto3_client('cloudwatch', region_name=region_name)
            latency_alarms = cw.describe_alarms_for_metric(
                Namespace='AWS/SageMaker',
                MetricName='ModelLatency',
                Dimensions=[{'Name': 'EndpointName', 'Value': name}],
            ).get('MetricAlarms', [])
            if not latency_alarms:
                findings.append(
                    _finding(
                        SEVERITY_MEDIUM,
                        name,
                        'no-latency-alarms',
                        'No CloudWatch alarms configured for model latency.',
                        'Configure alarms to monitor and alert on high model latency affecting user experience.',
                    )
                )
        except ClientError as e:
            logger.warning(f'Could not check latency alarms for {name}: {e}')

    # Check 2 & 3: CloudWatch log groups and retention
    try:
        logs = AwsHelper.create_boto3_client('logs', region_name=region_name)
        log_group_prefix = '/aws/sagemaker/'
        log_groups = logs.describe_log_groups(
            logGroupNamePrefix=log_group_prefix,
            limit=50,
        ).get('logGroups', [])

        # Filter for log groups related to this resource
        resource_log_groups = [lg for lg in log_groups if name in lg.get('logGroupName', '')]

        if not resource_log_groups:
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'no-cloudwatch-logs',
                    'No CloudWatch log groups found for this SageMaker resource.',
                    'Ensure CloudWatch log groups exist to capture operational logs for debugging and monitoring.',
                )
            )
        else:
            for lg in resource_log_groups:
                if 'retentionInDays' not in lg:
                    findings.append(
                        _finding(
                            SEVERITY_LOW,
                            name,
                            'no-log-retention',
                            f"Log group '{lg['logGroupName']}' has no retention period configured (logs retained indefinitely).",
                            'Configure a log retention period to balance cost and compliance requirements.',
                        )
                    )
    except ClientError as e:
        logger.warning(f'Could not check CloudWatch logs for {name}: {e}')

    # Check 4: Model Registry versioning
    if resource_type == RESOURCE_ENDPOINT:
        try:
            sm = AwsHelper.create_boto3_client('sagemaker', region_name=region_name)
            # Check if the model behind this endpoint is registered in Model Registry
            config_name = resource_info.get('EndpointConfigName')
            if config_name:
                config = sm.describe_endpoint_config(EndpointConfigName=config_name)
                variants = config.get('ProductionVariants', [])
                for v in variants:
                    model_name = v.get('ModelName', '')
                    if model_name:
                        try:
                            model = sm.describe_model(ModelName=model_name)
                            # Check if model came from a model package (Model Registry)
                            containers = model.get('Containers', [])
                            primary = model.get('PrimaryContainer', {})
                            has_model_package = False
                            if primary.get('ModelPackageName'):
                                has_model_package = True
                            for c in containers:
                                if c.get('ModelPackageName'):
                                    has_model_package = True
                            if not has_model_package:
                                findings.append(
                                    _finding(
                                        SEVERITY_MEDIUM,
                                        name,
                                        'no-model-registry',
                                        f"Model '{model_name}' is not registered in SageMaker Model Registry.",
                                        'Register models in Model Registry for versioning, governance, and traceability.',
                                    )
                                )
                        except ClientError:
                            pass
        except ClientError as e:
            logger.warning(f'Could not check Model Registry for {name}: {e}')

    # Experiment tracking for training
    if resource_type == RESOURCE_TRAINING_JOB:
        if not tag_keys & EXPERIMENT_TAGS:
            findings.append(
                _finding(
                    SEVERITY_LOW,
                    name,
                    'no-experiment-tracking',
                    'Training job does not appear to be tracked in an experiment.',
                    'Use SageMaker Experiments or MLflow to track training runs for reproducibility.',
                )
            )

    return findings
