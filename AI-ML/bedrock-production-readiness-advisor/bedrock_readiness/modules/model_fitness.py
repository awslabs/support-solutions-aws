# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pillar: Model Fitness -- Are you using the right model for each job?

Not security-sensitive -- this pillar is entirely about cost- and
performance-appropriate model selection (diversity, legacy usage,
premium-model dominance, CRIS), so it runs on both tracks.

Checks: MF-01 through MF-04
"""

from bedrock_readiness.core.models import Finding, PillarResult, CheckStatus, FixType, Config
from bedrock_readiness.core.scanner import AccountScanner, ScanError

CRIS_PREFIXES = ("us.", "eu.", "apac.", "global.")

# Defaults, overridable via Config.premium_model_markers / legacy_model_markers
# (see bedrock-readiness.example.yaml) so a new model generation -- Nova's
# next tier, a future Claude/Titan release -- doesn't need a code change to
# be classified correctly. "nova-premier" is included since it is Amazon's
# current flagship Nova tier (comparable in role to Opus/Sonnet-4), confirmed
# against AWS's public Nova documentation before adding it here.
PREMIUM_MODEL_MARKERS = ("opus", "sonnet-4", "nova-premier")
LEGACY_MODEL_MARKERS = ("claude-v2", "claude-instant", "titan-text-lite", "titan-text-express")
MIN_SAMPLE_INVOCATIONS = 50

PILLAR_ID = "model_fitness"
PILLAR_NAME = "Model Fitness"


def assess(scanner: AccountScanner, scan_data: dict, config: Config) -> PillarResult:
    result = PillarResult(pillar_id=PILLAR_ID, pillar_name=PILLAR_NAME)

    premium_markers = config.premium_model_markers or PREMIUM_MODEL_MARKERS
    legacy_markers = config.legacy_model_markers or LEGACY_MODEL_MARKERS

    metrics = scanner.list_metrics("AWS/Bedrock", "Invocations")
    if isinstance(metrics, ScanError):
        result.findings.append(Finding(
            check_id="MF-01", check_name="Model Fitness Assessment",
            status=CheckStatus.ERROR, impact=2, likelihood=2,
            message=f"Could not assess model fitness: {metrics.error}",
            recommendation="Verify cloudwatch:ListMetrics and cloudwatch:GetMetricData permissions",
        ))
        return result

    model_ids = set()
    for m in metrics:
        for d in m.get("Dimensions", []):
            if d.get("Name") == "ModelId":
                model_ids.add(d["Value"])

    model_invocation_counts = {}
    total_invocations = 0
    for model_id in model_ids:
        resp = scanner.get_metric_data_sum(
            "AWS/Bedrock", "Invocations",
            dimensions=[{"Name": "ModelId", "Value": model_id}],
            period=86400, hours=168,
        )
        if isinstance(resp, ScanError):
            continue
        values = resp.get("MetricDataResults", [{}])[0].get("Values", [])
        count = int(sum(values))
        model_invocation_counts[model_id] = count
        total_invocations += count

    if total_invocations < MIN_SAMPLE_INVOCATIONS:
        result.findings.append(Finding(
            check_id="MF-01", check_name="Model Fitness Sample Size",
            status=CheckStatus.SKIPPED, impact=1, likelihood=1,
            message=(f"Insufficient data ({total_invocations} invocations in 7 days, "
                      f"need {MIN_SAMPLE_INVOCATIONS}+) -- re-run after more Bedrock usage accumulates"),
        ))
        return result

    # MF-01: Single-model dependency
    if len(model_invocation_counts) == 1:
        result.findings.append(Finding(
            check_id="MF-01", check_name="Model Diversity (Single-Model Dependency)",
            status=CheckStatus.WARN, impact=2, likelihood=3,
            message="Single model for all use cases -- no fallback if that model is deprecated or throttled",
            recommendation="Evaluate alternative models for different use cases (Haiku for classification, Sonnet for reasoning)",
            fix_type=FixType.CODE, effort_minutes=120,
        ))
    else:
        result.findings.append(Finding(
            check_id="MF-01", check_name="Model Diversity (Single-Model Dependency)",
            status=CheckStatus.PASS, impact=2, likelihood=3,
            message=f"{len(model_invocation_counts)} distinct model(s) in use over the last 7 days",
        ))

    # MF-02: Premium model dominance
    expensive_models = [m for m in model_invocation_counts
                         if any(x in m.lower() for x in premium_markers)]
    expensive_pct = 0.0
    if expensive_models and total_invocations > 0:
        expensive_pct = sum(model_invocation_counts.get(m, 0) for m in expensive_models) / total_invocations * 100
    if expensive_pct > 60:
        result.findings.append(Finding(
            check_id="MF-02", check_name="Premium Model Usage Share",
            status=CheckStatus.WARN, impact=2, likelihood=2,
            message=f"{expensive_pct:.0f}% of invocations use premium models",
            recommendation="Evaluate cheaper models for simple tasks (classification, extraction)",
            fix_type=FixType.CODE, effort_minutes=120,
        ))
    else:
        result.findings.append(Finding(
            check_id="MF-02", check_name="Premium Model Usage Share",
            status=CheckStatus.PASS, impact=2, likelihood=2,
            message=f"Premium model share ({expensive_pct:.0f}%) is within a reasonable range",
        ))

    # MF-03: Legacy model usage
    legacy_in_use = [m for m in model_ids if any(l in m.lower() for l in legacy_markers)]
    if legacy_in_use:
        result.findings.append(Finding(
            check_id="MF-03", check_name="Legacy Model Usage",
            status=CheckStatus.WARN, impact=3, likelihood=2,
            message=f"Legacy/deprecated model(s) in use: {', '.join(legacy_in_use)}",
            recommendation="Migrate to current model versions for continued support",
            resource_ids=legacy_in_use,
            fix_type=FixType.CODE, effort_minutes=180,
        ))
    else:
        result.findings.append(Finding(
            check_id="MF-03", check_name="Legacy Model Usage",
            status=CheckStatus.PASS, impact=3, likelihood=2,
            message="No legacy/deprecated model IDs detected in recent invocation metrics",
        ))

    # MF-04: CRIS-informed model fitness signal (informational)
    cris_models = [m for m in model_ids if any(m.startswith(p) for p in CRIS_PREFIXES)]
    if cris_models:
        result.findings.append(Finding(
            check_id="MF-04", check_name="Cross-Region Inference Adoption",
            status=CheckStatus.PASS, impact=1, likelihood=1,
            message=f"CRIS active on {len(cris_models)} model(s) -- good for capacity resilience",
        ))

    return result
