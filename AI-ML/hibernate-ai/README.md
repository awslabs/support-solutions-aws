# HibernateAI — Intelligent Resource Lifecycle Management

An Amazon CloudWatch Dashboard custom widget that uses Amazon Bedrock to analyze resource usage
patterns and provide intelligent cost-optimization recommendations across 15+ AWS services, all
inside the AWS Console.

> **Disclaimer:** Before using this solution, you should consider conducting your own
> independent assessment to ensure that your use would comply with your own specific
> security and quality control practices and standards, as well as the laws, rules, and
> regulations that govern you and your content.

> ### 🟢 v1 — Read-Only / Advisory (this release)
> **This published version is advisory-only.** It analyzes idle resources and estimates savings
> but performs **no destructive or state-mutating actions** — no hibernate, terminate, stop,
> delete, modify, snapshot, or restore. Its IAM grants read-only discovery (`Describe*`/`List*`),
> CloudWatch metric reads, and Bedrock invoke **only** — the widget structurally cannot change any
> customer resource. This makes it safe to deploy anywhere and keeps the security surface minimal.
>
> **Hibernate/restore (destructive) capability ships separately** as a customer-authorized,
> independently security-reviewed add-on that a customer explicitly opts into deploying — it is NOT
> part of this read-only artifact. The "Safety & Governance" section below describes the controls
> that will govern that add-on.

## Features (v1, read-only)

- **Smart Recommendations** — Analyzes CloudWatch metrics (CPU, network, DB connections; configurable lookback, default 15 days) and uses Bedrock to identify idle resources, suggest right-sizing, and estimate savings. A **deterministic guardrail** overrides any AI recommendation the metrics contradict, and thumbs up/down feedback feeds a closed loop.
- **Model resilience** — The Bedrock model is config/env-driven with an ordered **fallback chain** (Haiku → Sonnet → …) and retry, so analysis survives throttling or model-availability changes.
- **Natural Language Queries** — Type "what's costing me the most?" or "show idle dev resources" and the AI interprets your intent (advisory responses; destructive commands are declined in read-only mode).
- **Flexible tagging & grouping** — Adapts to your existing tags (`project_name`, `App_name`, `asset_id`, `cost_center`, …) via configurable fallback keys; group the dashboard by any dimension (project / LOB / cost-center). No forced convention.
- **Cross-service idle detection** — EC2, RDS, Redshift, OpenSearch, ElastiCache, SageMaker, EMR, EKS, ELB, FSx, WorkSpaces, AppStream, and more — one consolidated view.
- **Enterprise multi-account** — Consolidated cross-account read view via a cross-account read role.
- **Zero External Access** — Everything runs inside the AWS Console, no public endpoints.

## Safety & Governance

**This read-only release performs no destructive actions**, so there is nothing here that can
stop, terminate, delete, or modify a customer resource — the IAM role and its permissions
boundary are read-only (`Describe*`/`List*` + CloudWatch metric reads + the solution's own
DynamoDB table/SNS topic). Discovery and recommendations only.

The destructive **hibernate/restore capability ships separately** as a customer-authorized
add-on with its own security review. That add-on carries layered, fail-safe governance
(explicit enrollment tag enforced at the IAM layer, protected-tag/deny-list/fail-safe-prod
classification, IaC-drift stop-start, cost-threshold approval, OU awareness, role separation,
and an IAM explicit-Deny backstop). Those controls are documented with the add-on, not here,
since none of them are exercised by this release.

## Prerequisites

- An AWS account with credentials configured in your terminal (`aws configure` or `aws sso login`).
- [AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html) v2 and Python 3.12.
- Amazon Bedrock model access enabled for the model(s) in your configured chain (default: Anthropic Claude 3 Haiku, with Claude 3.5 / 3 Sonnet fallbacks) in your target region.

## Deploy

```bash
# 1. Enable Bedrock model access in the console for your region:
#    https://console.aws.amazon.com/bedrock/home#/modelaccess

# 2. Install dependencies and deploy
python -m pip install -r requirements.txt
cdk deploy HibernateAI
```

The primary model and fallback chain are configurable at deploy time via the
`BEDROCK_MODEL_ID` / `BEDROCK_MODEL_FALLBACKS` environment variables (or at runtime via the
DynamoDB `__config__` item) — no code change required.

## Testing

```bash
python -m pip install -r requirements-dev.txt
pytest                              # unit + integration (AWS mocked with moto)
python tests/eval_recommendations.py   # recommendation-quality eval gate
cdk synth HibernateAI               # validate the stack
```

## Architecture

```
CloudWatch Dashboard "HibernateAI"
  └── Custom Widget (Lambda)
        ├── Discover: EC2/EBS/RDS/Aurora + 11 extended services, grouped by a configurable tag
        ├── AI Analyze: CloudWatch metrics → Bedrock (model chain w/ fallback) → guardrail → recs
        ├── Recommend: advisory hibernate candidates + estimated savings (read-only; no actions taken)
        ├── Feedback: 👍/👎 capture → closed-loop few-shot corrections
        └── NLP: natural language → intent parsing → advisory responses
```

Deploys a small set of resources via CDK: Lambda, IAM Role/Policy (read-only, capped by a
permissions boundary), DynamoDB state table, CloudWatch Dashboard, SNS topic. Multi-account
read-only rollout is available via CloudFormation StackSets. See `ARCHITECTURE.md` for the full
diagram.

## Getting Started

1. **Tag resources** so HibernateAI can group them. By default it groups by `project_name`,
   but it adapts to your existing tags (`App_name`, `asset_id`, `cost_center`, …) — see
   the flexible-tagging config in `ARCHITECTURE.md`.
   ```bash
   aws ec2 create-tags --resources i-xxxxxxxx \
     --tags Key=project_name,Value=my-project Key=environment,Value=dev
   ```
2. **Open the dashboard** — CloudWatch → Dashboards → **HibernateAI**. Project cards show
   resource counts and estimated hourly/monthly cost.
3. **Analyze** — click *AI Analyze All* to get recommendations from CloudWatch metrics
   (configurable lookback, default 15 days), validated by the deterministic guardrail. Use
   👍/👎 to feed the feedback loop.
4. **Review recommendations** — the dashboard shows advisory hibernate candidates and
   estimated savings. *(Acting on them — snapshot/terminate/restore — is the separate
   destructive add-on, not this read-only release.)*

## IAM permissions required

This read-only solution provisions a least-privilege Lambda execution role, capped by a
permissions boundary, that grants only:

- **Read-only discovery** — `Describe*` / `List*` on the supported services (EC2, EBS, RDS,
  Aurora, Redshift, OpenSearch, ElastiCache, SageMaker, EMR, EKS, ELB, FSx, WorkSpaces,
  AppStream) and `organizations:List*`/`Describe*` for multi-account discovery. These read
  actions do not support resource-level scoping, so they use `Resource: "*"`.
- **CloudWatch metric reads** — `cloudwatch:GetMetricData`, `GetMetricStatistics`,
  `ListMetrics`.
- **Amazon Bedrock** — `bedrock:InvokeModel` / `InvokeModelWithResponseStream`, scoped to the
  allow-listed Claude model ARNs only.
- **The solution's own resources** — read/write on its DynamoDB state table and `sns:Publish`
  on its own topic; CloudWatch Logs for the Lambda.
- **Cross-account (optional)** — `sts:AssumeRole` into a read-only cross-account role that is
  limited to `Describe*` + metric reads, trusts only the management account, and is gated by
  `aws:PrincipalOrgID` / `aws:ResourceOrgID`.

The role has **no** create, modify, stop, terminate, delete, snapshot, or restore permission
on customer resources, and the permissions boundary enforces this ceiling.

## Cleanup

Remove all resources created by this solution:

```bash
cdk destroy HibernateAI
```

The DynamoDB state table, Lambda, IAM role/policy, permissions boundary, CloudWatch dashboard,
and SNS topic are deleted with the stack. If you deployed the optional cross-account read role
via StackSets, delete those stack instances / the StackSet separately. Tags you added to your
own resources (e.g. `project_name`) are left untouched — remove them manually if desired.

## Cost

HibernateAI itself is near-free (Lambda + DynamoDB on-demand + a CloudWatch dashboard);
its own running cost is tracked in the widget's cost-transparency panel.

## Documentation

- `ARCHITECTURE.md` — architecture, read-only IAM posture, config reference
- Well-Architected alignment and the deployment/operations guide are maintained as separate
  supporting documents (not bundled in this repo).

## Security

This release is **read-only/advisory** — the IAM role (capped by a permissions boundary)
grants only `Describe*`/`List*` reads, CloudWatch metric reads, Bedrock `InvokeModel` on an
allow-listed model set, and access to the solution's own DynamoDB table and SNS topic. It
cannot stop, terminate, delete, or modify any customer resource. To report a security issue,
see the repository [SECURITY.md](../../SECURITY.md).

## Contributing

Contributions are welcome — see the repository [CONTRIBUTING.md](../../CONTRIBUTING.md) and
[CODE_OF_CONDUCT.md](../../CODE_OF_CONDUCT.md).

## License

Licensed under the MIT-0 License. See the repository [LICENSE](../../LICENSE).
