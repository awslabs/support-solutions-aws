# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app: deploy the Well-Architected Review MCP Server for Amazon SageMaker to Amazon
Bedrock AgentCore Runtime.

Builds the repo's own Dockerfile (see ../Dockerfile) as a local asset,
pushes it to a CDK-managed ECR repository, and creates an AgentCore
Runtime configured for the MCP protocol with a scoped, read-only
execution role matching the AWS services this server's validators
actually call (see ../well_architected_review_mcp_server_for_sagemaker/validators/).

Usage:
    cd cdk
    pip install -r requirements.txt
    cdk bootstrap   # one-time per account/region
    cdk deploy
"""

import os

from aws_cdk import App, Aspects, CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from cdk_nag import AwsSolutionsChecks, NagSuppressions
from constructs import Construct


# Read-only permissions matching the AWS services this server's validators
# actually call, verified against well_architected_review_mcp_server_for_sagemaker/validators/
# and aws_helper.py.
EXECUTION_ROLE_STATEMENTS = [
    iam.PolicyStatement(
        sid='SageMakerReadOnly',
        actions=[
            'sagemaker:DescribeEndpoint',
            'sagemaker:DescribeEndpointConfig',
            'sagemaker:DescribeTrainingJob',
            'sagemaker:DescribeNotebookInstance',
            'sagemaker:DescribeModel',
            'sagemaker:ListEndpoints',
            'sagemaker:ListTrainingJobs',
            'sagemaker:ListNotebookInstances',
            'sagemaker:ListModels',
            'sagemaker:ListTags',
        ],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='CloudWatchMetricsAndAlarmsReadOnly',
        actions=[
            'cloudwatch:ListMetrics',
            'cloudwatch:DescribeAlarmsForMetric',
            'cloudwatch:GetMetricStatistics',
        ],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='CloudWatchLogsReadOnly',
        actions=['logs:DescribeLogGroups'],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='S3ReadOnly',
        actions=[
            's3:GetBucketEncryption',
            's3:GetBucketVersioning',
            's3:GetBucketReplication',
            's3:GetLifecycleConfiguration',
            's3:GetIntelligentTieringConfiguration',
        ],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='IamReadOnly',
        actions=['iam:ListAttachedRolePolicies'],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='Ec2NetworkingReadOnly',
        actions=[
            'ec2:DescribeSubnets',
            'ec2:DescribeVpcEndpoints',
            'ec2:DescribeFlowLogs',
        ],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='KmsReadOnly',
        actions=['kms:GetKeyRotationStatus'],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='CloudTrailReadOnly',
        actions=['cloudtrail:DescribeTrails', 'cloudtrail:GetTrailStatus'],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='ConfigReadOnly',
        actions=['config:DescribeConfigurationRecorderStatus'],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='ApplicationAutoScalingReadOnly',
        actions=[
            'application-autoscaling:DescribeScalableTargets',
            'application-autoscaling:DescribeScalingPolicies',
        ],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='ServiceQuotasReadOnly',
        actions=['servicequotas:GetServiceQuota'],
        resources=['*'],
    ),
    iam.PolicyStatement(
        sid='StsCallerIdentityReadOnly',
        actions=['sts:GetCallerIdentity'],
        resources=['*'],
    ),
]


class SageMakerWaMcpAgentCoreStack(Stack):
    """AgentCore Runtime deployment of the SageMaker WA MCP server."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repo_root = os.path.join(os.path.dirname(__file__), '..')

        execution_role = iam.Role(
            self,
            'ExecutionRole',
            assumed_by=iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
            description=(
                'Read-only execution role for the SageMaker Well-Architected '
                'MCP server on AgentCore Runtime. Grants only the describe/'
                'list/get actions the validators actually call - see '
                'well_architected_review_mcp_server_for_sagemaker/validators/.'
            ),
        )
        for statement in EXECUTION_ROLE_STATEMENTS:
            execution_role.add_to_policy(statement)

        # Builds ../Dockerfile as a local asset; cdk deploy pushes it to a
        # CDK-managed ECR repository automatically.
        agent_runtime_artifact = agentcore.AgentRuntimeArtifact.from_asset(repo_root)

        runtime = agentcore.Runtime(
            self,
            'SageMakerWaMcpRuntime',
            runtime_name='sagemakerWaMcpServer',
            agent_runtime_artifact=agent_runtime_artifact,
            execution_role=execution_role,
            protocol_configuration=agentcore.ProtocolType.MCP,
            description='Well-Architected Review MCP Server for Amazon SageMaker',
            # authorizer_configuration intentionally omitted: IAM
            # authentication (SigV4) is the default when unset, per the
            # aws_bedrockagentcore construct README.
        )

        # cdk-nag AwsSolutions-IAM5: documented, reviewed suppressions.
        # (1) Resource::* on the read-only statements above - the SageMaker
        #     Describe*/List*, CloudWatch metrics, S3 Get*, EC2 Describe*, IAM
        #     ListAttachedRolePolicies, KMS/CloudTrail/Config/ServiceQuotas/STS
        #     actions granted here are read-only and do not support
        #     resource-level permissions (they act across resources or the AWS
        #     APIs reject a scoped ARN), so '*' is the least privilege possible
        #     for this scope.
        # (2) The log-group and workload-identity wildcards are generated by the
        #     aws_bedrockagentcore.Runtime L2 construct itself, not by this
        #     stack, and are required for the runtime to emit its own logs and
        #     resolve its workload identity.
        NagSuppressions.add_resource_suppressions(
            execution_role,
            [
                {
                    'id': 'AwsSolutions-IAM5',
                    'reason': (
                        'Read-only Describe/List/Get actions that do not support '
                        'resource-level permissions; scoped to read-only. '
                        'Log-group and workload-identity wildcards are emitted by '
                        'the aws_bedrockagentcore.Runtime L2 construct and are '
                        'required for runtime logging and workload-identity '
                        'resolution.'
                    ),
                    'appliesTo': [
                        'Resource::*',
                        'Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/aws/bedrock-agentcore/runtimes/*',
                        'Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:*',
                        'Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*',
                        'Resource::arn:<AWS::Partition>:bedrock-agentcore:<AWS::Region>:<AWS::AccountId>:workload-identity-directory/default/workload-identity/*',
                    ],
                },
            ],
            apply_to_children=True,
        )

        CfnOutput(
            self,
            'AgentRuntimeArn',
            value=runtime.agent_runtime_arn,
            description='ARN of the deployed AgentCore Runtime',
        )


app = App()
SageMakerWaMcpAgentCoreStack(app, 'SageMakerWaMcpAgentCoreStack')
# Enforce AWS Solutions security checks on every synth/deploy so the repo is
# self-verifying. Any new unsuppressed finding fails synth. reports=True writes
# an AwsSolutions-<stack>-NagReport.csv into the cdk output directory.
Aspects.of(app).add(AwsSolutionsChecks(verbose=True, reports=True))
app.synth()
