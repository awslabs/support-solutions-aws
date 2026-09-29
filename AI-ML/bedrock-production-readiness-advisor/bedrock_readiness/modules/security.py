# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Security -- Is access to Bedrock scoped the way it should be?

Added alongside Guardrails and Data Governance so this package covers all
eight readiness pillars using only read-only (describe/list/get) API calls.
Every finding here is a plain-text observation with a link to public AWS
documentation -- never a corrected IAM policy, CloudFormation/Terraform
snippet, or anything else shaped to be copied straight into an account. See
core/scanner.py for exactly which calls back these checks and why each is
scoped the way it is.

Checks: SEC-01 through SEC-04. All four operate only on the IAM roles this
scanner already identified as trusted by a Bedrock-family service principal
(see AccountScanner.list_roles_with_bedrock) -- never on every role in the
account, and never on a policy unrelated to Bedrock.
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError

PILLAR_ID = "security"
PILLAR_NAME = "Security"

# AWS managed policies broad enough that attaching one to a Bedrock-related
# role is worth flagging regardless of what else the role does.
BROAD_MANAGED_POLICY_NAMES = ("admin", "fullaccess")


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)

    roles = scan_data.get("iam", {}).get("bedrock_roles")
    role_docs = scan_data.get("iam", {}).get("bedrock_role_policy_documents")
    trust_info = scan_data.get("iam", {}).get("bedrock_role_trust_info")

    result.findings.append(_check_broad_managed_policies(roles))
    result.findings.append(_check_wildcard_policy_documents(role_docs))
    result.findings.append(_check_trust_policy_wildcard(trust_info))
    result.findings.append(_check_cloudtrail_coverage(scan_data))

    return result


def _check_broad_managed_policies(roles) -> Finding:
    if roles is None or isinstance(roles, ScanError):
        err = roles.error if isinstance(roles, ScanError) else "no data"
        return Finding(
            check_id="SEC-01", check_name="Broad Managed Policies on Bedrock Roles",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message=f"Could not enumerate IAM roles with a Bedrock-related policy: {err}",
            recommendation="Grant iam:ListRoles and iam:ListAttachedRolePolicies",
        )
    if not roles:
        return Finding(
            check_id="SEC-01", check_name="Broad Managed Policies on Bedrock Roles",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No IAM role with a Bedrock-, admin-, or fullaccess-named policy was found",
        )

    overly_broad = [r for r in roles if any(
        k in r.get("PolicyName", "").lower() for k in BROAD_MANAGED_POLICY_NAMES
    )]
    if overly_broad:
        names = sorted({r["RoleName"] for r in overly_broad})
        return Finding(
            check_id="SEC-01", check_name="Broad Managed Policies on Bedrock Roles",
            status=CheckStatus.FAIL, impact=4, likelihood=3,
            message=f"{len(names)} role(s) carry an admin- or fullaccess-named policy: "
                    f"{', '.join(names[:5])}",
            recommendation=(
                "Replace the broad managed policy with a customer-managed policy scoped "
                "to the specific Bedrock actions and resources this role needs"
            ),
            resource_ids=[r["Arn"] for r in overly_broad[:10]],
            fix_type=FixType.CONFIG, effort_minutes=60,
        )
    return Finding(
        check_id="SEC-01", check_name="Broad Managed Policies on Bedrock Roles",
        status=CheckStatus.PASS, impact=4, likelihood=3,
        message=f"{len(roles)} Bedrock-related role/policy pair(s) found, none "
                f"admin- or fullaccess-named",
        resource_ids=[r["Arn"] for r in roles[:5]],
    )


def _check_wildcard_policy_documents(role_docs) -> Finding:
    if role_docs is None or isinstance(role_docs, ScanError):
        err = role_docs.error if isinstance(role_docs, ScanError) else "no data"
        return Finding(
            check_id="SEC-02", check_name="Wildcard Bedrock Actions on Roles",
            status=CheckStatus.ERROR, impact=4, likelihood=3,
            message=f"Could not inspect IAM policy documents: {err}",
            recommendation=(
                "Grant iam:ListRolePolicies, iam:GetRolePolicy, iam:GetPolicy, and "
                "iam:GetPolicyVersion"
            ),
        )
    if not role_docs:
        return Finding(
            check_id="SEC-02", check_name="Wildcard Bedrock Actions on Roles",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No policy granting a Bedrock action was found on a Bedrock-trusted role",
        )

    unreadable = [p for p in role_docs if "error" in p]
    readable = [p for p in role_docs if "error" not in p]
    wildcard = [p for p in readable if p.get("wildcards")]

    if wildcard:
        role_names = sorted({p["role"] for p in wildcard})
        return Finding(
            check_id="SEC-02", check_name="Wildcard Bedrock Actions on Roles",
            status=CheckStatus.FAIL, impact=4, likelihood=3,
            message=(
                f"{len(role_names)} role(s) grant a wildcard bedrock:*/bedrock-runtime:*/"
                f"bedrock-mantle:* action rather than a specific one: "
                f"{', '.join(role_names[:5])}"
            ),
            recommendation=(
                "Scope the policy to the specific actions this role needs (e.g. "
                "bedrock:InvokeModel, bedrock:GetFoundationModel) instead of a "
                "service-level wildcard"
            ),
            resource_ids=role_names[:10],
            fix_type=FixType.CONFIG, effort_minutes=30,
        )
    if unreadable:
        # Some policy documents could not be read. The readable ones showed
        # no wildcard, but that is not the same as "no wildcard exists" --
        # an unreadable document could grant one. Reporting PASS here would
        # be false assurance: verification was incomplete, not clean.
        unreadable_roles = sorted({p["role"] for p in unreadable})
        errors = sorted({p.get("error", "unknown error") for p in unreadable})
        return Finding(
            check_id="SEC-02", check_name="Wildcard Bedrock Actions on Roles",
            status=CheckStatus.WARN, impact=4, likelihood=3,
            message=(
                f"{len(readable)} policy document(s) inspected with no wildcard "
                f"action found, but {len(unreadable)} policy document(s) on "
                f"{len(unreadable_roles)} role(s) could not be read and were NOT "
                f"checked: {', '.join(unreadable_roles[:5])} ({'; '.join(errors[:3])})"
            ),
            recommendation=(
                "Grant iam:GetRolePolicy / iam:GetPolicy / iam:GetPolicyVersion for "
                "the listed role(s) so their policy documents can actually be "
                "inspected for a wildcard grant"
            ),
            resource_ids=unreadable_roles[:10],
        )
    return Finding(
        check_id="SEC-02", check_name="Wildcard Bedrock Actions on Roles",
        status=CheckStatus.PASS, impact=4, likelihood=3,
        message=f"{len(readable)} Bedrock-related policy document(s) inspected, no "
                f"wildcard action found",
    )


def _check_trust_policy_wildcard(trust_info) -> Finding:
    if trust_info is None or isinstance(trust_info, ScanError):
        err = trust_info.error if isinstance(trust_info, ScanError) else "no data"
        return Finding(
            check_id="SEC-03", check_name="Bedrock Role Trust Policy Scoping",
            status=CheckStatus.ERROR, impact=3, likelihood=1,
            message=f"Could not read IAM role trust policies: {err}",
            recommendation="Grant iam:ListRoles",
        )
    if not trust_info:
        return Finding(
            check_id="SEC-03", check_name="Bedrock Role Trust Policy Scoping",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No IAM role trusted by a Bedrock service principal was found",
        )

    wildcard_roles = [r["RoleName"] for r in trust_info if r.get("TrustPrincipalWildcard")]
    if wildcard_roles:
        return Finding(
            check_id="SEC-03", check_name="Bedrock Role Trust Policy Scoping",
            status=CheckStatus.FAIL, impact=4, likelihood=1,
            message=(
                f"{len(wildcard_roles)} role(s) trusted by Bedrock also trust "
                f"Principal \"*\" in the same trust policy: {', '.join(wildcard_roles[:5])}"
            ),
            recommendation=(
                "Remove the wildcard principal and name only the specific service or "
                "account principals this role should be assumable by. A role trusted "
                "by any AWS principal can be assumed from outside your account"
            ),
            resource_ids=wildcard_roles,
            fix_type=FixType.CONFIG, effort_minutes=30,
        )
    return Finding(
        check_id="SEC-03", check_name="Bedrock Role Trust Policy Scoping",
        status=CheckStatus.PASS, impact=4, likelihood=1,
        message=f"None of the {len(trust_info)} Bedrock-trusted role(s) also trust a "
                f"wildcard principal",
    )


def _check_cloudtrail_coverage(scan_data: dict) -> Finding:
    trail_details = scan_data.get("cloudtrail", {}).get("trail_details")
    if trail_details is None or isinstance(trail_details, ScanError):
        err = trail_details.error if isinstance(trail_details, ScanError) else "no data"
        return Finding(
            check_id="SEC-04", check_name="CloudTrail Coverage for Bedrock",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message=f"Could not check CloudTrail status: {err}",
            recommendation="Grant cloudtrail:DescribeTrails, GetTrailStatus, GetEventSelectors",
        )
    if not trail_details:
        return Finding(
            check_id="SEC-04", check_name="CloudTrail Coverage for Bedrock",
            status=CheckStatus.FAIL, impact=3, likelihood=3,
            message="No CloudTrail trail exists in this account",
            recommendation=(
                "Create a trail covering this region so management-event API calls "
                "against Bedrock (and everything else) are recorded"
            ),
            fix_type=FixType.DEPLOY, effort_minutes=30,
        )

    logging_trails = [t for t in trail_details if t.get("IsLogging")]
    if not logging_trails:
        return Finding(
            check_id="SEC-04", check_name="CloudTrail Coverage for Bedrock",
            status=CheckStatus.FAIL, impact=3, likelihood=3,
            message=f"{len(trail_details)} trail(s) exist but none is actively logging",
            recommendation="Start logging on an existing trail, or create a new one",
            fix_type=FixType.CONFIG, effort_minutes=15,
        )

    data_event_trails = [
        t for t in logging_trails
        if any("bedrock" in str(sel).lower() for sel in t.get("AdvancedEventSelectors", []))
    ]
    if data_event_trails:
        return Finding(
            check_id="SEC-04", check_name="CloudTrail Coverage for Bedrock",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"{len(data_event_trails)} trail(s) are logging and explicitly "
                    f"cover Bedrock data events",
        )
    return Finding(
        check_id="SEC-04", check_name="CloudTrail Coverage for Bedrock",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message=(
            f"{len(logging_trails)} trail(s) are logging management events, which "
            f"already covers control-plane Bedrock calls (CreateGuardrail, PutModel*, "
            f"IAM changes). None has an advanced event selector naming Bedrock data "
            f"events (e.g. InvokeModel), so per-invocation audit detail is not captured"
        ),
        recommendation=(
            "If per-invocation audit detail is required, add an advanced event "
            "selector for Bedrock data events to an existing trail. This is high-volume "
            "and priced accordingly -- confirm the cost is acceptable before enabling it"
        ),
        fix_type=FixType.CONFIG, effort_minutes=20,
    )
