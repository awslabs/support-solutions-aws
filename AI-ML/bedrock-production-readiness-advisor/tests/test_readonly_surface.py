#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Derives this package's real AWS API surface from source and checks the claims.

The read-only guarantee is asserted in the README, in `--dry-run` output, in
report footers, and in the AppSec position. Prose can drift from code, so this
test parses every module and checks the actual call sites instead of trusting
the declarations.

Checks:
  1. Every operation called in source is declared in READ_ONLY_OPERATIONS or
     CONDITIONAL_OPERATIONS.
  2. Every declared operation is actually called somewhere -- no phantom
     entries padding the list.
  3. No operation in NEVER_CALLED_OPERATIONS appears anywhere in the package.
  4. No mutating operation is called at all: nothing matching create_/delete_/
     put_/update_/modify_/attach_/detach_/terminate_/deregister_/disassociate_.
  5. `--dry-run` output is rendered from the declarations, not a separate list.

No AWS credentials and no network required -- this reads source only.

Usage:
  python tests/test_readonly_surface.py
"""

import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
_pkg = os.path.join(_root, "bedrock_readiness")
sys.path.insert(0, _root)

from bedrock_readiness.core.scanner import (
    READ_ONLY_OPERATIONS,
    CONDITIONAL_OPERATIONS,
    NEVER_CALLED_OPERATIONS,
    api_name,
)

fails = []


def ok(cond, label, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def _package_sources() -> dict:
    out = {}
    for root, _, files in os.walk(_pkg):
        if "__pycache__" in root:
            continue
        for f in sorted(files):
            if f.endswith(".py"):
                path = os.path.join(root, f)
                out[os.path.relpath(path, _root)] = open(path).read()
    return out


SOURCES = _package_sources()
ALL_SOURCE = "\n".join(SOURCES.values())

DECLARED = {(s, m) for s, m, _ in READ_ONLY_OPERATIONS}
DECLARED_CONDITIONAL = {(s, m) for s, m, _ in CONDITIONAL_OPERATIONS}
NEVER = {(s, m) for s, m, _ in NEVER_CALLED_OPERATIONS}

# Call sites that do not go through _safe_call, mapped to the service they use.
# Kept explicit because a regex cannot reliably infer the service from a local
# variable name, and getting this wrong would weaken the whole test.
DIRECT_CLIENT_PATTERNS = [
    (r'session\.client\(\s*["\']([a-z0-9-]+)["\']', "client construction"),
]

# service -> local variable name used for its client in the source
CLIENT_VARS = {
    "sts": "sts",
    "iam": "iam",
    "ce": "ce",
    "s3": "s3",
    "cloudwatch": "cloudwatch",
}

MUTATING_PREFIXES = (
    "create", "delete", "put", "update", "modify", "attach", "detach",
    "terminate", "deregister", "disassociate", "remove", "revoke", "cancel",
    "enable", "disable", "associate", "register", "import", "export",
    "tag", "untag", "invoke_endpoint", "publish", "send",
)


print("\n== Operations reached via _safe_call ==")
safe_calls = set()
for rel, src in SOURCES.items():
    for svc, method in re.findall(
        r'_safe_call\(\s*["\']([a-z0-9-]+)["\']\s*,\s*["\']([a-z0-9_]+)["\']', src
    ):
        safe_calls.add((svc, method))
print(f"    found {len(safe_calls)} distinct _safe_call operations")

undeclared = sorted(safe_calls - DECLARED - DECLARED_CONDITIONAL)
ok(not undeclared, "every _safe_call operation is declared",
   ", ".join(api_name(s, m) for s, m in undeclared))

print("\n== Operations called directly on a boto3 client ==")
# Match `<var>.<method>(` where <var> is a known client variable.
direct = set()
for rel, src in SOURCES.items():
    for svc, var in CLIENT_VARS.items():
        for method in re.findall(rf'\b{var}\.([a-z_][a-z0-9_]*)\(', src):
            if method in ("client", "get_paginator", "exceptions"):
                continue
            direct.add((svc, method))
    # Paginated calls: get_paginator("list_roles")
    for method in re.findall(r'get_paginator\(\s*["\']([a-z0-9_]+)["\']', src):
        direct.add(("iam", method))
    # bedrock-runtime is built inline in diagram_review
    if "bedrock-runtime" in src:
        for method in re.findall(r'\bclient\.([a-z_][a-z0-9_]*)\(', src):
            direct.add(("bedrock-runtime", method))
print(f"    found {len(direct)} distinct direct-client operations:")
for s, m in sorted(direct):
    print(f"      {api_name(s, m)}")

undeclared_direct = sorted(direct - DECLARED - DECLARED_CONDITIONAL)
ok(not undeclared_direct, "every direct-client operation is declared",
   ", ".join(api_name(s, m) for s, m in undeclared_direct))

print("\n== Declarations are honest (no phantom entries) ==")
actual = safe_calls | direct
phantom = sorted((DECLARED | DECLARED_CONDITIONAL) - actual)
ok(not phantom, "every declared operation is actually called",
   ", ".join(api_name(s, m) for s, m in phantom))
print(f"    {len(DECLARED)} read-only + {len(DECLARED_CONDITIONAL)} conditional declared, "
      f"{len(actual)} found in source")

print("\n== Operations that must never appear ==")
for svc, method, capability in NEVER_CALLED_OPERATIONS:
    hits = [rel for rel, src in SOURCES.items()
            if re.search(rf'\b{method}\s*\(', src)
            or f'"{method}"' in src or f"'{method}'" in src]
    # A mention inside the NEVER_CALLED_OPERATIONS declaration itself is fine.
    hits = [h for h in hits if h != os.path.join("bedrock_readiness", "core", "scanner.py")]
    ok(not hits, f"{api_name(svc, method)} is never called", f"found in {hits}")

print("\n== No mutating operation anywhere in the package ==")
mutations = []
for rel, src in SOURCES.items():
    for prefix in MUTATING_PREFIXES:
        # Require an underscore after the verb so dict.update() and str.split()
        # style calls are not mistaken for boto3 operations.
        for m in re.finditer(rf'\.({prefix}_[a-z0-9_]+)\(', src):
            mutations.append(f"{rel}: .{m.group(1)}()")
ok(not mutations, "no mutating boto3 operation is called",
   "; ".join(sorted(set(mutations))[:6]))
print(f"    scanned {len(SOURCES)} modules for {len(MUTATING_PREFIXES)} mutating verb prefixes")

print("\n== S3 access is read-only ==")
s3_methods = sorted({m for s, m in actual if s == "s3"})
# get_object reads a diagram; get_bucket_encryption/get_public_access_block
# read CONFIGURATION metadata about the one bucket configured as the Bedrock
# invocation-logging destination (Data Governance pillar). All three are
# Get-only -- no bucket enumeration, no object write.
EXPECTED_S3_METHODS = ["get_bucket_encryption", "get_object", "get_public_access_block"]
ok(s3_methods == EXPECTED_S3_METHODS,
   "S3 access is limited to get_object plus the two logging-bucket config reads",
   str(s3_methods))
# Look for call syntax, not the bare word: scanner.py's declaration note says
# "never put_object", which is documentation rather than a call.
write_calls = re.findall(r'\.(put_object|delete_object|delete_objects|copy_object|'
                          r'put_bucket_[a-z_]+|list_objects[a-z_0-9]*)\(', ALL_SOURCE)
ok(not write_calls, "no S3 write or enumeration call exists", str(sorted(set(write_calls))))
ok("upload_file" not in ALL_SOURCE and "upload_fileobj" not in ALL_SOURCE,
   "the tool never uploads a file")

print("\n== --dry-run renders from the declarations ==")
cli_src = SOURCES[os.path.join("bedrock_readiness", "cli.py")]
ok("READ_ONLY_OPERATIONS" in cli_src,
   "cli.py renders READ_ONLY_OPERATIONS rather than its own list")
ok("NEVER_CALLED_OPERATIONS" in cli_src,
   "cli.py renders NEVER_CALLED_OPERATIONS rather than its own list")
# The old hand-maintained list had literal API-name strings in cli.py.
literal_api_names = re.findall(r'"[a-z-]+:[A-Z][A-Za-z]+"', cli_src)
ok(not literal_api_names,
   "cli.py contains no hardcoded API-name strings", str(literal_api_names[:5]))

print("\n== Rendering helper ==")
ok(api_name("bedrock", "list_guardrails") == "bedrock:ListGuardrails",
   "api_name renders boto3 methods as API names",
   api_name("bedrock", "list_guardrails"))
ok(api_name("service-quotas", "list_service_quotas") == "service-quotas:ListServiceQuotas",
   "hyphenated services render correctly")

print(f"\n  {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
