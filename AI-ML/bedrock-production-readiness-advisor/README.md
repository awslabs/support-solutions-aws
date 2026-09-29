# Bedrock Production Readiness Advisor

Answers, in a consistent way, *"is this Amazon Bedrock deployment
operationally ready for production, and what should I fix first?"* — against a
live account, an architecture diagram, or both.

> **This is sample code, for non-production usage.** You should work with your
> security and legal teams to meet your organizational security, regulatory,
> and compliance requirements before deployment.

## Overview

Teams shipping to Amazon Bedrock in production repeatedly hit the same silent
failure modes at launch: quotas that throttle real traffic despite passing
load tests, missing invocation logs that make an incident un-triageable,
guardrails configured but not actually enforced on the intended agents,
cross-region fallback that was never exercised, and cost alarms that were
never wired. Every team relearns these lessons the hard way, usually two or
three days into a production incident.

This solution runs **59 checks across 8 pillars** — Observability, Architecture &
Resilience, Quota & Capacity, Cost Optimization, Model Fitness, Security,
Guardrails, and Data Governance — using **read-only** API calls only. Every
finding is scored on Impact × Likelihood and rolled up into a severity-ranked
Priority Actions list. Every recommendation is **plain-English guidance
paired with a link to the AWS documentation** that explains how to make the
change. The exit code (0 when score ≥ 70, else 1) is a drop-in CI/CD gate.

Five delivery channels — CLI, Python API, MCP server, AgentCore-hosted Strands
agent, and Agent Skills — all wrap the same assessment pipeline. The tool runs
entirely under your own AWS credentials, in your own account and region. All
processing and reporting stays within your environment.

### Use cases

- **Pre-launch readiness gate.** Block a launch in CI/CD when critical findings
  remain unaddressed.
- **Design review before infrastructure exists.** Feed an architecture diagram
  in — no account is scanned — and get advisory design findings.
- **Drift check on an existing deployment.** Combine a live account scan with
  the intended architecture diagram to surface the capabilities the design
  promises that the account does not actually have.
- **Conversational assessment.** Ask directly from Kiro, Claude Code, Cursor,
  Windsurf, or Q Developer via the MCP server.
- **Recurring platform review.** Schedule the CLI or an AgentCore-hosted agent
  for periodic assessment across accounts, with results written to an S3
  bucket you own.

## Architecture

![Bedrock Production Readiness Advisor — architecture](docs/architecture.png)

**How it works:**

1. **A user invokes the assessment** through one of five delivery channels —
   CLI, Python API, MCP client, an AgentCore-hosted Strands agent, or an Agent
   Skill — using their own AWS credentials.
2. **The Config Loader** reads the optional YAML + CLI overrides and
   auto-detects `mode` (pre-production vs production) and `workload_type` from
   the environment.
3. **The Account Scanner** makes ~45 read-only `Describe` / `List` / `Get`
   calls across Amazon Bedrock, Bedrock AgentCore, CloudWatch (metrics + logs),
   X-Ray, Service Quotas, Cost Explorer, IAM, CloudTrail, EC2 (VPC endpoints),
   and one targeted S3 read on the Bedrock invocation-logging bucket if
   configured. *(Optional)* The **Diagram Scanner** loads an architecture
   diagram from a local path or `s3://…` and calls Amazon Bedrock Converse
   *in the customer's own account and region*.
4. **The 8 pillars evaluate** the scan data across 59 checks — Observability,
   Architecture & Resilience, Quota & Capacity, Cost Optimization, Model
   Fitness, Security, Guardrails, Data Governance.
5. **The Scorer** computes per-finding risk (Impact × Likelihood), assigns
   severity bands, and ranks Priority Actions.
6. *(Only when both sources are present)* **The Reconciler** produces a
   design-vs-reality table across 14 readiness capabilities — highlighting
   capabilities the diagram promises that the account does not have.
7. **The Reporter** renders the output as HTML (default), Markdown, or JSON.
8. **The report is written locally, or to an S3 bucket you own.** Exit code
   `0` (score ≥ 70) or `1` becomes the CI/CD gate.

Everything above runs inside your own AWS account and machine — your data
stays where you already trust it. See [ARCHITECTURE.md](ARCHITECTURE.md) for
the full workflow, sequence diagrams, and scope-enforcement details.

## Scope

**Assesses 59 checks across 8 pillars, using read-only API calls only:**

| Pillar | Checks | What it answers |
|--------|:------:|-----------------|
| Observability | 13 (`OBS-*`) | Can you see what your workload is doing? |
| Architecture & Resilience | 10 (`ARCH-*`) | Does it survive a bad day? |
| Quota & Capacity | 11 (`QC-*`) | Will it throttle at launch? |
| Cost Optimization | 9 (`COST-*`) | Are you overpaying? |
| Model Fitness | 4 (`MF-*`) | Right model for each job? |
| Security | 4 (`SEC-*`) | Is access to Bedrock scoped the way it should be? |
| Guardrails | 4 (`GR-*`) | Is harmful or unwanted content actually being filtered? |
| Data Governance | 4 (`DG-*`) | Where does prompt/response data actually go? |

Run `bedrock-readiness checks` for the full list.

**Every recommendation is grounded in official AWS documentation.** Each
finding pairs a plain-English description of what to change with 1–2 links to
the AWS page that documents how — so you can act on it directly, and the
guidance stays current with AWS as documentation evolves. Security findings
describe what an IAM policy grants (for example, a wildcard on `bedrock:*`) in
prose, so you understand the risk without needing to interpret the underlying
document yourself. `tests/test_no_deployable_remediation.py` guards this
contract on every commit.

**Security-sensitive reads are precisely scoped.** IAM policy documents
(`iam:GetRolePolicy` / `GetPolicy` / `GetPolicyVersion`) are inspected only for
roles trusted by a Bedrock service principal — identified via the
`AssumeRolePolicyDocument` that `ListRoles` returns. S3 configuration
(`GetBucketEncryption` / `GetPublicAccessBlock`) is read only on the specific
bucket, if any, configured as the Bedrock invocation-logging destination.

**The architecture-diagram review covers fewer pillars than the account
scan** -- Observability, Architecture, Quota & Capacity, Cost Optimization,
and Model Fitness only. A vision model reading a picture is a materially less
verifiable source for security-adjacent claims than a live API read, so that
boundary was left in place deliberately rather than widened along with the
account scan. See "Architecture diagram review" below.

## Safe by design

Every AWS API call the solution makes is a read: `describe`, `list`, or `get`,
plus `bedrock:InvokeModel` (inference) and `s3:GetObject` for optional diagram
review. The complete list is declared alongside the code that makes each call,
so you can preview everything the assessment would do with:

```bash
bedrock-readiness assess --dry-run
```

before ever running it against your account.

### Verified in CI

Every safety property is a numbered claim in
[`SECURITY_SCOPE.md`](SECURITY_SCOPE.md) mapped to an automated test that runs
on every commit as a required CI stage:

| Test | Enforces |
|------|----------|
| `tests/test_readonly_surface.py` | No mutating verb appears in any scanner call site |
| `tests/test_no_deployable_remediation.py` | No recommendation is a directly applicable policy or template |
| `tests/test_references.py` | Documentation links stay within scope and on AWS-owned hosts |
| `tests/run_diagram_review.py` | The diagram path cannot widen its own scope |
| `tests/test_fail_open_guards.py` | A check blocked by permissions reports "unable to verify" rather than being dropped |
| `tests/test_check_catalog.py` | The published check catalog matches what the code actually emits |
| `tests/test_skill_integrity.py` | Skill frontmatter and the shared guardrails stay intact |

All seven run offline: no AWS credentials, no network. You can run them yourself
before trusting anything above — see [Testing without AWS](#testing-without-aws).

The fail-open guard is worth understanding. When a permission gap prevents a
check from running, the finding surfaces as an explicit "unable to verify"
result — so you can see exactly which checks were skipped and grant additional
permissions if needed.

## Documentation references

Every finding carries 1-2 links to the AWS page that documents the change --
42 URLs covering all 59 checks, in `core/references.py`:

```
- [FAIL] Cost Alarm -- No cost alarm, runaway spending won't be caught until the bill
  - Fix: Create a budget alarm or CloudWatch alarm on token consumption metrics
  - Docs: Managing your costs with AWS Budgets
  - Docs: Using Amazon CloudWatch alarms
```

Every finding is paired with the authoritative AWS documentation, so you're
always pointed at up-to-date official guidance for how to make the change.

**Links come from a curated, tested map.** Documentation URLs are defined in
`core/references.py` and matched to check IDs by the code itself — so the link
you see next to a Cost finding is authoritative AWS Cost guidance, and a
Security finding points to authoritative AWS Security guidance.
`references.validate()` and `tests/test_references.py` verify every link
points to the right topic for its check.

The link set follows two rules:

- **HTTPS URLs on AWS-owned domains only** — every recommendation resolves to
  official AWS documentation.
- **Topic-scoped by pillar** — Cost findings link to Cost documentation,
  Security findings link to Security documentation, and so on.

## Deployment guide

### Prerequisites

- Python 3.10 or later
- AWS credentials with read-only access to Bedrock, Bedrock AgentCore,
  CloudWatch, X-Ray, Service Quotas, Cost Explorer, IAM, CloudTrail, EC2,
  and S3 in the account you want to assess. See
  [Permissions (all read-only)](#permissions-all-read-only) for the exact
  operations. Most engineers already have this via a standard
  `ReadOnlyAccess` role.
- *(Optional)* An Amazon Bedrock multimodal model enabled in the account and
  region, if you plan to submit an architecture diagram for review.

### Install

```bash
pip install -e .                 # core: CLI + Python API
pip install -e '.[mcp]'          # + MCP server
pip install -e '.[agentcore]'    # + Strands/AgentCore agent
```

### Run your first assessment

```bash
bedrock-readiness assess --dry-run                            # show every call, execute none
bedrock-readiness assess --region us-east-1 --format html     # full assessment, HTML report
```

`--dry-run` is the fastest way to demonstrate the read-only claim to a security
reviewer — it renders every AWS operation the tool would call, without making
a single one.

## Delivery channels

All five wrap the same pipeline and expose the same 8 pillars (5 for the
diagram review -- see "Scope" above).

### 1. CLI

```bash
bedrock-readiness assess --region us-east-1
bedrock-readiness assess --config bedrock-readiness.yaml --format html
bedrock-readiness assess --dry-run                        # show calls, execute none
bedrock-readiness assess --diagram <your-diagram>.png       # scan + design comparison
bedrock-readiness review-diagram --diagram <your-diagram>.png   # diagram only
bedrock-readiness checks --pillar quota_capacity
```

Exits `0` when the score is >= 70, else `1` -- usable directly as a CI/CD gate.

### 2. Python API

```python
from bedrock_readiness import assess, review_diagram, assess_platform, is_production_ready

result = assess(region="us-east-1", workload_type="rag-pipeline")
print(f"{result.overall_score}/100, {len(result.priority_actions)} actions")

# Design source -- works before any infrastructure exists
design = review_diagram(diagram="<your-diagram>.png")

# Both, with design-vs-reality reconciliation
both = assess_platform(region="us-east-1", diagram="<your-diagram>.png")
for row in both.reconciliation:
    if row["verdict"] == "DESIGN_NOT_IMPLEMENTED":
        print(f"Diagram shows {row['label']}, account does not have it")

if not is_production_ready(region="us-east-1"):
    raise SystemExit(1)
```

### 3. MCP server

Use it conversationally from Kiro, Claude Code, Cursor, Windsurf, or Q
Developer.

```json
{
  "mcpServers": {
    "bedrock-readiness": {
      "command": "bedrock-readiness-mcp",
      "env": { "AWS_PROFILE": "your-profile", "AWS_REGION": "us-east-1" }
    }
  }
}
```

Config locations: Kiro `.kiro/settings/mcp.json`, Claude Code
`~/.claude/claude_desktop_config.json`, Cursor `.cursor/mcp.json`.

Then ask: *"Is my Bedrock deployment ready for production?"*, *"Check just the
quota headroom in us-west-2"*, *"Review this diagram for readiness gaps"*,
*"What does this tool actually check?"*

13 tools: one per pillar (`check_observability` ... `check_data_governance`,
8 in total), plus `assess_bedrock_readiness`, `review_architecture_diagram`,
`assess_bedrock_platform`, `list_readiness_checks`, and `describe_scope`.

Each pillar has its own dedicated MCP tool (`check_observability`,
`check_security`, etc.), so clients discover the available pillars directly
from the MCP tool list. `list_readiness_checks` reads the declarative catalog
in `bedrock_readiness/modules/`, so it always reflects the exact set of
checks the package implements.

A scan is reused for up to 5 minutes across tool calls in one session, so an
LLM invoking several pillar tools in a turn does not re-run ~20 API calls each
time. Reuse age is always reported; pass `force_refresh=true` to override.

### 4. AgentCore / Strands agent

```bash
aws cloudformation deploy --template-file deploy/template.yaml \
  --stack-name bedrock-readiness-only --capabilities CAPABILITY_NAMED_IAM
```

The MCP server does **not** need this stack -- it runs locally over stdio using
your own credentials.

### 5. Skill (Agent Skills open standard)

Two skill tracks under `skill/`, both following the Agent Skills open
standard (`SKILL.md` + YAML frontmatter). Compatible with any client that
reads that standard, including Kiro, Claude Code, Cursor, Codex, Gemini CLI,
Windsurf, and others -- exact registration steps vary per platform, see
`skill/README.md` for the platform-neutral setup.

- **`skill/mcp-backed/`** -- calls the MCP server. Use when an MCP server is
  already running (or will be) that other tools also connect to. Needs
  MCP tool-call capability in the client.
- **`skill/standalone-cli/`** -- shells out to `bedrock-readiness assess`
  directly in the agent's own bash environment. Use for a one-shot
  assessment with no server to manage. Needs the package installed and
  AWS credentials reachable from the agent's shell.

Both tracks share `skill/guardrails.md`, so the rules for how to present
results (no binary ready/not-ready verdict, no drafting a corrected policy
even if asked, relay `ERROR` findings honestly, discover current scope
live rather than repeating hardcoded pillar/check counts) stay identical
across both. Neither track scans or scores anything itself -- every fact
comes from an existing channel (MCP or CLI).

`tests/test_skill_integrity.py` provides Tier 1 structural drift-guards:
frontmatter validity, that every MCP tool name mentioned in the MCP-backed
`SKILL.md` exists in the actual MCP server, that every CLI subcommand
mentioned in the standalone-CLI `SKILL.md` exists in the actual argparse,
that referenced files exist, and that no hardcoded pillar or check counts
sneak into the skill files. Runtime guardrail-following behavior (does an
agent actually refuse when asked "just write me the fixed policy") is not
covered by tests -- see `ARCHITECTURE.md`'s "Boundaries" section for the
explicit exception.

## Architecture diagram review

Reviews the *intended* design, so a team can get readiness feedback before
infrastructure exists.

```bash
bedrock-readiness review-diagram --diagram <your-diagram>.png
bedrock-readiness review-diagram --diagram s3://<bucket>/<key>.png
```

Accepts PNG / JPEG / GIF / WEBP up to 4.5 MB, from a local path or
`s3://bucket/key`. Local files are read with `open()`; `s3://` URIs are read
with `s3:GetObject` on that specific key. The image is sent to Bedrock
Converse **in your own account and region**, and only the path string is
retained for report provenance.

**Do not submit diagrams containing regulated or customer-confidential data.**

### Which model it uses

**There is no hardcoded default model.** The model is chosen from what your
account actually has:

1. Filter `ListFoundationModels` to models whose `inputModalities` include
   `IMAGE` -- the API decides capability, not a built-in list.
2. Take the **cheapest** by a maintained preference order (Nova Lite → Nova Pro
   / Claude Haiku → Sonnet → Opus). An unrecognised model ranks last, so a newly
   released expensive model never wins by default.
3. Reach it through an **inference profile** when one covers it: `global.`
   first (higher throughput and cheaper per the CRIS docs), then
   `us./eu./apac.`, else invoke the model directly.

The report states which model was used and how it was reached, e.g. *analysed by
`global.amazon.nova-lite-v1:0` (global-cris), selected from the models available
to this account*.

Override with `--diagram-model-id` (or `diagram_model_id:` in the YAML), which is
used verbatim with no capability check.

The cost ranking is a static heuristic, not live pricing -- reading the Price
List API would need `pricing:GetProducts`, a permission this tool otherwise has
no use for.

### If no model is available

The review is **skipped with a clear reason** and the account scan still runs:

- No image-capable model granted → skipped before the diagram is even read.
- Any invoke error (access denied, wrong region, throttling, bad response) →
  skipped with a classified reason. `botocore` retries are disabled
  (`max_attempts=1`) so a single call is made per assessment.

`assess --diagram` still produces the **full readiness assessment** — all 59
checks run without needing a model — and records the diagram-review outcome
in the report. `review-diagram` prints the reason and exits `1` when it has
nothing else to produce.

**Design findings are advisory.** A picture is not evidence about a live
account, so the diagram review contributes advisory findings that highlight
what to verify. The readiness score itself comes from the runtime scan of your
account.

### Design vs. reality

With both a scan and a diagram, the report reconciles them per capability:

Rows are ordered most-actionable first:

| Verdict | Diagram | Account | Meaning |
|---------|:-------:|:-------:|---------|
| `DESIGN_NOT_IMPLEMENTED` | PRESENT | ABSENT | Diagram shows it, account does not have it -- **usually the most useful rows** |
| `MISSING_IN_BOTH` | ABSENT | ABSENT | Neither the design nor the account has it |
| `ABSENT_IN_ACCOUNT` | UNCLEAR | ABSENT | Account does not have it; the diagram was unclear either way |
| `UNDOCUMENTED_IN_DESIGN` | ABSENT/UNCLEAR | PRESENT | Account has it, diagram does not show it |
| `ALIGNED` | PRESENT | PRESENT | Both have it |
| `INCONCLUSIVE` | any | not assessed | The scan could not determine it, so no comparison is possible |

The model only reports PRESENT / ABSENT / UNCLEAR per capability. The account
column comes entirely from real check results, and the verdict is a lookup on
the pair -- the model does not decide verdicts.

`INCONCLUSIVE` means the *scan* could not determine the state, not that the
diagram was vague. A gap the scan proves is reported as `ABSENT_IN_ACCOUNT`
even when the diagram says nothing about it, so a real high-severity gap is
never hidden behind a label that reads as "nothing to see here".

### How scope is enforced

A model asked to review an architecture will volunteer security advice unless
stopped. Three independent layers prevent that, and layers 2 and 3 do not
depend on the model complying with layer 1:

1. **Prompt** -- scoped to the 5 readiness pillars, security commentary in
   findings forbidden.
2. **Structural** -- the model is given `out_of_scope_notes` as the only outlet
   for anything else, and that field is discarded. Only its count is reported.
3. **Deterministic post-filter** -- every finding must declare one of the 5
   pillar categories, and any finding whose text matches the excluded-topic
   list is dropped regardless of category. Fenced code blocks are stripped, so
   no IaC template can be emitted even if the model produces one.

Both filter counts appear in the report, so you can see when something was
removed rather than having it silently vanish.

## Permissions (all read-only)

The authoritative list lives in `core/scanner.py` as `READ_ONLY_OPERATIONS`,
`CONDITIONAL_OPERATIONS`, and `NEVER_CALLED_OPERATIONS` -- declared beside the
code that performs the calls. To see it for your configuration, without making
any call:

```bash
bedrock-readiness assess --dry-run
bedrock-readiness assess --dry-run --diagram s3://your-bucket/your-diagram.png
```

That output is rendered from those declarations, so it cannot drift from what
the tool does, and `tests/test_readonly_surface.py` checks the declarations
against the real call sites in source.

In summary: `describe` / `list` / `get` across Bedrock, Bedrock Agent, AgentCore
Control, CloudWatch, Logs, EC2, Service Quotas, Cost Explorer and X-Ray.

For the Security pillar, `iam:GetRolePolicy` / `GetPolicy` / `GetPolicyVersion`
inspect the policies of roles trusted by a Bedrock service principal —
identified via the trust policy that `iam:ListRoles` returns. For Guardrails
and Data Governance, `bedrock:GetGuardrail`,
`ListEnforcedGuardrailsConfiguration`, and `GetAccountDataRetention` read
guardrail configuration and account-wide data-retention posture.
`cloudtrail:GetTrailStatus` / `GetEventSelectors` are called per trail found by
`describe_trails` to check whether it is logging and whether it covers Bedrock
data events. `s3:GetBucketEncryption` / `GetPublicAccessBlock` read the
configuration of the one bucket, if any, configured as the Bedrock
invocation-logging destination.

Diagram review adds `bedrock-runtime:Converse` (`bedrock:InvokeModel`) and, for
`s3://` diagrams, `s3:GetObject` on that key. `deploy/template.yaml` grants the
S3 permission only when `DiagramS3BucketName` is set, scoped precisely to the
bucket and prefix you name.

## Testing without AWS

```bash
python tests/run_fixtures.py --html            # 3 account fixtures, mock scanner
python tests/run_diagram_review.py             # diagram pipeline + scope filter
python tests/test_check_catalog.py             # catalog matches emitted check IDs
python tests/test_references.py                # doc-link scope rules
python tests/test_readonly_surface.py          # read-only guarantee, derived from source
python tests/test_no_deployable_remediation.py # no Finding ever carries a deployable artifact
python tests/test_fail_open_guards.py          # a partial-failure read never reports a clean PASS
python tests/test_skill_integrity.py           # skill files' tool/subcommand references match the code
```

No credentials and no network required. The diagram tests use a stubbed model
response via the `invoke_fn` seam, so they exercise parsing, the scope filter,
and reconciliation without calling Bedrock.

Reports are always produced from your own account, so the first real output is
`bedrock-readiness assess --dry-run` followed by
`bedrock-readiness assess --region <your-region>`. This ensures the report
reflects your actual environment.

`test_readonly_surface.py` is worth knowing about: it parses every module,
derives the actual set of AWS operations the solution makes, and verifies
that they match the declarations in `core/scanner.py`. The read-only property
is derived from the code on every test run — a change that touches the API
surface is caught by CI before it ships.

See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams and the full workflow.

## Cost

The solution itself is free and open source. Running it against your AWS
account incurs a few small charges — typically well under **$1/month** for
regular use. The main cost drivers are:

- **Baseline assessment** — a scan makes 1–3 [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)
  API calls ($0.01 per request). All other read APIs are free. **~$0.05 per scan.**
- **Diagram review (optional)** — one [Amazon Bedrock](https://aws.amazon.com/bedrock/pricing/)
  Converse call per diagram. The tool defaults to the cheapest image-capable
  model in your account (typically Nova Lite ≈ $0.0005; premium models like
  Claude Sonnet ≈ $0.025).
- **Reports in S3 (optional)** — [Amazon S3](https://aws.amazon.com/s3/pricing/)
  storage at $0.023/GB-month. A year of weekly reports is under $0.05/month.
- **Scheduled assessments (optional)** — [Amazon Bedrock AgentCore Runtime](https://aws.amazon.com/bedrock/agentcore/pricing/)
  ($0.0895/vCPU-hour) plus [Amazon EventBridge Scheduler](https://aws.amazon.com/eventbridge/pricing/).
  Daily schedule: under $0.20/month.

Running the CLI locally without S3 output or diagram reviews is essentially
free. For account-specific estimates, use the
[AWS Pricing Calculator](https://calculator.aws/).
