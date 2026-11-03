# backend/peldrun/llm/catalog/lookup.py
"""
PELDRUN Model Facts Lookup — Cache-First Merge Layer.

Combines every available source of model facts into a single
``LookupResult`` with a per-field source marker so the UI can honestly
report where each capability came from.

Data flow
---------

This module NEVER performs a live provider query. It only reads from:

    1. ``models_metadata.json`` (the local vault)
       — populated by the user (manual), or by the last Scan Models
         run (``provider_live``), or by legacy saveLocalMetadataVault
         (``provider_catalog`` / ``model_metadata``).

    2. LiteLLM external registry (cached JSON)
    3. HuggingFace config.json (per-model disk cache)

Live provider queries belong to the Scan Models flow
(``config_rtr.py::fetch-models``) which uses
``catalog.provider_live.query_providers_batch`` explicitly.

The user requirement — "query once, save, never re-query unless the
user clicks Scan Models again" — is enforced structurally: this module
has no code path that calls the provider's capability endpoint. The
only way to populate a ``provider_live`` source in the vault is via
the explicit Scan Models flow.

Priority order (matches ``capability_resolver._SOURCE_PRIORITY``)
------------------------------------------------------------------

    MANUAL                100
    PROVIDER_LIVE          95   (from last Scan Models)
    MODEL_METADATA         80
    PROVIDER_METADATA      70
    HUGGINGFACE_CONFIG     67
    EXTERNAL_REGISTRY      65
    PROVIDER_CATALOG       60
    CAPABILITY_PROBE       40
    RUNTIME_OBSERVATION    20
    UNKNOWN                 0

Design principles
-----------------

1. **No live queries.** See "Data flow" above.
2. **No heuristics.** If no source knows, the result is ``unknown``.
3. **Never raises.** Network failures, malformed JSON, and validation
   errors are logged and skipped.
4. **Preserves provenance.** Every field carries its true source.

See also
--------

    * ``backend/peldrun/llm/catalog/provider_live.py`` — live queries
    * ``backend/peldrun/llm/catalog/external_registry.py`` — LiteLLM
    * ``backend/peldrun/llm/catalog/huggingface.py`` — HF config
    * ``backend/peldrun/llm/capabilities.py`` — intersection
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.capability_resolver import resolve_model_capabilities
from peldrun.llm.catalog.external_registry import (
    RegistryEntry,
    get_external_model_registry,
)
from peldrun.llm.catalog.huggingface import (
    HFModelConfig,
    get_huggingface_config_loader,
)
from peldrun.llm.contracts import (
    CapabilitySource,
    CapabilityState,
    ModelCapabilities,
    ReasoningCapability,
    ReasoningMode,
    StructuredOutputCapability,
    StructuredOutputMode,
    ToolCallingCapability,
    ToolCallingMode,
)

logger = logging.getLogger("peldrun.llm.catalog.lookup")


# ---------------------------------------------------------------------------
# Priority map
# ---------------------------------------------------------------------------


_PRIORITY: Dict[CapabilitySource, int] = {
    CapabilitySource.MANUAL: 100,
    CapabilitySource.PROVIDER_LIVE: 95,
    CapabilitySource.MODEL_METADATA: 80,
    CapabilitySource.PROVIDER_METADATA: 70,
    CapabilitySource.HUGGINGFACE_CONFIG: 67,
    CapabilitySource.EXTERNAL_REGISTRY: 65,
    CapabilitySource.PROVIDER_CATALOG: 60,
    CapabilitySource.CAPABILITY_PROBE: 40,
    CapabilitySource.RUNTIME_OBSERVATION: 20,
    CapabilitySource.UNKNOWN: 0,
}


def _priority(source: CapabilitySource) -> int:
    return _PRIORITY.get(source, 0)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class LookupResult(BaseModel):
    """
    Final result of a lookup.

    Attributes
    ----------
    capabilities
        Ready to feed to ``capabilities.compute_resolved_capabilities``.
    sources
        Per-field origin of every resolved fact. Keys:
        ``vision``, ``audio``, ``tool_calling``, ``reasoning``,
        ``structured_output``. Values are ``CapabilitySource`` strings.
    external_matches
        Which external sources matched and under what key. Keys:
        ``"litellm"``, ``"huggingface"``. Used by the UI to display
        provenance ("Reported by LiteLLM (key: deepseek-r1)").
    context_window / context_source
        Best-known context window and its origin, when neither the
        metadata nor LiteLLM nor HF provided one in ``capabilities``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    capabilities: ModelCapabilities
    sources: Dict[str, CapabilitySource] = Field(default_factory=dict)
    external_matches: Dict[str, str] = Field(default_factory=dict)
    context_window: Optional[int] = None
    context_source: Optional[str] = None

    def to_capabilities(self) -> ModelCapabilities:
        return self.capabilities


# ---------------------------------------------------------------------------
# Per-field merge helpers
# ---------------------------------------------------------------------------


def _merge_vision(
    metadata_caps: ModelCapabilities,
    litellm_entry: Optional[RegistryEntry],
    hf_config: Optional[HFModelConfig],
) -> Tuple[bool, CapabilitySource]:
    """
    Merge the vision capability from every source.

    The candidates are ordered by priority; the highest wins.
    """
    candidates: List[Tuple[bool, CapabilitySource]] = []

    meta_source = metadata_caps.modalities.source
    if meta_source != CapabilitySource.UNKNOWN:
        candidates.append((bool(metadata_caps.vision), meta_source))

    if hf_config is not None and hf_config.has_vision is not None:
        candidates.append(
            (bool(hf_config.has_vision), CapabilitySource.HUGGINGFACE_CONFIG)
        )

    if litellm_entry is not None:
        val = litellm_entry.get("supports_vision")
        if isinstance(val, bool):
            candidates.append((val, CapabilitySource.EXTERNAL_REGISTRY))

    if not candidates:
        return False, CapabilitySource.UNKNOWN

    candidates.sort(key=lambda c: _priority(c[1]), reverse=True)
    return candidates[0]


def _merge_audio(
    metadata_caps: ModelCapabilities,
    litellm_entry: Optional[RegistryEntry],
    hf_config: Optional[HFModelConfig],
) -> Tuple[bool, CapabilitySource]:
    candidates: List[Tuple[bool, CapabilitySource]] = []

    if "audio" in (metadata_caps.modalities.input or []):
        candidates.append(
            (
                True,
                metadata_caps.modalities.source
                or CapabilitySource.MODEL_METADATA,
            )
        )

    if hf_config is not None and hf_config.has_audio is not None:
        candidates.append(
            (bool(hf_config.has_audio), CapabilitySource.HUGGINGFACE_CONFIG)
        )

    if litellm_entry is not None:
        val = litellm_entry.get("supports_audio_input")
        if isinstance(val, bool):
            candidates.append((val, CapabilitySource.EXTERNAL_REGISTRY))

    if not candidates:
        return False, CapabilitySource.UNKNOWN

    candidates.sort(key=lambda c: _priority(c[1]), reverse=True)
    return candidates[0]


def _merge_tools(
    metadata_caps: ModelCapabilities,
    litellm_entry: Optional[RegistryEntry],
) -> ToolCallingCapability:
    """
    Merge tool-calling capability.

    Only metadata + LiteLLM provide tool info. HF ``config.json`` has no
    field for it. Parallel and forced-choice are never inferred from
    LiteLLM — those sub-capabilities remain at metadata defaults.
    """
    meta = metadata_caps.tool_calling
    candidates: List[ToolCallingCapability] = []

    if meta.state != CapabilityState.UNKNOWN:
        candidates.append(meta)

    if litellm_entry is not None:
        val = litellm_entry.get("supports_function_calling")
        if isinstance(val, bool):
            candidates.append(
                ToolCallingCapability(
                    state=(
                        CapabilityState.SUPPORTED
                        if val
                        else CapabilityState.UNSUPPORTED
                    ),
                    source=CapabilitySource.EXTERNAL_REGISTRY,
                    mode=(
                        ToolCallingMode.NATIVE
                        if val
                        else ToolCallingMode.UNSUPPORTED
                    ),
                    parallel=False,
                    forced_choice=False,
                )
            )

    if not candidates:
        return meta

    candidates.sort(key=lambda c: _priority(c.source), reverse=True)
    return candidates[0]


def _merge_reasoning(
    metadata_caps: ModelCapabilities,
    litellm_entry: Optional[RegistryEntry],
) -> ReasoningCapability:
    """
    Merge reasoning capability.

    LiteLLM only provides a boolean. When it is the winner and it says
    yes, the mode is set to ``ADAPTIVE`` — the honest default when the
    control surface is unknown. The user can still flip it to
    toggle/effort/budget in the UI, writing a ``manual`` override.
    """
    meta = metadata_caps.reasoning_details
    candidates: List[ReasoningCapability] = []

    if meta.state != CapabilityState.UNKNOWN:
        candidates.append(meta)

    if litellm_entry is not None:
        val = litellm_entry.get("supports_reasoning")
        if isinstance(val, bool):
            candidates.append(
                ReasoningCapability(
                    state=(
                        CapabilityState.SUPPORTED
                        if val
                        else CapabilityState.UNSUPPORTED
                    ),
                    source=CapabilitySource.EXTERNAL_REGISTRY,
                    mode=ReasoningMode.ADAPTIVE if val else ReasoningMode.NONE,
                )
            )

    if not candidates:
        return meta

    candidates.sort(key=lambda c: _priority(c.source), reverse=True)
    return candidates[0]


def _merge_structured_output(
    metadata_caps: ModelCapabilities,
    litellm_entry: Optional[RegistryEntry],
) -> StructuredOutputCapability:
    meta = metadata_caps.structured_output
    candidates: List[StructuredOutputCapability] = []

    if meta.state != CapabilityState.UNKNOWN:
        candidates.append(meta)

    if litellm_entry is not None:
        val = litellm_entry.get("supports_response_schema")
        if isinstance(val, bool):
            candidates.append(
                StructuredOutputCapability(
                    state=(
                        CapabilityState.SUPPORTED
                        if val
                        else CapabilityState.UNSUPPORTED
                    ),
                    source=CapabilitySource.EXTERNAL_REGISTRY,
                    mode=(
                        StructuredOutputMode.JSON_SCHEMA
                        if val
                        else StructuredOutputMode.UNSUPPORTED
                    ),
                )
            )

    if not candidates:
        return meta

    candidates.sort(key=lambda c: _priority(c.source), reverse=True)
    return candidates[0]


def _merge_context_window(
    metadata_max_context: Optional[int],
    litellm_entry: Optional[RegistryEntry],
    hf_config: Optional[HFModelConfig],
) -> Tuple[Optional[int], Optional[str]]:
    """
    Merge the context window.

    Metadata (models_metadata.json) wins because it holds the value the
    provider reported for the exact model version. LiteLLM / HF fill in
    only when metadata has no value.
    """
    if isinstance(metadata_max_context, int) and metadata_max_context > 0:
        return metadata_max_context, "metadata"

    if litellm_entry is not None:
        val = litellm_entry.get("max_input_tokens")
        if isinstance(val, int) and val > 0:
            return val, "litellm"

    if (
        hf_config is not None
        and hf_config.max_position_embeddings is not None
    ):
        return hf_config.max_position_embeddings, "huggingface"

    return None, None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def lookup_model_facts(
    provider_id: str,
    model_id: str,
    *,
    connection_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    use_litellm: bool = True,
    use_huggingface: bool = True,
) -> LookupResult:
    """
    Resolve model capabilities from CACHE + external fallbacks.

    Live provider queries are NOT performed here. To populate the cache
    via the provider's own endpoint, use ``config_rtr.py::fetch-models``
    (the Scan Models button). This function only reads.

    Parameters
    ----------
    provider_id / model_id / connection_id
        Identity of the model.
    metadata
        The ``models_metadata.json`` vault. When ``None``, loaded from
        disk on demand.
    use_litellm / use_huggingface
        Disable either to skip that external source.

    Returns
    -------
    LookupResult
        Always. Even when both external sources fail, the metadata
        resolver still produces the correct result from the cache.
    """
    # ------------------------------------------------------------------
    # Step 1 — cache resolution (sync).
    #
    # resolve_model_capabilities honors manual overrides, provider_live
    # cache entries, model_metadata, and provider_catalog. Only the
    # fields whose state is UNKNOWN at the end of this step are
    # candidates for external fallback.
    # ------------------------------------------------------------------
    metadata_caps = resolve_model_capabilities(
        provider_id=provider_id,
        model_id=model_id,
        connection_id=connection_id,
        metadata=metadata,
    )

    # ------------------------------------------------------------------
    # Step 2 — external fallbacks (async, best-effort).
    # ------------------------------------------------------------------
    litellm_entry: Optional[RegistryEntry] = None
    hf_config: Optional[HFModelConfig] = None
    external_matches: Dict[str, str] = {}

    if use_litellm:
        try:
            registry = get_external_model_registry()
            await registry.ensure_fresh()
            litellm_entry = registry.lookup(model_id)
            if litellm_entry is not None:
                external_matches["litellm"] = litellm_entry.matched_key
        except Exception as exc:
            logger.debug(
                "LiteLLM lookup failed for %r: %s", model_id, exc
            )

    if use_huggingface:
        try:
            loader = get_huggingface_config_loader()
            hf_config = await loader.fetch_config(model_id)
            if hf_config is not None:
                external_matches["huggingface"] = (
                    f"{hf_config.repo_id}@{hf_config.branch}"
                )
        except Exception as exc:
            logger.debug(
                "HuggingFace lookup failed for %r: %s", model_id, exc
            )

    # ------------------------------------------------------------------
    # Step 3 — merge.
    # ------------------------------------------------------------------
    vision_bool, vision_source = _merge_vision(
        metadata_caps, litellm_entry, hf_config
    )
    audio_bool, audio_source = _merge_audio(
        metadata_caps, litellm_entry, hf_config
    )
    tool_caps = _merge_tools(metadata_caps, litellm_entry)
    reasoning_caps = _merge_reasoning(metadata_caps, litellm_entry)
    structured_caps = _merge_structured_output(
        metadata_caps, litellm_entry
    )

    # Rebuild the modalities input list.
    modalities_input: List[str] = list(
        metadata_caps.modalities.input or ["text"]
    )
    if vision_bool and "image" not in modalities_input:
        modalities_input.append("image")
    elif not vision_bool and vision_source != CapabilitySource.UNKNOWN:
        modalities_input = [
            m for m in modalities_input if m != "image"
        ]
    if audio_bool and "audio" not in modalities_input:
        modalities_input.append("audio")

    modalities_source = (
        vision_source
        if vision_source != CapabilitySource.UNKNOWN
        else (
            audio_source
            if audio_source != CapabilitySource.UNKNOWN
            else metadata_caps.modalities.source
        )
    )

    merged_caps = ModelCapabilities(
        vision=vision_bool,
        trained_for_tool_use=(
            tool_caps.state == CapabilityState.SUPPORTED
        ),
        reasoning=(
            True
            if reasoning_caps.state == CapabilityState.SUPPORTED
            else (
                False
                if reasoning_caps.state == CapabilityState.UNSUPPORTED
                else None
            )
        ),
        reasoning_details=reasoning_caps,
        tool_calling=tool_caps,
        streaming=metadata_caps.streaming,
        structured_output=structured_caps,
        modalities=type(metadata_caps.modalities)(
            input=modalities_input,
            output=list(metadata_caps.modalities.output or ["text"]),
            source=modalities_source,
        ),
    )

    # ------------------------------------------------------------------
    # Step 4 — context window fallback.
    # ------------------------------------------------------------------
    metadata_max_context = getattr(
        metadata_caps, "max_context_length", None
    )
    context_window, context_source = _merge_context_window(
        metadata_max_context, litellm_entry, hf_config
    )

    # ------------------------------------------------------------------
    # Step 5 — build result.
    # ------------------------------------------------------------------
    sources: Dict[str, CapabilitySource] = {
        "vision": vision_source,
        "audio": audio_source,
        "tool_calling": tool_caps.source,
        "reasoning": reasoning_caps.source,
        "structured_output": structured_caps.source,
    }

    logger.debug(
        "Lookup for %s::%s -> vision=%s/%s tools=%s/%s reasoning=%s/%s",
        provider_id,
        model_id,
        vision_bool,
        vision_source.value,
        merged_caps.trained_for_tool_use,
        tool_caps.source.value,
        merged_caps.reasoning,
        reasoning_caps.source.value,
    )

    return LookupResult(
        capabilities=merged_caps,
        sources=sources,
        external_matches=external_matches,
        context_window=context_window,
        context_source=context_source,
    )


__all__ = [
    "LookupResult",
    "lookup_model_facts",
]
