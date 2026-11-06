# backend/peldrun/llm/providers/lmstudio_native.py
"""
PELDRUN Native LM Studio Provider Adapter.

Talks directly to LM Studio's native REST API v0
(``POST /api/v0/chat/completions``) — a request/response shape that is
*almost* OpenAI Chat Completions but adds native capabilities the
OpenAI-compat endpoint cannot expose:

    * Explicit ``reasoning`` field in the response message (not just
      ``model_extra.reasoning_content``).
    * Explicit ``reasoning_effort`` request parameter at the top level.
    * A ``stats`` object on every response with precise timing:
      ``tokens_per_second``, ``time_to_first_token``,
      ``generation_time``, ``stop_reason``.
    * Native model discovery from ``/api/v0/models`` that declares
      ``type`` ("llm" / "vlm" / "embeddings") and, on recent versions,
      a ``capabilities: ["tool_use"]`` array.

Design invariants
-----------------

1. **Uses ``/api/v0``, NOT ``/v1``.** The OpenAI-compat endpoint
   (``/v1/chat/completions``) is a shim; the native endpoint
   (``/api/v0/chat/completions``) exposes the extras listed above.
   We deliberately choose the native path.

2. **No external SDK.** LM Studio exposes a small, stable REST API
   that is best handled with ``httpx`` directly. This matches the
   Ollama native adapter's approach.

3. **Stats-aware.** LM Studio's ``stats.time_to_first_token`` is
   measured by the LM Studio process, which is more accurate than
   client-side wall-clock timing. When present, we use it.

4. **Surface-specific metadata.** The adapter queries
   ``/api/v0/models`` during ``get_model_metadata`` to return the
   authoritative ``type`` and ``capabilities`` for a given model. This
   is what makes the capability resolver honest for local models.

5. **Cancellation-safe.** Closing the httpx stream is the ONLY way
   to stop generation on LM Studio's side. CancelledError is
   propagated unwrapped after ``aclose()`` completes.

6. **Base URL tolerance.** The caller may pass:
       ``http://localhost:1234``
       ``http://localhost:1234/v1``
       ``http://localhost:1234/api/v0``
   The adapter normalizes all three to the correct root.

See also
--------

    * ``backend/peldrun/llm/contracts.py``        — ProviderAdapter protocol
    * ``backend/peldrun/llm/request_compiler.py`` — compiled request
    * ``backend/peldrun/llm/errors.py``           — error taxonomy
    * ``backend/peldrun/llm/catalog/provider_live.py`` — LM Studio capability
"""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger("peldrun.llm.providers.lmstudio_native")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: LM Studio's default local port.
_DEFAULT_BASE_URL = "http://localhost:1234"


#: Default request timeout. LM Studio can be slow on first load.
_DEFAULT_TIMEOUT = 180.0


#: LM Studio's stop_reason values mapped to canonical finish reasons.
_LMSTUDIO_STOP_REASON_MAP: Dict[str, str] = {
    "eosFound": "stop",
    "userStopped": "stop",
    "stopStringFound": "stop",
    "maxPredictedTokensReached": "length",
    "contextLengthReached": "length",
    "toolCalls": "tool_calls",
}


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------
#
# LM Studio exposes multiple endpoint surfaces under one server root:
#
#   /v1/*        — OpenAI-compatible shim (chat completions, models)
#   /api/v1/*    — native v1 (recent versions; serves /api/v1/models)
#   /api/v0/*    — native v0 (older versions; serves /api/v0/models,
#                  /api/v0/chat/completions, /api/v0/chat/...)
#
# Callers routinely pass the base URL with one of these suffixes already
# attached (e.g. ``http://host:1235/api/v1``). Building an endpoint URL
# by naively stripping ``/v1`` produces a double path like
# ``/api/api/v0/models`` when the caller's value already contained
# ``/api`` - the exact failure observed in the LM Studio server log
# when ``baseUrl`` was set to ``http://host:1235/api/v1/``.
#
# ``_normalize_lmstudio_root`` strips every known suffix idempotently
# and collapses any legacy double-``/api/`` segment left in the vault.
_PROVIDER_SUFFIX_PATTERN = re.compile(
    r"/(?:api/v0|api/v1|openai/v1|openai|v1beta|v1|api)/?$",
    re.IGNORECASE,
)


def _normalize_lmstudio_root(base_url: str) -> str:
    """Return the LM Studio server root, with any endpoint suffix removed.

    Idempotent. Safe to call on the raw value from the vault; the result
    is always the server root (scheme + host + optional port + optional
    path prefix), never an endpoint-specific URL.

    Examples
    --------
        http://127.0.0.1:1234              -> http://127.0.0.1:1234
        http://127.0.0.1:1234/             -> http://127.0.0.1:1234
        http://127.0.0.1:1234/v1           -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api          -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api/v0       -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api/v1       -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api/api/v0   -> http://127.0.0.1:1234
    """
    cleaned = (base_url or "").strip().rstrip("/")

    # Collapse any literal double /api/ left in the vault by the legacy
    # client, so even a pre-broken value heals here.
    while "/api/api/" in cleaned:
        cleaned = cleaned.replace("/api/api/", "/api/")

    # Strip known suffixes, possibly repeated (e.g. "/v1/api/v1").
    while True:
        stripped = _PROVIDER_SUFFIX_PATTERN.sub("", cleaned)
        if stripped == cleaned or not stripped:
            break
        cleaned = stripped.rstrip("/")

    return cleaned


# ---------------------------------------------------------------------------
# Adapter profile
# ---------------------------------------------------------------------------


_PROFILE = AdapterProfile(
    provider_id="lmstudio_native",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,      # via OpenAI-shaped tool_calls[]
        forced_tool_choice=True,
        vision=True,
        audio=False,
        structured_output=True,        # json_object + json_schema
        usage_reporting=True,
        reasoning=True,                # via reasoning_effort
        prompt_caching=False,
    ),
    reasoning_strategies=[ReasoningStrategy.LMSTUDIO_NATIVE],
    tool_call_style=ToolCallStyle.LMSTUDIO_NATIVE,
    supported_param_names=[],
    notes=(
        "Native LM Studio adapter. /api/v0/chat/completions with "
        "explicit reasoning field and precise stats. Native model "
        "discovery from /api/v0/models."
    ),
    # Dynamic routing markers. Only match LM Studio's *characteristic*
    # substrings: the default ports, the product name, and the native
    # API paths. The previous list included the bare token ``"v1"``,
    # which false-positives on every OpenAI-compatible custom endpoint
    # (they all end with ``/v1``) and silently routed those requests
    # to this adapter. Removing it makes routing conservative:
    # a Custom Endpoint whose URL does not otherwise resemble LM Studio
    # now correctly falls through to ``openai_compat``.
    default_base_url_markers=[
        "1234", "1235", "lmstudio", "api/v0", "api/v1",
    ],
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class LMStudioNativeAdapter:
    """Native LM Studio adapter using /api/v0/."""

    ADAPTER_STYLE = "v1"
    API_CONTRACT = "peldrun.llm.provider.v1"
    PROFILE = _PROFILE
    provider_id = "lmstudio_native"

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
                    "LMStudioNativeAdapter requires either `context=` "
                    "or `connection=` (and optionally `profile=`)."
                )
            self._connection = connection
            self._profile = profile or _PROFILE
            self._credentials = (
                {"api_key": connection.api_key}
                if connection.api_key
                else {}
            )

        self.profile = self._profile

        # Normalize the base URL to the LM Studio root (no /v1, no
        # /api/v0, no /api/v1). This is the single source of truth for
        # every endpoint URL this adapter builds - see
        # ``_normalize_lmstudio_root`` for the exact normalization rules
        # and the failure mode this replaces.
        base_url = _normalize_lmstudio_root(
            self._connection.base_url or _DEFAULT_BASE_URL
        ) or _DEFAULT_BASE_URL.rstrip("/")
        self._base_url = base_url

        # Chat completions continue to use the v0 native API - that is
        # where LM Studio exposes the ``stats`` object and the native
        # ``reasoning`` field this adapter depends on. Even LM Studio
        # 0.3.x (which ships v1 model listing) keeps v0 for chat.
        self._api_v0 = f"{base_url}/api/v0"

        # Model listing is served by different endpoints depending on
        # the LM Studio version. Probe newest-first so a modern
        # installation never logs a spurious 404 for the older native
        # API:
        #   /api/v1/models — LM Studio 0.3.x native v1 (newest)
        #   /api/v0/models — LM Studio <0.3.x native v0 (legacy)
        #   /v1/models     — OpenAI-compat shim (universal fallback)
        self._models_endpoints = (
            f"{base_url}/api/v1/models",
            f"{base_url}/api/v0/models",
            f"{base_url}/v1/models",
        )

        # Optional bearer token. LM Studio's local server ignores it,
        # but reverse-proxied deployments may require one.
        self._api_key = (
            self._credentials.get("api_key")
            or self._connection.api_key
            or ""
        )

        self._timeout = float(
            self._connection.timeout or _DEFAULT_TIMEOUT
        )

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
            "LMStudioNativeAdapter ready: base_url=%s api_v0=%s",
            self._base_url,
            self._api_v0,
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: generate (non-streaming)
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started_at = time.time()
        compiled = compile_request(request, self._profile)

        try:
            body = self._build_body(compiled, stream=False)
            response = await self._http.post(
                f"{self._api_v0}/chat/completions", json=body
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"LM Studio did not respond within {self._timeout:.1f}s: "
                f"{exc}",
                provider=self.provider_id,
                model=compiled.model,
                phase="request",
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderConnectionError(
                f"Could not connect to LM Studio at {self._base_url}: "
                f"{exc}",
                provider=self.provider_id,
                model=compiled.model,
            ) from exc
        except Exception as exc:
            raise _map_httpx_exception(
                exc, provider=self.provider_id, model=compiled.model
            ) from exc

        if response.status_code != 200:
            raise _build_http_error(
                response,
                provider=self.provider_id,
                model=compiled.model,
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise ProviderUnavailableError(
                f"LM Studio returned non-JSON response: {exc}",
                provider=self.provider_id,
                model=compiled.model,
                status_code=response.status_code,
            ) from exc

        completed_at = time.time()
        result = self._convert_response(payload, compiled.model)
        # Prefer stats timings when available; fall back to wall clock.
        stats = payload.get("stats") if isinstance(payload, dict) else None
        if isinstance(stats, dict):
            ttft_s = stats.get("time_to_first_token")
            gen_time = stats.get("generation_time")
            if isinstance(ttft_s, (int, float)) and ttft_s >= 0:
                result.ttft_ms = round(float(ttft_s) * 1000.0, 2)
                if isinstance(gen_time, (int, float)) and gen_time >= 0:
                    result.latency_ms = round(
                        (float(ttft_s) + float(gen_time)) * 1000.0, 2
                    )
                else:
                    result.latency_ms = result.ttft_ms
        if result.latency_ms is None:
            result.latency_ms = round(
                (completed_at - started_at) * 1000.0, 2
            )
        if result.ttft_ms is None:
            result.ttft_ms = result.latency_ms

        # Attach diagnostics and raw stats for the Request Inspector.
        if result.raw is None:
            result.raw = {}
        if isinstance(result.raw, dict):
            result.raw.setdefault("_peldrun_diagnostics", compiled.diagnostics)
            if isinstance(stats, dict):
                result.raw.setdefault("_peldrun_stats", stats)

        return result

    # ------------------------------------------------------------------
    # ProviderAdapter: stream
    # ------------------------------------------------------------------

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        compiled = compile_request(request, self._profile)

        try:
            body = self._build_body(compiled, stream=True)
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
        captured_stats: Optional[Dict[str, Any]] = None
        finish_reason: Optional[str] = None

        # Track tool-call accumulation by index (OpenAI-shaped deltas).
        active_tools: Dict[int, Dict[str, Any]] = {}

        try:
            async with self._http.stream(
                "POST",
                f"{self._api_v0}/chat/completions",
                json=body,
            ) as response:
                if response.status_code != 200:
                    err = _build_http_error(
                        response,
                        provider=self.provider_id,
                        model=compiled.model,
                    )
                    yield _error_event(err, sequence=seq + 1)
                    return

                # SSE-style: ``data: <json>\n\n`` lines, ending with
                # ``data: [DONE]``.
                async for raw_line in response.aiter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        # Ignore SSE comments (`: ping`) and other
                        # non-data lines.
                        continue

                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    if not data_str:
                        continue

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError as exc:
                        logger.debug(
                            "Skipping malformed SSE line from LM Studio: "
                            "%s (line=%r)",
                            exc,
                            data_str[:120],
                        )
                        continue

                    if not isinstance(chunk, dict):
                        continue

                    # ---------------------------------------------------
                    # Usage / stats — may appear on the terminal chunk.
                    # ---------------------------------------------------
                    usage_obj = chunk.get("usage")
                    if isinstance(usage_obj, dict):
                        converted = _convert_usage(usage_obj)
                        if converted is not None:
                            captured_usage = converted

                    stats_obj = chunk.get("stats")
                    if isinstance(stats_obj, dict):
                        captured_stats = stats_obj

                    # ---------------------------------------------------
                    # Choices → deltas.
                    # ---------------------------------------------------
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue

                    first = choices[0]
                    if not isinstance(first, dict):
                        continue

                    delta = first.get("delta")
                    if isinstance(delta, dict):
                        # Content delta.
                        content_piece = delta.get("content")
                        if isinstance(content_piece, str) and content_piece:
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.CONTENT_DELTA,
                                content_delta=content_piece,
                                sequence=seq,
                            )

                        # Reasoning delta — native field, not
                        # model_extra. LM Studio exposes it directly.
                        reasoning_piece = delta.get("reasoning")
                        if not isinstance(reasoning_piece, str):
                            # Fallback for older LM Studio versions
                            # that used ``reasoning_content``.
                            alt = delta.get("reasoning_content")
                            if isinstance(alt, str):
                                reasoning_piece = alt
                        if (
                            isinstance(reasoning_piece, str)
                            and reasoning_piece
                        ):
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.REASONING_DELTA,
                                reasoning_delta=reasoning_piece,
                                sequence=seq,
                            )

                        # Tool call deltas — OpenAI-shaped.
                        tc_deltas = delta.get("tool_calls")
                        if isinstance(tc_deltas, list):
                            for tc_delta in tc_deltas:
                                if not isinstance(tc_delta, dict):
                                    continue
                                tool_index = tc_delta.get("index", 0)
                                if not isinstance(tool_index, int):
                                    tool_index = 0
                                fn = tc_delta.get("function") or {}
                                if not isinstance(fn, dict):
                                    fn = {}

                                if tool_index not in active_tools:
                                    tool_id = (
                                        tc_delta.get("id")
                                        or f"call_{seq}_{tool_index}"
                                    )
                                    tool_name = (
                                        fn.get("name") or ""
                                    )
                                    active_tools[tool_index] = {
                                        "id": str(tool_id),
                                        "name": str(tool_name),
                                        "arguments": "",
                                    }
                                    seq += 1
                                    yield LLMStreamEvent(
                                        type=StreamEventType.TOOL_CALL_START,
                                        tool_call_id=str(tool_id),
                                        tool_name=str(tool_name),
                                        tool_index=tool_index,
                                        sequence=seq,
                                    )
                                else:
                                    # Late ID assignment.
                                    incoming_id = tc_delta.get("id")
                                    if incoming_id:
                                        active_tools[tool_index][
                                            "id"
                                        ] = str(incoming_id)

                                args_piece = fn.get("arguments")
                                if (
                                    isinstance(args_piece, str)
                                    and args_piece
                                ):
                                    active_tools[tool_index][
                                        "arguments"
                                    ] += args_piece
                                    seq += 1
                                    yield LLMStreamEvent(
                                        type=StreamEventType.TOOL_CALL_DELTA,
                                        tool_call_id=active_tools[
                                            tool_index
                                        ]["id"],
                                        tool_name=active_tools[
                                            tool_index
                                        ]["name"],
                                        tool_index=tool_index,
                                        tool_arguments_delta=args_piece,
                                        sequence=seq,
                                    )

                    # ---------------------------------------------------
                    # Finish reason (per-choice).
                    # ---------------------------------------------------
                    chunk_finish = first.get("finish_reason")
                    if isinstance(chunk_finish, str) and chunk_finish:
                        finish_reason = chunk_finish

        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as exc:
            yield _error_event(
                ProviderTimeoutError(
                    f"LM Studio stream timed out: {exc}",
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
                    f"Could not connect to LM Studio at {self._base_url}: "
                    f"{exc}",
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

        # ---------------------------------------------------------------
        # Emit TOOL_CALL_COMPLETE for each accumulated tool.
        # ---------------------------------------------------------------
        for tool_index in sorted(active_tools.keys()):
            state = active_tools[tool_index]
            raw_args = state.get("arguments", "")
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
                tool_call_id=state["id"],
                tool_name=state["name"],
                tool_index=tool_index,
                tool_arguments_complete=parsed_args,
                sequence=seq,
            )

        # ---------------------------------------------------------------
        # Map stats.stop_reason → canonical if present, else use the
        # accumulated ``finish_reason`` from choices.
        # ---------------------------------------------------------------
        if captured_stats:
            stop_reason = captured_stats.get("stop_reason")
            if isinstance(stop_reason, str):
                finish_reason = _LMSTUDIO_STOP_REASON_MAP.get(
                    stop_reason, "stop"
                )

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
            provider_metadata=(
                {"stats": captured_stats}
                if captured_stats is not None
                else {}
            ),
            sequence=seq,
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: discover_models
    # ------------------------------------------------------------------

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        List installed models via ``GET /api/v0/models``.

        Each entry::

            {"id": "google/gemma-2-9b", "type": "llm",
             "publisher": "google", "arch": "gemma2",
             "quantization": "Q4_K_M", "state": "not-loaded",
             "max_context_length": 8192,
             "capabilities": ["tool_use"]}  # optional

        The ``capabilities`` array is only present on newer LM Studio
        versions. When absent, the caller (``provider_live.py``) treats
        tool support as ``unknown``.
        """
        # Probe every LM Studio model-listing endpoint in priority
        # order (v1 → v0 → compat shim). The first that returns 200
        # with a valid dict wins. This is what lets one adapter work
        # across LM Studio 0.2.x, 0.3.x, and future versions without
        # logging errors for endpoints that the running version does
        # not serve.
        payload: Optional[Dict[str, Any]] = None
        last_error: Optional[str] = None

        for url in self._models_endpoints:
            try:
                response = await self._http.get(url)
            except Exception as exc:
                last_error = f"{url}: network error ({exc})"
                logger.debug(
                    "LM Studio %s network error: %s", url, exc
                )
                continue

            if response.status_code != 200:
                last_error = f"{url}: HTTP {response.status_code}"
                logger.debug(
                    "LM Studio %s returned HTTP %d",
                    url,
                    response.status_code,
                )
                continue

            try:
                candidate = response.json()
            except Exception as exc:
                last_error = f"{url}: non-JSON response ({exc})"
                logger.debug(
                    "LM Studio %s returned non-JSON: %s", url, exc
                )
                continue

            if not isinstance(candidate, dict):
                last_error = (
                    f"{url}: JSON root is not a dict "
                    f"(got {type(candidate).__name__})"
                )
                continue

            payload = candidate
            break

        if payload is None:
            logger.warning(
                "LM Studio: no model-listing endpoint responded "
                "successfully at %r (last error: %s).",
                self._base_url,
                last_error or "unknown",
            )
            return []

        raw_models = payload.get("data") or payload.get("models") or []
        if not isinstance(raw_models, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue

            # LM Studio's /api/v1/models uses ``key`` as the model
            # identifier. The older /api/v0/models endpoint uses
            # ``id``. Some very old builds use ``name``. Accept all
            # three forms so the same adapter works across every LM
            # Studio release. Missing this fallback made the modern
            # /api/v1/models response produce an empty model list -
            # the exact reason Branch 0 (native adapter discovery)
            # silently returned [] on the user's installation.
            item_id = (
                item.get("key")
                or item.get("id")
                or item.get("name")
            )
            if not isinstance(item_id, str) or not item_id:
                continue

            entry: Dict[str, Any] = {"id": item_id}

            # Preserve every descriptive field the LM Studio response
            # carries. ``architecture`` is LM Studio's field name for
            # what the rest of PELDRUN calls ``arch``; both are kept
            # so downstream consumers (the metadata vault, the model
            # cards) can read either.
            for field in (
                "object",
                "type",
                "publisher",
                "arch",
                "architecture",
                "compatibility_type",
                "quantization",
                "state",
                "max_context_length",
                "format",
                "params_string",
                "size_bytes",
                "display_name",
                "description",
                "loaded_instances",
                "variants",
                "selected_variant",
            ):
                if field in item:
                    entry[field] = item[field]

            # ----------------------------------------------------------
            # Capabilities: preserve the RAW shape LM Studio sends.
            #
            # Modern LM Studio (0.3.x+) returns a DICT of well-typed
            # flags and nested objects:
            #
            #   "capabilities": {
            #     "vision": true,
            #     "trained_for_tool_use": true,
            #     "reasoning": {
            #       "allowed_options": ["off", "on"],
            #       "default": "on"
            #     }
            #   }
            #
            # Older /api/v0/models responses use a flat LIST of tokens:
            #
            #   "capabilities": ["completion", "tool_use", "vision"]
            #
            # The downstream parser (_parse_adapter_model_list in
            # config_rtr.py) knows how to translate both shapes. The
            # adapter's only job is to not drop them.
            # ----------------------------------------------------------
            raw_caps = item.get("capabilities")
            if isinstance(raw_caps, dict):
                entry["capabilities"] = dict(raw_caps)
            elif isinstance(raw_caps, list):
                entry["capabilities"] = list(raw_caps)

            results.append(entry)

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
                f"{self._api_v0}/models"
            )
            latency_ms = round(
                (time.time() - started_at) * 1000.0, 2
            )
            if response.status_code == 200:
                return {
                    "ok": True,
                    "message": "LM Studio native API reachable.",
                    "latency_ms": latency_ms,
                }
            return {
                "ok": False,
                "message": (
                    f"LM Studio returned HTTP {response.status_code}."
                ),
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round(
                (time.time() - started_at) * 1000.0, 2
            )
            return {
                "ok": False,
                "message": f"LM Studio unreachable: {exc}",
                "latency_ms": latency_ms,
            }

    # ------------------------------------------------------------------
    # ProviderAdapter: get_model_metadata
    # ------------------------------------------------------------------

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return the entry for a single model from ``/api/v0/models``.

        LM Studio does not expose a per-model endpoint like
        ``/api/v0/models/{id}``. We fetch the full list and match by
        exact ID, then by short-form ID (last segment after ``/``).
        """
        # Iterate the same endpoint priority list that
        # ``discover_models`` uses. See its docstring for the
        # rationale behind the v1 → v0 → compat ordering.
        payload: Optional[Dict[str, Any]] = None
        for url in self._models_endpoints:
            try:
                response = await self._http.get(url)
            except Exception:
                continue
            if response.status_code != 200:
                continue
            try:
                candidate = response.json()
            except Exception:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break

        if payload is None:
            return None

        raw_models = payload.get("data") or payload.get("models") or []
        if not isinstance(raw_models, list):
            return None

        for item in raw_models:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            if item_id == model_id or item_id.rsplit("/", 1)[-1] == model_id:
                return dict(item)

        return None

    # ------------------------------------------------------------------
    # ProviderAdapter: close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._http.aclose()
        except Exception as exc:
            logger.debug(
                "LM Studio httpx client close raised: %s", exc
            )

    # ==================================================================
    # Request building
    # ==================================================================

    def _build_body(
        self, compiled: CompiledRequest, *, stream: bool
    ) -> Dict[str, Any]:
        """
        Build the ``POST /api/v0/chat/completions`` request body.

        The shape matches OpenAI Chat Completions with a few native
        extensions that LM Studio accepts:

        * ``reasoning_effort`` at the top level (not in ``extra_body``).
        * ``top_k``, ``repeat_penalty``, ``min_p`` accepted directly.
        """
        messages: List[Dict[str, Any]] = []
        if compiled.system_prompt:
            messages.append(
                {"role": "system", "content": compiled.system_prompt}
            )
        messages.extend(self._convert_messages(compiled.messages))

        body: Dict[str, Any] = {
            "model": compiled.model,
            "messages": messages,
            "stream": bool(stream),
        }

        # --------------------------------------------------------------
        # Generation params.
        # --------------------------------------------------------------
        gen = compiled.generation
        for key in ("max_tokens", "temperature", "top_p", "top_k"):
            if key in gen and gen[key] is not None:
                body[key] = gen[key]
        if "stop" in gen and isinstance(gen["stop"], list) and gen["stop"]:
            body["stop"] = list(gen["stop"])
        if "seed" in gen and gen["seed"] is not None:
            body["seed"] = int(gen["seed"])
        if "frequency_penalty" in gen and gen["frequency_penalty"] is not None:
            body["frequency_penalty"] = float(gen["frequency_penalty"])
        if "presence_penalty" in gen and gen["presence_penalty"] is not None:
            body["presence_penalty"] = float(gen["presence_penalty"])

        # --------------------------------------------------------------
        # Reasoning — native top-level field.
        # --------------------------------------------------------------
        if compiled.reasoning:
            effort = compiled.reasoning.get("reasoning_effort")
            if isinstance(effort, str) and effort:
                body["reasoning_effort"] = effort

        # --------------------------------------------------------------
        # Tools + tool_choice — OpenAI-shaped, native-supported.
        #
        # [FIX-TOOL-CHOICE-WIRE-FORMAT] LM Studio (built on llama.cpp)
        # strictly validates ``tool_choice`` against the OpenAI Chat
        # Completions spec: the three generic modes MUST be plain
        # strings (``"auto"``, ``"none"``, ``"required"``). Sending the
        # legacy object form ``{"type": "auto"}`` triggers HTTP 400.
        #
        # The compiler (``request_compiler._compile_tools``) emits the
        # object form because Provider Packages (GigaChat etc.) parse
        # it with ``.get("type")`` internally and cannot handle a raw
        # string. Each adapter therefore translates the object form
        # into its own wire vocabulary. For LM Studio, that means
        # flattening the generic modes to a string. The
        # ``{"type": "function", ...}`` case stays an object per spec.
        if compiled.tools:
            body["tools"] = compiled.tools
            if compiled.tool_choice:
                _tc = compiled.tool_choice
                if isinstance(_tc, dict) and _tc.get("type") in (
                    "auto",
                    "none",
                    "required",
                ):
                    body["tool_choice"] = _tc["type"]
                else:
                    body["tool_choice"] = _tc

        # --------------------------------------------------------------
        # Response format — native supports json_object + json_schema.
        # --------------------------------------------------------------
        if compiled.response_format:
            body["response_format"] = compiled.response_format

        # --------------------------------------------------------------
        # Provider options (escape hatch).
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
        LM Studio's native API accepts OpenAI-shaped messages verbatim,
        including ``image_url`` parts and ``tool_calls`` arrays. We only
        skip stray system messages (the compiler already extracted the
        real system prompt).
        """
        result: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system":
                continue
            result.append(msg)
        return result

    # ------------------------------------------------------------------
    # Response conversion (non-streaming)
    # ------------------------------------------------------------------

    def _convert_response(
        self, payload: Dict[str, Any], model: str
    ) -> LLMResponse:
        """Convert a native response to ``LLMResponse``."""
        choices = payload.get("choices")
        finish_reason: Optional[str] = None
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []

        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") or {}
            if isinstance(message, dict):
                raw_content = message.get("content")
                if isinstance(raw_content, str):
                    content_parts.append(raw_content)
                elif isinstance(raw_content, list):
                    for part in raw_content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            txt = part.get("text")
                            if isinstance(txt, str):
                                content_parts.append(txt)

                # Native ``reasoning`` field (top-level).
                native_reasoning = message.get("reasoning")
                if isinstance(native_reasoning, str) and native_reasoning:
                    reasoning_parts.append(native_reasoning)
                else:
                    alt = message.get("reasoning_content")
                    if isinstance(alt, str) and alt:
                        reasoning_parts.append(alt)

                raw_tool_calls = message.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    for idx, tc in enumerate(raw_tool_calls):
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function") or {}
                        if not isinstance(fn, dict):
                            fn = {}
                        name = fn.get("name") or ""
                        raw_args = fn.get("arguments")
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
                        tool_calls.append(
                            ToolCall(
                                id=str(tc.get("id") or f"call_{idx}"),
                                name=str(name),
                                arguments=parsed_args,
                                type="function",
                            )
                        )

            finish_reason_raw = first.get("finish_reason")
            if isinstance(finish_reason_raw, str):
                finish_reason = finish_reason_raw

        # Map stats.stop_reason when finish_reason is missing/odd.
        stats = payload.get("stats")
        if isinstance(stats, dict) and finish_reason in (None, ""):
            stop_reason = stats.get("stop_reason")
            if isinstance(stop_reason, str):
                finish_reason = _LMSTUDIO_STOP_REASON_MAP.get(
                    stop_reason, "stop"
                )

        reasoning_text = (
            "".join(reasoning_parts) if reasoning_parts else None
        )

        return LLMResponse(
            content="".join(content_parts),
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=_convert_usage(payload.get("usage")),
            model=str(payload.get("model") or model),
            raw=payload,
        )


# ---------------------------------------------------------------------------
# Usage conversion
# ---------------------------------------------------------------------------


def _convert_usage(raw_usage: Any) -> Optional[TokenUsage]:
    """Convert an OpenAI-shaped usage object to ``TokenUsage``."""
    if not isinstance(raw_usage, dict):
        return None

    inp = raw_usage.get("prompt_tokens")
    out = raw_usage.get("completion_tokens")
    total = raw_usage.get("total_tokens")

    cached: Optional[int] = None
    prompt_details = raw_usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        val = prompt_details.get("cached_tokens")
        if isinstance(val, int):
            cached = val

    reasoning: Optional[int] = None
    comp_details = raw_usage.get("completion_tokens_details")
    if isinstance(comp_details, dict):
        val = comp_details.get("reasoning_tokens")
        if isinstance(val, int):
            reasoning = val

    if not isinstance(inp, int) and not isinstance(out, int) and not isinstance(
        total, int
    ):
        return None

    return TokenUsage(
        input_tokens=int(inp) if isinstance(inp, int) else None,
        output_tokens=int(out) if isinstance(out, int) else None,
        total_tokens=(
            int(total)
            if isinstance(total, int)
            else ((inp or 0) + (out or 0)) or None
        ),
        cached_input_tokens=cached,
        reasoning_output_tokens=reasoning,
        source=UsageSource.PROVIDER,
        estimated=False,
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
        if isinstance(message, dict):
            message = json.dumps(message)
    elif isinstance(body, str):
        message = body
    else:
        message = f"HTTP {response.status_code} from LM Studio."

    err = classify_http_status(
        response.status_code,
        provider=provider,
        model=model,
        message=str(message),
    )
    return refine_by_message(err, str(message))


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


__all__ = ["LMStudioNativeAdapter"]
