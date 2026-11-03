# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Multi-account / AWS Organizations support for Hibernate AI."""
import boto3
import json
import os

CROSS_ACCOUNT_ROLE = os.environ.get("CROSS_ACCOUNT_ROLE", "HibernateAI-CrossAccount")


def list_org_accounts():
    """List all accounts in the AWS Organization."""
    try:
        orgs = boto3.client("organizations")
        accounts = []
        paginator = orgs.get_paginator("list_accounts")
        for page in paginator.paginate():
            for acct in page["Accounts"]:
                if acct["Status"] == "ACTIVE":
                    accounts.append({"id": acct["Id"], "name": acct["Name"], "email": acct["Email"]})
        return accounts
    except Exception as e:
        return [{"id": boto3.client("sts").get_caller_identity()["Account"], "name": "Current Account", "error": str(e)}]


def get_cross_account_session(account_id):
    """Assume role in target account and return boto3 session."""
    sts = boto3.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE}"
    try:
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="HibernateAI", DurationSeconds=3600)["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    except Exception as e:
        print(f"CROSS_ACCOUNT_FAIL: {account_id} - {e}")
        return None


def discover_all_accounts():
    """Get enabled accounts from DynamoDB config or Organizations."""
    try:
        import config as hib_config
        accounts = hib_config.load_config().get("accounts")
        if accounts:
            return accounts
    except Exception:
        pass
    # Fallback to Org discovery
    return list_org_accounts()


def save_account_config(accounts):
    """Save enabled account list to DynamoDB WITHOUT clobbering other __config__ keys
    (deny-list, thresholds, grouping, model). Uses the typed config accessor."""
    import config as hib_config
    cfg = hib_config.load_config()
    cfg["accounts"] = accounts
    hib_config.save_config(cfg)


def get_cross_account_cfn_template():
    """Return CloudFormation template for the cross-account IAM role.

    READ-ONLY v1: this org-wide StackSet role is ADVISORY-ONLY. It grants only read/discovery
    (Describe/List) + CloudWatch metric reads so HibernateAI can analyze idle resources and
    estimate savings across member accounts. It grants NO destructive, mutating, create, or
    PassRole actions — a copied/deployed template cannot change any member-account resource.
    Destructive hibernate/restore capability ships in a separate, customer-authorized,
    security-reviewed add-on, not in this read-only role.
    """
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Hibernate AI Cross-Account READ-ONLY Role - deploy via StackSets to member accounts (advisory-only)",
        "Parameters": {
            "ManagementAccountId": {
                "Type": "String",
                "Description": "Account ID where Hibernate AI Lambda runs",
            },
            "OrgId": {
                "Type": "String",
                "Description": "AWS Organizations ID (o-xxxx) — confused-deputy guard on the trust policy",
            },
        },
        "Resources": {
            # Read-only permissions boundary: a hard ceiling that itself allows only
            # read/discovery actions, so no future inline edit could add destructive reach.
            "HibernateAIBoundary": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": "HibernateAI-Boundary",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Effect": "Allow", "Action": [
                                "ec2:Describe*", "rds:Describe*", "rds:ListTagsForResource",
                                "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
                                "cloudwatch:ListMetrics"],
                             "Resource": "*"},
                        ],
                    },
                },
            },
            "HibernateAIRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": CROSS_ACCOUNT_ROLE,
                    "PermissionsBoundary": {"Ref": "HibernateAIBoundary"},
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        # Confused-deputy guard: only the HibernateAI Lambda's management account
                        # may assume this role, AND only from within our AWS Organization.
                        "Statement": [{"Effect": "Allow",
                                       "Principal": {"AWS": {"Fn::Sub": "arn:aws:iam::${ManagementAccountId}:root"}},
                                       "Action": "sts:AssumeRole",
                                       "Condition": {"StringEquals": {"aws:PrincipalOrgID": {"Fn::Sub": "${OrgId}"}}}}],
                    },
                    "Policies": [{
                        "PolicyName": "HibernateAIPolicy",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                # READ-ONLY discovery + CloudWatch metric reads only. Describe/List
                                # APIs cannot be ARN-scoped (no resource at list time), so Resource:*
                                # here is the intrinsic read-only case. No create/mutate/destructive/
                                # PassRole actions exist in this role.
                                {"Effect": "Allow", "Action": [
                                    "ec2:Describe*",
                                    "rds:Describe*", "rds:ListTagsForResource",
                                    "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
                                    "cloudwatch:ListMetrics",
                                ], "Resource": "*"},
                            ],
                        },
                    }],
                },
            }
        },
    }


# ── StackSets deployment path ───────────────────────
def get_stackset_template():
    """Same role template as get_cross_account_cfn_template(), for use as a StackSet.
    Deployed to selected OUs, it self-populates ManagementAccountId via the admin account.
    Least-privilege note: the cross-account policy is still Resource:"*" for discovery
    breadth; narrowing is tracked as future work."""
    return get_cross_account_cfn_template()


def deploy_stackset_to_ous(ou_ids, management_account_id=None, stackset_name="HibernateAI-CrossAccount",
                           regions=None, cfn_client=None):
    """Create/ensure a CloudFormation StackSet for the cross-account role and roll it out
    to the given OUs via SERVICE_MANAGED auto-deployment (delegated-admin or management
    account). Returns a dict describing the operation. Best-effort and idempotent-ish:
    create if absent, else create stack instances for the OUs.
    """
    cfn = cfn_client or boto3.client("cloudformation")
    mgmt = management_account_id or boto3.client("sts").get_caller_identity()["Account"]
    regions = regions or [os.environ.get("AWS_REGION", "us-east-1")]
    # Resolve the Organization ID for the trust-policy PrincipalOrgID guard.
    try:
        org_id = boto3.client("organizations").describe_organization()["Organization"]["Id"]
    except Exception as e:
        print(f"deploy_stackset: could not resolve Org ID: {type(e).__name__}: {e}")
        org_id = ""
    template_body = json.dumps(get_stackset_template())

    result = {"stackset": stackset_name, "ous": ou_ids, "regions": regions}
    try:
        cfn.describe_stack_set(StackSetName=stackset_name)
        result["created"] = False
    except Exception:
        cfn.create_stack_set(
            StackSetName=stackset_name,
            Description="HibernateAI cross-account role",
            TemplateBody=template_body,
            Parameters=[{"ParameterKey": "ManagementAccountId", "ParameterValue": mgmt},
                        {"ParameterKey": "OrgId", "ParameterValue": org_id}],
            PermissionModel="SERVICE_MANAGED",
            AutoDeployment={"Enabled": True, "RetainStacksOnAccountRemoval": False},
            Capabilities=["CAPABILITY_NAMED_IAM"],
        )
        result["created"] = True

    try:
        op = cfn.create_stack_instances(
            StackSetName=stackset_name,
            DeploymentTargets={"OrganizationalUnitIds": ou_ids},
            Regions=regions,
        )
        result["operation_id"] = op.get("OperationId")
        result["status"] = "rolling_out"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    return result


# ── Consolidated multi-account dashboard ────────────
def aggregate_all_accounts(discover_fn):
    """Aggregate per-account project discovery into one consolidated view.

    ``discover_fn`` is a callable that runs discovery in the CURRENT session's account
    (e.g. index.discover_projects). For each enabled account we assume the cross-account
    role, run discovery in that account's session, and merge results, prefixing project
    keys with the account id so identically-named projects don't collide.

    Returns {"accounts": [...], "projects": {..}, "errors": [...]}.
    """
    accounts = discover_all_accounts()
    merged = {}
    errors = []
    current_account = None
    try:
        current_account = boto3.client("sts").get_caller_identity()["Account"]
    except Exception:
        pass

    for acct in accounts:
        acct_id = acct.get("id") if isinstance(acct, dict) else str(acct)
        if not acct_id:
            continue
        try:
            if acct_id == current_account:
                projects = discover_fn()
            else:
                sess = get_cross_account_session(acct_id)
                if sess is None:
                    errors.append({"account": acct_id, "error": "assume-role failed"})
                    continue
                projects = discover_fn(session=sess)
        except TypeError:
            # discover_fn doesn't accept a session kwarg (single-account build): only the
            # current account can be aggregated.
            if acct_id == current_account:
                projects = discover_fn()
            else:
                errors.append({"account": acct_id, "error": "discovery not session-aware"})
                continue
        except Exception as e:
            errors.append({"account": acct_id, "error": str(e)})
            continue

        for pname, pdata in (projects or {}).items():
            key = f"{acct_id}:{pname}"
            entry = dict(pdata)
            entry["account_id"] = acct_id
            merged[key] = entry

    return {"accounts": accounts, "projects": merged, "errors": errors}
