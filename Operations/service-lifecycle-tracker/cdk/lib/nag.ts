// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { IConstruct } from 'constructs';

/**
 * cdk-nag (AWS Solutions rules) support.
 *
 * Findings that are accepted for this sample are acknowledged next to the
 * construct they apply to, each with its reason, through CDK's native
 * `Validations.of(...).acknowledge()` (read by cdk-nag v3). Acknowledgements
 * only add construct metadata: the synthesized CloudFormation is unchanged.
 *
 * Granular rules (IAM4, IAM5) report one id per finding, e.g.
 * `AwsSolutions-IAM5[Resource::<MyFunction1234.Arn>:*]`, and cdk-nag matches
 * them by exact string. The helpers below render those ids the way cdk-nag
 * does, so they follow logical ids and partitions instead of hardcoding them.
 */

export interface NagAck {
  /** Rule id (`AwsSolutions-S1`) or rule id + finding (`AwsSolutions-IAM5[Resource::*]`). */
  readonly id: string;
  /** Why the finding is acceptable here. */
  readonly reason: string;
}

/** Acknowledge cdk-nag findings on `scope` and everything below it. */
export function acknowledge(scope: IConstruct, ...acks: NagAck[]): void {
  cdk.Validations.of(scope).acknowledge(...acks);
}

/** Same rendering as cdk-nag's flattenCfnReference (utils/flatten-cfn-reference). */
function flatten(node: unknown): string {
  if (node === undefined) return '';
  if (typeof node === 'string') return node.replace(/\${/g, '<').replace(/}/g, '>');
  const o = node as Record<string, any>;
  if (o['Fn::Join']) {
    const [delimiter, items] = o['Fn::Join'];
    return (items as unknown[]).map(flatten).join(delimiter);
  }
  if (o['Fn::Sub']) return flatten(o['Fn::Sub']);
  if (o['Fn::GetAtt']) {
    const [resource, attribute] = o['Fn::GetAtt'];
    return `<${flatten(resource)}.${flatten(attribute)}>`;
  }
  if (o['Fn::ImportValue']) return flatten(o['Fn::ImportValue']);
  if (o.Ref) return `<${flatten(o.Ref)}>`;
  return JSON.stringify(node);
}

/** IAM5 finding id for a wildcard resource, e.g. `${fn.functionArn}:*`. */
export function iam5Resource(scope: IConstruct, resource: string): string {
  return `AwsSolutions-IAM5[Resource::${flatten(cdk.Stack.of(scope).resolve(resource))}]`;
}

/** IAM4 finding id for an AWS managed policy attached to a role. */
export function iam4ManagedPolicy(scope: IConstruct, managedPolicyName: string): string {
  const arn = iam.ManagedPolicy.fromAwsManagedPolicyName(managedPolicyName).managedPolicyArn;
  return `AwsSolutions-IAM4[Policy::${flatten(cdk.Stack.of(scope).resolve(arn))}]`;
}

/**
 * Every IAM5 finding id of a policy document (wildcard actions and resources
 * of Allow statements), computed like cdk-nag's IAMNoWildcardPermissions.
 * Only used for policies of constructs that CDK itself owns and grants, such
 * as the BucketDeployment handler, whose statements this project does not write.
 */
export function iam5FindingsOf(scope: IConstruct, document: iam.PolicyDocument): string[] {
  const resolved = cdk.Stack.of(scope).resolve(document) ?? {};
  const ids = new Set<string>();
  const asArray = (v: unknown): unknown[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);
  for (const statement of resolved.Statement ?? []) {
    if (statement.Effect !== 'Allow') continue;
    for (const action of asArray(statement.Action) as string[]) {
      if (action.includes('*')) ids.add(`AwsSolutions-IAM5[Action::${action}]`);
    }
    for (const resource of asArray(statement.Resource)) {
      const flat = flatten(resource);
      if (flat.includes('*')) ids.add(`AwsSolutions-IAM5[Resource::${flat}]`);
    }
  }
  return [...ids];
}
