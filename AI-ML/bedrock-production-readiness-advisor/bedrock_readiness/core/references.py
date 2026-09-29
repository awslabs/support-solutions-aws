# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Curated AWS documentation references, keyed by check ID.

Turns a one-line recommendation into something a builder can act on, by
pointing at the official AWS page that explains how. Findings stay plain prose;
these links are attached at render time.

WHY THIS IS A STATIC MAP AND NOT MODEL OUTPUT
---------------------------------------------
No link in this file comes from a model, and no code path lets a model add one:

  * `Finding` has no URL field. A pillar module cannot attach a link even by
    accident, and the diagram-review model cannot emit one -- its output is
    parsed into `Finding` objects, which have nowhere to put a URL.
  * Renderers look links up from this map by `check_id`. The lookup key is a
    check ID the code itself generated, so an unmapped or invented ID simply
    returns nothing.

That matters for two reasons. A model asked for documentation links will
hallucinate plausible-looking URLs, and it may well reach for a security page
when the finding is adjacent to one. Both risks disappear if links can only
ever come from a human-curated file.

SCOPE RULES (enforced by `validate()`, asserted in tests/test_references.py)
----------------------------------------------------------------------------
  1. HTTPS only.
  2. AWS-owned domains only. Recommendations must resolve to official AWS
     documentation, so no third-party blogs, Medium, Stack Overflow, or vendor
     sites -- however good the article.
  3. Security/identity/encryption/guardrails/data-governance pages are
     restricted to the pillars that legitimately cover those topics --
     Security, Guardrails, and Data Governance. A page like that attached to
     an Observability or Cost check would reintroduce, by reference, guidance
     this package's other six pillars do not cover; attached to a Security,
     Guardrails, or Data Governance check it is simply the documentation for
     that check. RESTRICTED_TOPIC_PILLARS (below) names the three pillars this
     applies to; CHECK_PREFIX_TO_PILLAR maps every check ID to its pillar so
     `validate()` can tell which rule applies.
  4. No compliance or regulated-data pages, and no title that implies a
     compliance outcome (e.g. "HIPAA-compliant") -- for ANY pillar, including
     Security/Guardrails/Data Governance. Explaining an audit-logging or
     encryption *mechanism* is in scope; asserting a *compliance conclusion*
     never is.
  5. Every key must be a check ID this package actually implements.

Pointing at official documentation is also the distinction that keeps this
reference content rather than a remediation artifact: the tool explains what to
change and where AWS documents it, and never ships something to apply.
"""

from dataclasses import dataclass

ALLOWED_DOC_HOSTS = (
    "docs.aws.amazon.com",
    "aws.amazon.com",
)

# Pillars allowed to cite a page containing a RESTRICTED_TOPIC_FRAGMENTS /
# RESTRICTED_TOPIC_TERMS match. Every other pillar is still barred from
# linking to one -- rule 3 above.
RESTRICTED_TOPIC_PILLARS = {"security", "guardrails", "data_governance"}

CHECK_PREFIX_TO_PILLAR = {
    "OBS": "observability",
    "ARCH": "architecture",
    "QC": "quota_capacity",
    "COST": "cost_optimization",
    "MF": "model_fitness",
    "SEC": "security",
    "GR": "guardrails",
    "DG": "data_governance",
}


def pillar_for(owner: str) -> str:
    """Resolve an `owner` key (a check ID, or the `pillar:<name>` fallback-map
    key) to its pillar name. Public: tests/test_references.py uses this to
    scope the excluded-topic check to the pillars it should apply to."""
    if owner.startswith("pillar:"):
        return owner.split(":", 1)[1]
    return CHECK_PREFIX_TO_PILLAR.get(owner.split("-")[0], "")


# Path fragments that mark a page as covering a topic restricted to
# RESTRICTED_TOPIC_PILLARS. Checked against the URL as well as the title,
# because a security page can have an innocuous-sounding title.
RESTRICTED_TOPIC_FRAGMENTS = (
    "security", "encryption", "encrypt", "kms", "key-management",
    "iam", "identity", "auth", "credential", "secret",
    "guardrail", "content-filter", "prompt-injection",
    "data-protection", "data-retention", "privacy", "pii",
    "privatelink", "vpc-endpoint", "vpc-interface",
    "cloudtrail", "audit",
    "threat", "vulnerability", "penetration",
)

RESTRICTED_TOPIC_TERMS = (
    "secur", "encrypt", "kms", "iam", "identity", "authentic", "authoriz",
    "credential", "secret", "guardrail", "content filter", "prompt injection",
    "data protection", "data retention", "pii", "phi", "privatelink",
    "vpc endpoint", "audit", "cloudtrail", "threat", "vulnerab",
)

# Forbidden for EVERY pillar, with no exception -- a compliance-outcome claim
# is never something this tool makes, regardless of which check it would
# attach to.
ALWAYS_FORBIDDEN_URL_FRAGMENTS = (
    "compliance", "hipaa", "pci", "gdpr", "fedramp", "sox",
)

ALWAYS_FORBIDDEN_TITLE_TERMS = (
    "compliance", "compliant", "hipaa", "pci", "gdpr",
)


@dataclass(frozen=True)
class Reference:
    """A single AWS documentation link."""
    title: str
    url: str


# --- Reference library -------------------------------------------------------
# Defined once and reused across checks that share a remediation path, so a
# URL change is a one-line edit.

_INVOCATION_LOGGING = Reference(
    "Monitor model invocation using CloudWatch Logs and Amazon S3",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html",
)
_RUNTIME_METRICS = Reference(
    "Monitor bedrock-runtime inference using CloudWatch metrics",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html",
)
_MONITORING_OVERVIEW = Reference(
    "Monitor the bedrock-runtime endpoint",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring.html",
)
_CW_ALARMS = Reference(
    "Using Amazon CloudWatch alarms",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html",
)
_CW_DASHBOARDS = Reference(
    "Using Amazon CloudWatch dashboards",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Dashboards.html",
)
_CW_LOG_GROUPS = Reference(
    "Working with log groups and log streams",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html",
)
_AGENTCORE_OBS = Reference(
    "Get started with AgentCore Observability",
    "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html",
)
_EVALUATIONS = Reference(
    "Evaluate the performance of Amazon Bedrock resources",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/evaluation.html",
)
_KB_SYNC = Reference(
    "Sync your data source with your Amazon Bedrock knowledge base",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html",
)
_CRIS = Reference(
    "Increase throughput with cross-Region inference",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html",
)
_CRIS_GLOBAL = Reference(
    "Global cross-Region inference",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html",
)
_INFERENCE_PROFILES = Reference(
    "Set up a model invocation resource using inference profiles",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles.html",
)
_SDK_RETRY = Reference(
    "Retry behavior in the AWS SDKs and Tools Reference Guide",
    "https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html",
)
_WA_RETRY = Reference(
    "REL05-BP03 Control and limit retry calls (Well-Architected)",
    "https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_limit_retries.html",
)
_WA_QUEUE = Reference(
    "REL05-BP02 Throttle requests (Well-Architected)",
    "https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_mitigate_interaction_failure_throttle_requests.html",
)
_PROV_THROUGHPUT = Reference(
    "Increase model invocation capacity with Provisioned Throughput",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/prov-throughput.html",
)
_RUNTIME_QUOTAS = Reference(
    "Quotas for the bedrock-runtime endpoint",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html",
)
_QUOTA_INCREASE = Reference(
    "Requesting a quota increase",
    "https://docs.aws.amazon.com/servicequotas/latest/userguide/request-quota-increase.html",
)
_MODEL_ACCESS = Reference(
    "Add or remove access to Amazon Bedrock foundation models",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-access-modify.html",
)
_BATCH_INFERENCE = Reference(
    "Process multiple prompts with batch inference",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/batch-inference.html",
)
_PROMPT_ROUTING = Reference(
    "Understanding intelligent prompt routing in Amazon Bedrock",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html",
)
_PRICING = Reference(
    "Amazon Bedrock pricing",
    "https://aws.amazon.com/bedrock/pricing/",
)
_BUDGETS = Reference(
    "Managing your costs with AWS Budgets",
    "https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html",
)
_COST_EXPLORER = Reference(
    "Analyzing your costs with AWS Cost Explorer",
    "https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html",
)
_MODEL_LIFECYCLE = Reference(
    "Amazon Bedrock foundation model lifecycle",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html",
)
_MODEL_IDS = Reference(
    "Supported foundation models in Amazon Bedrock",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html",
)
_FLOWS = Reference(
    "Build an end-to-end generative AI workflow with Amazon Bedrock Flows",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/flows.html",
)
_AGENTCORE_RUNTIME = Reference(
    "Amazon Bedrock AgentCore Runtime",
    "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime.html",
)
_AGENTCORE_GATEWAY = Reference(
    "Amazon Bedrock AgentCore Gateway",
    "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html",
)
_AGENTCORE_MEMORY = Reference(
    "Amazon Bedrock AgentCore Memory",
    "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html",
)
_XRAY_TXN_SEARCH = Reference(
    "Transaction Search in CloudWatch",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search.html",
)
_GENAI_LENS = Reference(
    "Generative AI Lens (AWS Well-Architected)",
    "https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/generative-ai-lens.html",
)

# Security, Guardrails, Data Governance -- restricted-topic pages, citable
# only by checks in those three pillars (enforced by validate() above).
_IAM_LEAST_PRIVILEGE = Reference(
    "Prepare for least-privilege permissions",
    "https://docs.aws.amazon.com/IAM/latest/UserGuide/getting-started-reduce-permissions.html",
)
_WA_LEAST_PRIVILEGE = Reference(
    "SEC03-BP02 Grant least privilege access (Well-Architected)",
    "https://docs.aws.amazon.com/wellarchitected/2023-04-10/framework/sec_permissions_least_privileges.html",
)
_CLOUDTRAIL_DATA_EVENTS = Reference(
    "Logging data events with AWS CloudTrail",
    "https://docs.aws.amazon.com/awscloudtrail/latest/userguide/logging-data-events-with-cloudtrail.html",
)
_GUARDRAILS_OVERVIEW = Reference(
    "Detect and filter harmful content by using Amazon Bedrock Guardrails",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html",
)
_GUARDRAILS_COMPONENTS = Reference(
    "Create your guardrail (content, topic, word, and sensitive-information filters)",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-components.html",
)
_DATA_RETENTION = Reference(
    "Data retention in Amazon Bedrock",
    "https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html",
)
_ZDR_BLOG = Reference(
    "Enforce zero data retention on Amazon Bedrock (AWS Security Blog)",
    "https://aws.amazon.com/blogs/security/enforce-zero-data-retention-on-amazon-bedrock-with-bedrock-projects-and-service-control-policies/",
)
_S3_BUCKET_ENCRYPTION = Reference(
    "Amazon S3 default encryption for buckets",
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-encryption.html",
)
_S3_BLOCK_PUBLIC_ACCESS = Reference(
    "Blocking public access to your Amazon S3 storage",
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html",
)
_CW_LOGS_KMS = Reference(
    "Encrypt log data in CloudWatch Logs using AWS KMS",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html",
)


# --- Per-check mapping -------------------------------------------------------

REFERENCES: dict[str, tuple[Reference, ...]] = {
    # Observability
    "OBS-01": (_INVOCATION_LOGGING, _RUNTIME_METRICS),
    "OBS-02": (_CW_DASHBOARDS, _RUNTIME_METRICS),
    "OBS-03": (_CW_ALARMS, _RUNTIME_METRICS),
    "OBS-04": (_CW_ALARMS, _RUNTIME_METRICS),
    "OBS-05": (_CW_ALARMS, _RUNTIME_QUOTAS),
    "OBS-06": (_CW_ALARMS, _BUDGETS),
    "OBS-07": (_XRAY_TXN_SEARCH, _AGENTCORE_OBS),
    "OBS-08": (_INVOCATION_LOGGING, _CW_LOG_GROUPS),
    "OBS-09": (_AGENTCORE_OBS,),
    "OBS-10": (_AGENTCORE_OBS, _XRAY_TXN_SEARCH),
    "OBS-11": (_KB_SYNC, _CW_ALARMS),
    "OBS-12": (_EVALUATIONS,),
    "OBS-13": (_EVALUATIONS, _AGENTCORE_OBS),

    # Architecture & Resilience
    "ARCH-01": (_CRIS, _INFERENCE_PROFILES),
    "ARCH-02": (_MODEL_IDS, _GENAI_LENS),
    "ARCH-03": (_SDK_RETRY, _WA_RETRY),
    "ARCH-04": (_WA_QUEUE, _GENAI_LENS),
    "ARCH-05": (_MODEL_IDS, _PRICING),
    "ARCH-06": (_AGENTCORE_RUNTIME,),
    "ARCH-07": (_AGENTCORE_GATEWAY,),
    "ARCH-08": (_AGENTCORE_MEMORY,),
    "ARCH-09": (_KB_SYNC,),
    "ARCH-10": (_FLOWS,),

    # Quota & Capacity
    "QC-01": (_RUNTIME_QUOTAS, _QUOTA_INCREASE),
    "QC-02": (_RUNTIME_QUOTAS, _SDK_RETRY),
    "QC-03": (_RUNTIME_QUOTAS, _RUNTIME_METRICS),
    "QC-04": (_CW_ALARMS, _RUNTIME_QUOTAS),
    "QC-05": (_PROV_THROUGHPUT,),
    "QC-06": (_CRIS, _CRIS_GLOBAL),
    "QC-07": (_MODEL_ACCESS,),
    "QC-08": (_QUOTA_INCREASE, _RUNTIME_QUOTAS),
    "QC-09": (_BATCH_INFERENCE,),
    "QC-10": (_AGENTCORE_RUNTIME, _QUOTA_INCREASE),
    "QC-11": (_CRIS, _CRIS_GLOBAL),

    # Cost Optimization
    "COST-01": (_INVOCATION_LOGGING, _RUNTIME_METRICS),
    "COST-02": (_PRICING, _MODEL_IDS),
    "COST-03": (_PROMPT_ROUTING,),
    "COST-04": (_BATCH_INFERENCE, _PRICING),
    "COST-05": (_PROV_THROUGHPUT, _RUNTIME_METRICS),
    "COST-06": (_BUDGETS, _CW_ALARMS),
    "COST-07": (_KB_SYNC, _PRICING),
    "COST-08": (_BATCH_INFERENCE, _PRICING),
    "COST-09": (_COST_EXPLORER, _BUDGETS),

    # Model Fitness
    "MF-01": (_MODEL_IDS, _GENAI_LENS),
    "MF-02": (_PRICING, _MODEL_IDS),
    "MF-03": (_MODEL_LIFECYCLE, _MODEL_IDS),
    "MF-04": (_CRIS, _CRIS_GLOBAL),

    # Security
    "SEC-01": (_IAM_LEAST_PRIVILEGE, _WA_LEAST_PRIVILEGE),
    "SEC-02": (_WA_LEAST_PRIVILEGE, _IAM_LEAST_PRIVILEGE),
    "SEC-03": (_IAM_LEAST_PRIVILEGE, _WA_LEAST_PRIVILEGE),
    "SEC-04": (_CLOUDTRAIL_DATA_EVENTS,),

    # Guardrails
    "GR-01": (_GUARDRAILS_OVERVIEW, _GUARDRAILS_COMPONENTS),
    "GR-02": (_GUARDRAILS_OVERVIEW,),
    "GR-03": (_GUARDRAILS_COMPONENTS,),
    "GR-04": (_GUARDRAILS_OVERVIEW,),

    # Data Governance
    "DG-01": (_DATA_RETENTION, _ZDR_BLOG),
    "DG-02": (_S3_BUCKET_ENCRYPTION,),
    "DG-03": (_S3_BLOCK_PUBLIC_ACCESS,),
    "DG-04": (_CW_LOGS_KMS,),
}

# Fallback for design findings from the diagram review, which carry
# `DGM-<CATEGORY>-NN` IDs rather than a specific check ID. Security,
# Guardrails, and Data Governance are absent here on purpose: the diagram
# review's own scope filter (core/diagram_review.py) never allows a finding in
# those categories through, so there is no design finding this fallback would
# ever need to resolve for them. See core/models.py's READINESS_CAPABILITIES
# comment for why the diagram-review boundary was not moved along with the
# account-scan pillars.
PILLAR_REFERENCES: dict[str, tuple[Reference, ...]] = {
    "observability": (_MONITORING_OVERVIEW, _RUNTIME_METRICS),
    "architecture": (_GENAI_LENS, _CRIS),
    "quota_capacity": (_RUNTIME_QUOTAS, _QUOTA_INCREASE),
    "cost_optimization": (_PRICING, _COST_EXPLORER),
    "model_fitness": (_MODEL_IDS, _MODEL_LIFECYCLE),
}


def for_check(check_id: str) -> tuple[Reference, ...]:
    """Return the curated references for a check ID, or an empty tuple.

    Design findings (`DGM-<CATEGORY>-NN`) fall back to their pillar's general
    references. An unknown ID returns nothing rather than guessing.
    """
    if not check_id:
        return ()
    if check_id in REFERENCES:
        return REFERENCES[check_id]
    if check_id.startswith("DGM-"):
        parts = check_id.split("-")
        if len(parts) >= 3:
            category = "_".join(parts[1:-1]).lower()
            return PILLAR_REFERENCES.get(category, ())
    return ()


def validate(known_check_ids: set | None = None) -> list[str]:
    """Check every reference against the scope rules. Returns problem strings.

    Called by tests/test_references.py. Kept in this module so the rules live
    next to the data they constrain.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}

    all_refs: list[tuple[str, Reference]] = []
    for check_id, refs in REFERENCES.items():
        all_refs.extend((check_id, r) for r in refs)
    for pillar, refs in PILLAR_REFERENCES.items():
        all_refs.extend((f"pillar:{pillar}", r) for r in refs)

    for owner, ref in all_refs:
        if not ref.url.startswith("https://"):
            problems.append(f"{owner}: not HTTPS -- {ref.url}")
            continue

        host = ref.url.split("//", 1)[1].split("/", 1)[0].lower()
        if host not in ALLOWED_DOC_HOSTS:
            problems.append(f"{owner}: host {host!r} is not an AWS-owned domain")

        pillar = pillar_for(owner)
        topic_restricted_ok = pillar in RESTRICTED_TOPIC_PILLARS

        path = ref.url.split(host, 1)[1].lower() if host in ref.url else ref.url.lower()
        for frag in ALWAYS_FORBIDDEN_URL_FRAGMENTS:
            if frag in path:
                problems.append(f"{owner}: URL path claims a compliance outcome {frag!r} "
                                 f"-- {ref.url}")
                break
        if not topic_restricted_ok:
            for frag in RESTRICTED_TOPIC_FRAGMENTS:
                if frag in path:
                    problems.append(
                        f"{owner}: URL path contains {frag!r}, restricted to "
                        f"{sorted(RESTRICTED_TOPIC_PILLARS)} -- {ref.url}"
                    )
                    break

        title_lower = ref.title.lower()
        for term in ALWAYS_FORBIDDEN_TITLE_TERMS:
            if term in title_lower:
                problems.append(f"{owner}: title claims a compliance outcome {term!r} "
                                 f"-- {ref.title}")
                break
        if not topic_restricted_ok:
            for term in RESTRICTED_TOPIC_TERMS:
                if term in title_lower:
                    problems.append(
                        f"{owner}: title contains {term!r}, restricted to "
                        f"{sorted(RESTRICTED_TOPIC_PILLARS)} -- {ref.title}"
                    )
                    break

        if not ref.title.strip():
            problems.append(f"{owner}: empty title")

        if ref.url in seen and seen[ref.url] != ref.title:
            problems.append(
                f"{owner}: URL {ref.url} reused with a different title "
                f"({seen[ref.url]!r} vs {ref.title!r})"
            )
        seen[ref.url] = ref.title

    if known_check_ids is not None:
        for check_id in REFERENCES:
            if check_id not in known_check_ids:
                problems.append(f"{check_id}: not a check this package implements")

    return problems
