# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
CapacityDecisionAgent — Bedrock Converse + tool-use loop.

Flow:
1. User sends a natural-language ask
2. Agent → Bedrock with toolConfig
3. If model returns toolUse → execute tools → append results → loop
4. If model returns end_turn → return final text
5. Loop with max_iterations bound

Cite-source rule is enforced by the system prompt; this loop is provider-neutral.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

import boto3

try:
    from config import AWS_REGION, BEDROCK_MODEL_ID, SYSTEM_PROMPT
except ImportError:
    from src.config import AWS_REGION, BEDROCK_MODEL_ID, SYSTEM_PROMPT

try:
    from retry_handler import with_retry, _is_retryable
    from tool_cache import ToolCache
    from metrics import ToolMetricsEmitter
    from signal_extractor import assess_targets
except ImportError:
    from src.retry_handler import with_retry, _is_retryable
    from src.tool_cache import ToolCache
    from src.metrics import ToolMetricsEmitter
    from src.signal_extractor import assess_targets


# ---------------- Prompt caching helpers ----------------

# Bedrock Converse prompt caching is supported on a subset of models.
# Sending a ``cachePoint`` block to a model that doesn't support it
# produces a ValidationException from the service, which would break
# every request. The allowlist below is prefix-matched after stripping
# the cross-region inference prefix (``us.``, ``eu.``, ``apac.``,
# ``us-gov.``). It is tracked by vendor + model family rather than
# specific point-release IDs so new patches don't need a code change.
#
# Supported families (as of Nov 2026 Bedrock prompt caching GA):
#   - Anthropic Claude 3.5 Haiku, 3.5 Sonnet, 3.7 Sonnet
#   - Anthropic Claude 4.x (Haiku 4.5, Sonnet 4.5, Opus 4.5)
#   - Amazon Nova (Pro, Lite, Micro)
#
# NOT supported (as of the same point in time): Meta Llama, Mistral,
# AI21, Cohere, DeepSeek. If the operator deploys a model outside this
# list, the agent will skip the cachePoint block automatically and
# behave as it did before caching landed.
_CACHE_SUPPORTED_MODEL_PREFIXES: tuple[str, ...] = (
    "anthropic.claude-3-5-haiku-",
    "anthropic.claude-3-5-sonnet-",
    "anthropic.claude-3-7-sonnet-",
    "anthropic.claude-haiku-4-5-",
    "anthropic.claude-sonnet-4-5-",
    "anthropic.claude-opus-4-5-",
    "amazon.nova-pro-",
    "amazon.nova-lite-",
    "amazon.nova-micro-",
)

# Cross-region inference profile prefixes stripped before the suffix
# match. Add here when AWS launches a new partition's profile prefix.
_CROSS_REGION_PREFIXES: tuple[str, ...] = (
    "us.",
    "eu.",
    "apac.",
    "us-gov.",
)

# Valid cachePoint TTL values the Converse API accepts. The service
# contract (bedrock-runtime's CachePointBlock type) enumerates "5m"
# and "1h" only; sending anything else returns a ValidationException.
_VALID_CACHE_TTLS: frozenset[str] = frozenset({"5m", "1h"})


def _supports_prompt_caching(model_id: str) -> bool:
    """Return True if the model (or its cross-region inference profile)
    is on the prompt-caching allowlist.
    """
    if not isinstance(model_id, str) or not model_id:
        return False
    stripped = model_id
    for prefix in _CROSS_REGION_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    return any(stripped.startswith(p) for p in _CACHE_SUPPORTED_MODEL_PREFIXES)


def _supports_extended_ttl(model_id: str) -> bool:
    """Whether the model accepts an explicit cachePoint ``ttl``.

    Extended-TTL prompt caching (sending a ``ttl`` on the cachePoint) is
    an Anthropic-only Bedrock feature. Non-Anthropic models that DO
    support prompt caching (e.g. Amazon Nova) accept a cachePoint block
    but reject an explicit ``ttl`` with a ValidationException:
    "Extended TTL prompt caching is only supported for Anthropic models".
    For those models the agent emits a plain cachePoint (service-default
    TTL) and omits the ``ttl`` field. The cross-region inference prefix
    (``us.``, ``eu.`` ...) is stripped before the vendor match.
    """
    if not isinstance(model_id, str) or not model_id:
        return False
    stripped = model_id
    for prefix in _CROSS_REGION_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    return stripped.startswith("anthropic.")


def _parse_cache_ttl(value: str | None) -> str | None:
    """Parse the ``BEDROCK_PROMPT_CACHE_TTL`` env-var value.

    Returns one of:
        "5m" | "1h"  — caching enabled with that TTL
        None         — caching disabled (either explicit "off" or
                       invalid / missing value)

    Rationale: unknown strings fall through to None rather than raise,
    because an env-var typo should not break the Lambda. The resolved
    choice is logged by the agent on first use so misconfiguration is
    visible in CloudWatch.
    """
    if value is None:
        return "5m"  # default on
    normalized = value.strip().lower()
    if normalized in {"", "off", "false", "no", "0", "disabled"}:
        return None
    if normalized in _VALID_CACHE_TTLS:
        return normalized
    return "5m"  # unknown value — fall back to the safe default


class CapacityDecisionAgent:
    """Agent that orchestrates tools to produce capacity decisions."""

    def __init__(
        self,
        model_id: str = BEDROCK_MODEL_ID,
        region: str = AWS_REGION,
        max_iterations: int = 15,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        verbose: bool = False,
        lambda_context: Any | None = None,
        tool_cache: ToolCache | None = None,
        metrics_emitter: ToolMetricsEmitter | None = None,
        guardrail_identifier: str | None = None,
        guardrail_version: str | None = None,
    ):
        self.model_id = model_id
        self.region = region
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.verbose = verbose
        self.lambda_context = lambda_context
        self.tool_cache = tool_cache
        self.metrics_emitter = metrics_emitter
        # Bedrock Guardrails wiring. When guardrail_identifier is set, every
        # Converse call includes a ``guardrailConfig`` block so PROMPT_ATTACK,
        # sensitive-info, and content filters run server-side. This is the
        # supported prompt-injection defense — the coarse substring blocklist
        # in ``lambda_handler.py`` is only a pre-filter for obviously hostile
        # inputs, NOT the primary control.
        self.guardrail_identifier = guardrail_identifier
        self.guardrail_version = guardrail_version or "DRAFT"
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.tools: dict[str, dict[str, Any]] = {}
        self.token_budget = self._parse_budget(os.environ.get("TOKEN_BUDGET"))
        # Prompt-cache TTL — "5m" (default), "1h", or None (off). The
        # system prompt + tool definitions are stable across requests;
        # a cache point written at the end of each of those lists lets
        # subsequent Converse calls within the TTL window read the
        # prefix for 10% of the normal input-token price. See
        # docs/implementation-guide.md § Prompt Caching.
        self.prompt_cache_ttl = _parse_cache_ttl(
            os.environ.get("BEDROCK_PROMPT_CACHE_TTL"),
        )

    # ---------------- Tool registration ----------------

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        """Register a tool the agent can call.

        input_schema follows Bedrock toolSpec.inputSchema.json shape.
        handler signature: handler(**kwargs) -> JSON-serializable result
        """
        self.tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }

    # ---------------- Internal helpers ----------------

    def _prompt_caching_enabled(self) -> bool:
        """Whether this agent instance should emit cachePoint blocks.

        Caching is enabled only when both:
          1. ``BEDROCK_PROMPT_CACHE_TTL`` resolved to a valid value
             (not disabled by the operator), and
          2. The configured ``model_id`` is on the prompt-caching
             allowlist (sending a cachePoint to an unsupported model
             produces a ValidationException from Converse).

        Evaluated per call rather than cached so a mid-run
        reassignment of ``self.model_id`` (test fixtures, in-process
        model override) takes effect without re-instantiating.
        """
        return self.prompt_cache_ttl is not None and _supports_prompt_caching(
            self.model_id
        )

    def _cache_point_block(self) -> dict[str, Any]:
        """Build the Converse cachePoint content block.

        An explicit ``ttl`` selects Bedrock's extended-TTL prompt
        caching, which is Anthropic-only. For non-Anthropic models that
        support caching (e.g. Amazon Nova) we emit a plain cachePoint
        without ``ttl`` (service-default 5-minute TTL); sending a ttl
        there raises "Extended TTL prompt caching is only supported for
        Anthropic models". See ``_supports_extended_ttl``.
        """
        block: dict[str, Any] = {"type": "default"}
        if _supports_extended_ttl(self.model_id):
            block["ttl"] = self.prompt_cache_ttl
        return {"cachePoint": block}

    def _format_system(self) -> list[dict[str, Any]]:
        """System block list with an optional trailing cachePoint.

        The cache point sits at the end of ``system`` so everything in
        ``system`` (currently just the system prompt) is written to
        the cache; subsequent requests reuse that prefix for 10% of
        the normal input-token cost.
        """
        system: list[dict[str, Any]] = [{"text": SYSTEM_PROMPT}]
        if self._prompt_caching_enabled():
            system.append(self._cache_point_block())
        return system

    def _format_tool_config(self) -> dict[str, Any]:
        """Produce Bedrock-shape toolConfig from registered tools.

        When prompt caching is enabled, append a cachePoint entry at
        the END of ``tools`` so the whole tool-definition list is
        written to the cache (deterministically ordered, stable across
        requests from the same deploy).
        """
        tools: list[dict[str, Any]] = [
            {
                "toolSpec": {
                    "name": name,
                    "description": meta["description"],
                    "inputSchema": {"json": meta["input_schema"]},
                }
            }
            for name, meta in self.tools.items()
        ]
        if self._prompt_caching_enabled():
            tools.append(self._cache_point_block())
        return {"tools": tools}

    def _messages_with_cache_point(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return a shallow copy of ``messages`` with a cachePoint block
        appended to the content of the last message.

        Within a single ``run()`` loop the ``messages`` list grows every
        iteration (assistant tool_use + user tool_result pairs). Marking
        the end of the latest message as a cache point means each
        Converse call writes a progressively larger cached prefix, and
        the NEXT iteration can read everything up to the previous
        iteration's cache point for 10% of normal input-token price.

        The original ``messages`` list is NOT mutated — the cachePoint
        lives only in the transient copy sent to Converse. That keeps
        the loop's bookkeeping (``messages.append(...)`` on later
        iterations) honest; if we mutated in place we'd accumulate
        cachePoints across iterations and quickly exceed Bedrock's
        per-request limit (4 total across system / tools / messages).
        """
        if not messages:
            return messages
        last = messages[-1]
        last_content = last.get("content") or []
        new_content = list(last_content) + [self._cache_point_block()]
        new_last = {**last, "content": new_content}
        return [*messages[:-1], new_last]

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool call. Returns JSON-serializable result wrapped with metadata.

        On success, returns ``{"ok": True, "data": result}``.

        On NON-retryable failure, returns
        ``{"ok": False, "error": ..., "error_type": ..., "tool_name": ...}``.

        Retryable exceptions (throttling, 5xx, transient network errors) are
        RE-RAISED so the outer ``with_retry`` wrapper can back off and retry.
        This is the fix for the previously dead retry path: the old code
        caught every exception here, so ``with_retry`` never saw retryable
        errors on the tool call.
        """
        if name not in self.tools:
            return {
                "ok": False,
                "error": f"Unknown tool: {name}",
                "error_type": "UnknownToolError",
                "tool_name": name,
            }
        try:
            result = self.tools[name]["handler"](**args)
            return {"ok": True, "data": result}
        except Exception as exc:  # noqa: BLE001
            # Let retryable exceptions propagate so with_retry can back off.
            if _is_retryable(exc):
                raise
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": type(exc).__name__,
                "tool_name": name,
            }

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        return "\n".join(
            block["text"] for block in message.get("content", []) if "text" in block
        )

    @staticmethod
    def _extract_tool_uses(message: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            block["toolUse"]
            for block in message.get("content", [])
            if "toolUse" in block
        ]

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    @staticmethod
    def _parse_budget(value: str | None) -> int:
        """Parse TOKEN_BUDGET env var. Returns 20000 for invalid/missing values."""
        if value is None:
            return 20000
        try:
            parsed = int(value)
            return parsed if parsed >= 1 else 20000
        except (ValueError, TypeError):
            return 20000

    # ---------------- Main loop ----------------

    def run(self, user_message: str) -> dict[str, Any]:
        """Run the agent loop until end_turn or max_iterations.

        Returns:
            dict with keys:
                final_text: str
                messages: full conversation history
                tool_calls: list of (tool_name, input, output) tuples
                iterations: int
                stop_reason: str
                usage: BILLED token totals across all Bedrock turns. Bedrock's
                    Converse API charges ``inputTokens`` per call, and each
                    turn re-sends the growing conversation history, so the
                    sum below equals the actual on-invoice cost. This is the
                    number the ``TOKEN_BUDGET`` guard compares against.
                token_budget_limit: configured token budget (int)
                tool_outcomes: dict with succeeded and failed tool lists
        """
        if not self.tools:
            raise RuntimeError("No tools registered. Call register_tool() first.")

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"text": user_message}]}
        ]
        tool_calls: list[tuple[str, dict, Any]] = []
        tool_outcomes: dict[str, dict[str, str]] = {}
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read_tokens = 0
        total_cache_write_tokens = 0
        last_stop_reason = "max_iterations"

        for iteration in range(1, self.max_iterations + 1):
            self._log(f"\n--- Iteration {iteration} ---")

            # Timeout safety: skip Bedrock call if insufficient time remains
            if self.lambda_context is not None:
                remaining_ms = self.lambda_context.get_remaining_time_in_millis()
                if remaining_ms < 15000:
                    last_stop_reason = "timeout_safety"
                    self._log(
                        f"  ⏱ Only {remaining_ms}ms remaining — stopping for timeout safety"
                    )
                    break

            # Within-conversation prompt caching: mark the end of the
            # latest message as a cache point so iteration N+1 can
            # read everything up to iteration N's point at 10% of
            # normal input price. The original messages list stays
            # untouched; the cachePoint lives only in the transient
            # copy sent to Converse. See the per-iteration arithmetic
            # in docs/implementation-guide.md § Prompt Caching.
            if self._prompt_caching_enabled():
                converse_messages = self._messages_with_cache_point(messages)
            else:
                converse_messages = messages

            converse_kwargs: dict[str, Any] = {
                "modelId": self.model_id,
                "messages": converse_messages,
                "toolConfig": self._format_tool_config(),
                "system": self._format_system(),
                "inferenceConfig": {
                    "maxTokens": self.max_tokens,
                    "temperature": self.temperature,
                },
            }
            if self.guardrail_identifier:
                converse_kwargs["guardrailConfig"] = {
                    "guardrailIdentifier": self.guardrail_identifier,
                    "guardrailVersion": self.guardrail_version,
                    "trace": "enabled",
                }

            response = with_retry(
                self.client.converse,
                **converse_kwargs,
                max_attempts=3,
                base_delay=2.0,
                max_delay=10.0,
            )

            usage = response.get("usage", {})
            total_input_tokens += usage.get("inputTokens", 0) or 0
            total_output_tokens += usage.get("outputTokens", 0) or 0
            # Prompt-cache usage from Bedrock. Both fields default to 0
            # for responses on models that don't support caching and
            # for the first request within a TTL window (which pays
            # the cache-write premium instead of the cache-read
            # discount). See docs/implementation-guide.md § Prompt
            # Caching for the pricing model.
            total_cache_read_tokens += usage.get("cacheReadInputTokens", 0) or 0
            total_cache_write_tokens += usage.get("cacheWriteInputTokens", 0) or 0

            output_message = response["output"]["message"]
            messages.append(output_message)
            last_stop_reason = response.get("stopReason", "unknown")

            self._log(
                f"Stop reason: {last_stop_reason} | "
                f"in={usage.get('inputTokens')} out={usage.get('outputTokens')}"
            )

            # Token budget enforcement: stop if cumulative usage exceeds budget
            cumulative = total_input_tokens + total_output_tokens
            if cumulative > self.token_budget:
                last_stop_reason = "token_budget_exceeded"
                self._log(f"Token budget exceeded: {cumulative} > {self.token_budget}")
                break

            if last_stop_reason == "end_turn":
                break

            if last_stop_reason == "guardrail_intervened":
                # Bedrock Guardrails blocked the response. Stop the loop and
                # let the caller surface a generic-safe response.
                self._log("Guardrail intervened; halting agent loop.")
                break

            if last_stop_reason == "tool_use":
                tool_uses = self._extract_tool_uses(output_message)
                tool_result_blocks: list[dict[str, Any]] = []
                for tu in tool_uses:
                    name = tu["name"]
                    args = tu.get("input", {}) or {}
                    self._log(f"  → tool: {name}({json.dumps(args)})")

                    # Track execution time for metrics
                    start_time = time.time()

                    # 1. Check cache first (if tool_cache is available)
                    cached_result = None
                    if self.tool_cache is not None:
                        cached_result = self.tool_cache.get(name, args)

                    if cached_result is not None:
                        # Cache hit — use cached result directly. Duration
                        # is measured from ``start_time`` above whether we
                        # hit or miss, so the emitted metric still reflects
                        # wall-clock cost of the tool call site.
                        result = {"ok": True, "data": cached_result}
                        self._log(f"    ⚡ cache hit for {name}")
                    else:
                        # 2. Cache miss — execute tool with retry handler
                        try:
                            result = with_retry(self._execute_tool, name, args)
                        except Exception as exc:  # noqa: BLE001
                            # All retries exhausted or non-retryable error
                            error_type = type(exc).__name__
                            error_msg = f"{error_type}: {exc}"
                            result = {
                                "ok": False,
                                "error": error_msg,
                                "error_type": error_type,
                                "tool_name": name,
                            }

                        # 3. On success, cache the result
                        if result.get("ok") and self.tool_cache is not None:
                            self.tool_cache.put(name, args, result.get("data", result))

                    # 4. Emit tool metrics (duration, status)
                    elapsed_ms = (time.time() - start_time) * 1000
                    status = "Success" if result.get("ok") else "Failure"
                    if self.metrics_emitter is not None:
                        self.metrics_emitter.emit_tool_metric(name, elapsed_ms, status)

                    # Record tool outcome for response metadata
                    if result.get("ok"):
                        tool_outcomes[name] = {"status": "success"}
                    else:
                        error_type = result.get("error_type", "UnknownError")
                        error_msg = result.get("error", "Unknown error occurred")
                        tool_outcomes[name] = {
                            "status": "failure",
                            "error_type": error_type,
                            "error_msg": error_msg,
                        }

                    tool_calls.append((name, args, result))
                    self._log(f"    ← {json.dumps(result)[:200]}")

                    # Build tool result block for Bedrock
                    # On failure, pass structured error so model can acknowledge the gap
                    if result.get("ok"):
                        tool_result_content = [{"json": result}]
                    else:
                        tool_result_content = [
                            {
                                "json": {
                                    "error": True,
                                    "tool_name": name,
                                    "error_type": result.get(
                                        "error_type", "UnknownError"
                                    ),
                                    "error_description": result.get(
                                        "error", "Tool execution failed"
                                    ),
                                }
                            }
                        ]

                    tool_result_blocks.append(
                        {
                            "toolResult": {
                                "toolUseId": tu["toolUseId"],
                                "content": tool_result_content,
                            }
                        }
                    )
                messages.append({"role": "user", "content": tool_result_blocks})
                continue

            # Other stop reasons — return what we have
            break

        # Build succeeded/failed tool lists from outcomes
        succeeded_tools = [
            name
            for name, outcome in tool_outcomes.items()
            if outcome["status"] == "success"
        ]
        failed_tools = [
            {
                "tool": name,
                "error_type": outcome.get("error_type", "UnknownError"),
                "error": outcome.get("error_msg", "Unknown error"),
            }
            for name, outcome in tool_outcomes.items()
            if outcome["status"] == "failure"
        ]

        # Deterministic confidence scoring — extracts signals from tool_calls
        # and computes a verdict per (instance_type, region) investigated.
        # Defensive: scoring failures must never break the request.
        try:
            confidence_assessments = assess_targets(tool_calls)
        except Exception:  # noqa: BLE001
            confidence_assessments = []

        return {
            "final_text": self._extract_text(messages[-1])
            if messages[-1].get("role") == "assistant"
            else "",
            "messages": messages,
            "tool_calls": tool_calls,
            "iterations": iteration,
            "stop_reason": last_stop_reason,
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "cache_read_tokens": total_cache_read_tokens,
                "cache_write_tokens": total_cache_write_tokens,
            },
            "token_budget_limit": self.token_budget,
            "tool_outcomes": {
                "succeeded": succeeded_tools,
                "failed": failed_tools,
            },
            "confidence_assessments": confidence_assessments,
        }
