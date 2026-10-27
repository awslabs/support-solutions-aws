// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AwsSolutionsChecks } from 'cdk-nag';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { PipelineStack, EXTRACTION_INFERENCE_PROFILE } from '../lib/pipeline-stack';
import { ApiStack } from '../lib/api-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { SpokeStack } from '../lib/spoke-stack';
import { OrgStack } from '../lib/org-stack';

// Same wiring as bin/app.ts, with pip bundling skipped.
function buildApp(context: Record<string, string> = {}) {
  // FrontendStack uploads ../frontend/dist; the deploy scripts create this
  // placeholder before the first synth too (the folder is gitignored).
  const dist = path.join(__dirname, '..', '..', 'frontend', 'dist');
  if (!fs.existsSync(path.join(dist, 'index.html'))) {
    fs.mkdirSync(dist, { recursive: true });
    fs.writeFileSync(path.join(dist, 'index.html'), '<!DOCTYPE html><html><body><h1>Building...</h1></body></html>');
  }
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [], ...context } });
  const env = { account: '111122223333', region: 'us-east-1' };
  const data = new DataStack(app, 'NagData', { env });
  const auth = new AuthStack(app, 'NagAuth', { env });
  const pipeline = new PipelineStack(app, 'NagPipeline', {
    env, lifecycleTable: data.lifecycleTable, configTable: data.configTable, stateTable: data.stateTable,
    inventoryTable: data.inventoryTable, actionPlanTable: data.actionPlanTable, dataKey: data.dataKey,
  });
  const api = new ApiStack(app, 'NagApi', {
    env, apiFunction: pipeline.apiFunction, userPool: auth.userPool, userPoolClient: auth.userPoolClient,
  });
  const frontend = new FrontendStack(app, 'NagFrontend', {
    env, userPoolId: auth.userPool.userPoolId, userPoolClientId: auth.userPoolClient.userPoolClientId,
    apiUrl: api.apiUrl, region: env.region,
  });
  new SpokeStack(app, 'NagSpoke', { env, hubAccountId: env.account, synthesizer: new cdk.BootstraplessSynthesizer() });
  new OrgStack(app, 'NagOrg', {
    env, hubAccountId: env.account, targetOuIds: ['r-abcd'], synthesizer: new cdk.BootstraplessSynthesizer(),
  });
  return { app, data, pipeline, api, auth, frontend };
}

describe('cdk-nag AwsSolutions', () => {
  test('no unacknowledged error-level findings in any stack', () => {
    const { app } = buildApp();
    const report = new AwsSolutionsChecks(app).validateScope(app);
    const errors = report.violations
      .filter((v) => v.severity === 'error')
      .flatMap((v) => v.violatingResources.map((r) => `${v.ruleName} @ ${r.constructPath}`));
    expect(errors).toEqual([]);
  });

  test('still none with CloudFront access logging turned off (opt-in Regions)', () => {
    const { app } = buildApp({ cloudFrontLogging: 'false' });
    const report = new AwsSolutionsChecks(app).validateScope(app);
    expect(report.violations.filter((v) => v.severity === 'error')).toEqual([]);
  });
});

describe('security settings behind the cdk-nag fixes', () => {
  const { data, pipeline, api, auth, frontend } = buildApp();
  const pipelineTemplate = Template.fromStack(pipeline);
  const dataTemplate = Template.fromStack(data);
  const frontendTemplate = Template.fromStack(frontend);
  const importedKey = { 'Fn::ImportValue': Match.stringLikeRegexp('DataKey') };

  test('all five tables are encrypted with the rotating customer managed data key, which is retained', () => {
    dataTemplate.hasResource('AWS::KMS::Key', { Properties: { EnableKeyRotation: true }, DeletionPolicy: 'Retain' });
    const tables = dataTemplate.findResources('AWS::DynamoDB::Table');
    expect(Object.keys(tables)).toHaveLength(5);
    const [keyId] = Object.keys(dataTemplate.findResources('AWS::KMS::Key'));
    for (const table of Object.values(tables)) {
      expect(table.Properties.SSESpecification).toEqual({ SSEEnabled: true, SSEType: 'KMS', KMSMasterKeyId: { 'Fn::GetAtt': [keyId, 'Arn'] } });
    }
  });

  test('Lambda functions may use the data key only through DynamoDB', () => {
    pipelineTemplate.hasResourceProperties('AWS::IAM::ManagedPolicy', {
      PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
        Sid: 'DynamoDBTableKey', Resource: importedKey,
        Condition: { StringEquals: { 'kms:ViaService': 'dynamodb.us-east-1.amazonaws.com' } },
      })]) },
    });
  });

  test('schedule payload is encrypted with the data key, decryptable only for this stack\'s schedules', () => {
    pipelineTemplate.hasResourceProperties('AWS::Scheduler::Schedule', { KmsKeyArn: importedKey });
    pipelineTemplate.hasResourceProperties('AWS::IAM::Policy', {
      Roles: [{ Ref: Match.stringLikeRegexp('^SchedulerRole') }],
      PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
        Action: 'kms:Decrypt', Resource: importedKey,
        Condition: { StringLike: { 'kms:EncryptionContext:aws:scheduler:schedule:arn': {
          'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, ':scheduler:us-east-1:111122223333:schedule/default/aws-services-lifecycle-*']],
        } } },
      })]) },
    });
    for (const schedule of Object.values(pipelineTemplate.findResources('AWS::Scheduler::Schedule'))) {
      expect(schedule.Properties.Name).toMatch(/^aws-services-lifecycle-/);
    }
  });

  test('notification topic is encrypted at rest with the AWS managed SNS key', () => {
    pipelineTemplate.hasResourceProperties('AWS::SNS::Topic', {
      KmsMasterKeyId: { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, ':kms:us-east-1:111122223333:alias/aws/sns']] },
    });
  });

  test('CloudFront writes access logs to a private, TLS-only bucket that expires them', () => {
    frontendTemplate.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({ Logging: {
        Bucket: { 'Fn::GetAtt': [Match.stringLikeRegexp('^AccessLogsBucket'), 'RegionalDomainName'] }, Prefix: 'cloudfront/',
      } }),
    });
    frontendTemplate.hasResourceProperties('AWS::S3::Bucket', {
      OwnershipControls: { Rules: [{ ObjectOwnership: 'BucketOwnerPreferred' }] },
      PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
      LifecycleConfiguration: { Rules: [Match.objectLike({ ExpirationInDays: 30, Status: 'Enabled' })] },
    });
    const off = Template.fromStack(buildApp({ cloudFrontLogging: 'false' }).frontend);
    off.resourceCountIs('AWS::S3::Bucket', 1);
    off.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({ Logging: Match.absent() }),
    });
  });

  test('Bedrock access is limited to the extraction model, used through its global inference profile', () => {
    const extractor = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'data_extractor.py'), 'utf-8');
    expect(extractor).toContain(`model_id = "${EXTRACTION_INFERENCE_PROFILE}"`);
    const policy = JSON.stringify(Object.values(pipelineTemplate.findResources('AWS::IAM::ManagedPolicy'))[0]);
    expect(policy).not.toContain('foundation-model/*');
    expect(policy).not.toContain('inference-profile/*');
    expect(policy).toContain('bedrock:::foundation-model/amazon.nova-2-lite-v1:0');
  });

  test('DynamoDB grants name the existing indexes instead of index/*', () => {
    const policy = JSON.stringify(Object.values(pipelineTemplate.findResources('AWS::IAM::ManagedPolicy'))[0]);
    expect(policy).not.toContain('/index/*');
    for (const index of ['status-index', 'extraction-date-index', 'owner-index', 'plan-status-index']) {
      expect(policy).toContain(`/index/${index}`);
    }
  });

  test('notification topic denies publishers that do not use TLS', () => {
    pipelineTemplate.hasResourceProperties('AWS::SNS::TopicPolicy', {
      PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
        Effect: 'Deny', Action: 'sns:Publish', Condition: { Bool: { 'aws:SecureTransport': 'false' } },
      })]) },
    });
  });

  test('scheduler role may invoke the API function, not its versions', () => {
    pipelineTemplate.hasResourceProperties('AWS::IAM::Policy', {
      Roles: [{ Ref: Match.stringLikeRegexp('^SchedulerRole') }],
      PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
        Action: 'lambda:InvokeFunction', Resource: { 'Fn::GetAtt': [Match.stringLikeRegexp('^ApiFunction'), 'Arn'] },
      })]) },
    });
  });

  test('HTTP API $default stage writes access logs', () => {
    Template.fromStack(api).hasResourceProperties('AWS::ApiGatewayV2::Stage', {
      StageName: '$default',
      AccessLogSettings: { DestinationArn: Match.anyValue(), Format: Match.stringLikeRegexp('\\$context.requestId') },
    });
  });

  test('Lambda and HTTP API log groups keep logs for one year', () => {
    const groups = [pipelineTemplate, Template.fromStack(api)]
      .flatMap((t) => Object.values(t.findResources('AWS::Logs::LogGroup')));
    expect(groups).toHaveLength(3);
    for (const group of groups) {
      expect(group.Properties.RetentionInDays).toBe(365);
    }
  });

  test('user pool requires symbols in new passwords', () => {
    Template.fromStack(auth).hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: { PasswordPolicy: Match.objectLike({ MinimumLength: 8, RequireSymbols: true }) },
    });
  });
});
