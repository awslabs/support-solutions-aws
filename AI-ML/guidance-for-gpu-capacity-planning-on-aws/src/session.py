# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Session management for conversation memory.

Enables iterative capacity planning ("what about eu-west-1 instead?")
by storing conversation history in DynamoDB with a session TTL.

Sessions are lightweight — they store the last N messages and tool results
so the agent has context for follow-up questions without re-running tools.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = int(
    os.environ.get("SESSION_TTL_SECONDS", "3600")
)  # 1 hour default
MAX_MESSAGES_PER_SESSION = 20  # Keep last 20 messages to avoid context bloat


class SessionStore:
    """DynamoDB-backed session storage for conversation memory."""

    def __init__(self, table_name: str | None = None):
        """Initialize the session store.

        Args:
            table_name: DynamoDB table name. If None, sessions are disabled
                (the agent runs statelessly).
        """
        self.table_name = table_name
        self._table = None
        if table_name:
            try:
                self._table = boto3.resource("dynamodb").Table(table_name)
            except Exception:
                logger.warning("Failed to connect to session table %s", table_name)

    @property
    def enabled(self) -> bool:
        return self._table is not None

    def create_session(self) -> str:
        """Create a new session and return its ID."""
        session_id = str(uuid.uuid4())
        if not self.enabled:
            return session_id

        try:
            self._table.put_item(
                Item={
                    "session_id": session_id,
                    "messages": json.dumps([]),
                    "created_at": int(time.time()),
                    "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
                }
            )
        except Exception:
            logger.warning("Failed to create session %s", session_id, exc_info=True)

        return session_id

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Retrieve conversation history for a session.

        Returns empty list if session not found or store is disabled.
        """
        if not self.enabled or not session_id:
            return []

        try:
            resp = self._table.get_item(Key={"session_id": session_id})
            item = resp.get("Item")
            if not item:
                return []

            # Check TTL
            if time.time() > item.get("expires_at", 0):
                return []

            messages_str = item.get("messages", "[]")
            return json.loads(messages_str)
        except Exception:
            logger.warning("Failed to get session %s", session_id, exc_info=True)
            return []

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Save conversation messages to the session.

        Keeps only the last MAX_MESSAGES_PER_SESSION messages to prevent
        unbounded growth.
        """
        if not self.enabled or not session_id:
            return

        # Trim to last N messages
        trimmed = messages[-MAX_MESSAGES_PER_SESSION:]

        try:
            self._table.update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET messages = :m, expires_at = :e",
                ExpressionAttributeValues={
                    ":m": json.dumps(trimmed, default=str),
                    ":e": int(time.time()) + SESSION_TTL_SECONDS,
                },
            )
        except Exception:
            logger.warning("Failed to save session %s", session_id, exc_info=True)
