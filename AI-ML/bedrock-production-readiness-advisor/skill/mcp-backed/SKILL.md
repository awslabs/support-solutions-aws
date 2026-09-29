---
name: bedrock-readiness-mcp
description: >
  Assess whether an AWS account's Amazon Bedrock deployment is
  operationally ready for production, by calling the bedrock-readiness MCP
  server. Use when a builder asks about Bedrock/AgentCore production
  readiness, quota headroom, guardrails, security posture, data
  governance, or wants an architecture diagram reviewed before
  infrastructure exists. Requires the bedrock-readiness MCP server to be
  configured and reachable.
---

# Bedrock Readiness -- MCP-backed skill

This skill is a thin instruction layer over the `bedrock-readiness` MCP
server. It does not scan anything itself, has no AWS access of its own, and
does not duplicate any check logic. Every fact in a response must come from
calling one of the MCP server's tools; this file only tells you when to call
which tool and how to talk about the result.

> **Cross-platform.** This skill follows the Agent Skills open standard
> (SKILL.md + YAML frontmatter). It works with any compatible client:
> Kiro, Claude Code, Cursor, Codex, Gemini CLI, Windsurf, and others.
> Exact registration steps vary per platform; see `skill/README.md`.

## Prerequisite

This skill needs the `bedrock-readiness` MCP server already configured and
reachable (see the package README, "MCP server" section, for the config
snippet). If it is not configured, or a tool call fails outright, say so
plainly and point at that setup step. Do not attempt to answer a readiness
question from general knowledge instead.

## Which tool to call

- A full assessment ("is my Bedrock deployment ready", "how ready is this
  account"): call `assess_bedrock_readiness`.
- A single pillar ("check just the quota headroom", "how's my security
  posture"): call the matching `check_<pillar>` tool. Do not call
  `assess_bedrock_readiness` and then discard everything except the pillar
  the user asked about; call the specific tool.
- A proposed design, before any infrastructure exists ("review this
  architecture diagram"): call `review_architecture_diagram`. The diagram
  review covers a narrower set of pillars than a full account assessment;
  confirm the current set with `describe_scope` rather than repeating a
  hard-coded list. If asked whether the diagram review also covers security,
  guardrails, or data governance, say plainly that it does not, and why: a
  vision model reading a picture is a materially less verifiable source for
  security-adjacent claims than a live account read.
- Both a live account and a diagram, for design-vs-reality comparison: call
  `assess_bedrock_platform`.
- "What does this tool actually check?": call `list_readiness_checks`.
- "What is the scope of this tool?": call `describe_scope`.

A scan is reused for a few minutes across tool calls in one session (check
the reported reuse age; it is always returned). Do not re-run a full
assessment for every follow-up question in the same conversation if a recent
scan already covers it. If the account may have changed, or the user asks
for a fresh read, pass `force_refresh=true` rather than silently reusing a
stale scan without saying so.

## Guardrails

Read and follow every rule in `skill/guardrails.md`. That file is the single
source for how to talk about results, what never to generate, and when this
skill does not apply.
