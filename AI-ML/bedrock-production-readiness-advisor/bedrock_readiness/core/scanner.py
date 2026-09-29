# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AWS Account Scanner -- Read-only API calls to gather Bedrock deployment state.

All operations are non-destructive (describe, list, get only). Handles
permission errors gracefully -- marks checks as ERROR rather than crashing.

This scanner DOES inspect IAM policy documents (iam:GetPolicy / GetPolicyVersion
/ GetRolePolicy), guardrail configuration (bedrock:GetGuardrail), the account's
Zero Data Retention posture (bedrock:GetAccountDataRetention), and CloudTrail
coverage (cloudtrail:GetTrailStatus / GetEventSelectors) -- all Get/List/Describe
calls, never a mutation. This exists to support the Security, Guardrails, and
Data Governance pillars, on the same terms as every other pillar here: findings
are plain-text observations pointing at public AWS documentation, never a
deployable policy, template, or corrected configuration a customer could apply
as-is. See ../../README.md for the scope this package covers and does not.

Policy-document content is read only for roles this scanner has already
identified as Bedrock-trusted (via their AssumeRolePolicyDocument, itself
already returned for free by ListRoles) -- never for every role in the account,
and never for a bucket, key, or trail unrelated to a Bedrock resource.
"""

import boto3
import datetime
from botocore.exceptions import ClientError
from typing import Any, Optional
from .models import Config


# --- Declared API surface ------------------------------------------------------
#
# Every operation this package performs, declared beside the code that performs
# it. `--dry-run` renders these rather than a separately maintained list, so what
# the tool claims and what it does cannot drift apart.
#
# tests/test_readonly_surface.py derives the real call sites from source and
# asserts they match these tuples exactly, that nothing in
# NEVER_CALLED_OPERATIONS appears anywhere in the package, and that no mutating
# operation is called at all.
#
# Entries are (service, boto3_method, note).

READ_ONLY_OPERATIONS: tuple[tuple[str, str, str], ...] = (
    ("sts", "get_caller_identity", "resolve the account ID"),
    ("bedrock", "get_model_invocation_logging_configuration", ""),
    ("bedrock", "list_guardrails", "existence count only, never content-safety config"),
    ("bedrock", "list_custom_models", ""),
    ("bedrock", "list_foundation_models", "also used to pick a diagram-review model"),
    ("bedrock", "list_provisioned_model_throughputs", ""),
    ("bedrock", "list_inference_profiles", "also used to prefer a CRIS profile"),
    ("bedrock", "list_evaluation_jobs", ""),
    ("bedrock", "list_imported_models", ""),
    ("bedrock", "list_model_invocation_jobs", ""),
    ("bedrock-agent", "list_knowledge_bases", ""),
    ("bedrock-agent", "list_flows", ""),
    ("bedrock-agentcore-control", "list_agent_runtimes", ""),
    ("bedrock-agentcore-control", "list_gateways", ""),
    ("bedrock-agentcore-control", "list_memories", ""),
    ("bedrock-agentcore-control", "list_policies", ""),
    ("bedrock-agentcore-control", "list_workload_identities", ""),
    ("cloudwatch", "describe_alarms", ""),
    ("cloudwatch", "list_dashboards", ""),
    ("cloudwatch", "list_metrics", ""),
    ("cloudwatch", "get_metric_statistics", ""),
    ("cloudwatch", "get_metric_data", ""),
    ("logs", "describe_log_groups", ""),
    ("ec2", "describe_vpc_endpoints", ""),
    ("cloudtrail", "describe_trails", "existence only, never delivery status or selectors"),
    ("xray", "get_trace_segment_destination", ""),
    ("service-quotas", "list_service_quotas", ""),
    ("service-quotas", "list_requested_service_quota_change_history_by_quota", ""),
    ("ce", "get_cost_and_usage", ""),
    ("iam", "list_roles", "role names, attached-policy names, and (for free) trust policy"),
    ("iam", "list_attached_role_policies", "attached policy names and ARNs"),
    ("iam", "list_role_policies", "inline policy names only, for Bedrock-trusted roles"),
    ("iam", "get_role_policy", "inline policy document, for Bedrock-trusted roles only"),
    ("iam", "get_policy", "resolves a managed policy's current version ID"),
    ("iam", "get_policy_version", "managed policy document, for Bedrock-trusted roles only"),
    ("bedrock", "get_account_data_retention", "account-wide Zero Data Retention posture"),
    ("bedrock", "get_guardrail", "one guardrail's policy configuration and KMS key"),
    ("bedrock", "list_enforced_guardrails_configuration", "account/region enforced-guardrail config"),
    ("cloudtrail", "get_trail_status", "whether a trail is actively logging"),
    ("cloudtrail", "get_event_selectors", "whether a trail covers Bedrock data events"),
    ("s3", "get_bucket_encryption", "default encryption on the one bucket configured for "
                                     "Bedrock invocation-logging, if any"),
    ("s3", "get_public_access_block", "public-access settings on that same one bucket"),
)

# Performed only when a specific option is used.
CONDITIONAL_OPERATIONS: tuple[tuple[str, str, str], ...] = (
    ("sts", "assume_role", "only with --role-arn"),
    ("s3", "get_object", "only for an s3:// diagram, on that key only -- never put_object"),
    ("bedrock-runtime", "converse", "only when reviewing a diagram; inference, creates nothing"),
)

# Operations this package deliberately never performs, and the capability each
# would enable. Absent from the code, not merely unused -- there is no check here
# that would consume them.
#
# Note what moved off this list: iam:GetPolicy/GetPolicyVersion/GetRolePolicy,
# bedrock:GetAccountDataRetention, and cloudtrail:GetTrailStatus/GetEventSelectors
# are now called (see READ_ONLY_OPERATIONS above) to support the Security,
# Guardrails, and Data Governance pillars. What stays on this list is scoped
# deliberately narrower than "everything IAM/CloudTrail/KMS could offer" --
# each entry below would either read far more than a Bedrock-specific check
# needs, or duplicate a signal already available for free from a call already
# made.
NEVER_CALLED_OPERATIONS: tuple[tuple[str, str, str], ...] = (
    ("iam", "get_account_authorization_details",
     "a full paginated dump of every policy attached to every entity in the "
     "account -- SEC-02 reads policy documents only for the specific "
     "Bedrock-trusted roles this scanner already identified"),
    ("iam", "get_account_password_policy",
     "account-wide password policy -- identity hygiene unrelated to a "
     "specific Bedrock resource, out of scope for this package"),
    ("iam", "get_account_summary", "account-wide IAM entity counts, same reasoning"),
    ("s3", "get_bucket_policy", "a bucket's resource policy document -- DG-02 checks "
                                 "encryption and public-access block only, the two "
                                 "settings that most directly govern the invocation-log "
                                 "data itself"),
    ("s3", "get_bucket_policy_status", "same reasoning as get_bucket_policy"),
    ("kms", "describe_key", "a guardrail's encryption posture is read directly from "
                             "GetGuardrail's kmsKeyArn field, so no separate KMS call "
                             "is needed for DG-04"),
    ("kms", "get_key_rotation_status", "key rotation is a general-purpose AWS Config "
                                        "concern, not a Bedrock-specific readiness signal"),
)


def _has_bedrock_actions(policy_doc: dict) -> bool:
    """True if a policy document grants any bedrock/bedrock-runtime/
    bedrock-mantle action -- used to filter list_bedrock_role_policy_documents()
    down to policies actually relevant to Bedrock."""
    statements = policy_doc.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for stmt in statements:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        for action in actions:
            if any(action.lower().startswith(p) for p in
                   ("bedrock:", "bedrock-runtime:", "bedrock-mantle:")):
                return True
    return False


def _detect_bedrock_wildcards(policy_doc: dict) -> bool:
    """True if a policy document grants a literal `bedrock:*` /
    `bedrock-runtime:*` / `bedrock-mantle:*` wildcard action -- narrower and
    more directly Bedrock-relevant than a generic Action:"*" check."""
    statements = policy_doc.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for stmt in statements:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        for action in actions:
            if action in ("bedrock:*", "bedrock-runtime:*", "bedrock-mantle:*"):
                return True
    return False


def api_name(service: str, method: str) -> str:
    """Render a boto3 (service, method) pair as its API name, e.g. `bedrock:ListGuardrails`."""
    return f"{service}:{''.join(part.title() for part in method.split('_'))}"


class ScanError:
    """Represents a failed API call (permission denied, service unavailable)."""
    def __init__(self, service: str, operation: str, error: str):
        self.service = service
        self.operation = operation
        self.error = error

    def __repr__(self):
        return f"ScanError({self.service}:{self.operation} -> {self.error})"


class AccountScanner:
    """Scans an AWS account for Bedrock-related configuration using read-only calls."""

    def __init__(self, config: Config, region: Optional[str] = None):
        self.config = config
        self.region = region or config.regions[0]
        self.api_call_count = 0
        self._errors: list[ScanError] = []

        session_kwargs = {"region_name": self.region}
        if config.profile:
            session_kwargs["profile_name"] = config.profile

        self.session = boto3.Session(**session_kwargs)

        if config.role_arn:
            sts = self.session.client("sts")
            creds = sts.assume_role(
                RoleArn=config.role_arn,
                RoleSessionName="bedrock-readiness-only",
            )["Credentials"]
            self.session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=self.region,
            )

        self.account_id = self._get_account_id()

    def _get_account_id(self) -> str:
        sts = self.session.client("sts")
        self.api_call_count += 1
        return sts.get_caller_identity()["Account"]

    def _safe_call(self, service: str, method: str, **kwargs) -> Any:
        """Make a read-only AWS API call. Returns result or ScanError on failure.

        Client construction is inside the try/except on purpose: an older
        botocore that does not yet know a service (e.g. bedrock-agentcore-control
        on an outdated SDK) raises UnknownServiceError from session.client()
        BEFORE any call is made. Building the client here means that degrades to
        a single ScanError for that check -- never a crash of the whole scan,
        which is the graceful-degradation guarantee the rest of the tool relies
        on.
        """
        self.api_call_count += 1
        try:
            client = self.session.client(service)
            result = getattr(client, method)(**kwargs)
            if self.config.verbose:
                print(f"  check {service}:{method}")
            return result
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            err = ScanError(service, method, f"{error_code}: {error_msg}")
            self._errors.append(err)
            if self.config.verbose:
                print(f"  fail {service}:{method} -> {error_code}")
            return err
        except Exception as e:
            # type(e).__name__ only -- never the exception message. A
            # generic (non-ClientError) exception's message is unpredictable
            # and can carry ARNs, hostnames, or other detail from the
            # request; the type name alone is enough to explain the failure
            # in a Finding without risking any of that reaching the report.
            # This branch also catches UnknownServiceError / DataNotFoundError
            # from client construction on an SDK too old for a service.
            err = ScanError(service, method, type(e).__name__)
            self._errors.append(err)
            if self.config.verbose:
                print(f"  fail {service}:{method} -> {e}")
            return err

    @property
    def errors(self) -> list[ScanError]:
        return self._errors

    # --- Bedrock Core ----------------------------------------------------------

    def get_model_invocation_logging(self) -> Any:
        return self._safe_call("bedrock", "get_model_invocation_logging_configuration")

    def list_guardrails(self) -> list:
        """Guardrail summaries (id, name, status, version) -- one list call.
        Feeds both the existence check and get_guardrail_details() below."""
        resp = self._safe_call("bedrock", "list_guardrails")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("guardrails", [])

    def get_guardrail_details(self) -> list:
        """One GetGuardrail per guardrail this account has -- their policy
        configuration (content/topic/word/sensitive-information filters),
        status, version, and kmsKeyArn. Bounded by however many guardrails
        list_guardrails() found; never a blind loop over an unrelated resource
        set. A guardrail that fails to fetch is reported as a dict carrying its
        own error rather than dropping it from the list silently."""
        summaries = self.list_guardrails()
        if isinstance(summaries, ScanError):
            return summaries
        details = []
        for g in summaries:
            gid = g.get("id")
            if not gid:
                continue
            resp = self._safe_call("bedrock", "get_guardrail", guardrailIdentifier=gid)
            if isinstance(resp, ScanError):
                details.append({"id": gid, "name": g.get("name", gid), "error": resp.error})
            else:
                details.append(resp)
        return details

    def list_enforced_guardrails_configuration(self) -> Any:
        """Account/region-level automatic guardrail enforcement, if configured.
        A non-empty result is the strongest signal a guardrail actually applies
        to every invocation; an empty one means enforcement, if any, happens at
        the calling application's discretion and cannot be confirmed by a scan."""
        resp = self._safe_call("bedrock", "list_enforced_guardrails_configuration")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("guardrailsConfig", [])

    def get_account_data_retention(self) -> Any:
        """Account-wide data retention mode (none | default | inherit |
        provider_data_share). No input; a single account-level Get call."""
        return self._safe_call("bedrock", "get_account_data_retention")

    def list_custom_models(self) -> Any:
        resp = self._safe_call("bedrock", "list_custom_models")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("modelSummaries", [])

    def list_foundation_models(self) -> Any:
        resp = self._safe_call("bedrock", "list_foundation_models")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("modelSummaries", [])

    def list_knowledge_bases(self) -> Any:
        resp = self._safe_call("bedrock-agent", "list_knowledge_bases")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("knowledgeBaseSummaries", [])

    def list_provisioned_throughputs(self) -> Any:
        resp = self._safe_call("bedrock", "list_provisioned_model_throughputs")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("provisionedModelSummaries", [])

    def list_inference_profiles(self) -> Any:
        resp = self._safe_call("bedrock", "list_inference_profiles")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("inferenceProfileSummaries", [])

    def list_evaluation_jobs(self) -> Any:
        resp = self._safe_call("bedrock", "list_evaluation_jobs")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("jobSummaries", [])

    def list_flows(self) -> Any:
        resp = self._safe_call("bedrock-agent", "list_flows")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("flowSummaries", [])

    def list_imported_models(self) -> Any:
        resp = self._safe_call("bedrock", "list_imported_models")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("modelSummaries", [])

    def list_batch_inference_jobs(self, max_results: int = 10) -> Any:
        resp = self._safe_call("bedrock", "list_model_invocation_jobs", maxResults=max_results)
        if isinstance(resp, ScanError):
            return resp
        return resp.get("invocationJobSummaries", [])

    # --- AgentCore ---------------------------------------------------------------

    def list_agent_runtimes(self) -> Any:
        resp = self._safe_call("bedrock-agentcore-control", "list_agent_runtimes")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("agentRuntimes", [])

    def list_gateways(self) -> Any:
        resp = self._safe_call("bedrock-agentcore-control", "list_gateways")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("gateways", [])

    def list_memories(self) -> Any:
        resp = self._safe_call("bedrock-agentcore-control", "list_memories")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("memories", [])

    def list_policies(self) -> Any:
        resp = self._safe_call("bedrock-agentcore-control", "list_policies")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("policies", [])

    def list_workload_identities(self) -> Any:
        resp = self._safe_call("bedrock-agentcore-control", "list_workload_identities")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("workloadIdentities", [])

    # --- CloudWatch ----------------------------------------------------------------

    def list_alarms(self) -> list:
        resp = self._safe_call("cloudwatch", "describe_alarms", MaxRecords=100)
        if isinstance(resp, ScanError):
            return resp
        return resp.get("MetricAlarms", [])

    def list_dashboards(self) -> list:
        resp = self._safe_call("cloudwatch", "list_dashboards")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("DashboardEntries", [])

    def list_log_groups(self, prefix: str = "") -> Any:
        kwargs = {"limit": 50}
        if prefix:
            kwargs["logGroupNamePrefix"] = prefix
        resp = self._safe_call("logs", "describe_log_groups", **kwargs)
        if isinstance(resp, ScanError):
            return resp
        return resp.get("logGroups", [])

    def get_metric_statistics(self, namespace: str, metric_name: str,
                               dimensions: list, period: int = 86400,
                               hours: int = 168) -> Any:
        end = datetime.datetime.utcnow()
        start = end - datetime.timedelta(hours=hours)
        return self._safe_call(
            "cloudwatch", "get_metric_statistics",
            Namespace=namespace, MetricName=metric_name,
            Dimensions=dimensions, StartTime=start, EndTime=end,
            Period=period, Statistics=["Sum", "Average", "Maximum"],
        )

    def get_metric_data_sum(self, namespace: str, metric_name: str,
                             dimensions: Optional[list] = None,
                             period: int = 3600, hours: int = 168) -> Any:
        end_time = datetime.datetime.now(datetime.timezone.utc)
        start_time = end_time - datetime.timedelta(hours=hours)
        metric = {"Namespace": namespace, "MetricName": metric_name}
        if dimensions:
            metric["Dimensions"] = dimensions
        return self._safe_call(
            "cloudwatch", "get_metric_data",
            MetricDataQueries=[{
                "Id": "m1",
                "MetricStat": {"Metric": metric, "Period": period, "Stat": "Sum"},
            }],
            StartTime=start_time,
            EndTime=end_time,
        )

    def list_metrics(self, namespace: str, metric_name: str = "") -> Any:
        kwargs = {"Namespace": namespace}
        if metric_name:
            kwargs["MetricName"] = metric_name
        resp = self._safe_call("cloudwatch", "list_metrics", **kwargs)
        if isinstance(resp, ScanError):
            return resp
        return resp.get("Metrics", [])

    # --- IAM (existence/role-name summary only -- NOT policy document inspection) ---

    # Service principals that mark a role as Bedrock-trusted for
    # list_bedrock_role_trust_info() / SEC-03. Checked against each role's
    # AssumeRolePolicyDocument, which ListRoles already returns -- so this
    # costs nothing beyond the paginated ListRoles call made below.
    _BEDROCK_SERVICE_PRINCIPALS = (
        "bedrock.amazonaws.com",
        "bedrock-agentcore.amazonaws.com",
        "bedrock-agentcore-control.amazonaws.com",
    )

    def list_roles_with_bedrock(self) -> list:
        """IAM roles with a Bedrock-, admin-, or fullaccess-named policy
        attached (by policy NAME only -- SEC-01's broad-managed-policy check).

        One row per (role, attached policy) pair, matching this shape
        historically. Deliberately does not fetch policy document content --
        that is list_bedrock_role_policy_documents() below.
        """
        iam = self.session.client("iam")
        roles = []
        try:
            self.api_call_count += 1
            paginator = iam.get_paginator("list_roles")
            for page in paginator.paginate():
                for role in page["Roles"]:
                    self.api_call_count += 1
                    policies = iam.list_attached_role_policies(RoleName=role["RoleName"])
                    for pol in policies.get("AttachedPolicies", []):
                        pol_name_lower = pol["PolicyName"].lower()
                        if any(k in pol_name_lower for k in ["bedrock", "admin", "fullaccess"]):
                            roles.append({
                                "RoleName": role["RoleName"],
                                "PolicyName": pol["PolicyName"],
                                "Arn": role["Arn"],
                            })
        except Exception as e:
            self._errors.append(ScanError("iam", "list_roles", type(e).__name__))
        return roles

    def list_bedrock_role_trust_info(self) -> list:
        """Trust-policy scoping signal for SEC-03, read from
        AssumeRolePolicyDocument -- which the ListRoles call above already
        returns for free, so this makes no additional API call of its own.

        Returns one row per role actually trusted by a Bedrock-family service
        principal: {RoleName, Arn, TrustedServices, TrustPrincipalWildcard}.
        Kept separate from list_roles_with_bedrock() above so that method's
        established shape (one row per attached policy, matched by name) is
        untouched; this is an additive, more precise signal for SEC-03 only.
        """
        iam = self.session.client("iam")
        trust_info = []
        try:
            self.api_call_count += 1
            paginator = iam.get_paginator("list_roles")
            for page in paginator.paginate():
                for role in page["Roles"]:
                    trust_doc = role.get("AssumeRolePolicyDocument")
                    trusted_by = self._bedrock_trust_principals(trust_doc)
                    if not trusted_by:
                        continue
                    trust_info.append({
                        "RoleName": role["RoleName"],
                        "Arn": role["Arn"],
                        "TrustedServices": sorted(trusted_by),
                        "TrustPrincipalWildcard": self._trust_has_wildcard_principal(trust_doc),
                    })
        except Exception as e:
            self._errors.append(ScanError("iam", "list_roles", type(e).__name__))
        return trust_info

    @staticmethod
    def _bedrock_trust_principals(trust_doc: Optional[dict]) -> set:
        """Which Bedrock-family service principals this trust policy names."""
        found = set()
        if not isinstance(trust_doc, dict):
            return found
        statements = trust_doc.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        for stmt in statements:
            principal = stmt.get("Principal", {})
            services = principal.get("Service", []) if isinstance(principal, dict) else []
            if isinstance(services, str):
                services = [services]
            for svc in services:
                if svc in AccountScanner._BEDROCK_SERVICE_PRINCIPALS:
                    found.add(svc)
        return found

    @staticmethod
    def _trust_has_wildcard_principal(trust_doc: Optional[dict]) -> bool:
        """True if any statement trusts Principal "*" -- any AWS principal, not
        just the intended service. A confused-deputy / cross-account exposure
        signal for SEC-03, read from data already in hand."""
        if not isinstance(trust_doc, dict):
            return False
        statements = trust_doc.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        for stmt in statements:
            principal = stmt.get("Principal")
            if principal == "*":
                return True
            if isinstance(principal, dict):
                aws_principal = principal.get("AWS")
                if aws_principal == "*" or aws_principal == ["*"]:
                    return True
        return False

    def list_bedrock_role_policy_documents(self) -> Any:
        """Policy documents that grant a Bedrock-family action, for roles
        trusted by a Bedrock service principal only -- narrower than reading
        every role in the account, since list_bedrock_role_trust_info() above
        already identified which roles are Bedrock-relevant at no extra API
        cost. An AWS-managed policy's content is public and identical for
        every account, so it is not re-read.

        Returns a flat list, one entry per (role, policy): {role, arn, policy,
        type ("attached"|"inline"), wildcards}. `wildcards` is precomputed
        here -- True only for a literal `bedrock:*` / `bedrock-runtime:*` /
        `bedrock-mantle:*` action, not a generic `Action:"*"` -- so SEC-02 can
        report specific over-permissioned roles without re-parsing policy JSON
        itself. A document that fails to fetch is recorded with an "error" key
        rather than dropped, so a permission gap is visible.
        """
        trust_info = self.list_bedrock_role_trust_info()
        if isinstance(trust_info, ScanError) or not trust_info:
            return []

        iam = self.session.client("iam")
        out = []
        for role in trust_info:
            role_name = role["RoleName"]
            try:
                self.api_call_count += 1
                attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
                self.api_call_count += 1
                inline_names = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
            except Exception as e:
                out.append({"role": role_name, "arn": role["Arn"], "error": type(e).__name__})
                continue

            for pol in attached:
                arn = pol.get("PolicyArn", "")
                if ":aws:policy/" in arn:
                    continue  # AWS-managed -- public, well-known, not re-read
                try:
                    self.api_call_count += 1
                    version_id = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
                    self.api_call_count += 1
                    doc = iam.get_policy_version(
                        PolicyArn=arn, VersionId=version_id
                    )["PolicyVersion"]["Document"]
                except Exception as e:
                    out.append({"role": role_name, "arn": role["Arn"], "policy": pol["PolicyName"],
                                "type": "attached", "error": type(e).__name__})
                    continue
                if _has_bedrock_actions(doc):
                    out.append({"role": role_name, "arn": role["Arn"], "policy": pol["PolicyName"],
                                "type": "attached", "wildcards": _detect_bedrock_wildcards(doc)})

            for policy_name in inline_names:
                try:
                    self.api_call_count += 1
                    doc = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)["PolicyDocument"]
                except Exception as e:
                    out.append({"role": role_name, "arn": role["Arn"], "policy": policy_name,
                                "type": "inline", "error": type(e).__name__})
                    continue
                if _has_bedrock_actions(doc):
                    out.append({"role": role_name, "arn": role["Arn"], "policy": policy_name,
                                "type": "inline", "wildcards": _detect_bedrock_wildcards(doc)})
        return out

    # --- VPC / PrivateLink -----------------------------------------------------------

    def list_vpc_endpoints(self, service_filter: str = "bedrock") -> Any:
        resp = self._safe_call("ec2", "describe_vpc_endpoints")
        if isinstance(resp, ScanError):
            return resp
        return [ep for ep in resp.get("VpcEndpoints", [])
                if service_filter in ep.get("ServiceName", "")]

    # --- CloudTrail (existence/status only -- NOT data-event inspection) -------------

    def list_trails(self) -> Any:
        resp = self._safe_call("cloudtrail", "describe_trails")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("trailList", [])

    def get_trail_details(self) -> Any:
        """Per-trail logging status and event-selector coverage, for every
        trail describe_trails() found. Bounded by the account's actual trail
        count -- typically one or a small number, never an unrelated resource
        set. A trail whose status/selectors could not be read is reported as a
        dict carrying its own error rather than being dropped from the list."""
        trails = self.list_trails()
        if isinstance(trails, ScanError):
            return trails
        details = []
        for t in trails:
            name = t.get("Name") or t.get("TrailARN")
            if not name:
                continue
            status = self._safe_call("cloudtrail", "get_trail_status", Name=name)
            selectors = self._safe_call("cloudtrail", "get_event_selectors", TrailName=name)
            details.append({
                "Name": t.get("Name"),
                "TrailARN": t.get("TrailARN"),
                "IsMultiRegionTrail": t.get("IsMultiRegionTrail", False),
                "IsLogging": (None if isinstance(status, ScanError)
                              else status.get("IsLogging")),
                "StatusError": status.error if isinstance(status, ScanError) else None,
                "EventSelectors": ([] if isinstance(selectors, ScanError)
                                    else selectors.get("EventSelectors", [])),
                "AdvancedEventSelectors": ([] if isinstance(selectors, ScanError)
                                           else selectors.get("AdvancedEventSelectors", [])),
                "SelectorsError": selectors.error if isinstance(selectors, ScanError) else None,
            })
        return details

    # --- X-Ray ----------------------------------------------------------------------------

    def get_trace_segment_destination(self) -> Any:
        return self._safe_call("xray", "get_trace_segment_destination")

    # --- Service Quotas ----------------------------------------------------------------

    def list_service_quotas(self) -> Any:
        resp = self._safe_call("service-quotas", "list_service_quotas", ServiceCode="bedrock")
        if isinstance(resp, ScanError):
            return resp
        return resp.get("Quotas", [])

    def list_quota_change_history(self) -> Any:
        resp = self._safe_call(
            "service-quotas", "list_requested_service_quota_change_history_by_quota",
            ServiceCode="bedrock",
        )
        if isinstance(resp, ScanError):
            return resp
        return resp.get("RequestedQuotas", [])

    # --- Cost Explorer -------------------------------------------------------------------

    def get_bedrock_cost_and_usage(self, days: int = 90) -> Any:
        ce = self.session.client("ce", region_name="us-east-1")
        self.api_call_count += 1
        end_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            return ce.get_cost_and_usage(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
            )
        except Exception as e:
            err = ScanError("ce", "get_cost_and_usage", type(e).__name__)
            self._errors.append(err)
            return err

    # --- S3 (one bucket only: the Bedrock invocation-logging destination) ------------

    def get_logging_destination_security(self, invocation_logging: Any) -> Any:
        """Encryption and public-access settings for the ONE S3 bucket
        configured as the Bedrock invocation-logging destination, if any.

        Takes the already-fetched invocation-logging config rather than
        re-deriving it, so this makes zero calls when no S3 destination is
        configured, and exactly two when one is -- never a bucket enumeration,
        never any bucket other than the one Bedrock itself is told to write to.
        """
        if isinstance(invocation_logging, ScanError):
            # Propagate the failure rather than collapsing it into None --
            # None means "no S3 destination is configured" (a benign,
            # legitimate state DG-02/DG-03 skip on), which is not the same
            # thing as "could not determine whether one is configured".
            # Conflating the two would report a clean skip when the read
            # actually failed.
            return invocation_logging
        bucket = (invocation_logging.get("loggingConfig", {})
                                    .get("s3Config", {})
                                    .get("bucketName"))
        if not bucket:
            return None

        enc = self._safe_call("s3", "get_bucket_encryption", Bucket=bucket)
        pab = self._safe_call("s3", "get_public_access_block", Bucket=bucket)

        rules = []
        if not isinstance(enc, ScanError):
            rules = enc.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        block_cfg = (pab.get("PublicAccessBlockConfiguration", {})
                     if not isinstance(pab, ScanError) else {})

        return {
            "BucketName": bucket,
            "EncryptionRules": rules,
            "EncryptionError": enc.error if isinstance(enc, ScanError) else None,
            "PublicAccessBlock": block_cfg,
            "PublicAccessBlockError": pab.error if isinstance(pab, ScanError) else None,
        }

    # --- Convenience: full scan (of the pillars this package assesses) -----------------

    def scan_all(self) -> dict:
        """Run all scans and return combined results dict."""
        invocation_logging = self.get_model_invocation_logging()
        bedrock_roles = self.list_roles_with_bedrock()

        return {
            "account_id": self.account_id,
            "region": self.region,
            "bedrock": {
                "invocation_logging": invocation_logging,
                "guardrails": self.list_guardrails(),
                "guardrail_details": self.get_guardrail_details(),
                "enforced_guardrails": self.list_enforced_guardrails_configuration(),
                "data_retention": self.get_account_data_retention(),
                "custom_models": self.list_custom_models(),
                "foundation_models": self.list_foundation_models(),
                "knowledge_bases": self.list_knowledge_bases(),
                "provisioned_throughputs": self.list_provisioned_throughputs(),
                "inference_profiles": self.list_inference_profiles(),
                "evaluation_jobs": self.list_evaluation_jobs(),
                "flows": self.list_flows(),
                "imported_models": self.list_imported_models(),
                "batch_inference_jobs": self.list_batch_inference_jobs(),
            },
            "agentcore": {
                "runtimes": self.list_agent_runtimes(),
                "gateways": self.list_gateways(),
                "memories": self.list_memories(),
                "policies": self.list_policies(),
                "workload_identities": self.list_workload_identities(),
            },
            "cloudwatch": {
                "alarms": self.list_alarms(),
                "dashboards": self.list_dashboards(),
                "bedrock_log_groups": self.list_log_groups("/aws/bedrock"),
                "agentcore_log_groups": self.list_log_groups("/aws/bedrock-agentcore"),
            },
            "iam": {
                "bedrock_roles": bedrock_roles,
                "bedrock_role_trust_info": self.list_bedrock_role_trust_info(),
                "bedrock_role_policy_documents": self.list_bedrock_role_policy_documents(),
            },
            "vpc": {
                "bedrock_endpoints": self.list_vpc_endpoints("bedrock"),
            },
            "cloudtrail": {
                "trails": self.list_trails(),
                "trail_details": self.get_trail_details(),
            },
            "s3": {
                "logging_destination_security": self.get_logging_destination_security(
                    invocation_logging
                ),
            },
            "xray": {
                "trace_destination": self.get_trace_segment_destination(),
            },
            "quotas": {
                "service_quotas": self.list_service_quotas(),
                "change_history": self.list_quota_change_history(),
            },
            "cost": {
                "bedrock_cost_and_usage": self.get_bedrock_cost_and_usage(),
            },
        }
