# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Architecture diagram review -- readiness-only, design-source assessment.

Reviews an architecture *diagram* (the intended deployment) instead of a live
account, so a team can get readiness feedback before the infrastructure
exists. Pairs with the runtime scan to produce a design-vs-reality
reconciliation.

READ-ONLY / NO-WRITE GUARANTEES
-------------------------------
This module performs exactly three kinds of operation:
  1. Reading bytes from a local file path, or
  2. `s3:GetObject` for an `s3://bucket/key` diagram, and
  3. `bedrock:InvokeModel` (via the Converse API) to analyse the image.

It never calls Put/Create/Update/Delete on any service, never writes the
diagram anywhere, and never persists the image bytes. The only thing retained
about the diagram is the path/URI string, for report provenance.

SCOPE ENFORCEMENT (why this stays outside the AppSec escalation clauses)
-----------------------------------------------------------------------
A model asked to review an architecture will volunteer security advice unless
stopped. Producing security guidance is exactly what the "Security-Impacting
Changes" clause covers, so scope is enforced in three independent layers:

  Layer 1 -- Prompt: the system prompt scopes the review to this package's 5
             readiness pillars and forbids security/guardrails/data-governance
             commentary in findings.
  Layer 2 -- Structural: the model is given `out_of_scope_notes` as the only
             place to put anything else, and that field is DISCARDED (only its
             count is reported). It never reaches the user.
  Layer 3 -- Deterministic post-filter: every finding must declare a category
             in ALLOWED_CATEGORIES, and any finding whose text matches
             EXCLUDED_TOPIC_MARKERS is dropped regardless of category. Fenced
             code blocks are stripped so no IaC template can be emitted.

Layers 2 and 3 do not depend on the model complying with layer 1.

The review also never claims compliance with any regulation or standard, and
never emits CloudFormation/Terraform/CDK -- recommendations are plain text.

WHY THIS STAYS 5-PILLAR EVEN THOUGH THE ACCOUNT SCAN IS NOW 8-PILLAR
----------------------------------------------------------------------
The account scan (bedrock_readiness/modules/) now also assesses Security,
Guardrails, and Data Governance, using deterministic read-only API calls. This
module's scope was deliberately NOT widened to match. A vision model
free-associating security observations from a picture is a materially
different, harder-to-verify source than a deterministic API read, so
ALLOWED_CATEGORIES and EXCLUDED_TOPIC_MARKERS below are unchanged: this
diagram-review path still only ever produces Observability, Architecture,
Quota & Capacity, Cost Optimization, or Model Fitness findings, and reviewers
should not read the account-scan expansion as implying anything about the
model-vision path.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .models import (
    Finding,
    FindingSource,
    CheckStatus,
    FixType,
    PillarResult,
    READINESS_CAPABILITIES,
    RECONCILIATION_VERDICTS,
)

# --- Limits -------------------------------------------------------------------

MAX_DIAGRAM_BYTES = 4_500_000          # Converse caps images ~5 MB; keep margin
MAX_FINDINGS = 40                      # Cap model output we will surface
MAX_TEXT_CHARS = 600                   # Per free-text field, post-sanitisation
SUPPORTED_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}

# --- Model selection ----------------------------------------------------------
#
# There is deliberately NO default model ID. The model is chosen from what the
# account scan found available, so the tool never assumes access to something a
# customer has not been granted.
#
# Cheapest-first preference among image-capable models. Capability is decided by
# the API (`inputModalities` must contain IMAGE); this list only orders the ones
# that are actually available.
#
# This is a maintained heuristic, not live pricing. Reading the Price List API
# would need `pricing:GetProducts`, a permission this tool otherwise has no use
# for, and would add a network dependency to model selection. Override with
# `--diagram-model-id` when the ranking is wrong for a given account.
MODEL_COST_PREFERENCE = (
    "nova-lite",
    "nova-pro",
    "claude-3-haiku",
    "llama3-2-11b",
    "pixtral",
    "claude-3-5-sonnet",
    "claude-3-sonnet",
    "llama3-2-90b",
    "claude-3-7-sonnet",
    "claude-sonnet-4",
    "claude-3-opus",
    "claude-opus-4",
)

# Preference among inference-profile scopes for a chosen model. Global CRIS
# gives the highest throughput and is cheaper than single-region per the AWS
# cross-region inference docs, so it wins when available.
CRIS_SCOPE_RANK = {"global.": 0, "us.": 1, "eu.": 1, "apac.": 1}

# Derived so the two cannot drift apart.
CRIS_PREFIXES = tuple(CRIS_SCOPE_RANK)

# --- Scope enforcement --------------------------------------------------------

# A finding must declare one of these categories, matching this package's
# pillars exactly. Anything else is dropped.
ALLOWED_CATEGORIES = {
    "observability",
    "architecture",
    "quota_capacity",
    "cost_optimization",
    "model_fitness",
}

CATEGORY_TO_PILLAR_NAME = {
    "observability": "Observability",
    "architecture": "Architecture & Resilience",
    "quota_capacity": "Quota & Capacity",
    "cost_optimization": "Cost Optimization",
    "model_fitness": "Model Fitness",
}

# Substrings (lowercased) that mark a finding as outside this package's scope.
# Matching text is dropped, not rewritten -- we would rather lose a borderline
# readiness observation than deliver a security one.
#
# Deliberately NOT included here: generic words that legitimately appear in
# readiness findings ("policy" as in retry/scaling policy, "throttle",
# "isolation" as in session isolation), to avoid over-filtering.
EXCLUDED_TOPIC_MARKERS = (
    # Identity, access, credentials
    "iam ", "iam:", "iam policy", "iam role", "least privilege", "least-privilege",
    "privilege escalation", "wildcard action", "assume role", "assumerole", "sts:",
    "credential", "secret manager", "secrets manager", "api key", "access key",
    "password", "authentication", "authorization", "authn", "authz", "rbac",
    "identity provider", "cognito", "oauth", "jwt",
    # Encryption / key management
    "encrypt", "decrypt", "kms", "cmk", "customer managed key", "tls", "ssl",
    "in transit", "at rest", "certificate", "acm ",
    # Network isolation / perimeter
    "privatelink", "private link", "vpc endpoint", "vpce", "security group",
    "nacl", "network acl", "network isolation", "waf", "aws shield", "firewall",
    "publicly exposed", "public exposure", "internet-facing risk",
    # Guardrails / content safety
    "guardrail", "content filter", "content-filter", "prompt injection",
    "prompt attack", "jailbreak", "toxicity", "denied topic", "contextual grounding",
    "content safety", "moderation", "harmful content",
    # Data governance / regulated data / audit
    "zero data retention", "data retention", "zdr", "pii", "phi", "pci", "hipaa",
    "gdpr", "sox", "fedramp", "sensitive data", "personal data", "data residency",
    "data sovereignty", "compliance", "compliant", "audit trail", "cloudtrail",
    "data classification",
    # Threat / vulnerability framing
    "threat model", "attack surface", "attack vector", "vulnerab", "exploit",
    "cve-", "penetration test", "pentest", "malicious", "adversar", "breach",
    "exfiltrat", "insider threat",
)

# Markers of an emitted infrastructure-as-code template. Belt-and-braces: code
# fences are stripped first, so this only catches unfenced attempts.
IAC_MARKERS = (
    "awstemplateformatversion",
    'resource "aws_',
    '"type": "aws::',
    "type: aws::",
    "from aws_cdk",
    "new cdk.",
)

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_FENCE_RE = re.compile(r"`{1,2}([^`]*)`{1,2}")


class DiagramReviewError(Exception):
    """The diagram itself could not be used -- bad path, format, or size.

    A caller-input problem, worth surfacing loudly.
    """


class DiagramReviewSkipped(Exception):
    """The review could not run, so it was skipped. Carries a plain reason.

    Raised when no image-capable model is available, when the model call fails
    for any reason, or when the response is unusable. Never retried: one
    attempt, then skip. The account scan is unaffected -- all 59 readiness
    checks work without any model.
    """


@dataclass(frozen=True)
class ModelChoice:
    """The model the diagram review will use, and how it was reached."""
    invoke_id: str      # Passed to converse() -- may be an inference profile ID
    base_id: str        # Underlying foundation model
    via: str            # global-cris | geo-cris | direct | override
    rank: int           # Index into MODEL_COST_PREFERENCE; -1 for an override

    @property
    def description(self) -> str:
        label = {
            "global-cris": "global cross-region inference profile",
            "geo-cris": "geographic cross-region inference profile",
            "direct": "direct model invocation",
            "override": "explicitly requested",
        }.get(self.via, self.via)
        return f"{self.invoke_id} ({label})"


@dataclass
class DesignReview:
    """Structured result of a diagram review (design source only)."""
    diagram_source: str = ""
    design_summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    capability_statuses: dict = field(default_factory=dict)  # capability -> PRESENT|ABSENT|UNCLEAR
    out_of_scope_note_count: int = 0
    dropped_finding_count: int = 0        # Findings removed by the post-filter
    model_id: str = ""
    model_via: str = ""                   # How the model was reached (see ModelChoice.via)

    def as_pillar_results(self) -> list[PillarResult]:
        """Group design findings into PillarResult objects for reporting.

        Note these are display containers only -- design findings never feed
        the runtime pillar scores, because a model's read of a picture is not
        evidence about a live account.
        """
        by_pillar: dict[str, PillarResult] = {}
        for f in self.findings:
            category = _category_of(f.check_id)
            pillar_name = CATEGORY_TO_PILLAR_NAME.get(category, "Architecture & Resilience")
            pr = by_pillar.setdefault(
                category,
                PillarResult(pillar_id=category, pillar_name=f"{pillar_name} (design)"),
            )
            pr.findings.append(f)
        order = list(CATEGORY_TO_PILLAR_NAME)
        return [by_pillar[c] for c in order if c in by_pillar]


# --- Prompts ------------------------------------------------------------------

SYSTEM_PROMPT = """\
You review AWS architecture diagrams for Amazon Bedrock PRODUCTION READINESS.

You assess EXACTLY these five areas and nothing else:

1. observability     -- invocation logging, dashboards, metrics, alarms on
                        errors/latency/throttles/cost, distributed tracing,
                        agent session tracing, model/agent evaluation.
2. architecture      -- resilience and scalability: cross-region inference,
                        model fallback paths, retry/backoff, async queueing and
                        buffering, managed vs self-managed agent hosting,
                        error handling in flows, knowledge base sync health.
3. quota_capacity    -- quota headroom for expected traffic, throttling risk,
                        provisioned vs on-demand capacity, cross-region
                        capacity, concurrent session limits.
4. cost_optimization -- token/cost tracking and alerting, batch vs real-time
                        inference, provisioned throughput utilisation, model
                        right-sizing for cost, embedding/sync cost.
5. model_fitness     -- model selection appropriateness: single-model
                        dependency, premium-model overuse for simple tasks,
                        legacy/deprecated model usage, cross-region inference
                        profile adoption.

HARD SCOPE RULES -- these are not style preferences:

- Do NOT put security, identity, access control, encryption, key management,
  network isolation/perimeter, guardrails, content safety, prompt-injection,
  data governance, data retention, regulated data (PII/PHI/PCI/HIPAA/GDPR),
  audit logging, threat modelling, or vulnerability observations in "findings".
  Another reviewed tool covers those. If you notice something in those areas,
  put a single short sentence in "out_of_scope_notes" instead. Those notes are
  discarded and never shown to anyone, so do not rely on them to convey
  anything important.
- Do NOT state or imply that following your advice achieves compliance with
  any regulation, standard, or framework.
- Do NOT output CloudFormation, Terraform, CDK, or any other deployable
  template or code. Recommendations must be plain prose describing what to
  change, never an artefact to apply.
- Do NOT guess. If the diagram does not clearly show something, say UNCLEAR.
  A diagram legitimately omits detail; absence from a picture is not evidence
  of absence in the real deployment.

Return ONLY a single JSON object, no prose before or after it:

{
  "workload_summary": "2-3 sentences on what this architecture appears to do",
  "workload_type": "one of: inference-api, customer-facing-chatbot, rag-pipeline, multi-agent, batch-processing, fine-tuning, multi-modal, general",
  "capabilities": [
    {"capability": "<key from the capability list given by the user>",
     "status": "PRESENT|ABSENT|UNCLEAR",
     "evidence": "what in the diagram led you to this, one sentence"}
  ],
  "findings": [
    {"category": "observability|architecture|quota_capacity|cost_optimization|model_fitness",
     "title": "short finding name",
     "observation": "what the diagram shows or omits",
     "recommendation": "plain-prose change to make",
     "impact": 1-4,
     "likelihood": 1-4,
     "fix_type": "config|deploy|architecture|code",
     "effort_minutes": integer}
  ],
  "out_of_scope_notes": ["discarded -- see scope rules"]
}

Report a capability entry for EVERY capability key the user lists. Impact and
likelihood are 1 (lowest) to 4 (highest); their product is the risk score.\
"""


def _build_user_prompt(workload_type: str) -> str:
    cap_lines = "\n".join(
        f'  - "{key}": {meta["label"]} -- look for: {meta["diagram_hint"]}'
        for key, meta in READINESS_CAPABILITIES.items()
    )
    wt_line = (
        f"The team says this is a '{workload_type}' workload."
        if workload_type and workload_type != "general"
        else "The workload type is not declared -- infer it from the diagram."
    )
    return (
        f"Review the attached architecture diagram for Bedrock production readiness.\n"
        f"{wt_line}\n\n"
        f"Report a status for every one of these capabilities:\n{cap_lines}\n\n"
        f"Then list readiness findings within your five permitted areas. "
        f"Remember: UNCLEAR is the correct answer whenever the diagram does not "
        f"clearly show a capability."
    )


# --- Diagram loading (read-only) ---------------------------------------------


def load_diagram(source: str, session=None) -> tuple[bytes, str, str]:
    """Load diagram bytes from a local path or `s3://bucket/key`.

    Read-only: local filesystem read, or `s3:GetObject`. Never writes.

    Returns (image_bytes, converse_format, resolved_label).
    """
    if not source or not isinstance(source, str):
        raise DiagramReviewError("No diagram path or S3 URI provided.")

    fmt = _format_for(source)

    if source.startswith("s3://"):
        if session is None:
            raise DiagramReviewError("An AWS session is required to read an s3:// diagram.")
        bucket, _, key = source[len("s3://"):].partition("/")
        if not bucket or not key:
            raise DiagramReviewError(f"Malformed S3 URI: {source!r}. Expected s3://bucket/key.")
        s3 = session.client("s3")
        try:
            # GetObject only. This module has no code path that writes to S3.
            obj = s3.get_object(Bucket=bucket, Key=key)
            size = obj.get("ContentLength", 0)
            if size and size > MAX_DIAGRAM_BYTES:
                raise DiagramReviewError(
                    f"Diagram is {size} bytes, over the {MAX_DIAGRAM_BYTES}-byte limit."
                )
            data = obj["Body"].read(MAX_DIAGRAM_BYTES + 1)
        except DiagramReviewError:
            raise
        except Exception as e:
            raise DiagramReviewError(f"Could not read {source}: {e}") from e
    else:
        path = os.path.expanduser(source)
        if not os.path.isfile(path):
            raise DiagramReviewError(f"Diagram file not found: {path}")
        size = os.path.getsize(path)
        if size > MAX_DIAGRAM_BYTES:
            raise DiagramReviewError(
                f"Diagram is {size} bytes, over the {MAX_DIAGRAM_BYTES}-byte limit."
            )
        with open(path, "rb") as fp:
            data = fp.read(MAX_DIAGRAM_BYTES + 1)

    if len(data) > MAX_DIAGRAM_BYTES:
        raise DiagramReviewError(f"Diagram exceeds the {MAX_DIAGRAM_BYTES}-byte limit.")
    if not data:
        raise DiagramReviewError(f"Diagram is empty: {source}")

    return data, fmt, source


def _format_for(source: str) -> str:
    ext = os.path.splitext(source.split("?")[0])[1].lower()
    if ext not in SUPPORTED_FORMATS:
        raise DiagramReviewError(
            f"Unsupported diagram format {ext or '(none)'!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    return SUPPORTED_FORMATS[ext]


# --- Model invocation ---------------------------------------------------------


def _default_invoke(session, region: str, model_id: str,
                    image_bytes: bytes, image_format: str, user_prompt: str) -> str:
    """Invoke a Bedrock multimodal model via the Converse API.

    Requires only `bedrock:InvokeModel`. Inference does not create, modify, or
    delete any resource.

    `max_attempts=1` disables botocore's retries. The SDK would otherwise retry
    three times on throttling and transient errors; a diagram review is
    optional, so one attempt then skip is the intended behaviour rather than
    making the customer wait through a backoff sequence.
    """
    from botocore.config import Config as BotoConfig

    no_retry = BotoConfig(
        retries={"max_attempts": 1, "mode": "standard"},
        connect_timeout=10,
        read_timeout=120,
    )
    client = session.client("bedrock-runtime", region_name=region, config=no_retry)
    resp = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
                {"text": user_prompt},
            ],
        }],
        inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
    )
    parts = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(p.get("text", "") for p in parts)


def base_model_id(model_id: str) -> str:
    """Strip a cross-region inference prefix to get the underlying model ID.

    `list_foundation_models` returns base IDs (`anthropic.claude-...`) while the
    default here is a CRIS profile (`us.anthropic.claude-...`), so the two must
    be compared on the same footing.
    """
    for prefix in CRIS_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def find_image_capable_models(foundation_models) -> list[str]:
    """Model IDs in this account that accept image input and return text.

    Takes the `modelSummaries` list from `bedrock:ListFoundationModels`, or the
    full `scan_data` dict. Returns [] when the input is unusable, so callers
    can treat "unknown" and "none" the same way.
    """
    if isinstance(foundation_models, dict):
        foundation_models = foundation_models.get("bedrock", {}).get("foundation_models", [])
    if not isinstance(foundation_models, list):
        return []

    capable = []
    for m in foundation_models:
        if not isinstance(m, dict):
            continue
        inputs = m.get("inputModalities") or []
        outputs = m.get("outputModalities") or []
        if "IMAGE" in inputs and "TEXT" in outputs:
            model_id = m.get("modelId")
            if model_id:
                capable.append(model_id)
    return sorted(set(capable))


def _cost_rank(model_id: str) -> int:
    """Position in the cheapest-first preference list.

    An unrecognised model ranks last, so a newly released expensive model is
    never selected ahead of a known cheap one without a deliberate override.
    """
    low = model_id.lower()
    for i, marker in enumerate(MODEL_COST_PREFERENCE):
        if marker in low:
            return i
    return len(MODEL_COST_PREFERENCE)


def _inference_profiles(scan_data) -> list:
    if isinstance(scan_data, dict):
        profiles = scan_data.get("bedrock", {}).get("inference_profiles", [])
        return profiles if isinstance(profiles, list) else []
    return []


def _best_profile_for(base_id: str, profiles: list) -> Optional[str]:
    """Best-scoped inference profile covering a model, or None.

    Global CRIS beats geographic CRIS; anything else is ignored, since an
    unrecognised prefix tells us nothing about its scope.
    """
    matches: list[tuple[int, str]] = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        pid = p.get("inferenceProfileId") or ""
        if not pid or not base_id:
            continue
        covers = any(
            base_id in (m.get("modelArn") or "")
            for m in (p.get("models") or []) if isinstance(m, dict)
        )
        if not covers:
            continue
        for prefix, rank in CRIS_SCOPE_RANK.items():
            if pid.startswith(prefix):
                matches.append((rank, pid))
                break
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


def select_diagram_model(scan_data=None, model_id_override: Optional[str] = None
                          ) -> Optional[ModelChoice]:
    """Pick the model to review a diagram with, from what the account has.

    Returns None when no image-capable model is available, which the caller
    treats as "skip the diagram review" rather than an error.

    Selection order:
      1. An explicit override is used verbatim, with no capability check --
         the caller is assumed to know their account.
      2. Otherwise, filter to models whose `inputModalities` include IMAGE,
         then take the cheapest by MODEL_COST_PREFERENCE.
      3. Prefer reaching that model through an inference profile when one
         covers it (global CRIS first), else invoke it directly.
    """
    if model_id_override:
        return ModelChoice(
            invoke_id=model_id_override,
            base_id=base_model_id(model_id_override),
            via="override",
            rank=-1,
        )

    capable = find_image_capable_models(scan_data)
    if not capable:
        return None

    capable.sort(key=lambda m: (_cost_rank(m), m))
    chosen = capable[0]

    profile_id = _best_profile_for(chosen, _inference_profiles(scan_data))
    if profile_id:
        via = "global-cris" if profile_id.startswith("global.") else "geo-cris"
        return ModelChoice(profile_id, chosen, via, _cost_rank(chosen))
    return ModelChoice(chosen, chosen, "direct", _cost_rank(chosen))


NO_MODEL_REASON = (
    "no Bedrock model that accepts image input is available to this account in "
    "{region}. Grant access to a multimodal model on the Bedrock console "
    "'Model access' page to enable diagram review. The readiness assessment "
    "itself needs no model and is unaffected."
)


def _skip_reason(exc: Exception, choice: ModelChoice, region: str) -> str:
    """Plain-language reason a diagram review was skipped.

    Classified so the customer knows whether to grant model access, pick a
    different model, or look at credentials -- rather than being handed a raw
    botocore exception.
    """
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
    text = f"{code} {exc}".lower()
    model = choice.invoke_id

    if "accessdenied" in text or "not authorized" in text:
        return (
            f"access denied invoking {model} in {region} -- the model is not granted to "
            f"this account, or the caller lacks bedrock:InvokeModel on it"
        )
    if "resourcenotfound" in text or "could not be found" in text:
        return (
            f"{model} does not exist in {region} -- model and inference-profile "
            f"availability is region-specific"
        )
    if "validation" in text:
        return (
            f"Bedrock rejected the request for {model} in {region} -- most often a model "
            f"that does not accept image input, or a profile not enabled here"
        )
    if "throttl" in text or "toomanyrequests" in text:
        return f"throttled invoking {model} in {region} (not retried by design)"
    if "expiredtoken" in text or "invalidclienttokenid" in text or "credential" in text:
        return f"AWS credentials are invalid or expired ({exc})"
    return f"{model} could not be invoked in {region}: {exc}"


def review(
    diagram: str,
    session=None,
    region: str = "us-east-1",
    workload_type: str = "general",
    model_id: Optional[str] = None,
    invoke_fn: Optional[Callable] = None,
    scan_data=None,
) -> DesignReview:
    """Review an architecture diagram for readiness. Read-only throughout.

    The model is selected from `scan_data` -- the account's own available
    models -- rather than assumed. See `select_diagram_model`.

    Raises `DiagramReviewError` for an unusable diagram (bad path, format, or
    size), and `DiagramReviewSkipped` when the review cannot run: no
    image-capable model available, the model call failed, or the response was
    unusable. There is exactly one attempt at the model call; nothing is
    retried.

    `invoke_fn` exists so the pipeline can be exercised offline with a stubbed
    model response (see tests/run_diagram_review.py) -- no AWS credentials and
    no model call needed to test the parsing and scope-filter layers.
    """
    choice = select_diagram_model(scan_data, model_id_override=model_id)
    if choice is None:
        raise DiagramReviewSkipped(NO_MODEL_REASON.format(region=region))

    # Raises DiagramReviewError -- a bad diagram is caller input, not a skip.
    image_bytes, image_format, label = load_diagram(diagram, session=session)
    user_prompt = _build_user_prompt(workload_type)

    invoker = invoke_fn or _default_invoke
    try:
        raw = invoker(session, region, choice.invoke_id, image_bytes, image_format, user_prompt)
    except Exception as e:
        # One attempt only. No fallback to another model, no backoff retry.
        raise DiagramReviewSkipped(_skip_reason(e, choice, region)) from e
    finally:
        # Do not retain image bytes beyond the call.
        del image_bytes

    try:
        payload = _parse_json(raw)
    except DiagramReviewError as e:
        # An unusable response is not something the customer can fix, so it is
        # a skip rather than a hard error.
        raise DiagramReviewSkipped(
            f"{choice.invoke_id} returned a response that could not be parsed ({e})"
        ) from e

    review_obj = _build_review(payload, label, choice.invoke_id)
    review_obj.model_via = choice.via
    return review_obj


# --- Parsing + scope filtering ------------------------------------------------


def _parse_json(raw: str) -> dict:
    if not raw or not raw.strip():
        raise DiagramReviewError("Model returned an empty response.")
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise DiagramReviewError("Model response did not contain a JSON object.")
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise DiagramReviewError(f"Could not parse model JSON: {e}") from e
    if not isinstance(payload, dict):
        raise DiagramReviewError("Model JSON was not an object.")
    return payload


def _sanitize(text, limit: int = MAX_TEXT_CHARS) -> str:
    """Strip code fences and clamp length.

    Fence stripping is what guarantees no deployable template reaches the
    report, independent of whether the model honoured the prompt.
    """
    if not isinstance(text, str):
        return ""
    cleaned = _CODE_FENCE_RE.sub(" ", text)
    cleaned = _INLINE_FENCE_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("<", "&lt;").replace(">", "&gt;")
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip()


def _is_out_of_scope(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    if any(marker in blob for marker in EXCLUDED_TOPIC_MARKERS):
        return True
    return any(marker in blob for marker in IAC_MARKERS)


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _category_of(check_id: str) -> str:
    """Recover the pillar category from a design check ID (DGM-<category>-NN)."""
    parts = check_id.split("-")
    if len(parts) >= 3:
        return "_".join(parts[1:-1]).lower()
    return "architecture"


def _build_review(payload: dict, label: str, model_id: str) -> DesignReview:
    out_of_scope = payload.get("out_of_scope_notes") or []
    review_obj = DesignReview(
        diagram_source=label,
        design_summary=_sanitize(payload.get("workload_summary"), limit=800),
        # Intentionally count-only: the notes themselves are discarded and are
        # never rendered, logged, or returned.
        out_of_scope_note_count=len(out_of_scope) if isinstance(out_of_scope, list) else 0,
        model_id=model_id,
    )

    # --- Capabilities ---
    valid_statuses = {"PRESENT", "ABSENT", "UNCLEAR"}
    raw_caps = payload.get("capabilities") or []
    if isinstance(raw_caps, list):
        for entry in raw_caps:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("capability", "")).strip()
            if key not in READINESS_CAPABILITIES:
                continue  # Unknown capability keys are ignored, not invented.
            status = str(entry.get("status", "")).strip().upper()
            review_obj.capability_statuses[key] = (
                status if status in valid_statuses else "UNCLEAR"
            )
    for key in READINESS_CAPABILITIES:
        review_obj.capability_statuses.setdefault(key, "UNCLEAR")

    # --- Findings (scope-filtered) ---
    per_category_counts: dict[str, int] = {}
    raw_findings = payload.get("findings") or []
    if isinstance(raw_findings, list):
        for entry in raw_findings[:MAX_FINDINGS * 2]:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category", "")).strip().lower()
            if category not in ALLOWED_CATEGORIES:
                review_obj.dropped_finding_count += 1
                continue

            title = _sanitize(entry.get("title"), limit=120)
            observation = _sanitize(entry.get("observation"))
            recommendation = _sanitize(entry.get("recommendation"))
            if not title or not observation:
                review_obj.dropped_finding_count += 1
                continue

            if _is_out_of_scope(title, observation, recommendation):
                review_obj.dropped_finding_count += 1
                continue

            if len(review_obj.findings) >= MAX_FINDINGS:
                break

            per_category_counts[category] = per_category_counts.get(category, 0) + 1
            check_id = f"DGM-{category.upper()}-{per_category_counts[category]:02d}"

            fix_raw = str(entry.get("fix_type", "config")).strip().lower()
            try:
                fix_type = FixType(fix_raw)
            except ValueError:
                fix_type = FixType.CONFIG

            review_obj.findings.append(Finding(
                check_id=check_id,
                check_name=title,
                # Design findings are always WARN: a diagram cannot prove a
                # capability is absent in the real deployment, so nothing here
                # is reported as a hard FAIL.
                status=CheckStatus.WARN,
                impact=_clamp(entry.get("impact"), 1, 4, 2),
                likelihood=_clamp(entry.get("likelihood"), 1, 4, 2),
                message=observation,
                recommendation=recommendation,
                fix_type=fix_type,
                effort_minutes=_clamp(entry.get("effort_minutes"), 5, 2880, 30),
                source=FindingSource.DESIGN,
            ))

    return review_obj


# --- Design vs. reality reconciliation ---------------------------------------


def reconcile(capability_statuses: dict, pillar_results: list[PillarResult]) -> list[dict]:
    """Compare what the diagram shows against what the account actually has.

    Deterministic: the model only supplies PRESENT/ABSENT/UNCLEAR per
    capability; the runtime side comes entirely from real check results, and
    the verdict is a lookup on the pair. The model does not decide verdicts.
    """
    by_check: dict[str, Finding] = {}
    for pr in pillar_results:
        for f in pr.findings:
            by_check[f.check_id] = f

    rows = []
    for key, meta in READINESS_CAPABILITIES.items():
        design_status = str(capability_statuses.get(key, "UNCLEAR")).upper()
        mapped = [by_check[cid] for cid in meta["runtime_checks"] if cid in by_check]

        assessed = [f for f in mapped if f.status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.WARN)]
        if not mapped:
            runtime_status = "NOT_ASSESSED"
        elif not assessed:
            runtime_status = "UNKNOWN"     # mapped checks all ERROR/SKIPPED
        elif any(f.status == CheckStatus.PASS for f in assessed):
            runtime_status = "PRESENT"
        else:
            runtime_status = "ABSENT"

        # Nothing known from either side -- omit rather than emit a blank row.
        if design_status == "UNCLEAR" and runtime_status in ("NOT_ASSESSED", "UNKNOWN"):
            continue

        verdict = _verdict_for(design_status, runtime_status)

        severity = "INFO"
        if runtime_status == "ABSENT":
            failing = [f for f in assessed if f.status in (CheckStatus.FAIL, CheckStatus.WARN)]
            if failing:
                severity = max(failing, key=lambda f: f.risk_score).severity_label

        rows.append({
            "capability": key,
            "label": meta["label"],
            "pillar": CATEGORY_TO_PILLAR_NAME.get(meta["pillar"], meta["pillar"]),
            "design_status": design_status,
            "runtime_status": runtime_status,
            "verdict": verdict,
            "severity": severity,
            "runtime_checks": [cid for cid in meta["runtime_checks"] if cid in by_check],
        })

    # Ordered by how actionable the row is, then by runtime severity.
    priority = {verdict: i for i, verdict in enumerate(RECONCILIATION_VERDICTS)}
    sev_priority = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    rows.sort(key=lambda r: (priority.get(r["verdict"], 99),
                             sev_priority.get(r["severity"], 9)))
    return rows


def _verdict_for(design_status: str, runtime_status: str) -> str:
    """Map a (diagram, account) status pair to a verdict.

    Note the UNCLEAR-diagram / ABSENT-account case is NOT reported as
    INCONCLUSIVE. The scan is hard evidence that the account lacks the
    capability; only the design attribution is unknown. Calling that
    "inconclusive" would bury a real, often high-severity gap behind a label
    that reads as "nothing to see here". INCONCLUSIVE is reserved for the case
    where the *scan* could not determine the state.
    """
    if runtime_status in ("NOT_ASSESSED", "UNKNOWN"):
        return "INCONCLUSIVE"
    if design_status == "PRESENT":
        return "ALIGNED" if runtime_status == "PRESENT" else "DESIGN_NOT_IMPLEMENTED"
    if design_status == "ABSENT":
        return "UNDOCUMENTED_IN_DESIGN" if runtime_status == "PRESENT" else "MISSING_IN_BOTH"
    # design UNCLEAR, runtime known
    return "UNDOCUMENTED_IN_DESIGN" if runtime_status == "PRESENT" else "ABSENT_IN_ACCOUNT"
