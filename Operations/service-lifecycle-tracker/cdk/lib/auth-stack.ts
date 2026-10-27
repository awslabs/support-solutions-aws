// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import { acknowledge } from './nag';

/**
 * Cognito user pool for the admin SPA.
 *
 * Since issue #139 the browser only needs the ID token: every AWS call goes
 * through the HTTP API's JWT authorizer, so there is no identity pool and no
 * IAM role for authenticated users anymore.
 */
export class AuthStack extends cdk.Stack {
    public readonly userPool: cognito.UserPool;
    public readonly userPoolClient: cognito.UserPoolClient;

    constructor(scope: Construct, id: string, props?: cdk.StackProps) {
        super(scope, id, props);

        // Cognito User Pool - ADMIN ONLY (no self-signup)
        this.userPool = new cognito.UserPool(this, 'LifecycleTrackerUserPool', {
            userPoolName: 'aws-services-lifecycle-tracker-admin-users',
            selfSignUpEnabled: false, // DISABLED - Admin only
            signInAliases: {
                username: true,
                email: true,
            },
            autoVerify: {
                email: true,
            },
            standardAttributes: {
                email: {
                    required: true,
                    mutable: false,
                },
            },
            passwordPolicy: {
                minLength: 8,
                requireLowercase: true,
                requireUppercase: true,
                requireDigits: true,
                // Applies to passwords set from now on; existing passwords keep working.
                requireSymbols: true,
            },
            accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
            removalPolicy: cdk.RemovalPolicy.DESTROY, // For dev - change to RETAIN for prod
        });
        acknowledge(this.userPool, {
            id: 'AwsSolutions-COG8',
            reason: 'The Plus feature plan (threat protection) is billed per monthly active user. This sample keeps the Essentials plan: '
                + 'the pool is admin-only (self sign-up disabled, users created by an administrator) and only issues ID tokens to the SPA.',
        });

        // User Pool Client for frontend JWT authentication
        this.userPoolClient = new cognito.UserPoolClient(this, 'LifecycleTrackerUserPoolClient', {
            userPool: this.userPool,
            userPoolClientName: 'aws-services-lifecycle-tracker-web-client',
            authFlows: {
                userPassword: true,
                userSrp: true,
            },
            generateSecret: false, // Public client (frontend)
            preventUserExistenceErrors: true,
        });

        // Note: Admin user will be created manually after deployment
        // Use AWS CLI: aws cognito-idp admin-create-user --user-pool-id <POOL_ID> --username admin --user-attributes Name=email,Value=admin@company.com Name=email_verified,Value=true --message-action SUPPRESS

        // Outputs
        new cdk.CfnOutput(this, 'UserPoolId', {
            value: this.userPool.userPoolId,
            description: 'Cognito User Pool ID',
            exportName: 'AWSServicesLifecycleTrackerUserPoolId',
        });

        new cdk.CfnOutput(this, 'UserPoolArn', {
            value: this.userPool.userPoolArn,
            description: 'Cognito User Pool ARN',
            exportName: 'AWSServicesLifecycleTrackerUserPoolArn',
        });

        new cdk.CfnOutput(this, 'UserPoolClientId', {
            value: this.userPoolClient.userPoolClientId,
            description: 'Cognito User Pool Client ID',
            exportName: 'AWSServicesLifecycleTrackerUserPoolClientId',
        });

        new cdk.CfnOutput(this, 'AdminUsername', {
            value: 'admin',
            description: 'Admin username (password must be set manually)',
        });
    }
}
