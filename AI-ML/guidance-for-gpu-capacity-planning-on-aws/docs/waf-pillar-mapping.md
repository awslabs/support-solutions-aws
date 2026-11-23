# Well-Architected Framework Pillar Mapping

> **Disclaimer:** This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory, and compliance requirements before deployment.

## Guidance for AI-Powered GPU Capacity Planning on AWS

This document maps the solution's design decisions to the AWS Well-Architected Framework pillars.

---

## Operational Excellence

| Design Decision | WAF Alignment |
|----------------|---------------|
| Structured output format (feasibility verdict + cost + alternatives) | Consistent, repeatable operational procedures |
| Citation discipline, every claim cites its source API | Observability and auditability of decisions |
| CloudWatch logging of invocations, token usage, tool call latency | Measure operational health |
| Bounded iteration loop (max 8) with graceful degradation | Anticipate failure; design for recovery |
| Demo scenarios as regression tests | Validate behavior through regular testing |

**Relevant best practices:** OPS06 (Design workloads to understand their state), OPS08 (Reduce defects, ease remediation)

---

## Security

| Design Decision | WAF Alignment |
|----------------|---------------|
| Read-only APIs only, no mutating actions | Principle of least privilege |
| Least-privilege IAM policy: 11 read-only data-source actions (no mutating calls), scoped Bedrock invoke, no service-level `*` | Grant least privilege |
| No customer data stored, stateless Lambda execution | Protect data at rest |
| API Gateway with IAM auth (default) | Protect data in transit; authenticate requests |
| No secrets in code, model ID and region via environment variables | Manage secrets securely |
| Input validation in Lambda handler (rejects empty/malformed requests) | Validate inputs |

**Relevant best practices:** SEC03 (Manage permissions), SEC07 (Classify data), SEC09 (Protect data in transit)

---

## Reliability

| Design Decision | WAF Alignment |
|----------------|---------------|
| Multi-AZ availability check built into the agent's first tool call | Design for multi-AZ |
| Graceful handling of tool failures (reports failure, continues with available data) | Design for failure |
| Bounded retries with max_iterations | Prevent cascading failures |
| Stateless Lambda, no single point of failure | Eliminate single points of failure |
| Agent distinguishes "not offered" (permanent) vs "quota too low" (fixable) | Understand failure modes |

**Relevant best practices:** REL05 (Design interactions to prevent failures), REL09 (Back up data), REL11 (Design for failure)

---

## Performance Efficiency

| Design Decision | WAF Alignment |
|----------------|---------------|
| Parallel tool calls when inputs are independent | Use resources efficiently |
| Temperature 0.0 for deterministic, fast responses | Select appropriate resource type |
| Token budget awareness, system prompt is concise (~300 words) | Monitor and optimize |
| LRU caching on boto3 clients (avoids re-initialization per tool call) | Use caching to improve performance |
| Per-request model selection (Amazon Nova Micro or Lite for simple asks, Claude Haiku 4.5 default, Claude Sonnet 4.5 for complex reasoning) | Right-size resources |

**Relevant best practices:** PERF01 (Select appropriate resource types), PERF04 (Use monitoring to improve)

---

## Cost Optimization

| Design Decision | WAF Alignment |
|----------------|---------------|
| The solution itself optimizes customer costs (surfaces pricing + alternatives before deployment) | Expenditure awareness |
| Pay-per-invocation (Lambda + API Gateway), no idle infrastructure | Match supply with demand |
| Token cost tracking in output (`usage.input_tokens`, `usage.output_tokens`) | Monitor cost drivers |
| PPA/discount-aware projections, shows net cost, not just list price | Expenditure awareness |
| No infrastructure to maintain, serverless, scales to zero | Eliminate waste |
| Agent cost per run: ~$0.01-0.15 vs hours of manual research | Evaluate cost of alternatives |

**Relevant best practices:** COST02 (Governance), COST04 (Decommission resources), COST07 (Optimize over time)

---

## Sustainability

| Design Decision | WAF Alignment |
|----------------|---------------|
| Serverless, no always-on compute | Minimize idle resources |
| Helps customers right-size GPU deployments (avoid over-provisioning) | Maximize utilization |
| Bounded iterations prevent unnecessary compute | Reduce downstream impact |

**Relevant best practices:** SUS02 (Align to demand), SUS04 (Maximize utilization)
