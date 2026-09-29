# Skill -- Bedrock Readiness

A packaged instruction file (`SKILL.md`) that teaches any Agent Skills-compatible
coding agent how to run a Bedrock readiness assessment. Two variants are
provided; pick the one that matches your environment.

## Which track to use

| | MCP-backed | Standalone CLI |
|---|---|---|
| **Directory** | `skill/mcp-backed/` | `skill/standalone-cli/` |
| **What it calls** | The `bedrock-readiness` MCP server's 13 tools | The `bedrock-readiness` CLI directly via the agent's shell |
| **Prerequisite** | MCP server installed, configured, and reachable | Package installed (`pip install bedrock-readiness`), AWS credentials on PATH |
| **Best when** | You already run an MCP server that other tools also connect to, or your platform has strong MCP support | You want a one-shot assessment with no server to manage, or your platform's MCP support is limited |
| **Agent needs** | MCP tool-call capability | Shell/bash execution capability |

Both tracks produce identical findings from the same underlying engine. They
share the same guardrails (`skill/guardrails.md`), so the rules for how to
present results, what never to generate, and when to say "out of scope" are
the same regardless of which track you use.

## Cross-platform compatibility

Both skill tracks follow the Agent Skills open standard (a directory with a
`SKILL.md` file containing YAML frontmatter). This format is recognized by a
growing set of coding agents and AI tools, including but not limited to Kiro,
Claude Code, Cursor, Codex, Gemini CLI, and Windsurf.

Exact registration and discovery steps vary per platform. Some platforms read
`SKILL.md` from a specific directory (e.g., `.kiro/skills/`), others scan the
project tree, and others require explicit configuration. Check your platform's
documentation for the expected file layout and any additional frontmatter
fields beyond `name` and `description`.

## What this is, and is not

This is **not** a new execution engine. Neither track has AWS access, scanning
logic, or report generation of its own. The MCP-backed track calls the MCP
server's tools. The standalone CLI track shells out to `bedrock-readiness
assess`. Both ride existing delivery channels (MCP and CLI respectively), not
a new one.

The read-only guarantee and the no-deployable-remediation guarantee both live
in the core package's code and are enforced by its test suite
(`tests/test_readonly_surface.py`, `tests/test_no_deployable_remediation.py`).
Neither skill track can weaken those guarantees, because neither has a code path
that bypasses them. What the skill *can* fail to enforce is the conversational
extension of those guarantees (for example, an agent drafting a policy on its
own initiative after seeing a finding). That is why `guardrails.md` spells that
out explicitly, and why `tests/test_skill_integrity.py` checks for structural
drift between the skill files and the actual codebase.

## Shared guardrails

`skill/guardrails.md` contains the rules both tracks must follow:

- Discover scope live (never hardcode pillar/check counts).
- Never state a binary "ready" or "not ready" verdict.
- Never draft a deployable artifact (IAM policy, guardrail config, template).
- Relay `ERROR` findings honestly.
- Use only the documentation links the tool returns.
- Do not claim compliance with any regulation or standard.

Each track's `SKILL.md` references this file rather than restating its content,
so guardrail updates propagate to both tracks from a single edit.

## Automated test coverage

`tests/test_skill_integrity.py` provides Tier 1 structural drift-guards:

1. Every `SKILL.md` has valid YAML frontmatter with `name` and `description`.
2. Every MCP tool name mentioned in the MCP-backed track exists in the actual
   MCP server's registered tool set.
3. Every CLI subcommand mentioned in the standalone CLI track exists in the
   actual CLI's argument parser.
4. Every companion file referenced from a `SKILL.md` exists on disk.
5. No hardcoded pillar or check counts appear in any skill file.

This does not test whether a model follows the guardrails at runtime (that
would require an eval harness with real model invocations). See
`ARCHITECTURE.md` for the explicit exception noted for the skill channel.
