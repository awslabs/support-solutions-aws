# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# =============================================================================
# frontend.tf - Static SPA hosting, S3 + CloudFront (CDK: frontend-stack.ts)
# =============================================================================
# Terraform creates the bucket and the distribution only. The build in
# ../frontend/dist needs the API URL and Cognito IDs, which exist after apply,
# so the deploy script builds the SPA, syncs it to the bucket and invalidates
# the distribution as a post-apply step (CDK does the same with a placeholder
# build followed by BucketDeployment).
# =============================================================================

resource "aws_s3_bucket" "website" {
  bucket_prefix = "aws-services-lifecycle-tracker-web-"
  force_destroy = true # demo: `terraform destroy` empties and removes the bucket
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "website" {
  bucket = aws_s3_bucket.website.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "website" {
  bucket = aws_s3_bucket.website.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# -- CloudFront access logs ---------------------------------------------------
# Standard logs of every viewer request, kept 30 days (CDK: AccessLogsBucket).
# CloudFront delivers them through the bucket ACL (awslogsdelivery account), so
# ACLs stay enabled with BucketOwnerPreferred; the bucket owner owns the logs.
# Set enable_cloudfront_logging = false in opt-in Regions, where CloudFront
# cannot deliver standard logs to a bucket (the bucket then stays empty).

resource "aws_s3_bucket" "access_logs" {
  bucket_prefix = "aws-services-lifecycle-tracker-logs-"
  force_destroy = true # demo: `terraform destroy` empties and removes the bucket
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "access_logs" {
  #checkov:skip=CKV2_AWS_65:CloudFront standard logging writes through the bucket ACL (awslogsdelivery account), so ACLs must stay enabled on its log bucket
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # CloudFront log delivery supports SSE-S3, not the AWS managed KMS key
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# enforceSSL: deny any non-TLS access to the log bucket
resource "aws_s3_bucket_policy" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.access_logs.arn, "${aws_s3_bucket.access_logs.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.access_logs]
}

resource "aws_cloudfront_origin_access_control" "website" {
  name                              = "aws-services-lifecycle-tracker-oac"
  description                       = "OAC for the lifecycle tracker website bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "no-override"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

# Security headers on every response (CDK: SecurityHeaders).
# The SPA only loads its own script and stylesheet (fonts are data: URIs in that
# stylesheet) and only talks to the HTTP API and the Cognito user pool endpoint.
locals {
  content_security_policy = join("; ", [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self' ${aws_apigatewayv2_api.http.api_endpoint} https://cognito-idp.${var.region}.${data.aws_partition.current.dns_suffix}",
    "manifest-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "upgrade-insecure-requests",
  ])
}

resource "aws_cloudfront_response_headers_policy" "security_headers" {
  name    = "aws-services-lifecycle-tracker-security-headers-${var.region}"
  comment = "Service Lifecycle Tracker on AWS - security headers for the SPA"

  security_headers_config {
    content_security_policy {
      content_security_policy = local.content_security_policy
      override                = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }
    xss_protection {
      protection = true
      mode_block = true
      override   = true
    }
  }
}

resource "aws_cloudfront_distribution" "website" {
  # The default *.cloudfront.net certificate is always served with CloudFront's
  # TLSv1 security policy, whatever minimum_protocol_version says (same as CDK
  # cdk-nag CFR4). Deployers adding a custom domain and an ACM certificate
  # should set minimum_protocol_version = "TLSv1.2_2021".
  #checkov:skip=CKV_AWS_174:Default CloudFront certificate; a minimum TLS version can only be set with a custom domain and ACM certificate
  comment             = "Service Lifecycle Tracker on AWS - Frontend Distribution"
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_All"

  dynamic "logging_config" {
    for_each = var.enable_cloudfront_logging ? [1] : []
    content {
      bucket          = aws_s3_bucket.access_logs.bucket_regional_domain_name
      prefix          = "cloudfront/"
      include_cookies = false
    }
  }

  origin {
    origin_id                = "website-bucket"
    domain_name              = aws_s3_bucket.website.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.website.id
  }

  default_cache_behavior {
    target_origin_id           = "website-bucket"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security_headers.id
  }

  # SPA routing: unknown paths (and S3's 403 for missing keys) serve index.html
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = local.tags

  # Log delivery needs ACLs enabled on the log bucket before CloudFront grants itself access
  depends_on = [aws_s3_bucket_ownership_controls.access_logs]
}

# Grant CloudFront (this distribution only) read access to the bucket
resource "aws_s3_bucket_policy" "website" {
  bucket = aws_s3_bucket.website.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontServicePrincipalReadOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.website.arn}/*"
        Condition = {
          StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.website.arn }
        }
      },
      {
        # enforceSSL: CloudFront reads over HTTPS (viewers are redirected to HTTPS)
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.website.arn, "${aws_s3_bucket.website.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.website]
}
