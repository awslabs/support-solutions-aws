# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# variables.tf - Service Lifecycle Tracker on AWS (Terraform)
# =============================================================================
# deploy-all-terraform.{ps1,sh} writes terraform.tfvars from your AWS CLI
# configuration; copy terraform.tfvars.example for a manual run.

variable "region" {
  description = "AWS region to deploy into (Lambda durable functions and Amazon Nova must be available there)."
  type        = string
}

variable "backend_stage_dir" {
  description = "Directory holding the staged Lambda code (backend/*.py, shared aws_utils.py and the pip-installed arm64 wheels). Produced by the deploy script."
  type        = string
  default     = ".backend-stage"
}

variable "service_configs_path" {
  description = "Path to service_configs.json (the service definitions seeded into the config table)."
  type        = string
  default     = "../scripts/service_configs.json"
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days for the Lambda functions and the HTTP API access logs (default: 1 year)."
  type        = number
  default     = 365
}

variable "enable_cloudfront_logging" {
  description = "Write CloudFront access logs to a dedicated S3 bucket (kept 30 days). Set to false in opt-in Regions, where CloudFront cannot deliver standard logs to a bucket (the empty bucket is still created)."
  type        = bool
  default     = true
}

variable "enable_deployment_metrics" {
  description = "Create a zero-cost CloudFormation marker stack so this deployment is counted in AWS solution adoption metrics (see tracking.tf). Set to false to opt out."
  type        = bool
  default     = true
}

variable "api_throttle_rate" {
  description = "HTTP API steady-state request limit per second, across all callers. A dashboard load sends a handful of requests at once and a running refresh polls every 5 s."
  type        = number
  default     = 20

  validation {
    condition     = var.api_throttle_rate > 0
    error_message = "api_throttle_rate must be positive."
  }
}

variable "api_throttle_burst" {
  description = "HTTP API burst limit (requests), across all callers."
  type        = number
  default     = 40

  validation {
    condition     = var.api_throttle_burst > 0 && floor(var.api_throttle_burst) == var.api_throttle_burst
    error_message = "api_throttle_burst must be a positive integer."
  }
}

variable "api_reserved_concurrency" {
  description = "Reserved concurrency of the API function, -1 for none (default). Reserving fails on accounts whose Lambda concurrency quota is low (new accounts can start at 10)."
  type        = number
  default     = -1

  validation {
    condition     = var.api_reserved_concurrency == -1 || (var.api_reserved_concurrency >= 1 && floor(var.api_reserved_concurrency) == var.api_reserved_concurrency)
    error_message = "api_reserved_concurrency must be -1 or a positive integer."
  }
}

variable "pipeline_role_name" {
  description = "Name of the hub pipeline IAM role. Fixed by design (spoke trust policies pin it); IAM is global, so change it only if the name is already taken in this account, e.g. by a CDK deployment of this demo in another region."
  type        = string
  default     = "aws-services-lifecycle-pipeline-role"
}

variable "spoke_external_id" {
  description = "Optional sts:ExternalId presented when the pipeline assumes spoke roles (multi-account, issue #144). Leave empty for single-account."
  type        = string
  default     = ""
}
