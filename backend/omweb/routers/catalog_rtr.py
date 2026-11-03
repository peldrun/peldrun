# backend/omweb/routers/catalog_rtr.py
"""
PELDRUN Model Catalog Router.

Exposes the capability lookup layer (``lookup_model_facts``) and the
LiteLLM external registry as REST endpoints. Used by the frontend to
display truthful capability badges in ``ModelItemCard`` /
``ModelInfoModal``.

Design principles
-----------------

1. **No live provider queries.**
   This router NEVER calls Ollama's ``/api/show``, LM Studio's
   ``/api/v0/models``, or Google's per-model endpoint. Those queries
   belong to the Scan Models flow (``/api/config/fetch-models``),
   which caches every result into ``models_metadata.json``. This
   router only reads from that cache plus LiteLLM / HuggingFace as
   fallbacks. The separation is structural: nothing in this file can
   accidentally trigger a live scan.

2. **Honest sources.**
   Every response carries a ``sources`` map that names the true origin
   of each capability. If the answer is "we don't know", the value is
   ``"unknown"`` and the UI shows "Not reported" — not a fabricated
   badge.

3. **Idempotent reads.**
   ``GET /models/{model_id}`` never mutates state. Only the explicit
   ``POST /refresh`` endpoint triggers a network action, and even that
   only refreshes the LiteLLM cache (never the provider).

4. **Robust against broken inputs.**
   A model ID with slashes (e.g. ``google/gemma-2-9b``) is accepted
   verbatim via FastAPI's ``:path`` converter. Empty IDs, malformed
   providers, and missing vault entries all return clean responses
   rather than 5xx errors.

Mounting
--------

This router should be mounted by ``backend/omweb/main.py``::

    from omweb.routers import catalog_rtr
    app.include_router(
        catalog_rtr.router,
        prefix="/api/catalog",
        tags=["catalog"],
    )

See also
--------

    * ``backend/peldrun/llm/catalog/lookup.py`` — the merge layer
    * ``backend/peldrun/llm/catalog/external_registry.py`` — LiteLLM
    * ``backend/peldrun/llm/catalog/huggingface.py`` — HF config
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.catalog.external_registry import (
    REGISTRY_URL,
    get_external_model_registry,
)
from peldrun.llm.catalog.huggingface import get_huggingface_config_loader
from peldrun.llm.catalog.lookup import lookup_model_facts

logger = logging.getLogger("omweb.routers.catalog_rtr")

router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CapabilityFact(BaseModel):
    """One resolved capability, ready for the UI."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    value: bool
    source: str = Field(
        ...,
        description=(
            "Origin of this fact. One of: manual, provider_live, "
            "model_metadata, provider_metadata, huggingface_config, "
            "external_registry, provider_catalog, capability_probe, "
            "runtime_observation, unknown."
        ),
    )
    known: bool = Field(
        ...,
        description=(
            "True when the source is anything other than 'unknown'. "
            "False means the UI should render an Enable button rather "
            "than a definitive badge."
        ),
    )


class ModelFactsResponse(BaseModel):
    """Response body for GET /models/{model_id}."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ok: bool
    provider_id: str
    model_id: str

    vision: CapabilityFact
    tools: CapabilityFact
    reasoning: CapabilityFact

    reasoning_mode: str = Field(
        default="none",
        description=(
            "Reasoning control mode: none / automatic / adaptive / "
            "toggle / effort / budget / unreported. Drives which UI "
            "control the modal renders."
        ),
    )
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    reasoning_default_effort: Optional[str] = None
    reasoning_min_budget_tokens: Optional[int] = None
    reasoning_max_budget_tokens: Optional[int] = None

    structured_output_mode: str = "unsupported"

    audio: CapabilityFact
    modalities_input: list[str] = Field(default_factory=lambda: ["text"])
    modalities_output: list[str] = Field(default_factory=lambda: ["text"])

    context_window: Optional[int] = None
    context_source: Optional[str] = None

    external_matches: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Which external sources matched this model, and under what "
            "key. Keys: 'litellm', 'huggingface'. Missing key means no "
            "match."
        ),
    )


class RefreshResponse(BaseModel):
    """Response body for POST /refresh."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ok: bool
    message: str
    model_count: int = 0


class CatalogStatus(BaseModel):
    """Response body for GET /status."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ok: bool
    litellm: Dict[str, Any]
    huggingface: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_fact(value: bool, source: str) -> CapabilityFact:
    """Build a ``CapabilityFact`` from a raw boolean + source string."""
    return CapabilityFact(
        value=bool(value),
        source=source,
        known=(source != "unknown"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/models/{model_id:path}", response_model=ModelFactsResponse)
async def get_model_facts(
    model_id: str = Path(
        ...,
        description=(
            "The model identifier. May contain slashes "
            "(e.g. 'google/gemma-2-9b'); the ':path' converter "
            "preserves them verbatim."
        ),
    ),
    provider: str = Query(
        ...,
        description=(
            "Canonical provider identifier (e.g. 'ollama', 'lmstudio', "
            "'openai'). Required because the same model ID can exist "
            "under multiple providers with different capabilities."
        ),
    ),
    connection_id: Optional[str] = Query(
        default=None,
        description=(
            "Optional connection identifier for composite metadata "
            "lookups. When omitted, the legacy single-key lookup is "
            "used."
        ),
    ),
) -> ModelFactsResponse:
    """
    Resolve a model's capabilities from the cache + external fallbacks.

    Reads exclusively from ``models_metadata.json`` (populated by the
    last Scan Models run + any user overrides) plus the LiteLLM and
    HuggingFace caches. Does NOT query the provider.

    When the model has no data in any source, every capability is
    returned with ``source="unknown"`` and ``known=false`` so the UI
    shows Enable buttons instead of fabricated badges.
    """
    provider_clean = (provider or "").strip().lower()
    model_clean = (model_id or "").strip()

    if not provider_clean:
        raise HTTPException(
            status_code=400,
            detail="Query parameter 'provider' is required.",
        )
    if not model_clean:
        raise HTTPException(
            status_code=400,
            detail="Model ID must be non-empty.",
        )

    try:
        result = await lookup_model_facts(
            provider_id=provider_clean,
            model_id=model_clean,
            connection_id=connection_id,
        )
    except Exception as exc:
        logger.exception(
            "lookup_model_facts failed for %s::%s: %s",
            provider_clean,
            model_clean,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Capability lookup failed: {exc}",
        )

    caps = result.capabilities
    sources = result.sources

    reasoning_details = caps.reasoning_details
    tool_details = caps.tool_calling
    structured_details = caps.structured_output

    # Audio is a first-class field in the response even though the
    # backend exposes it only through ``modalities.input``.
    audio_present = "audio" in (caps.modalities.input or [])
    audio_source = sources.get("audio")
    if audio_source is None:
        audio_source = "unknown"
    elif audio_present is False and audio_source != "unknown":
        # A definitive "no" was recorded — the field is supported being
        # false, not unknown.
        pass

    return ModelFactsResponse(
        ok=True,
        provider_id=provider_clean,
        model_id=model_clean,
        vision=_to_fact(
            bool(caps.vision),
            sources.get("vision", "unknown").value
            if hasattr(sources.get("vision", "unknown"), "value")
            else str(sources.get("vision", "unknown")),
        ),
        tools=_to_fact(
            bool(caps.trained_for_tool_use),
            sources.get("tool_calling", "unknown").value
            if hasattr(sources.get("tool_calling", "unknown"), "value")
            else str(sources.get("tool_calling", "unknown")),
        ),
        reasoning=_to_fact(
            bool(caps.reasoning),
            sources.get("reasoning", "unknown").value
            if hasattr(sources.get("reasoning", "unknown"), "value")
            else str(sources.get("reasoning", "unknown")),
        ),
        reasoning_mode=reasoning_details.mode.value
        if hasattr(reasoning_details.mode, "value")
        else str(reasoning_details.mode),
        reasoning_effort_levels=list(reasoning_details.effort_levels or []),
        reasoning_default_effort=reasoning_details.default_effort,
        reasoning_min_budget_tokens=reasoning_details.min_budget_tokens,
        reasoning_max_budget_tokens=reasoning_details.max_budget_tokens,
        structured_output_mode=structured_details.mode.value
        if hasattr(structured_details.mode, "value")
        else str(structured_details.mode),
        audio=_to_fact(audio_present, audio_source.value
                       if hasattr(audio_source, "value")
                       else str(audio_source)),
        modalities_input=list(caps.modalities.input or ["text"]),
        modalities_output=list(caps.modalities.output or ["text"]),
        context_window=result.context_window,
        context_source=result.context_source,
        external_matches=dict(result.external_matches or {}),
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_external_catalog() -> RefreshResponse:
    """
    Force-refresh the LiteLLM external registry from GitHub.

    This is the ONLY endpoint in this router that triggers a network
    action. It does NOT touch any provider's endpoint and does NOT
    modify ``models_metadata.json`` — it only refreshes the cached
    copy of the LiteLLM JSON.

    Typical usage: the user opens Settings → Catalog and clicks
    "Refresh LiteLLM cache" when they want the newest models without
    waiting for the 24-hour TTL.
    """
    try:
        registry = get_external_model_registry()
        ok = await registry.refresh()
    except Exception as exc:
        logger.exception("LiteLLM refresh failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"LiteLLM refresh failed: {exc}",
        )

    if not ok:
        return RefreshResponse(
            ok=False,
            message=(
                "LiteLLM registry could not be refreshed. The previous "
                "cache (if any) is still being served. Check network "
                "connectivity to GitHub."
            ),
            model_count=registry.model_count(),
        )

    return RefreshResponse(
        ok=True,
        message=(
            f"LiteLLM registry refreshed successfully "
            f"({registry.model_count()} models)."
        ),
        model_count=registry.model_count(),
    )


@router.get("/status", response_model=CatalogStatus)
async def get_catalog_status() -> CatalogStatus:
    """
    Return the current state of every external catalog.

    Used by the Settings UI to show cache health without triggering any
    network call:
        * LiteLLM: model count, last fetch time, staleness.
        * HuggingFace: cache directory presence + file count.

    Both are read-only snapshots.
    """
    # ------------------------------------------------------------------
    # LiteLLM status.
    # ------------------------------------------------------------------
    litellm_status: Dict[str, Any]
    try:
        registry = get_external_model_registry()
        # Trigger lazy load if the cache exists on disk. This is a
        # synchronous disk read only — no network.
        registry._ensure_loaded()  # noqa: SLF001 — read-only accessor
        litellm_status = {
            "loaded": registry.is_loaded(),
            "model_count": registry.model_count(),
            "is_stale": registry.is_stale(),
            "source_url": REGISTRY_URL,
        }
    except Exception as exc:
        logger.warning("LiteLLM status lookup failed: %s", exc)
        litellm_status = {
            "loaded": False,
            "model_count": 0,
            "is_stale": True,
            "source_url": REGISTRY_URL,
            "error": str(exc),
        }

    # ------------------------------------------------------------------
    # HuggingFace status.
    # ------------------------------------------------------------------
    hf_status: Dict[str, Any]
    try:
        loader = get_huggingface_config_loader()
        cache_dir = loader._cache_dir  # noqa: SLF001 — read-only
        file_count = 0
        if cache_dir.exists():
            for _ in cache_dir.rglob("*.json"):
                file_count += 1
        hf_status = {
            "cache_dir_exists": cache_dir.exists(),
            "cached_model_count": file_count,
        }
    except Exception as exc:
        logger.warning("HuggingFace status lookup failed: %s", exc)
        hf_status = {
            "cache_dir_exists": False,
            "cached_model_count": 0,
            "error": str(exc),
        }

    return CatalogStatus(
        ok=True,
        litellm=litellm_status,
        huggingface=hf_status,
    )


__all__ = ["router"]
