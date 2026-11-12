# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the session.py module — DynamoDB-backed conversation memory."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch


from src.session import (
    MAX_MESSAGES_PER_SESSION,
    SESSION_TTL_SECONDS,
    SessionStore,
)


class TestSessionStoreDisabled:
    """When no table_name is provided, the store is disabled and operations are no-ops."""

    def test_disabled_when_no_table_name(self):
        store = SessionStore(table_name=None)
        assert store.enabled is False

    def test_create_session_returns_uuid_when_disabled(self):
        store = SessionStore(table_name=None)
        session_id = store.create_session()
        # Must be a string UUID (36 chars: 32 hex + 4 hyphens)
        assert isinstance(session_id, str)
        assert len(session_id) == 36
        assert session_id.count("-") == 4

    def test_get_messages_returns_empty_when_disabled(self):
        store = SessionStore(table_name=None)
        assert store.get_messages("any-id") == []

    def test_save_messages_is_noop_when_disabled(self):
        store = SessionStore(table_name=None)
        # Should not raise even with valid-looking session_id
        store.save_messages("any-id", [{"role": "user", "content": "hi"}])

    def test_get_messages_returns_empty_for_empty_session_id(self):
        # Even with a real table, empty session_id short-circuits
        with patch("src.session.boto3") as mock_boto:
            mock_boto.resource.return_value.Table.return_value = MagicMock()
            store = SessionStore(table_name="some-table")
            assert store.get_messages("") == []

    def test_save_messages_is_noop_for_empty_session_id(self):
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            store = SessionStore(table_name="some-table")
            store.save_messages("", [{"role": "user"}])
            mock_table.update_item.assert_not_called()


class TestSessionStoreEnabled:
    """When a table_name is provided, the store performs DynamoDB operations."""

    def test_enabled_when_table_name_provided(self):
        with patch("src.session.boto3") as mock_boto:
            mock_boto.resource.return_value.Table.return_value = MagicMock()
            store = SessionStore(table_name="my-session-table")
            assert store.enabled is True
            assert store.table_name == "my-session-table"

    def test_init_table_connection_failure_disables_store(self):
        """If DynamoDB connection fails at init, the store falls back to disabled."""
        with patch("src.session.boto3") as mock_boto:
            mock_boto.resource.side_effect = Exception("AWS credentials missing")
            store = SessionStore(table_name="my-session-table")
            assert store.enabled is False

    def test_create_session_persists_item(self):
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            store = SessionStore(table_name="my-table")
            session_id = store.create_session()

            mock_table.put_item.assert_called_once()
            item = mock_table.put_item.call_args[1]["Item"]
            assert item["session_id"] == session_id
            assert item["messages"] == "[]"
            assert isinstance(item["created_at"], int)
            assert item["expires_at"] == item["created_at"] + SESSION_TTL_SECONDS

    def test_create_session_handles_put_failure(self):
        """put_item failure is logged but does not raise."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_table.put_item.side_effect = Exception("ProvisionedThroughputExceeded")
            mock_boto.resource.return_value.Table.return_value = mock_table

            store = SessionStore(table_name="my-table")
            session_id = store.create_session()
            # Still returns a valid UUID
            assert isinstance(session_id, str)
            assert len(session_id) == 36

    def test_get_messages_returns_parsed_messages(self):
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            messages = [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ]
            mock_table.get_item.return_value = {
                "Item": {
                    "session_id": "abc-123",
                    "messages": json.dumps(messages),
                    "expires_at": int(time.time()) + 1000,
                }
            }

            store = SessionStore(table_name="my-table")
            result = store.get_messages("abc-123")

            assert result == messages
            mock_table.get_item.assert_called_once_with(Key={"session_id": "abc-123"})

    def test_get_messages_returns_empty_when_item_missing(self):
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            mock_table.get_item.return_value = {}  # No Item key

            store = SessionStore(table_name="my-table")
            assert store.get_messages("missing-id") == []

    def test_get_messages_returns_empty_when_session_expired(self):
        """If expires_at is in the past, treat session as expired."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            mock_table.get_item.return_value = {
                "Item": {
                    "session_id": "expired-id",
                    "messages": json.dumps([{"role": "user", "content": "old"}]),
                    "expires_at": int(time.time()) - 100,  # Already expired
                }
            }

            store = SessionStore(table_name="my-table")
            assert store.get_messages("expired-id") == []

    def test_get_messages_handles_dynamodb_error(self):
        """get_item failure is caught and returns empty list."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_table.get_item.side_effect = Exception("ResourceNotFoundException")
            mock_boto.resource.return_value.Table.return_value = mock_table

            store = SessionStore(table_name="my-table")
            assert store.get_messages("some-id") == []

    def test_get_messages_defaults_to_empty_when_messages_missing(self):
        """If item has no messages field, default to empty list."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            mock_table.get_item.return_value = {
                "Item": {
                    "session_id": "abc",
                    "expires_at": int(time.time()) + 1000,
                    # no "messages" key
                }
            }

            store = SessionStore(table_name="my-table")
            assert store.get_messages("abc") == []

    def test_save_messages_writes_to_dynamodb(self):
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            messages = [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ]

            store = SessionStore(table_name="my-table")
            store.save_messages("session-1", messages)

            mock_table.update_item.assert_called_once()
            kwargs = mock_table.update_item.call_args[1]
            assert kwargs["Key"] == {"session_id": "session-1"}
            assert "SET messages = :m" in kwargs["UpdateExpression"]
            saved_messages = json.loads(kwargs["ExpressionAttributeValues"][":m"])
            assert saved_messages == messages
            assert kwargs["ExpressionAttributeValues"][":e"] >= int(time.time())

    def test_save_messages_trims_to_max(self):
        """Only the most recent MAX_MESSAGES_PER_SESSION messages are saved."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            messages = [
                {"role": "user", "content": f"msg-{i}"}
                for i in range(MAX_MESSAGES_PER_SESSION + 5)
            ]

            store = SessionStore(table_name="my-table")
            store.save_messages("session-1", messages)

            kwargs = mock_table.update_item.call_args[1]
            saved = json.loads(kwargs["ExpressionAttributeValues"][":m"])
            assert len(saved) == MAX_MESSAGES_PER_SESSION
            # First saved should be the (max+5 - max) = 5th original message
            assert saved[0]["content"] == "msg-5"
            assert saved[-1]["content"] == f"msg-{MAX_MESSAGES_PER_SESSION + 4}"

    def test_save_messages_handles_dynamodb_error(self):
        """update_item failure is caught and does not raise."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_table.update_item.side_effect = Exception("ConditionalCheckFailed")
            mock_boto.resource.return_value.Table.return_value = mock_table

            store = SessionStore(table_name="my-table")
            # Should not raise
            store.save_messages("any", [{"role": "user", "content": "test"}])

    def test_save_messages_uses_default_str_serializer(self):
        """Non-JSON-serializable types (like Decimal) are handled via default=str."""
        with patch("src.session.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            from decimal import Decimal

            messages = [{"role": "user", "content": "x", "ts": Decimal("123.45")}]

            store = SessionStore(table_name="my-table")
            # Should not raise even with Decimal in payload
            store.save_messages("abc", messages)

            kwargs = mock_table.update_item.call_args[1]
            saved_str = kwargs["ExpressionAttributeValues"][":m"]
            # Decimal serialized as string via default=str
            assert "123.45" in saved_str
