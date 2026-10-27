# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# data.tf - DynamoDB tables + service config populator (CDK: data-stack.ts)
# =============================================================================
# Five tables, same names and key shapes as the CDK Data stack, so the backend
# code is identical on both paths.
#
# Difference from CDK: the CDK tables and their KMS key carry
# RemovalPolicy.RETAIN and survive `cdk destroy`. Here `terraform destroy`
# removes the tables and their data and schedules the key for deletion; this
# is a demo, and a teardown that leaves orphans is worse than one that does not.
# =============================================================================

# -- Customer managed key for the data at rest --------------------------------
# Encrypts the five tables and the EventBridge Scheduler payload
# (pipeline.tf). The Lambda roles may use it only through DynamoDB.
# Same key policy as the CDK default: the account administers the key and
# IAM policies grant its use.

resource "aws_kms_key" "data" {
  description             = "Service Lifecycle Tracker on AWS: DynamoDB tables and EventBridge Scheduler payload"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EnableIamPolicies"
      Effect    = "Allow"
      Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })

  tags = local.tags
}

resource "aws_kms_alias" "data" {
  name          = "alias/aws-services-lifecycle-tracker"
  target_key_id = aws_kms_key.data.key_id
}

# -- Public deprecation facts -------------------------------------------------

resource "aws_dynamodb_table" "lifecycle" {
  name         = "aws-services-lifecycle"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_name"
  range_key    = "item_id"

  attribute {
    name = "service_name"
    type = "S"
  }
  attribute {
    name = "item_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "deprecation_date"
    type = "S"
  }
  attribute {
    name = "extraction_date"
    type = "S"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    range_key       = "deprecation_date"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "extraction-date-index"
    hash_key        = "service_name"
    range_key       = "extraction_date"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn # customer managed key, like CDK TableEncryption.CUSTOMER_MANAGED
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}

# -- Repo-owned service configuration (seeded from service_configs.json) ------

resource "aws_dynamodb_table" "config" {
  name         = "service-extraction-config"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_name"

  attribute {
    name = "service_name"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}

# -- Backend-owned runtime state (issue #116) --------------------------------
# Extraction metadata and control rows (_health_match, _scan_targets, locks)
# live here, physically separated from the repo-owned config table so no
# deploy-time writer can touch runtime state.

resource "aws_dynamodb_table" "state" {
  name         = "service-extraction-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_name"

  attribute {
    name = "service_name"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}

# -- Account inventory (what the scan found) ---------------------------------

resource "aws_dynamodb_table" "inventory" {
  name         = "aws-account-inventory"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_name"
  range_key    = "item_id"

  attribute {
    name = "service_name"
    type = "S"
  }
  attribute {
    name = "item_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}

# -- Plans of action ----------------------------------------------------------

resource "aws_dynamodb_table" "action_plan" {
  name         = "deprecation-action-plans"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "plan_id"

  attribute {
    name = "plan_id"
    type = "S"
  }
  attribute {
    name = "owner"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "plan_status"
    type = "S"
  }
  attribute {
    name = "target_date"
    type = "S"
  }

  global_secondary_index {
    name            = "owner-index"
    hash_key        = "owner"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "plan-status-index"
    hash_key        = "plan_status"
    range_key       = "target_date"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}

# -- Service config populator -------------------------------------------------
# Same Python as the CDK custom resource (scripts/service_config_populator.py):
# upserts the static, repo-owned fields of every service and removes services
# that left service_configs.json, never touching backend-owned runtime state.
# Re-runs whenever the JSON or the populator code changes.

locals {
  service_configs = jsondecode(file(var.service_configs_path))
}

data "archive_file" "populator" {
  type        = "zip"
  source_file = "${path.module}/../scripts/service_config_populator.py"
  output_path = "${path.module}/build/populator.zip"
}

resource "aws_iam_role" "populator" {
  name        = "aws-services-lifecycle-config-populator-role"
  description = "Deploy-time seeding of the service-extraction-config table"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "populator_basic" {
  role       = aws_iam_role.populator.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Write (upsert/delete) plus read (scan for the reconcile step), config table only.
resource "aws_iam_role_policy" "populator_config_table" {
  name = "ConfigTableReadWrite"
  role = aws_iam_role.populator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
          "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
        ]
        Resource = [aws_dynamodb_table.config.arn, "${aws_dynamodb_table.config.arn}/index/*"]
      },
      {
        # The table is encrypted with the data key; DynamoDB uses it on the caller's behalf
        Effect = "Allow"
        Action = [
          "kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:ReEncryptFrom", "kms:ReEncryptTo",
          "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext",
        ]
        Resource  = aws_kms_key.data.arn
        Condition = { StringEquals = { "kms:ViaService" = "dynamodb.${var.region}.amazonaws.com" } }
      },
    ]
  })
}

# Managed explicitly so `terraform destroy` removes it (Lambda would otherwise
# auto-create one on first invocation and leave it behind).
resource "aws_cloudwatch_log_group" "populator" {
  name              = "/aws/lambda/aws-services-lifecycle-config-populator"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_lambda_function" "populator" {
  function_name    = "aws-services-lifecycle-config-populator"
  description      = "Seeds service_configs.json into the service-extraction-config table (deploy time only)"
  role             = aws_iam_role.populator.arn
  runtime          = "python3.14"
  architectures    = ["arm64"]
  handler          = "service_config_populator.handler"
  filename         = data.archive_file.populator.output_path
  source_code_hash = data.archive_file.populator.output_base64sha256
  timeout          = 300

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.populator.name
  }

  environment {
    variables = {
      CONFIG_TABLE_NAME = aws_dynamodb_table.config.name
    }
  }

  tags = local.tags

  depends_on = [aws_iam_role_policy_attachment.populator_basic, aws_iam_role_policy.populator_config_table]
}

resource "aws_lambda_invocation" "populate_service_configs" {
  function_name = aws_lambda_function.populator.function_name

  input = jsonencode({
    ServiceConfigs = jsonencode(local.service_configs.services)
    TableName      = aws_dynamodb_table.config.name
  })

  triggers = {
    configs   = filesha256(var.service_configs_path)
    populator = data.archive_file.populator.output_base64sha256
  }

  depends_on = [aws_dynamodb_table.config]
}
