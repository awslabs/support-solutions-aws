// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { acknowledge, iam4ManagedPolicy, iam5FindingsOf } from './nag';

interface FrontendStackProps extends cdk.StackProps {
  userPoolId: string;
  userPoolClientId: string;
  apiUrl: string;
  region: string;
}

/**
 * Content-Security-Policy of the SPA. terraform/frontend.tf
 * builds the same string; a test compares the two.
 */
export function contentSecurityPolicy(apiUrl: string, cognitoEndpoint: string): string {
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    `connect-src 'self' ${apiUrl} ${cognitoEndpoint}`,
    "manifest-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    'upgrade-insecure-requests',
  ].join('; ');
}

/**
 * Static SPA hosting (S3 + CloudFront). The build in ../frontend/dist must be
 * produced beforehand with the API URL and Cognito IDs baked in
 * (scripts/build-frontend.*).
 */
export class FrontendStack extends cdk.Stack {
  public readonly distributionUrl: string;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const websiteBucket = new s3.Bucket(this, 'WebsiteBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      // Bucket policy denying non-TLS requests. CloudFront reads through OAC
      // over HTTPS (viewers are redirected to HTTPS), deployments use the SDK.
      enforceSSL: true,
    });
    acknowledge(websiteBucket, {
      id: 'AwsSolutions-S1',
      reason: 'Static SPA assets only, readable solely by this CloudFront distribution (OAC, Block Public Access on). '
        + 'Every viewer request is recorded in the CloudFront access logs; S3 server access logs would only add the CloudFront origin fetches. '
        + 'Data access is through the authenticated API, which is access-logged.',
    });

    // CloudFront standard logs (one file per few minutes of viewer requests).
    // On by default; `--context cloudFrontLogging=false` turns it off, which is
    // needed in opt-in Regions: CloudFront cannot deliver these logs to a bucket there.
    const accessLogging = !['false', '0'].includes(String(this.node.tryGetContext('cloudFrontLogging') ?? 'true'));
    let accessLogsBucket: s3.Bucket | undefined;
    if (accessLogging) {
      accessLogsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED, // CloudFront log delivery supports SSE-S3, not the AWS managed KMS key
        enforceSSL: true,
        // CloudFront standard logging writes through the bucket ACL (awslogsdelivery
        // account), so ACLs stay enabled; the bucket owner owns the log objects.
        objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
        lifecycleRules: [{ expiration: cdk.Duration.days(30), abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) }],
      });
      acknowledge(accessLogsBucket, {
        id: 'AwsSolutions-S1',
        reason: 'This bucket is the access-log destination itself; logging it into another bucket would only log the log delivery.',
      });
    }

    const originAccessControl = new cloudfront.S3OriginAccessControl(this, 'OAC', {
      signing: cloudfront.Signing.SIGV4_NO_OVERRIDE,
    });

    // Security headers on every response. The SPA only
    // loads its own script and stylesheet (fonts are data: URIs in that
    // stylesheet) and only talks to the HTTP API and the Cognito user pool
    // endpoint; it cannot be framed.
    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      comment: 'Service Lifecycle Tracker on AWS - security headers for the SPA',
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          contentSecurityPolicy: contentSecurityPolicy(props.apiUrl, `https://cognito-idp.${this.region}.${this.urlSuffix}`),
          override: true,
        },
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, preload: true, override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
        xssProtection: { protection: true, modeBlock: true, override: true },
      },
    });

    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'Service Lifecycle Tracker on AWS - Frontend Distribution',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(websiteBucket, {
          originAccessControl,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy,
      },
      defaultRootObject: 'index.html',
      ...(accessLogsBucket ? { enableLogging: true, logBucket: accessLogsBucket, logFilePrefix: 'cloudfront/' } : {}),
      errorResponses: [
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
        },
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
        },
      ],
    });

    if (!accessLogging) {
      acknowledge(distribution, {
        id: 'AwsSolutions-CFR3',
        reason: 'Access logging turned off with --context cloudFrontLogging=false (opt-in Regions cannot receive CloudFront standard logs). '
          + 'The distribution only serves static SPA assets; data access goes through the Cognito-authenticated HTTP API, which has access logging enabled.',
      });
    }
    acknowledge(distribution, {
      id: 'AwsSolutions-CFR4',
      reason: 'The sample uses the default *.cloudfront.net certificate, for which CloudFront does not allow a minimum TLS version. '
        + 'Deployers adding a custom domain and ACM certificate should set minimumProtocolVersion TLSv1.2_2021.',
    });

    // Grant CloudFront OAC access to S3 bucket
    websiteBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [websiteBucket.arnForObjects('*')],
        principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
        conditions: {
          StringEquals: {
            'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
          },
        },
      })
    );

    const deployment = new s3deploy.BucketDeployment(this, 'DeployWebsite', {
      sources: [s3deploy.Source.asset('../frontend/dist')],
      destinationBucket: websiteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // cdk-nag: the deployment handler is a CDK-managed singleton Lambda
    // (runtime, managed policy and grants are chosen by aws-cdk-lib).
    const handler = this.node.children.find((c) => c.node.id.startsWith('Custom::CDKBucketDeployment'));
    const handlerPolicy = deployment.handlerRole.node.tryFindChild('DefaultPolicy') as iam.Policy | undefined;
    if (handler && handlerPolicy) {
      acknowledge(handler, {
        id: 'AwsSolutions-L1',
        reason: 'CDK-managed BucketDeployment handler; its Python runtime is set by aws-cdk-lib and moves forward with CDK upgrades.',
      }, {
        id: iam4ManagedPolicy(this, 'service-role/AWSLambdaBasicExecutionRole'),
        reason: 'AWS managed policy that only allows the CDK deployment handler to write its own CloudWatch Logs.',
      }, ...iam5FindingsOf(this, handlerPolicy.document).map((id) => ({
        id,
        reason: 'Grants generated by CDK BucketDeployment: read the CDK asset objects, write/delete objects in this website bucket only, '
          + 'and create CloudFront invalidations (no resource-level permissions) after the upload.',
      })));
    }

    this.distributionUrl = `https://${distribution.distributionDomainName}`;

    new cdk.CfnOutput(this, 'WebsiteUrl', {
      value: this.distributionUrl,
      description: 'CloudFront Distribution URL',
    });

    new cdk.CfnOutput(this, 'BucketName', {
      value: websiteBucket.bucketName,
      description: 'S3 Bucket Name',
    });

    if (accessLogsBucket) {
      new cdk.CfnOutput(this, 'AccessLogsBucketName', {
        value: accessLogsBucket.bucketName,
        description: 'S3 bucket receiving the CloudFront access logs (prefix cloudfront/, kept 30 days)',
      });
    }

    new cdk.CfnOutput(this, 'UserPoolId', {
      value: props.userPoolId,
      description: 'Cognito User Pool ID',
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: props.userPoolClientId,
      description: 'Cognito User Pool Client ID',
    });

    new cdk.CfnOutput(this, 'ApiUrl', {
      value: props.apiUrl,
      description: 'HTTP API base URL the frontend talks to',
    });

    new cdk.CfnOutput(this, 'Region', {
      value: props.region,
      description: 'AWS Region',
    });
  }
}
