# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data models for the Bedrock Readiness (readiness-only) solution.

This is a standalone solution, not a mode of a larger tool -- there is no
"track" flag here. It assesses eight pillars, including Security, Guardrails,
and Data Governance, on the same terms as every other pillar: read-only API
calls, severity-rated findings, and plain-text recommendations pointing at
public AWS documentation. See ../../README.md for what that boundary means in
practice -- in particular, no check here ever produces a deployable IAM
policy, guardrail configuration, or other artifact meant to be applied as-is.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CheckStatus(Enum):
    """Status of a single check evaluation."""
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIPPED = "SKIPPED"  # Not applicable to this workload/account
    ERROR = "ERROR"      # Could not assess (permission denied, API unavailable)


class FixType(Enum):
    """Classification of remediation effort."""
    CONFIG = "config"             # Change an AWS setting (minutes)
    DEPLOY = "deploy"             # Deploy a new resource (hours)
    ARCHITECTURE = "architecture"  # Redesign part of the system (days)
    CODE = "code"                 # Change application code (varies)


class FindingSource(Enum):
    """Where a finding came from.

    RUNTIME findings come from read-only API calls against a live account.
    DESIGN findings come from reviewing an architecture diagram, i.e. the
    *intended* deployment, before or alongside the real thing.
    """
    RUNTIME = "runtime"
    DESIGN = "design"


SEVERITY_BANDS = {
    "CRITICAL": {"min_risk": 12, "action": "Fix before production"},
    "HIGH": {"min_risk": 6, "action": "Address before or within first week of launch"},
    "MEDIUM": {"min_risk": 3, "action": "Plan within 30 days"},
    "LOW": {"min_risk": 1, "action": "Best practice for later"},
}


@dataclass
class Finding:
    """A single assessment finding produced by a pillar module.

    Note what is NOT here: there is no `remediation_key` field and no
    deployable-remediation concept anywhere in this package. Every
    `recommendation` string is plain-text guidance (e.g. "create a CloudWatch
    alarm for X"), never a generated CloudFormation/Terraform snippet, IAM
    policy document, or guardrail configuration a customer could apply as-is
    -- including for the Security, Guardrails, and Data Governance pillars.
    tests/test_no_deployable_remediation.py statically guards this.
    """
    check_id: str              # e.g., "OBS-01", "QC-03", "MF-02"
    check_name: str            # e.g., "Model Invocation Logging"
    status: CheckStatus
    impact: int                # 1-4
    likelihood: int            # 1-4
    message: str               # What was found
    resource_ids: list[str] = field(default_factory=list)  # Affected ARNs/IDs
    recommendation: str = ""
    fix_type: FixType = FixType.CONFIG
    depends_on: list[str] = field(default_factory=list)    # Check IDs that must be fixed first
    effort_minutes: int = 0    # Estimated fix effort
    source: FindingSource = FindingSource.RUNTIME  # runtime scan vs diagram review

    @property
    def risk_score(self) -> int:
        """Impact x Likelihood (1-16)."""
        return self.impact * self.likelihood

    @property
    def severity_label(self) -> str:
        """Human-readable severity based on risk score."""
        score = self.risk_score
        if score >= 12:
            return "CRITICAL"
        elif score >= 6:
            return "HIGH"
        elif score >= 3:
            return "MEDIUM"
        return "LOW"


@dataclass
class PillarResult:
    """Result from a single pillar assessment."""
    pillar_id: str             # e.g., "observability"
    pillar_name: str           # e.g., "Observability"
    findings: list[Finding] = field(default_factory=list)

    @property
    def score(self) -> int:
        """Pillar score: passed risk / total risk x 100."""
        scoreable = [f for f in self.findings if f.status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.WARN)]
        if not scoreable:
            return 0
        total_risk = sum(f.risk_score for f in scoreable)
        passed_risk = sum(f.risk_score for f in scoreable if f.status == CheckStatus.PASS)
        return int((passed_risk / total_risk) * 100) if total_risk > 0 else 0

    @property
    def total_checks(self) -> int:
        return len([f for f in self.findings if f.status != CheckStatus.SKIPPED])

    @property
    def passed_checks(self) -> int:
        return len([f for f in self.findings if f.status == CheckStatus.PASS])

    @property
    def failed_checks(self) -> int:
        return len([f for f in self.findings if f.status in (CheckStatus.FAIL, CheckStatus.WARN)])

    @property
    def error_checks(self) -> int:
        return len([f for f in self.findings if f.status == CheckStatus.ERROR])


@dataclass
class AssessmentResult:
    """Complete assessment result across all pillars."""
    account_id: str
    region: str
    mode: str                  # "pre-production" or "production"
    workload_type: str         # e.g., "customer-facing-chatbot"
    pillar_results: list[PillarResult] = field(default_factory=list)
    overall_score: int = 0                                  # Tracking metric only
    priority_actions: list[dict] = field(default_factory=list)  # Findings ranked by risk
    severity_summary: dict = field(default_factory=dict)    # Counts + effort by severity
    recommendations: list[str] = field(default_factory=list)  # Non-scoring suggestions
    total_api_calls: int = 0
    scan_duration_seconds: float = 0.0

    # --- Diagram-review additions (empty unless a diagram was reviewed) ---
    design_summary: str = ""                                # Model's read of the diagram
    design_findings: list = field(default_factory=list)     # list[Finding], source=DESIGN
    reconciliation: list[dict] = field(default_factory=list)  # Design vs. runtime per capability
    out_of_scope_note_count: int = 0                        # Discarded non-readiness observations
    dropped_finding_count: int = 0                          # Design findings cut by the scope filter
    diagram_source: str = ""                                # Path/URI reviewed (no bytes retained)
    diagram_model_id: str = ""                              # Model used for the design review
    diagram_model_via: str = ""                             # global-cris | geo-cris | direct | override
    # Set when a diagram was supplied but the review could not run. The scan is
    # unaffected; this is surfaced rather than swallowed so the omission is visible.
    diagram_skipped_reason: str = ""

    # False when no account was scanned (diagram-only review). The score is
    # meaningless without runtime evidence, so consumers must not print it.
    score_applicable: bool = True

    @property
    def has_design_review(self) -> bool:
        return bool(self.design_summary or self.design_findings or self.reconciliation)

    @property
    def diagram_was_skipped(self) -> bool:
        return bool(self.diagram_skipped_reason)


# --- Workload type defaults ---

WORKLOAD_TYPES = [
    "inference-api",
    "customer-facing-chatbot",
    "rag-pipeline",
    "multi-agent",
    "batch-processing",
    "fine-tuning",
    "multi-modal",
    "general",
]

# Weights across the 8 pillars this solution assesses. Values are fixed
# constants (not computed by a runtime normalization step) so this package
# has no "track" concept at all -- unlike the combined solution's
# resolve_weights() helper, there is nothing here to resolve.
#
# Security, Guardrails, and Data Governance weights vary by workload type on
# purpose: a customer-facing chatbot weights Guardrails higher than a batch
# job with no direct end-user exposure; a RAG pipeline and fine-tuning weight
# Data Governance higher, since both move data (retrieved documents, training
# data) through the account in ways an inference-only API does not. Each row
# sums to 1.0.
DEFAULT_WEIGHTS = {
    "inference-api":           {"observability": 0.15, "architecture": 0.21, "quota_capacity": 0.21, "cost_optimization": 0.08, "model_fitness": 0.15, "security": 0.08, "guardrails": 0.05, "data_governance": 0.07},
    "customer-facing-chatbot": {"observability": 0.21, "architecture": 0.18, "quota_capacity": 0.18, "cost_optimization": 0.10, "model_fitness": 0.08, "security": 0.07, "guardrails": 0.12, "data_governance": 0.06},
    "rag-pipeline":            {"observability": 0.17, "architecture": 0.22, "quota_capacity": 0.12, "cost_optimization": 0.17, "model_fitness": 0.09, "security": 0.07, "guardrails": 0.08, "data_governance": 0.08},
    "multi-agent":             {"observability": 0.24, "architecture": 0.24, "quota_capacity": 0.12, "cost_optimization": 0.09, "model_fitness": 0.09, "security": 0.09, "guardrails": 0.07, "data_governance": 0.06},
    "batch-processing":        {"observability": 0.12, "architecture": 0.16, "quota_capacity": 0.36, "cost_optimization": 0.08, "model_fitness": 0.08, "security": 0.08, "guardrails": 0.03, "data_governance": 0.09},
    "fine-tuning":             {"observability": 0.13, "architecture": 0.18, "quota_capacity": 0.19, "cost_optimization": 0.19, "model_fitness": 0.09, "security": 0.08, "guardrails": 0.04, "data_governance": 0.10},
    "multi-modal":             {"observability": 0.16, "architecture": 0.22, "quota_capacity": 0.16, "cost_optimization": 0.11, "model_fitness": 0.12, "security": 0.07, "guardrails": 0.09, "data_governance": 0.07},
    "general":                 {"observability": 0.16, "architecture": 0.23, "quota_capacity": 0.16, "cost_optimization": 0.11, "model_fitness": 0.12, "security": 0.08, "guardrails": 0.07, "data_governance": 0.07},
}


# --- Readiness capabilities used for design-vs-reality reconciliation ---
#
# Each entry maps one readiness capability a diagram can show to the runtime
# check ID(s) that verify it in a live account. This is what makes the
# diagram review's "design vs reality" table deterministic rather than
# model-invented: the model only reports PRESENT/ABSENT/UNCLEAR per
# capability, and the runtime verdict comes from the real check results.
#
# Every capability here belongs to Observability, Architecture, Quota &
# Capacity, Cost Optimization, or Model Fitness -- deliberately NOT Security,
# Guardrails, or Data Governance, even though the account scan now assesses
# those three too. That boundary is about the diagram-review path
# specifically: a vision model free-associating from a picture is a
# materially different (and less verifiable) source of security-adjacent
# claims than a deterministic API read, so core/diagram_review.py's model
# prompt and scope filter are left exactly as scoped as before -- see that
# module's docstring. Extending reconciliation to those three pillars would be
# a deliberate, separate decision, not something adding them to the account
# scan implies.
READINESS_CAPABILITIES = {
    "invocation_logging": {
        "label": "Model invocation logging",
        "pillar": "observability",
        "runtime_checks": ["OBS-01"],
        "diagram_hint": "Bedrock invocation logs routed to CloudWatch Logs and/or S3",
    },
    "genai_dashboard": {
        "label": "GenAI metrics dashboard",
        "pillar": "observability",
        "runtime_checks": ["OBS-02"],
        "diagram_hint": "A CloudWatch dashboard for model/agent metrics",
    },
    "error_alarm": {
        "label": "Error alarming",
        "pillar": "observability",
        "runtime_checks": ["OBS-03"],
        "diagram_hint": "Alarms/notifications on invocation errors or faults",
    },
    "latency_alarm": {
        "label": "Latency alarming",
        "pillar": "observability",
        "runtime_checks": ["OBS-04"],
        "diagram_hint": "Alarms on model/agent response latency",
    },
    "throttle_alarm": {
        "label": "Throttle alarming",
        "pillar": "observability",
        "runtime_checks": ["OBS-05", "QC-04"],
        "diagram_hint": "Alarms on throttling or approaching quota limits",
    },
    "distributed_tracing": {
        "label": "Distributed tracing",
        "pillar": "observability",
        "runtime_checks": ["OBS-07", "OBS-10"],
        "diagram_hint": "X-Ray / OTEL tracing spanning the request path into Bedrock",
    },
    "cross_region_inference": {
        "label": "Cross-region inference",
        "pillar": "architecture",
        "runtime_checks": ["ARCH-01", "QC-06", "QC-11"],
        "diagram_hint": "CRIS inference profiles or a second region for inference",
    },
    "model_fallback": {
        "label": "Model fallback path",
        "pillar": "architecture",
        "runtime_checks": ["ARCH-02"],
        "diagram_hint": "A secondary model invoked when the primary is degraded",
    },
    "retry_backoff": {
        "label": "Retry with backoff",
        "pillar": "architecture",
        "runtime_checks": ["ARCH-03"],
        "diagram_hint": "Retry/backoff logic in the caller before Bedrock",
    },
    "async_queue": {
        "label": "Async queue / buffering",
        "pillar": "architecture",
        "runtime_checks": ["ARCH-04"],
        "diagram_hint": "SQS/EventBridge/Step Functions buffering requests to Bedrock",
    },
    "provisioned_capacity": {
        "label": "Provisioned throughput",
        "pillar": "quota_capacity",
        "runtime_checks": ["QC-05"],
        "diagram_hint": "Provisioned Throughput / dedicated capacity for inference",
    },
    "batch_inference": {
        "label": "Batch inference path",
        "pillar": "cost_optimization",
        "runtime_checks": ["COST-04", "COST-08", "QC-09"],
        "diagram_hint": "A batch/offline inference path for non-realtime work",
    },
    "cost_monitoring": {
        "label": "Cost / token monitoring",
        "pillar": "cost_optimization",
        "runtime_checks": ["COST-06", "COST-01", "OBS-06"],
        "diagram_hint": "Budget or token-consumption monitoring and alerting",
    },
    "model_tiering": {
        "label": "Model tiering by task",
        "pillar": "model_fitness",
        "runtime_checks": ["COST-02", "MF-01", "MF-02"],
        "diagram_hint": "Different model sizes routed by task complexity",
    },
}

# Verdicts produced by reconciliation. Purely descriptive -- no verdict here
# asserts anything about the security posture of the design.
RECONCILIATION_VERDICTS = {
    "DESIGN_NOT_IMPLEMENTED": "Design shows it but the account does not have it yet",
    "MISSING_IN_BOTH": "Neither the design nor the account has it",
    "ABSENT_IN_ACCOUNT": "Account does not have it; the diagram was unclear either way",
    "UNDOCUMENTED_IN_DESIGN": "Account has it but the diagram does not show it",
    "ALIGNED": "Design shows it and the account has it",
    "INCONCLUSIVE": "The scan could not determine it, so no comparison is possible",
}


@dataclass
class Config:
    """Assessment configuration -- parsed from YAML or auto-detected."""
    mode: str = "auto"                          # pre-production | production | auto
    workload_type: str = "general"
    launch_date: Optional[str] = None           # ISO date string
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    production_estimate: dict = field(default_factory=dict)
    custom_weights: Optional[dict] = None       # Override default pillar weights
    severity_overrides: list[dict] = field(default_factory=list)
    # Model Fitness (MF-02/MF-03) classifies models by substring match against
    # a hardcoded default list, which needs a code change for every new model
    # generation. These let a caller override the defaults without one.
    premium_model_markers: Optional[list[str]] = None   # override modules.model_fitness.PREMIUM_MODEL_MARKERS
    legacy_model_markers: Optional[list[str]] = None    # override modules.model_fitness.LEGACY_MODEL_MARKERS
    output_format: str = "html"                 # html | markdown | json
    output_file: str = "readiness-report.html"
    include_fix_templates: bool = True
    previous_report: Optional[str] = None       # Path to previous state JSON
    profile: Optional[str] = None               # AWS CLI profile
    role_arn: Optional[str] = None               # Cross-account assume role (see APPSEC-NOTES.md)
    dry_run: bool = False
    verbose: bool = False

    # --- Diagram review (optional design-source input) ---
    diagram: Optional[str] = None                # Local path or s3://bucket/key
    diagram_model_id: Optional[str] = None       # Override the multimodal model
    include_account_scan: bool = True            # Allow diagram-only review

    @property
    def weights(self) -> dict:
        """Resolved pillar weights (custom or workload-type defaults)."""
        if self.custom_weights:
            return self.custom_weights
        return DEFAULT_WEIGHTS.get(self.workload_type, DEFAULT_WEIGHTS["general"])
