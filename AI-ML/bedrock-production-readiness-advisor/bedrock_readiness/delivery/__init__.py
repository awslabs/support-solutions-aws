# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Delivery channels for the readiness-only solution.

  ../cli.py             -- CLI (`bedrock-readiness assess` / `review-diagram`)
  ../api.py             -- Python API (assess / review_diagram / assess_platform)
  agentcore_agent.py    -- Strands Agent on AgentCore Runtime
  mcp_server.py         -- MCP server for Kiro / Claude Code / Cursor / Q

Every channel wraps the same read-only pipeline and exposes the same five
readiness pillars. None of them can reach a Security, Guardrails, or Data
Governance assessment, because those modules are not in this package.

The submodules are intentionally NOT imported here: each has its own optional
dependency (`strands-agents`, `mcp`), so importing this package must not
require either to be installed.
"""
