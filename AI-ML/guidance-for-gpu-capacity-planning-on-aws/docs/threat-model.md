# Threat Model

Guidance for AI-Powered GPU Capacity Planning on AWS

This document records the security risks introduced by this Guidance and the
mitigations that address them: assets, trust boundaries, data flows, a
STRIDE-based threat enumeration, and the residual risks the deploying customer
owns.

The control references point at the source of truth in this repository:
`template.yaml` for infrastructure and IAM, and `src/` for runtime behavior.
The deployed-control inventory in
[`implementation-guide.md`](implementation-guide.md#step-6-production-hardening)
is the companion checklist to this model.

## 1. Scope

**In scope:** the agent code (`src/`), the SAM template (`template.yaml`), and
the deployment it produces—an Amazon Bedrock-powered Lambda behind API
Gateway, with an optional Lambda Function URL, a DynamoDB cache, Secrets
Manager, a WAF WebACL, and CloudWatch observability.

**Out of scope:** the AWS control plane itself (Lambda, Bedrock, CloudTrail,
API Gateway, and the other managed services this Guidance consumes). Concerns
about those belong to AWS Security under the standard vulnerability reporting
process (see [`SECURITY.md`](../../../SECURITY.md)).

**Deployment model:** this is a reference implementation ("Guidance"), not a
managed service. It runs in the **customer's own AWS account** under the
customer's own IAM. The customer is the operator and owns the residual risks in
section 7.

## 2. System overview

The agent accepts a natural-language capacity question, plans with a Bedrock
model over a set of read-only tools, scores deployment confidence
deterministically, and returns a risk-scored decision package. See the
architecture overview and numbered steps in [`architecture/architecture-steps.md`](architecture/architecture-steps.md) and the data-flow
description in [`implementation-guide.md`](implementation-guide.md).

Every tool in `src/tools/` wraps a **read-only** AWS API (EC2
`Describe*`, CloudTrail `LookupEvents`, CloudWatch `GetMetricStatistics`,
SageMaker `Describe*`/`Search*`, Pricing `GetProducts`, Service Quotas
`ListServiceQuotas`). There are no mutating AWS calls anywhere in the agent.
This is the single most important property of the design: it bounds the blast
radius of every threat below.

## 3. Trust boundaries

| # | Boundary | Control at the boundary |
|---|----------|-------------------------|
| TB1 | Internet → API Gateway `/plan` | AWS WAF WebACL (rate limit, geo-block, AWS managed rule sets) + API Gateway authorization (SigV4 `AWS_IAM` or API key) |
| TB2 | API Gateway → Lambda | AWS service integration; API Gateway stage throttling (5 rps / 10 burst) |
| TB3 | Lambda → Amazon Bedrock | `bedrock:InvokeModel` scoped to foundation models + inference profiles (`BedrockInvokeScope=RegionPinned` by default); optional Bedrock Guardrail |
| TB4 | Lambda → AWS data-plane read APIs | IAM policy grants read-only `Describe`/`List`/`Lookup`/`GetProducts` actions only |
| TB5 | Lambda → customer account (optional) | `sts:AssumeRole` scoped to `AllowedCustomerRoleArnPattern`; only present when cross-account access is configured |
| TB6 | Lambda → Secrets Manager / DynamoDB | `GetSecretValue` scoped to the stack's secret ARN; DynamoDB actions scoped to the cache table ARN |
| TB7 | Internet → Lambda Function URL (opt-in) | `AWS_IAM` (SigV4) only; **bypasses WAF**; disabled by default (`EnableFunctionUrl=false`) |

## 4. Data and classification

| Data | Classification | Handling |
|------|----------------|----------|
| Natural-language query | Customer-supplied, low sensitivity | Size-capped (body ≤ 4 KB, query ≤ 2000 chars); not persisted beyond logs/trace |
| `customer_context` (the customer's own On-Demand Capacity Reservations (ODCRs), CloudTrail Insufficient Capacity Error (ICE) events) | Customer operational data, read from the customer's own account | Read-only; used in-memory to score the answer; cached results in the customer's own DynamoDB table with TTL |
| Caller identity (IAM ARN / API key ID / Cognito sub) | Low sensitivity | SHA-256 hashed (`caller_id_hash`) before any log line; raw identity never logged |
| Agent secret | Secret | Stored in Secrets Manager, cold-start cached; ARN and value never logged |
| Model output (capacity verdict) | Customer operational data | Returned to caller; deterministic scoring record is the auditable artifact |

No PII, PHI, PCI, or other regulated data is collected or processed. All
customer data read by the agent already belongs to the account the agent runs
in (or a cross-account role the customer explicitly grants).

## 5. Assets

- The customer's AWS account and its GPU-capacity posture (the thing an attacker
  would want to read or manipulate).
- The agent's Bedrock spend (a cost-exhaustion target).
- The agent secret and caller-identity data (confidentiality targets).
- The integrity of the capacity verdict (a misleading verdict could inform a bad
  deployment decision).

## 6. STRIDE threat enumeration

| Threat | Vector | Mitigation | Where |
|--------|--------|------------|-------|
| **Spoofing** | Unauthenticated call to `/plan` | API Gateway requires SigV4 (`AWS_IAM`) or an API key; WAF fronts the endpoint | `template.yaml` → `CapacityAgentApi.Auth`, `WafWebACL` |
| **Spoofing** | Unauthenticated call to the Function URL | Function URL is `AWS_IAM` only and disabled by default | `template.yaml` → `CapacityAgentFunctionUrl`, `EnableFunctionUrl` |
| **Tampering** | Oversized / malformed request | Input validation rejects body > 4 KB and query > 2000 chars; WAF managed rules screen known-bad inputs | `src/lambda_handler.py`; `AWSManagedRulesKnownBadInputsRuleSet` |
| **Tampering** | Cache poisoning | DynamoDB access scoped to the cache table; TTL expiry; 400 KB size guard; errors fall through to a live call | `src/tool_cache.py`, `template.yaml` → `ToolCacheTable` |
| **Repudiation** | Untraceable activity | Structured JSON logging with a correlation ID and hashed caller ID; X-Ray tracing active | `src/lambda_handler.py`, `src/metrics.py`; `Tracing: Active` |
| **Information disclosure** | Secret leakage | Secrets Manager with cold-start cache; ARN/value never logged; sentinel-value detection on first boot | `src/secrets_manager.py` |
| **Information disclosure** | Internal detail in error responses | Generic HTTP 500 with a correlation ID only; no stack traces returned | `src/lambda_handler.py` |
| **Information disclosure** | Caller identity in logs | SHA-256 hash of the identity (`caller_id_hash`); optional KMS encryption of log groups | `src/lambda_handler.py`, `template.yaml` → `LogEncryptionKeyArn` |
| **Denial of service** | Request flood | WAF per-IP rate limit (default 100 req / 5 min) + API Gateway throttle (5 rps / 10 burst) + `ReservedConcurrentExecutions` cap | `template.yaml` → `WafWebACL`, `MethodSettings`, `ReservedConcurrentExecutions` |
| **Denial of service** | Runaway Bedrock token spend | `BedrockSpendAlarm` on a token metric filter (fires within ~5 min); per-invocation token budget; timeout-aware loop | `template.yaml` → Bedrock metric filters + alarm; `src/agent.py` |
| **Denial of service** | Concurrency slot exhaustion | `ReservedConcurrentExecutions` bounds worst-case slot-time; `ThrottlesAlarm` signals saturation | `template.yaml`; sizing guidance in `implementation-guide.md` |
| **Elevation of privilege** | Over-broad execution role | Least privilege: read-only actions only; `Resource: "*"` used solely for `Describe`/`List`/`Lookup` actions that do not support resource-level scoping; Bedrock, Secrets, DynamoDB, and STS are ARN-scoped | `template.yaml` → function `Policies` |
| **Elevation of privilege** | Cross-account over-reach | `sts:AssumeRole` is conditional and scoped to `AllowedCustomerRoleArnPattern`; absent unless configured | `template.yaml` → `AssumeCustomerRole` statement |
| **Elevation of privilege** | Prompt injection steering the agent | Input pattern filter (`BLOCKED_PATTERNS`); optional server-side Bedrock Guardrail; **and crucially, every tool is read-only**—an injected instruction cannot change customer infrastructure because the agent holds no write permissions | `src/lambda_handler.py` → `BLOCKED_PATTERNS`; `BedrockGuardrailId`; `src/tools/` |

## 7. Residual risks and operator responsibilities

The deploying customer owns these:

- **Function URL perimeter.** If `EnableFunctionUrl=true`, the Function URL
  bypasses WAF and IAM SigV4 is the sole control. Enable it only when IAM is an
  acceptable sole perimeter, or front it with CloudFront + a regional WAF.
- **API key distribution.** With `AuthMethod=API_KEY`, the customer distributes
  and rotates the key.
- **Secret replacement.** The template ships a placeholder secret value; the
  customer must replace it after deploy.
- **Alarm delivery.** The customer subscribes an endpoint to the alarm SNS topic
  and tunes thresholds to their SLOs.
- **Concurrency sizing.** `ReservedConcurrentExecutions` should be sized to the
  customer's traffic (see `implementation-guide.md`).
- **Optional hardening.** KMS log encryption (`LogEncryptionKeyArn`) and Bedrock
  Guardrails (`BedrockGuardrailId`) are off by default and enabled per the
  customer's requirements.
- **Decision-support only.** The capacity verdict is a signal, not a guarantee;
  it must not be chained into unattended automation. See
  [Known Limitations](../README.md#known-limitations--when-not-to-use).

## 8. Disclaimer

This is non-production reference content released under the MIT-0 license. Work
with your own security and legal teams to meet your organizational security,
regulatory, and compliance requirements before deploying it.
