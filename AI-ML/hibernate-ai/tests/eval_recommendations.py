#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline evaluation harness for HibernateAI recommendation quality.

Measures precision/recall of the DETERMINISTIC guardrail (feedback.validate_recommendation)
against a labeled dataset of known-idle vs known-active resources. Runs in CI with no AWS
and no Bedrock: it feeds each labeled case an LLM verdict of "safe_to_save" and checks
whether the guardrail correctly keeps it (truly idle) or overrides it (actually active).

Exit non-zero if precision or recall falls below the threshold, so the pipeline fails.

Usage:  python tests/eval_recommendations.py [--min-precision 0.9] [--min-recall 0.9]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambdas" / "widget"))

import feedback  # noqa: E402


def _res(rid, cpu=None, conns=None, points=168):
    m = {}
    if cpu is not None:
        m["CPUUtilization"] = {"avg": cpu, "max": cpu, "points": points}
    if conns is not None:
        m["DatabaseConnections"] = {"avg": conns, "points": points}
    return {"id": rid, "type": "EC2" if conns is None else "RDS", "metrics_7d": m}


# Labeled dataset: (name, resources, is_truly_idle)
# "idle" == safe to hibernate; "active" == must NOT be hibernated.
DATASET = [
    ("idle-ec2-0cpu",      [_res("i-1", cpu=0.2)],           True),
    ("idle-ec2-low",       [_res("i-2", cpu=3.5)],           True),
    ("active-ec2-busy",    [_res("i-3", cpu=45.0)],          False),
    ("active-ec2-edge",    [_res("i-4", cpu=10.0)],          False),  # at threshold => active
    ("idle-rds-noconn",    [_res("d-1", conns=0.0)],         True),
    ("active-rds-conns",   [_res("d-2", conns=12.0)],        False),
    ("mixed-active",       [_res("i-5", cpu=2.0), _res("d-3", conns=8.0)], False),
    ("mixed-idle",         [_res("i-6", cpu=1.0), _res("d-4", conns=0.0)], True),
]


def evaluate(config=None):
    """Return metrics dict. Positive class = 'recommend hibernate' (guardrail keeps
    safe_to_save). We want high precision (don't hibernate active) and recall (catch idle).
    """
    tp = fp = tn = fn = 0
    for name, resources, truly_idle in DATASET:
        rec = {"project": name, "risk": "safe_to_save", "reason": "llm said so"}
        out = feedback.validate_recommendation(rec, resources, config=config)
        predicted_save = out.get("risk") == "safe_to_save"
        if predicted_save and truly_idle:
            tp += 1
        elif predicted_save and not truly_idle:
            fp += 1  # dangerous: would hibernate an active resource
        elif not predicted_save and not truly_idle:
            tn += 1
        else:
            fn += 1  # overly cautious: kept an idle resource running
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": precision, "recall": recall,
            "total": len(DATASET)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-precision", type=float, default=1.0,
                    help="Fail below this precision (default 1.0 — never hibernate active).")
    ap.add_argument("--min-recall", type=float, default=0.8)
    args = ap.parse_args(argv)

    m = evaluate()
    print(f"Eval over {m['total']} labeled cases: "
          f"precision={m['precision']:.3f} recall={m['recall']:.3f} "
          f"(tp={m['tp']} fp={m['fp']} tn={m['tn']} fn={m['fn']})")
    ok = m["precision"] >= args.min_precision and m["recall"] >= args.min_recall
    if not ok:
        print(f"FAIL: precision>={args.min_precision} recall>={args.min_recall} not met", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
