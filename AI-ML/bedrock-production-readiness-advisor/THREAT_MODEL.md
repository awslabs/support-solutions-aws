# Bedrock Production Readiness Advisor - Threat Model

> **Sample content -- not for production use without additional security
> testing.** Provided as is, with no warranty of workmanship or fitness for
> purpose, and no claim of compliance with any regulation or standard.

Derived from the Simplified AWS SA Threat Model Template.

---

## Introduction

### Purpose

Customers routinely complete a successful Amazon Bedrock proof of concept and
then hit production failures the PoC never exposed: throttling at scale, token
spend with no attribution, no invocation logging or alarms when something
breaks, guardrails added late on customer-facing applications, and unclear
data-retention posture for regulated workloads. Each is discoverable from
account configuration and metrics before it becomes an incident.

This tool performs a read-only assessment of an AWS account's Amazon Bedrock
and Bedrock AgentCore configuration and reports prioritized findings across
eight pillars, so those gaps are found deliberately rather than during an
outage.

This threat model covers the tool itself as published open source software. It
does not model the customer workloads the tool assesses.

### Project/Asset Overview

A Python package published as open source to the `awslabs` GitHub organization
under MIT-0. Zero infrastructure: there is no hosted service, no deployed
endpoint, and no data store. The operator runs it on demand in their own
environment using their own AWS credentials.

Major components:

| Component | Path | Role |
|---|---|---|
| Account scanner | `core/scanner.py` | Read-only AWS API calls; collects configuration and metrics |
| Pillar modules | `modules/*.py` | Eight independent assessors that emit `Finding` objects |
| Scorer | `core/scorer.py` | Risk-scores findings from impact x likelihood |
| Reporter | `core/reporter.py` | Renders HTML / JSON / Markdown output |
| Diagram review | `core/diagram_review.py` | Optional; analyses an architecture-diagram image via Bedrock Converse |
| Delivery paths | `cli.py`, `api.py`, `delivery/mcp_server.py`, `delivery/agentcore_agent.py`, `skill/` | Five interfaces over one engine |

Third-party dependencies: `boto3>=1.40.0` and `pyyaml==6.0.2` required;
`mcp>=1.2.0` and `strands-agents` optional per delivery path. All resolved
from PyPI via `requirements.txt`; none are vendored into the repository.

Build and distribution: standard Python packaging (`setup.py`). No container
image, no binary artifacts. CI runs on GitLab shared runners using
`public.ecr.aws/docker/library/python:3.12-slim`.

### Assumptions

| ID | Assumption | Comments |
|---|---|---|
| A-01 | The tool is used for assessment and education, not as a production control or compliance gate. | Stated in the disclaimer at the top of README.md, ARCHITECTURE.md, SECURITY_SCOPE.md. The tool never emits a binary ready/not-ready verdict. |
| A-02 | The operator supplies their own AWS credentials and is authorized to read the account being assessed. | The tool implements no authentication or authorization of its own. |
| A-03 | The operator is responsible for scoping the IAM identity used, and for where generated reports are stored and who can read them. | The tool writes reports to a path the operator chooses. |
| A-04 | TLS 1.2 or above protects all AWS API calls. | Provided by boto3 defaults; not configurable by this tool. |
| A-05 | Architecture diagrams submitted to the diagram-review path are supplied by the operator and are not attacker-controlled in the normal case. | T-004 covers the case where this does not hold. |
| A-06 | The operator's Bedrock model access and data-retention settings govern the diagram-review inference call. | The tool does not alter model configuration. |

### References

- **Code Repo:** https://github.com/awslabs/bedrock-production-readiness-advisor
- **Project Team:** Sruthi Vedula (sruved), Vipul Gargav (vggargav), Ankur Aggarwal (aaggwal) - AWS Enterprise Support
- **CSR/PCSR Link:** V2353850635
- **SFDC Opportunity Link:** N/A (1:M public asset)
- **Scope contract:** `SECURITY_SCOPE.md` in repository root
- **Code scan:** Holmes, 2026-09-04 - 0 critical, 0 high, 0 medium, 3 low, 0 malware. All three lows adjudicated as false positives during the security review.
- **Predecessor asset:** github.com/aws-samples/sample-bedrock-readiness-agent

---

## Solution Architecture

### Architecture Diagram

```
                    OPERATOR'S ENVIRONMENT (trust boundary)
  ┌───────────────────────────────────────────────────────────────┐
  │                                                               │
  │  Operator ──(1)──> CLI / Python API / MCP server / AgentCore  │
  │                     agent / Agent Skills skill                │
  │                              │                                │
  │                              ├──(2)──> AccountScanner         │
  │                              │            │                   │
  │                              │         (optional)             │
  │                              │         sts:AssumeRole ──(3)   │
  │                              │                                │
  │                              ├──(6)──> Scorer ──> Reporter    │
  │                              │                       │        │
  │                              │                      (7)       │
  │                              │                       v        │
  │                              │            HTML / JSON / MD    │
  │                              │            report on local FS  │
  │                              │                                │
  │                     (optional diagram path)                   │
  │                              └──(4)──> diagram_review         │
  │                                           │                   │
  └───────────────────────────────────────────┼───────────────────┘
                                              │
        ═══════════ trust boundary ═══════════ │ ═══════════
                                              │
   AWS APIs (operator's own account, operator's own credentials)
   (3) sts        (5) bedrock-runtime:Converse  <──(4) diagram image
   (2) bedrock, cloudwatch, iam, ce, s3, logs, servicequotas, sts
```

Numbered interactions:

1. Operator invokes the tool through one of five interfaces. No network
   listener is opened in any of them; the MCP server communicates over stdio
   with a local client.
2. `AccountScanner` makes read-only AWS API calls using the operator's
   credential chain.
3. Optional: if `role_arn` is configured, `sts:AssumeRole` is called and the
   returned short-lived credentials are held in memory for the session.
4. Optional: the operator supplies an architecture-diagram image, from a local
   path or `s3://` URI.
5. The diagram is sent to a Bedrock multimodal model via the Converse API in
   the operator's own account.
6. Findings are risk-scored.
7. A report is rendered to a local path the operator chooses.

**There is no AWS-operated component in this diagram.** Everything executes in
the operator's environment against the operator's account.

### Main Functionality/Use Cases

- **UC-1 Account assessment.** Read-only scan of a live account; 59 checks
  across eight pillars; severity-ranked findings plus a readiness score.
- **UC-2 Design review.** Assessment of an architecture diagram before any
  infrastructure exists.
- **UC-3 Combined.** Both, to surface drift between what was designed and what
  was deployed.
- **UC-4 Agent-driven assessment.** The same engine invoked by a coding agent
  through the MCP server, the AgentCore agent, or an Agent Skills skill.

### APIs

The tool exposes no public or private network API. It is a library and CLI. The
MCP server speaks the Model Context Protocol over stdio to a local client and
does not listen on a network socket. Appendix A is therefore not applicable.

**AWS APIs consumed** (all read-only except the two noted):

| Service | Actions |
|---|---|
| bedrock | `ListFoundationModels`, `GetFoundationModel`, `ListGuardrails`, `GetGuardrail`, `GetModelInvocationLoggingConfiguration`, `GetAccountDataRetention`, `ListEnforcedGuardrailsConfiguration`, `ListInferenceProfiles`, `ListModelInvocationJobs` |
| bedrock-runtime | `InvokeModel` via `Converse` - diagram path only; inference, creates nothing |
| cloudwatch | `ListMetrics`, `GetMetricData` |
| iam | `ListRoles`, `ListAttachedRolePolicies`, `ListRolePolicies`, `GetRolePolicy`, `GetPolicy`, `GetPolicyVersion` |
| ce | `GetCostAndUsage` |
| s3 | `GetObject` (diagram from `s3://` only), `GetBucketEncryption`, `GetPublicAccessBlock` |
| logs | `DescribeLogGroups` |
| servicequotas | `ListServiceQuotas` |
| sts | `GetCallerIdentity`, `AssumeRole` (only when `role_arn` is configured) |

### Assets/Dependency

| Asset Name | Asset Usage | Data Type | Comments |
|---|---|---|---|
| Customer AWS configuration metadata | Read to evaluate checks: Bedrock settings, guardrails, IAM roles and policies, log groups, quotas | Confidential | Read only. Includes account ID, ARNs, resource names. |
| CloudWatch metrics | Read for throttling, latency, token-usage, and capacity analysis | Confidential | Aggregate metric data. No prompt or completion content. |
| Cost Explorer cost and usage data | Read for cost-optimization checks | Confidential | Aggregate spend. |
| Short-lived STS credentials | Held in memory for the boto3 session when `role_arn` is set | Critical | `scanner.py:196-206`. Never written to disk, never logged, never in any report. |
| Operator-supplied architecture diagram | Sent to a Bedrock multimodal model for the design review | Confidential | Operator's own account and model access. Tool persists nothing. |
| Model inference output | Text returned by Converse, becomes design findings | Confidential | Sanitized at source before use. See M-004. |
| Generated report (HTML / JSON / Markdown) | The deliverable; contains findings and affected resource identifiers | Confidential | **Contains real account IDs and ARNs.** Written to an operator-chosen path. See T-002. |
| Third-party dependencies | `boto3`, `pyyaml`; optional `mcp`, `strands-agents` | N/A | Resolved from PyPI. Not vendored. See T-007. |
| The published source code | The asset itself | Public | MIT-0 in `awslabs`. |

---

## Threats & Mitigations

### Threat Actors

| Threat Actor # | Threat Actor Description |
|---|---|
| TA1 | An unauthenticated actor from the internet |
| TA2 | An actor who can influence content the tool ingests (a crafted architecture diagram, or resource names in a scanned account) |
| TA3 | An actor who obtains a generated report without authorization |
| TA4 | The operator themselves, acting in error rather than maliciously (misconfiguration, over-broad IAM) |
| TA5 | An actor who can publish to a package index the tool depends on (supply chain) |
| TA6 | An actor who forks or tampers with the published repository and redistributes it |
| TA7 | An actor with permissions in the AWS account being assessed |

Note that TA1 has a very small surface: the tool opens no listener and exposes
no endpoint, so there is no path from the internet to a running instance.

### Threat & Mitigation Detail

| Threat # | Priority | Threat | STRIDE | Affected Assets | Mitigations | Decision | Status/Notes |
|---|---|---|---|---|---|---|---|
| T-001 | High | An operator (TA4) grants the tool a broader IAM identity than it needs, for example an administrative role, increasing the blast radius if that identity is later misused. | Elevation of Privilege | Customer AWS configuration, STS credentials | M-001, M-002 | Mitigate | Exact least-privilege action list documented. Tool cannot enforce the caller's IAM. |
| T-002 | High | An actor (TA3) obtains a generated report and learns account IDs, ARNs, IAM role names, and specific security gaps, providing a target map for a follow-on attack. | Information Disclosure | Generated report | M-003 | Mitigate | Residual risk accepted: report handling is the operator's responsibility (A-03). The report exists to be read by a human. |
| T-003 | Medium | An actor (TA2) supplies a crafted architecture diagram containing text designed to override the model's instructions, causing the design review to emit attacker-chosen content into the report. (OWASP LLM01) | Tampering | Model inference output, Generated report | M-004, M-005, M-006 | Mitigate | Impact bounded: output is escaped, fenced content stripped, length clamped, and all design findings are forced to WARN. |
| T-004 | High | Model-generated text reaches the HTML report unescaped, so a crafted diagram yields script execution when the operator opens the report in a browser. (OWASP LLM02, stored XSS) | Tampering / Elevation of Privilege | Generated report | M-004 | Mitigate | **Closed.** `_sanitize()` escapes `<` and `>` on all four model-derived fields before they become a `Finding`. `reporter.py` never calls `html.escape`, so this escape-at-source is load-bearing - see M-004 note. |
| T-005 | High | A check that cannot complete because of a permission error is silently dropped, so the report reads clean for a control that was never actually inspected, giving the customer false assurance. | Repudiation | Generated report | M-007 | Mitigate | **Closed.** Blocked checks report "unable to verify". Enforced by `tests/test_fail_open_guards.py` in the blocking CI stage. |
| T-006 | Medium | Exception detail from a failed AWS call is written into a finding, leaking ARNs, resource names, or internal error text into a report that may be shared more widely than the account. | Information Disclosure | Generated report | M-008 | Mitigate | **Closed.** All eight scanner sites use `type(e).__name__` only, never the exception message. |
| T-007 | Medium | An actor (TA5) publishes a malicious version of a dependency, or a typosquatted package name, and it is installed into the operator's environment. | Tampering | Operator environment, Customer AWS configuration | M-009, M-010 | Mitigate | Small surface: two required dependencies, both first-party AWS or widely used. `pyyaml` pinned; `boto3` floored with documented rationale. |
| T-008 | Medium | An actor (TA6) forks the repository, adds mutating AWS calls, and redistributes it as the original, so users believe they are running a read-only tool. | Tampering / Spoofing | Customer AWS configuration | M-011, M-012 | Mitigate | Cannot be prevented for a fork. Mitigated for the canonical repo by a blocking CI guarantee and a published scope contract users can verify. |
| T-009 | Medium | A future contributor adds a mutating API call, or a recommendation containing a directly applicable IAM policy or IaC template, silently breaking the read-only and no-deployable-remediation guarantees. | Tampering | Customer AWS configuration, Generated report | M-011, M-013 | Mitigate | **Closed by CI.** `test_readonly_surface.py` and `test_no_deployable_remediation.py` are blocking; `allow_failure` is not set. |
| T-010 | Low | An operator-supplied `s3://` diagram URI is used to read an object the operator did not intend, or a diagram is read from a bucket outside the assessed account. | Information Disclosure | Operator-supplied diagram | M-014 | Mitigate | Bounded by the caller's own `s3:GetObject` permissions. The tool adds no privilege. |
| T-011 | Low | An actor (TA7) with account permissions names a resource so that the name is malicious or misleading when rendered in the report. | Tampering | Generated report | M-004, M-017 | Mitigate | The runtime scan path does **not** pass resource identifiers through `_sanitize()`. What makes this safe today is that AWS naming rules for the resource types read here do not permit angle brackets, and no tag-read path exists (`ListTagsFor*` appears nowhere in the package). That is a property of AWS, not of this code, so the first check that reads a free-text field would reopen T-004 silently. The one attribute-context interpolation (`fix_type`) is a validated enum and is closed by construction. |
| T-012 | Low | The diagram image is sent to a Bedrock model, and the operator has not considered where that inference occurs or whether invocation logging retains the image. | Information Disclosure | Operator-supplied diagram | M-015 | Mitigate | Inference runs in the operator's own account under their own model access and data-retention settings. Documented so the operator can make an informed choice. |
| T-013 | Low | An actor (TA1) attempts to reach a running instance of the tool over the network. | Spoofing / Information Disclosure | All | M-016 | Avoid | No listener, no endpoint, no hosted service. MCP transport is stdio to a local client. There is no reachable surface. |

---

## APPENDIX A - APIs

Not applicable. The tool exposes no public or private network API. See the
APIs section above for the AWS APIs it consumes.

---

## APPENDIX B - Mitigations

| Mitigation Number | Mitigation Description | Threats Mitigating | Status | Comments |
|---|---|---|---|---|
| M-001 | The exact least-privilege IAM action list is documented in the README, the MCP server module docstring, and the CLI help, so the operator can scope a dedicated read-only identity rather than reusing an administrative one. | T-001 | Complete | See the AWS APIs table above. |
| M-002 | The tool requests no privilege of its own and never elevates. When `role_arn` is unset it uses the caller's existing credential chain. | T-001 | Complete | |
| M-003 | The non-production disclaimer and the scope contract state that the report contains account-identifying detail, so the operator can classify and handle it accordingly. | T-002 | Complete | Residual risk owned by the operator per A-03. |
| M-004 | `_sanitize()` (`core/diagram_review.py:700-712`) escapes `<` and `>` and strips code fences, applied to all four model-derived fields (`title`, `observation`, `recommendation`, `workload_summary`) before they become a `Finding`. **Note for reviewers:** `core/reporter.py` builds HTML by f-string interpolation and never calls `html.escape`. Escaping is done at the source instead, so `_sanitize()` is the single control closing T-004. Any future code path that puts untrusted text into a `Finding` without passing through it would reopen this threat. | T-003, T-004, T-011 | Complete | Recommend a follow-up: add defence-in-depth escaping in `reporter.py` so the guarantee does not rest on one function. |
| M-005 | Model output length is clamped, bounding the volume of attacker-influenced text that can reach a report. The default is `MAX_TEXT_CHARS = 600` (`diagram_review.py:76`), applied to `observation` and `recommendation`; `title` is clamped to 120 and `workload_summary` is called with an explicit `limit=800`, so 800 is the real upper bound on any single field. | T-003 | Complete | The differing per-field limits are intentional but were previously misstated here as a single value. |
| M-006 | All design-review findings are forced to `WARN` status and can never be a hard `FAIL`, because a diagram cannot prove a capability is absent from a real deployment. Model influence therefore cannot manufacture a critical finding. | T-003 | Complete | `diagram_review.py`, status set unconditionally. |
| M-007 | A check blocked by a permission error emits an explicit "unable to verify" result rather than being dropped. | T-005 | Complete | Enforced by `tests/test_fail_open_guards.py`, blocking CI. |
| M-008 | Scanner error handling records `type(e).__name__` only, never the exception message, so ARNs and resource detail cannot reach a finding through an error path. | T-006 | Complete | All eight sites in `core/scanner.py`. Verified: zero `str(e)` remaining. |
| M-009 | Two required dependencies only (`boto3`, `pyyaml`), both first-party AWS or widely used. Optional dependencies are per delivery path and not installed by default. | T-007 | Complete | |
| M-010 | `pyyaml` is pinned exactly (`==6.0.2`). `boto3` uses a documented floor (`>=1.40.0`) with the rationale recorded inline, because several checks call APIs newer than 1.35. | T-007 | Complete | |
| M-011 | `SECURITY_SCOPE.md` states the security guarantees as numbered claims, each mapped to a named automated test, so a user can verify the claim rather than trust it. | T-008, T-009 | Complete | |
| M-012 | The canonical repository is published under the `awslabs` organization, giving users a verifiable provenance signal distinct from an arbitrary fork. | T-008 | Complete | |
| M-013 | Seven security-guarantee suites run in a **blocking** `guarantees` CI stage: read-only surface, no deployable remediation, doc-link scope, diagram-scope filter, fail-open guards, check-catalog consistency, and skill integrity. `allow_failure` is not set, so a change that breaks a guarantee fails the pipeline rather than shipping. All suites run offline with no credentials and no network. | T-009 | Complete | `.gitlab-ci.yml`. Previously stated as five: `diagram-scope-filter` was omitted from the list, and `skill-integrity` existed as a test but was not wired into the pipeline. Both corrected. |
| M-014 | Reading an `s3://` diagram uses only the caller's own `s3:GetObject` permission. The tool grants no additional access and reads nothing the operator could not already read. | T-010 | Complete | |
| M-015 | The diagram-review inference is documented as running in the operator's own account under their own model access, so data-retention and invocation-logging posture remain the operator's decision. The tool persists no image and no completion. | T-012 | Complete | |
| M-016 | Zero-infrastructure design: no hosted service, no deployed endpoint, no network listener, no data store. The MCP server uses stdio to a local client. | T-013 | Complete | |
| M-017 | AWS resource-naming rules for the types this tool reads do not permit angle brackets, and the package reads no tag values (`ListTagsFor*` appears nowhere). Runtime-scan identifiers therefore cannot carry markup into the report. **This is an external property, not a control in this code**, and it is the reason follow-up 1 matters: escaping in `reporter.py` would make the guarantee our own. | T-011 | Complete, but externally dependent | Confirmed during security review by sweeping the package for tag-read paths. |

---

## Did we do a good job?

Coverage rationale, per the template's guidance to take a risk-based approach:

- The two highest-value threats for this asset are **false assurance**
  (T-005 - a readiness tool that reports clean on a control it could not check
  is worse than no tool) and **report disclosure** (T-002 - the deliverable is
  a prioritized list of a customer's security gaps). Both are addressed, T-005
  by an enforced control and T-002 by disclosure plus operator ownership.
- The **LLM threats** (T-003, T-004) are the least conventional part of this
  asset and were modelled against OWASP LLM01 and LLM02 specifically. T-004 is
  closed by a single function, which is called out in M-004 as a fragility
  worth hardening rather than left implicit.
- **Three threats are closed by blocking CI rather than by review** (T-005,
  T-006, T-009). This is the strongest property of the asset: the guarantees
  are regression-tested on every commit, not asserted once at review time.
- **One residual risk is knowingly accepted:** T-002. A report enumerating
  security gaps is inherently sensitive, and the tool cannot control where the
  operator stores it. This is disclosed rather than mitigated away.

Known gaps and follow-ups:

1. `reporter.py` should escape on output as defence-in-depth. This matters on two
   paths, not one. On the design path it would remove the dependency on
   `_sanitize()` being the only control closing T-004 (M-004). On the runtime
   scan path it would replace an *external* guarantee with an internal one: today
   T-011 is safe only because AWS naming rules forbid angle brackets in the
   resource types read here (M-017), so the first check that reads a free-text
   field reopens T-004 without any code appearing to change.
2. `CODE_OF_CONDUCT.md` and `NOTICE` are absent, and there is no documented
   vulnerability-reporting path in `README.md`, `CONTRIBUTING.md`, or a
   `SECURITY.md`.
3. `RoleSessionName` is hardcoded to `bedrock-readiness-only`
   (`core/scanner.py:200`), so a cross-account run is not attributable to an
   individual in the target account's CloudTrail. Deriving it from the caller
   identity would fix that.
4. Threat modelling was performed by the authoring team and reviewed by an AWS
   Guardian who is not a contributor, as required.
