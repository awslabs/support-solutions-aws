# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Template-level regression guards for the SAM template.

These tests parse ``template.yaml`` as CFN YAML and assert on structural
invariants that ``sam validate --lint`` can't catch. They guard against
regressions on the critical fixes:

    C4: Bedrock Guardrail parameters + IAM statement present.
    C5: Function URL is opt-in behind ``EnableFunctionUrl`` (default off) so
        internet-exposed deployments don't accidentally ship an endpoint
        that bypasses WAF.
    C6: The stack-managed Secret uses a sentinel value the handler can
        detect (see test_secrets.py / test_lambda_handler.py).

The tests use PyYAML with a permissive multi-constructor that accepts CFN
intrinsic tags (``!If``, ``!Ref``, ``!Sub``, etc.) without evaluating them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_multi_constructor(loader, tag_suffix, node):
    """Preserve CFN intrinsic tags verbatim as ``{"Fn::<tag>": value}`` dicts
    so tests can walk them.
    """
    tag = f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {tag: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag: loader.construct_sequence(node, deep=True)}
    return {tag: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


@pytest.fixture(scope="module")
def template() -> dict:
    with TEMPLATE_PATH.open() as f:
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506 - _CfnLoader subclasses yaml.SafeLoader; parses trusted local CloudFormation templates


# ---------------------------------------------------------------------------
# Function URL opt-in
# ---------------------------------------------------------------------------


class TestFunctionUrlOptIn:
    """regression guard: Function URL default posture.

    History (2026-07-13): the Function URL was previously exposed via
    ``AWS::Serverless::Function.FunctionUrlConfig: !If [FunctionUrlEnabled,
    {AuthType, InvokeMode}, !Ref AWS::NoValue]``. That ``!If`` pattern
    tripped CloudFormation's built-in
    ``AWS::EarlyValidation::PropertyValidation`` hook on ChangeSet
    creation — with BOTH branches, in multiple regions. cfn-lint and
    ``sam validate`` stayed clean; only the actual CreateChangeSet
    surfaced the failure.

    Later the same day (F1 fix): the URL was re-added as a standalone
    ``AWS::Lambda::Url`` resource with a top-level ``Condition:
    FunctionUrlEnabled``. That form does NOT trip the EarlyValidation
    hook (verified end-to-end in a test account). The
    ``FunctionUrlConfig`` block on the SAM Function must NOT be
    reintroduced until AWS confirms the hook is fixed.
    """

    def test_enable_function_url_parameter_exists(self, template: dict):
        params = template["Parameters"]
        assert "EnableFunctionUrl" in params
        param = params["EnableFunctionUrl"]
        assert param["Default"] == "false"
        assert set(param["AllowedValues"]) == {"true", "false"}

    def test_function_url_condition_wired(self, template: dict):
        conds = template["Conditions"]
        assert "FunctionUrlEnabled" in conds
        body = conds["FunctionUrlEnabled"]
        assert body == {"Fn::Equals": [{"Fn::Ref": "EnableFunctionUrl"}, "true"]}

    def test_function_url_config_block_is_absent(self, template: dict):
        """FunctionUrlConfig block on the SAM Function must NOT return —
        it's a known EarlyValidation trigger. See class docstring."""
        fn = template["Resources"]["CapacityAgentFunction"]
        assert "FunctionUrlConfig" not in fn["Properties"], (
            "FunctionUrlConfig is a known CloudFormation early-validation "
            "trigger — see class docstring. Do not re-add without a fresh "
            "check that the underlying AWS-side hook has been fixed. Use "
            "the standalone CapacityAgentFunctionUrl (AWS::Lambda::Url) "
            "resource instead."
        )

    def test_standalone_url_resource_present(self, template: dict):
        """The Function URL must exist as a raw AWS::Lambda::Url resource
        so the workshop can service the 40–86s queries that exceed API
        Gateway's 29s hard timeout."""
        resources = template["Resources"]
        assert "CapacityAgentFunctionUrl" in resources
        url = resources["CapacityAgentFunctionUrl"]
        assert url["Type"] == "AWS::Lambda::Url"
        # Must be conditional so ``EnableFunctionUrl=false`` deploys don't
        # ship an unfronted URL by accident.
        assert url.get("Condition") == "FunctionUrlEnabled"
        assert url["Properties"]["AuthType"] == "AWS_IAM", (
            "AuthType must be AWS_IAM. NONE would expose an unauthenticated "
            "endpoint that WAF cannot front."
        )
        assert url["Properties"]["InvokeMode"] == "BUFFERED", (
            "Python Lambda runtime doesn't support RESPONSE_STREAM without "
            "the awslambdaric streaming pattern the handler doesn't "
            "implement. Ship BUFFERED."
        )

    def test_url_permission_present(self, template: dict):
        """AWS::Lambda::Url doesn't grant its own invoke permission.
        Without a companion AWS::Lambda::Permission with
        FunctionUrlAuthType=AWS_IAM, every call returns 403."""
        resources = template["Resources"]
        assert "CapacityAgentFunctionUrlPermission" in resources
        perm = resources["CapacityAgentFunctionUrlPermission"]
        assert perm["Type"] == "AWS::Lambda::Permission"
        assert perm.get("Condition") == "FunctionUrlEnabled"
        props = perm["Properties"]
        assert props["Action"] == "lambda:InvokeFunctionUrl"
        assert props["FunctionUrlAuthType"] == "AWS_IAM"

    def test_url_permission_principal_scoped_to_account(self, template: dict):
        """regression guard: the Function URL invoke Principal must NOT
        be the wildcard ``"*"``. Even with FunctionUrlAuthType=AWS_IAM
        (which requires SigV4), automated security scanners flag a
        wildcard Principal as world-accessible, and automated
        remediation may remove the statement — leaving the URL in place
        but returning 403. See the inline comment on the resource in
        template.yaml."""
        perm = template["Resources"]["CapacityAgentFunctionUrlPermission"]
        principal = perm["Properties"]["Principal"]
        # Accept either a bare {"Ref": "AWS::AccountId"} or an
        # {"Fn::Sub": "..."} that references the account ID. Reject
        # any string literal wildcard or "*"-in-Principal shapes.
        if isinstance(principal, dict):
            ref_target = principal.get("Ref") or principal.get("Fn::Ref")
            sub_target = principal.get("Fn::Sub")
            assert ref_target == "AWS::AccountId" or (
                isinstance(sub_target, str) and "AWS::AccountId" in sub_target
            ), (
                f"Principal must resolve to the deploying account, got {principal!r}. "
                "See test docstring."
            )
        else:
            # If a string literal ever slips in, reject any wildcard shape
            assert principal != "*", (
                'Principal="*" is flagged by security scanners as world-accessible '
                "regardless of FunctionUrlAuthType. Use "
                "``Principal: !Ref AWS::AccountId`` instead."
            )

    def test_function_url_output_present(self, template: dict):
        outputs = template["Outputs"]
        assert "FunctionUrl" in outputs
        assert outputs["FunctionUrl"].get("Condition") == "FunctionUrlEnabled"


# ---------------------------------------------------------------------------
# Bedrock Guardrails wiring
# ---------------------------------------------------------------------------


class TestBedrockGuardrailWiring:
    """regression guard: guardrail parameter + IAM branch + env-var wiring
    must all be present so ``BEDROCK_GUARDRAIL_ID`` on the Lambda propagates
    to the agent.
    """

    def test_guardrail_parameters_exist(self, template: dict):
        params = template["Parameters"]
        assert "BedrockGuardrailId" in params
        assert "BedrockGuardrailVersion" in params
        # Version must default to something safe; DRAFT is the Bedrock
        # convention for pre-publish testing.
        assert params["BedrockGuardrailVersion"]["Default"] == "DRAFT"

    def test_lambda_env_var_wiring(self, template: dict):
        env = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Environment"
        ]["Variables"]
        assert env["BEDROCK_GUARDRAIL_ID"] == {"Fn::Ref": "BedrockGuardrailId"}
        assert env["BEDROCK_GUARDRAIL_VERSION"] == {
            "Fn::Ref": "BedrockGuardrailVersion"
        }

    def test_guardrail_iam_statement_conditional(self, template: dict):
        policies = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Policies"
        ]
        # Policies is a list; first entry is a Statement dict.
        statements = policies[0]["Statement"]
        # Look for the guardrail statement wrapped in Fn::If.
        found = False
        for stmt in statements:
            if isinstance(stmt, dict) and "Fn::If" in stmt:
                branch = stmt["Fn::If"]
                if branch[0] == "HasGuardrail":
                    inner = branch[1]
                    if inner.get("Sid") == "BedrockGuardrailApply":
                        assert "bedrock:ApplyGuardrail" in inner["Action"]
                        found = True
                        break
        assert found, "Missing HasGuardrail Fn::If IAM statement"


# ---------------------------------------------------------------------------
# Existing hardening still present
# ---------------------------------------------------------------------------


class TestExistingHardening:
    """Guard existing hardening from disappearing during refactors."""

    def test_waf_associated_with_api_gateway_stage(self, template: dict):
        assoc = template["Resources"]["WafWebACLAssociation"]["Properties"]
        assert "restapis" in assoc["ResourceArn"]["Fn::Sub"]

    def test_reserved_concurrency_wired(self, template: dict):
        fn = template["Resources"]["CapacityAgentFunction"]["Properties"]
        assert fn["ReservedConcurrentExecutions"] == {"Fn::Ref": "ReservedConcurrency"}

    def test_x_ray_tracing_active(self, template: dict):
        fn = template["Resources"]["CapacityAgentFunction"]["Properties"]
        assert fn["Tracing"] == "Active"


# ---------------------------------------------------------------------------
# Bedrock IAM region-pinning
# ---------------------------------------------------------------------------


def _find_bedrock_iam_statements(template: dict) -> dict[str, dict]:
    """Return {Sid: statement} for every Bedrock-related IAM statement in
    the function's inline policy, including ones wrapped in Fn::If.
    """
    policies = template["Resources"]["CapacityAgentFunction"]["Properties"]["Policies"]
    statements = policies[0]["Statement"]
    out: dict[str, dict] = {}
    for stmt in statements:
        if isinstance(stmt, dict) and "Fn::If" in stmt:
            inner = stmt["Fn::If"][1]
            if isinstance(inner, dict) and "Sid" in inner:
                out[inner["Sid"]] = inner
        elif isinstance(stmt, dict) and "Sid" in stmt:
            out[stmt["Sid"]] = stmt
    return out


class TestBedrockInvokeIamScope:
    """regression guard: the default IAM scope for bedrock:InvokeModel
    must be region-pinned, not the historical wildcard.
    """

    def test_bedrock_invoke_scope_parameter_exists(self, template: dict):
        params = template["Parameters"]
        assert "BedrockInvokeScope" in params
        assert params["BedrockInvokeScope"]["Default"] == "RegionPinned"
        assert set(params["BedrockInvokeScope"]["AllowedValues"]) == {
            "RegionPinned",
            "Broad",
        }

    def test_region_pinned_statement_present(self, template: dict):
        stmts = _find_bedrock_iam_statements(template)
        assert "BedrockInvokeRegionPinned" in stmts
        stmt = stmts["BedrockInvokeRegionPinned"]
        assert stmt["Action"] == ["bedrock:InvokeModel"]

        # Every resource must be either the parameterized BedrockRegion or a
        # specific us-*/eu-*/ap-* region — no unbounded wildcards allowed.
        resources = stmt["Resource"]
        for res in resources:
            if isinstance(res, dict) and "Fn::Sub" in res:
                sub = res["Fn::Sub"]
                assert "${BedrockRegion}" in sub, (
                    f"RegionPinned resource must reference BedrockRegion, got {sub!r}"
                )
                assert ":bedrock:*:" not in sub, (
                    f"RegionPinned must not use wildcard region, got {sub!r}"
                )
            elif isinstance(res, str):
                # Static ARNs (cross-region inference profile targets) must
                # be region-specific — never a wildcard.
                assert ":bedrock:*:" not in res, (
                    f"RegionPinned static ARN must not use wildcard region, got {res!r}"
                )
                # Must reference a specific region between "arn:aws:bedrock:" and "::".
                parts = res.split(":")
                # arn:aws:bedrock:<region>::foundation-model/*
                # index 3 is the region
                assert parts[3] and parts[3] != "*", (
                    f"RegionPinned static ARN must specify a real region, got {res!r}"
                )

    def test_broad_statement_only_fires_when_scope_is_broad(self, template: dict):
        """The broad wildcard must be gated behind an Fn::If so it only
        applies when BedrockInvokeScope=Broad (opt-in).
        """
        policies = template["Resources"]["CapacityAgentFunction"]["Properties"][
            "Policies"
        ]
        statements = policies[0]["Statement"]

        broad_found_ungated = False
        broad_found_gated = False
        for stmt in statements:
            if not isinstance(stmt, dict):
                continue
            if "Fn::If" in stmt:
                # Check both branches
                branches = stmt["Fn::If"]
                # branches[1] is when true, branches[2] is when false
                for branch in (branches[1], branches[2]):
                    if (
                        isinstance(branch, dict)
                        and branch.get("Sid") == "BedrockInvokeBroad"
                    ):
                        # This is the gated broad — good
                        broad_found_gated = True
            elif stmt.get("Sid") == "BedrockInvokeBroad":
                # Broad exists at the top level (ungated) — bad
                broad_found_ungated = True

        assert not broad_found_ungated, (
            "BedrockInvokeBroad must not be an unconditional top-level statement"
        )
        assert broad_found_gated, (
            "BedrockInvokeBroad must exist as the false-branch of the "
            "BedrockInvokeRegionPinned Fn::If (so 'Broad' scope opt-in works)"
        )

    def test_region_pinned_covers_us_eu_apac_inference_profiles(self, template: dict):
        """US / EU / APAC cross-region inference profiles route to
        foundation models in multiple regions. RegionPinned must grant
        access to at least the main-region foundation-model ARN in each
        of the three groups so a profile-based BedrockModelId works.
        """
        stmts = _find_bedrock_iam_statements(template)
        pinned = stmts["BedrockInvokeRegionPinned"]

        # Collect all static string resources.
        static_resources = [r for r in pinned["Resource"] if isinstance(r, str)]

        # Assert we cover the well-known cross-region-profile targets.
        expected_regions = {
            "us-east-1",
            "us-east-2",
            "us-west-2",  # us.*
            "eu-west-1",
            "eu-central-1",
            "eu-west-3",  # eu.*
            "ap-northeast-1",
            "ap-southeast-1",
            "ap-southeast-2",  # apac.*
        }
        found_regions = set()
        for arn in static_resources:
            # arn:aws:bedrock:<region>::foundation-model/*
            parts = arn.split(":")
            if len(parts) >= 4 and parts[2] == "bedrock":
                found_regions.add(parts[3])
        missing = expected_regions - found_regions
        assert not missing, (
            f"RegionPinned Bedrock IAM missing foundation-model access in "
            f"regions {missing} — US/EU/APAC cross-region inference profiles "
            f"require every routing target to be granted."
        )


# ---------------------------------------------------------------------------
# Bedrock spend metric filters and alarm
# ---------------------------------------------------------------------------


class TestBedrockSpendObservability:
    """regression guard: metric filters extract usage_tracking log
    fields into CloudWatch metrics, and BedrockSpendAlarm fires when the
    total-tokens sum exceeds the threshold.
    """

    def test_token_alarm_threshold_parameter_exists(self, template: dict):
        params = template["Parameters"]
        assert "BedrockTokenAlarmThreshold" in params
        p = params["BedrockTokenAlarmThreshold"]
        assert p["Type"] == "Number"
        assert p["Default"] >= 10000, (
            f"Default {p['Default']} is too low — the metric filter can "
            f"generate spurious low-token samples during idle periods."
        )
        assert p.get("MinValue", 0) >= 10000

    def test_metric_filters_exist_for_all_token_fields(self, template: dict):
        """We emit input_tokens, output_tokens, total_tokens, and the
        two prompt-cache token fields so operators can plot each
        independently.
        """
        resources = template["Resources"]
        for name in (
            "BedrockTokenUsageMetricFilter",
            "BedrockInputTokensMetricFilter",
            "BedrockOutputTokensMetricFilter",
            "BedrockCacheReadTokensMetricFilter",
            "BedrockCacheWriteTokensMetricFilter",
        ):
            assert name in resources, f"Missing metric filter: {name}"
            r = resources[name]
            assert r["Type"] == "AWS::Logs::MetricFilter"
            # Filter pattern targets only usage_tracking events (not the
            # broader agent_invocation events).
            assert '"usage_tracking"' in r["Properties"]["FilterPattern"]
            # Metric namespace is stack-scoped to avoid collisions.
            t = r["Properties"]["MetricTransformations"][0]
            assert "/Bedrock" in t["MetricNamespace"]["Fn::Sub"]
            # DefaultValue: 0 ensures no-data periods count as zero, not
            # missing (which would prevent the alarm from ever firing
            # or the dashboard's cache-hit-rate math expression from
            # evaluating cleanly in idle windows).
            assert t["DefaultValue"] == 0

    def test_metric_value_fields_extract_correct_json_keys(self, template: dict):
        resources = template["Resources"]
        expected = {
            "BedrockTokenUsageMetricFilter": "$.total_tokens",
            "BedrockInputTokensMetricFilter": "$.input_tokens",
            "BedrockOutputTokensMetricFilter": "$.output_tokens",
            "BedrockCacheReadTokensMetricFilter": "$.cache_read_tokens",
            "BedrockCacheWriteTokensMetricFilter": "$.cache_write_tokens",
        }
        for name, path in expected.items():
            t = resources[name]["Properties"]["MetricTransformations"][0]
            assert t["MetricValue"] == path, (
                f"{name} MetricValue must extract {path!r}, got {t['MetricValue']!r}"
            )

    def test_spend_alarm_wired_to_total_tokens(self, template: dict):
        alarm = template["Resources"]["BedrockSpendAlarm"]
        assert alarm["Type"] == "AWS::CloudWatch::Alarm"
        p = alarm["Properties"]
        assert p["MetricName"] == "TotalTokens"
        # Namespace matches the metric filter's namespace.
        assert "/Bedrock" in p["Namespace"]["Fn::Sub"]
        assert p["Statistic"] == "Sum"
        # 5-minute window matches the metric filter's default resolution and
        # avoids paging on single-invocation spikes.
        assert p["Period"] == 300
        assert p["Threshold"] == {"Fn::Ref": "BedrockTokenAlarmThreshold"}
        assert p["ComparisonOperator"] == "GreaterThanThreshold"
        # Must notify SNS on both alarm and OK transitions so operators
        # know when spend drops back below threshold. The value routes
        # to either the caller-supplied ARN (production) or the stack's
        # own AgentAlarmTopic (workshop / sandbox default) — introduced
        # as part of the Option E redesign on 2026-07-15 after Workshop
        # Studio's lack of cross-stack output resolution surfaced.
        expected_topic_ref = [
            {
                "Fn::If": [
                    "HasExternalAlarmTopic",
                    {"Fn::Ref": "AlarmNotificationTopicArn"},
                    {"Fn::Ref": "AgentAlarmTopic"},
                ]
            }
        ]
        assert p["AlarmActions"] == expected_topic_ref
        assert p["OKActions"] == expected_topic_ref

    def test_spend_alarm_depends_on_metric_filter(self, template: dict):
        """DependsOn ensures the metric filter exists before the alarm
        tries to reference its metric namespace on stack create.
        """
        alarm = template["Resources"]["BedrockSpendAlarm"]
        depends = alarm.get("DependsOn")
        # DependsOn may be a string or list.
        if isinstance(depends, str):
            depends = [depends]
        assert depends and "BedrockTokenUsageMetricFilter" in depends


# ---------------------------------------------------------------------------
# log retention parameterised
# ---------------------------------------------------------------------------


class TestLogRetentionParameter:
    def test_log_retention_parameter_exists_with_safe_default(self, template: dict):
        params = template["Parameters"]
        assert "LogRetentionDays" in params
        p = params["LogRetentionDays"]
        assert p["Type"] == "Number"
        # 90-day default balances audit needs vs storage cost. Anything
        # below 30 is a red flag for regulated deploys.
        assert p["Default"] >= 30
        # AllowedValues MUST match the values CloudWatch Logs accepts.
        expected_values = {
            1,
            3,
            5,
            7,
            14,
            30,
            60,
            90,
            120,
            150,
            180,
            365,
            400,
            545,
            731,
            1096,
            1827,
            2192,
            2557,
            2922,
            3288,
            3653,
        }
        assert set(p["AllowedValues"]) == expected_values

    def test_log_group_uses_parameterised_retention(self, template: dict):
        lg = template["Resources"]["CapacityAgentLogGroup"]["Properties"]
        assert lg["RetentionInDays"] == {"Fn::Ref": "LogRetentionDays"}


# ---------------------------------------------------------------------------
# ReservedConcurrency default + bounds
# ---------------------------------------------------------------------------


class TestReservedConcurrencyBounds:
    def test_reserved_concurrency_default_is_documented_and_bounded(
        self, template: dict
    ):
        params = template["Parameters"]
        p = params["ReservedConcurrency"]
        # Default was raised from 10 → 50 in the review. Guard against silent
        # revert; raising further is a valid deploy-time choice but the
        # DEFAULT should hold ~30 concurrent long-running queries.
        assert p["Default"] >= 30, (
            f"ReservedConcurrency default {p['Default']} is too low; "
            f"raise to at least 30 to accommodate legitimate multi-tenant burst."
        )
        # MinValue and MaxValue bound the safe range.
        assert p.get("MinValue") == 1
        assert p.get("MaxValue") == 900


# ---------------------------------------------------------------------------
# split p99 alarms by endpoint
# ---------------------------------------------------------------------------


class TestP99LatencyAlarmsSplit:
    """regression guard: separate alarms for API Gateway (29s cap)
    and Function URL (180s cap). A single AWS/Lambda Duration alarm at
    30s would fire on every legitimate Function URL query.
    """

    def test_old_single_alarm_removed(self, template: dict):
        # The old P99LatencyAlarm was a single Duration alarm at 30s that
        # didn't distinguish endpoints. It must be replaced by two
        # endpoint-dimensioned alarms.
        assert "P99LatencyAlarm" not in template["Resources"], (
            "Old single-alarm P99LatencyAlarm still present; the review "
            "requires it to be replaced by ApiGatewayP99LatencyAlarm and "
            "FunctionUrlP99LatencyAlarm (endpoint-dimensioned)."
        )

    def test_api_gateway_p99_alarm_fires_below_api_gateway_hard_cap(
        self, template: dict
    ):
        r = template["Resources"]["ApiGatewayP99LatencyAlarm"]
        p = r["Properties"]
        # Threshold in ms — must be BELOW 29000 (API Gateway hard cap) so
        # the alarm fires before clients see 504s. 25s is a good buffer.
        assert p["Threshold"] < 29000
        assert p["Threshold"] >= 15000
        # Reads the endpoint-dimensioned EMF metric, not AWS/Lambda.
        assert p["Namespace"] == "CapacityAgent/Invocation"
        assert p["MetricName"] == "InvocationDuration"
        dims = {d["Name"]: d["Value"] for d in p["Dimensions"]}
        assert dims == {"Endpoint": "ApiGateway"}
        assert p["ExtendedStatistic"] == "p99"

    def test_function_url_p99_alarm_gated_and_bounded_below_hard_cap(
        self, template: dict
    ):
        r = template["Resources"]["FunctionUrlP99LatencyAlarm"]
        # Must only exist when the Function URL is enabled — otherwise
        # CloudWatch would report InsufficientData forever.
        assert r.get("Condition") == "FunctionUrlEnabled"

        p = r["Properties"]
        # Threshold must be BELOW the 180000ms (180s) Function URL timeout
        # so the alarm gives operators warning before clients time out.
        assert p["Threshold"] < 180000
        assert p["Threshold"] >= 120000
        assert p["Namespace"] == "CapacityAgent/Invocation"
        dims = {d["Name"]: d["Value"] for d in p["Dimensions"]}
        assert dims == {"Endpoint": "FunctionUrl"}
        assert p["ExtendedStatistic"] == "p99"


class TestIceMetricsIam:
    """Regression guard for the CloudWatch grant that backs
    ``get_ice_metrics``. Without ``cloudwatch:GetMetricStatistics`` the
    tool always returns ``capacity_signal='unknown'`` and the agent
    silently loses its primary ICE-history source.
    """

    def test_cloudwatch_get_metric_statistics_present(self, template: dict):
        stmts = _find_bedrock_iam_statements(template)
        assert "CloudWatchMetricsRead" in stmts, (
            "template.yaml is missing the CloudWatchMetricsRead IAM statement"
        )
        stmt = stmts["CloudWatchMetricsRead"]
        assert stmt["Effect"] == "Allow"
        actions = stmt["Action"]
        if isinstance(actions, str):
            actions = [actions]
        assert "cloudwatch:GetMetricStatistics" in actions
        assert stmt["Resource"] == "*"

    def test_grant_is_read_only(self, template: dict):
        """Only the Get* form is granted — never Put/Delete/List, which
        would allow writing or enumerating metrics beyond the query scope."""
        stmts = _find_bedrock_iam_statements(template)
        stmt = stmts["CloudWatchMetricsRead"]
        actions = stmt["Action"]
        if isinstance(actions, str):
            actions = [actions]
        for a in actions:
            assert a.startswith("cloudwatch:Get"), (
                f"CloudWatchMetricsRead granted a non-read action: {a}"
            )


# ---------------------------------------------------------------------------
# ApiDeploymentRefresher Custom Resource — guards the fix for the API Gateway
# propagation race on fresh API_KEY-auth deploys.
# ---------------------------------------------------------------------------


class TestApiDeploymentRefresher:
    """A tiny Custom Resource Lambda forces a fresh
    ``POST /restapis/{id}/deployments`` once the ``ApiUsagePlanKey``
    binding is in place. Without it, fresh SAM deploys with
    ``AuthMethod=API_KEY`` 403 at the edge for several minutes.

    These tests pin the resource shape so a future refactor can't
    silently weaken the IAM scope or drop the ``DependsOn`` that makes
    the refresh land AFTER the binding.
    """

    _RESOURCE_IDS = (
        "ApiDeploymentRefresherLogGroup",
        "ApiDeploymentRefresherRole",
        "ApiDeploymentRefresherFunction",
        "ApiDeploymentRefresher",
    )

    def test_all_four_resources_exist(self, template: dict):
        resources = template["Resources"]
        for logical_id in self._RESOURCE_IDS:
            assert logical_id in resources, (
                f"ApiDeploymentRefresher component {logical_id!r} missing"
            )

    def test_all_gated_on_is_api_key_auth(self, template: dict):
        """Only the API_KEY auth path has the propagation race — IAM
        auth doesn't go through the usage-plan-key edge cache. Every
        component resource MUST be conditional on IsAPIKeyAuth so IAM
        deploys don't pay for an unused Lambda or an unused log group.
        """
        resources = template["Resources"]
        for logical_id in self._RESOURCE_IDS:
            got = resources[logical_id].get("Condition")
            assert got == "IsAPIKeyAuth", (
                f"{logical_id} must be gated on IsAPIKeyAuth (got {got!r})"
            )

    def test_custom_resource_depends_on_usage_plan_key(self, template: dict):
        """The custom resource MUST fire AFTER ApiUsagePlanKey — that's
        the whole point. Without this DependsOn the refresh can race the
        binding and the fix stops working.
        """
        cr = template["Resources"]["ApiDeploymentRefresher"]
        depends_on = cr.get("DependsOn", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        assert "ApiUsagePlanKey" in depends_on, (
            f"ApiDeploymentRefresher must depend on ApiUsagePlanKey "
            f"(got DependsOn={depends_on!r})"
        )

    def test_iam_scope_restricted_to_this_stacks_api(self, template: dict):
        """The execution role grants ONLY ``apigateway:POST`` on the
        single REST API deployments ARN for this stack. No wildcards,
        no other control-plane actions.
        """
        role = template["Resources"]["ApiDeploymentRefresherRole"]
        policies = role["Properties"]["Policies"]
        assert len(policies) == 1, "exactly one inline policy expected"
        statements = policies[0]["PolicyDocument"]["Statement"]

        # Find the API Gateway statement.
        apig_stmts = [
            s for s in statements if s.get("Sid") == "CreateDeploymentOnThisApiOnly"
        ]
        assert len(apig_stmts) == 1, (
            f"exactly one 'CreateDeploymentOnThisApiOnly' statement expected, "
            f"got {len(apig_stmts)}"
        )
        apig = apig_stmts[0]
        assert apig["Effect"] == "Allow"
        assert apig["Action"] == "apigateway:POST", (
            f"IAM action should be exactly 'apigateway:POST' (got {apig['Action']!r})"
        )
        # Resource is !Sub which we preserve as {"Fn::Sub": "..."}.
        resource = apig["Resource"]
        assert "Fn::Sub" in resource, "Resource should be a !Sub template"
        resource_tpl = resource["Fn::Sub"]
        # Must reference the specific REST API, not wildcard.
        assert "/restapis/${CapacityAgentApi}/deployments" in resource_tpl, (
            f"IAM resource must scope to ${{CapacityAgentApi}} deployments, "
            f"got {resource_tpl!r}"
        )
        assert "*" not in resource_tpl, (
            f"IAM resource must NOT contain wildcards (got {resource_tpl!r})"
        )

    def test_iam_logs_scoped_to_own_log_group(self, template: dict):
        """Logs permissions MUST point at the function's own log group,
        not at ``*``.
        """
        role = template["Resources"]["ApiDeploymentRefresherRole"]
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]

        logs_stmts = [s for s in statements if s.get("Sid") == "WriteOwnLogsOnly"]
        assert len(logs_stmts) == 1
        logs = logs_stmts[0]
        assert logs["Effect"] == "Allow"
        assert set(logs["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}, (
            f"Logs actions must be exactly CreateLogStream + PutLogEvents "
            f"(got {logs['Action']!r})"
        )
        resource = logs["Resource"]
        assert "Fn::Sub" in resource
        resource_tpl = resource["Fn::Sub"]
        assert "ApiDeploymentRefresher" in resource_tpl, (
            f"Logs resource must name the function's own log group, "
            f"got {resource_tpl!r}"
        )
        # No logs:CreateLogGroup — we create the group explicitly, so the
        # Lambda role doesn't need (and shouldn't have) that permission.
        for stmt in statements:
            action = stmt.get("Action")
            if isinstance(action, str):
                action = [action]
            assert "logs:*" not in action, "wildcard logs action is forbidden"
            assert "logs:CreateLogGroup" not in action, (
                "the log group is created explicitly in the template; the "
                "Lambda role should not have logs:CreateLogGroup"
            )

    def test_role_has_exactly_two_statements(self, template: dict):
        """Pin the statement count so a future addition is a conscious
        decision reviewed in a diff, not an incremental slide to broader
        privileges.
        """
        role = template["Resources"]["ApiDeploymentRefresherRole"]
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        assert len(statements) == 2, (
            f"expected exactly 2 statements (apigateway + logs), got {len(statements)}"
        )

    def test_function_has_no_reserved_concurrency(self, template: dict):
        """The custom resource MUST NOT set ReservedConcurrentExecutions.

        Reasoning: AWS Lambda enforces a minimum-unreserved-concurrency
        of 100 per account. Vended sandbox accounts commonly have
        ~110 units of regional concurrency, 10 of which the agent
        Lambda already reserves (via the stack's ``ReservedConcurrency``
        parameter default). Adding even ``ReservedConcurrentExecutions: 1``
        on the refresher drops unreserved below the 100 floor and
        ``sam deploy`` fails with InvalidRequest. Observed in a
        Workshop Studio vended sandbox on 2026-10-21.

        We don't need the protection anyway: CloudFormation serializes
        Custom Resource invocations across a single stack lifecycle, so
        there's no concurrent-invocation storm risk this reservation
        would protect against. Defensive-overkill-without-upside should
        stay off.
        """
        fn = template["Resources"]["ApiDeploymentRefresherFunction"]
        rc = fn["Properties"].get("ReservedConcurrentExecutions")
        assert rc is None, (
            f"ApiDeploymentRefresherFunction must NOT set "
            f"ReservedConcurrentExecutions (got {rc!r}). See docstring "
            f"for why — if a future refactor adds it back, deploys to "
            f"concurrency-constrained accounts (Workshop Studio sandbox) "
            f"will fail at CFN time."
        )

    def test_function_timeout_accommodates_propagation_wait(self, template: dict):
        """The custom resource SLEEPS inside the Lambda to let API
        Gateway's edge cache pick up the fresh deployment before
        signaling SUCCESS to CloudFormation. The Timeout must be
        high enough to cover that sleep plus margin for the
        ``apigateway:CreateDeployment`` call and the CFN response PUT.

        Observed on 2026-10-21: propagation needs 60-120 seconds. We
        sleep 90s. Timeout should be at least 120s to leave margin;
        180s is what we ship.
        """
        fn = template["Resources"]["ApiDeploymentRefresherFunction"]
        timeout = fn["Properties"].get("Timeout")
        assert timeout is not None, (
            "ApiDeploymentRefresher must set an explicit Timeout"
        )
        assert timeout >= 120, (
            f"ApiDeploymentRefresher Timeout must be >= 120s to cover the "
            f"propagation wait + API call margin (got {timeout}s). If this "
            f"test fails after a refactor, read the module docstring in the "
            f"Code.ZipFile block — the sleep is load-bearing."
        )

    def test_function_code_includes_propagation_sleep(self, template: dict):
        """Pin the critical ``time.sleep(propagation_wait_seconds)`` in
        the inline Lambda code. Without it, requests 403 for 1-2 min
        after ``CREATE_COMPLETE``. A future refactor must either keep
        the sleep or replace it with an explicit propagation-check
        polling loop — but the behavior (don't signal SUCCESS until the
        stage actually serves) must survive.
        """
        fn = template["Resources"]["ApiDeploymentRefresherFunction"]
        code = fn["Properties"]["Code"]["ZipFile"]
        assert "time.sleep(propagation_wait_seconds)" in code, (
            "ApiDeploymentRefresher Lambda MUST wait for API Gateway edge-cache "
            "propagation before returning SUCCESS to CloudFormation. See the "
            "module docstring in the Code.ZipFile block for why — removing "
            "this sleep re-introduces the transient 403 that the Custom "
            "Resource exists to prevent."
        )

    def test_function_runtime_matches_agent_lambda(self, template: dict):
        """Keeping the runtime aligned with the agent Lambda means the
        operator only needs to track one Python version across the
        stack when the Lambda runtime EOLs. The agent Lambda inherits
        its Runtime from ``Globals.Function.Runtime``; the refresher
        sets it explicitly because ``AWS::Lambda::Function`` doesn't
        inherit from SAM Globals (only ``AWS::Serverless::Function``
        does).
        """
        agent_runtime = template["Globals"]["Function"]["Runtime"]
        refresher_runtime = template["Resources"]["ApiDeploymentRefresherFunction"][
            "Properties"
        ]["Runtime"]
        assert refresher_runtime == agent_runtime, (
            f"ApiDeploymentRefresher runtime ({refresher_runtime!r}) must match "
            f"agent Lambda ({agent_runtime!r}) so the operator tracks one Python "
            f"version at EOL time"
        )
