# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Well-Architected validation handler for the SageMaker WA MCP Server."""

import json
import os
from well_architected_review_mcp_server_for_sagemaker.aws_helper import AwsHelper
from well_architected_review_mcp_server_for_sagemaker.consts import (
    RESOURCE_ENDPOINT,
    RESOURCE_MODEL,
    RESOURCE_NOTEBOOK,
    RESOURCE_TRAINING_JOB,
    SUPPORTED_REGIONS,
)
from well_architected_review_mcp_server_for_sagemaker.logging_helper import LogLevel, log_with_request_id
from well_architected_review_mcp_server_for_sagemaker.models import (
    Finding,
    PillarCheck,
    PillarSummary,
    ResourceSummary,
)
from well_architected_review_mcp_server_for_sagemaker.report_generator import (
    generate_batch_html_report,
)
from well_architected_review_mcp_server_for_sagemaker.validators import run_all_validators
from botocore.exceptions import ClientError
from mcp.server.fastmcp import Context
from mcp.types import CallToolResult, TextContent
from pydantic import Field, validate_call
from typing import Optional


def _tool_result(
    text: str,
    structured: Optional[dict] = None,
    is_error: bool = False,
) -> CallToolResult:
    """Build a proper MCP tool result.

    Puts the human-readable summary in ``content`` and any machine-readable
    payload in ``structuredContent``, returning the real
    ``mcp.types.CallToolResult`` so FastMCP passes it through unchanged instead
    of serializing a wrapper model into a nested tool response.
    """
    return CallToolResult(
        content=[TextContent(type='text', text=text)],
        structuredContent=structured,
        isError=is_error,
    )


# Pillar metadata for the pillar_info tool
PILLAR_METADATA = {
    'operational_excellence': {
        'name': 'Operational Excellence',
        'description': 'Run and monitor systems to deliver business value and continually improve processes.',
        'checks': [
            {
                'id': 'no-cloudwatch-metrics',
                'description': 'Verifies CloudWatch metrics monitoring for endpoints',
            },
            {
                'id': 'no-cloudwatch-logs',
                'description': 'Confirms CloudWatch log groups exist for SageMaker resources',
            },
            {
                'id': 'no-log-retention',
                'description': 'Validates CloudWatch log retention periods are configured',
            },
            {
                'id': 'no-model-registry',
                'description': 'Ensures models are registered and versioned in Model Registry',
            },
            {
                'id': 'no-error-alarms',
                'description': 'Verifies CloudWatch alarms for endpoint 4XX/5XX errors',
            },
            {'id': 'no-latency-alarms', 'description': 'Confirms alarms for high model latency'},
            {
                'id': 'no-autoscaling',
                'description': 'Checks if auto-scaling is configured for endpoints',
            },
            {
                'id': 'no-experiment-tracking',
                'description': 'Validates experiment tracking for training jobs',
            },
        ],
    },
    'security': {
        'name': 'Security',
        'description': 'Protect data, systems, and assets through risk assessments and mitigation strategies.',
        'checks': [
            {
                'id': 'no-iam-role',
                'description': 'Validates IAM roles are used (not user credentials)',
            },
            {
                'id': 'overly-permissive-role',
                'description': 'Flags overly permissive IAM policies (AdministratorAccess, FullAccess)',
            },
            {'id': 'vpc-isolation', 'description': 'Verifies resources are deployed within a VPC'},
            {
                'id': 'direct-internet-access',
                'description': 'Ensures notebook direct internet access is disabled',
            },
            {
                'id': 'no-vpc-endpoint',
                'description': 'Confirms VPC endpoints exist for SageMaker API',
            },
            {'id': 'no-vpc-flow-logs', 'description': 'Validates VPC Flow Logs are enabled'},
            {'id': 's3-no-encryption', 'description': 'Confirms S3 bucket encryption is enabled'},
            {'id': 's3-no-versioning', 'description': 'Validates S3 bucket versioning is enabled'},
            {
                'id': 'no-kms-key-rotation',
                'description': 'Ensures CMK auto-rotation is enabled when CMK is used',
            },
            {'id': 'root-access', 'description': 'Confirms root access is disabled on notebooks'},
            {
                'id': 'no-cloudtrail',
                'description': 'Verifies CloudTrail is logging SageMaker API calls',
            },
            {
                'id': 'no-aws-config',
                'description': 'Validates AWS Config is recording configurations',
            },
            {
                'id': 'inter-container-encryption',
                'description': 'Checks inter-container traffic encryption',
            },
            {'id': 'network-isolation', 'description': 'Validates network isolation'},
        ],
    },
    'reliability': {
        'name': 'Reliability',
        'description': 'Ensure workloads perform their intended function correctly and consistently.',
        'checks': [
            {'id': 'no-multi-az', 'description': 'Verifies multi-AZ deployment for endpoints'},
            {
                'id': 'single-instance-endpoint',
                'description': 'Confirms 2+ instances for redundancy',
            },
            {
                'id': 'no-autoscaling',
                'description': 'Validates auto-scaling is configured for endpoints',
            },
            {
                'id': 'no-target-tracking-policy',
                'description': 'Confirms target-tracking scaling policies',
            },
            {'id': 'low-endpoint-quota', 'description': 'Reviews SageMaker service quotas'},
            {
                'id': 's3-no-versioning-artifacts',
                'description': 'Ensures S3 versioning for model artifacts',
            },
            {
                'id': 'no-cross-region-replication',
                'description': 'Validates cross-region replication for DR',
            },
            {'id': 'training-timeout', 'description': 'Validates training job timeout limits'},
            {'id': 'no-checkpointing', 'description': 'Checks training checkpointing'},
            {
                'id': 'no-retry-strategy',
                'description': 'Validates retry strategy for training jobs',
            },
        ],
    },
    'performance': {
        'name': 'Performance Efficiency',
        'description': 'Use computing resources efficiently to meet requirements and maintain efficiency as demand changes.',
        'checks': [
            {
                'id': 'underutilized-endpoint',
                'description': 'Identifies over-provisioned endpoints via CloudWatch CPU metrics',
            },
            {
                'id': 'overutilized-endpoint',
                'description': 'Identifies under-provisioned endpoints via CloudWatch CPU metrics',
            },
            {
                'id': 'older-instance-generation',
                'description': 'Flags older generation instance types',
            },
            {'id': 'no-data-capture', 'description': 'Checks data capture for model monitoring'},
            {
                'id': 'data-distribution',
                'description': 'Validates data distribution for multi-instance training',
            },
            {
                'id': 'older-notebook-instance',
                'description': 'Flags older notebook instance types',
            },
        ],
    },
    'cost': {
        'name': 'Cost Optimization',
        'description': 'Avoid unnecessary costs and optimize spending.',
        'checks': [
            {'id': 'no-spot-training', 'description': 'Verifies spot instances for training jobs'},
            {
                'id': 'running-notebook',
                'description': 'Identifies idle running notebook instances',
            },
            {
                'id': 'no-lifecycle-config',
                'description': 'Confirms notebook lifecycle configs for auto-stop',
            },
            {
                'id': 'consider-multi-model',
                'description': 'Checks if multi-model endpoints could reduce costs',
            },
            {
                'id': 's3-no-lifecycle-policy',
                'description': 'Confirms S3 lifecycle policies exist',
            },
            {
                'id': 's3-no-intelligent-tiering',
                'description': 'Verifies S3 Intelligent-Tiering is configured',
            },
            {'id': 'large-training-volume', 'description': 'Flags oversized training volumes'},
            {
                'id': 'consider-serverless',
                'description': 'Suggests serverless inference for low-traffic endpoints',
            },
        ],
    },
    'sustainability': {
        'name': 'Sustainability',
        'description': 'Minimize environmental impacts of running cloud workloads.',
        'checks': [
            {
                'id': 'consider-graviton-endpoint',
                'description': 'Checks Graviton instances for energy efficiency',
            },
            {'id': 'consider-graviton', 'description': 'Suggests Graviton for training jobs'},
            {
                'id': 'long-running-endpoint',
                'description': 'Lists active endpoints running >90 days for cleanup review',
            },
            {
                'id': 'stale-notebook',
                'description': 'Identifies notebooks stopped >30 days for potential deletion',
            },
            {'id': 'oversized-notebook', 'description': 'Flags oversized notebook instances'},
        ],
    },
}


class WellArchitectedValidationHandler:
    """Handler for Well-Architected validation operations.

    This class provides tools for validating SageMaker resources against
    the AWS Well-Architected Framework pillars.
    """

    def __init__(self, mcp, allow_sensitive_data_access: bool = False):
        """Initialize the Well-Architected validation handler.

        Args:
            mcp: The MCP server instance
            allow_sensitive_data_access: Whether to allow access to sensitive data
        """
        self.mcp = mcp
        self.allow_sensitive_data_access = allow_sensitive_data_access

        # Register tools
        self.mcp.tool(name='validate_sagemaker_resource')(self.validate_sagemaker_resource)
        self.mcp.tool(name='validate_all_endpoints')(self.validate_all_endpoints)
        self.mcp.tool(name='validate_all_resources')(self.validate_all_resources)
        self.mcp.tool(name='list_sagemaker_resources')(self.list_sagemaker_resources)
        self.mcp.tool(name='get_pillar_details')(self.get_pillar_details)

    def _get_sagemaker_client(
        self,
        ctx: Context,
        region_name: Optional[SUPPORTED_REGIONS] = None,
        profile_name: Optional[str] = None,
    ):
        """Get a SageMaker client for the specified region and profile.

        Args:
            ctx: The MCP context
            region_name: Optional AWS region name
            profile_name: Optional AWS profile name

        Returns:
            A boto3 SageMaker client
        """
        if profile_name:
            log_with_request_id(ctx, LogLevel.INFO, f'Using AWS profile: {profile_name}')
            os.environ['AWS_PROFILE'] = profile_name

        return AwsHelper.create_boto3_client('sagemaker', region_name=region_name)

    def _describe_resource(self, sm, resource_type: str, resource_name: str) -> dict:
        """Describe a SageMaker resource.

        Args:
            sm: SageMaker boto3 client
            resource_type: Type of resource
            resource_name: Name of the resource

        Returns:
            Resource description dictionary
        """
        if resource_type == RESOURCE_ENDPOINT:
            return sm.describe_endpoint(EndpointName=resource_name)
        elif resource_type == RESOURCE_TRAINING_JOB:
            return sm.describe_training_job(TrainingJobName=resource_name)
        elif resource_type == RESOURCE_NOTEBOOK:
            return sm.describe_notebook_instance(NotebookInstanceName=resource_name)
        elif resource_type == RESOURCE_MODEL:
            return sm.describe_model(ModelName=resource_name)
        else:
            raise ValueError(f'Unsupported resource type: {resource_type}')

    def _get_resource_arn(self, resource_info: dict, resource_type: str) -> str:
        """Extract the ARN from resource info.

        Args:
            resource_info: Resource description dictionary
            resource_type: Type of resource

        Returns:
            Resource ARN string
        """
        arn_keys = {
            RESOURCE_ENDPOINT: 'EndpointArn',
            RESOURCE_TRAINING_JOB: 'TrainingJobArn',
            RESOURCE_NOTEBOOK: 'NotebookInstanceArn',
            RESOURCE_MODEL: 'ModelArn',
        }
        return resource_info.get(arn_keys.get(resource_type, ''), '')

    def _build_summary(self, findings: list[dict]) -> dict[str, PillarSummary]:
        """Build a summary of findings by pillar.

        Args:
            findings: List of finding dictionaries

        Returns:
            Dictionary mapping pillar names to PillarSummary objects
        """
        summary: dict[str, PillarSummary] = {}
        for f in findings:
            pillar = f['pillar']
            if pillar not in summary:
                summary[pillar] = PillarSummary()
            sev = f['severity']
            current = getattr(summary[pillar], sev, 0)
            setattr(summary[pillar], sev, current + 1)
        return summary

    def _format_screen_summary(
        self,
        resource_name: str,
        resource_type: str,
        findings: list,
        summary: dict[str, PillarSummary],
        report_path: str | None = None,
    ) -> str:
        """Format a concise on-screen summary for a single resource validation.

        Args:
            resource_name: Name of the validated resource
            resource_type: Type of the resource
            findings: List of Finding objects
            summary: Summary by pillar
            report_path: Optional path to the HTML report

        Returns:
            Formatted summary string
        """
        total = len(findings)
        high = sum(1 for f in findings if f.severity == 'HIGH')
        medium = sum(1 for f in findings if f.severity == 'MEDIUM')
        low = sum(1 for f in findings if f.severity == 'LOW')

        lines = [
            f'Well-Architected Validation: {resource_type} "{resource_name}"',
            f'Total findings: {total} (HIGH: {high}, MEDIUM: {medium}, LOW: {low})',
            '',
        ]

        for pillar, ps in summary.items():
            pillar_total = ps.HIGH + ps.MEDIUM + ps.LOW
            parts = []
            if ps.HIGH:
                parts.append(f'{ps.HIGH} high')
            if ps.MEDIUM:
                parts.append(f'{ps.MEDIUM} medium')
            if ps.LOW:
                parts.append(f'{ps.LOW} low')
            lines.append(f'  {pillar}: {pillar_total} ({", ".join(parts)})')

        # Show HIGH severity findings inline
        high_findings = [f for f in findings if f.severity == 'HIGH']
        if high_findings:
            lines.append('')
            lines.append('HIGH severity findings:')
            for f in high_findings:
                lines.append(f'  [{f.check}] {f.detail}')

        if report_path:
            lines.append('')
            lines.append(f'Detailed HTML report: {report_path}')

        return '\n'.join(lines)

    def _format_batch_screen_summary(
        self,
        validated: list[str],
        findings: list,
        summary: dict[str, PillarSummary],
        report_path: str | None = None,
    ) -> str:
        """Format a concise on-screen summary for batch validation.

        Args:
            validated: List of validated resource names
            findings: List of Finding objects
            summary: Summary by pillar
            report_path: Optional path to the HTML report

        Returns:
            Formatted summary string
        """
        total = len(findings)
        high = sum(1 for f in findings if f.severity == 'HIGH')
        medium = sum(1 for f in findings if f.severity == 'MEDIUM')
        low = sum(1 for f in findings if f.severity == 'LOW')

        lines = [
            f'Well-Architected Validation: {len(validated)} resources',
            f'Total findings: {total} (HIGH: {high}, MEDIUM: {medium}, LOW: {low})',
            '',
        ]

        for pillar, ps in summary.items():
            pillar_total = ps.HIGH + ps.MEDIUM + ps.LOW
            parts = []
            if ps.HIGH:
                parts.append(f'{ps.HIGH} high')
            if ps.MEDIUM:
                parts.append(f'{ps.MEDIUM} medium')
            if ps.LOW:
                parts.append(f'{ps.LOW} low')
            lines.append(f'  {pillar}: {pillar_total} ({", ".join(parts)})')

        # Show HIGH severity findings inline
        high_findings = [f for f in findings if f.severity == 'HIGH']
        if high_findings:
            lines.append('')
            lines.append(f'HIGH severity findings ({len(high_findings)}):')
            for f in high_findings[:10]:  # Cap at 10 for readability
                lines.append(f'  [{f.resource}] [{f.check}] {f.detail}')
            if len(high_findings) > 10:
                lines.append(f'  ... and {len(high_findings) - 10} more (see HTML report)')

        if report_path:
            lines.append('')
            lines.append(f'Detailed HTML report: {report_path}')

        return '\n'.join(lines)

    @validate_call
    async def validate_sagemaker_resource(
        self,
        ctx: Context,
        resource_type: str = Field(
            description='Type of SageMaker resource: endpoint, training_job, notebook_instance, or model.',
        ),
        resource_name: str = Field(
            description='Name of the SageMaker resource to validate.',
        ),
        region_name: Optional[SUPPORTED_REGIONS] = Field(
            'us-east-1',
            description='AWS region name. Default is us-east-1.',
        ),
        profile_name: Optional[str] = Field(
            None,
            description='AWS profile name. If not provided, uses the default profile.',
        ),
    ) -> CallToolResult:
        """Validate a SageMaker resource against all Well-Architected Framework pillars.

        Checks security, reliability, performance efficiency, cost optimization,
        operational excellence, and sustainability best practices for the specified resource.

        ## Supported Resource Types
        - **endpoint**: SageMaker real-time inference endpoints
        - **training_job**: SageMaker training jobs
        - **notebook_instance**: SageMaker notebook instances
        - **model**: SageMaker models

        ## Checks Performed
        - **Security**: KMS encryption, VPC isolation, network isolation, inter-container encryption
        - **Reliability**: Multi-instance endpoints, training timeouts, checkpointing, retry strategies
        - **Performance**: Instance generation, data capture, data distribution, instance sizing
        - **Cost**: Cost tags, spot training, serverless inference, lifecycle configs
        - **Operational Excellence**: Operational tags, auto-scaling, experiment tracking
        - **Sustainability**: Graviton instances, spot utilization, right-sizing

        ## Fallback Options
        - If this tool fails, use AWS CLI: `aws sagemaker describe-endpoint --endpoint-name <name>`
        - Or use the AWS SageMaker Console to review resource configurations

        Args:
            ctx: MCP context
            resource_type: Type of SageMaker resource
            resource_name: Name of the resource to validate
            region_name: AWS region name (default: us-east-1)
            profile_name: AWS profile name (optional)

        Returns:
            CallToolResult with findings and summary
        """
        try:
            log_with_request_id(
                ctx,
                LogLevel.INFO,
                f'Validating {resource_type} "{resource_name}" in {region_name}',
            )
            sm = self._get_sagemaker_client(ctx, region_name, profile_name)
            resource_info = self._describe_resource(sm, resource_type, resource_name)
            arn = self._get_resource_arn(resource_info, resource_type)
            tags = sm.list_tags(ResourceArn=arn).get('Tags', []) if arn else []

            raw_findings = run_all_validators(resource_type, resource_info, tags, region_name)
            findings = [Finding(**f) for f in raw_findings]
            summary = self._build_summary(raw_findings)

            # Build concise on-screen summary (no HTML report for single resource)
            result_text = self._format_screen_summary(
                resource_name,
                resource_type,
                findings,
                summary,
            )

            return _tool_result(
                result_text,
                {
                    'resource': resource_name,
                    'resource_type': resource_type,
                    'summary': {k: v.model_dump() for k, v in summary.items()},
                    'findings': [f.model_dump() for f in findings],
                },
            )
        except ClientError as e:
            error_text = f'AWS API error: {str(e)}'
            log_with_request_id(ctx, LogLevel.ERROR, error_text)
            return _tool_result(error_text, is_error=True)
        except Exception as e:
            error_text = f'Validation error: {str(e)}'
            log_with_request_id(ctx, LogLevel.ERROR, error_text)
            return _tool_result(error_text, is_error=True)

    @validate_call
    async def validate_all_endpoints(
        self,
        ctx: Context,
        region_name: Optional[SUPPORTED_REGIONS] = Field(
            'us-east-1',
            description='AWS region name. Default is us-east-1.',
        ),
        profile_name: Optional[str] = Field(
            None,
            description='AWS profile name. If not provided, uses the default profile.',
        ),
    ) -> CallToolResult:
        """Validate all SageMaker endpoints in a region against Well-Architected pillars.

        Scans all endpoints in the specified region and returns aggregated findings
        across all six Well-Architected pillars.

        Args:
            ctx: MCP context
            region_name: AWS region name (default: us-east-1)
            profile_name: AWS profile name (optional)

        Returns:
            CallToolResult with aggregated findings
        """
        try:
            log_with_request_id(ctx, LogLevel.INFO, f'Validating all endpoints in {region_name}')
            sm = self._get_sagemaker_client(ctx, region_name, profile_name)
            endpoints = sm.list_endpoints(MaxResults=50).get('Endpoints', [])

            all_findings: list[dict] = []
            validated: list[str] = []

            for ep in endpoints:
                name = ep['EndpointName']
                try:
                    info = sm.describe_endpoint(EndpointName=name)
                    arn = info.get('EndpointArn', '')
                    tags = sm.list_tags(ResourceArn=arn).get('Tags', []) if arn else []
                    findings = run_all_validators(RESOURCE_ENDPOINT, info, tags, region_name)
                    all_findings.extend(findings)
                    validated.append(name)
                except ClientError as e:
                    log_with_request_id(
                        ctx, LogLevel.WARNING, f'Could not validate endpoint {name}: {e}'
                    )

            typed_findings = [Finding(**f) for f in all_findings]
            summary = self._build_summary(all_findings)

            # Build concise on-screen summary (no HTML report — use validate_all_resources for that)
            result_text = self._format_batch_screen_summary(
                validated,
                typed_findings,
                summary,
            )

            return _tool_result(
                result_text,
                {
                    'resources_validated': validated,
                    'total_findings': len(typed_findings),
                    'summary': {k: v.model_dump() for k, v in summary.items()},
                    'findings': [f.model_dump() for f in typed_findings],
                },
            )
        except ClientError as e:
            error_text = f'AWS API error: {str(e)}'
            log_with_request_id(ctx, LogLevel.ERROR, error_text)
            return _tool_result(error_text, is_error=True)

    @validate_call
    async def validate_all_resources(
        self,
        ctx: Context,
        region_name: Optional[SUPPORTED_REGIONS] = Field(
            'us-east-1',
            description='AWS region name. Default is us-east-1.',
        ),
        profile_name: Optional[str] = Field(
            None,
            description='AWS profile name. If not provided, uses the default profile.',
        ),
    ) -> CallToolResult:
        """Validate all SageMaker resources in a region against Well-Architected pillars.

        Scans all endpoints, training jobs, notebook instances, and models in the
        specified region. Returns an on-screen summary and generates a single
        comprehensive HTML report (wa-report.html) with all findings.

        Args:
            ctx: MCP context
            region_name: AWS region name (default: us-east-1)
            profile_name: AWS profile name (optional)

        Returns:
            CallToolResult with aggregated findings and path to HTML report
        """
        try:
            log_with_request_id(
                ctx, LogLevel.INFO, f'Validating all SageMaker resources in {region_name}'
            )
            sm = self._get_sagemaker_client(ctx, region_name, profile_name)

            all_findings: list[dict] = []
            validated: list[str] = []

            # Endpoints
            try:
                endpoints = sm.list_endpoints(MaxResults=50).get('Endpoints', [])
                for ep in endpoints:
                    name = ep['EndpointName']
                    try:
                        info = sm.describe_endpoint(EndpointName=name)
                        arn = info.get('EndpointArn', '')
                        tags = sm.list_tags(ResourceArn=arn).get('Tags', []) if arn else []
                        findings = run_all_validators(RESOURCE_ENDPOINT, info, tags, region_name)
                        for f in findings:
                            f['resource'] = f'endpoint/{f["resource"]}'
                        all_findings.extend(findings)
                        validated.append(f'endpoint/{name}')
                    except ClientError as e:
                        log_with_request_id(
                            ctx, LogLevel.WARNING, f'Could not validate endpoint {name}: {e}'
                        )
            except ClientError as e:
                log_with_request_id(ctx, LogLevel.WARNING, f'Could not list endpoints: {e}')

            # Training jobs (recent)
            try:
                jobs = sm.list_training_jobs(
                    MaxResults=50, SortBy='CreationTime', SortOrder='Descending'
                ).get('TrainingJobSummaries', [])
                for job in jobs:
                    name = job['TrainingJobName']
                    try:
                        info = sm.describe_training_job(TrainingJobName=name)
                        arn = info.get('TrainingJobArn', '')
                        tags = sm.list_tags(ResourceArn=arn).get('Tags', []) if arn else []
                        findings = run_all_validators(
                            RESOURCE_TRAINING_JOB, info, tags, region_name
                        )
                        for f in findings:
                            f['resource'] = f'training_job/{f["resource"]}'
                        all_findings.extend(findings)
                        validated.append(f'training_job/{name}')
                    except ClientError as e:
                        log_with_request_id(
                            ctx, LogLevel.WARNING, f'Could not validate training job {name}: {e}'
                        )
            except ClientError as e:
                log_with_request_id(ctx, LogLevel.WARNING, f'Could not list training jobs: {e}')

            # Notebook instances
            try:
                notebooks = sm.list_notebook_instances(MaxResults=50).get('NotebookInstances', [])
                for nb in notebooks:
                    name = nb['NotebookInstanceName']
                    try:
                        info = sm.describe_notebook_instance(NotebookInstanceName=name)
                        arn = info.get('NotebookInstanceArn', '')
                        tags = sm.list_tags(ResourceArn=arn).get('Tags', []) if arn else []
                        findings = run_all_validators(RESOURCE_NOTEBOOK, info, tags, region_name)
                        for f in findings:
                            f['resource'] = f'notebook/{f["resource"]}'
                        all_findings.extend(findings)
                        validated.append(f'notebook/{name}')
                    except ClientError as e:
                        log_with_request_id(
                            ctx,
                            LogLevel.WARNING,
                            f'Could not validate notebook {name}: {e}',
                        )
            except ClientError as e:
                log_with_request_id(
                    ctx, LogLevel.WARNING, f'Could not list notebook instances: {e}'
                )

            # Models (recent)
            try:
                models = sm.list_models(
                    MaxResults=50, SortBy='CreationTime', SortOrder='Descending'
                ).get('Models', [])
                for model in models:
                    name = model['ModelName']
                    try:
                        info = sm.describe_model(ModelName=name)
                        arn = info.get('ModelArn', '')
                        tags = sm.list_tags(ResourceArn=arn).get('Tags', []) if arn else []
                        findings = run_all_validators(RESOURCE_MODEL, info, tags, region_name)
                        for f in findings:
                            f['resource'] = f'model/{f["resource"]}'
                        all_findings.extend(findings)
                        validated.append(f'model/{name}')
                    except ClientError as e:
                        log_with_request_id(
                            ctx, LogLevel.WARNING, f'Could not validate model {name}: {e}'
                        )
            except ClientError as e:
                log_with_request_id(ctx, LogLevel.WARNING, f'Could not list models: {e}')

            typed_findings = [Finding(**f) for f in all_findings]
            summary = self._build_summary(all_findings)

            # Fetch account ID for the report
            account_id = None
            try:
                sts = AwsHelper.create_boto3_client('sts', region_name=region_name)
                account_id = sts.get_caller_identity().get('Account')
            except ClientError:
                pass

            # Generate single comprehensive HTML report
            report_path = generate_batch_html_report(
                validated,
                all_findings,
                summary,
                account_id=account_id,
                region=region_name,
            )

            # Build concise on-screen summary
            result_text = self._format_batch_screen_summary(
                validated,
                typed_findings,
                summary,
                report_path,
            )

            return _tool_result(
                result_text,
                {
                    'resources_validated': validated,
                    'total_findings': len(typed_findings),
                    'summary': {k: v.model_dump() for k, v in summary.items()},
                    'findings': [f.model_dump() for f in typed_findings],
                },
            )
        except Exception as e:
            error_text = f'Validation error: {str(e)}'
            log_with_request_id(ctx, LogLevel.ERROR, error_text)
            return _tool_result(error_text, is_error=True)

    @validate_call
    async def list_sagemaker_resources(
        self,
        ctx: Context,
        region_name: Optional[SUPPORTED_REGIONS] = Field(
            'us-east-1',
            description='AWS region name. Default is us-east-1.',
        ),
        profile_name: Optional[str] = Field(
            None,
            description='AWS profile name. If not provided, uses the default profile.',
        ),
    ) -> CallToolResult:
        """List SageMaker resources available for validation.

        Returns endpoints, training jobs, notebook instances, and models
        in the specified region.

        Args:
            ctx: MCP context
            region_name: AWS region name (default: us-east-1)
            profile_name: AWS profile name (optional)

        Returns:
            CallToolResult with resource lists
        """
        try:
            log_with_request_id(
                ctx, LogLevel.INFO, f'Listing SageMaker resources in {region_name}'
            )
            sm = self._get_sagemaker_client(ctx, region_name, profile_name)
            endpoint_summaries: list[ResourceSummary] = []
            training_job_summaries: list[ResourceSummary] = []
            notebook_summaries: list[ResourceSummary] = []
            model_summaries: list[ResourceSummary] = []

            try:
                eps = sm.list_endpoints(MaxResults=50).get('Endpoints', [])
                endpoint_summaries = [
                    ResourceSummary(name=e['EndpointName'], status=e['EndpointStatus'])
                    for e in eps
                ]
            except ClientError as e:
                log_with_request_id(ctx, LogLevel.WARNING, f'Could not list endpoints: {e}')

            try:
                jobs = sm.list_training_jobs(
                    MaxResults=50, SortBy='CreationTime', SortOrder='Descending'
                ).get('TrainingJobSummaries', [])
                training_job_summaries = [
                    ResourceSummary(name=j['TrainingJobName'], status=j['TrainingJobStatus'])
                    for j in jobs
                ]
            except ClientError as e:
                log_with_request_id(ctx, LogLevel.WARNING, f'Could not list training jobs: {e}')

            try:
                nbs = sm.list_notebook_instances(MaxResults=50).get('NotebookInstances', [])
                notebook_summaries = [
                    ResourceSummary(
                        name=n['NotebookInstanceName'], status=n['NotebookInstanceStatus']
                    )
                    for n in nbs
                ]
            except ClientError as e:
                log_with_request_id(
                    ctx, LogLevel.WARNING, f'Could not list notebook instances: {e}'
                )

            try:
                models = sm.list_models(
                    MaxResults=50, SortBy='CreationTime', SortOrder='Descending'
                ).get('Models', [])
                model_summaries = [ResourceSummary(name=m['ModelName']) for m in models]
            except ClientError as e:
                log_with_request_id(ctx, LogLevel.WARNING, f'Could not list models: {e}')

            structured = {
                'endpoints': [e.model_dump() for e in endpoint_summaries],
                'training_jobs': [j.model_dump() for j in training_job_summaries],
                'notebook_instances': [n.model_dump() for n in notebook_summaries],
                'models': [m.model_dump() for m in model_summaries],
            }
            result_text = json.dumps(structured, indent=2, default=str)

            return _tool_result(result_text, structured)
        except Exception as e:
            error_text = f'Error listing resources: {str(e)}'
            log_with_request_id(ctx, LogLevel.ERROR, error_text)
            return _tool_result(error_text, is_error=True)

    @validate_call
    async def get_pillar_details(
        self,
        ctx: Context,
        pillar: str = Field(
            description='Pillar name: security, reliability, performance, cost, operational_excellence, or sustainability.',
        ),
    ) -> CallToolResult:
        """Get detailed information about a specific Well-Architected pillar and its checks.

        Returns the pillar description and all validation checks that are performed
        for the specified pillar.

        Args:
            ctx: MCP context
            pillar: Pillar name

        Returns:
            CallToolResult with pillar details and checks
        """
        key = pillar.lower().replace(' ', '_').replace('-', '_')
        info = PILLAR_METADATA.get(key)

        if not info:
            error_text = f"Unknown pillar '{pillar}'. Valid: {list(PILLAR_METADATA.keys())}"
            return _tool_result(error_text, is_error=True)

        checks = [PillarCheck(**c) for c in info['checks']]
        result_text = json.dumps(info, indent=2)

        return _tool_result(
            result_text,
            {
                'name': info['name'],
                'description': info['description'],
                'checks': [c.model_dump() for c in checks],
            },
        )
