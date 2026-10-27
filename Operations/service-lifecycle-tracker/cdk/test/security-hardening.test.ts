// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AuthStack } from '../lib/auth-stack';
import { DataStack } from '../lib/data-stack';
import { PipelineStack } from '../lib/pipeline-stack';
import { ApiStack, DEFAULT_API_THROTTLE_BURST, DEFAULT_API_THROTTLE_RATE } from '../lib/api-stack';
import { FrontendStack, contentSecurityPolicy } from '../lib/frontend-stack';

// Security headers, the user in the API access log, throttling and the
// opt-in reserved concurrency.
function build(context: Record<string, string> = {}) {
  const dist = path.join(__dirname, '..', '..', 'frontend', 'dist');
  if (!fs.existsSync(path.join(dist, 'index.html'))) {
    fs.mkdirSync(dist, { recursive: true });
    fs.writeFileSync(path.join(dist, 'index.html'), '<!DOCTYPE html><html><body><h1>Building...</h1></body></html>');
  }
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [], ...context } });
  const env = { account: '111122223333', region: 'us-east-1' };
  const data = new DataStack(app, 'HData', { env });
  const auth = new AuthStack(app, 'HAuth', { env });
  const pipeline = new PipelineStack(app, 'HPipeline', {
    env, lifecycleTable: data.lifecycleTable, configTable: data.configTable, stateTable: data.stateTable,
    inventoryTable: data.inventoryTable, actionPlanTable: data.actionPlanTable, dataKey: data.dataKey,
  });
  const api = new ApiStack(app, 'HApi', {
    env, apiFunction: pipeline.apiFunction, userPool: auth.userPool, userPoolClient: auth.userPoolClient,
  });
  const frontend = new FrontendStack(app, 'HFrontend', {
    env, userPoolId: auth.userPool.userPoolId, userPoolClientId: auth.userPoolClient.userPoolClientId,
    apiUrl: api.apiUrl, region: env.region,
  });
  return {
    api: Template.fromStack(api), frontend: Template.fromStack(frontend), pipeline: Template.fromStack(pipeline),
  };
}

describe('security response headers', () => {
  const { frontend } = build();

  test('the distribution sends the security headers on every response', () => {
    frontend.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: {
          ContentSecurityPolicy: { ContentSecurityPolicy: Match.anyValue(), Override: true },
          StrictTransportSecurity: { AccessControlMaxAgeSec: 31536000, IncludeSubdomains: true, Preload: true, Override: true },
          ContentTypeOptions: { Override: true },
          FrameOptions: { FrameOption: 'DENY', Override: true },
          ReferrerPolicy: { ReferrerPolicy: 'strict-origin-when-cross-origin', Override: true },
          XSSProtection: { Protection: true, ModeBlock: true, Override: true },
        },
      }),
    });
    const policyId = Object.keys(frontend.findResources('AWS::CloudFront::ResponseHeadersPolicy'))[0];
    frontend.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({ ResponseHeadersPolicyId: { Ref: policyId } }),
      }),
    });
  });

  test('the CSP allows only the site, the HTTP API and the Cognito endpoint', () => {
    const policy = Object.values(frontend.findResources('AWS::CloudFront::ResponseHeadersPolicy'))[0];
    const csp = JSON.stringify(policy.Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig.ContentSecurityPolicy);
    for (const directive of ["default-src 'none'", "script-src 'self'", "frame-ancestors 'none'", "object-src 'none'"]) {
      expect(csp).toContain(directive);
    }
    expect(csp).toContain('https://cognito-idp.us-east-1.');
    expect(csp).toContain('Fn::ImportValue'); // the HTTP API URL, from the Api stack
    expect(csp).not.toContain('unsafe-');
  });

  test('Terraform builds the same CSP', () => {
    const tf = fs.readFileSync(path.join(__dirname, '..', '..', 'terraform', 'frontend.tf'), 'utf8');
    const block = tf.split('content_security_policy = join("; ", [')[1].split('])')[0];
    const tfDirectives = [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1])
      .map((d) => d.replace('${aws_apigatewayv2_api.http.api_endpoint}', 'API')
        .replace('https://cognito-idp.${var.region}.${data.aws_partition.current.dns_suffix}', 'COGNITO'));
    expect(tfDirectives.join('; ')).toEqual(contentSecurityPolicy('API', 'COGNITO'));
  });
});

describe('HTTP API stage', () => {
  test('stage-wide throttling and the Cognito sub in the access log', () => {
    const { api } = build();
    api.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
      StageName: '$default',
      DefaultRouteSettings: { ThrottlingRateLimit: DEFAULT_API_THROTTLE_RATE, ThrottlingBurstLimit: DEFAULT_API_THROTTLE_BURST },
      AccessLogSettings: { DestinationArn: Match.anyValue(), Format: Match.stringLikeRegexp('\\$context.authorizer.claims.sub') },
    });
  });

  test('throttling can be changed with context, and bad values are refused', () => {
    const { api } = build({ apiThrottleRate: '5', apiThrottleBurst: '10' });
    api.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
      DefaultRouteSettings: { ThrottlingRateLimit: 5, ThrottlingBurstLimit: 10 },
    });
    expect(() => build({ apiThrottleRate: '0' })).toThrow(/apiThrottleRate/);
  });
});

describe('API function reserved concurrency', () => {
  const apiFunction = (t: Template) => Object.values(t.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: 'aws-services-lifecycle-api' },
  }))[0];

  test('not reserved by default (accounts with a low concurrency quota keep deploying)', () => {
    expect(apiFunction(build().pipeline).Properties.ReservedConcurrentExecutions).toBeUndefined();
  });

  test('reserved when asked for', () => {
    expect(apiFunction(build({ apiReservedConcurrency: '10' }).pipeline).Properties.ReservedConcurrentExecutions).toBe(10);
    expect(() => build({ apiReservedConcurrency: '2.5' })).toThrow(/apiReservedConcurrency/);
  });
});
