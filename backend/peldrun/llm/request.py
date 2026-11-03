# backend/peldrun/llm/request.py
"""
PELDRUN Unified Internal Request Contract.

Defines ``LLMRequest`` — the adapter-agnostic request object that every
provider adapter receives. This is the internal translation target that
both execution paths build from their existing ``active_llm`` dicts:

    * ``omweb.agent_bridge_parts.dispatcher.run_direct_chat`` (Direct Chat)
    * ``peldrun.runtime.executor`` (Agent Mode)

Design invariants
-----------------

1. **Additive only.** This module introduces a new class hierarchy but
   modifies nothing. Existing call sites continue to pass raw dicts to
   ``AsyncLLMClient.chat_completion`` until M7 explicitly migrates them.
   No existing consumer is broken by landing it.

2. **Adapters never see provider-specific fields.** Every adapter reads
   ``LLMRequest`` and nothing else. Provider-specific settings live in
   ``provider_options`` — a free-form dict the adapter MAY read but the
   framework MUST NOT interpret. This is what allows the same request
   shape to serve OpenAI, Anthropic, Google, Ollama, LM Studio, and
   custom adapters without a single ``if provider == ...`` branch in
   the runtime.

3. **Capabilities are data, not decisions.** ``model_capabilities`` is
   carried on the request so the RequestCompiler can decide what to
   forward. The adapter itself never re-resolves capabilities — it
   trusts the compiled payload. This keeps capability logic in exactly
   one place (``capability_resolver`` + ``request_compiler``), not
   scattered across every adapter.

4. **Generation settings and reasoning are separate.** A model may
   allow ``temperature=1.5`` in some modes and require ``temperature=1.0``
   when reasoning is enabled. Keeping the two in separate sub-objects
   lets the RequestCompiler enforce provider-specific conflicts without
   ambiguity.

5. **No side effects at construction.** All validation is lazy. A
   malformed request raises only when the adapter actually tries to
   use the malformed field — which lets the compiler emit a targeted
   error instead of a generic Pydantic traceback.

See also
--------

    * ``backend/peldrun/llm/contracts.py``           — vocabulary
    * ``backend/peldrun/llm/capabilities.py``        — merged capability view
    * ``backend/peldrun/llm/request_compiler.py``    — compiles into payloads
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.contracts import (
    ConnectionContext,
    ModelCapabilities,
    ReasoningMode,
)


# ---------------------------------------------------------------------------
# Generation parameters
# ---------------------------------------------------------------------------


class GenerationConfig(BaseModel):
    """
    Model sampling and length parameters.

    All fields are optional because not every provider supports every
    parameter. The RequestCompiler drops unsupported fields before the
    adapter sees the compiled payload — a value here is a REQUEST, not
    a guarantee.

    Provider-specific quirks (e.g. Anthropic requiring ``max_tokens``,
    Google requiring ``max_output_tokens``) are resolved by the
    compiler using ``AdapterProfile``, not by branching here.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None

    #: Maximum tokens to generate. Named ``max_output_tokens`` for
    #: consistency with the frontend budget model; adapters map it to
    #: the provider's native name (``max_tokens``, ``max_output_tokens``,
    #: ``max_new_tokens``, ...).
    max_output_tokens: Optional[int] = None

    #: Stop sequences. Each provider takes a list of strings.
    stop: List[str] = Field(default_factory=list)

    #: Seed for deterministic sampling. Supported by OpenAI, Google,
    #: Anthropic (beta), LM Studio; not by Ollama. The compiler drops
    #: it silently when unsupported.
    seed: Optional[int] = None

    #: Frequency / presence penalties (OpenAI-style). Anthropic maps
    #: them to its own parameters; Google has no equivalent and the
    #: compiler drops them.
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None

    #: Whether the caller wants the model to think before responding
    #: (reasoning toggle for models that expose it as a boolean).
    #: Distinct from ``ReasoningConfig`` which carries the richer
    #: effort/budget controls.
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Reasoning configuration
# ---------------------------------------------------------------------------


class ReasoningConfig(BaseModel):
    """
    User intent for reasoning, expressed in provider-neutral terms.

    The RequestCompiler translates this into each provider's native
    vocabulary using ``AdapterProfile.reasoning_strategies``:

        * OpenAI → ``reasoning: { effort: ... }`` or ``reasoning_effort``.
        * Anthropic → ``thinking: { type: "enabled", budget_tokens: N }``
          or ``output_config: { effort: ... }``.
        * Google → ``thinking_config: { thinking_budget: N }`` or
          ``thinking_level: ...``.
        * Ollama → ``think: true | "high"``.
        * OpenAI-compatible → ``reasoning_effort`` in ``extra_body``.

    The user's intent NEVER dictates how the parameter is expressed —
    that is always the adapter's responsibility. This is why the same
    request works across all providers without any provider-specific
    logic in the caller.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    #: Whether reasoning is desired. ``False`` explicitly disables it
    #: (a valid state for models where reasoning is default-on).
    enabled: bool = False

    #: Primary control mode. When ``NONE``, the compiler will not emit
    #: any reasoning parameter, which is correct for models that enable
    #: reasoning automatically.
    mode: ReasoningMode = ReasoningMode.NONE

    #: Effort level ("low" / "medium" / "high" / "xhigh" / ...). Only
    #: meaningful when ``mode == EFFORT``.
    effort: Optional[str] = None

    #: Token budget. Only meaningful when ``mode == BUDGET``.
    budget_tokens: Optional[int] = None

    #: Optional cap on how much reasoning output the model may emit.
    #: Distinct from ``budget_tokens`` — some providers distinguish
    #: between "the model may think for N tokens" and "the reasoning
    #: output must fit in N tokens".
    max_reasoning_tokens: Optional[int] = None

    #: Free-form provider-specific reasoning overrides. Rarely needed;
    #: escape hatch for emerging providers whose reasoning control
    #: surface does not fit the model above.
    provider_options: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool calling configuration
# ---------------------------------------------------------------------------


class ToolChoiceSpec(BaseModel):
    """
    Explicit tool-choice directive.

    ``mode`` values map to the well-known OpenAI vocabulary:

        * ``"auto"``     — model decides whether to call a tool.
        * ``"none"``     — model must not call a tool.
        * ``"required"`` — model must call at least one tool.
        * ``"specific"`` — model must call the named tool (in ``name``).

    Adapters that do not support a mode MUST drop the directive and
    emit a warning event (not raise), so callers cannot accidentally
    break an otherwise-working provider just because it lacks
    ``required`` support.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mode: str = "auto"
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Structured output configuration
# ---------------------------------------------------------------------------


class ResponseFormat(BaseModel):
    """
    Structured output request.

    ``kind`` values:

        * ``"text"``        — no constraint (default).
        * ``"json_object"`` — provider must return a valid JSON object.
        * ``"json_schema"`` — provider must match ``schema`` exactly.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: str = "text"
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias="schema")
    name: Optional[str] = None
    strict: bool = False

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        protected_namespaces=(),
    )


# ---------------------------------------------------------------------------
# Unified request
# ---------------------------------------------------------------------------


class LLMRequest(BaseModel):
    """
    Adapter-agnostic request.

    This is the object the RequestCompiler consumes and the object
    every adapter's ``generate`` / ``stream`` methods receive. It
    carries only the information an adapter needs — nothing about
    agents, workspaces, budgets, or accounting.

    Construction
    ------------
    Callers (dispatcher, executor) construct this from their existing
    ``active_llm`` dict. Example::

        request = LLMRequest(
            provider_id=active_llm["provider"],
            model_id=active_llm["model"],
            messages=messages,
            connection=ConnectionContext(
                connection_id=active_llm["provider"],
                provider_id=active_llm["provider"],
                base_url=active_llm["base_url"],
                api_key=active_llm["api_key"],
                timeout=float(active_llm.get("timeout") or 120.0),
            ),
            generation=GenerationConfig(
                temperature=float(active_llm["temperature"]),
                max_output_tokens=int(active_llm["max_tokens"]),
            ),
            reasoning=ReasoningConfig(
                enabled=bool(active_llm.get("reasoning_effort")),
                mode=ReasoningMode.EFFORT,
                effort=active_llm.get("reasoning_effort"),
            ),
            model_capabilities=resolved_caps,
        )

    The request is immutable by convention: adapters MUST NOT mutate
    it. Any transformation (payload compilation) happens on a fresh
    dict that the adapter owns.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # --- Identity ---
    provider_id: str
    model_id: str
    connection_id: Optional[str] = None

    # --- Content ---
    #: Message list in OpenAI-compatible shape. Adapters convert to
    #: their own format (Anthropic ``system`` + ``messages``; Google
    #: ``contents``; Ollama ``messages``). The list MAY contain
    #: multimodal parts already (``{"type": "image_url", ...}``).
    messages: List[Dict[str, Any]]

    #: System prompt in raw text form. Present separately from
    #: ``messages`` because Anthropic requires it as a top-level field
    #: and Google prefers it inside ``system_instruction``.
    system_prompt: Optional[str] = None

    #: Tool definitions in OpenAI-compatible shape. Adapters convert
    #: to their own format when needed (Anthropic ``tools``, Google
    #: ``function_declarations``). The compiler drops these when the
    #: effective capability intersection says tools are unsupported.
    tools: List[Dict[str, Any]] = Field(default_factory=list)

    tool_choice: ToolChoiceSpec = Field(default_factory=ToolChoiceSpec)

    # --- Configuration ---
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    response_format: ResponseFormat = Field(default_factory=ResponseFormat)

    # --- Capabilities (resolved) ---
    #: The resolved capability profile for this (provider, model) pair.
    #: Populated by ``capability_resolver`` before the request reaches
    #: the adapter. Adapters MUST NOT re-resolve capabilities — they
    #: trust this object.
    model_capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)

    # --- Connection ---
    #: Per-connection credentials and endpoint configuration. The
    #: adapter only ever sees this object, never the full provider
    #: registry. This is the isolation boundary for Provider Packages.
    connection: ConnectionContext

    # --- Escape hatch ---
    #: Provider-specific overrides forwarded verbatim to the adapter.
    #: The framework never interprets these. Adapters MAY read them if
    #: their profile declares support for such options.
    provider_options: Dict[str, Any] = Field(default_factory=dict)

    # --- Metadata (diagnostics only) ---
    #: Free-form metadata for logging and Request Inspector. Adapters
    #: MUST NOT read this for control flow.
    metadata: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "GenerationConfig",
    "ReasoningConfig",
    "ToolChoiceSpec",
    "ResponseFormat",
    "LLMRequest",
]
