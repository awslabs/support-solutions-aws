# Threat Model — Well-Architected Review MCP Server for Amazon SageMaker

## 1. System description

An MCP (Model Context Protocol) server that validates Amazon SageMaker
resources (endpoints, training jobs, notebook instances, models) against the
six AWS Well-Architected pillars and returns findings with severities and
recommendations. All AWS access is **read-only** (Describe / List / Get); the
server never creates, modifies, or deletes customer resources.

It runs in two modes:

- **Local (stdio):** the user's IDE spawns the server as a subprocess; it uses
  the user's own AWS credentials and talks over stdin/stdout. No network
  listener.
- **Hosted (Amazon Bedrock AgentCore Runtime):** the server runs as an ARM64
  container behind the AgentCore Runtime endpoint, authenticated with SigV4
  (IAM) over HTTPS, using a scoped IAM execution role. Stateless HTTP.

## 2. Data flow

`MCP client / agent (or IDE via proxy)`  →  `server`  →  `AWS read-only APIs`
(SageMaker, CloudWatch, S3 config, EC2 networking, IAM, KMS, CloudTrail,
Config, Application Auto Scaling, Service Quotas, STS)  →  findings returned to
the caller. See the reference architecture in `DEPLOYMENT.md` for the hosted path.

## 3. Assets

- AWS credentials (local) / IAM execution role (hosted).
- Resource-configuration data contained in findings.
- The container image and its dependencies.
- The MCP transport channel.

## 4. Trust boundaries

- Caller → server (authentication).
- Server → AWS APIs (IAM authorization).
- Local machine (user-controlled) vs. hosted runtime (AWS-managed).

## 5. Assumptions

- AWS IAM correctly enforces the execution role's read-only scope.
- The customer secures their AWS account and controls who may invoke the
  hosted runtime.
- Caller AWS credentials are managed outside this server; the server stores no
  secrets.
- The MCP client/agent consuming findings is trusted by the operator.

## 6. Threats and mitigations (STRIDE)

| Threat | Scenario | Mitigation |
|---|---|---|
| **Spoofing** | Unauthorized caller invokes the hosted runtime | SigV4/IAM authentication required; endpoint is auth-gated. Local mode is single-user over stdio (no listener). |
| **Tampering** | Modified container image or dependencies | Image built and stored in CDK-managed ECR; dependencies pinned via `uv.lock`. Server performs no writes to customer resources. |
| **Repudiation** | API activity not attributable | All calls made under the execution role are recorded in the customer's CloudTrail; a custom user-agent identifies the tool. |
| **Information disclosure** | Findings expose resource configuration; on the hosted runtime, any authenticated caller shares the execution role's read view | Read-only config metadata only (no data-plane content). Restrict who can invoke the runtime via IAM; deploy separate stacks (separate roles) per team/environment; treat findings as sensitive. |
| **Denial of service** | Excessive tool invocations | Bounded by AWS API throttling and AgentCore runtime limits; server is stateless and stores nothing. |
| **Elevation of privilege** | Server obtains more access than intended | Least-privilege, read-only execution role (no write/mutate actions, no broad managed policies). The AgentCore construct adds only scoped operational permissions (CloudWatch Logs, X-Ray, `PutMetricData` limited to the `bedrock-agentcore` namespace, ECR image pull, workload-identity token). |

## 7. Out of scope

- The security posture of the customer's own SageMaker workloads (that is what
  the tool assesses, not what this model covers).
- The security of the MCP client/agent that consumes the findings.
- AWS platform security (covered by the AWS shared responsibility model).

## 8. Key property

The server is **read-only and stateless** — it cannot alter customer resources
and persists no customer data, which bounds the impact of every threat above.
