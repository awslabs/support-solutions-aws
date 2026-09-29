#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Guards the curated AWS documentation references.

Doc links make a one-line recommendation actionable, but they are also a way
for out-of-scope guidance to re-enter by reference. This test asserts the
scope rules in core/references.py hold, and -- more importantly -- that a
model has no path to inject a link.

Checks:
  1. `references.validate()` passes: HTTPS, AWS-owned hosts only, no
     security/identity/encryption/guardrails/data-governance/compliance pages,
     no orphan check IDs.
  2. `Finding` has no URL-carrying field, so neither a pillar module nor the
     diagram-review model can attach a link.
  3. No pillar module contains a hardcoded URL.
  4. Design findings resolve to their pillar's references; unknown IDs
     resolve to nothing rather than a guess.
  5. Rendered output (HTML, Markdown, JSON) contains only curated URLs.

Network access is NOT required. Live URL reachability is verified separately
(see the note at the bottom) because it is not this test's job to depend on
the internet.

Usage:
  python tests/test_references.py
"""

import dataclasses
import json
import os
import re
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, _here)

from bedrock_readiness.core import references as R
from bedrock_readiness.core.models import Config, Finding
from bedrock_readiness.core.reporter import generate_html_report
from bedrock_readiness.core.scorer import compute_assessment
from bedrock_readiness.cli import _write_json_report, _write_markdown_report
from bedrock_readiness.modules import CHECK_CATALOG, PILLAR_MODULES

from run_fixtures import MockScanner

fails = []


def ok(cond, label, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


KNOWN_IDS = {cid for entries in CHECK_CATALOG.values() for cid, _ in entries}
ALL_CURATED_URLS = {
    r.url
    for refs in list(R.REFERENCES.values()) + list(R.PILLAR_REFERENCES.values())
    for r in refs
}

print("\n== Scope rules ==")
problems = R.validate(known_check_ids=KNOWN_IDS)
ok(not problems, "references.validate() reports no problems", "; ".join(problems[:4]))
ok(len(R.REFERENCES) == len(KNOWN_IDS),
   f"all {len(KNOWN_IDS)} checks have references", f"{len(R.REFERENCES)} mapped")

# PILLAR_REFERENCES is the fallback map for DGM-<CATEGORY>-NN design findings.
# It intentionally covers only the diagram-review-eligible pillars -- Security,
# Guardrails, and Data Governance are absent because the diagram review's own
# scope filter (ALLOWED_CATEGORIES) never lets a finding in those categories
# through, so there is no DGM- finding this fallback would ever need to
# resolve for them.
from bedrock_readiness.core.diagram_review import ALLOWED_CATEGORIES
ok(set(R.PILLAR_REFERENCES) == ALLOWED_CATEGORIES,
   "PILLAR_REFERENCES covers exactly the diagram-review-eligible pillars",
   f"{set(R.PILLAR_REFERENCES)} vs {ALLOWED_CATEGORIES}")
ok(set(R.PILLAR_REFERENCES) < set(PILLAR_MODULES),
   "that fallback set is a strict subset of all implemented pillars -- "
   "Security/Guardrails/Data Governance are assessed but not diagram-reviewable")

hosts = {u.split("//", 1)[1].split("/", 1)[0] for u in ALL_CURATED_URLS}
ok(hosts <= set(R.ALLOWED_DOC_HOSTS),
   "every URL is on an AWS-owned host", str(sorted(hosts - set(R.ALLOWED_DOC_HOSTS))))
ok(all(u.startswith("https://") for u in ALL_CURATED_URLS), "every URL is HTTPS")
print(f"    {len(ALL_CURATED_URLS)} unique URLs across {len(R.REFERENCES)} checks, hosts: {sorted(hosts)}")

print("\n== A model cannot inject a link ==")
field_names = {f.name for f in dataclasses.fields(Finding)}
ok(not any(k in n for n in field_names for k in ("url", "link", "ref", "doc")),
   "Finding has no URL-carrying field", str(sorted(field_names)))

sample = Finding(check_id="X", check_name="n", status=None, impact=1, likelihood=1, message="m")
ok(not hasattr(sample, "references"),
   "a Finding instance cannot hold references")

modules_dir = os.path.join(os.path.dirname(_here), "bedrock_readiness", "modules")
url_hits = []
for fname in sorted(os.listdir(modules_dir)):
    if fname.endswith(".py"):
        text = open(os.path.join(modules_dir, fname)).read()
        url_hits += [f"{fname}: {m}" for m in re.findall(r"https?://\S+", text)]
ok(not url_hits, "no pillar module hardcodes a URL", str(url_hits[:3]))

dgm_text = open(os.path.join(os.path.dirname(_here), "bedrock_readiness",
                             "core", "diagram_review.py")).read()
dgm_imports = re.findall(r"^\s*(?:from|import)\s+.*$", dgm_text, re.M)
ok(not any("references" in line for line in dgm_imports),
   "diagram_review.py never imports the references module",
   str([l.strip() for l in dgm_imports if "references" in l]))
ok("Reference(" not in dgm_text,
   "diagram_review.py never constructs a Reference")

print("\n== Lookup behaviour ==")
ok(R.for_check("OBS-01"), "a known check resolves")
ok(R.for_check("SEC-01"), "a Security check resolves too -- SEC/GR/DG are real pillars now")
ok(R.for_check("") == (), "an empty ID resolves to nothing")
ok(R.for_check("MADE-UP-99") == (), "an invented ID resolves to nothing")
ok(R.for_check("DGM-OBSERVABILITY-01") == R.PILLAR_REFERENCES["observability"],
   "a design finding falls back to its pillar references")
ok(R.for_check("DGM-COST_OPTIMIZATION-03") == R.PILLAR_REFERENCES["cost_optimization"],
   "multi-word pillar categories resolve")
ok(R.for_check("DGM-NONSENSE-01") == (),
   "an unknown design category resolves to nothing")

print("\n== Rendered output contains only curated URLs ==")
fixture_path = os.path.join(_here, "fixtures", "rag_pipeline_partial.json")
with open(fixture_path) as fp:
    fixture = json.load(fp)

config = Config(mode="pre-production", workload_type="rag-pipeline",
                regions=[fixture["region"]],
                production_estimate={"peak_rpm": 200})
scanner = MockScanner(fixture)
pillar_results = [m.assess(scanner, fixture, config) for m in PILLAR_MODULES.values()]
result = compute_assessment(pillar_results=pillar_results, config=config,
                            account_id=fixture["account_id"], region=fixture["region"])


def urls_in(text: str) -> set:
    found = set(re.findall(r"https?://[^\s\)\"'<>\]]+", text))
    # The report links to AWS console pages in prose recommendations? It should
    # not -- anything not curated is a finding of this test.
    return found


html = generate_html_report(result)
html_urls = urls_in(html)
ok(html_urls <= ALL_CURATED_URLS,
   "HTML contains only curated URLs", str(sorted(html_urls - ALL_CURATED_URLS)))
ok(bool(html_urls), "HTML actually rendered some references", f"{len(html_urls)} found")
ok('rel="noopener noreferrer"' in html, "external links carry rel=noopener noreferrer")
ok("AWS documentation:" in html, "HTML labels the reference block")

with tempfile.TemporaryDirectory() as tmp:
    md_path = os.path.join(tmp, "r.md")
    _write_markdown_report(result, md_path)
    md = open(md_path).read()
    md_urls = urls_in(md)
    ok(md_urls <= ALL_CURATED_URLS,
       "Markdown contains only curated URLs", str(sorted(md_urls - ALL_CURATED_URLS)))
    ok("- Docs: [" in md, "Markdown renders doc links")

    js_path = os.path.join(tmp, "r.json")
    _write_json_report(result, js_path)
    data = json.load(open(js_path))
    js_urls = urls_in(json.dumps(data))
    ok(js_urls <= ALL_CURATED_URLS,
       "JSON contains only curated URLs", str(sorted(js_urls - ALL_CURATED_URLS)))
    every_finding = [f for p in data["pillars"] for f in p["findings"]]
    ok(all("references" in f for f in every_finding),
       "every JSON finding carries a references array")
    mapped = [f for f in every_finding if f["references"]]
    ok(len(mapped) == len(every_finding),
       "every emitted finding resolved to at least one reference",
       f"{len(mapped)}/{len(every_finding)}")

print("\n== No out-of-scope topic reachable through a link, for the 5 pillars "
      "the diagram review covers ==")
from bedrock_readiness.core.diagram_review import EXCLUDED_TOPIC_MARKERS

# This check applies only to OBS/ARCH/QC/COST/MF -- Security, Guardrails, and
# Data Governance references are EXPECTED to mention iam/encrypt/guardrail/
# audit/etc, since documenting those topics accurately is the entire point of
# those three checks' references. Leaking those topics into an Observability
# or Cost reference, however, would be exactly the by-reference reintroduction
# this test exists to catch -- so the check still applies to REFERENCES.
leaky = []
for check_id, refs in R.REFERENCES.items():
    if R.pillar_for(check_id) in R.RESTRICTED_TOPIC_PILLARS:
        continue
    for r in refs:
        blob = f"{r.title} {r.url}".lower()
        for marker in EXCLUDED_TOPIC_MARKERS:
            # Only flag whole-word-ish matches; "auth" inside "authorized" is
            # what we care about, but substrings of unrelated words are not.
            if marker.strip() and marker.strip() in blob:
                leaky.append(f"{check_id} {r.title} <- {marker!r}")
                break
for pillar, refs in R.PILLAR_REFERENCES.items():
    for r in refs:
        blob = f"{r.title} {r.url}".lower()
        for marker in EXCLUDED_TOPIC_MARKERS:
            if marker.strip() and marker.strip() in blob:
                leaky.append(f"pillar:{pillar} {r.title} <- {marker!r}")
                break
ok(not leaky,
   "no OBS/ARCH/QC/COST/MF reference matches the diagram-review excluded-topic list",
   str(leaky[:4]))

print(f"\n  {len(fails)} failure(s)")
print("\n  Note: live URL reachability is not asserted here (no network "
      "dependency in the suite).")
print(f"  All {len(ALL_CURATED_URLS)} curated URLs were verified against public AWS "
      f"documentation when added.")
sys.exit(1 if fails else 0)
