#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# GPU Capacity Planning Agent — recordable demo wrapper.
#
# Usage:
#   ./examples/demo_recorded.sh                 # all 4 scenarios at recording pace
#   ./examples/demo_recorded.sh feasible        # single scenario
#   ./examples/demo_recorded.sh --fast          # zero pauses (for QA / CI)
#   ./examples/demo_recorded.sh --live "..."    # live AWS query via ./examples/query.sh
#
# The script sets:
#   PYTHONUNBUFFERED=1  — so recording tools see output as it prints
#   DEMO_SPEED          — pacing multiplier (default 0.6, --fast sets 0)
#   NO_COLOR            — leave unset for colour (recommended for video)
#
# Requires: python (uses .venv/bin/python if present, else python3), AWS
# credentials ONLY for --live mode.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Pick the interpreter — prefer the project venv for reproducibility.
if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

# --- Parse flags ---
FAST=0
LIVE=0
QUERY=""
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast)
            FAST=1
            shift
            ;;
        --live)
            LIVE=1
            shift
            if [[ $# -gt 0 && "$1" != --* ]]; then
                QUERY="$1"
                shift
            fi
            ;;
        -h|--help)
            sed -n '2,17p' "$0"
            exit 0
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

# Slow-typing costs the recording nothing when NO_COLOR is set — colour off
# breaks the ANSI escape sequences, so tests should also set NO_COLOR.
export PYTHONUNBUFFERED=1

if [[ "$FAST" == "1" ]]; then
    export DEMO_SPEED=0
fi

# --- Live mode: proxy to query.sh which hits the deployed stack ---
if [[ "$LIVE" == "1" ]]; then
    if [[ -z "$QUERY" ]]; then
        echo "Live mode requires a query, e.g.:" >&2
        echo "  $0 --live \"22 g6 in af-south-1 for prod\"" >&2
        exit 1
    fi
    exec ./examples/query.sh "$QUERY"
fi

# --- Recorded mode ---
if [[ ${#POSITIONAL[@]} -gt 0 ]]; then
    "$PY" examples/demo_recorded.py "${POSITIONAL[@]}"
else
    "$PY" examples/demo_recorded.py
fi
