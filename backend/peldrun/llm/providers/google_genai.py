# backend/peldrun/llm/providers/google_genai.py
"""
PELDRUN Native Google Gemini Provider Adapter.

Talks directly to the Gemini API via the modern ``google-genai`` SDK
(``from google import genai``), NOT the legacy ``google-generativeai``
package that Google deprecated in late 2025.

Supports both Gemini Developer API and Vertex AI deployment modes,
chosen by connection configuration (never by model ID):

    Gemini Developer API:
        ConnectionContext(base_url=None, api_key="...")

    Vertex AI:
        ConnectionContext(
            base_url=None,
            api_key=None,
            extra_params={
                "vertexai": True,
                "project": "my-project-id",
                "location": "us-central1",
            },
        )

Key differences from OpenAI / Anthropic that this adapter handles
----------------------------------------------------------------

1. **``contents`` instead of ``messages``.** Every message becomes a
   ``Content`` object with ``role: "user"`` or ``role: "model"``
   (Google uses ``"model"`` where OpenAI uses ``"assistant"``).

2. **``system_instruction`` is a top-level field.** The compiler
   extracts it from the message list; the adapter passes it via
   ``GenerateContentConfig(system_instruction=...)``.

3. **Generation options are wrapped in ``GenerateContentConfig``.**
   ``temperature``, ``top_p``, ``top_k``, ``max_output_tokens``,
   ``stop_sequences`` — all under one config object.

4. **Tool calls use ``FunctionCall`` parts.** The response contains
   ``Part(function_call=FunctionCall(name=..., args=...))`` — one
   complete call per part, not incremental deltas. Streaming yields
   the complete function call in a single chunk.

5. **Thinking control varies by model family:**
       * Gemini 2.5 Pro / Flash: ``thinking_config.thinking_budget`` (tokens)
       * Gemini 3.0 (future): ``thinking_config.thinking_level``
       * Gemini 2.0 Flash Thinking: always-on, no config
   The compiler emits the right shape per surface; the adapter passes
   it through.

6. **Structured output uses ``response_mime_type`` + ``response_schema``.**
   Not a nested ``response_format`` object like OpenAI.

7. **Finish reasons are Google-specific** (``STOP``, ``MAX_TOKENS``,
   ``SAFETY``, ``RECITATION``, ``MALFORMED_FUNCTION_CALL``, ...).
   The adapter maps each to the canonical OpenAI-compatible value.

8. **Content can be BLOCKED** by safety filters before generation even
   starts. The adapter surfaces this as a ``ProviderContentPolicyError``.

9. **Usage metadata uses different field names**
   (``prompt_token_count``, ``candidates_token_count``,
   ``thoughts_token_count``). Normalized to ``TokenUsage``.

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

from google import genai
from google.genai import types as google_types

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
    ProviderContentPolicyError,
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

logger = logging.getLogger("peldrun.llm.providers.google_genai")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Google's default generation cap when the caller did not set one.
#: Gemini accepts 8192 by default; keeping this conservative.
_DEFAULT_MAX_OUTPUT_TOKENS = 8192


#: Finish-reason mapping from Google's enum to OpenAI-compatible values
#: that PELDRUN uses internally.
_FINISH_REASON_MAP: Dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "MALFORMED_FUNCTION_CALL": "tool_error",
    "UNEXPECTED_TOOL_CALL": "tool_error",
    "TOO_MANY_TOOL_CALLS": "tool_error",
    "OTHER": "stop",
}


# ---------------------------------------------------------------------------
# Adapter profile
# ---------------------------------------------------------------------------


_PROFILE = AdapterProfile(
    provider_id="google_genai",
    api_surface="generate_content",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,      # via separate function_call parts
        forced_tool_choice=True,       # via ToolConfig
        vision=True,
        audio=True,                    # Gemini accepts audio
        structured_output=True,        # via response_schema
        usage_reporting=True,
        reasoning=True,
        prompt_caching=True,           # via cached_content
    ),
    reasoning_strategies=[ReasoningStrategy.GOOGLE_GENAI],
    tool_call_style=ToolCallStyle.GOOGLE_FUNCTION_CALL,
    supported_param_names=[],
    notes=(
        "Native Google Gemini adapter. Uses google-genai SDK. "
        "Supports Gemini Developer API and Vertex AI."
    ),
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GoogleGenAIAdapter:
    """Native Google Gemini adapter (Developer API + Vertex AI)."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "google_genai"

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
                    "GoogleGenAIAdapter requires either `context=` or "
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

        # Vertex AI mode is opt-in via extra_params. Not inferred from
        # the model ID, not inferred from the base URL.
        extra = self._connection.extra_params or {}
        self._use_vertexai = bool(extra.get("vertexai"))

        api_key = (
            self._credentials.get("api_key")
            or self._connection.api_key
            or ""
        )

        if self._use_vertexai:
            project = extra.get("project")
            location = extra.get("location") or "us-central1"
            if not project:
                raise ValueError(
                    "Vertex AI mode requires 'project' in "
                    "connection.extra_params."
                )
            self._client = genai.Client(
                vertexai=True,
                project=str(project),
                location=str(location),
            )
            logger.debug(
                "GoogleGenAIAdapter ready: vertexai=True project=%s location=%s",
                project,
                location,
            )
        else:
            if not api_key:
                # The SDK raises at construction time with an empty key.
                # Use a sentinel so failures surface at request time.
                api_key = "MISSING_API_KEY"
            self._client = genai.Client(api_key=api_key)
            logger.debug(
                "GoogleGenAIAdapter ready: vertexai=False"
            )

    # ------------------------------------------------------------------
    # ProviderAdapter: generate
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started_at = time.time()
        compiled = compile_request(request, self._profile)

        try:
            kwargs = self._build_generate_kwargs(compiled)
            response = await self._client.aio.models.generate_content(
                **kwargs
            )
        except Exception as exc:
            raise _map_google_exception(
                exc, provider=self.provider_id, model=compiled.model
            ) from exc

        completed_at = time.time()
        result = self._convert_generate_response(response, compiled.model)
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
            kwargs = self._build_generate_kwargs(compiled)
        except Exception as exc:
            yield _error_event(
                _map_google_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=1,
            )
            return

        seq = 0
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None
        # Track whether we saw any function_call parts to avoid emitting
        # a spurious MESSAGE_COMPLETE before they are captured.
        tool_calls_seen: List[ToolCall] = []

        try:
            stream = await self._client.aio.models.generate_content_stream(
                **kwargs
            )
        except Exception as exc:
            yield _error_event(
                _map_google_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        try:
            async for chunk in stream:
                # ---------------------------------------------------
                # Usage metadata (may arrive on the final chunk).
                # ---------------------------------------------------
                usage_md = getattr(chunk, "usage_metadata", None)
                if usage_md is not None:
                    converted = _convert_google_usage(usage_md)
                    if converted is not None:
                        captured_usage = converted

                # ---------------------------------------------------
                # Candidates.
                # ---------------------------------------------------
                candidates = getattr(chunk, "candidates", None) or []
                if not candidates:
                    # Possibly a prompt-level block. Check top level.
                    prompt_feedback = getattr(chunk, "prompt_feedback", None)
                    if prompt_feedback is not None:
                        block_reason = getattr(
                            prompt_feedback, "block_reason", None
                        )
                        if block_reason:
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.PROVIDER_ERROR,
                                error={
                                    "code": "content_policy_blocked",
                                    "message": (
                                        f"Gemini blocked the prompt: "
                                        f"{block_reason}"
                                    ),
                                    "retryable": False,
                                    "provider": self.provider_id,
                                },
                                sequence=seq,
                            )
                            return
                    continue

                first = candidates[0]
                content = getattr(first, "content", None)
                parts = (
                    getattr(content, "parts", None) if content else None
                ) or []

                for part in parts:
                    text_val = getattr(part, "text", None)
                    if isinstance(text_val, str) and text_val:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.CONTENT_DELTA,
                            content_delta=text_val,
                            sequence=seq,
                        )

                    # Thinking parts (Gemini 2.5+ surfaces reasoning as
                    # a distinct part flag).
                    if getattr(part, "thought", False):
                        # Some SDK versions put thinking in
                        # `part.text` with `thought=True`; others use a
                        # separate field. We re-emit as reasoning_delta
                        # since the text is already yielded above.
                        # To avoid duplication, we skip re-yielding.
                        # This branch exists for future SDK versions
                        # that split the fields.
                        pass

                    # Function calls — Google sends them complete.
                    fn_call = getattr(part, "function_call", None)
                    if fn_call is not None:
                        call_id = (
                            getattr(fn_call, "id", None)
                            or f"call_{len(tool_calls_seen)}"
                        )
                        name = getattr(fn_call, "name", None) or ""
                        args = getattr(fn_call, "args", None) or {}
                        if not isinstance(args, dict):
                            try:
                                args = dict(args)
                            except Exception:
                                args = {}

                        tool_calls_seen.append(
                            ToolCall(
                                id=str(call_id),
                                name=str(name),
                                arguments=args,
                                type="function",
                            )
                        )

                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_id=str(call_id),
                            tool_name=str(name),
                            tool_arguments_delta=None,
                            sequence=seq,
                        )
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.TOOL_CALL_COMPLETE,
                            tool_call_id=str(call_id),
                            tool_name=str(name),
                            tool_arguments_complete=args,
                            sequence=seq,
                        )

                # Finish reason.
                finish_reason_raw = getattr(first, "finish_reason", None)
                if finish_reason_raw:
                    finish_reason = _map_finish_reason(finish_reason_raw)

        except asyncio.CancelledError:
            try:
                await stream.aclose()
            except Exception:
                pass
            raise
        except Exception as exc:
            yield _error_event(
                _map_google_exception(
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
            finish_reason=finish_reason or "stop",
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
        List models via ``client.aio.models.list()``.

        Google's response includes:
            * ``name``: ``"models/gemini-2.0-flash-exp"``
            * ``display_name``
            * ``input_token_limit``, ``output_token_limit``
            * ``supported_generation_methods``: ``["generateContent", ...]``
            * ``description``

        We forward everything verbatim. The ``provider_live.py`` layer
        can hit ``/v1beta/models/{name}`` for per-model capability.
        """
        try:
            models_pager = await self._client.aio.models.list()
        except Exception as exc:
            logger.error(
                "Gemini model discovery failed: %s", exc, exc_info=True
            )
            return []

        results: List[Dict[str, Any]] = []
        try:
            async for model in models_pager:
                if model is None:
                    continue
                name = getattr(model, "name", None)
                if not isinstance(name, str) or not name:
                    continue
                # Strip "models/" prefix so callers get the short name.
                short = name.split("/", 1)[-1] if "/" in name else name

                # Safely extract supported_generation_methods.
                raw_methods = getattr(model, "supported_generation_methods", None)
                supported_methods = []
                if isinstance(raw_methods, list):
                    supported_methods = [str(m) for m in raw_methods if m]

                results.append(
                    {
                        "id": short,
                        "full_name": name,
                        "display_name": getattr(model, "display_name", None),
                        "description": getattr(model, "description", None),
                        "input_token_limit": getattr(
                            model, "input_token_limit", None
                        ),
                        "output_token_limit": getattr(
                            model, "output_token_limit", None
                        ),
                        "supported_generation_methods": supported_methods,
                    }
                )
        except Exception as exc:
            logger.warning("Gemini model iteration failed: %s", exc)

        return results

    # ------------------------------------------------------------------
    # ProviderAdapter: validate_connection
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        started_at = time.time()
        try:
            pager = await self._client.aio.models.list()
            # Consume one item to confirm the response is real.
            async for _ in pager:
                break
            latency_ms = round((time.time() - started_at) * 1000.0, 2)
            return {
                "ok": True,
                "message": "Gemini API reachable.",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round((time.time() - started_at) * 1000.0, 2)
            err = _map_google_exception(
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
        Fetch per-model metadata from ``models.get``.

        Returns context limits and supported generation methods. No
        capability information (Gemini does not declare vision/tools
        in this endpoint).
        """
        try:
            name = (
                model_id
                if model_id.startswith("models/")
                else f"models/{model_id}"
            )
            model = await self._client.aio.models.get(model=name)
        except Exception:
            return None

        return {
            "id": model_id,
            "full_name": getattr(model, "name", None),
            "display_name": getattr(model, "display_name", None),
            "input_token_limit": getattr(model, "input_token_limit", None),
            "output_token_limit": getattr(model, "output_token_limit", None),
            "supported_generation_methods": list(
                getattr(model, "supported_generation_methods", None) or []
            ),
        }

    # ------------------------------------------------------------------
    # ProviderAdapter: close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        # genai.Client uses an underlying http client that it manages.
        # The SDK exposes ``close()`` on the sync client; the async
        # client may not. Try both.
        try:
            close_fn = getattr(self._client, "close", None)
            if callable(close_fn):
                result = close_fn()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as exc:
            logger.debug("Gemini client close raised: %s", exc)

    # ==================================================================
    # Request building
    # ==================================================================

    def _build_generate_kwargs(
        self, compiled: CompiledRequest
    ) -> Dict[str, Any]:
        """Build kwargs for ``generate_content`` / ``generate_content_stream``."""
        # --------------------------------------------------------------
        # 1. Convert messages → Google contents.
        # --------------------------------------------------------------
        contents = self._convert_messages(compiled.messages)

        # --------------------------------------------------------------
        # 2. Build generation config.
        # --------------------------------------------------------------
        config_kwargs: Dict[str, Any] = {}

        # System prompt.
        if compiled.system_prompt:
            config_kwargs["system_instruction"] = compiled.system_prompt

        # Generation params.
        if "max_output_tokens" in compiled.generation:
            config_kwargs["max_output_tokens"] = int(
                compiled.generation["max_output_tokens"]
            )
        else:
            config_kwargs["max_output_tokens"] = _DEFAULT_MAX_OUTPUT_TOKENS

        if "temperature" in compiled.generation:
            config_kwargs["temperature"] = float(
                compiled.generation["temperature"]
            )
        if "top_p" in compiled.generation:
            config_kwargs["top_p"] = float(compiled.generation["top_p"])
        if "top_k" in compiled.generation:
            config_kwargs["top_k"] = int(compiled.generation["top_k"])
        if "stop" in compiled.generation:
            stop_val = compiled.generation["stop"]
            if isinstance(stop_val, list) and stop_val:
                config_kwargs["stop_sequences"] = [
                    str(s) for s in stop_val
                ]

        # --------------------------------------------------------------
        # 3. Tools.
        # --------------------------------------------------------------
        if compiled.tools:
            function_decls = self._convert_tools(compiled.tools)
            if function_decls:
                config_kwargs["tools"] = [
                    google_types.Tool(function_declarations=function_decls)
                ]
                # Tool choice.
                if compiled.tool_choice:
                    tool_config = self._convert_tool_choice(
                        compiled.tool_choice
                    )
                    if tool_config is not None:
                        config_kwargs["tool_config"] = tool_config

        # --------------------------------------------------------------
        # 4. Reasoning — Google uses ``thinking_config``.
        # --------------------------------------------------------------
        if compiled.reasoning:
            thinking_config = self._build_thinking_config(
                compiled.reasoning
            )
            if thinking_config is not None:
                config_kwargs["thinking_config"] = thinking_config

        # --------------------------------------------------------------
        # 5. Structured output.
        # --------------------------------------------------------------
        if compiled.response_format:
            rf_type = compiled.response_format.get("type")
            if rf_type in ("json_object", "json_schema"):
                config_kwargs["response_mime_type"] = "application/json"
                if rf_type == "json_schema":
                    js = compiled.response_format.get("json_schema", {})
                    schema = js.get("schema")
                    if isinstance(schema, dict):
                        config_kwargs["response_schema"] = schema

        # --------------------------------------------------------------
        # 6. Provider options (escape hatch).
        # --------------------------------------------------------------
        if compiled.provider_options:
            config_kwargs.update(compiled.provider_options)

        # Build the final config object.
        try:
            config = google_types.GenerateContentConfig(**config_kwargs)
        except Exception as exc:
            # Config construction can fail on invalid values (e.g.
            # out-of-range temperature). Surface a clear error rather
            # than a generic SDK exception.
            logger.debug(
                "GenerateContentConfig build failed: %s (kwargs=%s)",
                exc,
                config_kwargs,
            )
            raise

        return {
            "model": compiled.model,
            "contents": contents,
            "config": config,
        }

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Any]:
        """
        Convert PELDRUN/OpenAI-shaped messages to Google ``Content`` list.

        Role mapping:
            OpenAI ``user``      → Google ``user``
            OpenAI ``assistant`` → Google ``model``
            OpenAI ``tool``      → Google ``user`` (function_response)
        """
        contents: List[Any] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            # ---------------------------------------------------------
            # Tool results — Google expects function_response parts
            # inside a user message.
            # ---------------------------------------------------------
            if role == "tool":
                tool_call_id = msg.get("tool_call_id") or ""
                # Google's FunctionResponse needs a name, not an id.
                # We look up the tool name from the preceding assistant
                # message's tool_calls; when unavailable, use the id as
                # name (best-effort).
                tool_name = self._lookup_tool_name(
                    messages, str(tool_call_id)
                )
                text_content = self._content_to_text(content)
                try:
                    response_obj = (
                        json.loads(text_content)
                        if text_content
                        else {}
                    )
                except Exception:
                    response_obj = {"content": text_content}

                parts = [
                    google_types.Part(
                        function_response=google_types.FunctionResponse(
                            name=str(tool_name or tool_call_id or "tool"),
                            response=response_obj
                            if isinstance(response_obj, dict)
                            else {"content": str(response_obj)},
                        )
                    )
                ]
                contents.append(
                    google_types.Content(role="user", parts=parts)
                )
                continue

            # ---------------------------------------------------------
            # Assistant with tool_calls → model content with
            # function_call parts.
            # ---------------------------------------------------------
            tool_calls = msg.get("tool_calls")
            if (
                role == "assistant"
                and isinstance(tool_calls, list)
                and tool_calls
            ):
                parts: List[Any] = []
                if content:
                    text_val = self._content_to_text(content)
                    if text_val:
                        parts.append(
                            google_types.Part(text=text_val)
                        )
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or ""
                    raw_args = (
                        fn.get("arguments") or tc.get("arguments") or {}
                    )
                    if isinstance(raw_args, str):
                        try:
                            args_dict = json.loads(raw_args)
                        except Exception:
                            args_dict = {"raw": raw_args}
                    elif isinstance(raw_args, dict):
                        args_dict = raw_args
                    else:
                        args_dict = {}
                    parts.append(
                        google_types.Part(
                            function_call=google_types.FunctionCall(
                                name=str(name),
                                args=args_dict,
                            )
                        )
                    )
                if parts:
                    contents.append(
                        google_types.Content(role="model", parts=parts)
                    )
                continue

            # ---------------------------------------------------------
            # Plain user / assistant messages.
            # ---------------------------------------------------------
            google_role = (
                "model" if role == "assistant" else "user"
            )

            if isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        txt = part.get("text", "")
                        if isinstance(txt, str) and txt:
                            parts.append(
                                google_types.Part(text=txt)
                            )
                    elif ptype == "image_url":
                        blob = self._convert_image_part(part)
                        if blob is not None:
                            parts.append(blob)
                if parts:
                    contents.append(
                        google_types.Content(
                            role=google_role, parts=parts
                        )
                    )
                continue

            text = self._content_to_text(content)
            if text:
                contents.append(
                    google_types.Content(
                        role=google_role,
                        parts=[google_types.Part(text=text)],
                    )
                )

        return contents

    @staticmethod
    def _lookup_tool_name(
        messages: List[Dict[str, Any]], tool_call_id: str
    ) -> Optional[str]:
        """Find the tool name for a given tool_call_id in the history."""
        if not tool_call_id:
            return None
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                if tc.get("id") == tool_call_id:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name")
                    if name:
                        return str(name)
        return None

    @staticmethod
    def _convert_image_part(part: Dict[str, Any]) -> Optional[Any]:
        """
        Convert an OpenAI ``image_url`` part to a Google ``Part`` with
        ``inline_data`` (base64) or ``file_data`` (URL).
        """
        image_payload = part.get("image_url")
        if isinstance(image_payload, dict):
            url = image_payload.get("url")
        else:
            url = image_payload
        if not isinstance(url, str) or not url:
            return None

        if url.startswith("data:"):
            try:
                header, _, data = url.partition(",")
                media_part = header.split(";", 1)[0]
                mime_type = (
                    media_part.split(":", 1)[1]
                    if ":" in media_part
                    else "image/png"
                )
                import base64 as _b64

                raw_bytes = _b64.b64decode(data)
                return google_types.Part(
                    inline_data=google_types.Blob(
                        mime_type=mime_type,
                        data=raw_bytes,
                    )
                )
            except Exception:
                return None

        if url.startswith(("http://", "https://")):
            # Google does not accept arbitrary URLs directly; the SDK
            # will fetch if the caller uses file_data with an
            # appropriate MIME type. Without MIME type, we skip.
            return None

        return None

    @staticmethod
    def _content_to_text(content: Any) -> str:
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
    ) -> List[Any]:
        """
        Convert OpenAI-shaped tools to Google ``FunctionDeclaration``s.
        """
        result: List[Any] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if "function" in tool else tool
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not name:
                continue

            parameters = fn.get("parameters") or {
                "type": "object",
                "properties": {},
                "required": [],
            }
            try:
                result.append(
                    google_types.FunctionDeclaration(
                        name=str(name),
                        description=str(
                            fn.get("description") or ""
                        ),
                        parameters=parameters,
                    )
                )
            except Exception as exc:
                logger.debug(
                    "Skipping tool %r: %s", name, exc
                )
        return result

    @staticmethod
    def _convert_tool_choice(
        tool_choice: Dict[str, Any]
    ) -> Optional[Any]:
        """
        Convert OpenAI-shaped tool_choice to Google's ToolConfig.

        Google's ToolConfig supports:
            * ``FunctionCallingConfigMode.AUTO`` — model decides
            * ``FunctionCallingConfigMode.ANY`` — must call some tool
            * ``FunctionCallingConfigMode.NONE`` — must not call
            * ``allowed_function_names`` — restrict to specific tools
        """
        if not isinstance(tool_choice, dict):
            return None
        choice_type = tool_choice.get("type")

        try:
            if choice_type == "auto":
                return google_types.ToolConfig(
                    function_calling_config=google_types.FunctionCallingConfig(
                        mode=google_types.FunctionCallingConfigMode.AUTO,
                    )
                )
            if choice_type == "none":
                return google_types.ToolConfig(
                    function_calling_config=google_types.FunctionCallingConfig(
                        mode=google_types.FunctionCallingConfigMode.NONE,
                    )
                )
            if choice_type == "required":
                return google_types.ToolConfig(
                    function_calling_config=google_types.FunctionCallingConfig(
                        mode=google_types.FunctionCallingConfigMode.ANY,
                    )
                )
            if choice_type == "function":
                fn = tool_choice.get("function") or {}
                name = fn.get("name")
                if not name:
                    return None
                return google_types.ToolConfig(
                    function_calling_config=google_types.FunctionCallingConfig(
                        mode=google_types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[str(name)],
                    )
                )
        except Exception as exc:
            logger.debug(
                "Failed to build ToolConfig: %s", exc
            )
        return None

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------

    @staticmethod
    def _build_thinking_config(
        reasoning: Dict[str, Any]
    ) -> Optional[Any]:
        """
        Build a ``ThinkingConfig`` from the compiler's reasoning dict.

        Two shapes:
            * ``{"thinking_budget": N}``  → token budget (2.5 series)
            * ``{"thinking_level": "high"}`` → effort level (3.0+)

        Google's ``ThinkingConfig`` accepts both. Only one is sent
        based on which key the compiler provided.
        """
        if not isinstance(reasoning, dict):
            return None

        budget = reasoning.get("thinking_budget")
        level = reasoning.get("thinking_level")

        try:
            if isinstance(budget, int) and budget >= 0:
                return google_types.ThinkingConfig(
                    thinking_budget=budget
                )
            if isinstance(level, str) and level.strip():
                return google_types.ThinkingConfig(
                    thinking_level=level.upper()
                )
        except Exception as exc:
            logger.debug(
                "Failed to build ThinkingConfig: %s", exc
            )
        return None

    # ------------------------------------------------------------------
    # Response conversion (non-streaming)
    # ------------------------------------------------------------------

    def _convert_generate_response(
        self, response: Any, model: str
    ) -> LLMResponse:
        """
        Convert a ``GenerateContentResponse`` to ``LLMResponse``.
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []

        # ----------------------------------------------------------
        # Prompt-level block?
        # ----------------------------------------------------------
        prompt_feedback = getattr(response, "prompt_feedback", None)
        if prompt_feedback is not None:
            block_reason = getattr(prompt_feedback, "block_reason", None)
            if block_reason:
                raise ProviderContentPolicyError(
                    f"Gemini blocked the prompt: {block_reason}",
                    provider=self.provider_id,
                    model=model,
                )

        candidates = getattr(response, "candidates", None) or []
        finish_reason: Optional[str] = None

        if candidates:
            first = candidates[0]
            content = getattr(first, "content", None)
            parts = (
                getattr(content, "parts", None) if content else None
            ) or []

            for part in parts:
                text_val = getattr(part, "text", None)
                if isinstance(text_val, str) and text_val:
                    # Reasoning vs content split based on the
                    # `thought` flag if present.
                    if getattr(part, "thought", False):
                        reasoning_parts.append(text_val)
                    else:
                        content_parts.append(text_val)

                fn_call = getattr(part, "function_call", None)
                if fn_call is not None:
                    call_id = (
                        getattr(fn_call, "id", None)
                        or f"call_{len(tool_calls)}"
                    )
                    name = getattr(fn_call, "name", None) or ""
                    args = getattr(fn_call, "args", None) or {}
                    if not isinstance(args, dict):
                        try:
                            args = dict(args)
                        except Exception:
                            args = {}
                    tool_calls.append(
                        ToolCall(
                            id=str(call_id),
                            name=str(name),
                            arguments=args,
                            type="function",
                        )
                    )

            finish_reason_raw = getattr(first, "finish_reason", None)
            if finish_reason_raw:
                finish_reason = _map_finish_reason(finish_reason_raw)

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
            usage=_convert_google_usage(
                getattr(response, "usage_metadata", None)
            ),
            model=str(getattr(response, "model_version", model) or model),
            raw=response,
        )


# ---------------------------------------------------------------------------
# Finish reason mapping
# ---------------------------------------------------------------------------


def _map_finish_reason(raw: Any) -> str:
    """Map Google's finish_reason enum to canonical strings."""
    # The SDK returns an enum or a string. Normalize to uppercase
    # string then look up.
    if raw is None:
        return "stop"
    raw_str = (
        raw.name if hasattr(raw, "name") else str(raw)
    ).upper()
    return _FINISH_REASON_MAP.get(raw_str, "stop")


# ---------------------------------------------------------------------------
# Usage conversion
# ---------------------------------------------------------------------------


def _convert_google_usage(raw_usage: Any) -> Optional[TokenUsage]:
    """Google ``usage_metadata`` → ``TokenUsage``."""
    if raw_usage is None:
        return None

    inp = getattr(raw_usage, "prompt_token_count", None)
    out = getattr(raw_usage, "candidates_token_count", None)
    total = getattr(raw_usage, "total_token_count", None)
    cached = getattr(raw_usage, "cached_content_token_count", None)
    thoughts = getattr(raw_usage, "thoughts_token_count", None)

    if inp is None and out is None and total is None:
        return None

    return TokenUsage(
        input_tokens=int(inp) if inp is not None else None,
        output_tokens=int(out) if out is not None else None,
        total_tokens=int(total) if total is not None else (
            int(inp or 0) + int(out or 0)
        ),
        cached_input_tokens=int(cached) if cached is not None else None,
        reasoning_output_tokens=int(thoughts) if thoughts is not None else None,
        source=UsageSource.PROVIDER,
        estimated=False,
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_google_exception(
    exc: BaseException, *, provider: str, model: str
) -> ProviderError:
    """
    Map a google-genai SDK exception to the canonical taxonomy.

    The SDK wraps underlying ``google.api_core.exceptions.*`` and also
    defines its own ``google.genai.errors.*``. We use duck typing via
    class name + status_code so we do not depend on which internal
    module raised the exception.
    """
    if is_cancellation(exc):
        raise exc

    exc_name = type(exc).__name__

    # Direct name-based mapping (covers both api_core and genai.errors).
    if exc_name in ("Unauthenticated", "AuthenticationError"):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=401
        )
    if exc_name in ("PermissionDenied", "PermissionError"):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=403
        )
    if exc_name in ("ResourceExhausted", "RateLimitError", "TooManyRequests"):
        return ProviderRateLimitError(
            str(exc), provider=provider, model=model, status_code=429
        )
    if exc_name in ("DeadlineExceeded", "APITimeoutError", "TimeoutError"):
        return ProviderTimeoutError(
            str(exc), provider=provider, model=model, phase="request"
        )
    if exc_name in ("ServiceUnavailable", "InternalServerError", "Unavailable"):
        return ProviderUnavailableError(
            str(exc), provider=provider, model=model, status_code=503
        )
    if exc_name == "InvalidArgument":
        err = classify_http_status(
            400, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if exc_name == "NotFound":
        return classify_http_status(
            404, provider=provider, model=model, message=str(exc)
        )
    if exc_name in ("APIConnectionError", "ConnectError"):
        return ProviderConnectionError(
            str(exc), provider=provider, model=model
        )

    # Fall back to status_code if the SDK exposes one.
    status = getattr(exc, "status_code", None) or getattr(
        exc, "code", None
    )
    if isinstance(status, int):
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))

    return ProviderError(
        f"{type(exc).__name__}: {exc}", provider=provider, model=model
    )


def _error_event(err: ProviderError, *, sequence: int) -> LLMStreamEvent:
    return LLMStreamEvent(
        type=StreamEventType.PROVIDER_ERROR,
        error=err.to_dict(),
        sequence=sequence,
    )


__all__ = ["GoogleGenAIAdapter"]
