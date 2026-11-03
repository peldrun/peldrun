# backend/peldrun/llm/capabilities.py
"""
PELDRUN Merged Capability View.

Computes the *effective* capability intersection between a model and an
adapter. This is the object the RequestCompiler (M4) consumes to decide
which parameters are safe to forward, and the object the UI reads to
display "enabled / downgraded / disabled" states truthfully.

Design invariants
-----------------

1. **Intersection, not union.** The runtime only attempts a capability
   when BOTH the model AND the adapter support it. This is the core
   architectural rule that prevents the exact failure mode described in
   the plan (Section 4): "user enables reasoning manually but the API
   cannot accept the parameter". We honor the user intent by *trying*,
   but only when the transport can carry it.

2. **No inference.** This module never guesses capabilities from model
   names. It only combines inputs that were resolved elsewhere. Any
   heuristic-based suggestion MUST live in the resolver and be marked
   with a low-priority source — never in the decision path.

3. **Every decision carries a reason.** When a capability is downgraded
   or disabled, a ``ConflictReason`` explains why. This is what powers
   the Request Inspector and the "why doesn't my model think?" answer
   in the UI.

4. **EffectiveState is four-valued, not boolean.** ``PASSTHROUGH`` and
   ``DOWNGRADED`` are distinct from ``DISABLED`` because they mean
   different things to the compiler:
       - ``DISABLED``     → do not send; model does not support.
       - ``PASSTHROUGH``  → do not send; we do not know; provider decides.
       - ``DOWNGRADED``   → user asked; adapter cannot carry; do not send
                            (but the UI must show a warning).
       - ``ENABLED``      → send the parameter as configured.

5. **Adapter-only capabilities are still reported.** Vision is
   sometimes adapter-only (an adapter that translates image parts to a
   text description before sending). This module reports the effective
   state on both sides so the UI can distinguish "model rejects images"
   from "adapter rewrites images".

See also
--------

    * ``backend/peldrun/llm/contracts.py``           — vocabulary
    * ``backend/peldrun/llm/request.py``             — LLMRequest
    * ``backend/peldrun/llm/capability_resolver.py`` — M3 (feeds this module)
    * ``backend/peldrun/llm/request_compiler.py``    — M4 (consumes this)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    CapabilitySource,
    CapabilityState,
    ModelCapabilities,
    ReasoningMode,
    StructuredOutputMode,
    ToolCallingMode,
)


# ---------------------------------------------------------------------------
# Decision vocabulary
# ---------------------------------------------------------------------------


class EffectiveState(str, Enum):
    """
    The decision the runtime makes for a single capability.

    ``ENABLED``      — send the parameter exactly as configured.
    ``DISABLED``     — do not send; the model does not support it.
    ``PASSTHROUGH``  — do not send; capability is unknown; the provider's
                       default behavior applies.
    ``DOWNGRADED``   — the user asked for it, but the adapter cannot
                       carry the parameter. Do not send; surface a
                       warning in the UI.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    PASSTHROUGH = "passthrough"
    DOWNGRADED = "downgraded"


class ConflictReason(str, Enum):
    """Why a capability was not granted its requested state."""

    #: The model declares the capability but the adapter cannot transmit it.
    ADAPTER_UNSUPPORTED = "adapter_unsupported"

    #: The adapter supports it but the model explicitly rejects it.
    MODEL_UNSUPPORTED = "model_unsupported"

    #: Neither side declares support.
    BOTH_UNSUPPORTED = "both_unsupported"

    #: The model state is unknown; we do not attempt by default.
    MODEL_UNKNOWN = "model_unknown"

    #: Adapter supports streaming but only in buffered mode (no native).
    STREAMING_BUFFERED_ONLY = "streaming_buffered_only"

    #: Adapter does not expose a streaming channel for this modality.
    STREAMING_CHANNEL_UNAVAILABLE = "streaming_channel_unavailable"

    #: The user explicitly disabled the capability.
    USER_DISABLED = "user_disabled"

    #: The connection is missing required fields (e.g. no API key for a
    #: provider that requires one). Not a capability issue per se, but
    #: surfaced here so the UI can show the real blocker.
    CONNECTION_INCOMPLETE = "connection_incomplete"


class StreamingMode(str, Enum):
    """Aggregate streaming mode for a (model, adapter) pair."""

    #: No streaming of any kind.
    UNSUPPORTED = "unsupported"

    #: Adapter buffers the full response, runtime simulates chunks. This
    #: is honest — the UI shows "buffered" instead of pretending to be
    #: live.
    BUFFERED = "buffered"

    #: Adapter streams natively. Individual channels (text, reasoning,
    #: tool_calls, usage) are still gated by their own flags.
    NATIVE = "native"


# ---------------------------------------------------------------------------
# Per-capability decision
# ---------------------------------------------------------------------------


class CapabilityDecision(BaseModel):
    """
    The decision for a single capability.

    This is the unit of information the RequestCompiler reads. Every
    field is populated deterministically from the model capabilities
    and adapter profile — no ambiguity, no defaults that depend on
    caller behavior.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    #: Capability key, e.g. ``"reasoning"``, ``"tool_calling"``,
    #: ``"vision"``, ``"structured_output.json_schema"``,
    #: ``"streaming.text"``.
    key: str

    #: Model-side state (after user overrides were merged).
    model_state: CapabilityState = CapabilityState.UNKNOWN

    #: Where the model state came from. Kept on the decision so the UI
    #: can display "manual override" vs "provider catalog" per field.
    model_source: CapabilitySource = CapabilitySource.UNKNOWN

    #: Whether the adapter profile declares transport support for this
    #: capability. This is a strict boolean from the adapter side.
    adapter_supports: bool = False

    #: The final effective state.
    effective: EffectiveState = EffectiveState.DISABLED

    #: Populated whenever ``effective`` is not ``ENABLED`` and the
    #: reason is not obvious (i.e. not because the model said no).
    conflict: Optional[ConflictReason] = None

    #: Human-readable explanation for logging and UI tooltips. Never
    #: used for control flow.
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Capability conflict (aggregate)
# ---------------------------------------------------------------------------


class CapabilityConflict(BaseModel):
    """
    A record of a disagreement between model capability and adapter
    support, surfaced so the UI can warn the user.

    Example
    -------
    The user marks a custom model as "supports reasoning", but the
    OpenAI-compatible adapter for that endpoint does not expose a
    reasoning channel. The user needs to know that their setting is
    being honored as intent but not as behavior.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    key: str
    model_state: CapabilityState
    adapter_supports: bool
    reason: ConflictReason
    message: str


# ---------------------------------------------------------------------------
# Resolved capabilities (aggregate)
# ---------------------------------------------------------------------------


class ResolvedCapabilities(BaseModel):
    """
    Complete effective capability view for a single (provider, model)
    pair against a specific adapter profile.

    Consumed by
    -----------
    * ``request_compiler.RequestCompiler`` — decides which parameters
      to forward to the adapter.
    * ``omweb.agent_bridge_parts.dispatcher`` — decides whether to
      attempt streaming, pass tools, etc.
    * Frontend UI (via ``to_metadata_dict``) — displays truthful
      capability badges.

    Construction
    ------------
    Never construct this by hand. Use ``compute_resolved_capabilities``
    which applies the intersection rules consistently.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    provider_id: str
    model_id: str

    decisions: Dict[str, CapabilityDecision] = Field(default_factory=dict)
    conflicts: List[CapabilityConflict] = Field(default_factory=list)

    #: Aggregate streaming mode. Derived from the adapter's native /
    #: buffered flags plus the model's streaming flags — but as a
    #: summary, not a per-channel gate. Per-channel gates live in
    #: ``decisions["streaming.text"]`` etc.
    streaming_mode: StreamingMode = StreamingMode.UNSUPPORTED

    #: The reasoning mode that will actually be used at runtime (may
    #: differ from the model's declared mode if the adapter forced a
    #: downgrade).
    effective_reasoning_mode: ReasoningMode = ReasoningMode.NONE

    #: The effective tool-calling mode (native / emulated / unsupported).
    #: Downgrades from ``native`` to ``emulated`` only if the adapter
    #: explicitly declares emulation support.
    effective_tool_calling_mode: ToolCallingMode = ToolCallingMode.UNSUPPORTED

    #: The effective structured-output mode.
    effective_structured_output_mode: StructuredOutputMode = (
        StructuredOutputMode.UNSUPPORTED
    )

    # -------------------------------------------------------------------
    # Convenience queries used by the runtime
    # -------------------------------------------------------------------

    def is_enabled(self, key: str) -> bool:
        """Return True iff the decision for ``key`` is ``ENABLED``."""
        decision = self.decisions.get(key)
        return decision is not None and decision.effective == EffectiveState.ENABLED

    def should_attempt_reasoning(self) -> bool:
        """True iff the compiler should emit a reasoning parameter."""
        return self.is_enabled("reasoning")

    def should_attempt_tools(self) -> bool:
        """True iff the compiler should emit tool definitions."""
        return self.is_enabled("tool_calling")

    def should_attempt_parallel_tools(self) -> bool:
        """True iff parallel tool calls may be enabled."""
        return self.is_enabled("tool_calling.parallel")

    def should_attempt_forced_tool_choice(self) -> bool:
        """True iff ``tool_choice: required/specific`` is safe to send."""
        return self.is_enabled("tool_calling.forced_choice")

    def should_attempt_vision(self) -> bool:
        """True iff image parts may be included in the messages."""
        return self.is_enabled("vision")

    def should_attempt_structured_output(self) -> bool:
        """True iff the caller may set a non-text ``response_format``."""
        return self.effective_structured_output_mode != StructuredOutputMode.UNSUPPORTED

    def supports_streaming_channel(self, channel: str) -> bool:
        """
        Return True iff the specified streaming channel is natively
        available.

        ``channel`` is one of ``"text"``, ``"reasoning"``,
        ``"tool_calls"``, ``"usage"``.
        """
        decision = self.decisions.get(f"streaming.{channel}")
        return decision is not None and decision.effective == EffectiveState.ENABLED

    def get_conflict(self, key: str) -> Optional[CapabilityConflict]:
        """Return the conflict record for ``key``, or None if there is none."""
        for c in self.conflicts:
            if c.key == key:
                return c
        return None

    # -------------------------------------------------------------------
    # Serialization helpers for the metadata vault and UI
    # -------------------------------------------------------------------

    def to_metadata_dict(self) -> Dict[str, Any]:
        """
        Produce the shape stored in ``models_metadata.json`` under the
        ``capabilities`` / ``capability_sources`` / ``capability_details``
        keys.

        This is a *derived* view for persistence and display. It does
        NOT replace the resolved view for runtime decisions.
        """
        caps: Dict[str, Any] = {
            "vision": self.is_enabled("vision"),
            "trained_for_tool_use": self.is_enabled("tool_calling"),
            "reasoning": self.is_enabled("reasoning") or (
                self.decisions.get("reasoning", CapabilityDecision(key="reasoning"))
                .model_state == CapabilityState.SUPPORTED
            ),
        }

        sources: Dict[str, str] = {}
        for key in ("vision", "tool_calling", "reasoning"):
            decision = self.decisions.get(key)
            if decision is not None:
                sources[key] = decision.model_source.value

        details: Dict[str, Any] = {}
        reasoning_decision = self.decisions.get("reasoning")
        if reasoning_decision is not None:
            details["reasoning"] = {
                "mode": self.effective_reasoning_mode.value,
                "effort_levels": _EFFORT_LEVELS_FOR_MODE.get(
                    self.effective_reasoning_mode, []
                ),
            }

        tool_decision = self.decisions.get("tool_calling")
        if tool_decision is not None:
            details["tool_calling"] = {
                "mode": self.effective_tool_calling_mode.value,
                "parallel": self.should_attempt_parallel_tools(),
                "forced_choice": self.should_attempt_forced_tool_choice(),
            }

        details["streaming"] = {
            "text": self.supports_streaming_channel("text"),
            "reasoning": self.supports_streaming_channel("reasoning"),
            "tool_calls": self.supports_streaming_channel("tool_calls"),
            "usage": self.supports_streaming_channel("usage"),
            "mode": self.streaming_mode.value,
        }

        details["structured_output"] = {
            "mode": self.effective_structured_output_mode.value,
        }

        return {
            "capabilities": caps,
            "capability_sources": sources,
            "capability_details": details,
        }


#: Populated lazily from ``ReasoningMode`` values for UI convenience.
_EFFORT_LEVELS_FOR_MODE: Dict[ReasoningMode, List[str]] = {
    ReasoningMode.EFFORT: ["low", "medium", "high"],
    ReasoningMode.BUDGET: [],
    ReasoningMode.TOGGLE: [],
    ReasoningMode.ADAPTIVE: [],
    ReasoningMode.AUTOMATIC: [],
    ReasoningMode.NONE: [],
}


# ---------------------------------------------------------------------------
# Internal resolvers (per-capability state normalization)
# ---------------------------------------------------------------------------


def _resolve_reasoning_state(
    caps: ModelCapabilities,
) -> Tuple[CapabilityState, CapabilitySource, ReasoningMode]:
    """
    Determine the model-side reasoning state and mode.

    Prefers the detailed ``reasoning_details`` sub-object when its
    ``state`` is not ``UNKNOWN``; falls back to the flat
    ``reasoning`` boolean for backward compatibility with the existing
    frontend metadata shape.
    """
    detail = caps.reasoning_details
    if detail.state != CapabilityState.UNKNOWN:
        return detail.state, detail.source, detail.mode

    if caps.reasoning is True:
        return (
            CapabilityState.SUPPORTED,
            CapabilitySource.MODEL_METADATA,
            detail.mode if detail.mode != ReasoningMode.NONE else ReasoningMode.ADAPTIVE,
        )
    if caps.reasoning is False:
        return (
            CapabilityState.UNSUPPORTED,
            CapabilitySource.MODEL_METADATA,
            ReasoningMode.NONE,
        )
    return CapabilityState.UNKNOWN, CapabilitySource.UNKNOWN, ReasoningMode.NONE


def _resolve_tool_state(
    caps: ModelCapabilities,
) -> Tuple[CapabilityState, CapabilitySource, ToolCallingMode]:
    """
    Determine the model-side tool-calling state and mode.

    Falls back to the flat ``trained_for_tool_use`` boolean when the
    detailed state is ``UNKNOWN``.
    """
    detail = caps.tool_calling
    if detail.state != CapabilityState.UNKNOWN:
        return detail.state, detail.source, detail.mode

    if caps.trained_for_tool_use:
        return (
            CapabilityState.SUPPORTED,
            CapabilitySource.MODEL_METADATA,
            ToolCallingMode.NATIVE,
        )
    return CapabilityState.UNKNOWN, CapabilitySource.UNKNOWN, ToolCallingMode.UNSUPPORTED


def _resolve_vision_state(
    caps: ModelCapabilities,
) -> Tuple[CapabilityState, CapabilitySource]:
    """
    Determine the model-side vision state.

    Honors ``modalities.source`` when the resolver / lookup layer set
    it explicitly, so the true origin (manual, HuggingFace, LiteLLM,
    provider catalog, ...) survives all the way to the adapter-facing
    decision. Falls back to the flat ``vision`` boolean when no source
    was recorded.
    """
    if caps.modalities.source != CapabilitySource.UNKNOWN:
        state = (
            CapabilityState.SUPPORTED
            if caps.vision
            else CapabilityState.UNSUPPORTED
        )
        return state, caps.modalities.source
    if "image" in (caps.modalities.input or []):
        return CapabilityState.SUPPORTED, CapabilitySource.MODEL_METADATA
    if caps.vision:
        return CapabilityState.SUPPORTED, CapabilitySource.MODEL_METADATA
    return CapabilityState.UNKNOWN, CapabilitySource.UNKNOWN


def _resolve_structured_output_state(
    caps: ModelCapabilities,
) -> Tuple[CapabilityState, CapabilitySource, StructuredOutputMode]:
    """Determine the model-side structured-output capability."""
    detail = caps.structured_output
    if detail.state != CapabilityState.UNKNOWN:
        return detail.state, detail.source, detail.mode
    return CapabilityState.UNKNOWN, CapabilitySource.UNKNOWN, StructuredOutputMode.UNSUPPORTED


# ---------------------------------------------------------------------------
# Main intersection function
# ---------------------------------------------------------------------------


def compute_resolved_capabilities(
    provider_id: str,
    model_id: str,
    model_caps: ModelCapabilities,
    adapter_profile: AdapterProfile,
) -> ResolvedCapabilities:
    """
    Compute the effective capability view.

    This is the only supported entry point. Callers must NOT construct
    ``ResolvedCapabilities`` by hand — the intersection rules are
    subtle and MUST be applied consistently across every dispatch path.

    Parameters
    ----------
    provider_id : str
        Provider identifier (e.g. ``"openai"``, ``"anthropic"``,
        ``"lmstudio"``).
    model_id : str
        Model identifier as requested by the caller.
    model_caps : ModelCapabilities
        The user's merged capability profile for this (provider, model)
        pair. Already reflects manual overrides — the resolver (M3) is
        responsible for that merge.
    adapter_profile : AdapterProfile
        The transport capability descriptor for the specific adapter
        instance serving this connection.

    Returns
    -------
    ResolvedCapabilities
        Ready for ``RequestCompiler.compile()``.
    """
    features: AdapterFeatures = adapter_profile.features

    decisions: Dict[str, CapabilityDecision] = {}
    conflicts: List[CapabilityConflict] = []

    # ------------------------------------------------------------------
    # Reasoning
    # ------------------------------------------------------------------
    reasoning_state, reasoning_source, reasoning_mode = _resolve_reasoning_state(
        model_caps
    )
    adapter_can_reason = bool(features.reasoning) and bool(
        adapter_profile.reasoning_strategies
    )

    if reasoning_state == CapabilityState.SUPPORTED or reasoning_state == CapabilityState.UNVERIFIED:
        if adapter_can_reason:
            effective_reasoning = EffectiveState.ENABLED
            effective_reasoning_mode = reasoning_mode
            conflict = None
            reason = None
        else:
            effective_reasoning = EffectiveState.DOWNGRADED
            effective_reasoning_mode = ReasoningMode.NONE
            conflict = ConflictReason.ADAPTER_UNSUPPORTED
            reason = (
                "The model supports reasoning, but this adapter cannot "
                "transmit reasoning parameters. The setting is preserved "
                "as intent but will not affect the API call."
            )
            conflicts.append(
                CapabilityConflict(
                    key="reasoning",
                    model_state=reasoning_state,
                    adapter_supports=False,
                    reason=ConflictReason.ADAPTER_UNSUPPORTED,
                    message=reason,
                )
            )
    elif reasoning_state == CapabilityState.UNSUPPORTED:
        effective_reasoning = EffectiveState.DISABLED
        effective_reasoning_mode = ReasoningMode.NONE
        conflict = ConflictReason.MODEL_UNSUPPORTED
        reason = "The model declares no reasoning support."
    else:  # UNKNOWN
        # Unknown model + adapter supports → PASSTHROUGH: do not send,
        # let the provider's default apply. This honors both "we don't
        # know" and "the user did not ask".
        effective_reasoning = EffectiveState.PASSTHROUGH
        effective_reasoning_mode = ReasoningMode.NONE
        conflict = None
        reason = "Reasoning capability is unknown; leaving to provider default."

    decisions["reasoning"] = CapabilityDecision(
        key="reasoning",
        model_state=reasoning_state,
        model_source=reasoning_source,
        adapter_supports=adapter_can_reason,
        effective=effective_reasoning,
        conflict=conflict,
        reason=reason,
    )

    # ------------------------------------------------------------------
    # Tool calling
    # ------------------------------------------------------------------
    tool_state, tool_source, tool_mode = _resolve_tool_state(model_caps)
    adapter_can_tools = bool(features.tool_calling)

    if tool_state == CapabilityState.SUPPORTED or tool_state == CapabilityState.UNVERIFIED:
        if adapter_can_tools:
            effective_tools = EffectiveState.ENABLED
            effective_tool_mode = tool_mode
            conflict = None
            reason = None
        else:
            # Adapter cannot emit tools. Downgrade to unsupported — no
            # emulation fallback unless the adapter explicitly declares
            # emulation support (see Section 15 example 2 in the plan).
            effective_tools = EffectiveState.DOWNGRADED
            effective_tool_mode = ToolCallingMode.UNSUPPORTED
            conflict = ConflictReason.ADAPTER_UNSUPPORTED
            reason = (
                "The model supports tool calling, but this adapter cannot "
                "emit tool call structures. Tools will not be sent."
            )
            conflicts.append(
                CapabilityConflict(
                    key="tool_calling",
                    model_state=tool_state,
                    adapter_supports=False,
                    reason=ConflictReason.ADAPTER_UNSUPPORTED,
                    message=reason,
                )
            )
    elif tool_state == CapabilityState.UNSUPPORTED:
        effective_tools = EffectiveState.DISABLED
        effective_tool_mode = ToolCallingMode.UNSUPPORTED
        conflict = ConflictReason.MODEL_UNSUPPORTED
        reason = "The model declares no tool-calling support."
    else:  # UNKNOWN
        if adapter_can_tools:
            # Adapter can send tools but we don't know if the model uses
            # them. PASSTHROUGH is the honest answer — the compiler will
            # not emit tool definitions unless the caller explicitly
            # requests them.
            effective_tools = EffectiveState.PASSTHROUGH
            effective_tool_mode = ToolCallingMode.UNSUPPORTED
        else:
            effective_tools = EffectiveState.DISABLED
            effective_tool_mode = ToolCallingMode.UNSUPPORTED
        conflict = ConflictReason.MODEL_UNKNOWN
        reason = "Tool-calling capability is unknown for this model."

    decisions["tool_calling"] = CapabilityDecision(
        key="tool_calling",
        model_state=tool_state,
        model_source=tool_source,
        adapter_supports=adapter_can_tools,
        effective=effective_tools,
        conflict=conflict,
        reason=reason,
    )

    # Sub-decisions: parallel + forced choice. These require BOTH the
    # parent tool-calling to be enabled AND the specific flags.
    model_parallel = bool(model_caps.tool_calling.parallel)
    model_forced = bool(model_caps.tool_calling.forced_choice)
    adapter_parallel = bool(features.parallel_tool_calls)
    adapter_forced = bool(features.forced_tool_choice)

    parallel_effective = (
        EffectiveState.ENABLED
        if effective_tools == EffectiveState.ENABLED
        and model_parallel
        and adapter_parallel
        else EffectiveState.DISABLED
    )
    decisions["tool_calling.parallel"] = CapabilityDecision(
        key="tool_calling.parallel",
        model_state=(
            CapabilityState.SUPPORTED
            if model_parallel
            else CapabilityState.UNKNOWN
        ),
        model_source=tool_source,
        adapter_supports=adapter_parallel,
        effective=parallel_effective,
    )

    forced_effective = (
        EffectiveState.ENABLED
        if effective_tools == EffectiveState.ENABLED
        and model_forced
        and adapter_forced
        else EffectiveState.DISABLED
    )
    decisions["tool_calling.forced_choice"] = CapabilityDecision(
        key="tool_calling.forced_choice",
        model_state=(
            CapabilityState.SUPPORTED if model_forced else CapabilityState.UNKNOWN
        ),
        model_source=tool_source,
        adapter_supports=adapter_forced,
        effective=forced_effective,
    )

    # ------------------------------------------------------------------
    # Vision
    # ------------------------------------------------------------------
    vision_state, vision_source = _resolve_vision_state(model_caps)
    adapter_can_vision = bool(features.vision)

    if vision_state == CapabilityState.SUPPORTED or vision_state == CapabilityState.UNVERIFIED:
        if adapter_can_vision:
            effective_vision = EffectiveState.ENABLED
            conflict = None
            reason = None
        else:
            effective_vision = EffectiveState.DOWNGRADED
            conflict = ConflictReason.ADAPTER_UNSUPPORTED
            reason = (
                "The model accepts images, but this adapter cannot send "
                "image parts to the provider."
            )
            conflicts.append(
                CapabilityConflict(
                    key="vision",
                    model_state=vision_state,
                    adapter_supports=False,
                    reason=ConflictReason.ADAPTER_UNSUPPORTED,
                    message=reason,
                )
            )
    elif vision_state == CapabilityState.UNSUPPORTED:
        effective_vision = EffectiveState.DISABLED
        conflict = ConflictReason.MODEL_UNSUPPORTED
        reason = "The model declares no image input support."
    else:  # UNKNOWN
        if adapter_can_vision:
            effective_vision = EffectiveState.PASSTHROUGH
            conflict = None
            reason = "Vision capability is unknown; will not send images unless explicitly enabled."
        else:
            effective_vision = EffectiveState.DISABLED
            conflict = ConflictReason.ADAPTER_UNSUPPORTED
            reason = "Neither the model nor the adapter declares vision support."

    decisions["vision"] = CapabilityDecision(
        key="vision",
        model_state=vision_state,
        model_source=vision_source,
        adapter_supports=adapter_can_vision,
        effective=effective_vision,
        conflict=conflict,
        reason=reason,
    )

    # ------------------------------------------------------------------
    # Structured output
    # ------------------------------------------------------------------
    so_state, so_source, so_mode = _resolve_structured_output_state(model_caps)
    adapter_can_so = bool(features.structured_output)

    if so_state == CapabilityState.SUPPORTED or so_state == CapabilityState.UNVERIFIED:
        if adapter_can_so:
            effective_so = EffectiveState.ENABLED
            effective_so_mode = so_mode
            conflict = None
            reason = None
        else:
            effective_so = EffectiveState.DOWNGRADED
            effective_so_mode = StructuredOutputMode.UNSUPPORTED
            conflict = ConflictReason.ADAPTER_UNSUPPORTED
            reason = (
                "The model supports structured output, but this adapter "
                "cannot forward a response_format parameter."
            )
            conflicts.append(
                CapabilityConflict(
                    key="structured_output",
                    model_state=so_state,
                    adapter_supports=False,
                    reason=ConflictReason.ADAPTER_UNSUPPORTED,
                    message=reason,
                )
            )
    elif so_state == CapabilityState.UNSUPPORTED:
        effective_so = EffectiveState.DISABLED
        effective_so_mode = StructuredOutputMode.UNSUPPORTED
        conflict = ConflictReason.MODEL_UNSUPPORTED
        reason = "The model declares no structured output support."
    else:  # UNKNOWN
        effective_so = EffectiveState.PASSTHROUGH
        effective_so_mode = StructuredOutputMode.UNSUPPORTED
        conflict = ConflictReason.MODEL_UNKNOWN
        reason = "Structured output capability is unknown for this model."

    decisions["structured_output"] = CapabilityDecision(
        key="structured_output",
        model_state=so_state,
        model_source=so_source,
        adapter_supports=adapter_can_so,
        effective=effective_so,
        conflict=conflict,
        reason=reason,
    )

    # ------------------------------------------------------------------
    # Streaming — aggregate + per-channel
    # ------------------------------------------------------------------
    if not features.native_streaming and not features.buffered_streaming:
        aggregate_streaming = StreamingMode.UNSUPPORTED
    elif features.buffered_streaming and not features.native_streaming:
        aggregate_streaming = StreamingMode.BUFFERED
    else:
        aggregate_streaming = StreamingMode.NATIVE

    # Per-channel gates. A channel is enabled only if:
    #   1. Adapter supports NATIVE streaming (not just buffered), AND
    #   2. Model declares the channel.
    # Buffered mode reports all channels as DISABLED because the caller
    # receives a full response, not per-channel deltas.
    model_streaming = model_caps.streaming
    for channel, model_flag in (
        ("text", model_streaming.text),
        ("reasoning", model_streaming.reasoning),
        ("tool_calls", model_streaming.tool_calls),
        ("usage", model_streaming.usage),
    ):
        if aggregate_streaming == StreamingMode.NATIVE and model_flag:
            channel_state = EffectiveState.ENABLED
            channel_conflict: Optional[ConflictReason] = None
            channel_reason: Optional[str] = None
        elif aggregate_streaming == StreamingMode.BUFFERED:
            channel_state = EffectiveState.DISABLED
            channel_conflict = ConflictReason.STREAMING_BUFFERED_ONLY
            channel_reason = "Streaming is buffered; per-channel deltas are not available."
        elif aggregate_streaming == StreamingMode.NATIVE and not model_flag:
            channel_state = EffectiveState.DISABLED
            channel_conflict = ConflictReason.STREAMING_CHANNEL_UNAVAILABLE
            channel_reason = f"Model does not declare native streaming for {channel}."
        else:
            channel_state = EffectiveState.DISABLED
            channel_conflict = ConflictReason.BOTH_UNSUPPORTED
            channel_reason = "Streaming is not supported by either side."

        decisions[f"streaming.{channel}"] = CapabilityDecision(
            key=f"streaming.{channel}",
            model_state=(
                CapabilityState.SUPPORTED
                if model_flag
                else CapabilityState.UNKNOWN
            ),
            model_source=model_streaming.source,
            adapter_supports=features.native_streaming,
            effective=channel_state,
            conflict=channel_conflict,
            reason=channel_reason,
        )

    return ResolvedCapabilities(
        provider_id=provider_id,
        model_id=model_id,
        decisions=decisions,
        conflicts=conflicts,
        streaming_mode=aggregate_streaming,
        effective_reasoning_mode=effective_reasoning_mode,
        effective_tool_calling_mode=effective_tool_mode,
        effective_structured_output_mode=effective_so_mode,
    )


__all__ = [
    "EffectiveState",
    "ConflictReason",
    "StreamingMode",
    "CapabilityDecision",
    "CapabilityConflict",
    "ResolvedCapabilities",
    "compute_resolved_capabilities",
]
