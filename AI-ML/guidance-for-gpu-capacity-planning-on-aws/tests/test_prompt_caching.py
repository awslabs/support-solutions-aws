# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Bedrock Converse prompt caching in src/agent.py.

Covers:
- Model-support allowlist (``_supports_prompt_caching``)
- TTL env-var parsing (``_parse_cache_ttl``)
- cachePoint blocks appear in Converse kwargs for supported models
- cachePoint blocks DO NOT appear for unsupported models
- cachePoint blocks DO NOT appear when caching is explicitly off
- Cache-read / cache-write token accumulation into the ``usage`` dict
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.agent import (
    CapacityDecisionAgent,
    _parse_cache_ttl,
    _supports_extended_ttl,
    _supports_prompt_caching,
)


# --------------- helpers ---------------


def _end_turn_response(
    text: str = "ok",
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": cache_read,
            "cacheWriteInputTokens": cache_write,
        },
    }


def _dummy_handler(**kwargs) -> dict:
    return {"ok": True}


def _agent(
    mock_client: MagicMock,
    model_id: str,
    cache_ttl_env: str | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> CapacityDecisionAgent:
    """Build an agent instance with a mocked boto3 client and a
    specific prompt-cache TTL env var."""
    if monkeypatch is not None:
        if cache_ttl_env is None:
            monkeypatch.delenv("BEDROCK_PROMPT_CACHE_TTL", raising=False)
        else:
            monkeypatch.setenv("BEDROCK_PROMPT_CACHE_TTL", cache_ttl_env)
    with patch("src.agent.boto3.client", return_value=mock_client):
        agent = CapacityDecisionAgent(model_id=model_id, region="us-west-2")
    agent.register_tool(
        name="dummy_tool",
        description="A dummy tool",
        input_schema={"type": "object", "properties": {}},
        handler=_dummy_handler,
    )
    return agent


# --------------- _supports_prompt_caching ---------------


class TestSupportsPromptCaching:
    @pytest.mark.parametrize(
        "model_id",
        [
            # Anthropic Claude 4.x — the current defaults.
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.anthropic.claude-opus-4-5-20250929-v1:0",
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "apac.anthropic.claude-haiku-4-5-20251001-v1:0",
            # Anthropic Claude 3.5 / 3.7 — supported.
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
            # Amazon Nova family — supported.
            "amazon.nova-pro-v1:0",
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-micro-v1:0",
        ],
    )
    def test_supported_models(self, model_id):
        assert _supports_prompt_caching(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            # Older Claude 3.0 variants not on the allowlist.
            "anthropic.claude-3-haiku-20240307-v1:0",
            "anthropic.claude-3-opus-20240229-v1:0",
            # Meta Llama — not supported for Bedrock prompt caching.
            "us.meta.llama3-1-70b-instruct-v1:0",
            # Mistral — not supported.
            "mistral.mistral-large-2402-v1:0",
            # AI21, Cohere — not supported.
            "ai21.jamba-1-5-large-v1:0",
            "cohere.command-r-plus-v1:0",
            # DeepSeek on Bedrock — not supported for prompt caching.
            "us.deepseek.r1-v1:0",
            # Nova Reel (video) — out of scope for this agent anyway,
            # but confirm the allowlist doesn't accidentally match.
            "amazon.nova-reel-v1:0",
            # Garbage inputs.
            "",
            "not-a-model-id",
        ],
    )
    def test_unsupported_models(self, model_id):
        assert _supports_prompt_caching(model_id) is False

    def test_rejects_non_string(self):
        assert _supports_prompt_caching(None) is False  # type: ignore[arg-type]
        assert _supports_prompt_caching(42) is False  # type: ignore[arg-type]


# --------------- _supports_extended_ttl ---------------


class TestSupportsExtendedTtl:
    @pytest.mark.parametrize(
        "model_id",
        [
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "apac.anthropic.claude-3-7-sonnet-20250219-v1:0",
        ],
    )
    def test_anthropic_supports_extended_ttl(self, model_id):
        assert _supports_extended_ttl(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            # Nova supports prompt caching but NOT an explicit ttl.
            "amazon.nova-pro-v1:0",
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-micro-v1:0",
            # Non-caching families.
            "us.meta.llama3-1-70b-instruct-v1:0",
            "mistral.mistral-large-2402-v1:0",
            "",
        ],
    )
    def test_non_anthropic_rejects_extended_ttl(self, model_id):
        assert _supports_extended_ttl(model_id) is False

    def test_rejects_non_string(self):
        assert _supports_extended_ttl(None) is False  # type: ignore[arg-type]
        assert _supports_extended_ttl(42) is False  # type: ignore[arg-type]


# --------------- _parse_cache_ttl ---------------


class TestParseCacheTtl:
    def test_none_defaults_to_5m(self):
        """Env var unset → default on at 5m. Caching stays enabled
        without any operator action."""
        assert _parse_cache_ttl(None) == "5m"

    @pytest.mark.parametrize("value", ["5m", "1h"])
    def test_valid_values_pass_through(self, value):
        assert _parse_cache_ttl(value) == value

    @pytest.mark.parametrize("value", ["5M", "  5m  ", "1H"])
    def test_valid_values_normalized(self, value):
        """Case and whitespace tolerance so env-var pasting mistakes
        don't disable the feature silently."""
        assert _parse_cache_ttl(value) == value.strip().lower()

    @pytest.mark.parametrize(
        "value", ["off", "OFF", "false", "no", "0", "disabled", ""]
    )
    def test_off_variants_disable_caching(self, value):
        assert _parse_cache_ttl(value) is None

    @pytest.mark.parametrize("value", ["10m", "2h", "forever", "yes"])
    def test_unknown_values_fall_back_to_5m(self, value):
        """Unknown strings must not break the Lambda — fall back to
        the safe default instead of raising."""
        assert _parse_cache_ttl(value) == "5m"


# --------------- Agent integration ---------------


class TestAgentCachePointBlocks:
    """When caching is on + model supports it, cachePoint blocks
    appear in both ``system`` and ``toolConfig.tools``."""

    def test_cachepoint_appears_for_supported_model(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="5m",
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        kwargs = mock_client.converse.call_args.kwargs
        # system list ends with a cachePoint block.
        assert kwargs["system"][-1] == {"cachePoint": {"type": "default", "ttl": "5m"}}
        # toolConfig.tools list ends with a cachePoint entry.
        assert kwargs["toolConfig"]["tools"][-1] == {
            "cachePoint": {"type": "default", "ttl": "5m"}
        }

    def test_cachepoint_respects_1h_ttl(self, monkeypatch):
        # Extended TTL (an explicit ttl) is Anthropic-only.
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            cache_ttl_env="1h",
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        kwargs = mock_client.converse.call_args.kwargs
        assert kwargs["system"][-1]["cachePoint"]["ttl"] == "1h"
        assert kwargs["toolConfig"]["tools"][-1]["cachePoint"]["ttl"] == "1h"

    def test_cachepoint_omits_ttl_for_nova(self, monkeypatch):
        """Amazon Nova supports prompt caching but NOT extended TTL, so
        the cachePoint must be emitted WITHOUT a ttl field. Sending a
        ttl raises "Extended TTL prompt caching is only supported for
        Anthropic models" and fails every request. Regression guard for
        the 2026-11 workshop blocker (workshop default is Nova Pro)."""
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.amazon.nova-pro-v1:0",
            cache_ttl_env="1h",  # requested, but must NOT reach Nova
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        kwargs = mock_client.converse.call_args.kwargs
        # cachePoint present (Nova supports caching) but with NO ttl.
        assert kwargs["system"][-1] == {"cachePoint": {"type": "default"}}
        assert kwargs["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
        # The within-conversation cache point is also ttl-free.
        last_content = kwargs["messages"][-1]["content"]
        assert last_content[-1] == {"cachePoint": {"type": "default"}}


class TestAgentCachePointSuppression:
    """When caching is off OR model is unsupported, no cachePoint
    block should be sent — Bedrock returns ValidationException for
    cachePoint on unsupported models."""

    def test_cachepoint_suppressed_for_unsupported_model(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="mistral.mistral-large-2402-v1:0",
            cache_ttl_env="5m",  # on, but model doesn't support
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        kwargs = mock_client.converse.call_args.kwargs
        for block in kwargs["system"]:
            assert "cachePoint" not in block
        for entry in kwargs["toolConfig"]["tools"]:
            assert "cachePoint" not in entry

    @pytest.mark.parametrize("off_value", ["off", "false", "", "0"])
    def test_cachepoint_suppressed_when_feature_flag_off(self, monkeypatch, off_value):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env=off_value,
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        kwargs = mock_client.converse.call_args.kwargs
        for block in kwargs["system"]:
            assert "cachePoint" not in block
        for entry in kwargs["toolConfig"]["tools"]:
            assert "cachePoint" not in entry


class TestCacheTokenAccumulation:
    """``usage`` dict surfaces cache read/write totals summed across
    every Bedrock turn in the agent loop."""

    def test_cache_tokens_recorded_from_response(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response(
            cache_read=800, cache_write=1200
        )
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="5m",
            monkeypatch=monkeypatch,
        )

        result = agent.run("hello")

        assert result["usage"]["cache_read_tokens"] == 800
        assert result["usage"]["cache_write_tokens"] == 1200

    def test_cache_tokens_default_to_zero(self, monkeypatch):
        """When Bedrock omits the cache fields entirely (older
        responses, non-caching models), zeros are returned — never
        None or a crash."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="off",  # suppresses cachePoint; still safe
            monkeypatch=monkeypatch,
        )

        result = agent.run("hello")

        assert result["usage"]["cache_read_tokens"] == 0
        assert result["usage"]["cache_write_tokens"] == 0


# --------------- Within-conversation message cache ---------------


class TestWithinConversationCaching:
    """A cachePoint is injected into the LAST message's content on every
    Converse call so iteration N+1 reads iteration N's growing
    conversation history for 10% of normal input price."""

    def _tool_use_response(
        self, tool_use_id: str = "tu-1", tool_name: str = "dummy_tool"
    ) -> dict:
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": tool_use_id,
                                "name": tool_name,
                                "input": {"q": "x"},
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 50, "outputTokens": 10},
        }

    def test_cachepoint_appended_to_last_user_message(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="5m",
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        sent_messages = mock_client.converse.call_args.kwargs["messages"]
        # Last message is the user turn with the ask. Its content ends
        # with the cachePoint block.
        last_content = sent_messages[-1]["content"]
        assert last_content[-1] == {"cachePoint": {"type": "default", "ttl": "5m"}}
        # And the text content is still there — cachePoint is appended,
        # not replacing.
        assert any("text" in block for block in last_content)

    def test_cachepoint_not_appended_when_feature_off(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="off",
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        sent_messages = mock_client.converse.call_args.kwargs["messages"]
        last_content = sent_messages[-1]["content"]
        for block in last_content:
            assert "cachePoint" not in block

    def test_cachepoint_not_appended_for_unsupported_model(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="mistral.mistral-large-2402-v1:0",
            cache_ttl_env="5m",
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        sent_messages = mock_client.converse.call_args.kwargs["messages"]
        last_content = sent_messages[-1]["content"]
        for block in last_content:
            assert "cachePoint" not in block

    def test_cachepoint_moves_forward_each_iteration(self, monkeypatch):
        """Across a multi-iteration tool loop, each Converse call gets
        a cachePoint on the current LAST message. The original messages
        list must NOT accumulate cachePoints (that would blow past
        Bedrock's 4-cachepoint-per-request limit after 2 iterations)."""
        mock_client = MagicMock()
        # Iteration 1: tool_use → one tool call → iteration 2: end_turn.
        mock_client.converse.side_effect = [
            self._tool_use_response(),
            _end_turn_response(),
        ]
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="5m",
            monkeypatch=monkeypatch,
        )

        agent.run("hello")

        # Should have been called twice (one tool_use + one end_turn).
        assert mock_client.converse.call_count == 2

        iter1_messages = mock_client.converse.call_args_list[0].kwargs["messages"]
        iter2_messages = mock_client.converse.call_args_list[1].kwargs["messages"]

        # Iter 1 sent 1 message, iter 2 sent 3 (user, assistant_tool_use,
        # user_tool_result). Last message of each ends with ONE cachePoint.
        assert len(iter1_messages) == 1
        assert len(iter2_messages) == 3
        assert iter1_messages[-1]["content"][-1] == {
            "cachePoint": {"type": "default", "ttl": "5m"}
        }
        assert iter2_messages[-1]["content"][-1] == {
            "cachePoint": {"type": "default", "ttl": "5m"}
        }

        # Count cachepoints in the ENTIRE iter 2 request (system + tools
        # + messages). Must be <= 3 so we stay under Bedrock's 4-max.
        iter2_kwargs = mock_client.converse.call_args_list[1].kwargs
        total_cps = (
            sum(1 for b in iter2_kwargs["system"] if "cachePoint" in b)
            + sum(1 for e in iter2_kwargs["toolConfig"]["tools"] if "cachePoint" in e)
            + sum(
                1
                for msg in iter2_kwargs["messages"]
                for block in msg.get("content", [])
                if "cachePoint" in block
            )
        )
        assert total_cps == 3

    def test_original_messages_list_not_mutated(self, monkeypatch):
        """The messages list the agent appends to across iterations
        must stay cachePoint-free. Only the transient copy sent to
        Converse carries the cachePoint."""
        mock_client = MagicMock()
        mock_client.converse.return_value = _end_turn_response()
        agent = _agent(
            mock_client,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cache_ttl_env="5m",
            monkeypatch=monkeypatch,
        )

        result = agent.run("hello")

        # The returned messages (which is the loop's internal list)
        # must be clean of cachePoints.
        for msg in result["messages"]:
            for block in msg.get("content", []):
                assert "cachePoint" not in block
