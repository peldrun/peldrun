# backend/peldrun/llm/capability_resolver.py
"""
PELDRUN Capability Resolver.

Merges capability information from all available sources into a single
``ModelCapabilities`` object per (provider, model) pair. This is the ONLY
supported entry point for resolving what a model can do — every caller
(``dispatcher.py``, ``executor.py``, the config router, the UI persistence
path) goes through this module.

Precedence (plan Section 4.1)
-----------------------------

    User Manual Override       (highest)
            down
    Explicit Model Metadata
            down
    Provider Model Catalog
            down
    Capability Probe
            down
    Runtime Observation
            down
    Unknown                    (lowest / fallback)

Design invariants
-----------------

1. **Read-only.** This module NEVER writes to ``models_metadata.json``.
   The frontend persists entries (via ``saveLocalMetadataVault``); this
   module only reads them. Persistence of a resolved snapshot is the
   responsibility of ``ResolvedCapabilities.to_metadata_dict()``
   (in ``capabilities.py``), triggered by explicit user saves.

2. **No heuristics as decision input.** The existing
   ``inferModelCapabilities()`` in ``modelMetadata.ts`` remains in the
   frontend as a DISPLAY-ONLY suggestion. This resolver NEVER reads
   model name patterns to determine capability. If a legacy entry
   contains only inferred booleans (no ``capability_sources``), the
   resolver uses the "safe legacy heuristic" documented below — which
   errs on the side of ``unknown`` rather than fabricating support.

3. **Composite key precedence** (plan Section 4.2):
       ``provider_id::connection_id::model_id`` (canonical, new)
           ↓
       ``provider_id::model_id``             (intermediate form)
           ↓
       ``model_id``                          (legacy)
   Legacy lookups are preserved so that existing entries keep working
   during migration.

4. **Per-field source tracking.** The resolver emits a per-field
   ``CapabilitySource`` alongside each value, so downstream code (and
   the UI) can display "manual override" vs "provider catalog".

5. **Never raises on malformed input.** A broken metadata entry yields
   ``unknown`` states, not an exception. The runtime never crashes
   because a user hand-edited a JSON file badly.

6. **Backward-compatible with the current ``models_metadata.json``.**
   Entries without ``capability_sources`` still resolve — the resolver
   applies the following conservative heuristic:

       * ``reasoning`` set to a boolean → source: ``manual``
         (only ``toggleModelReasoning`` and discovery write it, and
         discovery-set values are typically re-confirmed by the user).
       * ``vision`` / ``trained_for_tool_use`` set to a boolean →
         source: ``provider_catalog``
         (these are written by discovery, or inferred by the toggle
         helper — both cases are catalog-derived, never user-confirmed
         in the legacy UI).
       * Any field absent → source: ``unknown``.

   This preserves the user's reasoning toggle across migration without
   discarding catalog data for the other fields.

See also
--------

    * ``backend/peldrun/llm/contracts.py``    — vocabulary + models
    * ``backend/peldrun/llm/capabilities.py`` — ``ModelCapabilities`` consumer
    * ``backend/peldrun/llm/request.py``      — ``LLMRequest.model_capabilities``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.llm.contracts import (
    CapabilitySource,
    CapabilityState,
    ModelCapabilities,
    ModalitiesCapability,
    ReasoningCapability,
    ReasoningMode,
    StreamingCapability,
    StructuredOutputCapability,
    StructuredOutputMode,
    ToolCallingCapability,
    ToolCallingMode,
)

logger = logging.getLogger("peldrun.llm.capability_resolver")


# ---------------------------------------------------------------------------
# Source precedence
# ---------------------------------------------------------------------------


#: Higher number = higher priority. Any field with a lower-priority source
#: is discarded when a higher-priority source has a value for the same
#: field.
_SOURCE_PRIORITY: Dict[CapabilitySource, int] = {
    CapabilitySource.MANUAL: 100,
    # PROVIDER_LIVE = what the provider's own endpoint returned during
    # the last Scan Models run. Beats every non-manual source.
    CapabilitySource.PROVIDER_LIVE: 95,
    CapabilitySource.MODEL_METADATA: 80,
    CapabilitySource.PROVIDER_METADATA: 70,
    # HuggingFace config.json is author-published and definitive for
    # vision/audio presence; it beats LiteLLM when both speak.
    CapabilitySource.HUGGINGFACE_CONFIG: 67,
    CapabilitySource.EXTERNAL_REGISTRY: 65,
    CapabilitySource.PROVIDER_CATALOG: 60,
    CapabilitySource.CAPABILITY_PROBE: 40,
    CapabilitySource.RUNTIME_OBSERVATION: 20,
    CapabilitySource.UNKNOWN: 0,
}


def _priority(source: CapabilitySource) -> int:
    """Return the numeric priority of a capability source (higher wins)."""
    return _SOURCE_PRIORITY.get(source, 0)


# ---------------------------------------------------------------------------
# Composite key resolution
# ---------------------------------------------------------------------------


def _candidate_keys(
    provider_id: str,
    model_id: str,
    connection_id: Optional[str],
) -> List[str]:
    """
    Return the composite keys to try, in priority order.

    The canonical form is ``provider_id::connection_id::model_id``. The
    intermediate and legacy forms follow. Short-form model ids (e.g.
    ``models/gemini-pro`` → ``gemini-pro``) are expanded at every level.
    """
    provider_norm = (provider_id or "").strip().lower()
    model_norm = (model_id or "").strip()
    conn_norm = (connection_id or "").strip()

    keys: List[str] = []

    def _push(key: str) -> None:
        if key and key not in keys:
            keys.append(key)

    # Variants of the model id (short and long).
    model_variants: List[str] = [model_norm]
    if "/" in model_norm:
        short = model_norm.rsplit("/", 1)[-1]
        if short and short != model_norm:
            model_variants.append(short)
    else:
        model_variants.append(f"models/{model_norm}")

    # Canonical: provider::connection::model
    if conn_norm:
        for m in model_variants:
            _push(f"{provider_norm}::{conn_norm}::{m}")

    # Intermediate: provider::model
    for m in model_variants:
        _push(f"{provider_norm}::{m}")

    # Legacy: model (and short/long variants)
    for m in model_variants:
        _push(m)

    return keys


# ---------------------------------------------------------------------------
# Safe field extraction
# ---------------------------------------------------------------------------


def _as_bool(value: Any) -> Optional[bool]:
    """Return ``bool`` for a well-typed value, else ``None``."""
    if isinstance(value, bool):
        return value
    return None


def _as_str_list(value: Any) -> Optional[List[str]]:
    """Return ``List[str]`` for a well-typed value, else ``None``."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _as_int(value: Any) -> Optional[int]:
    """Return ``int`` for a well-typed value, else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Return ``Dict`` for a well-typed value, else ``None``."""
    if isinstance(value, dict):
        return value
    return None


# ---------------------------------------------------------------------------
# Legacy source inference
# ---------------------------------------------------------------------------


def _infer_legacy_source(
    field: str,
    raw_value: Any,
    entry: Dict[str, Any],
) -> CapabilitySource:
    """
    Determine the source of a legacy entry's field that has no explicit
    ``capability_sources`` record.

    Rules (per module docstring, invariant 6):

        * ``reasoning`` → ``manual``
          Only ``toggleModelReasoning`` and discovery write this field.
          Discovery-set values are re-confirmed by the user during
          migration; the toggle path is explicit intent.

        * ``vision`` / ``trained_for_tool_use`` → ``provider_catalog``
          These are written by discovery OR inferred by the toggle
          helper from model names. Both are catalog-derived — never
          user-confirmed in the legacy UI.

        * Any other field → ``unknown``
    """
    if raw_value is None:
        return CapabilitySource.UNKNOWN
    if field == "reasoning":
        return CapabilitySource.MANUAL
    if field in ("vision", "trained_for_tool_use"):
        return CapabilitySource.PROVIDER_CATALOG
    return CapabilitySource.UNKNOWN


def _read_explicit_sources(entry: Dict[str, Any]) -> Dict[str, CapabilitySource]:
    """
    Extract the ``capability_sources`` map from an entry when present,
    coercing string values into ``CapabilitySource`` enums.

    Unknown or malformed values are dropped (the caller falls back to
    ``_infer_legacy_source``).
    """
    raw = entry.get("capability_sources")
    if not isinstance(raw, dict):
        return {}

    result: Dict[str, CapabilitySource] = {}
    for field, value in raw.items():
        if not isinstance(field, str) or not isinstance(value, str):
            continue
        try:
            result[field] = CapabilitySource(value)
        except ValueError:
            continue
    return result


# ---------------------------------------------------------------------------
# Per-field resolvers
# ---------------------------------------------------------------------------


class _MergedValue:
    """
    Accumulator for a single field.

    Tracks the winning value, its source, and the priority used to
    select it. A new candidate only replaces the current winner when
    its source priority is strictly higher — ties preserve the
    previously-seen value (which came from an earlier, higher-priority
    layer like ``provider_catalog`` when the tie is with a lower layer).
    """

    __slots__ = ("value", "source", "priority")

    def __init__(self) -> None:
        self.value: Any = None
        self.source: CapabilitySource = CapabilitySource.UNKNOWN
        self.priority: int = -1

    def offer(self, value: Any, source: CapabilitySource) -> None:
        if value is None:
            return
        prio = _priority(source)
        if prio > self.priority:
            self.value = value
            self.source = source
            self.priority = prio


def _resolve_reasoning(
    entry: Dict[str, Any],
    catalog_entry: Optional[Dict[str, Any]],
    probe_result: Optional[Dict[str, Any]],
    sources: Dict[str, CapabilitySource],
) -> ReasoningCapability:
    """
    Merge reasoning state from entry + catalog + probe.

    Reads (in priority order):
        * ``capability_details.reasoning.state`` + ``.source`` (canonical)
        * ``capabilities.reasoning`` (boolean, legacy)
        * ``catalog_entry.capabilities.reasoning`` (from discovery)
        * ``probe_result.reasoning`` (from a paid probe)
    """
    state = _MergedValue()
    mode = _MergedValue()
    effort_levels = _MergedValue()
    default_effort = _MergedValue()
    min_budget = _MergedValue()
    max_budget = _MergedValue()

    # Layer 1: probe result (lowest priority — comes first so higher
    # layers overwrite it).
    if probe_result:
        probe_val = probe_result.get("reasoning")
        if isinstance(probe_val, bool):
            state.offer(
                CapabilityState.SUPPORTED if probe_val else CapabilityState.UNSUPPORTED,
                CapabilitySource.CAPABILITY_PROBE,
            )

    # Layer 2: provider catalog (from discovery endpoint).
    if catalog_entry:
        cat_caps = catalog_entry.get("capabilities")
        if isinstance(cat_caps, dict):
            cat_val = cat_caps.get("reasoning")
            if isinstance(cat_val, bool):
                state.offer(
                    CapabilityState.SUPPORTED if cat_val else CapabilityState.UNSUPPORTED,
                    CapabilitySource.PROVIDER_CATALOG,
                )

    # Layer 3: entry capability_details (may be a manual override OR
    # explicit metadata).
    detail = _as_dict(entry.get("capability_details"))
    if detail:
        reasoning_detail = _as_dict(detail.get("reasoning"))
        if reasoning_detail:
            explicit_state = reasoning_detail.get("state")
            if isinstance(explicit_state, str):
                try:
                    state.offer(
                        CapabilityState(explicit_state),
                        sources.get("reasoning", CapabilitySource.MODEL_METADATA),
                    )
                except ValueError:
                    pass

            explicit_mode = reasoning_detail.get("mode")
            if isinstance(explicit_mode, str):
                try:
                    mode.offer(
                        ReasoningMode(explicit_mode),
                        sources.get("reasoning", CapabilitySource.MODEL_METADATA),
                    )
                except ValueError:
                    pass

            levels = _as_str_list(reasoning_detail.get("effort_levels"))
            if levels is not None:
                effort_levels.offer(
                    levels,
                    sources.get("reasoning", CapabilitySource.MODEL_METADATA),
                )

            default_eff = reasoning_detail.get("default_effort")
            if isinstance(default_eff, str):
                default_effort.offer(
                    default_eff,
                    sources.get("reasoning", CapabilitySource.MODEL_METADATA),
                )

            min_b = _as_int(reasoning_detail.get("min_budget_tokens"))
            if min_b is not None:
                min_budget.offer(
                    min_b,
                    sources.get("reasoning", CapabilitySource.MODEL_METADATA),
                )

            max_b = _as_int(reasoning_detail.get("max_budget_tokens"))
            if max_b is not None:
                max_budget.offer(
                    max_b,
                    sources.get("reasoning", CapabilitySource.MODEL_METADATA),
                )

    # Layer 4: legacy flat boolean in entry.capabilities.
    entry_caps = _as_dict(entry.get("capabilities"))
    if entry_caps:
        legacy_val = _as_bool(entry_caps.get("reasoning"))
        if legacy_val is not None:
            explicit_source = sources.get("reasoning")
            if explicit_source is None:
                explicit_source = _infer_legacy_source(
                    "reasoning", legacy_val, entry
                )
            state.offer(
                CapabilityState.SUPPORTED if legacy_val else CapabilityState.UNSUPPORTED,
                explicit_source,
            )

    # Build the final ReasoningCapability.
    final_state = state.value or CapabilityState.UNKNOWN
    final_source = state.source if state.value is not None else CapabilitySource.UNKNOWN
    final_mode = mode.value or (
        ReasoningMode.ADAPTIVE if final_state == CapabilityState.SUPPORTED else ReasoningMode.NONE
    )

    return ReasoningCapability(
        state=final_state,
        source=final_source,
        mode=final_mode,
        effort_levels=effort_levels.value or [],
        default_effort=default_effort.value,
        min_budget_tokens=min_budget.value,
        max_budget_tokens=max_budget.value,
    )


def _resolve_tool_calling(
    entry: Dict[str, Any],
    catalog_entry: Optional[Dict[str, Any]],
    probe_result: Optional[Dict[str, Any]],
    sources: Dict[str, CapabilitySource],
) -> ToolCallingCapability:
    """
    Merge tool-calling state from entry + catalog + probe.

    Reads (in priority order):
        * ``capability_details.tool_calling.state`` + ``.mode``
        * ``capabilities.trained_for_tool_use`` (boolean, legacy)
        * ``catalog_entry.capabilities.trained_for_tool_use``
        * ``probe_result.tool_calling``
    """
    state = _MergedValue()
    mode = _MergedValue()
    parallel = _MergedValue()
    forced_choice = _MergedValue()

    # Layer 1: probe.
    if probe_result:
        probe_val = probe_result.get("tool_calling")
        if isinstance(probe_val, bool):
            state.offer(
                CapabilityState.SUPPORTED if probe_val else CapabilityState.UNSUPPORTED,
                CapabilitySource.CAPABILITY_PROBE,
            )

    # Layer 2: catalog.
    if catalog_entry:
        cat_caps = _as_dict(catalog_entry.get("capabilities"))
        if cat_caps:
            cat_val = _as_bool(cat_caps.get("trained_for_tool_use"))
            if cat_val is not None:
                state.offer(
                    CapabilityState.SUPPORTED if cat_val else CapabilityState.UNSUPPORTED,
                    CapabilitySource.PROVIDER_CATALOG,
                )

    # Layer 3: entry capability_details.
    detail = _as_dict(entry.get("capability_details"))
    if detail:
        tc_detail = _as_dict(detail.get("tool_calling"))
        if tc_detail:
            field_source = sources.get("tool_calling", CapabilitySource.MODEL_METADATA)

            explicit_state = tc_detail.get("state")
            if isinstance(explicit_state, str):
                try:
                    state.offer(CapabilityState(explicit_state), field_source)
                except ValueError:
                    pass

            explicit_mode = tc_detail.get("mode")
            if isinstance(explicit_mode, str):
                try:
                    mode.offer(ToolCallingMode(explicit_mode), field_source)
                except ValueError:
                    pass

            p_val = _as_bool(tc_detail.get("parallel"))
            if p_val is not None:
                parallel.offer(p_val, field_source)

            f_val = _as_bool(tc_detail.get("forced_choice"))
            if f_val is not None:
                forced_choice.offer(f_val, field_source)

    # Layer 4: legacy flat boolean.
    entry_caps = _as_dict(entry.get("capabilities"))
    if entry_caps:
        legacy_val = _as_bool(entry_caps.get("trained_for_tool_use"))
        if legacy_val is not None:
            explicit_source = sources.get("trained_for_tool_use")
            if explicit_source is None:
                explicit_source = _infer_legacy_source(
                    "trained_for_tool_use", legacy_val, entry
                )
            state.offer(
                CapabilityState.SUPPORTED if legacy_val else CapabilityState.UNSUPPORTED,
                explicit_source,
            )

    final_state = state.value or CapabilityState.UNKNOWN
    final_source = state.source if state.value is not None else CapabilitySource.UNKNOWN
    final_mode = mode.value or (
        ToolCallingMode.NATIVE if final_state == CapabilityState.SUPPORTED else ToolCallingMode.UNSUPPORTED
    )

    return ToolCallingCapability(
        state=final_state,
        source=final_source,
        mode=final_mode,
        parallel=bool(parallel.value) if parallel.value is not None else False,
        forced_choice=bool(forced_choice.value) if forced_choice.value is not None else False,
    )


def _resolve_vision(
    entry: Dict[str, Any],
    catalog_entry: Optional[Dict[str, Any]],
    probe_result: Optional[Dict[str, Any]],
    sources: Dict[str, CapabilitySource],
    modalities: ModalitiesCapability,
) -> Tuple[bool, CapabilityState, CapabilitySource]:
    """
    Merge vision support from entry + catalog + probe.

    Returns a tuple ``(effective_bool, state, source)``. ``effective_bool``
    is what the flat ``ModelCapabilities.vision`` field receives.
    """
    state = _MergedValue()

    # Layer 1: probe.
    if probe_result:
        probe_val = probe_result.get("vision")
        if isinstance(probe_val, bool):
            state.offer(
                CapabilityState.SUPPORTED if probe_val else CapabilityState.UNSUPPORTED,
                CapabilitySource.CAPABILITY_PROBE,
            )

    # Layer 2: catalog.
    if catalog_entry:
        cat_caps = _as_dict(catalog_entry.get("capabilities"))
        if cat_caps:
            cat_val = _as_bool(cat_caps.get("vision"))
            if cat_val is not None:
                state.offer(
                    CapabilityState.SUPPORTED if cat_val else CapabilityState.UNSUPPORTED,
                    CapabilitySource.PROVIDER_CATALOG,
                )

    # Layer 3: entry capability_details.modalities (authoritative).
    detail = _as_dict(entry.get("capability_details"))
    if detail:
        mod_detail = _as_dict(detail.get("modalities"))
        if mod_detail:
            input_modalities = _as_str_list(mod_detail.get("input"))
            if input_modalities is not None:
                field_source = sources.get("vision", CapabilitySource.MODEL_METADATA)
                state.offer(
                    (
                        CapabilityState.SUPPORTED
                        if "image" in input_modalities
                        else CapabilityState.UNSUPPORTED
                    ),
                    field_source,
                )

    # Layer 4: legacy flat boolean.
    entry_caps = _as_dict(entry.get("capabilities"))
    if entry_caps:
        legacy_val = _as_bool(entry_caps.get("vision"))
        if legacy_val is not None:
            explicit_source = sources.get("vision")
            if explicit_source is None:
                explicit_source = _infer_legacy_source("vision", legacy_val, entry)
            state.offer(
                CapabilityState.SUPPORTED if legacy_val else CapabilityState.UNSUPPORTED,
                explicit_source,
            )

    final_state = state.value or CapabilityState.UNKNOWN
    final_source = state.source if state.value is not None else CapabilitySource.UNKNOWN
    effective_bool = final_state in (
        CapabilityState.SUPPORTED,
        CapabilityState.UNVERIFIED,
    )

    # Reflect into the modalities object.
    if effective_bool and "image" not in modalities.input:
        modalities.input.append("image")

    # Record the source so downstream code (capabilities.py, lookup.py)
    # can distinguish "explicitly false" from "unknown".
    if final_source != CapabilitySource.UNKNOWN:
        modalities.source = final_source

    return effective_bool, final_state, final_source


def _resolve_streaming(
    entry: Dict[str, Any],
    catalog_entry: Optional[Dict[str, Any]],
    sources: Dict[str, CapabilitySource],
) -> StreamingCapability:
    """
    Merge per-channel streaming flags.

    Streaming is a first-class capability but rarely has explicit
    metadata — most providers stream text natively and silently reject
    or ignore per-channel options. The resolver records what is
    EXPLICITLY stated; the adapter's profile gates the actual attempt.
    """
    detail = _as_dict(entry.get("capability_details"))
    streaming_detail = _as_dict(detail.get("streaming")) if detail else None

    field_source = sources.get("streaming", CapabilitySource.MODEL_METADATA)

    text = bool(streaming_detail.get("text")) if streaming_detail else False
    reasoning = bool(streaming_detail.get("reasoning")) if streaming_detail else False
    tool_calls = bool(streaming_detail.get("tool_calls")) if streaming_detail else False
    usage = bool(streaming_detail.get("usage")) if streaming_detail else False

    # Derive a conservative state: SUPPORTED if any channel is on,
    # UNKNOWN if nothing was declared.
    any_declared = (
        streaming_detail is not None
        and any(k in streaming_detail for k in ("text", "reasoning", "tool_calls", "usage"))
    )
    if any_declared:
        state = CapabilityState.SUPPORTED if any([text, reasoning, tool_calls, usage]) else CapabilityState.UNSUPPORTED
        source = field_source
    else:
        state = CapabilityState.UNKNOWN
        source = CapabilitySource.UNKNOWN

    return StreamingCapability(
        state=state,
        source=source,
        text=text,
        reasoning=reasoning,
        tool_calls=tool_calls,
        usage=usage,
    )


def _resolve_structured_output(
    entry: Dict[str, Any],
    catalog_entry: Optional[Dict[str, Any]],
    sources: Dict[str, CapabilitySource],
) -> StructuredOutputCapability:
    """Merge structured output support."""
    detail = _as_dict(entry.get("capability_details"))
    so_detail = _as_dict(detail.get("structured_output")) if detail else None

    if so_detail:
        field_source = sources.get("structured_output", CapabilitySource.MODEL_METADATA)
        mode_str = so_detail.get("mode")
        if isinstance(mode_str, str):
            try:
                return StructuredOutputCapability(
                    state=CapabilityState.SUPPORTED,
                    source=field_source,
                    mode=StructuredOutputMode(mode_str),
                )
            except ValueError:
                pass

        # Legacy boolean pair (json_object / json_schema).
        if so_detail.get("json_schema"):
            return StructuredOutputCapability(
                state=CapabilityState.SUPPORTED,
                source=field_source,
                mode=StructuredOutputMode.JSON_SCHEMA,
            )
        if so_detail.get("json_object"):
            return StructuredOutputCapability(
                state=CapabilityState.SUPPORTED,
                source=field_source,
                mode=StructuredOutputMode.JSON_OBJECT,
            )

    return StructuredOutputCapability(
        state=CapabilityState.UNKNOWN,
        source=CapabilitySource.UNKNOWN,
        mode=StructuredOutputMode.UNSUPPORTED,
    )


def _resolve_modalities(
    entry: Dict[str, Any],
) -> ModalitiesCapability:
    """Merge multimodal I/O support."""
    detail = _as_dict(entry.get("capability_details"))
    mod_detail = _as_dict(detail.get("modalities")) if detail else None
    if mod_detail:
        input_modalities = _as_str_list(mod_detail.get("input")) or ["text"]
        output_modalities = _as_str_list(mod_detail.get("output")) or ["text"]
        return ModalitiesCapability(
            input=input_modalities, output=output_modalities
        )
    return ModalitiesCapability()


# ---------------------------------------------------------------------------
# Entry lookup
# ---------------------------------------------------------------------------


def _lookup_entry(
    vault: Dict[str, Any],
    candidate_keys: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Return the first matching entry from the vault using the candidate
    key list, along with the key that matched.

    Returns ``(None, None)`` when no key matches. Non-dict entries are
    skipped (defensive against hand-edited metadata files).
    """
    for key in candidate_keys:
        entry = vault.get(key)
        if isinstance(entry, dict):
            return entry, key
    return None, None


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


class CapabilityResolver:
    """
    Stateless resolver. Safe to instantiate per call site or share as a
    singleton; it holds no mutable state and no cached data.
    """

    def __init__(self, storage_root: Optional[Path] = None) -> None:
        """
        Parameters
        ----------
        storage_root
            Optional override for the storage root used when ``metadata``
            is not supplied to ``resolve()``. When omitted, the canonical
            ``peldrun.storage_paths.get_storage_root()`` is used at call
            time.
        """
        self._storage_root = storage_root

    # ------------------------------------------------------------------

    def resolve(
        self,
        provider_id: str,
        model_id: str,
        *,
        connection_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        provider_catalog_entry: Optional[Dict[str, Any]] = None,
        probe_result: Optional[Dict[str, Any]] = None,
    ) -> ModelCapabilities:
        """
        Resolve the effective ``ModelCapabilities`` for a (provider, model)
        pair.

        Parameters
        ----------
        provider_id
            Canonical provider identifier (``"openai"``, ``"lmstudio"``,
            ``"anthropic"``, ...).
        model_id
            The model ID as requested by the caller.
        connection_id
            Optional composite key component. When supplied, the
            canonical key ``provider::connection::model`` is tried
            before the intermediate and legacy forms.
        metadata
            The vault (same shape as ``models_metadata.json``). When
            ``None``, loads from disk on each call. Callers that already
            hold the vault should pass it for performance and
            testability.
        provider_catalog_entry
            Optional catalog entry returned by a discovery call for this
            specific model. Used to seed lower-priority defaults that a
            manual override in ``metadata`` can still supersede.
        probe_result
            Optional output of a capability probe (see plan Section 16.2).
            Lowest priority below catalog.

        Returns
        -------
        ModelCapabilities
            Ready for ``compute_resolved_capabilities()``.
        """
        vault = metadata if metadata is not None else self._load_vault()
        candidate_keys = _candidate_keys(provider_id, model_id, connection_id)
        entry, matched_key = _lookup_entry(vault, candidate_keys)

        if entry is None:
            # No metadata for this model. Fall back to catalog-only if
            # available; otherwise everything is ``unknown``.
            return self._resolve_unknown_with_catalog(
                provider_id=provider_id,
                model_id=model_id,
                provider_catalog_entry=provider_catalog_entry,
                probe_result=probe_result,
            )

        # Read explicit sources (new format) or fall back to legacy
        # inference (old format).
        explicit_sources = _read_explicit_sources(entry)

        # Merge modalities first — vision resolution may append to its
        # ``input`` list, so it must run before we finalize.
        modalities = _resolve_modalities(entry)

        # Reasoning.
        reasoning = _resolve_reasoning(
            entry=entry,
            catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
            sources=explicit_sources,
        )

        # Tool calling.
        tool_calling = _resolve_tool_calling(
            entry=entry,
            catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
            sources=explicit_sources,
        )

        # Vision + modalities.
        vision_bool, _vision_state, _vision_source = _resolve_vision(
            entry=entry,
            catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
            sources=explicit_sources,
            modalities=modalities,
        )

        # Streaming.
        streaming = _resolve_streaming(
            entry=entry,
            catalog_entry=provider_catalog_entry,
            sources=explicit_sources,
        )

        # Structured output.
        structured_output = _resolve_structured_output(
            entry=entry,
            catalog_entry=provider_catalog_entry,
            sources=explicit_sources,
        )

        # Legacy flat booleans expected by the existing frontend and by
        # ``_check_model_metadata_reasoning`` in ``dispatcher.py``.
        flat_reasoning: Optional[bool]
        if reasoning.state == CapabilityState.SUPPORTED:
            flat_reasoning = True
        elif reasoning.state == CapabilityState.UNSUPPORTED:
            flat_reasoning = False
        else:
            flat_reasoning = None

        logger.debug(
            "Resolved capabilities for %s (key=%s): "
            "vision=%s tools=%s reasoning=%s",
            f"{provider_id}::{model_id}",
            matched_key or "<no-entry>",
            vision_bool,
            tool_calling.state.value,
            reasoning.state.value,
        )

        return ModelCapabilities(
            vision=vision_bool,
            trained_for_tool_use=(tool_calling.state == CapabilityState.SUPPORTED),
            reasoning=flat_reasoning,
            reasoning_details=reasoning,
            tool_calling=tool_calling,
            streaming=streaming,
            structured_output=structured_output,
            modalities=modalities,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_vault(self) -> Dict[str, Any]:
        """
        Load the current ``models_metadata.json`` from disk.

        Returns an empty dict when the file is missing or malformed —
        the resolver never raises on storage errors, because a broken
        metadata file must not break an otherwise-working run.
        """
        try:
            if self._storage_root is not None:
                path = self._storage_root / "models_metadata.json"
            else:
                from peldrun.storage_paths import get_storage_root

                path = get_storage_root() / "models_metadata.json"

            if not path.is_file():
                return {}

            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning(
                    "models_metadata.json root is not a dict (got %s); "
                    "treating as empty.",
                    type(parsed).__name__,
                )
                return {}
            return parsed
        except Exception as exc:
            logger.warning(
                "Could not load models_metadata.json: %s — treating as empty.",
                exc,
            )
            return {}

    def _resolve_unknown_with_catalog(
        self,
        provider_id: str,
        model_id: str,
        provider_catalog_entry: Optional[Dict[str, Any]],
        probe_result: Optional[Dict[str, Any]],
    ) -> ModelCapabilities:
        """
        Resolve capabilities when NO vault entry exists for this model.

        Catalog and probe can still inform the result — they are just
        the only available sources. Everything they do not cover
        remains ``unknown``.
        """
        # Synthesize an empty entry and let the per-field resolvers do
        # the merge with whatever catalog / probe data we have.
        empty_entry: Dict[str, Any] = {}
        modalities = ModalitiesCapability()

        reasoning = _resolve_reasoning(
            entry=empty_entry,
            catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
            sources={},
        )
        tool_calling = _resolve_tool_calling(
            entry=empty_entry,
            catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
            sources={},
        )
        vision_bool, _, _ = _resolve_vision(
            entry=empty_entry,
            catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
            sources={},
            modalities=modalities,
        )
        streaming = _resolve_streaming(
            entry=empty_entry,
            catalog_entry=provider_catalog_entry,
            sources={},
        )
        structured_output = _resolve_structured_output(
            entry=empty_entry,
            catalog_entry=provider_catalog_entry,
            sources={},
        )

        flat_reasoning: Optional[bool]
        if reasoning.state == CapabilityState.SUPPORTED:
            flat_reasoning = True
        elif reasoning.state == CapabilityState.UNSUPPORTED:
            flat_reasoning = False
        else:
            flat_reasoning = None

        return ModelCapabilities(
            vision=vision_bool,
            trained_for_tool_use=(tool_calling.state == CapabilityState.SUPPORTED),
            reasoning=flat_reasoning,
            reasoning_details=reasoning,
            tool_calling=tool_calling,
            streaming=streaming,
            structured_output=structured_output,
            modalities=modalities,
        )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


#: Default resolver — stateless, safe to reuse across calls.
_default_resolver = CapabilityResolver()


def resolve_model_capabilities(
    provider_id: str,
    model_id: str,
    *,
    connection_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    provider_catalog_entry: Optional[Dict[str, Any]] = None,
    probe_result: Optional[Dict[str, Any]] = None,
    storage_root: Optional[Path] = None,
) -> ModelCapabilities:
    """
    Convenience wrapper around ``CapabilityResolver.resolve()``.

    Uses a shared default resolver unless ``storage_root`` is supplied,
    in which case a per-call resolver is created. Both paths are
    functionally identical; the shared resolver is preferred because it
    avoids re-importing ``storage_paths`` per call.
    """
    if storage_root is None:
        return _default_resolver.resolve(
            provider_id=provider_id,
            model_id=model_id,
            connection_id=connection_id,
            metadata=metadata,
            provider_catalog_entry=provider_catalog_entry,
            probe_result=probe_result,
        )
    return CapabilityResolver(storage_root=storage_root).resolve(
        provider_id=provider_id,
        model_id=model_id,
        connection_id=connection_id,
        metadata=metadata,
        provider_catalog_entry=provider_catalog_entry,
        probe_result=probe_result,
    )


__all__ = [
    "CapabilityResolver",
    "resolve_model_capabilities",
]
