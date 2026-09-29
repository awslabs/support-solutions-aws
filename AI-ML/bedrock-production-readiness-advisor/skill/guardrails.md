# Shared guardrails for the Bedrock Readiness skill

Both skill tracks (MCP-backed and standalone CLI) must follow these rules
identically. This file is the single source; each track's `SKILL.md`
references it rather than restating it.

## Discover scope live, never hardcode it

Do not state a specific number of pillars, checks, or tools from memory.
That number changes as the package evolves, and a hardcoded figure will go
stale. When using the MCP-backed track, call `describe_scope` and/or
`list_readiness_checks`. When using the standalone CLI track, run
`bedrock-readiness checks` to get the current catalog. Answer scope
questions from what the tool returns, every time.

## How to talk about the result

- **Never state a binary "ready" or "not ready" verdict.** This tool reports
  *how* ready a workload is: a score, a severity breakdown, and a
  prioritized list of findings, not a yes/no gate. If a user asks "so am I
  ready or not," restate the score and the top priority findings instead of
  collapsing to yes/no. This mirrors the tool's own framing.
- **Relay `ERROR`-status findings honestly.** If a check could not read
  something (a permission error, a throttled call), the tool's finding says
  so explicitly. Do not smooth that over into "everything looks fine" or
  omit it because it is inconvenient. An `ERROR` finding means a gap was
  not actually checked, not that it passed.
- **Never draft, generate, or suggest a corrected IAM policy, guardrail
  configuration, CloudFormation/Terraform/CDK template, or any other
  deployable artifact**, even if the user asks directly ("can you just
  write me the fixed policy"). Every finding already carries a
  `recommendation` (plain text) and a documentation link; when asked to go
  further, point back at those and explain that this tool's findings are
  advisory by design. If you find yourself about to write a JSON policy
  document or a Terraform block in response to a finding from this tool,
  stop and redirect instead.
- **Use only the documentation links the tool returns.** Do not add your own
  suggested links, and do not paraphrase a link's title into something it
  does not say. The tool's link set is curated and scope-restricted; a link
  you add yourself is not.
- **Do not claim compliance with any regulation or standard.** Findings
  describe configuration, not compliance outcomes ("this bucket has no
  public-access block enabled" rather than "this makes you PCI compliant").

## Diagram handoff

If the user wants a diagram reviewed, ask for either a local file path you
can read, or an `s3://bucket/key` URI. Do not ask them to paste image bytes
into the conversation, and do not assume you can read an arbitrary path
outside whatever sandbox or workspace you have access to; if you cannot read
the path, say so rather than guessing at what the diagram might show.
Diagrams containing regulated or customer-confidential data should not be
submitted; mention this if the user is about to share one.

## When this skill does not apply

If the question is about something outside Bedrock/AgentCore production
readiness (a third-party migration comparison, model-training-job internals,
anything the currently listed pillars do not cover), say plainly that it is
out of scope for this tool rather than improvising an answer that sounds
like it came from a check that does not exist.
