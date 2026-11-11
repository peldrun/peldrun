# backend/peldrun/llm/providers/openai_native.py
"""
PELDRUN Native OpenAI Provider Adapter.

The first native adapter in the multi-provider system. Talks directly
to OpenAI's SDK across BOTH of its surfaces:

    * Chat Completions  (``client.chat.completions.create``)
    * Responses API     (``client.responses.create``)

The API surface is chosen from ``connection.extra_params["api_surface"]``
or, when absent, from ``adapter_profile.api_surface``. **Never** from the
model ID — because two models from the same OpenAI account can require
different surfaces (e.g. ``o1`` on Responses, ``gpt-4o`` on Chat
Completions), and the plan explicitly forbids name-based inference.

Design invariants
-----------------

1. **Compiles internally.** The adapter receives an ``LLMRequest``,
   calls ``compile_request()`` to produce a ``CompiledRequest``, and
   then translates that into SDK kwargs. The adapter does not duplicate
   any capability logic — the compiler handles it.

2. **API surface is chosen, not guessed.** Comes from the connection
   configuration. Falls back to the profile default
   (``chat_completions``).

3. **Two error paths.** SDK-specific exceptions (``openai.AuthenticationError``,
   etc.) are mapped to the canonical ``ProviderError`` hierarchy defined
   in ``errors.py``. A message-based refinement layer catches the
   cases where the SDK misclassifies (a 400 that is really a
   context-length error, a 429 that is really a permanent quota
   exhaustion).

4. **Streaming is truthful.** Both surfaces stream natively. When the
   caller asks for stream=True we stream; we do not buffer. Every
   event carries a monotonic ``sequence`` so the downstream normalizer
   (M6) can preserve order when bridging to legacy ``StreamChunk``
   consumers.

5. **Cancellation propagates unwrapped.** The adapter closes the HTTP
   stream on ``asyncio.CancelledError``, then re-raises the cancellation
   untouched. Never wrapped in a ``ProviderError``.

6. **Usage is normalized.** Chat Completions returns
   ``prompt_tokens`` / ``completion_tokens``; Responses returns
   ``input_tokens`` / ``output_tokens``. Both are converted to the
   canonical ``TokenUsage`` shape used across PELDRUN.

7. **Tool calling works on both surfaces.** Chat Completions uses the
   classic ``tools`` + ``tool_calls`` shape. Responses uses the newer
   ``tools`` array of flattened function definitions and returns
   ``function_call`` output items. The adapter normalizes both back to
   the ``ToolCall`` shape the runtime expects.

See also
--------

    * ``backend/peldrun/llm/contracts.py``        — ProviderAdapter protocol
    * ``backend/peldrun/llm/request_compiler.py`` — compiled request
    * ``backend/peldrun/llm/errors.py``           — error taxonomy
    * ``backend/peldrun/llm/client.py``           — TokenUsage / ToolCall
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import openai
from openai import AsyncOpenAI

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
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_status,
    is_cancellation,
    refine_by_message,
)
from peldrun.llm.request import LLMRequest
from peldrun.llm.request_compiler import CompiledRequest, compile_request

logger = logging.getLogger("peldrun.llm.providers.openai_native")


# ---------------------------------------------------------------------------
# Adapter profile (class attribute — consumed by ProviderFactory)
# ---------------------------------------------------------------------------


#: OpenAI exposes every modern capability. The profile declares the
#: MAXIMUM surface area the adapter can carry; the actual per-model
#: intersection is decided by ``compute_resolved_capabilities``.
_PROFILE = AdapterProfile(
    provider_id="openai_native",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        forced_tool_choice=True,
        vision=True,
        audio=False,           # Not wired in this adapter version
        structured_output=True,
        usage_reporting=True,
        reasoning=True,
        prompt_caching=True,
    ),
    reasoning_strategies=[
        # Ordered by preference. The compiler tries each until one can
        # express the model's current mode.
        ReasoningStrategy.OPENAI_RESPONSES,
        ReasoningStrategy.OPENAI_CHAT_COMPLETIONS,
    ],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
    # No allowlist restriction — OpenAI rejects unknown keys itself.
    supported_param_names=[],
    notes=(
        "Native OpenAI adapter. Supports Chat Completions and the "
        "Responses API. Surface selection via "
        "connection.extra_params['api_surface']."
    ),
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenAINativeAdapter:
    """
    Native OpenAI adapter. Conforms to ``ProviderAdapter``.

    Instantiated by ``ProviderFactory._instantiate_v1``:

        OpenAINativeAdapter(context=ProviderContext(...))
        # or
        OpenAINativeAdapter(connection=ConnectionContext(...), profile=...)
    """

    #: Adapter protocol generation.
    ADAPTER_STYLE = "v1"

    #: Contract version — required by the registry.
    API_CONTRACT = "peldrun.llm.provider.v1"

    #: Profile consumed by the factory.
    PROFILE = _PROFILE

    #: Provider identifier (used by capability resolution and diagnostics).
    provider_id = "openai_native"

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
        # Support both constructor styles used by the factory.
        if context is not None:
            self._connection: ConnectionContext = context.connection
            self._profile: AdapterProfile = context.profile or _PROFILE
            self._credentials: Dict[str, str] = dict(context.credentials or {})
        else:
            if connection is None:
                raise TypeError(
                    "OpenAINativeAdapter requires either `context=` or "
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

        # Resolve base_url: explicit connection wins, else OpenAI default.
        resolved_base_url = (
            self._connection.base_url or "https://api.openai.com/v1"
        ).rstrip("/")
        # The OpenAI SDK appends its own path suffixes; we normalize any
        # caller-supplied trailing /v1 into the base_url the SDK expects.
        if not resolved_base_url.endswith("/v1"):
            resolved_base_url = f"{resolved_base_url}/v1"

        api_key = (
            self._credentials.get("api_key")
            or self._connection.api_key
            or "EMPTY"
        )

        # SDK client construction. One client per adapter instance —
        # the factory does NOT cache, so this is created per-run.
        self._client = AsyncOpenAI(
            base_url=resolved_base_url,
            api_key=api_key,
            timeout=float(self._connection.timeout or 120.0),
        )

        # API surface: explicit per-connection override wins, then the
        # profile default. Never inferred from the model ID.
        override = (self._connection.extra_params or {}).get("api_surface")
        if isinstance(override, str) and override.strip():
            self._api_surface = override.strip()
        else:
            self._api_surface = self._profile.api_surface or "chat_completions"

        logger.debug(
            "OpenAINativeAdapter ready: base_url=%s surface=%s",
            resolved_base_url,
            self._api_surface,
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: generate (non-streaming)
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """
        Execute a non-streaming completion on the configured surface.
        """
        started_at = time.time()
        compiled = compile_request(request, self._profile)

        try:
            if compiled.api_surface == "responses":
                response = await self._generate_responses(compiled)
            else:
                response = await self._generate_chat_completions(compiled)
        except Exception as exc:
            raise _map_openai_exception(
                exc, provider=self.provider_id, model=compiled.model
            ) from exc

        completed_at = time.time()
        response.latency_ms = round((completed_at - started_at) * 1000.0, 2)
        response.ttft_ms = response.latency_ms  # non-streaming: TTFT == total

        # Attach compile diagnostics for the Request Inspector.
        if response.raw is None:
            response.raw = {}
        if isinstance(response.raw, dict):
            response.raw.setdefault("_peldrun_diagnostics", compiled.diagnostics)

        return response

    # ------------------------------------------------------------------
    # ProviderAdapter: stream
    # ------------------------------------------------------------------

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Execute a streaming completion on the configured surface.
        """
        compiled = compile_request(request, self._profile)

        if compiled.api_surface == "responses":
            async for event in self._stream_responses(compiled):
                yield event
        else:
            async for event in self._stream_chat_completions(compiled):
                yield event

    # ------------------------------------------------------------------
    # ProviderAdapter: discover_models
    # ------------------------------------------------------------------

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        Return the model list from ``GET /v1/models``.

        OpenAI's response has only ``{id, object, created, owned_by}``.
        We forward this honestly — the model catalog layer (LiteLLM)
        fills in capability information later.
        """
        try:
            response = await self._client.models.list()
        except Exception as exc:
            logger.warning(
                "OpenAI model discovery failed: %s", exc
            )
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
                    "object": getattr(item, "object", "model"),
                    "created": getattr(item, "created", None),
                    "owned_by": getattr(item, "owned_by", None),
                }
            )
        return results

    # ------------------------------------------------------------------
    # ProviderAdapter: validate_connection
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        """Ping ``/v1/models`` and report latency/status."""
        started_at = time.time()
        try:
            await self._client.models.list()
            latency_ms = round((time.time() - started_at) * 1000.0, 2)
            return {
                "ok": True,
                "message": "OpenAI API reachable.",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round((time.time() - started_at) * 1000.0, 2)
            err = _map_openai_exception(
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
        OpenAI exposes no per-model metadata endpoint.

        Returns ``None``. The model catalog layer (LiteLLM) provides
        capability and pricing data for OpenAI models.
        """
        return None

    # ------------------------------------------------------------------
    # ProviderAdapter: close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release the underlying HTTP client. Idempotent."""
        try:
            await self._client.close()
        except Exception as exc:
            logger.debug("OpenAI client close raised: %s", exc)

    # ==================================================================
    # Chat Completions path
    # ==================================================================

    async def _generate_chat_completions(
        self, compiled: CompiledRequest
    ) -> LLMResponse:
        kwargs = self._build_chat_completions_kwargs(compiled)
        response = await self._client.chat.completions.create(**kwargs)
        return self._convert_chat_completion_response(response, compiled.model)

    def _build_chat_completions_kwargs(
        self, compiled: CompiledRequest
    ) -> Dict[str, Any]:
        """Assemble the SDK kwargs for Chat Completions."""
        messages: List[Dict[str, Any]] = []
        if compiled.system_prompt:
            messages.append(
                {"role": "system", "content": compiled.system_prompt}
            )
        messages.extend(compiled.messages)

        kwargs: Dict[str, Any] = {
            "model": compiled.model,
            "messages": messages,
        }

        # Generation params.
        for key in (
            "max_tokens",
            "temperature",
            "top_p",
            "stop",
            "seed",
            "frequency_penalty",
            "presence_penalty",
        ):
            if key in compiled.generation and compiled.generation[key] is not None:
                kwargs[key] = compiled.generation[key]

        # Tools.
        #
        # [FIX-TOOL-CHOICE-WIRE-FORMAT] Same rationale as
        # ``lmstudio_native.py::_build_body``. The OpenAI Python SDK
        # and the OpenAI REST API both require the generic modes as
        # plain strings; the legacy ``{"type": "auto"}`` object form is
        # rejected with HTTP 400. The compiler emits the object form
        # for plugin compatibility (GigaChat etc.), so this adapter
        # translates it locally.
        if compiled.tools:
            kwargs["tools"] = compiled.tools
            if compiled.tool_choice:
                _tc = compiled.tool_choice
                if isinstance(_tc, dict) and _tc.get("type") in (
                    "auto",
                    "none",
                    "required",
                ):
                    kwargs["tool_choice"] = _tc["type"]
                else:
                    kwargs["tool_choice"] = _tc

        # Response format.
        if compiled.response_format:
            kwargs["response_format"] = compiled.response_format

        # Reasoning (Chat Completions): the compiler emits
        # ``{"reasoning_effort": "..."}``. OpenAI's SDK does not have
        # an explicit parameter — it must go through ``extra_body``.
        if compiled.reasoning:
            effort = compiled.reasoning.get("reasoning_effort")
            if effort:
                extra_body = dict(kwargs.get("extra_body") or {})
                extra_body["reasoning_effort"] = effort
                kwargs["extra_body"] = extra_body

        # Provider options merge (escape hatch).
        if compiled.provider_options:
            kwargs.update(compiled.provider_options)

        return kwargs

    def _convert_chat_completion_response(
        self,
        response: Any,
        model: str,
    ) -> LLMResponse:
        """Convert ``ChatCompletion`` to ``LLMResponse``."""
        choices = getattr(response, "choices", None) or []
        if not choices:
            return LLMResponse(
                content="",
                model=getattr(response, "model", model),
                raw=response,
                usage=_convert_chat_usage(getattr(response, "usage", None)),
                finish_reason=None,
            )

        first = choices[0]
        message = getattr(first, "message", None)
        content = (
            getattr(message, "content", None) if message is not None else None
        )
        # Content may be a list of parts (rare) — flatten to text.
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )

        # Reasoning extraction — OpenAI's Chat Completions returns
        # reasoning tokens (count) but not the reasoning text.
        reasoning_text: Optional[str] = None
        if message is not None:
            extra = getattr(message, "model_extra", None) or {}
            for key in ("reasoning_content", "reasoning", "thinking"):
                val = extra.get(key) if isinstance(extra, dict) else None
                if isinstance(val, str) and val:
                    reasoning_text = val
                    break

        # Tool calls.
        tool_calls = self._extract_tool_calls_from_message(message)

        finish_reason = getattr(first, "finish_reason", None)

        return LLMResponse(
            content=content or "",
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_convert_chat_usage(getattr(response, "usage", None)),
            model=getattr(response, "model", model),
            raw=response,
        )

    async def _stream_chat_completions(
        self, compiled: CompiledRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """Stream Chat Completions as ``LLMStreamEvent``."""
        kwargs = self._build_chat_completions_kwargs(compiled)
        kwargs["stream"] = True
        # Request usage in the final chunk.
        kwargs["stream_options"] = {"include_usage": True}

        seq = 0
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            yield _error_event(
                _map_openai_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        # Track tool-call deltas by index so we can emit
        # TOOL_CALL_COMPLETE only once per tool.
        active_tools: Dict[int, Dict[str, Any]] = {}
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None
        # [P2-18 FIX — Terminal marker tracking]
        # Same pattern as P1-13 in ollama_native.py / lmstudio_native.py.
        # OpenAI's Chat Completions streaming contract requires every
        # well-formed stream to end with a chunk whose ``finish_reason``
        # is set. If the loop exits WITHOUT observing one, the stream
        # was truncated (network drop, provider crash, mid-SSE cut).
        # Emitting ``finish_reason="stop"`` in that case silently
        # reports truncation as success.
        saw_finish_reason: bool = False

        try:
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    captured_usage = _convert_chat_usage(chunk_usage)
                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.USAGE_UPDATE,
                        usage=captured_usage,
                        sequence=seq,
                    )

                if not choices:
                    continue

                first = choices[0]
                delta = getattr(first, "delta", None)
                if delta is not None:
                    # Content delta.
                    content_piece = getattr(delta, "content", None)
                    if isinstance(content_piece, str) and content_piece:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.CONTENT_DELTA,
                            content_delta=content_piece,
                            sequence=seq,
                        )

                    # Reasoning delta.
                    reasoning_piece: Optional[str] = None
                    extra = getattr(delta, "model_extra", None) or {}
                    if isinstance(extra, dict):
                        for key in ("reasoning_content", "reasoning", "thinking"):
                            val = extra.get(key)
                            if isinstance(val, str) and val:
                                reasoning_piece = val
                                break
                    if reasoning_piece:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.REASONING_DELTA,
                            reasoning_delta=reasoning_piece,
                            sequence=seq,
                        )

                    # Tool call deltas.
                    raw_tc_deltas = getattr(delta, "tool_calls", None) or []
                    for tc_delta in raw_tc_deltas:
                        tool_index = getattr(tc_delta, "index", 0) or 0
                        fn = getattr(tc_delta, "function", None)

                        if tool_index not in active_tools:
                            tool_id = getattr(tc_delta, "id", None) or f"call_{tool_index}"
                            tool_name = (
                                getattr(fn, "name", None) if fn else None
                            ) or ""
                            active_tools[tool_index] = {
                                "id": tool_id,
                                "name": tool_name,
                                "arguments": "",
                            }
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_id=tool_id,
                                tool_name=tool_name,
                                tool_index=tool_index,
                                sequence=seq,
                            )
                        else:
                            # Preserve ID if it arrived late.
                            if getattr(tc_delta, "id", None):
                                active_tools[tool_index]["id"] = tc_delta.id

                        if fn is not None:
                            args_piece = getattr(fn, "arguments", None)
                            if isinstance(args_piece, str) and args_piece:
                                active_tools[tool_index]["arguments"] += args_piece
                                seq += 1
                                yield LLMStreamEvent(
                                    type=StreamEventType.TOOL_CALL_DELTA,
                                    tool_call_id=active_tools[tool_index]["id"],
                                    tool_name=active_tools[tool_index]["name"],
                                    tool_index=tool_index,
                                    tool_arguments_delta=args_piece,
                                    sequence=seq,
                                )

                chunk_finish = getattr(first, "finish_reason", None)
                if chunk_finish:
                    finish_reason = chunk_finish
                    # [P2-18 FIX] Terminal marker observed.
                    saw_finish_reason = True

        except asyncio.CancelledError:
            # Cancel propagation — close the stream and re-raise.
            try:
                await stream.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            yield _error_event(
                _map_openai_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        # Emit TOOL_CALL_COMPLETE for each accumulated tool.
        for tool_idx in sorted(active_tools.keys()):
            tool = active_tools[tool_idx]
            parsed_args: Any = tool["arguments"]
            if isinstance(parsed_args, str) and parsed_args:
                try:
                    parsed_args = json.loads(parsed_args)
                except Exception:
                    parsed_args = {"raw": parsed_args}
            elif not parsed_args:
                parsed_args = {}
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call_id=tool["id"],
                tool_name=tool["name"],
                tool_index=tool_idx,
                tool_arguments_complete=parsed_args,
                sequence=seq,
            )

        # [P2-18 FIX — Honest terminal marker]
        # The pre-fix code emitted ``finish_reason=finish_reason or "stop"``
        # even when no ``finish_reason`` was ever observed. That
        # silently reported truncated streams as "completed", so
        # consumers (StreamNormalizer, ToolCallAgent) accepted
        # partial output as final and never retried.
        #
        # Post-fix:
        #   * saw_finish_reason=True  → emit the real reason, or
        #     "stop" when a provider sent an empty string.
        #   * saw_finish_reason=False → emit "incomplete" so
        #     ToolCallAgent.think retries via its INCOMPLETE_STREAM
        #     path (see E.1 in llm/client.py).
        if saw_finish_reason:
            _effective_finish_reason = finish_reason or "stop"
        else:
            _effective_finish_reason = "incomplete"
            logger.warning(
                "[P2-18] OpenAI Chat Completions stream ended "
                "WITHOUT a terminal finish_reason (model=%s). "
                "Emitting finish_reason='incomplete' so consumers "
                "can retry with a fresh attempt.",
                compiled.model,
            )

        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=_effective_finish_reason,
            usage=captured_usage,
            sequence=seq,
        )

    @staticmethod
    def _extract_tool_calls_from_message(
        message: Any,
    ) -> List[ToolCall]:
        """Convert ``message.tool_calls`` to ``ToolCall`` list."""
        if message is None:
            return []
        raw_calls = getattr(message, "tool_calls", None) or []
        result: List[ToolCall] = []
        for idx, tc in enumerate(raw_calls):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", None) or ""
            raw_args = getattr(fn, "arguments", None)
            parsed_args: Any
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            result.append(
                ToolCall(
                    id=getattr(tc, "id", None) or f"call_{idx}",
                    name=name,
                    arguments=parsed_args,
                    type="function",
                )
            )
        return result

    # ==================================================================
    # Responses API path
    # ==================================================================

    async def _generate_responses(
        self, compiled: CompiledRequest
    ) -> LLMResponse:
        kwargs = self._build_responses_kwargs(compiled)
        response = await self._client.responses.create(**kwargs)
        return self._convert_responses_response(response, compiled.model)

    def _build_responses_kwargs(
        self, compiled: CompiledRequest
    ) -> Dict[str, Any]:
        """
        Assemble the SDK kwargs for the Responses API.

        Key shape differences vs Chat Completions:

        * ``messages`` → ``input`` (same message list format).
        * ``system_prompt`` → ``instructions``.
        * ``max_tokens`` → ``max_output_tokens``.
        * Reasoning is ``reasoning={"effort": "..."}`` (not extra_body).
        * Tools are FLAT function definitions (no nested ``function`` key).
        """
        kwargs: Dict[str, Any] = {
            "model": compiled.model,
            "input": compiled.messages,
        }
        if compiled.system_prompt:
            kwargs["instructions"] = compiled.system_prompt

        # Generation.
        if "max_output_tokens" in compiled.generation:
            kwargs["max_output_tokens"] = compiled.generation["max_output_tokens"]
        # Only pass temperature when the compiler kept it (i.e. no
        # conflict with reasoning).
        for key in ("temperature", "top_p"):
            if key in compiled.generation:
                kwargs[key] = compiled.generation[key]

        # Reasoning.
        if compiled.reasoning:
            # Responses API shape: {"effort": "high"} or
            # {"effort": "medium", "summary": "auto"}.
            if "effort" in compiled.reasoning:
                kwargs["reasoning"] = {"effort": compiled.reasoning["effort"]}

        # Tools — flatten to Responses shape.
        if compiled.tools:
            flat_tools: List[Dict[str, Any]] = []
            for tool in compiled.tools:
                fn = tool.get("function", {}) if isinstance(tool, dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                flat_tools.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get(
                            "parameters",
                            {"type": "object", "properties": {}, "required": []},
                        ),
                    }
                )
            if flat_tools:
                kwargs["tools"] = flat_tools
                if compiled.tool_choice:
                    # Responses API tool_choice follows the same
                    # string-for-generic-modes rule as Chat Completions.
                    _tc = compiled.tool_choice
                    if isinstance(_tc, dict) and _tc.get("type") in (
                        "auto",
                        "none",
                        "required",
                    ):
                        kwargs["tool_choice"] = _tc["type"]
                    else:
                        kwargs["tool_choice"] = _tc

        # Response format — Responses uses "text.format" instead of
        # "response_format".
        if compiled.response_format:
            rf = compiled.response_format
            if rf.get("type") == "json_object":
                kwargs["text"] = {"format": {"type": "json_object"}}
            elif rf.get("type") == "json_schema":
                js = rf.get("json_schema", {})
                kwargs["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": js.get("name", "response"),
                        "schema": js.get("schema", {}),
                        "strict": js.get("strict", False),
                    }
                }

        # Provider options merge.
        if compiled.provider_options:
            kwargs.update(compiled.provider_options)

        return kwargs

    def _convert_responses_response(
        self, response: Any, model: str
    ) -> LLMResponse:
        """
        Convert a Responses API result to ``LLMResponse``.

        The Responses API returns:
            ``output`` = list of items:
              - ``{"type": "reasoning", "summary": [...]}``
              - ``{"type": "message", "content": [{"type": "output_text", "text": "..."}]}``
              - ``{"type": "function_call", "name": ..., "arguments": "...", "call_id": "..."}``
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        finish_reason: Optional[str] = None

        output = getattr(response, "output", None) or []
        for item in output:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", None) == "output_text":
                        txt = getattr(part, "text", None)
                        if isinstance(txt, str) and txt:
                            content_parts.append(txt)
            elif item_type == "reasoning":
                for summary_item in getattr(item, "summary", None) or []:
                    txt = getattr(summary_item, "text", None)
                    if isinstance(txt, str) and txt:
                        reasoning_parts.append(txt)
            elif item_type == "function_call":
                raw_args = getattr(item, "arguments", None)
                parsed_args: Any
                if isinstance(raw_args, str) and raw_args:
                    try:
                        parsed_args = json.loads(raw_args)
                    except Exception:
                        parsed_args = {"raw": raw_args}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {}
                tool_calls.append(
                    ToolCall(
                        id=getattr(item, "call_id", None) or "call",
                        name=getattr(item, "name", None) or "",
                        arguments=parsed_args,
                        type="function",
                    )
                )

        # Finish reason — Responses reports it on the top-level response
        # as ``status`` (``"completed"``, ``"incomplete"``, etc.).
        status = getattr(response, "status", None)
        if status == "completed":
            finish_reason = "stop"
        elif status == "incomplete":
            finish_reason = "length"
        else:
            finish_reason = status

        reasoning_text = (
            "".join(reasoning_parts) if reasoning_parts else None
        )
        content_text = "".join(content_parts)

        return LLMResponse(
            content=content_text,
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_convert_responses_usage(getattr(response, "usage", None)),
            model=getattr(response, "model", model),
            raw=response,
        )

    async def _stream_responses(
        self, compiled: CompiledRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Stream Responses API events as ``LLMStreamEvent``.

        OpenAI's Responses streaming emits a fine-grained event stream.
        We map the events that carry user-visible data:

            ``response.output_text.delta``           → CONTENT_DELTA
            ``response.reasoning_summary_text.delta`` → REASONING_DELTA
            ``response.output_item.added`` (function) → TOOL_CALL_START
            ``response.function_call_arguments.delta`` → TOOL_CALL_DELTA
            ``response.output_item.done`` (function)  → TOOL_CALL_COMPLETE
            ``response.completed``                    → USAGE_UPDATE + MESSAGE_COMPLETE
        """
        kwargs = self._build_responses_kwargs(compiled)
        kwargs["stream"] = True

        seq = 0
        try:
            stream = await self._client.responses.create(**kwargs)
        except Exception as exc:
            yield _error_event(
                _map_openai_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        # Map call_id → state for function calls in flight.
        active_tools: Dict[str, Dict[str, Any]] = {}
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None
        # [P2-18 FIX — Terminal marker tracking]
        # The Responses API's terminal marker is the
        # ``response.completed`` event. If we never observe it, the
        # stream was truncated mid-response and we must not claim
        # success. Identical concern to the Chat Completions path
        # above, different wire format.
        saw_terminal_marker: bool = False

        try:
            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", None)
                    if isinstance(delta, str) and delta:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.CONTENT_DELTA,
                            content_delta=delta,
                            sequence=seq,
                        )

                elif event_type == "response.reasoning_summary_text.delta":
                    delta = getattr(event, "delta", None)
                    if isinstance(delta, str) and delta:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.REASONING_DELTA,
                            reasoning_delta=delta,
                            sequence=seq,
                        )

                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", None) == "function_call":
                        call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
                        name = getattr(item, "name", None) or ""
                        active_tools[call_id] = {
                            "id": call_id,
                            "name": name,
                            "arguments": "",
                        }
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_id=call_id,
                            tool_name=name,
                            sequence=seq,
                        )

                elif event_type == "response.function_call_arguments.delta":
                    call_id = getattr(event, "call_id", None) or getattr(event, "item_id", "")
                    delta = getattr(event, "delta", None)
                    if (
                        isinstance(delta, str)
                        and delta
                        and call_id in active_tools
                    ):
                        active_tools[call_id]["arguments"] += delta
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call_id=call_id,
                            tool_name=active_tools[call_id]["name"],
                            tool_arguments_delta=delta,
                            sequence=seq,
                        )

                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", None) == "function_call":
                        call_id = getattr(item, "call_id", None) or getattr(item, "id", "")
                        if call_id in active_tools:
                            raw_args = active_tools[call_id]["arguments"]
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
                                tool_call_id=call_id,
                                tool_name=active_tools[call_id]["name"],
                                tool_arguments_complete=parsed_args,
                                sequence=seq,
                            )
                            del active_tools[call_id]

                elif event_type == "response.completed":
                    response_obj = getattr(event, "response", None)
                    if response_obj is not None:
                        # [P2-18 FIX] Terminal marker observed.
                        saw_terminal_marker = True
                        # [P2-18 FIX — Dict-or-object tolerance]
                        # The real OpenAI SDK returns an
                        # attribute-bearing Pydantic object, but
                        # proxies, wrappers, and test doubles may
                        # hand back a plain dict. Accept both
                        # shapes so the status check works
                        # regardless. Without this, a dict input
                        # silently produces ``finish_reason=None``
                        # and the stream is misclassified as
                        # truncated even though it completed.
                        _resp_usage = (
                            response_obj.get("usage")
                            if isinstance(response_obj, dict)
                            else getattr(response_obj, "usage", None)
                        )
                        captured_usage = _convert_responses_usage(
                            _resp_usage
                        )
                        status = (
                            response_obj.get("status")
                            if isinstance(response_obj, dict)
                            else getattr(response_obj, "status", None)
                        )
                        if status == "completed":
                            finish_reason = "stop"
                        elif status == "incomplete":
                            finish_reason = "length"
                        else:
                            finish_reason = status

                elif event_type == "response.failed":
                    response_obj = getattr(event, "response", None)
                    error = (
                        getattr(response_obj, "error", None)
                        if response_obj is not None
                        else None
                    )
                    message = (
                        getattr(error, "message", None)
                        if error is not None
                        else "Responses API reported failure."
                    )
                    seq += 1
                    yield LLMStreamEvent(
                        type=StreamEventType.PROVIDER_ERROR,
                        error={
                            "code": "responses_api_failed",
                            "message": str(message),
                            "retryable": False,
                            "provider": self.provider_id,
                        },
                        sequence=seq,
                    )
                    return

        except asyncio.CancelledError:
            try:
                await stream.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            yield _error_event(
                _map_openai_exception(
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

        # [P2-18 FIX — Honest terminal marker, trust-the-marker]
        # The presence of ``response.completed`` is sufficient proof
        # that the stream ended cleanly. ``finish_reason`` might
        # still be None when the SDK omitted ``status`` (or when a
        # proxy returned a partial dict) — in that case we MUST
        # default to "stop", not "incomplete". The historical
        # ``saw_terminal_marker AND finish_reason`` conjunction
        # incorrectly reported cleanly-terminated streams as
        # truncated whenever the status field was unhelpful.
        if saw_terminal_marker:
            _effective_finish_reason = finish_reason or "stop"
        else:
            _effective_finish_reason = "incomplete"
            logger.warning(
                "[P2-18] OpenAI Responses stream ended WITHOUT "
                "a 'response.completed' event (model=%s). "
                "Emitting finish_reason='incomplete' so consumers "
                "can retry with a fresh attempt.",
                compiled.model,
            )

        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=_effective_finish_reason,
            usage=captured_usage,
            sequence=seq,
        )


# ---------------------------------------------------------------------------
# Usage conversion
# ---------------------------------------------------------------------------


def _convert_chat_usage(raw_usage: Any) -> Optional[TokenUsage]:
    """Chat Completions usage → ``TokenUsage``."""
    if raw_usage is None:
        return None

    inp = getattr(raw_usage, "prompt_tokens", None)
    out = getattr(raw_usage, "completion_tokens", None)
    total = getattr(raw_usage, "total_tokens", None)

    cached: Optional[int] = None
    prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
    if prompt_details is not None:
        cached = getattr(prompt_details, "cached_tokens", None)

    reasoning: Optional[int] = None
    comp_details = getattr(raw_usage, "completion_tokens_details", None)
    if comp_details is not None:
        reasoning = getattr(comp_details, "reasoning_tokens", None)

    if inp is None and out is None and total is None:
        return None

    return TokenUsage(
        input_tokens=int(inp) if inp is not None else None,
        output_tokens=int(out) if out is not None else None,
        total_tokens=int(total) if total is not None else (
            int(inp or 0) + int(out or 0)
        ),
        cached_input_tokens=int(cached) if cached is not None else None,
        reasoning_output_tokens=int(reasoning) if reasoning is not None else None,
        source=UsageSource.PROVIDER,
        estimated=False,
    )


def _convert_responses_usage(raw_usage: Any) -> Optional[TokenUsage]:
    """Responses API usage → ``TokenUsage``."""
    if raw_usage is None:
        return None

    inp = getattr(raw_usage, "input_tokens", None)
    out = getattr(raw_usage, "output_tokens", None)
    total = getattr(raw_usage, "total_tokens", None)

    cached: Optional[int] = None
    inp_details = getattr(raw_usage, "input_tokens_details", None)
    if inp_details is not None:
        cached = getattr(inp_details, "cached_tokens", None)

    reasoning: Optional[int] = None
    out_details = getattr(raw_usage, "output_tokens_details", None)
    if out_details is not None:
        reasoning = getattr(out_details, "reasoning_tokens", None)

    if inp is None and out is None and total is None:
        return None

    return TokenUsage(
        input_tokens=int(inp) if inp is not None else None,
        output_tokens=int(out) if out is not None else None,
        total_tokens=int(total) if total is not None else (
            int(inp or 0) + int(out or 0)
        ),
        cached_input_tokens=int(cached) if cached is not None else None,
        reasoning_output_tokens=int(reasoning) if reasoning is not None else None,
        source=UsageSource.PROVIDER,
        estimated=False,
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_openai_exception(
    exc: BaseException, *, provider: str, model: str
) -> ProviderError:
    """
    Translate an OpenAI SDK exception into the canonical taxonomy.

    Cancellation must be handled by the CALLER (this function is only
    called for non-cancellation errors).
    """
    if is_cancellation(exc):
        # Defensive — caller should have caught this earlier.
        raise exc

    # SDK-specific typed exceptions.
    if isinstance(exc, openai.AuthenticationError):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=401
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=403
        )
    if isinstance(exc, openai.RateLimitError):
        retry_after: Optional[float] = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            hdr = None
            try:
                hdr = resp.headers.get("retry-after")
            except Exception:
                pass
            if hdr:
                try:
                    retry_after = float(hdr)
                except (TypeError, ValueError):
                    pass
        err = ProviderRateLimitError(
            str(exc),
            provider=provider,
            model=model,
            status_code=429,
            retry_after_seconds=retry_after,
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError(
            str(exc), provider=provider, model=model, phase="request"
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderConnectionError(
            str(exc), provider=provider, model=model
        )
    if isinstance(exc, openai.BadRequestError):
        status = getattr(exc, "status_code", 400) or 400
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 500) or 500
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, openai.APIError):
        return ProviderUnavailableError(
            str(exc), provider=provider, model=model
        )

    # Generic — wrap with a stable message.
    return ProviderError(
        f"{type(exc).__name__}: {exc}", provider=provider, model=model
    )


def _error_event(err: ProviderError, *, sequence: int) -> LLMStreamEvent:
    """Wrap a ``ProviderError`` in a ``PROVIDER_ERROR`` stream event."""
    return LLMStreamEvent(
        type=StreamEventType.PROVIDER_ERROR,
        error=err.to_dict(),
        sequence=sequence,
    )


__all__ = ["OpenAINativeAdapter"]
