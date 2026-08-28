"""
Security integration tests for FIS Recommender MCP Server.
Run: python -m pytest test_security.py -v
"""
import json
import asyncio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://localhost:8000/mcp"


@pytest.fixture
def call_tool():
    """Helper to call an MCP tool and return parsed result."""
    async def _call(tool_name, arguments):
        async with streamablehttp_client(MCP_URL, {}, timeout=30, terminate_on_close=False) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return json.loads(result.content[0].text)
    return lambda tool, args: asyncio.run(_call(tool, args))


# === Input Validation Tests ===

class TestRecommendInputValidation:
    def test_rejects_oversized_input(self, call_tool):
        """Finding #3: Input size limit enforced."""
        result = call_tool("recommend_fis_experiments", {"finding": {"summary": "x" * 20000}})
        assert "error" in result
        assert "too large" in result["error"].lower()

    def test_handles_empty_finding(self, call_tool):
        """Returns empty recommendations for unrecognized findings."""
        result = call_tool("recommend_fis_experiments", {"finding": {"summary": "nothing relevant"}})
        assert result["count"] == 0
        assert result["recommendations"] == []

    def test_handles_non_string_summary(self, call_tool):
        """Finding #3: Type validation."""
        result = call_tool("recommend_fis_experiments", {"finding": {"summary": 12345}})
        assert "error" in result


class TestCreateTemplateValidation:
    def test_rejects_disallowed_action(self, call_tool):
        """Finding #3: Action allowlist enforced."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:terminate-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {"Env": "test"},
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:test-alarm",
            }
        })
        assert "error" in result
        assert "not allowed" in result["error"].lower()

    def test_rejects_excessive_duration(self, call_tool):
        """Finding #3: Duration cap enforced (max 60 min)."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT120M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {"Env": "test"},
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:test-alarm",
            }
        })
        assert "error" in result
        assert "duration" in result["error"].lower()

    def test_rejects_empty_tags(self, call_tool):
        """Finding #3: Empty tags rejected (would match all resources)."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {},
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:test-alarm",
            }
        })
        assert "error" in result
        assert "tags" in result["error"].lower()

    def test_rejects_missing_stop_condition(self, call_tool):
        """Finding #11: Stop condition required."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {"Env": "test"},
            }
        })
        assert "error" in result
        assert "stopConditionArn" in result["error"]

    def test_rejects_invalid_stop_condition_arn(self, call_tool):
        """Finding #11: stopConditionArn format validated."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {"Env": "test"},
                "stopConditionArn": "not-a-valid-arn",
            }
        })
        assert "error" in result
        assert "stopConditionArn" in result["error"]

    def test_rejects_invalid_role_arn(self, call_tool):
        """Finding #5: roleArn format validated."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "not-a-valid-arn",
                "tags": {"Env": "test"},
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:test-alarm",
            }
        })
        assert "error" in result
        assert "roleArn" in result["error"]

    def test_rejects_cross_account_role_and_alarm(self, call_tool):
        """Finding #5: Cross-account check between roleArn and stopConditionArn."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::111111111111:role/FISRole",
                "tags": {"Env": "test"},
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:222222222222:alarm:test-alarm",
            }
        })
        assert "error" in result
        assert "same AWS account" in result["error"]

    def test_rejects_selection_mode_all(self, call_tool):
        """Finding #3: ALL selection mode rejected."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {"Env": "test"},
                "selectionMode": "ALL",
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:test-alarm",
            }
        })
        assert "error" in result
        assert "selectionMode" in result["error"]


# === Error Handling Tests ===

class TestErrorHandling:
    def test_no_stack_trace_in_errors(self, call_tool):
        """Finding #13: No internal details leaked in errors."""
        result = call_tool("create_fis_template", {
            "recommendation": {},
            "target": {}
        })
        assert "error" in result
        assert "Traceback" not in json.dumps(result)
        assert "File " not in json.dumps(result)

    def test_no_aws_details_in_errors(self, call_tool):
        """Finding #13: No AWS internals leaked."""
        result = call_tool("create_fis_template", {
            "recommendation": {"action": "aws:ec2:stop-instances", "duration": "PT3M", "description": "Test"},
            "target": {
                "roleArn": "arn:aws:iam::123456789012:role/FISRole",
                "tags": {"Env": "test"},
                "stopConditionArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:test-alarm",
            }
        })
        # Even if FIS API fails, should not leak account/role details
        if "error" in result:
            assert "AccessDenied" not in result["error"]
            assert "ExpiredToken" not in result["error"]


# === Lambda Handler Tests ===

class TestLambdaHandlerSecurity:
    def test_tool_allowlist(self):
        """Finding #3: Only recommend_fis_experiments allowed via Lambda."""
        from lambda_function import lambda_handler

        result = lambda_handler({"tool": "create_fis_template", "arguments": {}}, None)
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert "not allowed" in body["error"].lower()

    def test_tool_allowlist_unknown_tool(self):
        """Finding #3: Unknown tools rejected."""
        from lambda_function import lambda_handler

        result = lambda_handler({"tool": "malicious_tool", "arguments": {}}, None)
        assert result["statusCode"] == 403

    def test_token_url_validation(self):
        """Finding #8: Invalid token URLs rejected."""
        import os
        os.environ["COGNITO_CLIENT_ID"] = "test"
        os.environ["COGNITO_CLIENT_SECRET"] = "test"
        os.environ["COGNITO_TOKEN_URL"] = "https://evil.com/steal-creds"
        os.environ["AGENT_ARN"] = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test"

        from lambda_function import get_bearer_token
        with pytest.raises(ValueError, match="Invalid token URL"):
            get_bearer_token()

    def test_secrets_manager_retrieval_path(self):
        """Finding #2: Secret fetched from Secrets Manager when ARN is set."""
        import os
        from unittest.mock import patch, MagicMock

        os.environ["COGNITO_CLIENT_ID"] = "test"
        os.environ["COGNITO_CLIENT_SECRET_ARN"] = "arn:aws:secretsmanager:us-east-1:123456789012:secret:fis-mcp/test"
        os.environ["COGNITO_TOKEN_URL"] = "https://fismcp.auth.us-east-1.amazoncognito.com/oauth2/token"
        os.environ["AGENT_ARN"] = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test"
        os.environ.pop("COGNITO_CLIENT_SECRET", None)

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "test-secret"}

        with patch("boto3.client", return_value=mock_sm):
            # Will fail at HTTP call but should reach Secrets Manager first
            try:
                from lambda_function import get_bearer_token
                get_bearer_token()
            except Exception:
                pass
            mock_sm.get_secret_value.assert_called_once_with(
                SecretId="arn:aws:secretsmanager:us-east-1:123456789012:secret:fis-mcp/test"
            )


# === Config Store Tests ===

class TestConfigStoreSecurity:
    def test_config_file_permissions(self, tmp_path):
        """Finding #1: Config file has 600 permissions."""
        import config_store
        import stat

        original = config_store.CONFIG_FILE
        config_store.CONFIG_FILE = str(tmp_path / ".fis_config.json")

        config_store.save({"test_key": "test_value"})

        file_stat = os.stat(config_store.CONFIG_FILE)
        permissions = stat.S_IMODE(file_stat.st_mode)
        assert permissions == 0o600

        config_store.CONFIG_FILE = original


import os
