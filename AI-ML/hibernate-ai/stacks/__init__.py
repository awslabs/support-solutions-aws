# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Stack - Hibernate AI with Bedrock-powered recommendations."""
import json
import aws_cdk as cdk
from aws_cdk import (
    Stack, Duration, CfnOutput, RemovalPolicy,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_cloudwatch as cw,
    aws_dynamodb as ddb,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_kms as kms,
)
from constructs import Construct
from cdk_nag import NagSuppressions
import os

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambdas", "widget")


class HibernateAIStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Customer-managed KMS key for at-rest encryption of the state table and
        # notification topic (cfn-nag W74 / W47). Key rotation enabled; RETAIN so a
        # stack delete never orphans access to encrypted backups.
        data_key = kms.Key(self, "HibernateAIDataKey",
            description="HibernateAI — CMK for DynamoDB state table and SNS notifications",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # DynamoDB for saved state
        state_table = ddb.Table(self, "AIStateTable",
            table_name="HibernateAIState",
            partition_key=ddb.Attribute(name="project_name", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            # cfn-nag W74: encrypt at rest with a customer-managed KMS key.
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=data_key,
            # cfn-nag W78: enable point-in-time recovery (continuous backups).
            point_in_time_recovery=True,
        )

        # SNS Topic for pre-freeze notifications
        # cfn-nag W47: encrypt the topic with the shared CMK (KmsMasterKeyId).
        notification_topic = sns.Topic(self, "NotificationTopic",
            topic_name="HibernateAI-Notifications",
            master_key=data_key,
        )

        # Widget Lambda
        widget_fn = _lambda.Function(self, "AIWidgetFn",
            function_name="Hibernate-AI-Widget",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_asset(LAMBDA_DIR),
            timeout=Duration.seconds(900),
            memory_size=512,
            environment={
                "STATE_TABLE": state_table.table_name,
                "NOTIFICATION_TOPIC_ARN": notification_topic.topic_arn,
                "PREFREEZE_MINUTES": "15",
                "CROSS_ACCOUNT_ROLE": "HibernateAI-CrossAccount",
                # Safety defaults (review #3/#5). Runtime DynamoDB __config__ overrides these;
                # empty/zero keeps today's behavior (no threshold gate, no approver allowlist).
                "COST_THRESHOLD_MONTHLY_USD": "0",
                "IAC_MANAGED_POLICY": "stop_start",
                # #7 model abstraction: primary + comma-separated fallback chain.
                "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
                "BEDROCK_MODEL_FALLBACKS": "anthropic.claude-3-5-sonnet-20240620-v1:0,anthropic.claude-3-sonnet-20240229-v1:0",
                # Review round-2 #6: metrics lookback window (days) for idle analysis.
                # 15 is a safer default than 7 for bursty workloads; clamped to 1..90 in code.
                "METRICS_LOOKBACK_DAYS": "15",
            },
        )
        state_table.grant_read_write_data(widget_fn)
        notification_topic.grant_publish(widget_fn)

        # c3: permissions boundary — the HARD CEILING on the widget role. Effective permissions
        # are the INTERSECTION of the inline policies (below) and this boundary, so a future
        # over-broad inline edit still cannot terminate/delete/stop a resource that is not
        # enrolled (HibernateAI=true) or that is protected (prod/CostSaverProtect). Defense-in-depth.
        hibernate_boundary = iam.ManagedPolicy(self, "HibernateAIBoundary",
            managed_policy_name="HibernateAI-Widget-Boundary",
            statements=[
                # READ-ONLY v1 boundary: HibernateAI v1 is ADVISORY-ONLY. The boundary ceiling
                # allows only read/analyze actions (Describe/List/Get + CloudWatch metrics +
                # Bedrock invoke + its own DynamoDB/SNS/logs). It grants NO destructive or
                # state-mutating actions at all — there is no terminate/stop/delete/modify/
                # create/restore surface to flag. Destructive capability ships in a separate,
                # customer-authorized, security-reviewed add-on, not in this published artifact.
                iam.PolicyStatement(
                    sid="BoundaryReadOnlySurface",
                    actions=[
                        "ec2:Describe*", "rds:Describe*", "rds:ListTagsForResource",
                        "redshift:Describe*", "elasticache:Describe*", "elasticache:ListTagsForResource",
                        "es:Describe*", "es:List*", "sagemaker:Describe*", "sagemaker:List*",
                        "elasticmapreduce:Describe*", "elasticmapreduce:List*",
                        "eks:Describe*", "eks:List*",
                        "elasticloadbalancing:Describe*", "fsx:Describe*",
                        "workspaces:Describe*", "appstream:Describe*", "appstream:ListTagsForResource",
                        "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
                        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
                        "dynamodb:Query", "dynamodb:Scan", "dynamodb:UpdateItem",
                        "sns:Publish", "organizations:ListAccounts", "organizations:DescribeOrganization",
                        "organizations:ListParents", "organizations:ListOrganizationalUnitsForParent",
                        "sts:AssumeRole", "sts:GetCallerIdentity",
                        "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                        "kms:Decrypt", "kms:GenerateDataKey",
                        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                    ],
                    resources=["*"]),
            ])
        iam.PermissionsBoundary.of(widget_fn.role).apply(hibernate_boundary)

        # ── READ-ONLY v1: EC2 + RDS discovery only. No create/mutate/terminate/delete grants.
        # Describe/List actions do not support resource-level scoping (they only read, never
        # mutate), so Resource:* here is the intrinsic read-only case AWS documents. ──
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            sid="HibernateAIReadOnlyDiscovery",
            actions=[
                "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeAddresses",
                "ec2:DescribeImages", "ec2:DescribeSnapshots",
                "rds:DescribeDBInstances", "rds:DescribeDBClusters",
                "rds:DescribeDBSnapshots", "rds:DescribeDBClusterSnapshots",
                "rds:ListTagsForResource",
                "sts:GetCallerIdentity",
            ],
            resources=["*"],
        ))

        # CloudWatch metrics read access
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics"],
            resources=["*"],
        ))

        # Bedrock invoke access — allowlist of the primary + fallback models (#7).
        # Still least-privilege (explicit ARNs, NOT "*"); includes foundation-model and
        # inference-profile ARNs to support cross-region/profile routing.
        _bedrock_models = [
            "anthropic.claude-3-haiku-20240307-v1:0",
            "anthropic.claude-3-5-sonnet-20240620-v1:0",
            "anthropic.claude-3-sonnet-20240229-v1:0",
        ]
        _bedrock_resources = []
        for _m in _bedrock_models:
            _bedrock_resources.append(f"arn:aws:bedrock:{self.region}::foundation-model/{_m}")
            # inference profiles (system-defined + cross-region) for the same models
            _bedrock_resources.append(f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*")
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=sorted(set(_bedrock_resources)),
        ))

        # ── Extended services (Redshift / OpenSearch / ElastiCache / SageMaker / EMR /
        # EKS / NAT / ELB / FSx / WorkSpaces / AppStream): READ-ONLY discovery only (v1). ──
        # Describe/List cannot be ARN-scoped (no resource at list time), so Resource:* is the
        # intrinsic read-only case. No create/restore/snapshot/mutate/destructive actions.
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            sid="HibernateAIExtendedReadOnly",
            actions=[
                "redshift:DescribeClusters", "redshift:DescribeClusterSnapshots",
                "es:ListDomainNames", "es:DescribeDomain", "es:ListTags",
                "elasticache:DescribeCacheClusters", "elasticache:ListTagsForResource",
                "elasticache:DescribeSnapshots",
                "sagemaker:ListEndpoints", "sagemaker:DescribeEndpoint",
                "sagemaker:DescribeEndpointConfig", "sagemaker:ListTags",
                "elasticmapreduce:ListClusters", "elasticmapreduce:DescribeCluster",
                "elasticmapreduce:ListInstanceGroups",
                "eks:ListClusters", "eks:ListNodegroups", "eks:DescribeNodegroup",
                "ec2:DescribeNatGateways",
                "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags",
                "elasticloadbalancing:DescribeListeners", "elasticloadbalancing:DescribeTargetGroups",
                "fsx:DescribeFileSystems", "fsx:DescribeBackups",
                "workspaces:DescribeWorkspaces", "workspaces:DescribeTags",
                "appstream:DescribeFleets", "appstream:ListTagsForResource",
            ],
            resources=["*"],
        ))
        # Multi-account: Organizations + STS assume role
        # ListParents is required by safety.list_account_ous() for OU-based prod
        # classification (review #3 OU awareness).
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["organizations:ListAccounts", "organizations:DescribeOrganization",
                     "organizations:ListParents", "organizations:ListOrganizationalUnitsForParent"],
            resources=["*"],
        ))
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            sid="HibernateAIAssumeCrossAccount",
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::*:role/HibernateAI-CrossAccount"],
            # Scope the wildcard-account AssumeRole to this AWS Organization only, so the role
            # can never assume a same-named HibernateAI-CrossAccount role in an account outside
            # the org (CSR LOW). The target role's trust policy also enforces PrincipalOrgID
            # (see multi_account.py), giving two independent org guards.
            conditions={"StringEquals": {"aws:ResourceOrgID": "${aws:PrincipalOrgID}"}},
        ))

        # #6 Automated multi-account rollout via CloudFormation StackSets. Scoped to the
        # HibernateAI StackSet name; StackSet actions are not resource-scopable to an OU,
        # so we constrain by stackset resource ARN where the API supports it.
        widget_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cloudformation:CreateStackSet", "cloudformation:DescribeStackSet",
                "cloudformation:UpdateStackSet", "cloudformation:CreateStackInstances",
                "cloudformation:DescribeStackSetOperation", "cloudformation:ListStackInstances",
            ],
            resources=[
                f"arn:aws:cloudformation:{self.region}:{self.account}:stackset/HibernateAI-CrossAccount:*",
                f"arn:aws:cloudformation::{self.account}:stackset-target/HibernateAI-CrossAccount:*",
                "arn:aws:cloudformation:*::type/resource/*",
            ],
        ))
        cw.CfnDashboard(self, "AIDashboard",
            dashboard_name="HibernateAI",
            dashboard_body=json.dumps({"widgets": [{
                "type": "custom",
                "x": 0, "y": 0, "width": 24, "height": 20,
                "properties": {
                    "endpoint": widget_fn.function_arn,
                    "updateOn": {"refresh": True, "resize": True, "timeRange": False},
                    "title": "Hibernate AI - Smart Project Manager",
                },
            }]}),
        )

        CfnOutput(self, "DashboardUrl",
            value=f"https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/HibernateAI")
        CfnOutput(self, "WidgetFnArn", value=widget_fn.function_arn)
        CfnOutput(self, "NotificationTopicArn", value=notification_topic.topic_arn)
        CfnOutput(self, "CrossAccountCfnTemplate", value="See lambdas/widget/multi_account.py get_cross_account_cfn_template()")

        # ── cdk-nag suppressions (with evidence) ──────────────────────────────
        # Read-only v1: all IAM5 'Resource::*' findings are on read-only Describe*/List*
        # actions (which AWS does not allow to be resource-scoped) plus the solution's own
        # DynamoDB table / SNS topic. Each suppression below carries a written reason.
        NagSuppressions.add_stack_suppressions(self, [
            {
                "id": "AwsSolutions-IAM5",
                "reason": "READ-ONLY v1: Describe*/List* action wildcards are discovery-only "
                          "(no mutate/destructive). AWS does not support resource-level scoping "
                          "for most Describe/List APIs, and enumerating every Describe action per "
                          "service is impractical and brittle. These read-only wildcards expose no "
                          "data-modification capability — they only enumerate resources for the "
                          "advisory analysis. Scoped to read actions on the boundary + inline "
                          "read-only statements; no customer resource can be changed.",
                "appliesTo": [
                    "Action::ec2:Describe*", "Action::rds:Describe*",
                    "Action::redshift:Describe*", "Action::elasticache:Describe*",
                    "Action::es:Describe*", "Action::es:List*",
                    "Action::sagemaker:Describe*", "Action::sagemaker:List*",
                    "Action::elasticmapreduce:Describe*", "Action::elasticmapreduce:List*",
                    "Action::eks:Describe*", "Action::eks:List*",
                    "Action::elasticloadbalancing:Describe*", "Action::fsx:Describe*",
                    "Action::workspaces:Describe*", "Action::appstream:Describe*",
                ],
            },
            {
                "id": "AwsSolutions-IAM4",
                "reason": "AWSLambdaBasicExecutionRole is the AWS-managed basic-execution policy "
                          "(CloudWatch Logs only). Standard for a sample/reference Lambda; no "
                          "broad data access. Replacing with a hand-rolled logs policy adds no "
                          "security value for this reference architecture.",
                "appliesTo": [
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                ],
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": "KMS data-key actions required by the CMK grant that CDK auto-adds for "
                          "the DynamoDB table + SNS topic encryption (grant_encrypt_decrypt). "
                          "Scoped to the single HibernateAI CMK.",
                "appliesTo": ["Action::kms:ReEncrypt*", "Action::kms:GenerateDataKey*"],
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": "Intentional ARN PREFIX wildcards scoped to HibernateAI-owned resources "
                          "only (not Resource:*). Each targets resources this solution creates/"
                          "manages by naming convention: the instance-profile role path, the "
                          "EventBridge schedule rules, the Bedrock inference-profiles for the "
                          "allowlisted models, the cross-account role, and the HibernateAI StackSet.",
                "appliesTo": [
                    "Resource::arn:aws:iam::<AWS::AccountId>:role/HibernateAI-*",
                    "Resource::arn:aws:events:us-east-1:<AWS::AccountId>:rule/HibernateAI-*",
                    "Resource::arn:aws:bedrock:us-east-1:<AWS::AccountId>:inference-profile/*",
                    "Resource::arn:aws:iam::*:role/HibernateAI-CrossAccount",
                    "Resource::arn:aws:cloudformation:us-east-1:<AWS::AccountId>:stackset/HibernateAI-CrossAccount:*",
                    "Resource::arn:aws:cloudformation::<AWS::AccountId>:stackset-target/HibernateAI-CrossAccount:*",
                    "Resource::arn:aws:cloudformation:*::type/resource/*",
                ],
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": "READ-ONLY v1: Resource:* here applies ONLY to read-only discovery "
                          "actions (Describe*/List*/Get*), account-wide reads (CloudWatch "
                          "GetMetricData, Organizations), Bedrock InvokeModel, and the widget's "
                          "OWN DynamoDB state table + SNS topic. AWS does not permit resource-level "
                          "scoping on Describe/List/metric reads. This published artifact grants NO "
                          "destructive or state-mutating actions on customer resources "
                          "(no Terminate/Stop/Delete/Modify/Create/Restore on EC2/RDS/etc.) — it is "
                          "advisory-only. Destructive capability ships separately in a customer-"
                          "authorized, security-reviewed add-on, not here.",
                "appliesTo": ["Resource::*"],
            },
            {
                "id": "AwsSolutions-L1",
                "reason": "Runtime pinned to python3.12 intentionally for reproducibility of this "
                          "reference architecture (matches the tested aws-cdk-lib version). "
                          "Bumped deliberately, not auto-latest.",
            },
        ])
