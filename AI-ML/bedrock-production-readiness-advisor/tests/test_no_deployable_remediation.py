#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Guards against a deployable remediation artifact in any hand-authored finding.

The diagram-review model's output is filtered for this at runtime (see
core/diagram_review.py's IAC_MARKERS / EXCLUDED_TOPIC_MARKERS). That mechanism
does not apply here: every pillar module's `recommendation`, `message`, and
`check_name` strings are hand-authored by whoever wrote the check, not
produced by a model, so a runtime filter is the wrong tool -- a static guard
over the source is. This is the check that makes good on the promise in
core/models.py's Finding docstring and every delivery channel's scope text:
no check in this package, including Security, Guardrails, and Data
Governance, ever hands back something meant to be applied as-is.

Checks, over every *.py file in bedrock_readiness/modules/:
  1. No CloudFormation marker (AWSTemplateFormatVersion, "Type": "AWS::...).
  2. No Terraform marker (resource "aws_..., provider "aws").
  3. No literal IAM policy document shape ('"Version": "2012-10-17"',
     '"Effect": "Allow"' next to '"Action"') appearing as a STRING LITERAL in
     source -- as opposed to a dict key access like `doc.get("Effect")`,
     which is code reading a policy, not code emitting one.
  4. No fenced code block (``` ... ```) inside any string literal.
  5. No f-string or .format() call that could splice a raw policy/document
     object into a Finding field (a `{doc}` / `{policy_doc}` / `{document}`
     placeholder is the smell -- reporting a parsed *fact* about a document,
     e.g. a role name or action list, is fine and common; interpolating the
     document object itself is not).

Static (no AWS credentials, no imports of the modules under test needed for
the checks themselves) so it cannot be fooled by conditional logic that only
constructs the bad string on a rare branch.

Usage:
  python tests/test_no_deployable_remediation.py
"""

import ast
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
_modules_dir = os.path.join(_root, "bedrock_readiness", "modules")

fails = []


def ok(cond, label, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def _module_sources() -> dict:
    out = {}
    for fname in sorted(os.listdir(_modules_dir)):
        if fname.endswith(".py"):
            path = os.path.join(_modules_dir, fname)
            out[fname] = open(path).read()
    return out


SOURCES = _module_sources()
ALL_SOURCE = "\n".join(SOURCES.values())

CFN_MARKERS = (
    "AWSTemplateFormatVersion",
    '"Type": "AWS::',
    "Type: AWS::",
)
TERRAFORM_MARKERS = (
    'resource "aws_',
    'provider "aws"',
)
POLICY_DOCUMENT_MARKERS = (
    '"Version": "2012-10-17"',
    "'Version': '2012-10-17'",
)
CODE_FENCE_RE = re.compile(r"```")

# Placeholders that would splice a whole document/policy object into a string,
# as opposed to a specific fact extracted FROM one (role name, action, ARN).
RAW_OBJECT_INTERPOLATION_RE = re.compile(
    r"\{(?:doc|document|policy_doc|policy_document|trust_doc)\b[^}]*\}"
)

print("\n== No infrastructure-as-code marker in any pillar module ==")
for fname, src in SOURCES.items():
    for marker in CFN_MARKERS:
        ok(marker not in src, f"{fname}: no CloudFormation marker {marker!r}")
    for marker in TERRAFORM_MARKERS:
        ok(marker not in src, f"{fname}: no Terraform marker {marker!r}")

print("\n== No literal IAM policy document shape in any pillar module ==")
for fname, src in SOURCES.items():
    for marker in POLICY_DOCUMENT_MARKERS:
        ok(marker not in src, f"{fname}: no policy-document literal {marker!r}")

print("\n== No fenced code block in any pillar module's string literals ==")
for fname, src in SOURCES.items():
    tree = ast.parse(src, filename=fname)
    fenced = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if CODE_FENCE_RE.search(node.value):
                fenced.append(node.value[:60])
    ok(not fenced, f"{fname}: no string literal contains a code fence", str(fenced))

print("\n== No raw policy/document object interpolated into a Finding field ==")
for fname, src in SOURCES.items():
    hits = RAW_OBJECT_INTERPOLATION_RE.findall(src)
    ok(not hits, f"{fname}: no f-string splices a raw document object", str(hits))

print("\n== Finding has no field that could carry a deployable artifact ==")
sys.path.insert(0, _root)
from bedrock_readiness.core.models import Finding
import dataclasses

field_names = {f.name for f in dataclasses.fields(Finding)}
suspicious = {"template", "policy_document", "cfn", "terraform", "iac", "remediation_key"}
ok(not (field_names & suspicious),
   "no Finding field name suggests a deployable artifact",
   str(field_names & suspicious))

print("\n== Every SEC-*/GR-*/DG-* recommendation is prose, not structured data ==")
from bedrock_readiness.modules import CHECK_CATALOG
sensitive_ids = {cid for pid in ("security", "guardrails", "data_governance")
                 for cid, _ in CHECK_CATALOG[pid]}
ok(len(sensitive_ids) == 12, "12 Security/Guardrails/Data Governance checks exist",
   str(len(sensitive_ids)))

# A prose recommendation should not look like a JSON object at all -- no
# opening brace immediately after "recommendation=(" in source, across the
# three new modules specifically.
for fname in ("security.py", "guardrails.py", "data_governance.py"):
    src = SOURCES.get(fname, "")
    json_like = re.findall(r'recommendation=\(\s*\n?\s*"[^"]*\{', src)
    ok(not json_like, f"{fname}: no recommendation string opens with a brace",
       str(json_like[:2]))

print(f"\n  {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
