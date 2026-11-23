# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the secrets management module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.secrets_manager import SecretsRetrievalError, clear_cache, get_secrets


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the module-level secrets cache before each test."""
    clear_cache()
    yield
    clear_cache()


class TestGetSecrets:
    """Tests for the get_secrets function."""

    @patch("src.secrets_manager.boto3.client")
    def test_successful_retrieval(self, mock_boto_client):
        """Secrets are fetched and returned as a dict on first call."""
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": "sk-123", "db_pass": "secret"})
        }

        result = get_secrets(
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:my-secret"
        )

        assert result == {"api_key": "sk-123", "db_pass": "secret"}
        mock_sm.get_secret_value.assert_called_once_with(
            SecretId="arn:aws:secretsmanager:us-west-2:123456789012:secret:my-secret"
        )

    @patch("src.secrets_manager.boto3.client")
    def test_caching_returns_cached_value(self, mock_boto_client):
        """Second call returns cached value without making another API call."""
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"key": "value"})
        }

        secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:test"
        first_result = get_secrets(secret_arn)
        second_result = get_secrets(secret_arn)

        assert first_result == second_result == {"key": "value"}
        # Only one API call should have been made
        assert mock_sm.get_secret_value.call_count == 1

    @patch("src.secrets_manager.time.sleep")
    @patch("src.secrets_manager.boto3.client")
    def test_retry_on_client_error(self, mock_boto_client, mock_sleep):
        """Retries on ClientError and succeeds on subsequent attempt."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm

        error_response = {
            "Error": {"Code": "InternalServiceError", "Message": "Temporary"}
        }
        mock_sm.get_secret_value.side_effect = [
            ClientError(error_response, "GetSecretValue"),
            {"SecretString": json.dumps({"recovered": "true"})},
        ]

        result = get_secrets(
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:retry-test"
        )

        assert result == {"recovered": "true"}
        assert mock_sm.get_secret_value.call_count == 2

    @patch("src.secrets_manager.time.sleep")
    @patch("src.secrets_manager.boto3.client")
    def test_raises_after_all_retries_exhausted(self, mock_boto_client, mock_sleep):
        """Raises SecretsRetrievalError after max retries are exhausted."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm

        error_response = {
            "Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}
        }
        mock_sm.get_secret_value.side_effect = ClientError(
            error_response, "GetSecretValue"
        )

        with pytest.raises(SecretsRetrievalError) as exc_info:
            get_secrets("arn:aws:secretsmanager:us-west-2:123456789012:secret:missing")

        assert "missing" in str(exc_info.value)
        assert "ClientError" in str(exc_info.value)
        # 1 initial + 2 retries = 3 total attempts
        assert mock_sm.get_secret_value.call_count == 3

    @patch("src.secrets_manager.time.sleep")
    @patch("src.secrets_manager.boto3.client")
    def test_error_does_not_expose_secret_values(self, mock_boto_client, mock_sleep):
        """Error message contains secret identifier but not secret values."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm

        error_response = {
            "Error": {"Code": "DecryptionFailure", "Message": "Cannot decrypt"}
        }
        mock_sm.get_secret_value.side_effect = ClientError(
            error_response, "GetSecretValue"
        )

        secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:api-keys"
        with pytest.raises(SecretsRetrievalError) as exc_info:
            get_secrets(secret_arn)

        error_msg = str(exc_info.value)
        assert secret_arn in error_msg
        # Should not contain any actual secret values
        assert "sk-" not in error_msg
        assert "password" not in error_msg

    @patch("src.secrets_manager.time.sleep")
    @patch("src.secrets_manager.boto3.client")
    def test_handles_invalid_json_in_secret(self, mock_boto_client, mock_sleep):
        """Raises SecretsRetrievalError when SecretString is not valid JSON."""
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {"SecretString": "not-valid-json{{"}

        with pytest.raises(SecretsRetrievalError) as exc_info:
            get_secrets("arn:aws:secretsmanager:us-west-2:123456789012:secret:bad-json")

        assert "JSONDecodeError" in str(exc_info.value)

    @patch("src.secrets_manager.time.sleep")
    @patch("src.secrets_manager.boto3.client")
    def test_custom_max_retries(self, mock_boto_client, mock_sleep):
        """Respects custom max_retries parameter."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm

        error_response = {"Error": {"Code": "InternalServiceError", "Message": "Fail"}}
        mock_sm.get_secret_value.side_effect = ClientError(
            error_response, "GetSecretValue"
        )

        with pytest.raises(SecretsRetrievalError):
            get_secrets(
                "arn:aws:secretsmanager:us-west-2:123456789012:secret:test",
                max_retries=1,
            )

        # 1 initial + 1 retry = 2 total attempts
        assert mock_sm.get_secret_value.call_count == 2


class TestSecretsRetrievalError:
    """Tests for the SecretsRetrievalError exception class."""

    def test_contains_secret_id(self):
        """Error stores and exposes the secret identifier."""
        err = SecretsRetrievalError(
            secret_id="arn:aws:secretsmanager:us-west-2:123:secret:my-secret",
            reason="ClientError",
        )
        assert err.secret_id == "arn:aws:secretsmanager:us-west-2:123:secret:my-secret"
        assert "my-secret" in str(err)

    def test_contains_reason(self):
        """Error stores and exposes the failure reason."""
        err = SecretsRetrievalError(secret_id="test-secret", reason="TimeoutError")
        assert err.reason == "TimeoutError"
        assert "TimeoutError" in str(err)

    def test_is_exception(self):
        """SecretsRetrievalError is a proper Exception subclass."""
        err = SecretsRetrievalError(secret_id="test", reason="test")
        assert isinstance(err, Exception)


class TestClearCache:
    """Tests for the clear_cache utility function."""

    @patch("src.secrets_manager.boto3.client")
    def test_clear_cache_forces_refetch(self, mock_boto_client):
        """Clearing cache causes next get_secrets call to fetch from API."""
        mock_sm = MagicMock()
        mock_boto_client.return_value = mock_sm
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"key": "v1"})
        }

        secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:test"
        get_secrets(secret_arn)

        # Update the mock to return different value
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"key": "v2"})
        }

        clear_cache()
        result = get_secrets(secret_arn)

        assert result == {"key": "v2"}
        assert mock_sm.get_secret_value.call_count == 2
