# backend/peldrun/llm/providers/anthropic.py
"""
PELDRUN Native Anthropic Provider Adapter.

Talks directly to Anthropic's Messages API via the official SDK. The
adapter owns the full translation from PELDRUN's internal
``CompiledRequest`` to Anthropic's wire format, and from Anthropic's
``Message`` / streaming events back to PELDRUN's canonical
``LLMResponse`` / ``LLMStreamEvent``.

Key differences from OpenAI that this adapter handles
-----------------------------------------------------

1. **System prompt is a top-level field**, not a ``role: "system"``
   message. The compiler extracts it; the adapter passes it as
   ``system=``.

2. **``max_tokens`` is mandatory.** OpenAI can infer it; Anthropic
   requires it. When the compiler did not produce one, the adapter
   uses a safe default and records a diagnostic.

3. **Tool calls use a different shape:**
       OpenAI:    ``message.tool_calls[]`` with ``function.{name,arguments}``
       Anthropic: content blocks ``{"type": "tool_use", ...}`` inside
                  ``message.content``
   The adapter normalizes both directions.

4. **Tool results are ``tool_result`` blocks inside USER messages.**
   The adapter converts PELDRUN's OpenAI-shaped ``{role: "tool", ...}``
   messages into Anthropic's user-message content blocks.

5. **Three thinking modes:**
       * Adaptive:      ``thinking: {type: "adaptive"}`` — new models
       * Extended:      ``thinking: {type: "enabled", budget_tokens: N}``
       * Effort:        ``output_config: {effort: "high"}`` — newest models
   The compiler emits the right mode; the adapter just passes it.

6. **Extended thinking requires ``temperature=1.0`` (or omitted).**
   The compiler already drops ``temperature`` when reasoning is active
   for this surface. The adapter does not double-enforce.

7. **Budget vs. max_tokens constraint.** Anthropic requires
   ``budget_tokens < max_tokens`` and ``budget_tokens >= 1024``.
   The adapter validates and adjusts when needed, recording any
   adjustment in the diagnostics.

8. **Prompt caching is opt-in per block** via ``cache_control``.
   Not wired in this adapter version — reserved for a future release.

See also
--------

    * ``backend/peldrun/llm/contracts.py``        — ProviderAdapter protocol
    * ``backend/peldrun/llm/request_compiler.py`` — compiled request
    * ``backend/peldrun/llm/errors.py``           — error taxonomy
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import anthropic
from anthropic import AsyncAnthropic

from peldrun.llm.client import (
    LLMResponse,
    TokenUsage,
    ToolCall,
    UsageSource,
)
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ConnectionContext,
    LLMStreamEvent,
    ReasoningStrategy,
    StreamEventType,
    ToolCallStyle,
)
from peldrun.llm.errors import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_status,
    is_cancellation,
    refine_by_message,
)
from peldrun.llm.request import LLMRequest
from peldrun.llm.request_compiler import CompiledRequest, compile_request

logger = logging.getLogger("peldrun.llm.providers.anthropic")


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------


#: Anthropic requires max_tokens. When the caller omitted it, we use
#: this safe value. Small enough to fit any model's context; large
#: enough for a useful answer.
_DEFAULT_MAX_TOKENS = 4096


#: Anthropic's extended thinking requires this minimum budget.
_MIN_THINKING_BUDGET = 1024


#: Default extended-thinking budget when the compiler emitted only a
#: toggle (no explicit budget).
_DEFAULT_THINKING_BUDGET = 4000


#: [P1-12 FIX — Output reserve]
#: Minimum number of tokens that must remain available for the
#: model's actual response AFTER the thinking budget has been
#: allocated. The historical code only enforced
#: ``budget < max_tokens``, which permits cases like
#: ``max_tokens=1500, budget=1400`` where only 100 tokens remain
#: for output — the model spends its entire allowance on internal
#: reasoning and returns an empty or truncated response with NO
#: warning.
#:
#: ``1024`` matches the minimum extended-thinking budget. It gives
#: the model enough room for a useful answer while keeping the
#: reasoning allowance meaningful. When the caller's
#: ``max_tokens`` cannot accommodate both a minimum thinking budget
#: AND this reserve (i.e. ``max_tokens < MIN_THINKING_BUDGET +
#: MIN_OUTPUT_RESERVE = 2048``), extended thinking is DISABLED —
#: sending a thinking budget that leaves no room for output would
#: produce the exact silent-empty-response failure this constant
#: exists to prevent.
_MIN_OUTPUT_RESERVE = 1024


# ---------------------------------------------------------------------------
# Adapter profile
# ---------------------------------------------------------------------------


_PROFILE = AdapterProfile(
    provider_id="anthropic",
    api_surface="messages",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        forced_tool_choice=True,
        vision=True,
        audio=False,           # Anthropic has no audio input
        # Anthropic exposes no native structured-output parameter.
        # Callers that need JSON should use tool-forcing or prompt
        # engineering. The compiler will drop response_format when the
        # adapter declares structured_output=False.
        structured_output=False,
        usage_reporting=True,
        reasoning=True,
        prompt_caching=True,   # via cache_control (future)
    ),
    reasoning_strategies=[ReasoningStrategy.ANTHROPIC_MESSAGES],
    tool_call_style=ToolCallStyle.ANTHROPIC_TOOL_USE,
    supported_param_names=[],
    notes=(
        "Native Anthropic adapter. Messages API with adaptive / extended "
        "thinking. Structured output is not natively supported."
    ),
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AnthropicAdapter:
    """Native Anthropic Messages API adapter."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "anthropic"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        context: Any = None,
        *,
        connection: Optional[ConnectionContext] = None,
        profile: Optional[AdapterProfile] = None,
    ) -> None:
        if context is not None:
            self._connection: ConnectionContext = context.connection
            self._profile: AdapterProfile = context.profile or _PROFILE
            self._credentials: Dict[str, str] = dict(context.credentials or {})
        else:
            if connection is None:
                raise TypeError(
                    "AnthropicAdapter requires either `context=` or "
                    "`connection=` (and optionally `profile=`)."
                )
            self._connection = connection
            self._profile = profile or _PROFILE
            self._credentials = (
                {"api_key": connection.api_key}
                if connection.api_key
                else {}
            )

        self.profile = self._profile

        # Anthropic SDK expects the base_url WITHOUT /v1 — it appends
        # /v1/messages internally. We normalize any caller-supplied
        # /v1 suffix away.
        resolved_base_url = self._connection.base_url
        if resolved_base_url:
            resolved_base_url = resolved_base_url.rstrip("/")
            if resolved_base_url.endswith("/v1"):
                resolved_base_url = resolved_base_url[: -len("/v1")]
        else:
            resolved_base_url = "https://api.anthropic.com"

        api_key = (
            self._credentials.get("api_key")
            or self._connection.api_key
            or ""
        )

        # The SDK requires an api_key even if empty — passing "" would
        # raise immediately. Use a sentinel so failures surface at
        # request time with a helpful message.
        if not api_key:
            api_key = "MISSING_API_KEY"

        self._client = AsyncAnthropic(
            base_url=resolved_base_url,
            api_key=api_key,
            timeout=float(self._connection.timeout or 120.0),
        )

        logger.debug(
            "AnthropicAdapter ready: base_url=%s", resolved_base_url
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: generate
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started_at = time.time()
        compiled = compile_request(request, self._profile)

        try:
            kwargs = self._build_messages_kwargs(compiled)
            response = await self._client.messages.create(**kwargs)
        except Exception as exc:
            raise _map_anthropic_exception(
                exc, provider=self.provider_id, model=compiled.model
            ) from exc

        completed_at = time.time()
        result = self._convert_message_response(response, compiled.model)
        result.latency_ms = round((completed_at - started_at) * 1000.0, 2)
        result.ttft_ms = result.latency_ms

        if result.raw is None:
            result.raw = {}
        if isinstance(result.raw, dict):
            result.raw.setdefault("_peldrun_diagnostics", compiled.diagnostics)

        return result

    # ------------------------------------------------------------------
    # ProviderAdapter: stream
    # ------------------------------------------------------------------

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        compiled = compile_request(request, self._profile)

        try:
            kwargs = self._build_messages_kwargs(compiled)
        except Exception as exc:
            yield _error_event(
                _map_anthropic_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=1,
            )
            return

        seq = 0
        # Content-block state machine: index → {type, accumulated data}.
        active_blocks: Dict[int, Dict[str, Any]] = {}
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", None)

                    # ---------------------------------------------------
                    # message_start — capture initial usage.
                    # ---------------------------------------------------
                    if event_type == "message_start":
                        msg = getattr(event, "message", None)
                        usage_obj = getattr(msg, "usage", None) if msg else None
                        if usage_obj is not None:
                            captured_usage = _convert_anthropic_usage(
                                usage_obj
                            )

                    # ---------------------------------------------------
                    # content_block_start — a new block (text / thinking /
                    # tool_use) begins.
                    # ---------------------------------------------------
                    elif event_type == "content_block_start":
                        block_index = getattr(event, "index", 0)
                        content_block = getattr(event, "content_block", None)
                        block_type = (
                            getattr(content_block, "type", None)
                            if content_block is not None
                            else None
                        )

                        if block_type == "tool_use":
                            tool_id = (
                                getattr(content_block, "id", None) or "call"
                            )
                            tool_name = (
                                getattr(content_block, "name", None) or ""
                            )
                            active_blocks[block_index] = {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "arguments": "",
                            }
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_id=tool_id,
                                tool_name=tool_name,
                                tool_index=block_index,
                                sequence=seq,
                            )
                        elif block_type == "thinking":
                            active_blocks[block_index] = {
                                "type": "thinking",
                            }
                        elif block_type == "text":
                            active_blocks[block_index] = {"type": "text"}
                        else:
                            # Unknown block type — remember it so the
                            # stop handler does not crash.
                            active_blocks[block_index] = {
                                "type": block_type or "unknown"
                            }

                    # ---------------------------------------------------
                    # content_block_delta — incremental content.
                    # ---------------------------------------------------
                    elif event_type == "content_block_delta":
                        block_index = getattr(event, "index", 0)
                        delta = getattr(event, "delta", None)
                        delta_type = (
                            getattr(delta, "type", None)
                            if delta is not None
                            else None
                        )

                        if delta_type == "text_delta":
                            text_piece = getattr(delta, "text", None)
                            if isinstance(text_piece, str) and text_piece:
                                seq += 1
                                yield LLMStreamEvent(
                                    type=StreamEventType.CONTENT_DELTA,
                                    content_delta=text_piece,
                                    sequence=seq,
                                )

                        elif delta_type == "thinking_delta":
                            thinking_piece = getattr(delta, "thinking", None)
                            if (
                                isinstance(thinking_piece, str)
                                and thinking_piece
                            ):
                                seq += 1
                                yield LLMStreamEvent(
                                    type=StreamEventType.REASONING_DELTA,
                                    reasoning_delta=thinking_piece,
                                    sequence=seq,
                                )

                        elif delta_type == "input_json_delta":
                            partial = getattr(delta, "partial_json", None)
                            if (
                                isinstance(partial, str)
                                and partial
                                and block_index in active_blocks
                                and active_blocks[block_index].get("type")
                                == "tool_use"
                            ):
                                active_blocks[block_index][
                                    "arguments"
                                ] += partial
                                seq += 1
                                yield LLMStreamEvent(
                                    type=StreamEventType.TOOL_CALL_DELTA,
                                    tool_call_id=active_blocks[block_index][
                                        "id"
                                    ],
                                    tool_name=active_blocks[block_index][
                                        "name"
                                    ],
                                    tool_index=block_index,
                                    tool_arguments_delta=partial,
                                    sequence=seq,
                                )

                        # signature_delta for thinking blocks is
                        # ignored — the signature is only needed on
                        # the request side for continuity, not for
                        # rendering.

                    # ---------------------------------------------------
                    # content_block_stop — finalize the block.
                    # ---------------------------------------------------
                    elif event_type == "content_block_stop":
                        block_index = getattr(event, "index", 0)
                        block_state = active_blocks.pop(block_index, None)
                        if block_state and block_state.get("type") == "tool_use":
                            raw_args = block_state.get("arguments", "")
                            parsed_args: Any
                            if isinstance(raw_args, str) and raw_args:
                                try:
                                    parsed_args = json.loads(raw_args)
                                except Exception:
                                    parsed_args = {"raw": raw_args}
                            else:
                                parsed_args = {}
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.TOOL_CALL_COMPLETE,
                                tool_call_id=block_state["id"],
                                tool_name=block_state["name"],
                                tool_index=block_index,
                                tool_arguments_complete=parsed_args,
                                sequence=seq,
                            )

                    # ---------------------------------------------------
                    # message_delta — final stop_reason + output usage.
                    # ---------------------------------------------------
                    elif event_type == "message_delta":
                        delta = getattr(event, "delta", None)
                        stop_reason = (
                            getattr(delta, "stop_reason", None)
                            if delta is not None
                            else None
                        )
                        if stop_reason:
                            finish_reason = stop_reason

                        usage_obj = getattr(event, "usage", None)
                        if usage_obj is not None:
                            # Anthropic's message_delta usage is
                            # cumulative output_tokens. Merge with the
                            # input_tokens we captured from message_start.
                            delta_usage = _convert_anthropic_usage(usage_obj)
                            if captured_usage is not None and delta_usage:
                                captured_usage = TokenUsage(
                                    input_tokens=(
                                        captured_usage.input_tokens
                                        or delta_usage.input_tokens
                                    ),
                                    output_tokens=(
                                        delta_usage.output_tokens
                                        or captured_usage.output_tokens
                                    ),
                                    total_tokens=(
                                        (captured_usage.input_tokens or 0)
                                        + (delta_usage.output_tokens or 0)
                                    )
                                    or captured_usage.total_tokens,
                                    cached_input_tokens=(
                                        captured_usage.cached_input_tokens
                                        or delta_usage.cached_input_tokens
                                    ),
                                    source=UsageSource.PROVIDER,
                                    estimated=False,
                                )
                            elif delta_usage:
                                captured_usage = delta_usage

                    # ---------------------------------------------------
                    # error — surface as a stream error event.
                    # ---------------------------------------------------
                    elif event_type == "error":
                        err_obj = getattr(event, "error", None)
                        message = (
                            getattr(err_obj, "message", None)
                            if err_obj is not None
                            else "Anthropic stream reported an error."
                        )
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.PROVIDER_ERROR,
                            error={
                                "code": "anthropic_stream_error",
                                "message": str(message),
                                "retryable": False,
                                "provider": self.provider_id,
                            },
                            sequence=seq,
                        )
                        return

                    # message_stop, ping, and future event types fall
                    # through and are ignored.

        except asyncio.CancelledError:
            # Cancel — the context manager closes the stream; we just
            # propagate.
            raise
        except Exception as exc:
            yield _error_event(
                _map_anthropic_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        if captured_usage:
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.USAGE_UPDATE,
                usage=captured_usage,
                sequence=seq,
            )

        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason or "end_turn",
            usage=captured_usage,
            sequence=seq,
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: discover_models
    # ------------------------------------------------------------------

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        Return Anthropic's ``/v1/models`` list.

        The response has ``{id, display_name, created_at, type}``. No
        capability information — the LiteLLM layer fills that in.
        """
        try:
            response = await self._client.models.list()
        except Exception as exc:
            logger.warning("Anthropic model discovery failed: %s", exc)
            return []

        raw_items = getattr(response, "data", []) or []
        results: List[Dict[str, Any]] = []
        for item in raw_items:
            item_id = getattr(item, "id", None)
            if not isinstance(item_id, str) or not item_id:
                continue
            results.append(
                {
                    "id": item_id,
                    "display_name": getattr(item, "display_name", None),
                    "created_at": getattr(item, "created_at", None),
                    "type": getattr(item, "type", "model"),
                }
            )
        return results

    # ------------------------------------------------------------------
    # ProviderAdapter: validate_connection
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        started_at = time.time()
        try:
            await self._client.models.list()
            latency_ms = round((time.time() - started_at) * 1000.0, 2)
            return {
                "ok": True,
                "message": "Anthropic API reachable.",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round((time.time() - started_at) * 1000.0, 2)
            err = _map_anthropic_exception(
                exc, provider=self.provider_id, model=""
            )
            return {
                "ok": False,
                "message": err.message,
                "latency_ms": latency_ms,
                "code": err.code,
                "status_code": err.status_code,
            }

    # ------------------------------------------------------------------
    # ProviderAdapter: get_model_metadata
    # ------------------------------------------------------------------

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Anthropic's ``/v1/models/{id}`` returns descriptive metadata
        but no capability declarations.
        """
        try:
            response = await self._client.models.retrieve(model_id)
        except Exception:
            return None

        return {
            "id": getattr(response, "id", model_id),
            "display_name": getattr(response, "display_name", None),
            "type": getattr(response, "type", "model"),
        }

    # ------------------------------------------------------------------
    # ProviderAdapter: close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as exc:
            logger.debug("Anthropic client close raised: %s", exc)

    # ==================================================================
    # Request building
    # ==================================================================

    def _build_messages_kwargs(
        self, compiled: CompiledRequest
    ) -> Dict[str, Any]:
        """
        Convert a ``CompiledRequest`` into Anthropic ``messages.create``
        kwargs.
        """
        # --------------------------------------------------------------
        # 1. max_tokens is mandatory. Apply default if missing.
        # --------------------------------------------------------------
        max_tokens = compiled.generation.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            max_tokens = _DEFAULT_MAX_TOKENS

        # --------------------------------------------------------------
        # 2. Convert messages from OpenAI-shape → Anthropic-shape.
        # --------------------------------------------------------------
        anthropic_messages = self._convert_messages(compiled.messages)

        kwargs: Dict[str, Any] = {
            "model": compiled.model,
            "max_tokens": int(max_tokens),
            "messages": anthropic_messages,
        }

        # System prompt.
        if compiled.system_prompt:
            kwargs["system"] = compiled.system_prompt

        # --------------------------------------------------------------
        # 3. Generation — only include params that Anthropic accepts.
        # --------------------------------------------------------------
        if "temperature" in compiled.generation:
            kwargs["temperature"] = float(
                compiled.generation["temperature"]
            )
        if "top_p" in compiled.generation:
            kwargs["top_p"] = float(compiled.generation["top_p"])
        if "top_k" in compiled.generation:
            kwargs["top_k"] = int(compiled.generation["top_k"])
        if "stop" in compiled.generation:
            stop_val = compiled.generation["stop"]
            if isinstance(stop_val, list) and stop_val:
                kwargs["stop_sequences"] = [str(s) for s in stop_val]

        # --------------------------------------------------------------
        # 4. Tools + tool_choice.
        # --------------------------------------------------------------
        if compiled.tools:
            kwargs["tools"] = self._convert_tools(compiled.tools)
            if compiled.tool_choice:
                choice = self._convert_tool_choice(compiled.tool_choice)
                if choice is not None:
                    kwargs["tool_choice"] = choice

        # --------------------------------------------------------------
        # 5. Reasoning — the compiler emits one of three shapes.
        # --------------------------------------------------------------
        if compiled.reasoning:
            thinking_block, output_config = self._build_reasoning_kwargs(
                compiled.reasoning, max_tokens
            )
            if thinking_block is not None:
                kwargs["thinking"] = thinking_block
            if output_config is not None:
                kwargs["output_config"] = output_config

        # --------------------------------------------------------------
        # 6. Provider options (escape hatch).
        # --------------------------------------------------------------
        if compiled.provider_options:
            kwargs.update(compiled.provider_options)

        return kwargs

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert PELDRUN/OpenAI-shaped messages to Anthropic's format.

        Handles:
            * Plain text messages (user/assistant).
            * Multimodal ``image_url`` parts → ``image`` blocks.
            * Assistant ``tool_calls`` → ``tool_use`` blocks.
            * ``role: "tool"`` messages → user messages with
              ``tool_result`` blocks.
        """
        result: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                # The compiler already extracted the system prompt;
                # stray system messages are skipped.
                continue

            content = msg.get("content")

            # -------------------------------------------------------
            # Tool result messages (role == "tool").
            # -------------------------------------------------------
            if role == "tool":
                tool_call_id = msg.get("tool_call_id") or "unknown"
                text_content = self._content_to_text(content)
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": str(tool_call_id),
                                "content": text_content,
                            }
                        ],
                    }
                )
                continue

            # -------------------------------------------------------
            # Assistant messages with tool_calls → tool_use blocks.
            # -------------------------------------------------------
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
                blocks: List[Dict[str, Any]] = []
                if content:
                    text_val = self._content_to_text(content)
                    if text_val:
                        blocks.append({"type": "text", "text": text_val})
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or ""
                    raw_args = fn.get("arguments") or tc.get("arguments") or {}
                    if isinstance(raw_args, str):
                        try:
                            args_dict = json.loads(raw_args)
                        except Exception:
                            args_dict = {"raw": raw_args}
                    elif isinstance(raw_args, dict):
                        args_dict = raw_args
                    else:
                        args_dict = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id") or "call",
                            "name": str(name),
                            "input": args_dict,
                        }
                    )
                result.append({"role": "assistant", "content": blocks})
                continue

            # -------------------------------------------------------
            # Regular user / assistant messages — may be text or
            # multimodal parts.
            # -------------------------------------------------------
            if isinstance(content, list):
                converted_blocks: List[Dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        txt = part.get("text", "")
                        if isinstance(txt, str) and txt:
                            converted_blocks.append(
                                {"type": "text", "text": txt}
                            )
                    elif ptype == "image_url":
                        image_block = self._convert_image_part(part)
                        if image_block is not None:
                            converted_blocks.append(image_block)
                if converted_blocks:
                    result.append(
                        {"role": role, "content": converted_blocks}
                    )
                continue

            # Plain string content.
            text = self._content_to_text(content)
            if text:
                result.append({"role": role, "content": text})

        return result

    @staticmethod
    def _convert_image_part(
        part: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Convert an OpenAI ``image_url`` part to an Anthropic ``image``
        block.

        Anthropic requires the image to be either base64 data or an
        https URL, and does NOT accept ``detail`` hints. Only
        ``data:`` URIs and ``http(s)://`` URLs are passed through; other
        schemes are dropped (with a debug log).
        """
        image_payload = part.get("image_url")
        if isinstance(image_payload, dict):
            url = image_payload.get("url")
        else:
            url = image_payload  # Some callers pass the URL directly.
        if not isinstance(url, str) or not url:
            return None

        if url.startswith("data:"):
            # data:image/png;base64,XXXX
            try:
                header, _, data = url.partition(",")
                media_part = header.split(";", 1)[0]
                # media_part = "data:image/png"
                media_type = media_part.split(":", 1)[1] if ":" in media_part else "image/png"
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            except Exception:
                return None

        if url.startswith(("http://", "https://")):
            return {
                "type": "image",
                "source": {"type": "url", "url": url},
            }

        # Anthropic does not accept other schemes (artifact://, file://).
        return None

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """Flatten any content shape to plain text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            fragments: List[str] = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        txt = part.get("text", "")
                        if isinstance(txt, str) and txt:
                            fragments.append(txt)
                elif isinstance(part, str):
                    fragments.append(part)
            return "\n".join(fragments)
        if content is None:
            return ""
        return str(content)

    # ------------------------------------------------------------------
    # Tool conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_tools(
        tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert OpenAI-shaped tools to Anthropic's format.

        OpenAI: ``{"type": "function", "function": {name, description, parameters}}``
        Anthropic: ``{name, description, input_schema}``
        """
        result: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if "function" in tool else tool
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not name:
                continue
            result.append(
                {
                    "name": str(name),
                    "description": str(fn.get("description") or ""),
                    "input_schema": fn.get("parameters")
                    or {"type": "object", "properties": {}, "required": []},
                }
            )
        return result

    @staticmethod
    def _convert_tool_choice(
        tool_choice: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Convert OpenAI-shaped tool_choice to Anthropic's format.

        OpenAI: ``{"type": "auto"|"required"|"none"|"function", "function": {name}}``
        Anthropic: ``{"type": "auto"|"any"|"tool"|"none", "name": ...}``
        """
        if not isinstance(tool_choice, dict):
            return None
        choice_type = tool_choice.get("type")

        if choice_type == "auto":
            return {"type": "auto"}
        if choice_type == "required":
            return {"type": "any"}
        if choice_type == "none":
            return {"type": "none"}
        if choice_type == "function":
            fn = tool_choice.get("function") or {}
            name = fn.get("name")
            if not name:
                return None
            return {"type": "tool", "name": str(name)}
        return None

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reasoning_kwargs(
        reasoning: Dict[str, Any],
        max_tokens: int,
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Convert the compiler's reasoning dict to Anthropic kwargs.

        Returns ``(thinking_block, output_config)``.

        Three shapes from the compiler:

            * ``{"type": "adaptive"}`` — new models.
            * ``{"type": "enabled", "budget_tokens": N}`` — extended.
            * ``{"output_config": {"effort": "high"}}`` — effort.

        Budget validation: Anthropic requires ``1024 <= budget < max_tokens``.
        When the compiler's budget violates this, we adjust it.
        """
        if not isinstance(reasoning, dict):
            return None, None

        rtype = reasoning.get("type")

        if rtype == "adaptive":
            return {"type": "adaptive"}, None

        if rtype == "enabled":
            # [P1-12 FIX — Budget reserve enforcement]
            #
            # The historical logic enforced only ``budget <
            # max_tokens``. That allowed cases where the thinking
            # budget consumed almost the entire output allowance,
            # leaving too few tokens for the model's actual answer.
            # Real-world symptom: the run bills for reasoning
            # tokens, the user sees "thinking..." in the UI, and
            # the response comes back empty with NO error message
            # — the model exhausted ``max_tokens`` on internal
            # reasoning.
            #
            # Post-fix invariants (in order):
            #
            #   1. Budget must be an int >= _MIN_THINKING_BUDGET.
            #      Anything else is normalized to the default (4000).
            #
            #   2. The caller's ``max_tokens`` MUST be large enough
            #      to fit both a minimum thinking budget AND the
            #      output reserve. When
            #      ``max_tokens < _MIN_THINKING_BUDGET + _MIN_OUTPUT_RESERVE``,
            #      extended thinking is DISABLED entirely (returns
            #      ``(None, None)``). Sending a thinking block under
            #      these limits guarantees an unusable response.
            #
            #   3. The final budget is capped at
            #      ``max_tokens - _MIN_OUTPUT_RESERVE`` so the
            #      response ALWAYS has at least 1024 tokens of room,
            #      even if the caller requested a larger budget.
            #
            # Every adjustment is logged at INFO (no silent
            # truncation) so operators can diagnose budget-related
            # failures from the log alone.
            _min_required_max_tokens = (
                _MIN_THINKING_BUDGET + _MIN_OUTPUT_RESERVE
            )

            if max_tokens < _min_required_max_tokens:
                logger.warning(
                    "Anthropic adapter: extended thinking DISABLED. "
                    "max_tokens=%d cannot accommodate "
                    "_MIN_THINKING_BUDGET=%d + "
                    "_MIN_OUTPUT_RESERVE=%d (=%d minimum). "
                    "Sending a thinking budget under these limits "
                    "would guarantee an empty or truncated "
                    "response. Increase max_tokens to >= %d to "
                    "enable thinking.",
                    max_tokens,
                    _MIN_THINKING_BUDGET,
                    _MIN_OUTPUT_RESERVE,
                    _min_required_max_tokens,
                    _min_required_max_tokens,
                )
                return None, None

            budget = reasoning.get("budget_tokens")
            if not isinstance(budget, int) or budget < _MIN_THINKING_BUDGET:
                budget = _DEFAULT_THINKING_BUDGET

            # Enforce the output reserve as a hard upper bound.
            _max_allowed_budget = max_tokens - _MIN_OUTPUT_RESERVE
            if budget > _max_allowed_budget:
                logger.info(
                    "Anthropic adapter: thinking budget capped from "
                    "%d to %d (max_tokens=%d - output reserve=%d) "
                    "so the model has room to answer.",
                    budget,
                    _max_allowed_budget,
                    max_tokens,
                    _MIN_OUTPUT_RESERVE,
                )
                budget = _max_allowed_budget

            return {"type": "enabled", "budget_tokens": budget}, None

        output_config = reasoning.get("output_config")
        if isinstance(output_config, dict):
            return None, output_config

        return None, None

    # ------------------------------------------------------------------
    # Response conversion (non-streaming)
    # ------------------------------------------------------------------

    def _convert_message_response(
        self, response: Any, model: str
    ) -> LLMResponse:
        """
        Convert an Anthropic ``Message`` object to ``LLMResponse``.

        Anthropic's content is a list of blocks:
            * ``{"type": "text", "text": "..."}``
            * ``{"type": "thinking", "thinking": "..."}``
            * ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []

        raw_content = getattr(response, "content", None) or []
        for block in raw_content:
            btype = getattr(block, "type", None)
            if btype == "text":
                txt = getattr(block, "text", None)
                if isinstance(txt, str) and txt:
                    content_parts.append(txt)
            elif btype == "thinking":
                txt = getattr(block, "thinking", None)
                if isinstance(txt, str) and txt:
                    reasoning_parts.append(txt)
            elif btype == "tool_use":
                tool_id = getattr(block, "id", None) or "call"
                tool_name = getattr(block, "name", None) or ""
                raw_input = getattr(block, "input", None)
                args_dict: Any = raw_input if isinstance(raw_input, dict) else {}
                tool_calls.append(
                    ToolCall(
                        id=str(tool_id),
                        name=str(tool_name),
                        arguments=args_dict,
                        type="function",
                    )
                )

        reasoning_text = "".join(reasoning_parts) if reasoning_parts else None
        content_text = "".join(content_parts)
        finish_reason = getattr(response, "stop_reason", None)

        return LLMResponse(
            content=content_text,
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_convert_anthropic_usage(getattr(response, "usage", None)),
            model=getattr(response, "model", model),
            raw=response,
        )


# ---------------------------------------------------------------------------
# Usage conversion
# ---------------------------------------------------------------------------


def _convert_anthropic_usage(raw_usage: Any) -> Optional[TokenUsage]:
    """Anthropic usage → ``TokenUsage``."""
    if raw_usage is None:
        return None

    inp = getattr(raw_usage, "input_tokens", None)
    out = getattr(raw_usage, "output_tokens", None)
    cached = getattr(raw_usage, "cache_read_input_tokens", None)
    cache_created = getattr(raw_usage, "cache_creation_input_tokens", None)

    total: Optional[int] = None
    if inp is not None or out is not None:
        total = (int(inp or 0)) + (int(out or 0))
        # Cache creation and cache read are part of input_tokens in
        # Anthropic's accounting — we surface them separately but do
        # not add them to total, since Anthropic already counts them.
        if cached is None and cache_created is not None:
            cached = cache_created

    if inp is None and out is None:
        return None

    return TokenUsage(
        input_tokens=int(inp) if inp is not None else None,
        output_tokens=int(out) if out is not None else None,
        total_tokens=total,
        cached_input_tokens=int(cached) if cached is not None else None,
        reasoning_output_tokens=None,  # Anthropic does not report separately
        source=UsageSource.PROVIDER,
        estimated=False,
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_anthropic_exception(
    exc: BaseException, *, provider: str, model: str
) -> ProviderError:
    """
    Translate an Anthropic SDK exception into the canonical taxonomy.
    """
    if is_cancellation(exc):
        raise exc

    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=401
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=403
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderRateLimitError(
            str(exc), provider=provider, model=model, status_code=429
        )
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError(
            str(exc), provider=provider, model=model, phase="request"
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderConnectionError(
            str(exc), provider=provider, model=model
        )
    if isinstance(exc, anthropic.BadRequestError):
        status = getattr(exc, "status_code", 400) or 400
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 500) or 500
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, anthropic.APIError):
        return ProviderUnavailableError(
            str(exc), provider=provider, model=model
        )

    return ProviderError(
        f"{type(exc).__name__}: {exc}", provider=provider, model=model
    )


def _error_event(err: ProviderError, *, sequence: int) -> LLMStreamEvent:
    return LLMStreamEvent(
        type=StreamEventType.PROVIDER_ERROR,
        error=err.to_dict(),
        sequence=sequence,
    )


__all__ = ["AnthropicAdapter"]
