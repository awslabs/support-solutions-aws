#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

#
# One-click deployment for the GPU Capacity Planning Agent guidance.
#
# This script deploys the Amazon Bedrock agent stack end-to-end from a
# clean AWS account. It is designed for the AWS Solutions Library
# Automated Sample Code Validation environment:
#
#   CodeBuild image: aws/codebuild/amazonlinux-x86_64-standard:5.0
#   Compute type:    Linux Container
#   Privileged flag: enabled
#
# It also runs cleanly on any Linux workstation with an AWS profile
# that has AdministratorAccess (or the equivalent scoped policy) to
# the target account. macOS and Windows are NOT tested — use the
# `make deploy` target on those platforms.
#
# What this script does:
#   1. Verifies AWS account access and captures the account ID
#   2. Configures the account-wide API Gateway CloudWatch Logs role
#      (required by AccessLogSetting on the deployed API)
#   3. Enables Bedrock model access for the configured model
#   4. Clones the guidance repository (skipped if invoked from a clone)
#   5. Runs sam build + sam deploy with the workshop-tuned parameters
#   6. Prints stack outputs (API endpoint, Function URL, API key, etc.)
#
# Static parameters are declared at the top of the script. Adjust
# them there rather than exporting overrides at runtime — the SLIB
# validation environment does not pass runtime arguments.
#
# Cleanup:
#   ./deployment/cleanup.sh $STACK_NAME
#

set -euo pipefail

# =============================================================================
# Static parameters — edit these to change the deploy target
# =============================================================================
export AWS_REGION="us-east-1"

# Bedrock model. Claude Haiku 4.5 is the customer-deploy default —
# fastest & cheapest current-generation Anthropic model at $1/M input,
# $5/M output (4-5x cheaper than Sonnet 4.5). Callers can escalate a
# hard query to Sonnet 4.5 or Nova Pro per request via the ``model_id``
# field in the request body; see ``AllowedBedrockModelIds`` in
# template.yaml for the allowlist. Fresh AWS accounts (e.g. the SLIB
# validation environment) can invoke Claude without the Marketplace
# subscription block that Workshop Studio-vended sandboxes have at
# the SCP level.
BEDROCK_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_REGION="us-east-1"

# Stack name. Timestamp suffix keeps re-runs from colliding with prior
# state and matches the SLIB dynamic-resource-name requirement.
STACK_NAME_BASE="gpu-capacity-agent"
DATE_TAG=$(date -u +%Y%m%d%H%M%S)
STACK_NAME="${STACK_NAME_BASE}-${DATE_TAG}"

# Deploy configuration. These match the docs/implementation-guide.md
# defaults for a small-fleet production posture.
AUTH_METHOD="API_KEY"
ENABLE_FUNCTION_URL="true"
RESERVED_CONCURRENCY="10"
WAF_RATE_LIMIT="100"

# Guidance repository. When SLIB accepts the submission, the code
# lands under the aws-solutions-library-samples GitHub org and this
# script is expected to be able to clone from there.
REPO_URL="https://github.com/aws-solutions-library-samples/guidance-for-gpu-capacity-planning-on-aws.git"
REPO_DIR="guidance-for-gpu-capacity-planning-on-aws"

# IAM role name for the account-wide API Gateway CloudWatch Logs
# setting. Reused across every deploy of this guidance in the
# account; the role itself is idempotent to create.
APIGW_LOG_ROLE_NAME="ApiGatewayCloudWatchLogsRole"

# =============================================================================
# Preflight
# =============================================================================
echo "==> Verifying AWS credentials"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
echo "    Account:  $ACCOUNT_ID"
echo "    Caller:   $CALLER_ARN"
echo "    Region:   $AWS_REGION"
echo "    Stack:    $STACK_NAME"

# =============================================================================
# Step 1: Ensure the API Gateway CloudWatch Logs account role is set
# =============================================================================
# AccessLogSetting on the deployed API Gateway requires an account-
# wide CloudWatchRoleArn. AWS::ApiGateway::Account is a singleton per
# account per region — if template.yaml owned it, a customer stack
# delete would break other API Gateways in the account. So we set it
# out-of-band from the SAM stack, idempotently.
echo "==> Configuring API Gateway CloudWatch Logs account role"

EXISTING_ROLE_ARN=$(aws iam get-role \
    --role-name "$APIGW_LOG_ROLE_NAME" \
    --query 'Role.Arn' \
    --output text 2>/dev/null || echo "")

if [ -z "$EXISTING_ROLE_ARN" ]; then
    echo "    Creating $APIGW_LOG_ROLE_NAME"
    APIGW_ROLE_ARN=$(aws iam create-role \
        --role-name "$APIGW_LOG_ROLE_NAME" \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"apigateway.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        --query 'Role.Arn' \
        --output text)
    aws iam attach-role-policy \
        --role-name "$APIGW_LOG_ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
    echo "    Role created and policy attached: $APIGW_ROLE_ARN"
else
    APIGW_ROLE_ARN="$EXISTING_ROLE_ARN"
    echo "    Role already exists: $APIGW_ROLE_ARN"
fi

aws apigateway update-account \
    --region "$AWS_REGION" \
    --patch-operations "op=replace,path=/cloudwatchRoleArn,value=$APIGW_ROLE_ARN" \
    > /dev/null

# API Gateway takes ~15-30 seconds to propagate the account setting.
# Sleeping here avoids a race on first-time-in-account deploys.
echo "    Waiting 30s for account role to propagate"
sleep 30

# =============================================================================
# Step 2: Install prerequisites not preinstalled on AL2023
# =============================================================================
echo "==> Installing SAM CLI and Python dependencies"
# python3, pip, git, aws CLI, jq, and unzip are already in the SLIB
# CodeBuild AL2023 image. SAM CLI is not.
pip install --quiet --upgrade aws-sam-cli boto3

# =============================================================================
# Step 3: Clone the repository (idempotent — remove any prior copy)
# =============================================================================
# When run from an existing clone (a developer workstation), the
# script would clone into a nested directory. The SLIB validator
# always starts from a clean CodeBuild scratch directory, so this
# clone is the expected first fetch of the code.
if [ -d "$REPO_DIR" ]; then
    echo "==> Removing prior clone at $REPO_DIR"
    rm -rf "$REPO_DIR"
fi

echo "==> Cloning guidance repository"
git clone --depth 1 "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"

# =============================================================================
# Step 4: Enable Bedrock model access for the configured model
# =============================================================================
# In a fresh AWS account, the first Bedrock InvokeModel/Converse call
# triggers an automatic subscription (Marketplace-listed models like
# Anthropic Claude require aws-marketplace:Subscribe permissions,
# which AdministratorAccess includes). The SLIB validation
# environment has those permissions. Making a one-shot Converse call
# here fails fast if model access is blocked by the target account's
# guardrails.
echo "==> Testing Bedrock model access ($BEDROCK_MODEL_ID)"
if aws bedrock-runtime converse \
    --region "$BEDROCK_REGION" \
    --model-id "$BEDROCK_MODEL_ID" \
    --messages '[{"role":"user","content":[{"text":"ok"}]}]' \
    --inference-config maxTokens=5 \
    > /dev/null 2>&1; then
    echo "    Bedrock model access verified"
else
    echo "    WARNING: Bedrock model access denied for $BEDROCK_MODEL_ID."
    echo "    The stack will deploy but the Lambda will return 500 at runtime"
    echo "    until model access is granted. In fresh AWS accounts this is"
    echo "    usually granted automatically on first invocation; on locked-"
    echo "    down accounts (e.g. Workshop Studio vended sandboxes) it may"
    echo "    require an alternate model (see docs/implementation-guide.md)."
fi

# =============================================================================
# Step 5: Python virtualenv + guidance dependencies
# =============================================================================
echo "==> Setting up Python virtualenv"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# =============================================================================
# Step 6: SAM build + deploy
# =============================================================================
echo "==> Running sam build"
sam build

echo "==> Running sam deploy (stack: $STACK_NAME)"
sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --resolve-s3 \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --no-confirm-changeset \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
        "AuthMethod=$AUTH_METHOD" \
        "EnableFunctionUrl=$ENABLE_FUNCTION_URL" \
        "BedrockModelId=$BEDROCK_MODEL_ID" \
        "BedrockRegion=$BEDROCK_REGION" \
        "ReservedConcurrency=$RESERVED_CONCURRENCY" \
        "WafRateLimit=$WAF_RATE_LIMIT"

# Wait for the stack to be fully complete (sam deploy exits before
# every downstream trigger has settled).
echo "==> Waiting for stack to reach CREATE_COMPLETE"
aws cloudformation wait stack-create-complete \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" 2>/dev/null || \
    aws cloudformation wait stack-update-complete \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION"

# =============================================================================
# Step 7: Print outputs
# =============================================================================
echo "==> Stack outputs"
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].Outputs' \
    --output table

echo ""
echo "=============================================================="
echo "Deployment complete."
echo ""
echo "Stack:    $STACK_NAME"
echo "Region:   $AWS_REGION"
echo ""
echo "To exercise the agent from the repo root:"
echo "  export STACK_NAME=$STACK_NAME"
echo "  ./examples/query-quick.sh \"Is p4d.24xlarge available in us-east-1?\""
echo ""
echo "To tear down:"
echo "  ./deployment/cleanup.sh $STACK_NAME"
echo "=============================================================="
