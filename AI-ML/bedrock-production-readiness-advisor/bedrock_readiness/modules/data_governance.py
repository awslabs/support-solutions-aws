# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Data Governance -- Where does prompt/response data actually go?

Checks: DG-01 through DG-04, including the account's Zero Data Retention
(ZDR) posture. DG-01's severity mapping follows the mode semantics AWS
documents in "Enforce zero data retention on Amazon Bedrock with Bedrock
Projects and service control policies" (AWS Security Blog) and the Bedrock
user guide's data-retention page -- not an assumption about which mode is
"good": `none` is zero retention, `default` shares nothing with model
providers (some models may retain up to 30 days for trust/safety), `inherit`
means no explicit choice has been made at this scope, and `provider_data_share`
means prompts/responses may be shared with and retained by the model provider.

Checks: DG-01 through DG-04. Every check here reports on Bedrock's own account
setting or on the ONE S3 bucket Bedrock is configured to write invocation logs
to -- never a bucket, key, or account-wide policy unrelated to Bedrock.
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError

PILLAR_ID = "data_governance"
PILLAR_NAME = "Data Governance"


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)

    result.findings.append(_check_data_retention_mode(scan_data))
    result.findings.append(_check_s3_logging_encryption(scan_data))
    result.findings.append(_check_s3_logging_public_access(scan_data))
    result.findings.append(_check_log_group_encryption(scan_data))

    return result


def _check_data_retention_mode(scan_data: dict) -> Finding:
    retention = scan_data.get("bedrock", {}).get("data_retention")
    if retention is None or isinstance(retention, ScanError):
        err = retention.error if isinstance(retention, ScanError) else "no data"
        return Finding(
            check_id="DG-01", check_name="Account Data Retention Mode",
            status=CheckStatus.ERROR, impact=4, likelihood=2,
            message=f"Could not read the account's data retention mode: {err}",
            recommendation="Grant bedrock:GetAccountDataRetention",
        )

    mode = retention.get("mode", "")

    if mode == "none":
        return Finding(
            check_id="DG-01", check_name="Account Data Retention Mode",
            status=CheckStatus.PASS, impact=4, likelihood=2,
            message="Data retention mode is `none` -- prompts and responses are "
                    "processed and immediately discarded (zero data retention)",
        )
    if mode == "default":
        return Finding(
            check_id="DG-01", check_name="Account Data Retention Mode",
            status=CheckStatus.PASS, impact=3, likelihood=2,
            message="Data retention mode is `default` -- no data is shared with model "
                    "providers, though some models may retain data up to 30 days for "
                    "trust-and-safety checks",
        )
    if mode == "inherit":
        return Finding(
            check_id="DG-01", check_name="Account Data Retention Mode",
            status=CheckStatus.WARN, impact=2, likelihood=2,
            message="Data retention mode is `inherit` -- no explicit choice has been "
                    "made at the account level; this account defers to the service "
                    "default (or an org-level setting), which can change without an "
                    "account-level decision",
            recommendation=(
                "If this account's data-handling requirements are known, set an "
                "explicit account-level mode (`none` or `default`) rather than "
                "relying on inheritance"
            ),
            fix_type=FixType.CONFIG, effort_minutes=10,
        )
    if mode == "provider_data_share":
        return Finding(
            check_id="DG-01", check_name="Account Data Retention Mode",
            status=CheckStatus.WARN, impact=4, likelihood=3,
            message="Data retention mode is `provider_data_share` -- prompts and "
                    "responses may be shared with and retained by the model provider "
                    "for up to 30 days",
            recommendation=(
                "Confirm this is an intentional choice. It is required for some models "
                "to function, but if this account has regulatory or contractual "
                "commitments against third-party data sharing, switch to `none` or "
                "`default` and review which models remain available"
            ),
            fix_type=FixType.CONFIG, effort_minutes=15,
        )
    return Finding(
        check_id="DG-01", check_name="Account Data Retention Mode",
        status=CheckStatus.WARN, impact=2, likelihood=1,
        message=f"Data retention mode reported an unrecognised value: {mode!r}",
        recommendation="Check the Bedrock console's data retention setting directly",
        fix_type=FixType.CONFIG, effort_minutes=5,
    )


def _check_s3_logging_encryption(scan_data: dict) -> Finding:
    sec = scan_data.get("s3", {}).get("logging_destination_security")
    if isinstance(sec, ScanError):
        # Distinct from sec is None below: this means the read that would
        # tell us whether a destination even exists failed outright, not
        # that it succeeded and found none. Reporting SKIPPED here would
        # claim "nothing to check" when verification never actually ran.
        return Finding(
            check_id="DG-02", check_name="Invocation Log Bucket Encryption",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message=f"Could not determine whether Bedrock invocation logging has an "
                    f"S3 destination: {sec.error}",
            recommendation="Grant bedrock:GetModelInvocationLoggingConfiguration",
        )
    if sec is None:
        return Finding(
            check_id="DG-02", check_name="Invocation Log Bucket Encryption",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No S3 destination is configured for Bedrock invocation logging",
        )
    if sec.get("EncryptionError"):
        return Finding(
            check_id="DG-02", check_name="Invocation Log Bucket Encryption",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message=f"Could not read encryption settings for bucket "
                    f"{sec['BucketName']}: {sec['EncryptionError']}",
            recommendation="Grant s3:GetBucketEncryption on the invocation-logging bucket",
        )
    rules = sec.get("EncryptionRules", [])
    if not rules:
        return Finding(
            check_id="DG-02", check_name="Invocation Log Bucket Encryption",
            status=CheckStatus.FAIL, impact=3, likelihood=2,
            message=f"Bucket {sec['BucketName']} (the Bedrock invocation-logging "
                    f"destination) has no default encryption configuration",
            recommendation="Enable default encryption (SSE-S3 or SSE-KMS) on this bucket",
            resource_ids=[sec["BucketName"]],
            fix_type=FixType.CONFIG, effort_minutes=10,
        )
    uses_cmk = any(
        r.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == "aws:kms"
        for r in rules
    )
    if uses_cmk:
        return Finding(
            check_id="DG-02", check_name="Invocation Log Bucket Encryption",
            status=CheckStatus.PASS, impact=3, likelihood=2,
            message=f"Bucket {sec['BucketName']} is encrypted with a KMS key",
        )
    return Finding(
        check_id="DG-02", check_name="Invocation Log Bucket Encryption",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message=f"Bucket {sec['BucketName']} has default encryption enabled, but not "
                f"with a KMS key (Amazon Bedrock invocation logs may include prompt "
                f"and response content)",
        recommendation=(
            "Switch the bucket's default encryption to SSE-KMS with a customer-managed "
            "key if this data needs key-level access control or audit"
        ),
        resource_ids=[sec["BucketName"]],
        fix_type=FixType.CONFIG, effort_minutes=15,
    )


def _check_s3_logging_public_access(scan_data: dict) -> Finding:
    sec = scan_data.get("s3", {}).get("logging_destination_security")
    if isinstance(sec, ScanError):
        # Same distinction as DG-02: a failed read is not the same as a
        # successful read that found no destination.
        return Finding(
            check_id="DG-03", check_name="Invocation Log Bucket Public Access",
            status=CheckStatus.ERROR, impact=4, likelihood=1,
            message=f"Could not determine whether Bedrock invocation logging has an "
                    f"S3 destination: {sec.error}",
            recommendation="Grant bedrock:GetModelInvocationLoggingConfiguration",
        )
    if sec is None:
        return Finding(
            check_id="DG-03", check_name="Invocation Log Bucket Public Access",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No S3 destination is configured for Bedrock invocation logging",
        )
    if sec.get("PublicAccessBlockError"):
        return Finding(
            check_id="DG-03", check_name="Invocation Log Bucket Public Access",
            status=CheckStatus.ERROR, impact=4, likelihood=1,
            message=f"Could not read public-access-block settings for bucket "
                    f"{sec['BucketName']}: {sec['PublicAccessBlockError']}",
            recommendation="Grant s3:GetPublicAccessBlock on the invocation-logging bucket",
        )
    block = sec.get("PublicAccessBlock", {})
    fully_blocked = all(block.get(k) for k in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"
    ))
    if fully_blocked:
        return Finding(
            check_id="DG-03", check_name="Invocation Log Bucket Public Access",
            status=CheckStatus.PASS, impact=4, likelihood=1,
            message=f"Bucket {sec['BucketName']} has all four public-access-block "
                    f"settings enabled",
        )
    return Finding(
        check_id="DG-03", check_name="Invocation Log Bucket Public Access",
        status=CheckStatus.FAIL, impact=4, likelihood=1,
        message=f"Bucket {sec['BucketName']} (the Bedrock invocation-logging "
                f"destination, which may include prompt and response content) does "
                f"not have all four public-access-block settings enabled",
        recommendation="Enable all four S3 Block Public Access settings on this bucket",
        resource_ids=[sec["BucketName"]],
        fix_type=FixType.CONFIG, effort_minutes=5,
    )


def _check_log_group_encryption(scan_data: dict) -> Finding:
    log_groups = scan_data.get("cloudwatch", {}).get("bedrock_log_groups")
    if isinstance(log_groups, ScanError):
        # A ScanError is truthy (no __bool__/__len__ override), so without
        # this check it would fall through to `not log_groups` (False) and
        # then be iterated as if it were a list -- a hard crash, not a
        # graceful "unable to verify" finding.
        return Finding(
            check_id="DG-04", check_name="Invocation Log Group Encryption",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not list Bedrock CloudWatch log groups: {log_groups.error}",
            recommendation="Grant logs:DescribeLogGroups",
        )
    if not log_groups:
        return Finding(
            check_id="DG-04", check_name="Invocation Log Group Encryption",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No CloudWatch Logs destination for Bedrock invocation logging "
                    "was found",
        )

    without_cmk = [lg.get("logGroupName", "?") for lg in log_groups if not lg.get("kmsKeyId")]
    if without_cmk:
        return Finding(
            check_id="DG-04", check_name="Invocation Log Group Encryption",
            status=CheckStatus.WARN, impact=2, likelihood=2,
            message=(
                f"{len(without_cmk)} Bedrock log group(s) use CloudWatch Logs' default "
                f"encryption rather than a customer-managed KMS key: "
                f"{', '.join(without_cmk[:5])}. Logs are encrypted at rest either way -- "
                f"this is about key-level access control, not whether encryption exists"
            ),
            recommendation=(
                "Associate a customer-managed KMS key with this log group if "
                "key-level access control or key-usage audit is required for "
                "invocation-log content"
            ),
            resource_ids=without_cmk,
            fix_type=FixType.CONFIG, effort_minutes=15,
        )
    return Finding(
        check_id="DG-04", check_name="Invocation Log Group Encryption",
        status=CheckStatus.PASS, impact=2, likelihood=2,
        message=f"All {len(log_groups)} Bedrock log group(s) are associated with a "
                f"customer-managed KMS key",
    )
