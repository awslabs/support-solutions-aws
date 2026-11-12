#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# Tear down a GPU Capacity Planning Agent deployment.
#
# Usage:
#   ./deployment/cleanup.sh [STACK_NAME]
#
# STACK_NAME defaults to the environment variable STACK_NAME if set,
# otherwise gpu-capacity-agent. When invoked without a name in the SLIB
# validation environment, the caller is expected to export
# STACK_NAME to the name deploy.sh created.
#
# What this script does:
#   1. Empties the CloudFormation-managed S3 buckets (SAM's sam-cli-
#      artifacts bucket is NOT touched; only stack-owned buckets)
#   2. Deletes the CloudFormation stack and waits for DELETE_COMPLETE
#   3. Reports any resources that could not be deleted (usually only
#      the log groups, which have a retention setting and CFN leaves
#      alone by design)
#
# The account-wide API Gateway CloudWatch Logs role (created by
# deploy.sh) is deliberately NOT removed — it's account-wide, other
# API Gateways in the account may rely on it, and it's idempotent
# to re-create.
#

set -euo pipefail

STACK_NAME="${1:-${STACK_NAME:-gpu-capacity-agent}}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

echo "==> Tearing down stack: $STACK_NAME (region: $AWS_REGION)"

# Verify the stack exists before doing anything destructive
STACK_STATUS=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "MISSING")

if [ "$STACK_STATUS" = "MISSING" ]; then
    echo "    Stack not found — nothing to clean up."
    exit 0
fi

echo "    Current status: $STACK_STATUS"

# Empty any stack-managed S3 buckets before delete. CloudFormation
# refuses to delete a non-empty bucket, and the SAM-generated buckets
# often have artifact objects that outlive the deploy.
BUCKETS=$(aws cloudformation list-stack-resources \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'StackResourceSummaries[?ResourceType==`AWS::S3::Bucket`].PhysicalResourceId' \
    --output text)

for bucket in $BUCKETS; do
    if [ -n "$bucket" ]; then
        echo "==> Emptying stack-owned bucket: $bucket"
        aws s3 rm "s3://$bucket" --recursive --quiet || true
        # Handle versioned buckets
        aws s3api delete-objects \
            --bucket "$bucket" \
            --delete "$(aws s3api list-object-versions \
                --bucket "$bucket" \
                --output json \
                --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
                2>/dev/null || echo '{"Objects":[]}')" \
            > /dev/null 2>&1 || true
    fi
done

echo "==> Deleting stack: $STACK_NAME"
aws cloudformation delete-stack \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION"

echo "==> Waiting for stack delete to complete"
aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION"

echo ""
echo "=============================================================="
echo "Cleanup complete: $STACK_NAME removed."
echo ""
echo "NOT removed (deliberately):"
echo "  - IAM role ApiGatewayCloudWatchLogsRole and its account setting"
echo "    (account-wide; other API Gateways may depend on it)"
echo "  - CloudWatch log groups (retained per LogRetentionDays param)"
echo "  - SAM-generated artifacts bucket (--resolve-s3 default)"
echo "=============================================================="
