# backend/peldrun/llm/providers/ollama_native.py
"""
PELDRUN Native Ollama Provider Adapter.

Talks directly to Ollama's native HTTP API at ``/api/chat``. No SDK
dependency — Ollama exposes a small, stable REST surface that is
better handled with ``httpx`` directly than through any wrapper.

Key differences from OpenAI / Anthropic / Google that this adapter
handles
------------------------------------------------------------------

1. **NDJSON streaming.** Ollama streams newline-delimited JSON, not
   SSE and not chunked SDK objects. Each line is a complete JSON
   document::

       {"model": "llama3.2:7b", "message": {"role": "assistant",
        "content": "Hello"}, "done": false}
       ...
       {"model": "llama3.2:7b", "message": {"role": "assistant",
        "content": ""}, "done": true, "done_reason": "stop",
        "prompt_eval_count": 25, "eval_count": 10}

   The adapter parses these lines manually.

2. **Generation params live under ``options``.** Ollama does NOT
   accept ``temperature`` at the top level. It accepts a flat
   ``options`` object::

       "options": {
         "temperature": 0.7,
         "num_predict": 1024,   # = max_tokens
         "top_p": 1.0,
         "top_k": 40,
         "stop": ["..."]
       }

   The adapter renames ``max_tokens`` → ``num_predict``.

3. **Thinking is a top-level ``think`` field.** Ollama 0.5+ accepts::

       "think": true          # boolean toggle
       "think": "low"         # effort level
       "think": "medium"
       "think": "high"

   The thinking output arrives in ``message.thinking`` (a string),
   NOT in ``message.content``. The adapter surfaces it as
   ``REASONING_DELTA``.

4. **Tool calls are COMPLETE objects, not deltas.** Unlike OpenAI,
   Ollama sends ``tool_calls`` as a complete array in a single
   chunk. The adapter emits ``TOOL_CALL_START`` + ``TOOL_CALL_COMPLETE``
   for each call.

5. **``function.arguments`` is a dict, not a JSON string.**
   Ollama returns the parsed arguments directly. OpenAI returns them
   as a string that must be ``json.loads``-ed.

6. **Images are base64 strings, not URLs.** The ``images`` field on a
   message is a list of base64 strings (no data URI prefix). The
   adapter converts OpenAI's ``image_url`` parts (which carry a
   ``data:image/...;base64,XXX`` URI) to Ollama's format.

7. **No system prompt field.** Ollama expects the system prompt as
   a ``role: "system"`` message inside ``messages``. The adapter
   re-inserts the compiler-extracted system prompt at the top.

8. **Structured output is ``format: "json"`` only.** Ollama does
   NOT support JSON schema enforcement. The adapter refuses to send
   ``response_format`` and records a diagnostic.

9. **Usage field names differ:**
       ``prompt_eval_count``    → input_tokens
       ``eval_count``           → output_tokens
       (no total field — computed)

10. **Base URL must NOT include ``/v1``.** Ollama's native API is
    served at the root, while the OpenAI-compat endpoint is at
    ``/v1``. The adapter strips ``/v1`` so the caller can paste
    either form.

See also
--------

    * ``backend/peldrun/llm/contracts.py``        — ProviderAdapter protocol
    * ``backend/peldrun/llm/request_compiler.py`` — compiled request
    * ``backend/peldrun/llm/errors.py``           — error taxonomy
    * ``backend/peldrun/llm/catalog/provider_live.py`` — Ollama capability query
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

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
    ProviderConnectionError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_status,
    is_cancellation,
    refine_by_message,
)
from peldrun.llm.request import LLMRequest
from peldrun.llm.request_compiler import CompiledRequest, compile_request

logger = logging.getLogger("peldrun.llm.providers.ollama_native")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default Ollama base URL when the connection did not specify one.
_DEFAULT_BASE_URL = "http://localhost:11434"


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------
#
# Ollama exposes two surfaces under one server root: ``/v1/*`` (OpenAI
# compat shim) and ``/api/*`` (native). Callers routinely paste the base
# URL with one of these suffixes attached; the previous implementation
# only checked ``.endswith("/v1")`` and would leave ``/api`` behind when
# the caller's value was ``.../api/v1``.
_PROVIDER_SUFFIX_PATTERN = re.compile(
    r"/(?:api/v0|api/v1|openai/v1|openai|v1beta|v1|api)/?$",
    re.IGNORECASE,
)


def _normalize_ollama_root(base_url: str) -> str:
    """Return the Ollama server root, with any endpoint suffix removed."""
    cleaned = (base_url or "").strip().rstrip("/")
    while "/api/api/" in cleaned:
        cleaned = cleaned.replace("/api/api/", "/api/")
    while True:
        stripped = _PROVIDER_SUFFIX_PATTERN.sub("", cleaned)
        if stripped == cleaned or not stripped:
            break
        cleaned = stripped.rstrip("/")
    return cleaned


#: Default request timeout.
_DEFAULT_TIMEOUT = 120.0


#: Ollama's default generation cap when none is set. Matches the
#: runtime's historical default.
_DEFAULT_NUM_PREDICT = 4096


#: Map Ollama's ``done_reason`` to canonical finish reasons.
_DONE_REASON_MAP: Dict[str, str] = {
    "stop": "stop",
    "length": "length",
    "load": "stop",              # model just finished loading
    "unload": "stop",            # model unloaded
    "error": "tool_error",
}


# ---------------------------------------------------------------------------
# Adapter profile
# ---------------------------------------------------------------------------


_PROFILE = AdapterProfile(
    provider_id="ollama_native",
    api_surface="ollama_native",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,     # Ollama sends complete calls; multiple allowed
        forced_tool_choice=False,     # Ollama has no tool_choice parameter
        vision=True,
        audio=False,
        # Only "json" format is supported; no schema enforcement.
        structured_output=False,
        usage_reporting=True,
        reasoning=True,               # via `think` in 0.5+
        prompt_caching=False,
    ),
    reasoning_strategies=[ReasoningStrategy.OLLAMA_NATIVE],
    tool_call_style=ToolCallStyle.OLLAMA_TOOL_CALL,
    supported_param_names=[],
    notes=(
        "Native Ollama adapter. Supports /api/chat with streaming, "
        "thinking (0.5+), tool calling, and vision. Structured output "
        "is limited to format='json'."
    ),
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OllamaNativeAdapter:
    """Native Ollama adapter using the documented REST API."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "ollama_native"

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
                    "OllamaNativeAdapter requires either `context=` or "
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

        # Strip any endpoint suffix (``/v1``, ``/api/v1``, ``/api``)
        # so the adapter always builds paths from the server root.
        # See ``_normalize_ollama_root`` above for the rationale.
        base_url = _normalize_ollama_root(
            self._connection.base_url or _DEFAULT_BASE_URL
        ) or _DEFAULT_BASE_URL.rstrip("/")
        self._base_url = base_url

        # Optional bearer token (for reverse-proxied Ollama deployments).
        self._api_key = (
            self._credentials.get("api_key")
            or self._connection.api_key
            or ""
        )

        self._timeout = float(
            self._connection.timeout or _DEFAULT_TIMEOUT
        )

        # A single AsyncClient is reused across calls for connection
        # pooling. Closed via ``close()``.
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._headers = headers
        self._http = httpx.AsyncClient(
            timeout=self._timeout, headers=headers
        )

        logger.debug(
            "OllamaNativeAdapter ready: base_url=%s", self._base_url
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: generate (non-streaming)
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started_at = time.time()
        compiled = compile_request(request, self._profile)

        try:
            body = self._build_chat_body(compiled, stream=False)
            response = await self._http.post(
                f"{self._base_url}/api/chat", json=body
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Ollama did not respond within {self._timeout:.1f}s: {exc}",
                provider=self.provider_id,
                model=compiled.model,
                phase="request",
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderConnectionError(
                f"Could not connect to Ollama at {self._base_url}: {exc}",
                provider=self.provider_id,
                model=compiled.model,
            ) from exc
        except Exception as exc:
            raise _map_httpx_exception(
                exc, provider=self.provider_id, model=compiled.model
            ) from exc

        if response.status_code != 200:
            raise _build_http_error(
                response, provider=self.provider_id, model=compiled.model
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Ollama returned non-JSON response: {exc}",
                provider=self.provider_id,
                model=compiled.model,
                status_code=response.status_code,
            ) from exc

        completed_at = time.time()
        result = self._convert_chat_response(payload, compiled.model)
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
            body = self._build_chat_body(compiled, stream=True)
        except Exception as exc:
            yield _error_event(
                _map_httpx_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=1,
            )
            return

        seq = 0
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None
        # [P1-13 FIX — Terminal marker tracking]
        # The historical implementation emitted MESSAGE_COMPLETE
        # unconditionally after the NDJSON loop, even if the
        # provider stream terminated WITHOUT ever sending a
        # ``done: true`` chunk. That produced the same silent
        # "truncation reported as success" bug that
        # ``AsyncLLMClient.stream`` and ``StreamNormalizer``
        # already guard against — but on the native Ollama path,
        # where the earlier fixes do not reach.
        #
        # This flag tracks whether the provider sent the
        # terminal marker. It is set to True ONLY inside the
        # ``if chunk.get("done")`` branch, immediately before
        # ``break``. After the loop, MESSAGE_COMPLETE uses it to
        # pick the honest ``finish_reason``.
        saw_terminal_marker: bool = False

        try:
            async with self._http.stream(
                "POST", f"{self._base_url}/api/chat", json=body
            ) as response:
                if response.status_code != 200:
                    err = _build_http_error(
                        response,
                        provider=self.provider_id,
                        model=compiled.model,
                    )
                    yield _error_event(err, sequence=seq + 1)
                    return

                # Parse NDJSON one line at a time.
                async for raw_line in response.aiter_lines():
                    if not raw_line or not raw_line.strip():
                        continue
                    try:
                        chunk = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        logger.debug(
                            "Skipping malformed NDJSON line from Ollama: %s (line=%r)",
                            exc,
                            raw_line[:120],
                        )
                        continue

                    if not isinstance(chunk, dict):
                        continue

                    message = chunk.get("message") or {}
                    if not isinstance(message, dict):
                        message = {}

                    # ---------------------------------------------------
                    # Thinking delta.
                    # ---------------------------------------------------
                    thinking_piece = message.get("thinking")
                    if isinstance(thinking_piece, str) and thinking_piece:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.REASONING_DELTA,
                            reasoning_delta=thinking_piece,
                            sequence=seq,
                        )

                    # ---------------------------------------------------
                    # Content delta.
                    # ---------------------------------------------------
                    content_piece = message.get("content")
                    if isinstance(content_piece, str) and content_piece:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.CONTENT_DELTA,
                            content_delta=content_piece,
                            sequence=seq,
                        )

                    # ---------------------------------------------------
                    # Tool calls — complete objects, not deltas.
                    # ---------------------------------------------------
                    tool_calls = message.get("tool_calls")
                    if isinstance(tool_calls, list) and tool_calls:
                        for idx, tc in enumerate(tool_calls):
                            if not isinstance(tc, dict):
                                continue
                            fn = tc.get("function") or {}
                            name = fn.get("name") or ""
                            args = fn.get("arguments") or {}
                            if not isinstance(args, dict):
                                try:
                                    args = dict(args)
                                except Exception:
                                    args = {}
                            call_id = (
                                tc.get("id")
                                or fn.get("id")
                                or f"call_{seq}_{idx}"
                            )
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_id=str(call_id),
                                tool_name=str(name),
                                tool_index=idx,
                                sequence=seq,
                            )
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.TOOL_CALL_COMPLETE,
                                tool_call_id=str(call_id),
                                tool_name=str(name),
                                tool_index=idx,
                                tool_arguments_complete=args,
                                sequence=seq,
                            )

                    # ---------------------------------------------------
                    # Terminal chunk.
                    # ---------------------------------------------------
                    if chunk.get("done"):
                        # [P1-13 FIX] Mark that the provider sent
                        # the terminal marker. This is the ONLY
                        # place in the loop that sets it, so the
                        # post-loop check has a hard guarantee:
                        # if ``saw_terminal_marker`` is False, the
                        # stream ended WITHOUT ``done: true`` — it
                        # was truncated, not completed.
                        saw_terminal_marker = True
                        # Capture usage from the terminal chunk.
                        prompt_count = chunk.get("prompt_eval_count")
                        eval_count = chunk.get("eval_count")
                        if (
                            isinstance(prompt_count, int)
                            or isinstance(eval_count, int)
                        ):
                            captured_usage = TokenUsage(
                                input_tokens=(
                                    int(prompt_count)
                                    if isinstance(prompt_count, int)
                                    else None
                                ),
                                output_tokens=(
                                    int(eval_count)
                                    if isinstance(eval_count, int)
                                    else None
                                ),
                                total_tokens=(
                                    (int(prompt_count or 0))
                                    + (int(eval_count or 0))
                                )
                                or None,
                                source=UsageSource.PROVIDER,
                                estimated=False,
                            )
                        done_reason = chunk.get("done_reason")
                        if isinstance(done_reason, str):
                            finish_reason = _DONE_REASON_MAP.get(
                                done_reason.lower(), "stop"
                            )
                        break

        except asyncio.CancelledError:
            # httpx context manager closes the connection on cancel.
            raise
        except httpx.TimeoutException as exc:
            yield _error_event(
                ProviderTimeoutError(
                    f"Ollama stream timed out: {exc}",
                    provider=self.provider_id,
                    model=compiled.model,
                    phase="stream",
                ),
                sequence=seq + 1,
            )
            return
        except httpx.ConnectError as exc:
            yield _error_event(
                ProviderConnectionError(
                    f"Could not connect to Ollama at {self._base_url}: {exc}",
                    provider=self.provider_id,
                    model=compiled.model,
                ),
                sequence=seq + 1,
            )
            return
        except Exception as exc:
            yield _error_event(
                _map_httpx_exception(
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

        # [P1-13 FIX — Honest terminal marker]
        # The historical implementation emitted
        # ``finish_reason=finish_reason or "stop"`` even when the
        # provider never sent ``done: true``. That silently reported
        # a truncated stream as a normal completion, so consumers
        # (ToolCallAgent, Direct Chat) accepted partial output as
        # final and never retried.
        #
        # Post-fix:
        #   * If ``saw_terminal_marker`` is True — the provider
        #     explicitly signalled completion via ``done: true``.
        #     Emit the accumulated finish_reason (default "stop").
        #   * If False — the stream ended without ``done: true``
        #     (network drop, provider crash, partial NDJSON). Emit
        #     ``finish_reason="incomplete"`` so downstream code can
        #     distinguish truncation from completion and retry via
        #     ``StreamNormalizer`` / ``ToolCallAgent.think``.
        #
        # ``StreamEventType.MESSAGE_COMPLETE`` is still emitted in
        # both cases — this preserves the invariant that every
        # well-formed stream terminates with a single
        # MESSAGE_COMPLETE event. Only the ``finish_reason`` value
        # becomes truthful.
        if saw_terminal_marker:
            _effective_finish_reason = finish_reason or "stop"
        else:
            _effective_finish_reason = "incomplete"
            logger.warning(
                "[P1-13] Ollama stream ended WITHOUT a terminal "
                "'done: true' chunk (model=%s). Emitting "
                "finish_reason='incomplete' so consumers can retry "
                "with a fresh attempt.",
                compiled.model,
            )

        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=_effective_finish_reason,
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
        List installed models via ``GET /api/tags``.

        Each entry::

            {"name": "llama3.2:7b", "model": "llama3.2:7b",
             "size": 1234567890, "digest": "sha256:...",
             "details": {"family": "llama", "parameter_size": "3.2B",
                         "quantization_level": "Q4_K_M"}}

        Capability info (tools / thinking / vision) is NOT in this
        endpoint — the ``provider_live.py`` layer calls ``/api/show``
        for that during Scan Models.
        """
        try:
            response = await self._http.get(
                f"{self._base_url}/api/tags"
            )
        except Exception as exc:
            logger.warning(
                "Ollama /api/tags failed: %s", exc
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "Ollama /api/tags returned HTTP %d",
                response.status_code,
            )
            return []

        try:
            payload = response.json()
        except Exception:
            return []

        raw_models = (
            payload.get("models")
            if isinstance(payload, dict)
            else None
        ) or []
        if not isinstance(raw_models, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if not isinstance(name, str) or not name:
                continue
            details = item.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            results.append(
                {
                    "id": name,
                    "size": item.get("size"),
                    "digest": item.get("digest"),
                    "family": details.get("family"),
                    "parameter_size": details.get("parameter_size"),
                    "quantization_level": details.get(
                        "quantization_level"
                    ),
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
            response = await self._http.get(
                f"{self._base_url}/api/tags"
            )
            latency_ms = round(
                (time.time() - started_at) * 1000.0, 2
            )
            if response.status_code == 200:
                return {
                    "ok": True,
                    "message": "Ollama reachable.",
                    "latency_ms": latency_ms,
                }
            return {
                "ok": False,
                "message": (
                    f"Ollama returned HTTP {response.status_code}."
                ),
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round(
                (time.time() - started_at) * 1000.0, 2
            )
            return {
                "ok": False,
                "message": f"Ollama unreachable: {exc}",
                "latency_ms": latency_ms,
            }

    # ------------------------------------------------------------------
    # ProviderAdapter: get_model_metadata
    # ------------------------------------------------------------------

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Query ``POST /api/show`` for one model.

        Returns the raw capability list and details. Same shape used
        by ``provider_live.py``.
        """
        try:
            response = await self._http.post(
                f"{self._base_url}/api/show",
                json={"name": model_id},
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        capabilities = payload.get("capabilities")
        details = payload.get("details")
        model_info = payload.get("model_info")

        context_window: Optional[int] = None
        if isinstance(model_info, dict):
            for key, val in model_info.items():
                if (
                    isinstance(key, str)
                    and key.endswith(".context_length")
                    and isinstance(val, int)
                    and val > 0
                ):
                    context_window = val
                    break

        return {
            "id": model_id,
            "capabilities": capabilities
            if isinstance(capabilities, list)
            else [],
            "details": details if isinstance(details, dict) else {},
            "context_window": context_window,
        }

    # ------------------------------------------------------------------
    # ProviderAdapter: close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception as exc:
            logger.debug(
                "Ollama httpx client close raised: %s", exc
            )

    # ==================================================================
    # Request building
    # ==================================================================

    def _build_chat_body(
        self, compiled: CompiledRequest, *, stream: bool
    ) -> Dict[str, Any]:
        """Build the ``POST /api/chat`` request body."""
        # --------------------------------------------------------------
        # 1. Messages — re-insert the system prompt at the top.
        # --------------------------------------------------------------
        messages: List[Dict[str, Any]] = []
        if compiled.system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": compiled.system_prompt,
                }
            )
        messages.extend(self._convert_messages(compiled.messages))

        body: Dict[str, Any] = {
            "model": compiled.model,
            "messages": messages,
            "stream": bool(stream),
        }

        # --------------------------------------------------------------
        # 2. Options — Ollama's generation params object.
        # --------------------------------------------------------------
        options: Dict[str, Any] = {}
        gen = compiled.generation

        if "max_tokens" in gen and isinstance(
            gen["max_tokens"], int
        ):
            options["num_predict"] = int(gen["max_tokens"])
        elif "max_output_tokens" in gen and isinstance(
            gen["max_output_tokens"], int
        ):
            options["num_predict"] = int(gen["max_output_tokens"])
        else:
            options["num_predict"] = _DEFAULT_NUM_PREDICT

        for key in ("temperature", "top_p", "top_k"):
            if key in gen and gen[key] is not None:
                options[key] = gen[key]
        if "stop" in gen and isinstance(gen["stop"], list) and gen["stop"]:
            options["stop"] = list(gen["stop"])
        if "seed" in gen and gen["seed"] is not None:
            options["seed"] = int(gen["seed"])

        if options:
            body["options"] = options

        # --------------------------------------------------------------
        # 3. Reasoning — ``think`` accepts bool or effort level.
        # --------------------------------------------------------------
        if compiled.reasoning:
            think_val = compiled.reasoning.get("think")
            if think_val is not None:
                body["think"] = think_val

        # --------------------------------------------------------------
        # 4. Tools — same shape as OpenAI.
        # --------------------------------------------------------------
        if compiled.tools:
            body["tools"] = compiled.tools
            # Ollama has no tool_choice — the compiler already drops it
            # because the profile declares forced_tool_choice=False.

        # --------------------------------------------------------------
        # 5. Structured output — only format: "json" is supported.
        # --------------------------------------------------------------
        if compiled.response_format:
            rf_kind = compiled.response_format.get("type")
            if rf_kind == "json_object":
                body["format"] = "json"
            # JSON schema is not supported — the compiler declared
            # structured_output=False, so response_format should not
            # have reached here.

        # --------------------------------------------------------------
        # 6. Provider options (escape hatch).
        # --------------------------------------------------------------
        if compiled.provider_options:
            body.update(compiled.provider_options)

        return body

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert PELDRUN/OpenAI-shaped messages to Ollama's format.

        Differences vs OpenAI:

        * Images are base64 strings in an ``images`` array (no data URI).
        * Assistant ``tool_calls`` are forwarded as-is (Ollama accepts
          the OpenAI shape).
        * ``role: "tool"`` messages are forwarded as-is.
        """
        result: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                # Already handled at the top of ``_build_chat_body``.
                # Any stray system messages are skipped.
                continue

            content = msg.get("content")

            # -----------------------------------------------------------
            # Multimodal — extract base64 images from image_url parts.
            # -----------------------------------------------------------
            images: List[str] = []
            text_content: Optional[str] = None

            if isinstance(content, list):
                text_fragments: List[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        txt = part.get("text", "")
                        if isinstance(txt, str) and txt:
                            text_fragments.append(txt)
                    elif ptype == "image_url":
                        b64_str = self._extract_base64_image(part)
                        if b64_str is not None:
                            images.append(b64_str)
                text_content = "\n".join(text_fragments)
            elif isinstance(content, str):
                text_content = content

            # -----------------------------------------------------------
            # Build the Ollama message.
            # -----------------------------------------------------------
            out_msg: Dict[str, Any] = {"role": role}
            out_msg["content"] = text_content or ""

            if images:
                out_msg["images"] = images

            # Assistant tool_calls — Ollama accepts the OpenAI shape.
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                out_msg["tool_calls"] = tool_calls

            # tool_call_id for role=="tool".
            if role == "tool":
                tool_id = msg.get("tool_call_id")
                if tool_id:
                    out_msg["tool_call_id"] = tool_id

            result.append(out_msg)

        return result

    @staticmethod
    def _extract_base64_image(
        part: Dict[str, Any],
    ) -> Optional[str]:
        """
        Extract raw base64 from a ``data:image/...;base64,XXX`` URL.

        Ollama's ``images`` array expects plain base64 strings (no
        MIME prefix). HTTP URLs are not supported — Ollama does not
        fetch remote images. The caller must inline the bytes.
        """
        image_payload = part.get("image_url")
        if isinstance(image_payload, dict):
            url = image_payload.get("url")
        else:
            url = image_payload
        if not isinstance(url, str) or not url:
            return None
        if not url.startswith("data:"):
            return None
        try:
            _, _, data = url.partition(",")
            return data or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Response conversion (non-streaming)
    # ------------------------------------------------------------------

    def _convert_chat_response(
        self, payload: Dict[str, Any], model: str
    ) -> LLMResponse:
        """Convert a ``/api/chat`` response to ``LLMResponse``."""
        message = payload.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        content = message.get("content") or ""
        thinking = message.get("thinking")
        reasoning_text = (
            str(thinking) if isinstance(thinking, str) and thinking else None
        )

        # Tool calls — complete objects.
        tool_calls: List[ToolCall] = []
        raw_tool_calls = message.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for idx, tc in enumerate(raw_tool_calls):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args = fn.get("arguments") or {}
                if not isinstance(args, dict):
                    try:
                        args = dict(args)
                    except Exception:
                        args = {}
                call_id = tc.get("id") or f"call_{idx}"
                tool_calls.append(
                    ToolCall(
                        id=str(call_id),
                        name=str(name),
                        arguments=args,
                        type="function",
                    )
                )

        # Usage.
        prompt_count = payload.get("prompt_eval_count")
        eval_count = payload.get("eval_count")
        usage: Optional[TokenUsage] = None
        if isinstance(prompt_count, int) or isinstance(eval_count, int):
            usage = TokenUsage(
                input_tokens=(
                    int(prompt_count)
                    if isinstance(prompt_count, int)
                    else None
                ),
                output_tokens=(
                    int(eval_count)
                    if isinstance(eval_count, int)
                    else None
                ),
                total_tokens=(
                    (int(prompt_count or 0)) + (int(eval_count or 0))
                )
                or None,
                source=UsageSource.PROVIDER,
                estimated=False,
            )

        done_reason = payload.get("done_reason")
        finish_reason = (
            _DONE_REASON_MAP.get(done_reason.lower(), "stop")
            if isinstance(done_reason, str)
            else "stop"
        )

        return LLMResponse(
            content=str(content) if content else "",
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=str(payload.get("model") or model),
            raw=payload,
        )


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


def _build_http_error(
    response: httpx.Response,
    *,
    provider: str,
    model: str,
) -> ProviderError:
    """Build a ``ProviderError`` from a non-2xx response."""
    try:
        body = response.json()
    except Exception:
        body = None

    message = ""
    if isinstance(body, dict):
        message = (
            body.get("error")
            or body.get("message")
            or json.dumps(body)
        )
    elif isinstance(body, str):
        message = body
    else:
        message = f"HTTP {response.status_code} from Ollama."

    err = classify_http_status(
        response.status_code,
        provider=provider,
        model=model,
        message=message,
    )
    return refine_by_message(err, message)


def _map_httpx_exception(
    exc: BaseException, *, provider: str, model: str
) -> ProviderError:
    """Map a generic httpx exception to the canonical taxonomy."""
    if is_cancellation(exc):
        raise exc
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(
            str(exc), provider=provider, model=model, phase="request"
        )
    if isinstance(exc, httpx.ConnectError):
        return ProviderConnectionError(
            str(exc), provider=provider, model=model
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_status(
            exc.response.status_code,
            provider=provider,
            model=model,
            message=str(exc),
        )
    if isinstance(exc, httpx.TransportError):
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


__all__ = ["OllamaNativeAdapter"]
