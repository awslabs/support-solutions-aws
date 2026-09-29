# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Guardrails -- Is harmful or unwanted content actually being filtered?

Checks: GR-01 through GR-04. Reads what bedrock:ListGuardrails/GetGuardrail
and bedrock:ListEnforcedGuardrailsConfiguration already expose about each
guardrail's own configuration -- this pillar cannot and does not evaluate
prompts or model output; that is not observable from a scan.

None of these checks read or repeat the content of a topic definition, word
list, or PII regex -- only whether each policy category is configured at all,
and whether an account-level enforcement mechanism exists. See
core/scanner.py for the exact calls involved.
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError

PILLAR_ID = "guardrails"
PILLAR_NAME = "Guardrails"

# Workload types where a customer or another system's output reaches an end
# user directly, and a missing guardrail is a materially different risk than
# for an internal batch job.
USER_FACING_WORKLOADS = {"customer-facing-chatbot", "rag-pipeline", "multi-agent", "inference-api"}


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)

    guardrails = scan_data.get("bedrock", {}).get("guardrails")
    details = scan_data.get("bedrock", {}).get("guardrail_details")
    enforced = scan_data.get("bedrock", {}).get("enforced_guardrails")

    result.findings.append(_check_any_guardrail_exists(guardrails, config.workload_type))
    result.findings.append(_check_enforcement(guardrails, enforced))
    result.findings.append(_check_sensitive_information_filter(guardrails, details))
    result.findings.append(_check_draft_only(guardrails, details))

    return result


def _check_any_guardrail_exists(guardrails, workload_type: str) -> Finding:
    if guardrails is None or isinstance(guardrails, ScanError):
        # ScanError is truthy and has no __len__, so without this check a
        # failed ListGuardrails call would fall through to `if guardrails:`
        # below and crash on len(guardrails) instead of reporting ERROR.
        err = guardrails.error if isinstance(guardrails, ScanError) else "no data"
        return Finding(
            check_id="GR-01", check_name="Guardrail Configured",
            status=CheckStatus.ERROR, impact=3, likelihood=3,
            message=f"Could not list guardrails: {err}",
            recommendation="Grant bedrock:ListGuardrails",
        )
    if guardrails:
        return Finding(
            check_id="GR-01", check_name="Guardrail Configured",
            status=CheckStatus.PASS, impact=3, likelihood=3,
            message=f"{len(guardrails)} guardrail(s) configured in this account/region",
        )

    user_facing = workload_type in USER_FACING_WORKLOADS
    return Finding(
        check_id="GR-01", check_name="Guardrail Configured",
        status=CheckStatus.WARN if user_facing else CheckStatus.SKIPPED,
        impact=3 if user_facing else 1, likelihood=3 if user_facing else 1,
        message=(
            f"No Bedrock guardrail exists in this account/region"
            + (f" for a {workload_type} workload" if user_facing else "")
        ),
        recommendation=(
            "Create a guardrail with content filters and a sensitive-information "
            "filter appropriate to this application, and attach it to the model or "
            "inference profile invocation"
            if user_facing else ""
        ),
        fix_type=FixType.CONFIG, effort_minutes=60 if user_facing else 0,
    )


def _check_enforcement(guardrails, enforced) -> Finding:
    if isinstance(guardrails, ScanError):
        return Finding(
            check_id="GR-02", check_name="Account-Level Guardrail Enforcement",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not list guardrails: {guardrails.error}",
            recommendation="Grant bedrock:ListGuardrails",
        )
    if not guardrails:
        return Finding(
            check_id="GR-02", check_name="Account-Level Guardrail Enforcement",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No guardrail exists to enforce",
        )
    if enforced is None or isinstance(enforced, ScanError):
        err = enforced.error if isinstance(enforced, ScanError) else "no data"
        return Finding(
            check_id="GR-02", check_name="Account-Level Guardrail Enforcement",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not check enforced-guardrails configuration: {err}",
            recommendation="Grant bedrock:ListEnforcedGuardrailsConfiguration",
        )
    if enforced:
        return Finding(
            check_id="GR-02", check_name="Account-Level Guardrail Enforcement",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"{len(enforced)} guardrail(s) are enforced automatically at the "
                    f"account/region level",
        )
    return Finding(
        check_id="GR-02", check_name="Account-Level Guardrail Enforcement",
        status=CheckStatus.WARN, impact=2, likelihood=2,
        message=(
            f"{len(guardrails)} guardrail(s) exist but none is configured for "
            f"automatic account-level enforcement. Whether a given invocation applies "
            f"one depends on the calling application passing guardrailIdentifier -- "
            f"that per-call choice is not visible to a read-only scan"
        ),
        recommendation=(
            "If every invocation in this account should be filtered regardless of the "
            "caller, configure enforced guardrails at the account or region level "
            "rather than relying on each caller to pass one"
        ),
        fix_type=FixType.CONFIG, effort_minutes=30,
    )


def _check_sensitive_information_filter(guardrails, details) -> Finding:
    if isinstance(guardrails, ScanError):
        return Finding(
            check_id="GR-03", check_name="Sensitive-Information Filtering",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message=f"Could not list guardrails: {guardrails.error}",
            recommendation="Grant bedrock:ListGuardrails",
        )
    if not guardrails:
        return Finding(
            check_id="GR-03", check_name="Sensitive-Information Filtering",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No guardrail exists to check",
        )
    if details is None or isinstance(details, ScanError):
        # details being a ScanError (not a list) would otherwise reach
        # `for g in details:` below and raise TypeError instead of
        # reporting ERROR.
        err = details.error if isinstance(details, ScanError) else "no data"
        return Finding(
            check_id="GR-03", check_name="Sensitive-Information Filtering",
            status=CheckStatus.ERROR, impact=3, likelihood=2,
            message=f"Could not read guardrail policy configuration: {err}",
            recommendation="Grant bedrock:GetGuardrail",
        )

    without_filter = []
    unreadable = []
    for g in details:
        if "error" in g:
            unreadable.append(g.get("name", g.get("id", "?")))
            continue
        sip = g.get("sensitiveInformationPolicy", {})
        if not sip.get("piiEntities") and not sip.get("regexes"):
            without_filter.append(g.get("name", g.get("guardrailId", "?")))

    if without_filter:
        return Finding(
            check_id="GR-03", check_name="Sensitive-Information Filtering",
            status=CheckStatus.WARN, impact=3, likelihood=2,
            message=f"{len(without_filter)} guardrail(s) have no PII entity or custom "
                    f"regex filter configured: {', '.join(without_filter[:5])}",
            recommendation=(
                "If prompts or responses can contain personal data, add a "
                "sensitive-information filter naming the specific PII entities or "
                "regex patterns relevant to this application"
            ),
            resource_ids=without_filter,
            fix_type=FixType.CONFIG, effort_minutes=30,
        )
    if unreadable:
        # Some guardrails' policy configuration could not be read. The
        # readable ones all had a filter, but an unreadable guardrail might
        # not -- reporting PASS here would claim a clean check that never
        # actually happened for those guardrails.
        readable_count = len(details) - len(unreadable)
        return Finding(
            check_id="GR-03", check_name="Sensitive-Information Filtering",
            status=CheckStatus.WARN, impact=3, likelihood=2,
            message=(
                f"{readable_count} guardrail(s) have a sensitive-information filter "
                f"configured, but {len(unreadable)} guardrail(s) could not be read "
                f"and were NOT checked: {', '.join(unreadable[:5])}"
            ),
            recommendation="Grant bedrock:GetGuardrail for the listed guardrail(s)",
            resource_ids=unreadable[:10],
        )
    return Finding(
        check_id="GR-03", check_name="Sensitive-Information Filtering",
        status=CheckStatus.PASS, impact=3, likelihood=2,
        message="Every guardrail has a sensitive-information filter configured",
    )


def _check_draft_only(guardrails, details) -> Finding:
    if isinstance(guardrails, ScanError):
        return Finding(
            check_id="GR-04", check_name="Guardrail Versioning",
            status=CheckStatus.ERROR, impact=2, likelihood=1,
            message=f"Could not list guardrails: {guardrails.error}",
            recommendation="Grant bedrock:ListGuardrails",
        )
    if not guardrails:
        return Finding(
            check_id="GR-04", check_name="Guardrail Versioning",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message="No guardrail exists to check",
        )
    if details is None or isinstance(details, ScanError):
        err = details.error if isinstance(details, ScanError) else "no data"
        return Finding(
            check_id="GR-04", check_name="Guardrail Versioning",
            status=CheckStatus.ERROR, impact=2, likelihood=1,
            message=f"Could not read guardrail version information: {err}",
            recommendation="Grant bedrock:GetGuardrail",
        )

    unreadable = [g.get("name", g.get("id", "?")) for g in details if "error" in g]
    draft_only = [g.get("name", g.get("guardrailId", "?")) for g in details
                  if "error" not in g and g.get("version") == "DRAFT"]
    if draft_only:
        return Finding(
            check_id="GR-04", check_name="Guardrail Versioning",
            status=CheckStatus.WARN, impact=2, likelihood=1,
            message=f"{len(draft_only)} guardrail(s) exist only as DRAFT, with no "
                    f"published version: {', '.join(draft_only[:5])}",
            recommendation=(
                "Publish a version once the guardrail configuration is stable. DRAFT "
                "changes take effect immediately for anything invoking it, which makes "
                "behaviour harder to pin to a known-good configuration"
            ),
            resource_ids=draft_only,
            fix_type=FixType.CONFIG, effort_minutes=10,
        )
    if unreadable:
        # Previously this branch was unreachable in the count: an unreadable
        # guardrail was silently excluded from draft_only with no trace left
        # anywhere in the finding, so a fully-unreadable guardrail set still
        # reported PASS ("every guardrail has a published version") despite
        # zero guardrails actually having been checked.
        readable_count = len(details) - len(unreadable)
        return Finding(
            check_id="GR-04", check_name="Guardrail Versioning",
            status=CheckStatus.WARN, impact=2, likelihood=1,
            message=(
                f"{readable_count} guardrail(s) have a published version, but "
                f"{len(unreadable)} guardrail(s) could not be read and were NOT "
                f"checked: {', '.join(unreadable[:5])}"
            ),
            recommendation="Grant bedrock:GetGuardrail for the listed guardrail(s)",
            resource_ids=unreadable[:10],
        )
    return Finding(
        check_id="GR-04", check_name="Guardrail Versioning",
        status=CheckStatus.PASS, impact=2, likelihood=1,
        message="Every guardrail has at least one published version",
    )
