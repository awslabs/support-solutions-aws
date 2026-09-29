#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test runner -- exercises the readiness-only assessment pipeline against
fixture data. No AWS credentials needed.

Usage:
  python tests/run_fixtures.py                    # Run all fixtures
  python tests/run_fixtures.py chatbot_preprod    # Run single fixture
  python tests/run_fixtures.py --html             # Generate HTML reports
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bedrock_readiness.core.models import Config
from bedrock_readiness.core.scorer import compute_assessment
from bedrock_readiness.core.reporter import generate_html_report
from bedrock_readiness.modules import PILLAR_MODULES
from bedrock_readiness.core.scanner import ScanError


class MockScanner:
    """Mock scanner that returns fixture data instead of calling AWS."""

    def __init__(self, fixture_data: dict):
        self.account_id = fixture_data["account_id"]
        self.region = fixture_data["region"]
        self.api_call_count = 0
        self._errors = []
        self._fixture = fixture_data

    @property
    def errors(self):
        return self._errors

    def get_metric_statistics(self, **kwargs):
        return {"Datapoints": []}

    def get_metric_data_sum(self, *args, **kwargs):
        return {"MetricDataResults": [{"Values": []}]}

    def list_metrics(self, namespace: str, metric_name: str = ""):
        return []

    def scan_all(self):
        return self._fixture


FIXTURE_CONFIGS = {
    "chatbot_preprod": {
        "mode": "pre-production",
        "workload_type": "customer-facing-chatbot",
        "production_estimate": {"peak_rpm": 2000, "average_rpm": 800},
    },
    "multi_agent_prod": {
        "mode": "production",
        "workload_type": "multi-agent",
        "production_estimate": {"peak_rpm": 500},
    },
    "rag_pipeline_partial": {
        "mode": "pre-production",
        "workload_type": "rag-pipeline",
        "production_estimate": {"peak_rpm": 200, "average_rpm": 80},
    },
}


def run_fixture(fixture_name: str, generate_html: bool = False):
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", f"{fixture_name}.json")
    if not os.path.exists(fixture_path):
        print(f"Fixture not found: {fixture_path}")
        return None

    with open(fixture_path) as f:
        fixture_data = json.load(f)

    print(f"\n{'=' * 70}")
    print(f"  FIXTURE: {fixture_name}")
    print(f"  {fixture_data.get('description', '')}")
    print(f"{'=' * 70}\n")

    cfg_data = FIXTURE_CONFIGS.get(fixture_name, {})
    config = Config(
        mode=cfg_data.get("mode", "auto"),
        workload_type=cfg_data.get("workload_type", "general"),
        regions=[fixture_data["region"]],
        production_estimate=cfg_data.get("production_estimate", {}),
    )

    scanner = MockScanner(fixture_data)
    scan_data = fixture_data

    start = time.time()
    pillar_results = []
    for pillar_id, module in PILLAR_MODULES.items():
        pr = module.assess(scanner, scan_data, config)
        pillar_results.append(pr)
        status = "OK  " if pr.score >= 70 else "WARN" if pr.score >= 50 else "FAIL"
        print(f"  [{status}] {pr.pillar_name:<28} {pr.score:>3}/100  "
              f"({pr.passed_checks}/{pr.total_checks} passed, {pr.failed_checks} failed)")

    duration = time.time() - start

    result = compute_assessment(
        pillar_results=pillar_results,
        config=config,
        account_id=fixture_data["account_id"],
        region=fixture_data["region"],
        scan_duration=duration,
        api_calls=45,
    )

    counts = result.severity_summary.get("counts", {})
    effort = result.severity_summary.get("effort", {})

    print(f"\n  {'-' * 60}")
    print(f"  Overall Score: {result.overall_score}/100")
    print(f"  Critical: {counts.get('CRITICAL', 0)}  |  High: {counts.get('HIGH', 0)}  |  "
          f"Medium: {counts.get('MEDIUM', 0)}  |  Low: {counts.get('LOW', 0)}")
    print(f"  Effort (Critical + High): ~{effort.get('CRITICAL', 0) + effort.get('HIGH', 0)} min")
    print(f"  {'-' * 60}")

    if result.priority_actions:
        print("\n  Priority Actions (top 5, ranked by risk):")
        for i, fix in enumerate(result.priority_actions[:5], 1):
            print(f"    {i}. [{fix['severity']:<8}] {fix['check_name']:<35} "
                  f"({fix['fix_type']}, ~{fix['effort_minutes']} min)")

    if generate_html:
        html = generate_html_report(result)
        output_path = os.path.join(os.path.dirname(__file__), f"output_{fixture_name}.html")
        with open(output_path, "w") as fp:
            fp.write(html)
        print(f"\n  HTML report: {output_path}")

    return result


def main():
    generate_html = "--html" in sys.argv
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
    all_fixtures = [f.replace(".json", "") for f in os.listdir(fixtures_dir) if f.endswith(".json")]

    requested = [a for a in sys.argv[1:] if not a.startswith("--")]
    fixtures_to_run = [f for f in requested if f in all_fixtures] if requested else sorted(all_fixtures)
    if requested and not fixtures_to_run:
        print(f"Available fixtures: {', '.join(all_fixtures)}")
        sys.exit(1)

    print(f"\nRunning {len(fixtures_to_run)} fixture(s): {', '.join(fixtures_to_run)}")

    results = {}
    for fixture in fixtures_to_run:
        result = run_fixture(fixture, generate_html=generate_html)
        if result:
            results[fixture] = result

    if len(results) > 1:
        print(f"\n\n{'=' * 70}")
        print("  SUMMARY")
        print(f"{'=' * 70}\n")
        print(f"  {'Fixture':<25} {'Score':>6}  {'Crit':>4} {'High':>4} {'Med':>4} {'Low':>4}")
        print(f"  {'-' * 60}")
        for name, r in results.items():
            c = r.severity_summary.get("counts", {})
            print(f"  {name:<25} {r.overall_score:>3}/100  "
                  f"{c.get('CRITICAL', 0):>4} {c.get('HIGH', 0):>4} {c.get('MEDIUM', 0):>4} {c.get('LOW', 0):>4}")


if __name__ == "__main__":
    main()
