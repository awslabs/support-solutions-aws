// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';
import { HEALTH_READ_ACTIONS, HUB_PIPELINE_ROLE_NAME, SCANNER_READ_ACTIONS, SPOKE_ROLE_NAME } from './scan-permissions';
import { ACTION_PLAN_TABLE_INDEXES, LIFECYCLE_TABLE_INDEXES } from './data-stack';
import { acknowledge, iam4ManagedPolicy, iam5Resource } from './nag';
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export interface PipelineStackProps extends cdk.StackProps {
  lifecycleTable: dynamodb.ITable;
  configTable: dynamodb.ITable;
  stateTable: dynamodb.ITable;
  inventoryTable: dynamodb.ITable;
  actionPlanTable: dynamodb.ITable;
  /** Customer managed key of the five tables (DataStack.dataKey); also encrypts the schedule payload. */
  dataKey: kms.IKey;
  /** Optional sts:ExternalId presented when assuming spoke roles (#144). */
  spokeExternalId?: string;
}

const BACKEND_DIR = path.join(__dirname, '..', '..', 'backend');
const PIPELINE_FUNCTION_NAME = 'aws-services-lifecycle-pipeline';
const API_FUNCTION_NAME = 'aws-services-lifecycle-api';
// One place for the Lambda Python version: runtime of both functions, the
// Docker bundling image and the pip wheel target must agree.
const PYTHON_RUNTIME = lambda.Runtime.PYTHON_3_14;
const PYTHON_VERSION = '3.14';
// CloudWatch Logs retention of the Lambda log groups (and of the HTTP API
// access logs, api-stack.ts): one year, same default as Terraform log_retention_days.
const LOG_RETENTION = logs.RetentionDays.ONE_YEAR;
// EventBridge Scheduler schedules of this stack; the scheduler role may only
// decrypt payloads of schedules named with this prefix.
const SCHEDULE_NAME_PREFIX = 'aws-services-lifecycle-';

// The one model the extractor calls (backend/data_extractor.py, model_id).
// The Bedrock grant is scoped to it, so changing the model means changing it
// in both places; cdk/test checks they agree.
export const EXTRACTION_FOUNDATION_MODEL = 'amazon.nova-2-lite-v1:0';
export const EXTRACTION_INFERENCE_PROFILE = `global.${EXTRACTION_FOUNDATION_MODEL}`;

// Shared region/account helpers (shared/utils/aws_utils.py). The Lambda
// bundle cannot import from outside its own directory, so the file is copied
// into the staged backend at synth time: one source of truth, no local copy.
const SHARED_AWS_UTILS = path.join(__dirname, '..', '..', 'shared', 'utils', 'aws_utils.py');
const STAGE_DIR = path.join(__dirname, '..', '.backend-stage');

/**
 * Assemble the Lambda source tree: backend/*.py + requirements.txt plus the
 * shared aws_utils.py. Tests and caches are never staged.
 */
function stageBackend(): string {
  fs.rmSync(STAGE_DIR, { recursive: true, force: true });
  fs.mkdirSync(STAGE_DIR, { recursive: true });
  for (const file of fs.readdirSync(BACKEND_DIR)) {
    if (file.endsWith('.py') || file === 'requirements.txt') {
      fs.copyFileSync(path.join(BACKEND_DIR, file), path.join(STAGE_DIR, file));
    }
  }
  fs.copyFileSync(SHARED_AWS_UTILS, path.join(STAGE_DIR, 'aws_utils.py'));
  return STAGE_DIR;
}

/**
 * Bundle the Python code without Docker: pip resolves Linux/arm64 wheels for
 * the Lambda runtime (all dependencies are pure Python), then the staged
 * sources are copied alongside. Returning false lets CDK fall back to the
 * Docker bundling image.
 */
function bundleBackendLocally(stageDir: string, outputDir: string): boolean {
  const pipArgs = [
    '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
    '--platform', 'manylinux2014_aarch64', '--only-binary=:all:',
    '--python-version', PYTHON_VERSION, '--implementation', 'cp',
    '--target', outputDir,
    '-r', path.join(stageDir, 'requirements.txt'),
  ];
  let installed = false;
  for (const python of ['python', 'python3']) {
    const run = spawnSync(python, pipArgs, { stdio: ['ignore', 'pipe', 'pipe'] });
    if (run.status === 0) {
      installed = true;
      break;
    }
  }
  if (!installed) {
    return false;
  }
  for (const file of fs.readdirSync(stageDir)) {
    if (file.endsWith('.py')) {
      fs.copyFileSync(path.join(stageDir, file), path.join(outputDir, file));
    }
  }
  return true;
}

/**
 * Lifecycle refresh pipeline (issue #139).
 *
 * One code bundle, two Lambda functions:
 *  - pipeline: Lambda durable function running the whole refresh
 *    (extract -> scan -> reconcile -> notify) as one checkpointed execution.
 *    Invoked through the `live` alias (durable functions need a qualified ARN).
 *  - api: plain function behind API Gateway (see ApiStack) serving UI actions,
 *    starting/observing pipeline executions, and receiving the weekly
 *    schedule.
 * Replaces the Step Functions machine and the AgentCore runtime.
 */
export class PipelineStack extends cdk.Stack {
  public readonly pipelineFunction: lambda.Function;
  public readonly pipelineAlias: lambda.Alias;
  public readonly apiFunction: lambda.Function;
  public readonly notificationTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);

    this.notificationTopic = new sns.Topic(this, 'ExtractionNotifications', {
      topicName: 'aws-services-lifecycle-notifications',
      displayName: 'Service Lifecycle Tracker on AWS notifications',
      enforceSSL: true, // topic policy denying non-TLS publishers (boto3 always uses HTTPS)
      // Encrypted at rest with the AWS managed key for SNS: its key policy lets
      // principals of this account use it through SNS, which covers the two
      // publishers (the Lambda functions). AWS services that publish
      // themselves (CloudWatch alarms, EventBridge) would need a customer managed key.
      masterKey: kms.Alias.fromAliasName(this, 'SnsAwsManagedKey', 'alias/aws/sns'),
    });

    const deadLetterQueue = new sqs.Queue(this, 'SchedulerDLQ', {
      queueName: 'aws-services-lifecycle-scheduler-dlq',
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });
    acknowledge(deadLetterQueue, {
      id: 'AwsSolutions-SQS3',
      reason: 'This queue is the dead-letter queue of the EventBridge Scheduler target; a DLQ needs no DLQ of its own.',
    });

    // ------------------------------------------------------------------
    // Shared code bundle
    // ------------------------------------------------------------------
    const stageDir = stageBackend();
    const backendCode = lambda.Code.fromAsset(stageDir, {
      bundling: {
        image: PYTHON_RUNTIME.bundlingImage,
        platform: 'linux/arm64',
        command: [
          'bash', '-c',
          'pip install --quiet -r requirements.txt -t /asset-output && cp *.py /asset-output/',
        ],
        local: { tryBundle: (outputDir: string) => bundleBackendLocally(stageDir, outputDir) },
      },
    });

    const tableEnvironment = {
      LIFECYCLE_TABLE_NAME: props.lifecycleTable.tableName,
      CONFIG_TABLE_NAME: props.configTable.tableName,
      STATE_TABLE_NAME: props.stateTable.tableName,
      INVENTORY_TABLE_NAME: props.inventoryTable.tableName,
      ACTION_PLAN_TABLE_NAME: props.actionPlanTable.tableName,
      NOTIFICATION_TOPIC_ARN: this.notificationTopic.topicArn,
    };

    // ------------------------------------------------------------------
    // Shared data-plane permissions (same boundary as before, issue #116):
    // full access to backend-owned tables, read + UpdateItem only on the
    // repo-owned configuration table (no Put/Delete/BatchWrite).
    // Indexes are listed by name (the GSIs defined in data-stack.ts): a new
    // GSI must be added here too.
    // ------------------------------------------------------------------
    const indexArns = (table: dynamodb.ITable, indexes: Record<string, string>) =>
      Object.values(indexes).map((name) => `${table.tableArn}/index/${name}`);

    // Global cross-Region inference profile: Bedrock authorizes the profile in
    // this Region, the model in this Region and the Region-less global model
    // ARN; the two model grants only apply through this profile.
    // https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html
    const inferenceProfileArn =
      `arn:${this.partition}:bedrock:${this.region}:${this.account}:inference-profile/${EXTRACTION_INFERENCE_PROFILE}`;

    const dataAccessPolicy = new iam.ManagedPolicy(this, 'LifecycleDataAccess', {
      description: 'Lifecycle tracker Lambda access to DynamoDB, Bedrock, AWS Health and read-only discovery APIs',
      statements: [
        new iam.PolicyStatement({
          sid: 'DynamoDBAgentOwnedAccess',
          actions: [
            'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem',
            'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
          ],
          resources: [
            props.lifecycleTable.tableArn, ...indexArns(props.lifecycleTable, LIFECYCLE_TABLE_INDEXES),
            props.stateTable.tableArn,
            props.inventoryTable.tableArn,
            props.actionPlanTable.tableArn, ...indexArns(props.actionPlanTable, ACTION_PLAN_TABLE_INDEXES),
          ],
        }),
        new iam.PolicyStatement({
          sid: 'DynamoDBConfigReadAndUpdate',
          actions: ['dynamodb:GetItem', 'dynamodb:UpdateItem', 'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:BatchGetItem'],
          resources: [props.configTable.tableArn], // the configuration table has no index
        }),
        new iam.PolicyStatement({
          // The tables are encrypted with the customer managed data key; DynamoDB
          // uses it on behalf of the caller. Only through DynamoDB, never directly.
          sid: 'DynamoDBTableKey',
          actions: [
            'kms:Decrypt', 'kms:DescribeKey', 'kms:Encrypt', 'kms:ReEncryptFrom', 'kms:ReEncryptTo',
            'kms:GenerateDataKey', 'kms:GenerateDataKeyWithoutPlaintext',
          ],
          resources: [props.dataKey.keyArn],
          conditions: { StringEquals: { 'kms:ViaService': `dynamodb.${this.region}.amazonaws.com` } },
        }),
        new iam.PolicyStatement({
          sid: 'BedrockInferenceProfile',
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream', 'bedrock:Converse', 'bedrock:ConverseStream'],
          resources: [inferenceProfileArn],
        }),
        new iam.PolicyStatement({
          sid: 'BedrockModelThroughProfile',
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream', 'bedrock:Converse', 'bedrock:ConverseStream'],
          resources: [
            `arn:${this.partition}:bedrock:${this.region}::foundation-model/${EXTRACTION_FOUNDATION_MODEL}`,
            `arn:${this.partition}:bedrock:::foundation-model/${EXTRACTION_FOUNDATION_MODEL}`,
          ],
          conditions: { StringEquals: { 'bedrock:InferenceProfileArn': inferenceProfileArn } },
        }),
        new iam.PolicyStatement({
          // Scan-time cross-check: which inventory ARNs appear in open planned
          // lifecycle notices (#141). Needs Business/Enterprise Support at runtime.
          sid: 'HealthAPIAccess',
          actions: HEALTH_READ_ACTIONS,
          resources: ['*'], // Health API has no resource-level permissions
        }),
        new iam.PolicyStatement({
          // Extended Support cost exposure (#142): unit prices from the Price
          // List API, vCPUs per DB instance class from EC2.
          sid: 'ExtendedSupportPricing',
          actions: ['pricing:GetProducts', 'ec2:DescribeInstanceTypes'],
          resources: ['*'], // neither API supports resource-level permissions
        }),
        new iam.PolicyStatement({
          sid: 'AccountResourceDiscovery',
          actions: SCANNER_READ_ACTIONS,
          resources: ['*'], // List/Describe calls: read-only, no resource scoping available
        }),
        new iam.PolicyStatement({
          // Multi-account scan (#144): assume the read-only spoke role in member
          // accounts, and resolve account lists / names from Organizations
          // (works for the management account or a delegated administrator).
          sid: 'HubAndSpokeScan',
          actions: ['sts:AssumeRole'],
          resources: [`arn:${this.partition}:iam::*:role/${SPOKE_ROLE_NAME}`],
        }),
        new iam.PolicyStatement({
          sid: 'OrganizationsRead',
          actions: [
            'organizations:DescribeOrganization', 'organizations:ListAccounts', 'organizations:DescribeAccount',
            'organizations:ListRoots', 'organizations:ListOrganizationalUnitsForParent', 'organizations:ListParents',
            'organizations:DescribeOrganizationalUnit', 'organizations:ListAccountsForParent',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'PublishNotifications',
          actions: ['sns:Publish'],
          resources: [this.notificationTopic.topicArn],
        }),
      ],
    });
    acknowledge(dataAccessPolicy, {
      id: iam5Resource(this, '*'),
      reason: 'Read-only List/Describe/Get calls only (account scanners, AWS Health, Price List, ec2:DescribeInstanceTypes, Organizations reads). '
        + 'These APIs have no resource-level permissions or must enumerate every resource of the account, so they cannot be scoped to ARNs.',
    }, {
      id: iam5Resource(this, `arn:${this.partition}:iam::*:role/${SPOKE_ROLE_NAME}`),
      reason: 'Multi-account scan: the member accounts are discovered at run time from AWS Organizations, so the account id is a wildcard. '
        + 'Only the fixed-name read-only spoke role can be assumed, and each spoke role trusts only this hub pipeline role (optionally with an ExternalId).',
    });

    // ------------------------------------------------------------------
    // Durable pipeline function
    // ------------------------------------------------------------------
    const pipelineLogGroup = new logs.LogGroup(this, 'PipelineLogGroup', {
      logGroupName: `/aws/lambda/${PIPELINE_FUNCTION_NAME}`,
      retention: LOG_RETENTION,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Fixed role name so spoke trust policies can pin the exact principal.
    const pipelineRole = new iam.Role(this, 'PipelineRole', {
      roleName: HUB_PIPELINE_ROLE_NAME,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Lifecycle tracker pipeline role (hub): scans this account and assumes spoke roles',
    });

    this.pipelineFunction = new lambda.Function(this, 'PipelineFunction', {
      functionName: PIPELINE_FUNCTION_NAME,
      role: pipelineRole,
      description: 'Lifecycle refresh pipeline: extract -> scan -> reconcile -> notify (Lambda durable function)',
      runtime: PYTHON_RUNTIME,
      architecture: lambda.Architecture.ARM_64,
      handler: 'pipeline.handler',
      code: backendCode,
      memorySize: 1024,
      timeout: cdk.Duration.minutes(15),
      logGroup: pipelineLogGroup,
      environment: {
        ...tableEnvironment,
        SPOKE_ROLE_NAME,
        ...(props.spokeExternalId ? { SPOKE_EXTERNAL_ID: props.spokeExternalId } : {}),
      },
      durableConfig: {
        executionTimeout: cdk.Duration.hours(2),
        retentionPeriod: cdk.Duration.days(14),
      },
    });
    this.pipelineFunction.role!.addManagedPolicy(dataAccessPolicy);
    this.pipelineFunction.role!.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicDurableExecutionRolePolicy'),
    );
    acknowledge(pipelineRole, {
      id: iam4ManagedPolicy(this, 'service-role/AWSLambdaBasicDurableExecutionRolePolicy'),
      reason: 'AWS managed policy for Lambda durable functions: the checkpoint/state APIs of the function\'s own executions plus its CloudWatch Logs. '
        + 'AWS maintains it as the durable execution API evolves.',
    });

    // Durable functions must be invoked through a qualified ARN.
    this.pipelineAlias = new lambda.Alias(this, 'PipelineLiveAlias', {
      aliasName: 'live',
      version: this.pipelineFunction.currentVersion,
    });

    // ------------------------------------------------------------------
    // Plain API function (UI actions, pipeline control, weekly schedule)
    // ------------------------------------------------------------------
    const apiLogGroup = new logs.LogGroup(this, 'ApiLogGroup', {
      logGroupName: `/aws/lambda/${API_FUNCTION_NAME}`,
      retention: LOG_RETENTION,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Optional cap on the API function's concurrency:
    // --context apiReservedConcurrency=<n>. Off by default: reserving
    // concurrency fails on accounts whose Lambda concurrency quota is low (new
    // accounts can start at 10), and the HTTP API stage is already throttled.
    const apiReservedConcurrencyRaw = this.node.tryGetContext('apiReservedConcurrency');
    let apiReservedConcurrency: number | undefined;
    if (apiReservedConcurrencyRaw !== undefined && apiReservedConcurrencyRaw !== '') {
      apiReservedConcurrency = Number(apiReservedConcurrencyRaw);
      if (!Number.isInteger(apiReservedConcurrency) || apiReservedConcurrency < 1) {
        throw new Error(`--context apiReservedConcurrency must be a positive integer, got '${apiReservedConcurrencyRaw}'`);
      }
    }

    this.apiFunction = new lambda.Function(this, 'ApiFunction', {
      functionName: API_FUNCTION_NAME,
      description: 'Lifecycle tracker API: UI actions, refresh pipeline control, weekly schedule entry point',
      runtime: PYTHON_RUNTIME,
      architecture: lambda.Architecture.ARM_64,
      handler: 'api.handler',
      code: backendCode,
      memorySize: 512,
      timeout: cdk.Duration.minutes(5),
      reservedConcurrentExecutions: apiReservedConcurrency,
      logGroup: apiLogGroup,
      environment: {
        ...tableEnvironment,
        PIPELINE_FUNCTION_ARN: this.pipelineAlias.functionArn,
      },
    });
    this.apiFunction.role!.addManagedPolicy(dataAccessPolicy);
    this.apiFunction.addToRolePolicy(new iam.PolicyStatement({
      sid: 'PipelineControl',
      actions: [
        'lambda:InvokeFunction',
        'lambda:GetDurableExecution',
        'lambda:GetDurableExecutionHistory',
        'lambda:ListDurableExecutionsByFunction',
      ],
      resources: [this.pipelineFunction.functionArn, `${this.pipelineFunction.functionArn}:*`],
    }));
    this.apiFunction.addToRolePolicy(new iam.PolicyStatement({
      // Settings page: list the regions enabled for this account (dropdown of
      // regions to scan). API function only; the pipeline never needs it.
      sid: 'RegionListing',
      actions: ['ec2:DescribeRegions'],
      resources: ['*'], // DescribeRegions has no resource-level permissions
    }));
    acknowledge(this.apiFunction, {
      id: iam4ManagedPolicy(this, 'service-role/AWSLambdaBasicExecutionRole'),
      reason: 'AWS managed policy that only allows the function to write its own CloudWatch Logs (CDK default for a Lambda function role).',
    }, {
      id: iam5Resource(this, '*'),
      reason: 'ec2:DescribeRegions only (Settings page region list); the action has no resource-level permissions.',
    }, {
      id: iam5Resource(this, `${this.pipelineFunction.functionArn}:*`),
      reason: 'Durable executions are addressed through qualified ARNs (the live alias and the published version that ran them). '
        + 'Runs started on an earlier version must stay observable after a deploy moves the alias, so all versions of this one function are allowed.',
    });

    // ------------------------------------------------------------------
    // Schedules (EventBridge Scheduler -> API function)
    // Both go through the API function: the weekly run reuses the same
    // naming / adopt-running logic as a UI-triggered refresh.
    // ------------------------------------------------------------------
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
      description: 'EventBridge Scheduler role invoking the lifecycle tracker API function',
    });
    // The schedule targets the unqualified function ARN, so that ARN is all
    // the role needs (grantInvoke would also allow every version, `:*`).
    schedulerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [this.apiFunction.functionArn],
    }));
    deadLetterQueue.grantSendMessages(schedulerRole);
    // The schedule payload is encrypted with the data key: Scheduler decrypts it
    // with this role at each invocation, with the schedule ARN as encryption context.
    // https://docs.aws.amazon.com/scheduler/latest/UserGuide/encryption-rest.html
    schedulerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['kms:Decrypt'],
      resources: [props.dataKey.keyArn],
      conditions: {
        StringLike: {
          'kms:EncryptionContext:aws:scheduler:schedule:arn':
            `arn:${this.partition}:scheduler:${this.region}:${this.account}:schedule/default/${SCHEDULE_NAME_PREFIX}*`,
        },
      },
    }));

    const scheduleTarget = (payload: Record<string, string>): scheduler.CfnSchedule.TargetProperty => ({
      arn: this.apiFunction.functionArn,
      roleArn: schedulerRole.roleArn,
      input: JSON.stringify(payload),
      retryPolicy: { maximumEventAgeInSeconds: 3600, maximumRetryAttempts: 0 },
      deadLetterConfig: { arn: deadLetterQueue.queueArn },
    });

    const weeklySchedule = new scheduler.CfnSchedule(this, 'WeeklyRefreshSchedule', {
      name: `${SCHEDULE_NAME_PREFIX}weekly-refresh`,
      description: 'Weekly end-to-end lifecycle refresh (durable pipeline)',
      scheduleExpression: 'rate(7 days)',
      scheduleExpressionTimezone: 'UTC',
      flexibleTimeWindow: { mode: 'OFF' },
      kmsKeyArn: props.dataKey.keyArn,
      target: scheduleTarget({ action: 'start_refresh', refresh_origin: 'Auto' }),
    });

    // ------------------------------------------------------------------
    // Outputs
    // ------------------------------------------------------------------
    new cdk.CfnOutput(this, 'PipelineFunctionAliasArn', {
      value: this.pipelineAlias.functionArn,
      description: 'Qualified ARN of the durable refresh pipeline (invoke with --durable-execution-name)',
    });
    new cdk.CfnOutput(this, 'ApiFunctionArn', {
      value: this.apiFunction.functionArn,
      description: 'ARN of the API function (UI actions, pipeline control, weekly schedule)',
    });
    new cdk.CfnOutput(this, 'NotificationTopicArn', {
      value: this.notificationTopic.topicArn,
      description: 'SNS topic receiving refresh completion summaries',
    });
    new cdk.CfnOutput(this, 'WeeklyScheduleName', { value: weeklySchedule.name! });
    new cdk.CfnOutput(this, 'DeadLetterQueueUrl', { value: deadLetterQueue.queueUrl });
  }
}
