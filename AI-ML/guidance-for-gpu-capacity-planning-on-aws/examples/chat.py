#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Interactive GPU Capacity Planning Agent CLI.

Usage:
    python3 examples/chat.py
"""
from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path

# Suppress boto3 Python 3.9 deprecation warning
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*Boto3 will no longer support.*")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent import CapacityDecisionAgent
from src.tools import ALL_TOOLS


def render_markdown(text: str) -> str:
    """Strip all markdown formatting for clean terminal output."""
    # Remove bold: **text** → text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # Remove italic: *text* → text
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # Remove code backticks: `text` → text
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Remove heading markers: ## Title → Title
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove links: [text](url) → text (url)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 (\2)", text)
    # Remove HTML-like URLs: <url> → url
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    # Remove horizontal rules
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\*\*\*+$", "", text, flags=re.MULTILINE)
    # Remove table separator rows: |---|---|
    text = re.sub(r"^\|[\s\-:|]+\|$", "", text, flags=re.MULTILINE)
    # Clean table pipes: | cell | cell | → cell | cell
    text = re.sub(r"^\|\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*\|$", "", text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

WELCOME = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 GPU Capacity Planning Agent — Interactive Mode
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This agent assesses GPU capacity risk for SageMaker inference deployments.
It checks availability, pricing, quotas, and recommends capacity strategies.

Example queries:
  • "I need to deploy a Mistral 7B model in us-west-2. What's my risk?"
  • "22 g6 instances in af-south-1 for prod, launching in 2 weeks. Risk?"
  • "Is p4d.24xlarge available in us-east-1? Get pricing and quota."
  • "Compare g5.2xlarge vs g6.2xlarge in us-east-1 for a 7B model."

Type 'quit' or 'exit' to leave. Press Ctrl+C to cancel a running query.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def main() -> int:
    print(WELCOME)

    # Set a generous token budget for interactive use (Lambda uses env var)
    import os
    os.environ.setdefault("TOKEN_BUDGET", "80000")

    agent = CapacityDecisionAgent(verbose=False)
    for tool_spec in ALL_TOOLS:
        agent.register_tool(**tool_spec)

    print(f"  Model: {agent.model_id}")
    print(f"  Region: {agent.region}")
    print(f"  Tools: {', '.join(agent.tools.keys())}")
    print()

    while True:
        try:
            query = input("📝 Ask: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye!")
            return 0

        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            print("\nGoodbye!")
            return 0

        print("\n⏳ Thinking...\n")

        try:
            result = agent.run(query)
        except KeyboardInterrupt:
            print("\n\n⚠️  Query cancelled.\n")
            continue
        except Exception as exc:
            print(f"\n❌ Error: {exc}\n")
            continue

        # Print the answer (rendered for terminal)
        answer = result.get("final_text", "(no answer)")
        print(render_markdown(answer))

        # Print metadata footer
        usage = result.get("usage", {})
        total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        tool_count = len(result.get("tool_calls", []))
        stop_reason = result.get("stop_reason", "unknown")
        incomplete = stop_reason != "end_turn"

        print()
        print("━" * 80)
        print(
            f"📊 Tools: {tool_count} | "
            f"Tokens: {total_tokens:,} | "
            f"Cost: ~${total_tokens * 0.000007:.3f} | "
            f"Status: {'⚠️  Incomplete' if incomplete else '✅ Complete'}"
        )

        # Show tool outcomes
        outcomes = result.get("tool_outcomes", {})
        succeeded = outcomes.get("succeeded", [])
        failed = outcomes.get("failed", [])
        if succeeded:
            print(f"  ✅ Succeeded: {', '.join(succeeded)}")
        if failed:
            print(f"  ❌ Failed: {', '.join(f['tool'] for f in failed)}")

        print("━" * 80)
        print()


if __name__ == "__main__":
    sys.exit(main())
