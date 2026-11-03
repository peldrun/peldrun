# backend/peldrun/llm/catalog/provider_live.py
"""
PELDRUN Live Provider Capability Query.

Queries the provider's own endpoint for a specific model's capabilities
in real time. This is the highest-fidelity source short of a paid
capability probe: the provider itself declares what the model can do.

Design principles
-----------------

1. **Only providers with a real capability endpoint.**
   Not every provider exposes model capabilities. Ollama, LM Studio,
   and Google do; OpenAI, Anthropic, DeepSeek, and the generic
   OpenAI-compatible surface do not. For providers without a
   capability endpoint, this module returns ``None`` and the caller
   falls back to cached metadata or LiteLLM — the honest answer.

2. **Definitive answers only.**
   When a provider sends ``capabilities: ["completion", "vision"]``,
   the ABSENCE of ``"tools"`` means tools are definitively NOT
   supported (the list is exhaustive). When a provider sends only
   ``{id, object, owned_by}``, nothing is definitive. This module
   distinguishes the two cases rigorously.

3. **Timeout-hardened.**
   A 5-second default timeout. A slow or unreachable provider returns
   ``None`` and the caller continues. Never blocks the UI.

4. **Never raises.**
   Network errors, invalid JSON, unexpected shapes → ``None`` +
   debug log. The caller's merge logic handles the missing data.

5. **Read-only.**
   This module does NOT write to ``models_metadata.json``. The
   persistence layer (``config_rtr.py::fetch-models`` enrichment, or
   ``lookup.py`` cache-miss path) decides what to save and when.
   Separation of "query" from "persist" keeps the module pure.

6. **No heuristics.**
   No model-name pattern matching anywhere. If the provider does not
   say it, this module does not return it.

Providers supported
-------------------

* **Ollama** — ``POST /api/show`` returns ``capabilities``, context
  length, parameter size, quantization, and model family.

* **LM Studio** — ``GET /api/v0/models`` returns per-model ``type``
  ("llm" or "vlm"), context window, publisher, architecture, and
  quantization.

* **Google Gemini** — ``GET /v1beta/{model_name}`` returns
  ``inputTokenLimit``, ``outputTokenLimit``, and
  ``supportedGenerationMethods``.

* **OpenAI, Anthropic, DeepSeek, xAI, others** — no capability
  endpoint. Returns ``None``.

See also
--------

    * ``backend/peldrun/llm/catalog/lookup.py`` — merge layer
    * ``backend/peldrun/llm/catalog/external_registry.py`` — LiteLLM
    * ``backend/peldrun/llm/catalog/huggingface.py`` — HF config
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

from peldrun.llm.contracts import ConnectionContext

logger = logging.getLogger("peldrun.llm.catalog.provider_live")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default timeout for a live query. Short enough that a wedged provider
#: does not block the UI for more than a few seconds, long enough that a
#: cold-start Ollama process still responds.
DEFAULT_TIMEOUT_SECONDS: float = 5.0


#: Ollama's /api/show endpoint lives at the root of the base URL, NOT
#: under ``/v1``. The caller frequently provides a URL with ``/v1``
#: appended (from the LM Studio / OpenAI-compat convention), so this
#: module strips the suffix before appending the native path.
_OLLAMA_SHOW_PATH: str = "/api/show"


#: LM Studio's v0 models endpoint.
_LMSTUDIO_MODELS_PATH: str = "/api/v0/models"


#: Google's per-model endpoint (appended to the base URL).
_GOOGLE_MODEL_PATH_TEMPLATE: str = "/v1beta/{model_name}"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ProviderLiveResult:
    """
    Definitive facts returned by a provider's own endpoint.

    Fields
    ------
    capabilities
        Mapping of capability name to ``True`` (definitively supported)
        or ``False`` (definitively unsupported). A missing key means
        "the provider did not declare this capability", which is
        equivalent to "unknown". Keys use the standard PELDRUN
        vocabulary: ``"vision"``, ``"tools"``, ``"reasoning"``.
    context_window
        Context length in tokens, when reported.
    max_output_tokens
        Maximum output tokens, when reported (Google only).
    metadata
        Descriptive fields reported by the provider. Uses the same
        shape that ``models_metadata.json`` stores:
        ``{"params_string", "quantization_name", "architecture",
        "publisher", "family"}``.
    raw
        The full parsed response for diagnostics. Never shown to the UI
        except through the Request Inspector.
    queried_at
        Unix timestamp of the query.
    source
        Always ``"provider_live"``. Kept as a field so downstream
        callers do not need to hardcode the string.
    """

    provider_id: str
    model_id: str
    capabilities: Dict[str, bool] = field(default_factory=dict)
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw: Optional[Dict[str, Any]] = None
    queried_at: float = field(default_factory=time.time)
    source: str = "provider_live"

    # ------------------------------------------------------------------
    # Conversion to models_metadata.json shape
    # ------------------------------------------------------------------

    def to_metadata_dict(self) -> Dict[str, Any]:
        """
        Convert to the shape stored under one key in
        ``models_metadata.json``.

        Every capability derived from this live query is annotated with
        ``capability_sources = "provider_live"`` so the resolver honors
        it with priority 95 (below manual override, above every other
        source). Manual overrides set by the user via the modal carry
        the source ``"manual"`` and are NOT overwritten here — the
        persistence layer (``config_rtr.py::save_metadata_cache``)
        performs a merge-aware update that preserves ``manual`` markers.

        Reasoning control mode is included for Ollama because that
        provider exposes a ``think: bool`` toggle. For providers whose
        control mechanism is not exposed by the live endpoint, no mode
        is emitted and the UI falls back to "always on, no control" —
        which is honest.

        The output is designed to be merged directly into the vault via
        ``save_metadata_cache({self.model_id: result.to_metadata_dict()})``.
        """
        entry: Dict[str, Any] = {
            "key": self.model_id,
            "type": "llm",
            "capabilities": {},
            "capability_sources": {},
            "capability_details": {},
        }

        field_map = {
            "vision": "vision",
            "tools": "trained_for_tool_use",
            "reasoning": "reasoning",
        }

        for cap_name, cap_value in self.capabilities.items():
            target_field = field_map.get(cap_name)
            if target_field is None:
                continue
            entry["capabilities"][target_field] = bool(cap_value)
            entry["capability_sources"][target_field] = "provider_live"

        # Attach reasoning control mode when we know it.
        # Ollama: reasoning is exposed via `think: true|false` → toggle.
        # LM Studio / Google: control mechanism not declared by the
        # endpoint → leave mode unset (resolver treats as NONE).
        if (
            self.provider_id == "ollama"
            and "reasoning" in self.capabilities
        ):
            reasoning_supported = bool(self.capabilities["reasoning"])
            entry["capability_details"]["reasoning"] = {
                "mode": "toggle" if reasoning_supported else "none",
                "state": "supported" if reasoning_supported else "unsupported",
            }

        if self.context_window is not None and self.context_window > 0:
            entry["max_context_length"] = int(self.context_window)
        if self.max_output_tokens is not None and self.max_output_tokens > 0:
            entry["max_output_tokens"] = int(self.max_output_tokens)

        params = self.metadata.get("params_string")
        if isinstance(params, str) and params:
            entry["params_string"] = params

        quant = self.metadata.get("quantization_name")
        if isinstance(quant, str) and quant:
            entry["quantization"] = {"name": quant}

        arch = self.metadata.get("architecture")
        if isinstance(arch, str) and arch:
            entry["architecture"] = arch

        publisher = self.metadata.get("publisher")
        if isinstance(publisher, str) and publisher:
            entry["publisher"] = publisher

        family = self.metadata.get("family")
        if isinstance(family, str) and family:
            entry["family"] = family

        return entry

    def __repr__(self) -> str:
        return (
            f"ProviderLiveResult(provider={self.provider_id!r}, "
            f"model={self.model_id!r}, caps={self.capabilities!r}, "
            f"ctx={self.context_window})"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def query_provider_live(
    provider_id: str,
    model_id: str,
    connection: ConnectionContext,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[ProviderLiveResult]:
    """
    Query the provider's own endpoint for a specific model's capabilities.

    Parameters
    ----------
    provider_id
        Canonical provider identifier (``"ollama"``, ``"lmstudio"``,
        ``"google"``, ...).
    model_id
        The model ID as the provider knows it. Must be the provider's
        own identifier (e.g. ``"llama3.2:7b"`` for Ollama,
        ``"google/gemma-2-9b"`` for LM Studio).
    connection
        Per-connection endpoint + credentials. ``base_url`` and
        ``api_key`` are used; everything else is ignored.
    timeout
        HTTP timeout in seconds. Defaults to 5s.

    Returns
    -------
    Optional[ProviderLiveResult]
        Populated when the provider returned usable capability data.
        ``None`` when:
            * The provider has no capability endpoint.
            * The provider is unreachable.
            * The provider returned a 404 or an unexpected shape.
            * The provider's response did not carry any capability
              facts (e.g. an older Ollama version without the
              ``capabilities`` field).

    Never raises.
    """
    normalized = (provider_id or "").strip().lower()

    if normalized == "ollama":
        return await _query_ollama(model_id, connection, timeout=timeout)

    if normalized in ("lmstudio", "lm_studio", "lm-studio"):
        return await _query_lmstudio(model_id, connection, timeout=timeout)

    if normalized in ("google", "gemini", "google_genai", "google-genai"):
        return await _query_google(model_id, connection, timeout=timeout)

    # OpenAI, Anthropic, DeepSeek, xAI, Mistral, Groq, ... — no
    # capability endpoint exists. Return None so the merge layer falls
    # back to cache / LiteLLM honestly.
    logger.debug(
        "No live capability endpoint for provider %r; skipping.",
        provider_id,
    )
    return None


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


async def _query_ollama(
    model_id: str,
    connection: ConnectionContext,
    *,
    timeout: float,
) -> Optional[ProviderLiveResult]:
    """
    Query Ollama's ``/api/show`` endpoint.

    Ollama returns::

        {
          "capabilities": ["completion", "tools", "thinking", "vision"],
          "details": {
            "family": "llama",
            "parameter_size": "8.0B",
            "quantization_level": "Q4_K_M"
          },
          "model_info": {
            "llama.context_length": 131072,
            ...
          }
        }

    The ``capabilities`` array is exhaustive: if ``"vision"`` is not
    present, the model definitively does NOT support vision. Absence is
    treated as ``False`` (not ``unknown``).

    Older Ollama versions do not include ``capabilities`` at all. In
    that case we return ``None`` because we have no capability facts —
    only descriptive details. The caller can still benefit from the
    context window if a future cache exists, but we do not fabricate
    capability answers.
    """
    base = (connection.base_url or "").rstrip("/")
    if not base:
        return None
    # Strip /v1 — Ollama's native API lives at the root.
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}{_OLLAMA_SHOW_PATH}"

    body = {"name": model_id}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if connection.api_key:
        headers["Authorization"] = f"Bearer {connection.api_key}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except Exception as exc:
        logger.debug(
            "Ollama /api/show network error for %r: %s", model_id, exc
        )
        return None

    if resp.status_code == 404:
        logger.debug(
            "Ollama /api/show returned 404 for %r (model not found).",
            model_id,
        )
        return None
    if resp.status_code != 200:
        logger.debug(
            "Ollama /api/show returned HTTP %d for %r.",
            resp.status_code,
            model_id,
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.debug(
            "Ollama /api/show returned non-JSON for %r: %s", model_id, exc
        )
        return None

    if not isinstance(data, dict):
        return None

    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, list):
        # Older Ollama — no capabilities field. Do not fabricate.
        logger.debug(
            "Ollama /api/show for %r has no `capabilities` array "
            "(older Ollama version). Returning None so the caller "
            "falls back to other sources.",
            model_id,
        )
        return None

    caps_set = {str(c).lower() for c in raw_caps if isinstance(c, str)}

    # Exhaustive: absence means definitively unsupported.
    capabilities: Dict[str, bool] = {
        "vision": "vision" in caps_set,
        "tools": "tools" in caps_set,
        "reasoning": "thinking" in caps_set,
    }

    # Context from model_info — the key is "{architecture}.context_length".
    context: Optional[int] = None
    model_info = data.get("model_info")
    if isinstance(model_info, dict):
        for key, val in model_info.items():
            if isinstance(key, str) and key.endswith(".context_length"):
                if isinstance(val, int) and val > 0:
                    context = val
                    break

    # Descriptive metadata from details.
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    metadata: Dict[str, Any] = {}
    params_str = details.get("parameter_size")
    if isinstance(params_str, str) and params_str:
        metadata["params_string"] = params_str
    quant_str = details.get("quantization_level")
    if isinstance(quant_str, str) and quant_str:
        metadata["quantization_name"] = quant_str
    family = details.get("family")
    if isinstance(family, str) and family:
        metadata["family"] = family
        metadata["architecture"] = family

    return ProviderLiveResult(
        provider_id="ollama",
        model_id=model_id,
        capabilities=capabilities,
        context_window=context,
        max_output_tokens=None,
        metadata=metadata,
        raw=data,
    )


# ---------------------------------------------------------------------------
# LM Studio
# ---------------------------------------------------------------------------


async def _query_lmstudio(
    model_id: str,
    connection: ConnectionContext,
    *,
    timeout: float,
) -> Optional[ProviderLiveResult]:
    """
    Query LM Studio's ``/api/v0/models`` endpoint.

    Each entry in the response carries::

        {
          "id": "google/gemma-2-9b",
          "object": "model",
          "type": "llm" | "vlm" | "embeddings",
          "publisher": "google",
          "arch": "gemma2",
          "compatibility_type": "gguf",
          "quantization": "Q4_K_M",
          "state": "loaded" | "not-loaded",
          "max_context_length": 8192,
          "loaded_context_length": 4096
        }

    ``type == "vlm"`` means the model has a vision encoder. ``type ==
    "llm"`` means it definitively does not (LM Studio marks VLMs
    explicitly). Tools and reasoning are NOT declared by LM Studio, so
    we leave those out of ``capabilities`` — the merge layer treats
    missing keys as unknown.
    """
    base = (connection.base_url or "").rstrip("/")
    if not base:
        return None
    # Strip /v1 suffix.
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}{_LMSTUDIO_MODELS_PATH}"

    headers = {"Accept": "application/json"}
    if connection.api_key:
        headers["Authorization"] = f"Bearer {connection.api_key}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        logger.debug(
            "LM Studio /api/v0/models network error: %s", exc
        )
        return None

    if resp.status_code != 200:
        logger.debug(
            "LM Studio /api/v0/models returned HTTP %d.", resp.status_code
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.debug(
            "LM Studio /api/v0/models returned non-JSON: %s", exc
        )
        return None

    if not isinstance(data, dict):
        return None

    raw_models = data.get("data") or data.get("models") or []
    if not isinstance(raw_models, list):
        return None

    # Find the entry that matches the requested model_id. LM Studio
    # returns IDs with an owner prefix (e.g.
    # "deepseek/deepseek-r1-0528-qwen3-8b"), but the vault may store
    # the short form. Both are the SAME model — matching them is ID
    # normalization, not capability inference.
    target: Optional[Dict[str, Any]] = None
    matched_server_id: Optional[str] = None
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id") or entry.get("name")
        if not isinstance(entry_id, str):
            continue
        if entry_id == model_id:
            target = entry
            matched_server_id = entry_id
            break
        # Short-form match: "deepseek/x" → "x"
        if "/" in entry_id and entry_id.rsplit("/", 1)[-1] == model_id:
            target = entry
            matched_server_id = entry_id
            break
        # Reverse short-form match: stored as "x", server has "owner/x"
        if "/" not in model_id and entry_id.endswith("/" + model_id):
            target = entry
            matched_server_id = entry_id
            break

    if target is None:
        logger.debug(
            "LM Studio /api/v0/models has no entry matching %r.", model_id
        )
        return None

    # ------------------------------------------------------------------
    # Definitive vision detection from `type`.
    #
    # LM Studio marks vision-enabled models as `type: "vlm"` and
    # text-only models as `type: "llm"`. Embeddings models have
    # `type: "embeddings"` and do not participate in chat capabilities.
    # ------------------------------------------------------------------
    model_type = str(target.get("type") or "").lower()
    capabilities: Dict[str, bool] = {}
    if model_type == "vlm":
        capabilities["vision"] = True
    elif model_type == "llm":
        capabilities["vision"] = False
    # Embeddings type — not a chat model. Skip capability claims.

    # ------------------------------------------------------------------
    # Tool-use detection from the `capabilities` array.
    #
    # LM Studio v0.4.x+ exposes an optional `capabilities` array on each
    # model entry. Currently the only documented value is `"tool_use"`,
    # which means the model was loaded with a chat template that
    # declares tool support. Its ABSENCE means one of two things:
    #
    #   1. The field is missing entirely → the model does not declare
    #      tool support. We report `unknown` (do not fabricate either
    #      True or False).
    #
    #   2. The array is present but does not contain `"tool_use"` → the
    #      model explicitly does NOT support tools. We report `False`.
    #
    # Both cases are surfaced honestly. The user can override via the
    # info modal if their experience differs from LM Studio's
    # declaration.
    # ------------------------------------------------------------------
    raw_caps = target.get("capabilities")
    if isinstance(raw_caps, list):
        caps_set = {str(c).lower() for c in raw_caps if isinstance(c, str)}
        # The array is exhaustive for the capabilities LM Studio knows
        # about. `"tool_use"` is the only one currently documented.
        capabilities["tools"] = "tool_use" in caps_set
        # Note: LM Studio does NOT declare `vision` in this array — it
        # uses `type: "vlm"` for that, handled above. It also does not
        # declare `reasoning`. Both stay absent from the map unless
        # their dedicated source (type or a future field) provides them.

    # Context window.
    context: Optional[int] = None
    ctx_val = target.get("max_context_length")
    if isinstance(ctx_val, int) and ctx_val > 0:
        context = ctx_val

    # Descriptive metadata.
    metadata: Dict[str, Any] = {}
    publisher = target.get("publisher")
    if isinstance(publisher, str) and publisher:
        metadata["publisher"] = publisher
    arch = target.get("arch")
    if isinstance(arch, str) and arch:
        metadata["architecture"] = arch
    quant = target.get("quantization")
    if isinstance(quant, str) and quant:
        metadata["quantization_name"] = quant

    # Include the server-side ID in metadata so the caller can see
    # WHICH entry matched (helps diagnose "why is my model showing
    # no capabilities?" — the answer is often "the server has it
    # under a different key").
    if matched_server_id and matched_server_id != model_id:
        metadata["server_model_id"] = matched_server_id

    return ProviderLiveResult(
        provider_id="lmstudio",
        model_id=model_id,  # Use the requested ID so the vault merge lands on the correct key
        capabilities=capabilities,
        context_window=context,
        max_output_tokens=None,
        metadata=metadata,
        raw=target,
    )


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------


async def _query_google(
    model_id: str,
    connection: ConnectionContext,
    *,
    timeout: float,
) -> Optional[ProviderLiveResult]:
    """
    Query Google's per-model endpoint.

    Google returns::

        {
          "name": "models/gemini-1.5-pro",
          "baseModelId": "gemini-1.5-pro-001",
          "version": "001",
          "displayName": "Gemini 1.5 Pro",
          "description": "...",
          "inputTokenLimit": 2097152,
          "outputTokenLimit": 8192,
          "supportedGenerationMethods": ["generateContent", "countTokens"],
          "temperature": 1,
          "maxTemperature": 2,
          "topP": 0.95,
          "topK": 64
        }

    Google does NOT declare vision, tools, or reasoning in this
    endpoint. Only context limits and generation methods are available.
    We return only those facts — the merge layer knows capability
    answers are absent.

    Model name normalization:
        * Google's API expects ``models/gemini-pro`` as the path
          segment. If the caller passes ``gemini-pro`` (the short
          form commonly used in PELDRUN), we prepend ``models/``.
        * If the caller already passes ``models/gemini-pro``, it is
          used verbatim.
    """
    base = (connection.base_url or "").rstrip("/")
    if not base:
        # Google has a canonical base; use it when the caller did not
        # provide one (very common for cloud providers).
        base = "https://generativelanguage.googleapis.com"

    # Normalize model name to the path form.
    normalized_model = model_id.strip()
    if not normalized_model.startswith("models/"):
        normalized_model = f"models/{normalized_model}"

    path = _GOOGLE_MODEL_PATH_TEMPLATE.format(model_name=normalized_model)
    url = f"{base}{path}"

    headers = {"Accept": "application/json"}
    if connection.api_key:
        # Google supports both header styles.
        headers["x-goog-api-key"] = connection.api_key

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        logger.debug("Google model query network error: %s", exc)
        return None

    if resp.status_code == 404:
        logger.debug("Google model query 404 for %r.", model_id)
        return None
    if resp.status_code != 200:
        logger.debug(
            "Google model query returned HTTP %d for %r.",
            resp.status_code,
            model_id,
        )
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.debug("Google model query returned non-JSON: %s", exc)
        return None

    if not isinstance(data, dict):
        return None

    # Context limits — the only definitive facts Google exposes here.
    ctx_val = data.get("inputTokenLimit")
    context: Optional[int] = ctx_val if isinstance(ctx_val, int) and ctx_val > 0 else None
    out_val = data.get("outputTokenLimit")
    max_out: Optional[int] = out_val if isinstance(out_val, int) and out_val > 0 else None

    # Descriptive metadata.
    metadata: Dict[str, Any] = {"publisher": "google"}
    display_name = data.get("displayName")
    if isinstance(display_name, str) and display_name:
        metadata["display_name"] = display_name

    # No capabilities are returned by Google here — leave the
    # ``capabilities`` dict empty so the merge layer treats vision,
    # tools, and reasoning as unknown.
    return ProviderLiveResult(
        provider_id="google",
        model_id=model_id,
        capabilities={},
        context_window=context,
        max_output_tokens=max_out,
        metadata=metadata,
        raw=data,
    )


# ---------------------------------------------------------------------------
# Convenience: run multiple queries concurrently
# ---------------------------------------------------------------------------


async def query_providers_batch(
    provider_id: str,
    model_ids: list[str],
    connection: ConnectionContext,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    concurrency: int = 4,
) -> Dict[str, ProviderLiveResult]:
    """
    Query multiple models concurrently.

    Used by the "Scan Models" enrichment path in
    ``config_rtr.py::fetch-models``. The default concurrency of 4
    keeps a 20-model scan under ~10 seconds even with a cold provider,
    while remaining polite to local inference servers (Ollama, LM
    Studio) that dislike being hammered with parallel requests.

    Returns
    -------
    Dict[str, ProviderLiveResult]
        Mapping of ``model_id`` to its result. Models that failed
        (returned ``None``) are OMITTED from the dict — the caller
        should treat their absence as "no live data available".
    """
    if not model_ids:
        return {}

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _guarded(mid: str) -> Optional[tuple[str, Optional[ProviderLiveResult]]]:
        async with sem:
            try:
                result = await query_provider_live(
                    provider_id=provider_id,
                    model_id=mid,
                    connection=connection,
                    timeout=timeout,
                )
                return (mid, result)
            except Exception as exc:
                logger.debug(
                    "Batch query failed for %r: %s", mid, exc
                )
                return (mid, None)

    results = await asyncio.gather(*[_guarded(mid) for mid in model_ids])
    out: Dict[str, ProviderLiveResult] = {}
    for item in results:
        if item is None:
            continue
        mid, res = item
        if res is not None:
            out[mid] = res
    return out


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ProviderLiveResult",
    "query_provider_live",
    "query_providers_batch",
]
