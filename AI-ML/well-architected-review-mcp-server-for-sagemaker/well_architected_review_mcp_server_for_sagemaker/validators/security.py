# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Security pillar validator for SageMaker resources."""

from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    PILLAR_SECURITY,
    RESOURCE_ENDPOINT,
    RESOURCE_MODEL,
    RESOURCE_NOTEBOOK,
    RESOURCE_TRAINING_JOB,
    SEVERITY_HIGH,
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
    """Create a security finding."""
    return {
        'pillar': PILLAR_SECURITY,
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
        checkpoint_uri = resource_info.get('CheckpointConfig', {}).get('S3Uri')
        b = _bucket_from_uri(checkpoint_uri)
        if b:
            buckets.add(b)

    if resource_type == RESOURCE_MODEL:
        primary = resource_info.get('PrimaryContainer', {})
        b = _bucket_from_uri(primary.get('ModelDataUrl'))
        if b:
            buckets.add(b)
        for c in resource_info.get('Containers', []):
            b = _bucket_from_uri(c.get('ModelDataUrl'))
            if b:
                buckets.add(b)

    if resource_type == RESOURCE_ENDPOINT:
        data_capture_uri = resource_info.get('DataCaptureConfig', {}).get('DestinationS3Uri')
        b = _bucket_from_uri(data_capture_uri)
        if b:
            buckets.add(b)

    return list(buckets)


def validate_security(
    resource_type: str, resource_info: dict, tags: list[dict], region_name: str | None = None
) -> list[dict]:
    """Validate a SageMaker resource against Security pillar best practices.

    Focused on actionable security checks:
    - IAM role presence and least-privilege
    - VPC isolation and direct internet access
    - VPC endpoints and Flow Logs
    - S3 bucket encryption and versioning
    - KMS key rotation (when CMK is used)
    - Root access on notebooks
    - CloudTrail logging
    - AWS Config recording
    - Inter-container encryption and network isolation

    Args:
        resource_type: Type of SageMaker resource
        resource_info: Resource description from AWS API
        tags: Resource tags
        region_name: AWS region name

    Returns:
        List of security findings
    """
    findings: list[dict] = []
    name = _get_resource_name(resource_info)

    # IAM role presence
    role_arn = resource_info.get('RoleArn') or resource_info.get('ExecutionRoleArn')
    if not role_arn:
        findings.append(
            _finding(
                SEVERITY_HIGH,
                name,
                'no-iam-role',
                'Resource does not have an IAM execution role configured.',
                'Use IAM roles (not user credentials) for secure access control on SageMaker resources.',
            )
        )

    # Overly permissive IAM role
    if role_arn:
        try:
            iam = AwsHelper.create_boto3_client('iam')
            role_name = role_arn.split('/')[-1] if '/' in role_arn else role_arn
            attached = iam.list_attached_role_policies(RoleName=role_name).get(
                'AttachedPolicies', []
            )
            for policy in attached:
                policy_name = policy.get('PolicyName', '')
                if 'AdministratorAccess' in policy_name or 'FullAccess' in policy_name:
                    findings.append(
                        _finding(
                            SEVERITY_HIGH,
                            name,
                            'overly-permissive-role',
                            f"IAM role '{role_name}' has overly permissive policy '{policy_name}'.",
                            'Apply least-privilege permissions to SageMaker execution roles.',
                        )
                    )
        except ClientError as e:
            logger.warning(f'Could not check IAM role policies for {name}: {e}')

    # VPC isolation
    if resource_type == RESOURCE_NOTEBOOK:
        if not resource_info.get('SubnetId'):
            findings.append(
                _finding(
                    SEVERITY_HIGH,
                    name,
                    'vpc-isolation',
                    'Notebook instance is not deployed in a VPC.',
                    'Deploy notebook instances in a VPC with private subnets to restrict network access.',
                )
            )
        if resource_info.get('DirectInternetAccess') == 'Enabled':
            findings.append(
                _finding(
                    SEVERITY_HIGH,
                    name,
                    'direct-internet-access',
                    'Notebook instance has direct internet access enabled.',
                    'Disable direct internet access and route traffic through a NAT gateway or VPC endpoints.',
                )
            )
    else:
        vpc_config = resource_info.get('VpcConfig') or resource_info.get('NetworkConfig', {}).get(
            'VpcConfig'
        )
        if not vpc_config:
            findings.append(
                _finding(
                    SEVERITY_HIGH,
                    name,
                    'vpc-isolation',
                    'Resource is not deployed in a VPC.',
                    'Deploy SageMaker resources in a VPC with private subnets and security groups.',
                )
            )

    # VPC endpoints and Flow Logs
    vpc_config = resource_info.get('VpcConfig') or resource_info.get('NetworkConfig', {}).get(
        'VpcConfig'
    )
    subnet_id = resource_info.get('SubnetId')
    if vpc_config or subnet_id:
        try:
            ec2 = AwsHelper.create_boto3_client('ec2', region_name=region_name)
            resolve_subnet = subnet_id or (
                vpc_config.get('Subnets', [None])[0] if vpc_config else None
            )
            if resolve_subnet:
                subnet_desc = ec2.describe_subnets(SubnetIds=[resolve_subnet]).get('Subnets', [])
                if subnet_desc:
                    vpc_id = subnet_desc[0].get('VpcId')
                    if vpc_id:
                        vpce = ec2.describe_vpc_endpoints(
                            Filters=[
                                {'Name': 'vpc-id', 'Values': [vpc_id]},
                                {
                                    'Name': 'service-name',
                                    'Values': [
                                        f'com.amazonaws.{region_name or "us-east-1"}.sagemaker.api'
                                    ],
                                },
                            ]
                        ).get('VpcEndpoints', [])
                        if not vpce:
                            findings.append(
                                _finding(
                                    SEVERITY_MEDIUM,
                                    name,
                                    'no-vpc-endpoint',
                                    'No VPC endpoint for SageMaker API in the resource VPC.',
                                    'Create VPC endpoints to avoid routing SageMaker API traffic through the internet.',
                                )
                            )

                        flow_logs = ec2.describe_flow_logs(
                            Filters=[{'Name': 'resource-id', 'Values': [vpc_id]}]
                        ).get('FlowLogs', [])
                        if not flow_logs:
                            findings.append(
                                _finding(
                                    SEVERITY_MEDIUM,
                                    name,
                                    'no-vpc-flow-logs',
                                    f"VPC '{vpc_id}' does not have Flow Logs enabled.",
                                    'Enable VPC Flow Logs for network traffic monitoring and security analysis.',
                                )
                            )
        except ClientError as e:
            logger.warning(f'Could not check VPC endpoints/flow logs for {name}: {e}')

    # KMS key rotation (only when CMK is explicitly configured)
    kms_key = resource_info.get('KmsKeyId') or resource_info.get('OutputDataConfig', {}).get(
        'KmsKeyId'
    )
    if kms_key:
        try:
            kms_client = AwsHelper.create_boto3_client('kms', region_name=region_name)
            key_id = kms_key.split('/')[-1] if '/' in kms_key else kms_key
            rotation = kms_client.get_key_rotation_status(KeyId=key_id)
            if not rotation.get('KeyRotationEnabled', False):
                findings.append(
                    _finding(
                        SEVERITY_MEDIUM,
                        name,
                        'no-kms-key-rotation',
                        'Customer-managed KMS key does not have automatic rotation enabled.',
                        'Enable automatic KMS key rotation for enhanced security posture.',
                    )
                )
        except ClientError as e:
            logger.warning(f'Could not check KMS key rotation for {name}: {e}')

    # S3 bucket encryption and versioning
    s3_buckets = _extract_s3_buckets(resource_type, resource_info)
    if s3_buckets:
        try:
            s3 = AwsHelper.create_boto3_client('s3', region_name=region_name)
            for bucket in s3_buckets:
                try:
                    s3.get_bucket_encryption(Bucket=bucket)
                except ClientError as e:
                    if (
                        e.response['Error']['Code']
                        == 'ServerSideEncryptionConfigurationNotFoundError'
                    ):
                        findings.append(
                            _finding(
                                SEVERITY_HIGH,
                                name,
                                's3-no-encryption',
                                f"S3 bucket '{bucket}' does not have encryption enabled.",
                                'Enable server-side encryption on S3 buckets storing training data and models.',
                            )
                        )

                try:
                    versioning = s3.get_bucket_versioning(Bucket=bucket)
                    if versioning.get('Status') != 'Enabled':
                        findings.append(
                            _finding(
                                SEVERITY_MEDIUM,
                                name,
                                's3-no-versioning',
                                f"S3 bucket '{bucket}' does not have versioning enabled.",
                                'Enable S3 bucket versioning for data protection and recovery.',
                            )
                        )
                except ClientError:
                    pass
        except ClientError as e:
            logger.warning(f'Could not check S3 buckets for {name}: {e}')

    # Root access on notebooks
    if resource_type == RESOURCE_NOTEBOOK:
        if resource_info.get('RootAccess') == 'Enabled':
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'root-access',
                    'Notebook instance has root access enabled.',
                    'Disable root access on production notebook instances to limit security exposure.',
                )
            )

    # CloudTrail logging
    try:
        ct = AwsHelper.create_boto3_client('cloudtrail', region_name=region_name)
        trails = ct.describe_trails().get('trailList', [])
        active_trails = []
        for trail in trails:
            try:
                status = ct.get_trail_status(Name=trail['TrailARN'])
                if status.get('IsLogging', False):
                    active_trails.append(trail)
            except ClientError:
                pass
        if not active_trails:
            findings.append(
                _finding(
                    SEVERITY_HIGH,
                    name,
                    'no-cloudtrail',
                    'No active CloudTrail trail found logging API calls.',
                    'Enable CloudTrail to log all SageMaker API calls for audit and compliance.',
                )
            )
    except ClientError as e:
        logger.warning(f'Could not check CloudTrail for {name}: {e}')

    # AWS Config recording
    try:
        config_client = AwsHelper.create_boto3_client('config', region_name=region_name)
        recorders = config_client.describe_configuration_recorder_status().get(
            'ConfigurationRecordersStatus', []
        )
        active_recorders = [r for r in recorders if r.get('recording', False)]
        if not active_recorders:
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'no-aws-config',
                    'AWS Config is not actively recording resource configurations.',
                    'Enable AWS Config to track SageMaker resource configuration changes.',
                )
            )
    except ClientError as e:
        logger.warning(f'Could not check AWS Config for {name}: {e}')

    # Inter-container encryption (training)
    if resource_type == RESOURCE_TRAINING_JOB:
        if not resource_info.get('EnableInterContainerTrafficEncryption', False):
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'inter-container-encryption',
                    'Inter-container traffic encryption is not enabled for training job.',
                    'Enable inter-container traffic encryption to protect data in transit between instances.',
                )
            )

    # Network isolation
    if resource_type in (RESOURCE_TRAINING_JOB, RESOURCE_MODEL):
        if not resource_info.get('EnableNetworkIsolation', False):
            findings.append(
                _finding(
                    SEVERITY_MEDIUM,
                    name,
                    'network-isolation',
                    'Network isolation is not enabled.',
                    'Enable network isolation to prevent the container from making outbound network calls.',
                )
            )

    return findings
