# Security Scope Statement

> **Sample content -- not for production use without additional security
> testing.** Provided as is, with no warranty of workmanship or fitness for
> purpose, and no claim of compliance with any regulation or standard.

This document states, in one place, what this solution does and does not do
from a security perspective, and points at the automated tests that enforce
each claim. It exists so a security reviewer does not have to reconstruct the
scope contract from source comments.

Every claim below is enforced structurally in code and verified by an
automated test that runs offline (no AWS credentials, no network required).

## What this solution is

A read-only Amazon Bedrock production-readiness assessor. It reports whether a
Bedrock deployment is *operationally* ready across eight pillars
(Observability, Architecture & Resilience, Quota & Capacity, Cost
Optimization, Model Fitness, Security, Guardrails, Data Governance) and links
each finding to public AWS documentation. It is 1:Many reusable sample
content, licensed MIT-0.

## The scope contract

Every claim below is enforced structurally in code and asserted by a test in
`tests/`, not merely documented here.

| # | Claim | How it is enforced | Test |
|---|-------|--------------------|------|
| 1 | **Read-only only.** Every AWS call is `describe`/`list`/`get`, plus `bedrock:InvokeModel` (diagram inference) and `s3:GetObject` (diagram from S3). No `create`/`put`/`update`/`delete`/`modify`/`attach`/`detach`/... anywhere. | The real call sites are derived from source and checked against the declared surface in `core/scanner.py`; any mutating verb prefix fails the test. | `tests/test_readonly_surface.py` |
| 2 | **No deployable remediation, for any pillar.** No recommendation is ever a corrected IAM policy, guardrail configuration, CloudFormation/Terraform/CDK template, or anything else meant to be applied as-is. Recommendations are plain prose. | `Finding` has no URL/template/policy field. Pillar module sources are statically scanned for IaC markers, policy-document literals, code fences, and raw-document interpolation. | `tests/test_no_deployable_remediation.py` |
| 3 | **No prescriptive security guidance.** The Security/Guardrails/Data Governance pillars *report on* posture (e.g. "this role grants a wildcard Bedrock action") and link to AWS docs. They never tell a customer how to *implement* an authN/authZ solution, hand back a WAF ruleset, or emit a security config to deploy. | Same static guard as #2, plus doc-link scope rules restricting security/identity/encryption pages to the three security pillars and forbidding any compliance-outcome claim. | `tests/test_no_deployable_remediation.py`, `tests/test_references.py` |
| 4 | **No security advice from the diagram review.** A vision model reading a picture is an unverifiable source, so the diagram path produces only Observability/Architecture/Quota/Cost/Model-Fitness findings -- never Security/Guardrails/Data-Governance. | Three-layer scope filter (prompt + discarded `out_of_scope_notes` + deterministic post-filter that drops security/IaC-marked findings and strips code fences). Layers 2-3 do not depend on the model complying. | `tests/run_diagram_review.py` |
| 5 | **Scoped, least-privilege reads.** IAM policy-document *contents* are read only for roles already identified as trusted by a Bedrock service principal -- never a blanket account dump. S3 config reads hit only the one bucket Bedrock is configured to log to. | `GetAccountAuthorizationDetails` and bucket-policy/enumeration calls are on the never-called list and absent from source; policy reads are gated on `AssumeRolePolicyDocument`. | `tests/test_readonly_surface.py` |
| 6 | **No compliance claims.** The tool never states that following it makes a customer compliant with HIPAA/PCI/GDPR/etc. | Reference titles and URLs are barred from compliance-outcome terms for every pillar, no exception. | `tests/test_references.py` |
| 7 | **A partial-failure read never reports a clean PASS.** If a security-relevant document could not be read, the finding is WARN/ERROR, never PASS. | Fail-open guards across the security pillars. | `tests/test_fail_open_guards.py` |
| 8 | **No data retained.** Diagram bytes are read, analyzed, and discarded; only the path string is kept for provenance. Generic errors log the exception *type name only*, never the message, so ARNs/hostnames/contents cannot leak into a report. | `del image_bytes` after the model call; `ScanError` stores `type(e).__name__` for non-`ClientError` exceptions. | Reviewed in `core/diagram_review.py`, `core/scanner.py` |

## Design boundaries

The solution deliberately keeps the following outside its scope:

- It **assesses**, it does not implement. Every pillar is a read-only
  observation (claim #1); it changes no authentication, authorization, or
  security control.
- It emits **no deployable security artifact** (claims #2, #3). It cannot
  produce a corrected IAM policy, guardrail configuration, WAF rule, or IaC
  template, by structure.
- It points **at** AWS security best practices via curated AWS-owned
  documentation links, and never claims a compliance outcome (claims #3, #6).
- It handles **only configuration metadata** about resources, never regulated
  data or a customer's actual policy documents.

## Reproducing the evidence

All guarantee tests run offline -- no AWS credentials, no network:

```bash
python tests/test_readonly_surface.py           # claims 1, 5
python tests/test_no_deployable_remediation.py  # claims 2, 3
python tests/test_references.py                  # claims 3, 6
python tests/run_diagram_review.py              # claim 4
python tests/test_fail_open_guards.py            # claim 7
python tests/test_check_catalog.py               # catalog matches emitted checks
python tests/run_fixtures.py                     # end-to-end pipeline
```

These run offline — no AWS credentials, no network — so you can reproduce the
scope contract above yourself.
