// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpUserPoolAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface ApiStackProps extends cdk.StackProps {
  apiFunction: lambda.IFunction;
  userPool: cognito.IUserPool;
  userPoolClient: cognito.IUserPoolClient;
}

// Stage-wide throttling, across all callers. A dashboard
// load sends a handful of requests at once and a running refresh polls every
// 5 s, so these leave ample headroom for several users while bounding what a
// looping client can cost. Override with --context apiThrottleRate=<req/s>
// and --context apiThrottleBurst=<requests>.
export const DEFAULT_API_THROTTLE_RATE = 20;
export const DEFAULT_API_THROTTLE_BURST = 40;

function positiveNumberContext(scope: Construct, key: string, fallback: number): number {
  const raw = scope.node.tryGetContext(key);
  if (raw === undefined || raw === null || raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`--context ${key} must be a positive number, got '${raw}'`);
  }
  return value;
}

/**
 * HTTP API in front of the API Lambda (issue #139).
 *
 * The Cognito user-pool JWT authorizer validates the ID token the SPA sends
 * in the Authorization header; the browser itself holds no AWS credentials.
 *
 *   POST /actions          router actions (list_services, update_service, ...)
 *   POST /refresh          start (or adopt) a durable pipeline execution
 *   GET  /refresh/{arn}    execution status for UI polling / re-attach
 */
export class ApiStack extends cdk.Stack {
  public readonly httpApi: apigwv2.HttpApi;
  public readonly apiUrl: string;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const authorizer = new HttpUserPoolAuthorizer('CognitoAuthorizer', props.userPool, {
      userPoolClients: [props.userPoolClient],
    });

    this.httpApi = new apigwv2.HttpApi(this, 'LifecycleTrackerHttpApi', {
      apiName: 'aws-services-lifecycle-tracker-api',
      description: 'Service Lifecycle Tracker on AWS - UI API (Cognito JWT)',
      defaultAuthorizer: authorizer,
      corsPreflight: {
        // The CloudFront domain is only known after the frontend deploys,
        // which itself needs this API URL at build time; auth is enforced
        // by the JWT authorizer, not by the origin allow-list.
        allowOrigins: ['*'],
        allowMethods: [apigwv2.CorsHttpMethod.GET, apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
        allowHeaders: ['Content-Type', 'Authorization'],
        maxAge: cdk.Duration.hours(1),
      },
    });

    const integration = new HttpLambdaIntegration('ApiFunctionIntegration', props.apiFunction);

    // Access logs for the $default stage. HttpApi does not expose access-log
    // settings for the stage it creates, so they are set on that stage's L1
    // (an in-place update of the existing stage). No request bodies, headers
    // or tokens are logged. The /aws/vendedlogs/ prefix lets CloudWatch Logs
    // cover the delivery with one resource policy instead of one per group.
    const accessLogGroup = new logs.LogGroup(this, 'ApiAccessLogs', {
      logGroupName: '/aws/vendedlogs/apigateway/aws-services-lifecycle-tracker-api',
      retention: logs.RetentionDays.ONE_YEAR, // same as the Lambda log groups and Terraform log_retention_days
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const defaultStage = this.httpApi.defaultStage!.node.defaultChild as apigwv2.CfnStage;
    defaultStage.defaultRouteSettings = {
      throttlingRateLimit: positiveNumberContext(this, 'apiThrottleRate', DEFAULT_API_THROTTLE_RATE),
      throttlingBurstLimit: Math.round(positiveNumberContext(this, 'apiThrottleBurst', DEFAULT_API_THROTTLE_BURST)),
    };
    defaultStage.accessLogSettings = {
      destinationArn: accessLogGroup.logGroupArn,
      format: JSON.stringify({
        requestId: '$context.requestId',
        ip: '$context.identity.sourceIp',
        requestTime: '$context.requestTime',
        httpMethod: '$context.httpMethod',
        routeKey: '$context.routeKey',
        status: '$context.status',
        protocol: '$context.protocol',
        responseLength: '$context.responseLength',
        integrationLatency: '$context.integrationLatency',
        authorizerError: '$context.authorizer.error',
        // Cognito user (sub claim) of authenticated requests: who did what
        user: '$context.authorizer.claims.sub',
        integrationError: '$context.integrationErrorMessage',
      }),
    };

    this.httpApi.addRoutes({ path: '/actions', methods: [apigwv2.HttpMethod.POST], integration });
    this.httpApi.addRoutes({ path: '/refresh', methods: [apigwv2.HttpMethod.POST], integration });
    // Greedy: durable execution ARNs contain '/' segments
    this.httpApi.addRoutes({ path: '/refresh/{arn+}', methods: [apigwv2.HttpMethod.GET], integration });

    // apiEndpoint has no trailing slash
    this.apiUrl = this.httpApi.apiEndpoint;

    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.apiUrl,
      description: 'HTTP API base URL used by the frontend (VITE_API_URL)',
    });
    new cdk.CfnOutput(this, 'ApiId', {
      value: this.httpApi.apiId,
      description: 'HTTP API ID',
    });
  }
}
