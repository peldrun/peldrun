# backend/peldrun/llm/contracts.py
"""
PELDRUN Universal Provider Adapter Contracts.

Defines the typed protocol surface shared by every LLM provider adapter:

    * Built-in native adapters (OpenAI, Anthropic, Google, Ollama, LM Studio).
    * OpenAI-compatible adapters for custom or unknown providers.
    * Externally installed Provider Packages (loaded via plugin_host).

This module is the single source of truth for the adapter vocabulary:

    * Capability states and sources.
    * Reasoning / tool-calling / streaming / structured-output modes.
    * ModelCapabilities vs AdapterProfile (model vs transport capabilities).
    * ConnectionContext (per-connection credentials, isolated by design).
    * ProviderAdapter Protocol.
    * LLMStreamEvent (normalized streaming event).

Design invariants
-----------------

1. This module is additive. It does not import or modify any existing
   runtime module except ``peldrun.llm.client`` (for ``TokenUsage``).
   No existing consumer is broken by landing it.

2. Model capabilities and adapter capabilities are strictly separate.
   A model may support reasoning while the transport does not, and
   vice versa. Only the INTERSECTION is honored at dispatch time.
   This is the core fix for "model supports X but provider does not
   advertise it" — the user can enable X manually, but the adapter
   still refuses to send unsupported parameters.

3. ``unknown`` and ``unverified`` are first-class states. Missing
   provider metadata MUST NOT collapse to ``unsupported`` — that would
   discard the user's manual override.

4. The protocol never dictates a wire format. Each adapter owns its
   own SDK, HTTP transport, streaming parser, and error mapping. This
   is what makes adding a new provider a self-contained exercise.

5. Capability inference from model names is NOT performed here. This
   module only models the vocabulary. Inference lives in
   ``capability_resolver.py`` and is only ever a suggestion — never a
   final answer.

See also
--------

    * ``backend/peldrun/llm/request.py``           — LLMRequest + GenerationConfig
    * ``backend/peldrun/llm/capabilities.py``      — merged user + provider caps
    * ``backend/peldrun/llm/capability_resolver.py`` — precedence resolver
    * ``backend/peldrun/llm/errors.py``            — ProviderError hierarchy
    * ``backend/peldrun/llm/providers/registry.py`` — adapter registry
    * ``backend/peldrun/llm/providers/factory.py``  — adapter factory
"""

from __future__ import annotations

from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.client import TokenUsage

if TYPE_CHECKING:
    # Imported lazily to avoid a runtime import cycle. The factory
    # always supplies a concrete ``LLMRequest`` instance; the Protocol
    # only needs the type at type-check time.
    from peldrun.llm.request import LLMRequest
    from peldrun.llm.client import LLMResponse


# ---------------------------------------------------------------------------
# Capability vocabulary
# ---------------------------------------------------------------------------


class CapabilityState(str, Enum):
    """
    Four-state model for a single capability.

    The four states exist because a simple boolean loses information
    that materially affects user trust:

    * ``supported``   — proven to work (manual override, provider
      catalog, or successful probe).
    * ``unsupported`` — proven NOT to work (explicit denial).
    * ``unknown``     — no information available; do not attempt by
      default, but do not warn either.
    * ``unverified``  — the user has enabled it, but no probe has
      confirmed the behavior. Distinct from ``supported`` so the UI
      can display "enabled (unverified)" instead of falsely claiming
      the capability is proven.

    The runtime treats ``supported`` and ``unverified`` identically
    when deciding to send a parameter — both mean "attempt it". The
    distinction is purely informational.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    UNVERIFIED = "unverified"


class CapabilitySource(str, Enum):
    """
    Origin of a capability determination.

    Ordered from highest to lowest priority by ``capability_resolver``.
    A higher-priority source always wins over a lower one for the same
    capability key.
    """

    #: Explicit user override in the model settings UI.
    MANUAL = "manual"

    #: Explicit metadata declared alongside the model definition.
    MODEL_METADATA = "model_metadata"

    #: Returned by the provider's model catalog (``/models``, ``/api/tags``).
    PROVIDER_CATALOG = "provider_catalog"

    #: Returned by a dedicated metadata endpoint (``/api/v0/models``, etc.).
    PROVIDER_METADATA = "provider_metadata"

    #: Determined by an explicit capability probe (paid call, user-approved).
    CAPABILITY_PROBE = "capability_probe"

    #: Inferred from observed behavior at runtime (last resort).
    RUNTIME_OBSERVATION = "runtime_observation"

    #: Reported by the LiteLLM community registry.
    EXTERNAL_REGISTRY = "external_registry"

    #: Reported by the HuggingFace model's config.json.
    HUGGINGFACE_CONFIG = "huggingface_config"

    #: Reported live by the provider's own capability endpoint
    #: (Ollama /api/show, LM Studio /api/v0/models, Google model endpoint).
    #: This is the highest-confidence non-manual source.
    PROVIDER_LIVE = "provider_live"

    #: No information available.
    UNKNOWN = "unknown"


class ReasoningMode(str, Enum):
    """
    Control mechanism exposed by the provider for reasoning.

    These map to provider-specific APIs:

    * ``effort``  → OpenAI ``reasoning.effort``, Anthropic
                    ``output_config.effort``.
    * ``budget``  → Anthropic ``thinking.budget_tokens``, Google
                    ``thinking_config.thinking_budget``.
    * ``toggle``  → Anthropic ``thinking.type = enabled``,
                    Ollama ``think = true``.
    * ``adaptive``→ provider decides; PELDRUN sends no parameter.
    * ``automatic``→ always-on, no control surface.
    """

    NONE = "none"
    AUTOMATIC = "automatic"
    ADAPTIVE = "adaptive"
    EFFORT = "effort"
    BUDGET = "budget"
    TOGGLE = "toggle"


class ReasoningStrategy(str, Enum):
    """
    Adapter-level translation strategy for reasoning parameters.

    Tells the RequestCompiler HOW to express the user's reasoning intent
    in the target API's native vocabulary. One strategy per adapter.
    """

    #: OpenAI Responses API: ``reasoning: { effort: "..." }``.
    OPENAI_RESPONSES = "openai_responses"

    #: OpenAI Chat Completions: ``reasoning_effort`` parameter.
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"

    #: Anthropic Messages API: ``thinking`` block or ``output_config.effort``.
    ANTHROPIC_MESSAGES = "anthropic_messages"

    #: Google GenAI: ``thinking_config`` in the generation config.
    GOOGLE_GENAI = "google_genai"

    #: Ollama native: ``think`` parameter (bool or string level).
    OLLAMA_NATIVE = "ollama_native"

    #: LM Studio native: forwarded inside the request body.
    LMSTUDIO_NATIVE = "lmstudio_native"

    #: OpenAI-compatible (generic): ``reasoning_effort`` extra_body.
    OPENAI_COMPATIBLE = "openai_compatible"

    #: No mapping — the adapter never forwards reasoning parameters.
    UNSUPPORTED = "unsupported"


class ToolCallingMode(str, Enum):
    """Whether and how the provider can invoke tools."""

    #: No tool support of any kind.
    UNSUPPORTED = "unsupported"

    #: Native tool calls (OpenAI function_calls, Anthropic tool_use,
    #: Google functionCall, Ollama tool_calls). Structured, safe, and
    #: the model was trained for it.
    NATIVE = "native"

    #: Emulated via prompt-engineering + text parsing. Less reliable;
    #: MUST validate the extracted arguments against the tool schema
    #: before executing anything.
    EMULATED = "emulated"


class ToolCallStyle(str, Enum):
    """Wire format used by the provider for tool calls."""

    OPENAI_FUNCTION = "openai_function"
    ANTHROPIC_TOOL_USE = "anthropic_tool_use"
    GOOGLE_FUNCTION_CALL = "google_function_call"
    OLLAMA_TOOL_CALL = "ollama_tool_call"
    LMSTUDIO_NATIVE = "lmstudio_native"


class StreamingSupport(str, Enum):
    """
    Granular streaming support.

    A provider may support text streaming but NOT reasoning streaming,
    and vice versa. The UI uses this to decide which toggles to show;
    the runtime uses it to decide what to attempt.
    """

    UNSUPPORTED = "unsupported"
    BUFFERED = "buffered"               # full response, then simulated chunks
    NATIVE_TEXT = "native_text"
    NATIVE_REASONING = "native_reasoning"
    NATIVE_TOOL_CALLS = "native_tool_calls"
    NATIVE_USAGE = "native_usage"       # usage arrives in the final chunk


class StructuredOutputMode(str, Enum):
    """Structured output support level."""

    UNSUPPORTED = "unsupported"
    JSON_OBJECT = "json_object"         # {"type": "json_object"}
    JSON_SCHEMA = "json_schema"         # {"type": "json_schema", "schema": {...}}


# ---------------------------------------------------------------------------
# Capability models
# ---------------------------------------------------------------------------


class ReasoningCapability(BaseModel):
    """
    Model-side reasoning capability.

    The ``details`` sub-fields describe the control surface the MODEL
    supports. The adapter separately declares which of those controls
    it can actually transmit (see ``AdapterProfile.reasoning_strategies``).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    state: CapabilityState = CapabilityState.UNKNOWN
    source: CapabilitySource = CapabilitySource.UNKNOWN

    #: Primary control mode (effort / budget / toggle / automatic / ...).
    mode: ReasoningMode = ReasoningMode.NONE

    #: Effort levels accepted (e.g. ["low", "medium", "high", "xhigh"]).
    effort_levels: List[str] = Field(default_factory=list)

    #: Default effort when the user enables reasoning without picking one.
    default_effort: Optional[str] = None

    #: Minimum / maximum budget in tokens (when ``mode == BUDGET``).
    min_budget_tokens: Optional[int] = None
    max_budget_tokens: Optional[int] = None

    #: Free-form notes shown in the UI (e.g. "provider auto-enables").
    notes: Optional[str] = None


class ToolCallingCapability(BaseModel):
    """Model-side tool calling capability."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    state: CapabilityState = CapabilityState.UNKNOWN
    source: CapabilitySource = CapabilitySource.UNKNOWN

    mode: ToolCallingMode = ToolCallingMode.UNSUPPORTED

    #: Whether the model can emit multiple tool calls in one turn.
    parallel: bool = False

    #: Whether the model honors ``tool_choice = {type: "function", ...}``.
    forced_choice: bool = False

    #: Whether the model accepts ``tool_choice = "required"`` (or similar).
    required_choice: bool = False

    notes: Optional[str] = None


class StreamingCapability(BaseModel):
    """Which streaming channels are natively supported by the model."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    state: CapabilityState = CapabilityState.UNKNOWN
    source: CapabilitySource = CapabilitySource.UNKNOWN

    text: bool = False
    reasoning: bool = False
    tool_calls: bool = False
    usage: bool = False

    notes: Optional[str] = None


class StructuredOutputCapability(BaseModel):
    """Structured output support level."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    state: CapabilityState = CapabilityState.UNKNOWN
    source: CapabilitySource = CapabilitySource.UNKNOWN

    mode: StructuredOutputMode = StructuredOutputMode.UNSUPPORTED


class ModalitiesCapability(BaseModel):
    """
    Multimodal I/O support.

    Kept as a free-form list rather than an enum so that future
    modalities (audio, video, documents) can be added without a
    breaking contract change.

    The ``source`` field records where the modalities were observed.
    ``capabilities.py`` uses it to distinguish "explicitly unsupported"
    (``vision=False`` + known source) from "unknown" (no source).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    input: List[str] = Field(default_factory=lambda: ["text"])
    output: List[str] = Field(default_factory=lambda: ["text"])
    source: CapabilitySource = CapabilitySource.UNKNOWN


class ModelCapabilities(BaseModel):
    """
    Aggregated capability profile for a single (provider, model) pair.

    The flat boolean fields (``vision``, ``trained_for_tool_use``,
    ``reasoning``) are retained for backward compatibility with the
    existing frontend ``ModelMetadata.capabilities`` shape. They must
    be kept in sync with the detailed sub-objects by the resolver —
    the resolver is the single writer.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Backward-compatible flat booleans (existing UI consumes these).
    vision: bool = False
    trained_for_tool_use: bool = False
    reasoning: Optional[bool] = None

    # Detailed sub-capabilities.
    reasoning_details: ReasoningCapability = Field(default_factory=ReasoningCapability)
    tool_calling: ToolCallingCapability = Field(default_factory=ToolCallingCapability)
    streaming: StreamingCapability = Field(default_factory=StreamingCapability)
    structured_output: StructuredOutputCapability = Field(default_factory=StructuredOutputCapability)
    modalities: ModalitiesCapability = Field(default_factory=ModalitiesCapability)


# ---------------------------------------------------------------------------
# Adapter profile (transport capabilities — MUST NOT be confused with
# model capabilities)
# ---------------------------------------------------------------------------


class AdapterFeatures(BaseModel):
    """
    What a specific adapter instance can actually transmit over the wire.

    This is deliberately separate from ``ModelCapabilities``. A model
    may support reasoning while the adapter cannot serialize it (e.g.
    an OpenAI-compatible custom endpoint that rejects
    ``reasoning_effort``); conversely, an adapter may support sending
    tools while a specific model refuses to use them.

    The runtime only attempts a feature when BOTH the adapter and the
    model declare support.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model_discovery: bool = False
    native_streaming: bool = False
    buffered_streaming: bool = True
    tool_calling: bool = False
    parallel_tool_calls: bool = False
    forced_tool_choice: bool = False
    vision: bool = False
    audio: bool = False
    structured_output: bool = False
    usage_reporting: bool = False
    reasoning: bool = False
    prompt_caching: bool = False


class AdapterProfile(BaseModel):
    """
    Runtime descriptor for a specific adapter instance.

    The factory builds this from the adapter's declared class attributes
    plus any per-connection overrides. The RequestCompiler consumes it
    to decide which parameters are safe to send.

    Key fields
    ----------
    ``api_surface``
        The wire surface this adapter talks to. Examples:
        ``"chat_completions"``, ``"responses"``, ``"messages"``,
        ``"generate_content"``, ``"ollama_native"``, ``"lmstudio_native"``.

    ``reasoning_strategies``
        Ordered list of ReasoningStrategy values this adapter can
        translate. The compiler picks the first strategy the model's
        ReasoningMode is compatible with.

    ``tool_call_style``
        Which wire format the adapter uses for tool calls.

    ``supported_param_names``
        Explicit allowlist of optional API parameter names. The
        compiler refuses to forward anything not in this set, which
        prevents "helpful" auto-fill from breaking custom providers
        that reject unknown keys.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    provider_id: str
    adapter_version: str = "1.0.0"
    api_surface: str = "chat_completions"

    features: AdapterFeatures = Field(default_factory=AdapterFeatures)

    reasoning_strategies: List[ReasoningStrategy] = Field(default_factory=list)
    tool_call_style: ToolCallStyle = ToolCallStyle.OPENAI_FUNCTION

    #: Optional allowlist of accepted parameter names. Empty list means
    #: "the adapter has not declared a restriction" — used only for
    #: legacy adapters during migration. New adapters SHOULD populate
    #: this to prevent accidental parameter leakage.
    supported_param_names: List[str] = Field(default_factory=list)

    #: Optional per-adapter notes surfaced in the Request Inspector.
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Connection context
# ---------------------------------------------------------------------------


class ConnectionContext(BaseModel):
    """
    Isolated per-connection credentials and endpoint configuration.

    The adapter ONLY receives this object — it never has access to the
    full provider registry, other connections' secrets, or the workspace
    filesystem. This is the boundary that makes Provider Packages safe
    to load: a malicious package cannot read another provider's key.

    The ``api_key`` field is sensitive. Callers MUST NOT log the raw
    model; use ``redacted_dump()`` for telemetry.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    connection_id: str
    provider_id: str

    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_type: Optional[str] = None

    #: Per-connection HTTP timeout in seconds.
    timeout: float = 120.0

    #: Additional headers forwarded on every request (e.g. OpenRouter
    #: ``HTTP-Referer``, ``X-Title``).
    extra_headers: Dict[str, str] = Field(default_factory=dict)

    #: Free-form per-provider options forwarded verbatim to the adapter.
    #: The adapter is responsible for validating them; the framework
    #: does not interpret them.
    extra_params: Dict[str, Any] = Field(default_factory=dict)

    #: Optional dedicated metadata endpoint.
    metadata_endpoint: Optional[str] = None

    #: TLS verification flag.
    verify_ssl: bool = True

    #: Whether the adapter may read workspace files (default False).
    #: Provider adapters never need this; it exists so the same context
    #: can serve future tool/plugin packages that do.
    workspace_access: bool = False

    def redacted_dump(self) -> Dict[str, Any]:
        """
        Return a dict safe for logging and telemetry.

        Secrets are replaced with fixed markers, not truncated values,
        because even 4 characters of an API key are useful to an
        attacker who guesses the format.
        """
        data = self.model_dump()
        if data.get("api_key"):
            data["api_key"] = "<redacted>"
        # Redact authorization-style headers.
        headers = data.get("extra_headers") or {}
        for header_name in list(headers.keys()):
            if header_name.lower() in (
                "authorization",
                "x-api-key",
                "x-goog-api-key",
                "api-key",
            ):
                headers[header_name] = "<redacted>"
        return data


# ---------------------------------------------------------------------------
# Streaming event
# ---------------------------------------------------------------------------


class StreamEventType(str, Enum):
    """Normalized streaming event types emitted by every adapter."""

    MESSAGE_START = "message_start"
    CONTENT_DELTA = "content_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETE = "tool_call_complete"
    USAGE_UPDATE = "usage_update"
    MESSAGE_COMPLETE = "message_complete"
    PROVIDER_ERROR = "provider_error"


class LLMStreamEvent(BaseModel):
    """
    Adapter-agnostic streaming event.

    Every adapter's ``stream()`` method yields a sequence of these.
    The StreamNormalizer converts them into the legacy ``StreamChunk``
    shape that ``on_chunk`` / ``on_thought_chunk`` callbacks expect, so
    existing consumers (ToolCallAgent, Direct Chat) keep working
    unchanged.

    Fields are all optional because each event type only populates a
    subset. The ``type`` discriminator tells the consumer which fields
    to read.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: StreamEventType

    # Text content.
    content_delta: Optional[str] = None
    reasoning_delta: Optional[str] = None

    # Tool call lifecycle.
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_index: Optional[int] = None
    tool_arguments_delta: Optional[str] = None
    tool_arguments_complete: Optional[Dict[str, Any]] = None

    # Terminal metadata.
    usage: Optional[TokenUsage] = None
    finish_reason: Optional[str] = None

    # Structured error payload for PROVIDER_ERROR events. Shape:
    #   {
    #     "code": "auth_failed",
    #     "message": "...",
    #     "retryable": false,
    #     "provider": "openai",
    #   }
    error: Optional[Dict[str, Any]] = None

    # Adapter-specific extras. Consumers MUST NOT rely on these for
    # control flow; they exist for diagnostics and Request Inspector.
    provider_metadata: Dict[str, Any] = Field(default_factory=dict)

    #: Monotonic sequence number scoped to a single stream. Used by the
    #: normalizer to preserve order when bridging to SSE.
    sequence: int = 0


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProviderAdapter(Protocol):
    """
    Structural contract for every provider adapter.

    Adapters are intentionally lightweight. They own:
        * The SDK / HTTP client instance for one connection.
        * Request compilation (PELDRUN → provider payload).
        * Response parsing (provider response → LLMResponse).
        * Stream parsing (provider stream → LLMStreamEvent).
        * Error mapping (provider exception → ProviderError).

    They do NOT own:
        * Capability resolution (see ``capability_resolver``).
        * Budget enforcement (see ``runtime/executor``).
        * Accounting (see ``llm/accounting``).
        * Tool execution (see ``ToolRegistry`` / ``ToolCallAgent``).

    The protocol is structural, not nominal. Any class with these
    methods is a valid adapter. This lets Provider Packages define
    adapters in their own module without importing PELDRUN internals
    beyond this file.
    """

    #: Human-readable provider identifier (e.g. "openai", "anthropic").
    #: Populated by the adapter; used only for logging and diagnostics.
    provider_id: str

    #: Adapter profile describing transport capabilities.
    profile: AdapterProfile

    async def generate(self, request: "LLMRequest") -> "LLMResponse":
        """
        Execute a single non-streaming request.

        Must return a fully populated ``LLMResponse`` including
        ``content`` (may be empty string), ``tool_calls`` (may be empty
        list), ``finish_reason``, and ``usage`` (may be ``None`` when
        the provider does not report usage — the accounting layer will
        fall back to estimation).

        Raises ``ProviderError`` on failure.
        """
        ...

    def stream(self, request: "LLMRequest") -> AsyncIterator[LLMStreamEvent]:
        """
        Execute a single streaming request.

        Yields a sequence of ``LLMStreamEvent``. The caller may cancel
        at any ``await`` point; adapters MUST handle ``CancelledError``
        by closing the underlying connection before re-raising.

        Adapters that do not support native streaming MUST emulate it
        by calling ``generate()`` and yielding a single
        ``MESSAGE_COMPLETE`` event carrying the full response. They
        MUST NOT claim native streaming support in their profile.
        """
        ...

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        Return a list of model descriptors available on this connection.

        Each item MUST include at least ``{"id": "..."}``. Additional
        fields (``display_name``, ``max_context_length``,
        ``capabilities``) are forwarded to the metadata vault verbatim.

        Adapters that do not support discovery MUST return an empty
        list. The user can still enter model IDs manually.
        """
        ...

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        """
        Probe the connection without sending a generation request.

        Returns ``{"ok": bool, "message": str, "latency_ms": float}``.
        Auth errors, timeouts, and unsupported endpoints must be
        reported as ``ok=False`` with a human-readable message.
        """
        ...

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return provider-supplied metadata for a single model.

        Returns ``None`` when the provider exposes no per-model
        metadata endpoint. Never raises — a discovery failure must not
        break UI rendering.
        """
        ...

    async def close(self) -> None:
        """
        Release the underlying HTTP client and any open connections.

        Called by the factory on reconfiguration and by app shutdown.
        MUST be idempotent.
        """
        ...


__all__ = [
    # Enums
    "CapabilityState",
    "CapabilitySource",
    "ReasoningMode",
    "ReasoningStrategy",
    "ToolCallingMode",
    "ToolCallStyle",
    "StreamingSupport",
    "StructuredOutputMode",
    "StreamEventType",
    # Capability models
    "ReasoningCapability",
    "ToolCallingCapability",
    "StreamingCapability",
    "StructuredOutputCapability",
    "ModalitiesCapability",
    "ModelCapabilities",
    # Adapter profile + connection
    "AdapterFeatures",
    "AdapterProfile",
    "ConnectionContext",
    # Streaming
    "LLMStreamEvent",
    # Protocol
    "ProviderAdapter",
]
