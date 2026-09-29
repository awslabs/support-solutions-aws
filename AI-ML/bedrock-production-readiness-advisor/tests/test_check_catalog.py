#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Guards the declarative check catalog against drift.

CHECK_CATALOG in bedrock_readiness/modules/__init__.py is what the MCP server's
`list_readiness_checks` tool and `bedrock-readiness checks` report. The
pre-split platform's MCP server hardcoded its own table and it went stale --
missing QC-11, COST-08/09, and the entire Model Fitness, Security, Guardrails,
and Data Governance pillars.

This test asserts:
  1. The catalog is internally well-formed (unique IDs, correct prefixes).
  2. Every catalogued pillar matches PILLAR_MODULES exactly -- no pillar exists
     in one but not the other.
  3. Every check ID the pillar modules actually emit exists in the catalog, so
     adding a check without cataloguing it fails here.
  4. Coverage is reported, since fixture-driven runs cannot reach every
     workload-conditional check.

No AWS credentials required.

Usage:
  python tests/test_check_catalog.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bedrock_readiness.core.models import Config, WORKLOAD_TYPES
from bedrock_readiness.modules import (
    PILLAR_MODULES,
    PILLAR_NAMES,
    CHECK_CATALOG,
    total_check_count,
)

from run_fixtures import MockScanner

EXPECTED_PREFIX = {
    "observability": "OBS",
    "architecture": "ARCH",
    "quota_capacity": "QC",
    "cost_optimization": "COST",
    "model_fitness": "MF",
    "security": "SEC",
    "guardrails": "GR",
    "data_governance": "DG",
}


class RichMockScanner(MockScanner):
    """MockScanner with enough CloudWatch metric data to exercise Model Fitness.

    The base MockScanner returns empty metrics, which makes model_fitness bail
    out early on sample size -- so MF-02/03/04 were never executed by any test.
    This subclass returns two model IDs with enough invocations to pass the
    sample-size gate: one cross-region (us.*) and one legacy (claude-v2), so
    every MF check has something to assert on.
    """

    _MODEL_IDS = (
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0",   # CRIS profile
        "anthropic.claude-v2",                             # legacy marker
    )

    def list_metrics(self, namespace: str, metric_name: str = ""):
        return [
            {"Namespace": namespace, "MetricName": metric_name or "Invocations",
             "Dimensions": [{"Name": "ModelId", "Value": mid}]}
            for mid in self._MODEL_IDS
        ]

    def get_metric_data_sum(self, *args, **kwargs):
        # 100 invocations per model -> 200 total, above MIN_SAMPLE_INVOCATIONS.
        return {"MetricDataResults": [{"Values": [100.0]}]}

    def get_metric_statistics(self, **kwargs):
        return {"Datapoints": [{"Sum": 0.0, "Average": 0.0, "Maximum": 0.0}]}


def _collect_emitted_check_ids() -> dict:
    """Run every fixture against every workload type and collect check IDs."""
    fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    emitted: dict[str, set] = {pid: set() for pid in PILLAR_MODULES}

    for fname in sorted(os.listdir(fixtures_dir)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(fixtures_dir, fname)) as fp:
            fixture = json.load(fp)

        for workload_type in WORKLOAD_TYPES:
            for mode in ("pre-production", "production"):
                config = Config(
                    mode=mode,
                    workload_type=workload_type,
                    regions=[fixture["region"]],
                    production_estimate={"peak_rpm": 500, "average_rpm": 200},
                )
                scanner = RichMockScanner(fixture)
                for pillar_id, module in PILLAR_MODULES.items():
                    pr = module.assess(scanner, fixture, config)
                    for f in pr.findings:
                        emitted[pillar_id].add(f.check_id)
    return emitted


def main() -> int:
    failures = []
    passes = 0

    def ok(condition, label, detail=""):
        nonlocal passes
        if condition:
            passes += 1
            print(f"  [PASS] {label}")
        else:
            failures.append(label)
            print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

    print("\n== Catalog shape ==")
    ok(set(CHECK_CATALOG) == set(PILLAR_MODULES),
       "catalog covers exactly the implemented pillars",
       f"catalog={sorted(CHECK_CATALOG)} modules={sorted(PILLAR_MODULES)}")
    ok(set(CHECK_CATALOG) == set(PILLAR_NAMES),
       "every catalogued pillar has a display name")

    all_ids = [cid for entries in CHECK_CATALOG.values() for cid, _, _ in entries]
    ok(len(all_ids) == len(set(all_ids)),
       "catalog check IDs are unique",
       f"{len(all_ids)} entries, {len(set(all_ids))} unique")
    ok(total_check_count() == len(all_ids),
       f"total_check_count() agrees with the catalog ({total_check_count()})")

    bad_prefix = [
        cid for pid, entries in CHECK_CATALOG.items()
        for cid, _, _ in entries if not cid.startswith(EXPECTED_PREFIX[pid] + "-")
    ]
    ok(not bad_prefix, "every ID uses its pillar's prefix", str(bad_prefix))

    bad_origin = [
        (cid, origin) for entries in CHECK_CATALOG.values()
        for cid, _, origin in entries if origin not in ("platform", "agent")
    ]
    ok(not bad_origin, "every entry records a known origin", str(bad_origin))

    unnamed = [cid for entries in CHECK_CATALOG.values()
               for cid, name, _ in entries if not name.strip()]
    ok(not unnamed, "every entry has a name", str(unnamed))

    print("\n== Emitted checks vs. catalog ==")
    emitted = _collect_emitted_check_ids()

    uncatalogued = []
    for pillar_id, ids in emitted.items():
        catalogued = {cid for cid, _, _ in CHECK_CATALOG[pillar_id]}
        for cid in sorted(ids):
            if cid not in catalogued:
                uncatalogued.append(f"{pillar_id}:{cid}")
    ok(not uncatalogued,
       "every emitted check ID is in the catalog",
       f"missing from catalog: {uncatalogued}")

    print("\n== Coverage (informational) ==")
    total_emitted = 0
    for pillar_id in CHECK_CATALOG:
        catalogued = {cid for cid, _, _ in CHECK_CATALOG[pillar_id]}
        hit = emitted[pillar_id] & catalogued
        total_emitted += len(hit)
        missing = sorted(catalogued - hit)
        pct = int(len(hit) / len(catalogued) * 100) if catalogued else 0
        print(f"  {PILLAR_NAMES[pillar_id]:<28} {len(hit):>2}/{len(catalogued):<2} ({pct:>3}%)"
              + (f"  not exercised: {', '.join(missing)}" if missing else ""))
    overall = int(total_emitted / len(all_ids) * 100)
    print(f"\n  Fixture coverage: {total_emitted}/{len(all_ids)} checks ({overall}%)")
    print("  Unexercised checks are workload- or resource-conditional; the")
    print("  fixtures do not contain every resource combination.")

    # Model Fitness previously had zero coverage -- assert it now runs.
    mf_hit = emitted["model_fitness"] & {cid for cid, _, _ in CHECK_CATALOG["model_fitness"]}
    ok(len(mf_hit) == 4,
       "all 4 Model Fitness checks execute against fixture metrics",
       f"executed: {sorted(mf_hit)}")

    qc11 = "QC-11" in emitted["quota_capacity"]
    ok(qc11, "QC-11 (CRIS usage) executes")
    cost_new = {"COST-08", "COST-09"} <= emitted["cost_optimization"]
    ok(cost_new, "COST-08 and COST-09 execute",
       f"cost IDs seen: {sorted(emitted['cost_optimization'])}")

    print(f"\n  {passes} passed, {len(failures)} failed")
    if failures:
        print("\n  Failures:")
        for f in failures:
            print(f"    - {f}")
        return 1
    return 0


if __name__ == "__main__":
    print("=" * 70)
    print("  Check catalog -- drift guard")
    print("=" * 70)
    sys.exit(main())
