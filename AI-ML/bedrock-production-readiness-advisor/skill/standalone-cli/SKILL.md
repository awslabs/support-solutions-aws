---
name: bedrock-readiness-cli
description: >
  Assess whether an AWS account's Amazon Bedrock deployment is
  operationally ready for production, by running the bedrock-readiness CLI
  directly. Use when a builder asks about Bedrock/AgentCore production
  readiness, quota headroom, guardrails, security posture, data
  governance, or wants an architecture diagram reviewed before
  infrastructure exists. No MCP server required; runs in the agent's own
  shell environment.
---

# Bedrock Readiness -- standalone CLI skill

This skill runs the `bedrock-readiness` CLI directly in the agent's shell
environment. It does not need an MCP server, but it does need the package
installed (`pip install bedrock-readiness` or `pip install -e .`) and valid
AWS credentials reachable from the shell. Every fact in a response must come
from the CLI's output; this file only tells you which commands to run and
how to talk about the result.

> **Cross-platform.** This skill follows the Agent Skills open standard
> (SKILL.md + YAML frontmatter). It works with any compatible client that
> has shell/bash execution: Kiro, Claude Code, Cursor, Codex, Gemini CLI,
> Windsurf, and others. Exact registration steps vary per platform; see
> `skill/README.md`.

## Prerequisite

The `bedrock-readiness` package must be installed and the `bedrock-readiness`
command must be on PATH. AWS credentials must be configured (via
`AWS_PROFILE`, environment variables, or the default credential chain). If
the command is not found or credentials are missing, say so plainly and point
at the package's install instructions. Do not attempt to answer a readiness
question from general knowledge instead.

Verify availability:

```bash
bedrock-readiness checks
```

If that prints the check catalog, the package is installed and working.

## Which command to run

- A full assessment:
  ```bash
  bedrock-readiness assess --region us-east-1 --format json
  ```
  Use `--format json` for structured output you can parse, or `--format html`
  / `--format markdown` for human-readable reports.

- A single pillar's checks (to understand what is covered):
  ```bash
  bedrock-readiness checks --pillar quota_capacity
  ```
  Note: the CLI does not have a per-pillar `assess` subcommand. To assess a
  single pillar, run the full assessment and filter the JSON output to the
  pillar you need.

- A proposed design, before any infrastructure exists:
  ```bash
  bedrock-readiness review-diagram --diagram /path/to/diagram.png --region us-east-1
  ```
  The diagram review covers a narrower set of pillars than a full account
  assessment. Run `bedrock-readiness checks` to see all pillars, and note
  that the diagram review only covers a subset. If asked whether the diagram
  review also covers security, guardrails, or data governance, say plainly
  that it does not.

- Both a live account and a diagram:
  ```bash
  bedrock-readiness assess --region us-east-1 --diagram /path/to/diagram.png --format json
  ```
  This produces the scored report plus a design-vs-reality reconciliation.

- Dry run (show what API calls would be made, execute none):
  ```bash
  bedrock-readiness assess --dry-run
  ```

- What does this tool check:
  ```bash
  bedrock-readiness checks
  ```

## Interpreting the exit code

`bedrock-readiness assess` exits `0` when the overall score is >= 70, and
`1` otherwise. Do not treat exit code `1` as a crash; it means the score
was below the readiness threshold. Parse the output (especially with
`--format json`) for the actual score and findings.

## Guardrails

Read and follow every rule in `skill/guardrails.md`. That file is the single
source for how to talk about results, what never to generate, and when this
skill does not apply.
