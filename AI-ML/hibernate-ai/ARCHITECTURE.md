# Hibernate AI — Architecture & Flow Diagrams

> **v1 is read-only / advisory.** The widget reads and analyzes AWS resource usage and presents
> recommendations. It does **not** snapshot, stop, terminate, delete, or otherwise mutate any
> customer resource. The destructive hibernate/restore capability is a **separate future add-on**
> (not in v1) that will ship later with its own security review.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          AWS Customer Account                                │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │           CloudWatch Dashboard "HibernateAI" (read-only / advisory)    │   │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌──────────────┐  │   │
│  │  │  Project A  │ │  Project B  │ │  Project C  │ │  AI Advisor  │  │   │
│  │  │  Dev (3 EC2)│ │  QA (5 EC2) │ │  Stg (2 RDS)│ │  💡 Analyze  │  │   │
│  │  │  $45/day    │ │  $120/day   │ │  $80/day    │ │  📊 Report   │  │   │
│  │  │ advice only │ │ advice only │ │ advice only │ │ (no actions) │  │   │
│  │  └─────────────┘ └─────────────┘ └─────────────┘ └──────────────┘  │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│           │                                                                   │
│           ▼                                                                   │
│  ┌─────────────────┐                                                         │
│  │  Lambda Function │ ◄──── EventBridge Schedule (daily analysis)            │
│  │  (Hibernate AI)  │        read-only / advisory                            │
│  └────────┬─────────┘                                                         │
│           │                                                                   │
│     ┌─────┼──────────────────────────────────┐                               │
│     │     │     │        │        │          │                               │
│     ▼     ▼     ▼        ▼        ▼          ▼                               │
│  ┌─────┐┌────┐┌─────┐┌───────┐┌──────┐┌──────────┐                         │
│  │ EC2 ││EBS ││ RDS ││Aurora ││Redsh.││ 10+ more │  Describe*/List* (read)  │
│  │ API ││API ││ API ││ API   ││ API  ││ services │                          │
│  └─────┘└────┘└─────┘└───────┘└──────┘└──────────┘                         │
│     │     │     │        │        │          │                               │
│     ▼     ▼     ▼        ▼        ▼          ▼                               │
│  ┌──────────────────────────────────────────────────┐                        │
│  │              CloudWatch Metrics (read)              │                        │
│  │    CPU, Memory, Connections, Network, IOPS          │                        │
│  │    (configurable lookback: default 15 days, 1–90)   │                        │
│  └──────────────────────────────────────────────────┘                        │
│           │                                                                   │
│           ▼                                                                   │
│  ┌──────────────────┐      ┌────────────────────┐                            │
│  │  Amazon Bedrock   │      │    DynamoDB Table    │                            │
│  │  (Claude 3 Haiku) │      │  (solution's own)    │                            │
│  │                   │      │  - Runtime config     │                            │
│  │ • Usage analysis  │      │  - Rules              │                            │
│  │ • Recommendations │      │  - Feedback           │                            │
│  │ • NLP intent      │      │  - Recommendation log │                            │
│  └──────────────────┘      └────────────────────┘                            │
│                                                                              │
│  ┌──────────────────┐      ┌────────────────────┐                            │
│  │   SNS / Email     │      │   IAM Role           │                            │
│  │  Notifications    │      │   (read-only +        │                            │
│  │                   │      │    perms boundary)    │                            │
│  └──────────────────┘      └────────────────────┘                            │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Flow Diagram — Advisory Recommendation Flow (v1)

v1 reads and analyzes only. No resource is snapshotted, stopped, terminated, or deleted.

```
┌──────────┐    ┌───────────────┐    ┌──────────────────┐    ┌─────────────┐
│  Trigger │───▶│ Discover      │───▶│ Analyze Usage    │───▶│ Recommend   │
│          │    │ (read-only)   │    │ (CloudWatch,     │    │ (Bedrock)   │
│• Schedule│    │• By project   │    │  default 15d)    │    │• safe_to_save│
│• Manual  │    │• By env tag   │    │• CPU avg/peak    │    │• review_needed│
│• NLP cmd │    │• By service   │    │• Network / conns │    │• keep_running│
└──────────┘    └───────────────┘    └──────────────────┘    └──────┬──────┘
   (Describe*/List* only — no mutating calls)                        │
                                                                      ▼
                    ┌──────────────────┐    ┌───────────────────┐
                    │ Present Advice   │◄───│ Guardrail cross-   │
                    │ (no actions)     │    │ check vs metrics   │
                    │• Dashboard cards │    │• downgrade-only    │
                    │• Savings estimate│    │  (safer, never     │
                    │• Confidence score│    │   more aggressive) │
                    └──────────────────┘    └───────────────────┘
```

### Future add-on (NOT in v1)

The destructive hibernate (snapshot + terminate) and wake (restore) flows are a **separate
future add-on** and are **not implemented or deployable in v1**. The shipped v1 code **cannot**
perform them — it has no destructive IAM permissions and the destructive widget routes return a
"read-only (advisory) mode" response. When this add-on is built, it will be delivered with its
own dedicated dedicated security review before any mutating capability is enabled.

## Flow Diagram — AI Analysis Flow

```
┌──────────────┐    ┌──────────────────┐    ┌───────────────────┐
│ Collect      │───▶│ Build Prompt     │───▶│ Bedrock Claude    │
│ Metrics      │    │                  │    │                   │
│• CW (def 15d)│    │• Resource type   │    │• Analyze patterns │
│• Per resource│    │• Usage metrics   │    │• Classify risk    │
│• Per project │    │• Cost data       │    │• Estimate savings │
└──────────────┘    └──────────────────┘    └────────┬──────────┘
                                                      │
                                                      ▼
                    ┌──────────────────┐    ┌───────────────────┐
                    │ Present to User  │◄───│ Generate          │
                    │ (advice only)    │    │ Recommendations   │
                    │• Dashboard cards │    │• Per resource     │
                    │• Savings estimate│    │• Per project      │
                    │• Confidence score│    │• With reasoning   │
                    └──────────────────┘    └───────────────────┘
```

## Supported Services (Target: 15+)

In v1 the widget only **reads and analyzes** each service (Describe*/List* + CloudWatch
metrics) to produce advice. The "Planned" column describes capability of the **future add-on
(not in v1)** and is **not implemented or deployable in the shipped code**.

| # | Service | What v1 analyzes (read-only) | Planned (future add-on, not in v1) |
|---|---------|------------------------------|------------------------------------|
| 1 | EC2 | Instance inventory + CPU/network metrics | Stop or Snapshot+Terminate / Start or Launch from AMI |
| 2 | EBS (unattached) | Volume inventory + attachment/usage | Snapshot+Delete / Create from snapshot |
| 3 | RDS | DB instance inventory + connections/CPU | Snapshot+Delete / Restore from snapshot |
| 4 | Aurora | Cluster inventory + usage | Snapshot+Delete cluster / Restore cluster |
| 5 | Redshift | Cluster inventory + usage | Pause or Snapshot+Delete / Resume or Restore |
| 6 | OpenSearch | Domain inventory + usage | Snapshot+Delete / Restore from snapshot |
| 7 | ElastiCache | Cluster inventory + usage | Snapshot+Delete / Restore |
| 8 | SageMaker Endpoints | Endpoint inventory + usage | Delete endpoint / Re-create from config |
| 9 | EMR | Cluster inventory + usage | Terminate cluster / Re-create from saved config |
| 10 | EKS Node Groups | Node group inventory + usage | Scale to 0 / Scale back up |
| 11 | NAT Gateway | Gateway inventory + usage | Delete+save EIP / Re-create+associate EIP |
| 12 | ELB (idle) | Load balancer inventory + usage | Delete+save config / Re-create |
| 13 | FSx | File system inventory + usage | Backup+Delete / Restore from backup |
| 14 | WorkSpaces | Workspace inventory + usage | Stop / Start |
| 15 | AppStream Fleets | Fleet inventory + usage | Stop fleet / Start fleet |

## Pre-requisites

Resources are grouped by tag. By default HibernateAI groups by `project_name`, but it
**adapts to your existing tags** — you do not have to adopt our convention:
```
project_name = <project-identifier>     (default grouping key, e.g. "lending-platform")
environment  = <env>                     (e.g., "dev", "staging", "qa", "sandbox")
```

Flexible tagging (review #1/#2), configured in DynamoDB `__config__` via `config.py`:
- `grouping_tag_keys` — ordered fallback list; discovery uses the first present key per
  resource (e.g. `["project_name", "App_name", "asset_id"]`). Falls back to `__untagged__`.
- `environment_tag_keys` — ordered fallback for environment classification
  (e.g. `["environment", "env", "Stage"]`).
- `grouping_dimension` — group the dashboard by any tag (project / LOB / cost-center);
  overrides the fallback list when set.
- Onboarding auto-detects common tag keys already in the account and proposes a mapping
  (`detect_tags` action); our convention is only required if nothing usable is found.

Backward compatible: with no config, grouping is exactly `project_name` as before.

Optional tags (interpreted for **recommendation** purposes only in v1):
```
hibernate_exempt   = true                (exclude this resource from recommendations)
```

## Safety & Governance (defense-in-depth)

**v1 performs no destructive actions**, so the primary safety guarantee is structural: IAM and
the permissions boundary make destruction **impossible** — the role simply has no
snapshot/stop/terminate/delete/create permissions on customer resources, so there is no
destructive path to gate. The controls below therefore govern only **recommendation quality**
(what the widget RECOMMENDS), not the gating of any destructive execution.

| Layer | Module | Behavior (advisory only) |
|-------|--------|--------------------------|
| 1. Protected tags | `index._is_protected` | Excludes `Environment=prod/prd` and `CostSaverProtect` resources from "safe to hibernate" advice. |
| 2. Custom rules | `rules.evaluate_resource` | User tag/metric/state/age skip rules shape which resources are recommended. |
| 3. Deny/skip-list | `safety.matches_deny_list` | Account IDs, OU IDs, resource ARNs, tag-combos in `__config__` are excluded from recommendations. |
| 4. Fail-safe classification | `safety.is_protected_failsafe` | **Unknown/unclassifiable environment ⇒ treated as PROTECTED** for recommendation purposes. |
| 5. OU awareness | `safety.account_is_prod_ou` | Accounts in a configured prod OU are surfaced as **recommend-only / do-not-hibernate** advice. |
| 6. Guardrail cross-check | `feedback.validate_recommendation` | Deterministic CloudWatch-metric cross-check that can only **downgrade** a recommendation to be safer. |

All governance config lives in DynamoDB under the reserved `__config__` key (see `config.py`)
and is **backward compatible**: an empty config reproduces the default advisory behavior (group
by `project_name`, no deny-list).

### IAM-layer defense (read-only policy + permissions boundary)

v1's safety is enforced in IAM so that no code path or misconfiguration can mutate a resource —
the capability is simply absent:
- **Read-only execution policy.** The widget role grants only `Describe*`/`List*`/`Get*` on the
  supported services (EC2, RDS, Redshift, OpenSearch/es, ElastiCache, SageMaker, EMR, EKS, ELB,
  FSx, WorkSpaces, AppStream), `cloudwatch:GetMetricData`/`GetMetricStatistics`/`ListMetrics`,
  `dynamodb:GetItem`/`PutItem`/`DeleteItem` on the solution's **own** state table only,
  `sns:Publish` on its own topic, `organizations` List/Describe (read), `sts:AssumeRole` +
  `GetCallerIdentity`, and `bedrock:InvokeModel`/`InvokeModelWithResponseStream`. There are
  **no** `Terminate`/`Delete`/`Stop`/`Snapshot`/`Create` actions on customer resources.
- **Permissions boundary (`HibernateAI-Widget-Boundary`).** Applied to the widget role as a
  **hard ceiling** that caps effective permissions at read-only (Describe/List/Get + CloudWatch
  metric reads + the solution's own DynamoDB table + SNS publish + STS). It prevents privilege
  creep: any Allow added outside this read-only surface is denied by the boundary.
- **`Resource:"*"` is read-only only.** The remaining `Resource:"*"` entries are the
  `Describe*`/`List*` discovery actions (which AWS does not allow to be resource-scoped) and the
  solution's own DynamoDB table. They read; they cannot create, modify, or delete. Documented in
  `stacks/__init__.py` and guarded by `tests/test_iam_least_privilege.py`.
- **Bedrock** is an ARN allowlist of three Claude model ARNs, not `*`.
- **StackSets / Organizations / cross-account** reads are scoped to the `HibernateAI-*` names;
  the cross-account role is read-only (see Enterprise multi-account below).

## Model abstraction & resilience (review #7)

The Bedrock model is **not hardcoded**. `advisor.get_model_chain()` builds an ordered
try-list from (1) DynamoDB `__config__` (`bedrock_model` + `bedrock_model_fallbacks`),
(2) env `BEDROCK_MODEL_ID` / `BEDROCK_MODEL_FALLBACKS`, (3) built-in defaults
(Claude 3 Haiku → Claude 3.5 Sonnet → Claude 3 Sonnet). `_call_bedrock` retries throttles with backoff and
**falls back to the next model** on `Throttling`/`ModelNotReady`/`ServiceUnavailable`/
`ResourceNotFound`/`AccessDenied`/`Validation` errors; if all fail it returns a structured
error (the widget degrades instead of crashing). Inference-profile / cross-region ARNs are
supported. CDK IAM grants `bedrock:InvokeModel` to an **allowlist of model ARNs** (+
inference-profile ARNs) — still least-privilege, never `*`.

## Future add-on — restore resilience, network drift & RTO (NOT in v1)

> This section describes design intent for the **separate future hibernate/restore add-on**.
> It is **not implemented in v1**; the shipped v1 code captures no snapshots and performs no
> restore. The add-on will be delivered with its own dedicated security review.

- **Full network context** would be captured at save: AMI, instance type, subnet, security
  groups, private IP, placement/AZ, IAM instance profile, and Elastic IP allocation.
- **Pre-restore validation** (`network.validate_restore_network`): before relaunch, verify the
  captured security groups and subnet still exist, reconciling drift rather than failing
  silently.
- **RTO expectations:** restore would relaunch from the AMI captured at save; typical EC2 RTO is
  minutes (AMI boot + waiter).
- **Max-hibernation assumptions:** restore validity depends on AMI/snapshot retention — document
  a retention policy that exceeds your maximum expected hibernation window.

## Recommendation validation & feedback loop (review #8)

Bedrock output is not trusted blindly:
- **Deterministic guardrail** (`feedback.validate_recommendation`): before display, each
  `safe_to_save` verdict is cross-checked against the resource's CloudWatch metrics
  (configurable lookback, default 15 days, clamped 1–90). If avg CPU
  ≥ `max_cpu_pct_for_save` (default 10%) or avg DB connections ≥
  `max_db_connections_for_save` (default 1) — with actual datapoints — the verdict is
  downgraded to `review_needed` and flagged. The guardrail only ever makes a recommendation
  *safer*, never more aggressive.
- **Human feedback** (`handle_feedback`): thumbs up/down (+ optional reason) per
  recommendation, stored under `__feedback__<id>` in DynamoDB.
- **Closed loop:** recent feedback is injected into the advisor prompt as few-shot
  corrections (`feedback.recent_feedback_examples`) so past mistakes are not repeated.
- **Eval harness** (`tests/eval_recommendations.py`): an offline labeled idle/active
  dataset measures precision/recall of the guardrail and **fails CI** below threshold
  (default: precision 1.0 = never green-light an active resource, recall ≥ 0.8). Run it in
  the pipeline: `python tests/eval_recommendations.py`.

## Enterprise multi-account (review #6)

- **Read-only cross-account aggregation.** `multi_account.aggregate_all_accounts` iterates the
  enabled accounts (`discover_all_accounts`), assumes the **read-only** `HibernateAI-CrossAccount`
  role, runs **read-only discovery** in each, and merges the per-account project cards into one
  view (keys prefixed with the account id). Accounts that can't be reached are reported in an
  `errors` list rather than failing the whole view.
- **Read-only cross-account role.** The `HibernateAI-CrossAccount` role grants only EC2/RDS
  `Describe*` + CloudWatch metric reads; it trusts only the management account and requires
  `aws:PrincipalOrgID`; `AssumeRole` is scoped with `aws:ResourceOrgID`. It cannot mutate
  resources in member accounts.
- **StackSet rollout is not performed in v1.** The `deploy_stackset` route is among the
  destructive/privileged routes **disabled in v1** (returns a read-only response). Operators
  deploy the read-only cross-account role via their own CloudFormation StackSet out-of-band.
- **Config safety:** the enabled-account list is stored via the typed `config.py` accessor, so
  saving accounts no longer clobbers deny-list / grouping / model config.

## Deployment (CDK)

```bash
cdk deploy HibernateAI
```

Creates these resources (all read-only / advisory — none grant destructive capability):
- Lambda Function (Hibernate AI advisory engine)
- IAM Role + Policy (read-only) + permissions boundary (`HibernateAI-Widget-Boundary`)
- DynamoDB Table (solution's own config/rules/feedback state)
- CloudWatch Dashboard (UI)
- EventBridge Rule (scheduled analysis)
- SNS Topic (notifications)
- CDK Metadata
