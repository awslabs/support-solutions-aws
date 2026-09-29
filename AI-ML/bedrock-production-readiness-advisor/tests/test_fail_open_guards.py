#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Guards against fail-open reporting: a check that could not fully verify its
data must never return PASS with the failure buried in a footnote (or, worse,
with no trace of it at all). For a readiness tool, reporting clean when it
couldn't actually check is false assurance.

This was a real bug: SEC-02 and GR-03 returned PASS with a parenthetical
"(N could not be read)" note when some IAM policy documents / guardrails
were unreadable and none of the readable ones showed the bad condition.
GR-04 was worse -- it filtered out unreadable guardrails with no trace at
all, so a fully-unreadable guardrail set still reported a clean PASS. DG-02/
DG-03 also collapsed "the read failed" and "there is nothing to check" into
the same SKIPPED outcome via get_logging_destination_security() returning
None for both cases. DG-04 didn't check for a ScanError at all, so a failed
logs:DescribeLogGroups call would have been iterated as if it were a list of
log groups and raised an unhandled TypeError instead of a Finding.

A related bug, found later in review: guardrails.py never referenced
ScanError at all, in any of its four checks. list_guardrails(),
list_enforced_guardrails_configuration(), and get_guardrail_details() all
return a ScanError *instance* on failure, not None. ScanError has no
__bool__/__len__ override, so it is truthy and not iterable -- a failed
ListGuardrails call would reach `if guardrails:` (true) and then
`len(guardrails)`, raising TypeError instead of a Finding, in every one of
GR-01 through GR-04.

Each check below is exercised directly (not through a full fixture) with a
synthetic partial-failure input, and asserted to NOT report PASS (or to fail
outright, for the crash cases).

No AWS credentials required.

Usage:
  python tests/test_fail_open_guards.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bedrock_readiness.core.models import CheckStatus
from bedrock_readiness.core.scanner import ScanError
from bedrock_readiness.modules.security import _check_wildcard_policy_documents
from bedrock_readiness.modules.guardrails import (
    _check_any_guardrail_exists,
    _check_enforcement,
    _check_sensitive_information_filter,
    _check_draft_only,
)
from bedrock_readiness.modules.data_governance import (
    _check_s3_logging_encryption,
    _check_s3_logging_public_access,
    _check_log_group_encryption,
)

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


print("== SEC-02: unreadable policy document must not yield a clean PASS ==")
role_docs = [
    {"role": "RoleA", "arn": "arn:aws:iam::1:role/RoleA", "policy": "Inline1",
     "type": "inline", "wildcards": False},
    {"role": "RoleB", "arn": "arn:aws:iam::1:role/RoleB", "policy": "Managed1",
     "type": "attached", "error": "AccessDenied: not authorized to perform: iam:GetPolicyVersion"},
]
finding = _check_wildcard_policy_documents(role_docs)
check("status is not PASS when a policy document could not be read", finding.status != CheckStatus.PASS)
check("status is WARN (not silently ERROR/SKIPPED either)", finding.status == CheckStatus.WARN)
check("the unreadable role is named in the message", "RoleB" in finding.message)
check("the underlying error is surfaced, not swallowed", "AccessDenied" in finding.message)

print()
print("== SEC-02: all-readable, no-wildcard input still reports a clean PASS ==")
clean_docs = [
    {"role": "RoleA", "arn": "arn:aws:iam::1:role/RoleA", "policy": "Inline1",
     "type": "inline", "wildcards": False},
]
finding = _check_wildcard_policy_documents(clean_docs)
check("status is PASS when everything was actually readable", finding.status == CheckStatus.PASS)

print()
print("== GR-03: unreadable guardrail must not yield a clean PASS ==")
details = [
    {"id": "gr-1", "name": "GuardrailA", "sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL"}]}},
    {"id": "gr-2", "name": "GuardrailB", "error": "AccessDenied: bedrock:GetGuardrail"},
]
finding = _check_sensitive_information_filter(["gr-1", "gr-2"], details)
check("status is not PASS when a guardrail could not be read", finding.status != CheckStatus.PASS)
check("status is WARN", finding.status == CheckStatus.WARN)
check("the unreadable guardrail is named in the message", "GuardrailB" in finding.message)

print()
print("== GR-04: fully-unreadable guardrail set must not yield a clean PASS ==")
all_unreadable = [
    {"id": "gr-1", "name": "GuardrailA", "error": "AccessDenied: bedrock:GetGuardrail"},
]
finding = _check_draft_only(["gr-1"], all_unreadable)
check("status is not PASS when every guardrail was unreadable", finding.status != CheckStatus.PASS)
check("status is WARN", finding.status == CheckStatus.WARN)
check("the unreadable guardrail is named in the message", "GuardrailA" in finding.message)

print()
print("== GR-01: a failed ListGuardrails call must not crash on len(ScanError) ==")
guardrails_error = ScanError("bedrock", "list_guardrails", "AccessDeniedException")
try:
    finding_gr01 = _check_any_guardrail_exists(guardrails_error, "customer-facing-chatbot")
    crashed_gr01 = False
except TypeError:
    finding_gr01 = None
    crashed_gr01 = True
check("does not raise len() on a ScanError", not crashed_gr01)
check("reports ERROR rather than a false PASS", finding_gr01 is not None and finding_gr01.status == CheckStatus.ERROR)
check("the underlying error is surfaced", finding_gr01 is not None and "AccessDeniedException" in finding_gr01.message)

print()
print("== GR-02: a failed ListGuardrails/enforced-config call must not crash either ==")
try:
    finding_gr02a = _check_enforcement(guardrails_error, [])
    crashed_gr02a = False
except TypeError:
    finding_gr02a = None
    crashed_gr02a = True
check("GR-02 does not raise on guardrails=ScanError", not crashed_gr02a)
check("GR-02 reports ERROR for guardrails=ScanError", finding_gr02a is not None and finding_gr02a.status == CheckStatus.ERROR)

enforced_error = ScanError("bedrock", "list_enforced_guardrails_configuration", "ThrottlingException")
real_guardrails = [{"id": "gr-1", "name": "GuardrailA"}]
try:
    finding_gr02b = _check_enforcement(real_guardrails, enforced_error)
    crashed_gr02b = False
except TypeError:
    finding_gr02b = None
    crashed_gr02b = True
check("GR-02 does not raise on enforced=ScanError", not crashed_gr02b)
check("GR-02 reports ERROR for enforced=ScanError", finding_gr02b is not None and finding_gr02b.status == CheckStatus.ERROR)

print()
print("== scanner.py never puts a raw exception message into a ScanError ==")
import re
scanner_path = os.path.join(os.path.dirname(__file__), "..", "bedrock_readiness", "core", "scanner.py")
with open(scanner_path) as f:
    scanner_source = f.read()
raw_str_e = re.findall(r"\bstr\(e\)", scanner_source)
check("no str(e) remains in scanner.py (use type(e).__name__ instead)", len(raw_str_e) == 0)

print()
print("== DG-02/DG-03: a failed logging-config read is distinct from 'no destination' ==")
sec_error = ScanError("bedrock", "get_model_invocation_logging_configuration", "AccessDenied")
finding02 = _check_s3_logging_encryption({"s3": {"logging_destination_security": sec_error}})
finding03 = _check_s3_logging_public_access({"s3": {"logging_destination_security": sec_error}})
check("DG-02 reports ERROR, not SKIPPED, on a failed read", finding02.status == CheckStatus.ERROR)
check("DG-02 does not claim 'no S3 destination is configured'",
      "no S3 destination" not in finding02.message.lower())
check("DG-03 reports ERROR, not SKIPPED, on a failed read", finding03.status == CheckStatus.ERROR)
check("DG-03 does not claim 'no S3 destination is configured'",
      "no S3 destination" not in finding03.message.lower())

print()
print("== DG-02/DG-03: a genuinely absent destination still reports SKIPPED ==")
finding02_none = _check_s3_logging_encryption({"s3": {"logging_destination_security": None}})
finding03_none = _check_s3_logging_public_access({"s3": {"logging_destination_security": None}})
check("DG-02 reports SKIPPED when there really is no destination", finding02_none.status == CheckStatus.SKIPPED)
check("DG-03 reports SKIPPED when there really is no destination", finding03_none.status == CheckStatus.SKIPPED)

print()
print("== DG-04: a failed logs:DescribeLogGroups call must not crash or fail open ==")
log_groups_error = ScanError("logs", "describe_log_groups", "ThrottlingException")
try:
    finding04 = _check_log_group_encryption({"cloudwatch": {"bedrock_log_groups": log_groups_error}})
    crashed = False
except TypeError:
    finding04 = None
    crashed = True
check("does not raise iterating a ScanError", not crashed)
check("reports ERROR rather than a false PASS/SKIPPED", finding04 is not None and finding04.status == CheckStatus.ERROR)

print()
print("== model_fitness: premium/legacy marker lists are config-overridable ==")
from bedrock_readiness.core.models import Config
from bedrock_readiness.modules.model_fitness import PREMIUM_MODEL_MARKERS, LEGACY_MODEL_MARKERS

check("nova-premier is in the default premium markers", "nova-premier" in PREMIUM_MODEL_MARKERS)
default_cfg = Config()
check("Config defaults leave markers as None (falls back to module defaults)",
      default_cfg.premium_model_markers is None and default_cfg.legacy_model_markers is None)
custom_cfg = Config(premium_model_markers=["my-custom-model"], legacy_model_markers=["old-model"])
effective_premium = custom_cfg.premium_model_markers or PREMIUM_MODEL_MARKERS
effective_legacy = custom_cfg.legacy_model_markers or LEGACY_MODEL_MARKERS
check("a custom premium_model_markers list overrides the default", effective_premium == ["my-custom-model"])
check("a custom legacy_model_markers list overrides the default", effective_legacy == ["old-model"])

print()
print("=" * 70)
print(f"  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
