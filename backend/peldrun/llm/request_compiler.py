# backend/peldrun/llm/request_compiler.py
"""
PELDRUN Request Compiler.

Turns a user-level ``LLMRequest`` plus the resolved capability decisions
and adapter profile into a single ``CompiledRequest`` that an adapter
can execute directly.

The compiler is the ONLY place where:
    * Parameters unsupported by the model or by the transport are
      dropped (never sent silently).
    * Parameter name differences between providers are resolved
      (``max_output_tokens`` → ``max_tokens`` / ``max_completion_tokens``
      depending on the surface).
    * ``reasoning`` intent is translated into the exact wire format
      the adapter expects (``effort``, ``budget_tokens``, ``think``, ...).
    * ``temperature`` is dropped when it conflicts with reasoning
      (OpenAI o1/o3 series, Anthropic extended thinking).
    * A structured diagnostic trail is built so the Request Inspector
      can explain "why didn't reasoning fire?" without guesswork.

Design principles
-----------------

1. **Pure.** No I/O, no network, no logging side effects beyond a
   debug-level log. Fully testable in isolation.

2. **Capability-first.** Every optional parameter is only included when
   ``ResolvedCapabilities`` marks its decision as ``ENABLED``. A
   capability that is ``DOWNGRADED`` or ``DISABLED`` results in a
   diagnostic entry, never a silently-dropped parameter.

3. **Strategy-driven.** Reasoning, tool choice, and structured output
   are compiled according to ``AdapterProfile`` (via
   ``reasoning_strategies``, ``tool_call_style``, ``api_surface``). The
   compiler knows each strategy's wire format; the adapter only calls
   the SDK.

4. **Surface-aware.** ``max_output_tokens`` maps to:
       * ``max_tokens``             — Chat Completions, OpenAI-compat
       * ``max_output_tokens``      — OpenAI Responses API
       * ``max_tokens``             — Anthropic (required field)
       * ``max_output_tokens``      — Google GenAI
       * ``num_predict``            — Ollama native (``options``)
   The mapping is chosen by ``api_surface``, not by provider name.

5. **Diagnostics are truthful.** If a parameter is dropped because of
   a capability mismatch, the reason is recorded with the source
   (``adapter_unsupported``, ``model_unsupported``, ``conflict``,
   ``missing_required``). The Request Inspector surfaces this verbatim.

See also
--------

    * ``backend/peldrun/llm/contracts.py``   — vocabulary
    * ``backend/peldrun/llm/request.py``     — LLMRequest
    * ``backend/peldrun/llm/capabilities.py`` — intersection
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from peldrun.llm.capabilities import (
    EffectiveState,
    ResolvedCapabilities,
    compute_resolved_capabilities,
)
from peldrun.llm.contracts import (
    AdapterProfile,
    ReasoningStrategy,
    StructuredOutputMode,
    ToolCallStyle,
)
from peldrun.llm.request import (
    LLMRequest,
    ReasoningConfig,
    ResponseFormat,
    ToolChoiceSpec,
)

logger = logging.getLogger("peldrun.llm.request_compiler")


# ---------------------------------------------------------------------------
# Compiled request
# ---------------------------------------------------------------------------


@dataclass
class CompiledRequest:
    """
    Fully-compiled request, ready for an adapter to execute.

    The adapter reads the fields whose names match its SDK surface:

    * ``api_surface == "chat_completions"``:
        uses ``messages``, ``tools``, ``generation``, ``response_format``,
        ``reasoning`` (already shaped for Chat Completions).

    * ``api_surface == "responses"``:
        uses ``input`` (or ``messages`` — the adapter decides), ``tools``,
        ``generation``, ``reasoning``.

    * ``api_surface == "messages"`` (Anthropic):
        uses ``system_prompt``, ``messages``, ``tools``, ``generation.max_output_tokens``,
        ``reasoning`` (already shaped as a ``thinking`` block or
        ``output_config.effort``).

    * ``api_surface == "generate_content"`` (Google):
        uses ``messages``, ``generation``, ``reasoning`` (thinking_config),
        ``response_format``.

    * ``api_surface == "ollama_native"``:
        uses ``messages``, ``tools``, ``generation``, ``reasoning.think``.

    * ``api_surface == "lmstudio_native"``:
        uses ``messages``, ``tools``, ``generation``, ``reasoning``
        (forwarded verbatim).
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    api_surface: str
    model: str

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    messages: List[Dict[str, Any]]

    #: Only used by the Responses API surface (some models require
    #: ``input`` instead of ``messages``). When ``None``, the adapter
    #: uses ``messages``.
    input: Optional[List[Dict[str, Any]]] = None

    #: Only used by Anthropic. Extracted from the system role in
    #: ``messages`` before the adapter sends the request.
    system_prompt: Optional[str] = None

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Response format
    # ------------------------------------------------------------------
    response_format: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    #: Normalized generation options. The adapter maps these to its
    #: SDK's kwarg names.
    generation: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Reasoning (strategy-specific)
    # ------------------------------------------------------------------
    #: Shape depends on the winning ``ReasoningStrategy``. See
    #: ``_compile_reasoning_for_strategy`` for the exact shapes.
    reasoning: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Provider escape hatch
    # ------------------------------------------------------------------
    provider_options: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Diagnostics (Request Inspector)
    # ------------------------------------------------------------------
    #: List of dicts, each with ``field``, ``action`` (``dropped`` /
    #: ``adjusted`` / ``included``), ``reason`` (a stable string), and
    #: optional ``detail``.
    diagnostics: Dict[str, Any] = field(
        default_factory=lambda: {
            "dropped": [],
            "adjusted": [],
            "included": [],
            "conflicts": [],
        }
    )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict form for adapters that prefer raw dicts."""
        return {
            "api_surface": self.api_surface,
            "model": self.model,
            "messages": self.messages,
            "input": self.input,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
            "tool_choice": self.tool_choice,
            "response_format": self.response_format,
            "generation": dict(self.generation),
            "reasoning": self.reasoning,
            "provider_options": dict(self.provider_options),
            "diagnostics": dict(self.diagnostics),
        }

    def redacted_preview(self) -> Dict[str, Any]:
        """
        Return a redacted view suitable for the Request Inspector.

        Hides message content by default. Message roles and part-type
        counts are preserved so the user can verify structure without
        seeing potentially sensitive text.
        """
        redacted_messages = []
        for msg in self.messages:
            role = msg.get("role", "?")
            content = msg.get("content")
            if isinstance(content, str):
                redacted_messages.append(
                    {"role": role, "content": f"<{len(content)} chars>"}
                )
            elif isinstance(content, list):
                parts_summary = []
                for part in content:
                    if isinstance(part, dict):
                        ptype = part.get("type", "?")
                        if ptype == "text":
                            parts_summary.append(
                                f"text({len(part.get('text', '') or '')})"
                            )
                        elif ptype == "image_url":
                            parts_summary.append("image_url")
                        else:
                            parts_summary.append(str(ptype))
                    else:
                        parts_summary.append("?")
                redacted_messages.append(
                    {"role": role, "parts": parts_summary}
                )
            else:
                redacted_messages.append({"role": role, "content": "<?>"})

        return {
            "api_surface": self.api_surface,
            "model": self.model,
            "messages": redacted_messages,
            "system_prompt_present": bool(self.system_prompt),
            "tools_count": len(self.tools) if self.tools else 0,
            "tool_choice": self.tool_choice,
            "response_format_kind": (
                self.response_format.get("type")
                if self.response_format
                else None
            ),
            "generation": dict(self.generation),
            "reasoning": self.reasoning,
            "provider_options_keys": sorted(self.provider_options.keys()),
            "diagnostics": dict(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


class RequestCompiler:
    """
    Stateless request compiler.

    Thread-safe. Instance reuse is safe. Prefer the module-level
    ``compile_request`` helper for simple call sites.
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def compile(
        self,
        request: LLMRequest,
        adapter_profile: AdapterProfile,
        resolved_caps: Optional[ResolvedCapabilities] = None,
    ) -> CompiledRequest:
        """
        Compile a request into a ``CompiledRequest``.

        Parameters
        ----------
        request
            The user-level ``LLMRequest``.
        adapter_profile
            The adapter's declared transport capabilities. Determines
            which API surface, reasoning strategy, and tool style to
            use.
        resolved_caps
            Pre-computed intersection of model + adapter capabilities.
            When omitted, the compiler calls
            ``compute_resolved_capabilities`` itself using
            ``request.model_capabilities`` and ``adapter_profile``.

        Returns
        -------
        CompiledRequest
            Fully compiled. Never raises on missing optional fields —
            diagnostics record what was dropped and why.
        """
        if resolved_caps is None:
            resolved_caps = compute_resolved_capabilities(
                provider_id=request.provider_id,
                model_id=request.model_id,
                model_caps=request.model_capabilities,
                adapter_profile=adapter_profile,
            )

        # ------------------------------------------------------------------
        # 1. Extract system prompt from messages.
        #
        # The input LLMRequest separates ``system_prompt`` from
        # ``messages`` already, but legacy callers sometimes embed the
        # system message inline. This normalizes both shapes.
        # ------------------------------------------------------------------
        system_prompt, messages = self._extract_system_prompt(
            request.messages, request.system_prompt
        )

        # ------------------------------------------------------------------
        # 2. Resolve API surface.
        #
        # Some models require a specific surface (e.g. o1-series requires
        # Responses API for Function Calling); the adapter declares its
        # default surface; the caller may override via
        # ``provider_options["api_surface"]`` (rare).
        # ------------------------------------------------------------------
        api_surface = self._resolve_api_surface(request, adapter_profile)

        # ------------------------------------------------------------------
        # 3. Compile generation.
        #
        # Translate ``max_output_tokens`` to the surface-specific name,
        # drop unsupported generation params, and handle the
        # temperature/reasoning conflict.
        # ------------------------------------------------------------------
        generation, gen_diagnostics = self._compile_generation(
            request, api_surface, adapter_profile, resolved_caps
        )

        # ------------------------------------------------------------------
        # 4. Compile reasoning.
        #
        # Emit the strategy-specific shape. Returns ``None`` when
        # reasoning is not enabled.
        # ------------------------------------------------------------------
        reasoning, reasoning_diagnostics = self._compile_reasoning(
            request.reasoning,
            resolved_caps,
            adapter_profile,
        )

        # ------------------------------------------------------------------
        # 5. Compile tools.
        #
        # Sanitize tools to OpenAI shape, drop if unsupported. Also
        # translates ``tool_choice`` per the adapter's tool_call_style.
        # ------------------------------------------------------------------
        tools, tool_choice, tools_diagnostics = self._compile_tools(
            request.tools,
            request.tool_choice,
            resolved_caps,
            adapter_profile,
        )

        # ------------------------------------------------------------------
        # 6. Compile response format.
        # ------------------------------------------------------------------
        response_format, rf_diagnostics = self._compile_response_format(
            request.response_format,
            resolved_caps,
        )

        # ------------------------------------------------------------------
        # 7. Merge diagnostics.
        # ------------------------------------------------------------------
        diagnostics: Dict[str, Any] = {
            "dropped": [],
            "adjusted": [],
            "included": [],
            "conflicts": [],
        }
        for bucket in (
            gen_diagnostics,
            reasoning_diagnostics,
            tools_diagnostics,
            rf_diagnostics,
        ):
            for key in ("dropped", "adjusted", "included", "conflicts"):
                diagnostics[key].extend(bucket.get(key, []))

        # ------------------------------------------------------------------
        # 8. Build the final result.
        # ------------------------------------------------------------------
        return CompiledRequest(
            api_surface=api_surface,
            model=request.model_id,
            messages=messages,
            input=None,  # Future: Responses API can use input format
            system_prompt=system_prompt,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            generation=generation,
            reasoning=reasoning,
            provider_options=dict(request.provider_options or {}),
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------------
    # Helpers — extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_system_prompt(
        messages: List[Dict[str, Any]],
        explicit_system: Optional[str],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """
        Separate the system prompt from the messages list.

        Anthropic and Google require the system prompt as a top-level
        field, not a ``role: "system"`` message. This helper normalizes
        both shapes:

        * When ``explicit_system`` is provided, all ``system`` messages
          are REMOVED from ``messages`` and ``explicit_system`` wins.
        * When ``explicit_system`` is absent, the first ``system``
          message is extracted and returned as the system prompt.
        * Any further ``system`` messages are kept inline (rare; some
          callers rely on multiple system messages for OpenAI).
        """
        if not messages:
            return explicit_system, []

        result: List[Dict[str, Any]] = []
        first_system: Optional[str] = None

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                content = msg.get("content", "")
                text = content if isinstance(content, str) else str(content)
                if first_system is None and not explicit_system:
                    first_system = text
                    continue
                # Skip additional system messages when explicit system
                # is already provided — they are redundant.
                if explicit_system:
                    continue
            result.append(msg)

        return (explicit_system or first_system), result

    # ------------------------------------------------------------------
    # Helpers — API surface resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_api_surface(
        request: LLMRequest,
        adapter_profile: AdapterProfile,
    ) -> str:
        """
        Resolve the actual API surface to use.

        Precedence:
            1. ``request.provider_options["api_surface"]`` — explicit
               caller override. Rare; used for testing or advanced use.
            2. ``adapter_profile.api_surface`` — the adapter's default.

        The compiler does NOT consult model capabilities here — the
        adapter's profile is the authoritative source.
        """
        override = (request.provider_options or {}).get("api_surface")
        if isinstance(override, str) and override.strip():
            return override.strip()
        return adapter_profile.api_surface or "chat_completions"

    # ------------------------------------------------------------------
    # Helpers — generation
    # ------------------------------------------------------------------

    #: Generation parameter names whose values must be dropped when
    #: reasoning is active, for providers whose reasoning modes require
    #: deterministic sampling (temperature = 1.0, no top_p, no
    #: frequency/presence penalties).
    _REASONING_CONFLICTS = (
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
    )

    def _compile_generation(
        self,
        request: LLMRequest,
        api_surface: str,
        adapter_profile: AdapterProfile,
        resolved_caps: ResolvedCapabilities,
    ) -> tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
        """
        Build the generation parameters dict.

        Returns ``(generation_dict, diagnostics)``.

        Rules
        -----
        * ``max_output_tokens`` is renamed per surface (see module
          docstring).
        * When reasoning is enabled AND the surface requires
          deterministic sampling (OpenAI o1/o3, Anthropic extended
          thinking), temperature and sampling params are dropped.
        * When ``supported_param_names`` is declared on the adapter
          profile, only those names are forwarded.
        """
        diag: Dict[str, List[Dict[str, Any]]] = {
            "dropped": [],
            "adjusted": [],
            "included": [],
            "conflicts": [],
        }
        gen = request.generation
        result: Dict[str, Any] = {}

        # ------------------------------------------------------------------
        # 1. Max output tokens — renamed per surface.
        # ------------------------------------------------------------------
        if gen.max_output_tokens is not None:
            max_name = self._max_output_tokens_key(api_surface)
            result[max_name] = int(gen.max_output_tokens)
            diag["included"].append(
                {
                    "field": max_name,
                    "value": int(gen.max_output_tokens),
                }
            )

        # ------------------------------------------------------------------
        # 2. Temperature and sampling.
        #
        # Deferred until after reasoning is known — see step 4.
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # 3. Always-safe generation params.
        # ------------------------------------------------------------------
        if gen.stop:
            result["stop"] = list(gen.stop)
            diag["included"].append({"field": "stop", "count": len(gen.stop)})
        if gen.seed is not None:
            result["seed"] = int(gen.seed)
            diag["included"].append({"field": "seed", "value": int(gen.seed)})

        # ------------------------------------------------------------------
        # 4. Temperature / reasoning conflict check.
        #
        # Determine once whether reasoning forces deterministic
        # sampling on this surface, then apply to all sampling params.
        # ------------------------------------------------------------------
        forces_deterministic = self._surface_forces_deterministic_sampling(
            api_surface, request.reasoning, resolved_caps
        )

        if forces_deterministic:
            for field_name in self._REASONING_CONFLICTS:
                value = getattr(gen, field_name, None)
                if value is not None:
                    diag["dropped"].append(
                        {
                            "field": field_name,
                            "reason": "conflict_with_reasoning",
                            "detail": (
                                "This provider requires deterministic "
                                "sampling when reasoning is active. "
                                f"{field_name}={value} was dropped."
                            ),
                        }
                    )
                    diag["conflicts"].append(
                        {"field": field_name, "reason": "reasoning_active"}
                    )
        else:
            # Apply temperature and sampling normally.
            if gen.temperature is not None:
                result["temperature"] = float(gen.temperature)
                diag["included"].append(
                    {"field": "temperature", "value": float(gen.temperature)}
                )
            if gen.top_p is not None:
                result["top_p"] = float(gen.top_p)
                diag["included"].append(
                    {"field": "top_p", "value": float(gen.top_p)}
                )
            if gen.top_k is not None:
                result["top_k"] = int(gen.top_k)
                diag["included"].append(
                    {"field": "top_k", "value": int(gen.top_k)}
                )
            if gen.frequency_penalty is not None:
                result["frequency_penalty"] = float(gen.frequency_penalty)
                diag["included"].append(
                    {
                        "field": "frequency_penalty",
                        "value": float(gen.frequency_penalty),
                    }
                )
            if gen.presence_penalty is not None:
                result["presence_penalty"] = float(gen.presence_penalty)
                diag["included"].append(
                    {
                        "field": "presence_penalty",
                        "value": float(gen.presence_penalty),
                    }
                )

        # ------------------------------------------------------------------
        # 5. Surface-specific name alignment.
        #
        # Ollama expects generation params under ``options`` with
        # different names. Instead of renaming here (which would make
        # the compiled object less portable), record the target shape
        # in ``provider_options`` so the Ollama adapter knows to
        # re-nest these fields.
        #
        # For now we leave this to the adapter — the compiler's job is
        # to produce the correctly-named fields for the surface, not to
        # restructure the tree.
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # 6. Supported-param-names filter (if the adapter declares one).
        # ------------------------------------------------------------------
        allowed = set(adapter_profile.supported_param_names or [])
        if allowed:
            filtered: Dict[str, Any] = {}
            for key, val in result.items():
                if key in allowed:
                    filtered[key] = val
                else:
                    diag["dropped"].append(
                        {
                            "field": key,
                            "reason": "not_in_adapter_allowlist",
                            "detail": (
                                "Adapter declares supported_param_names "
                                "but does not include this key. Dropped "
                                "to avoid rejection by the provider."
                            ),
                        }
                    )
            result = filtered

        return result, diag

    @staticmethod
    def _max_output_tokens_key(api_surface: str) -> str:
        """
        Return the surface-specific name for the max output tokens
        parameter.
        """
        if api_surface == "responses":
            return "max_output_tokens"
        if api_surface == "generate_content":
            return "max_output_tokens"
        # Chat Completions, Anthropic, Ollama OpenAI-compat,
        # LM Studio OpenAI-compat all use ``max_tokens``.
        return "max_tokens"

    @staticmethod
    def _surface_forces_deterministic_sampling(
        api_surface: str,
        reasoning: ReasoningConfig,
        resolved_caps: ResolvedCapabilities,
    ) -> bool:
        """
        Return ``True`` when reasoning being active on this surface
        forces deterministic sampling (temperature must be 1.0 or
        absent).

        This is a HARD provider rule, not a suggestion:

        * OpenAI Responses / Chat Completions with o1/o3 series →
          temperature must be 1.0. We drop it entirely.
        * Anthropic ``extended thinking`` (mode TOGGLE with
          ``budget_tokens``) → temperature must be 1.0. We drop it.
        * Google GenAI → no restriction.
        * Ollama → no restriction.
        * LM Studio → no restriction.

        The check is surface-based, not provider-name-based, so a
        custom OpenAI-compat endpoint proxying an o1 model is handled
        correctly.
        """
        if not resolved_caps.should_attempt_reasoning():
            return False

        # The reasoning modes we honor at compile time.
        mode = resolved_caps.effective_reasoning_mode
        if mode is None:
            return False
        mode_value = mode.value if hasattr(mode, "value") else str(mode)

        if api_surface in ("chat_completions", "responses"):
            # OpenAI's reasoning surfaces.
            return mode_value in ("effort", "toggle", "budget")

        if api_surface == "messages":
            # Anthropic Messages API. Extended thinking forces
            # temperature=1.0; adaptive thinking does NOT.
            return mode_value in ("toggle", "budget")

        return False

    # ------------------------------------------------------------------
    # Helpers — reasoning
    # ------------------------------------------------------------------

    def _compile_reasoning(
        self,
        reasoning: ReasoningConfig,
        resolved_caps: ResolvedCapabilities,
        adapter_profile: AdapterProfile,
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        """
        Compile the reasoning block for the winning strategy.

        Returns ``(reasoning_dict_or_None, diagnostics)``.

        When reasoning is not enabled at the effective level, returns
        ``(None, ...)`` and records why.
        """
        diag: Dict[str, List[Dict[str, Any]]] = {
            "dropped": [],
            "adjusted": [],
            "included": [],
            "conflicts": [],
        }

        if not resolved_caps.should_attempt_reasoning():
            if reasoning.enabled:
                diag["dropped"].append(
                    {
                        "field": "reasoning",
                        "reason": "effective_decision_disabled",
                        "detail": resolved_caps.decisions.get(
                            "reasoning", type("", (), {"reason": ""})()
                        ).reason
                        if resolved_caps.decisions.get("reasoning")
                        else "Reasoning was requested but not granted by "
                             "the capability intersection.",
                    }
                )
            return None, diag

        # Determine the winning strategy.
        strategies = list(adapter_profile.reasoning_strategies or [])
        if not strategies:
            diag["dropped"].append(
                {
                    "field": "reasoning",
                    "reason": "no_strategy_declared",
                    "detail": (
                        "Adapter profile declares no reasoning_strategies. "
                        "Reasoning parameters cannot be compiled."
                    ),
                }
            )
            return None, diag

        # Compile for the first (highest-priority) strategy. If that
        # strategy cannot express the model's mode, try the next.
        winning_strategy: Optional[ReasoningStrategy] = None
        compiled: Optional[Dict[str, Any]] = None

        for strategy in strategies:
            candidate = self._compile_reasoning_for_strategy(
                strategy, reasoning, resolved_caps
            )
            if candidate is not None:
                winning_strategy = strategy
                compiled = candidate
                break

        if compiled is None or winning_strategy is None:
            diag["dropped"].append(
                {
                    "field": "reasoning",
                    "reason": "no_strategy_can_express_mode",
                    "detail": (
                        "None of the adapter's declared reasoning "
                        "strategies can express the current mode "
                        f"({resolved_caps.effective_reasoning_mode})."
                    ),
                }
            )
            return None, diag

        diag["included"].append(
            {
                "field": "reasoning",
                "strategy": winning_strategy.value,
                "mode": (
                    resolved_caps.effective_reasoning_mode.value
                    if hasattr(resolved_caps.effective_reasoning_mode, "value")
                    else str(resolved_caps.effective_reasoning_mode)
                ),
            }
        )
        return compiled, diag

    @staticmethod
    def _compile_reasoning_for_strategy(
        strategy: ReasoningStrategy,
        reasoning: ReasoningConfig,
        resolved_caps: ResolvedCapabilities,
    ) -> Optional[Dict[str, Any]]:
        """
        Emit the reasoning block for a specific strategy.

        Returns ``None`` when the strategy cannot express the current
        mode. Returns the dict shape that the adapter expects.

        Shapes
        ------
        OPENAI_RESPONSES:
            ``{"effort": "high"}``

        OPENAI_CHAT_COMPLETIONS / OPENAI_COMPATIBLE:
            ``{"reasoning_effort": "high"}``
            (adapter merges this into ``extra_body``)

        ANTHROPIC_MESSAGES:
            When mode == adaptive: ``{"type": "adaptive"}``
            When mode == toggle/budget:
                ``{"type": "enabled", "budget_tokens": N}``
            When mode == effort:
                ``{"output_config": {"effort": "high"}}``
                (Anthropic's effort surface is a separate field)

        GOOGLE_GENAI:
            ``{"thinking_budget": N}`` or
            ``{"thinking_level": "high"}`` when the model uses levels.

        OLLAMA_NATIVE:
            ``{"think": true}`` or ``{"think": "high"}``

        LMSTUDIO_NATIVE:
            ``{"reasoning_effort": "high"}`` (forwarded verbatim)
        """
        mode = resolved_caps.effective_reasoning_mode
        mode_value = mode.value if hasattr(mode, "value") else str(mode)
        effort = reasoning.effort
        budget = reasoning.budget_tokens

        # ------------------------------------------------------------------
        # OpenAI Responses.
        # ------------------------------------------------------------------
        if strategy == ReasoningStrategy.OPENAI_RESPONSES:
            if mode_value == "effort" and effort:
                return {"effort": effort}
            if mode_value in ("toggle", "budget"):
                # Responses API accepts ``effort`` or ``summary`` for
                # reasoning. Default to "medium" when only a toggle was
                # requested.
                return {"effort": "medium"}
            return None

        # ------------------------------------------------------------------
        # OpenAI Chat Completions / OpenAI-compatible.
        # ------------------------------------------------------------------
        if strategy in (
            ReasoningStrategy.OPENAI_CHAT_COMPLETIONS,
            ReasoningStrategy.OPENAI_COMPATIBLE,
        ):
            if mode_value == "effort" and effort:
                return {"reasoning_effort": effort}
            if mode_value in ("toggle", "budget"):
                return {"reasoning_effort": "medium"}
            return None

        # ------------------------------------------------------------------
        # Anthropic Messages.
        # ------------------------------------------------------------------
        if strategy == ReasoningStrategy.ANTHROPIC_MESSAGES:
            if mode_value == "adaptive":
                return {"type": "adaptive"}
            if mode_value in ("toggle", "budget"):
                # Anthropic's extended thinking requires a token budget.
                # Default to 4000 when the user only provided a toggle.
                return {
                    "type": "enabled",
                    "budget_tokens": budget if budget else 4000,
                }
            if mode_value == "effort" and effort:
                # Anthropic's newer effort surface — separate field.
                return {"output_config": {"effort": effort}}
            return None

        # ------------------------------------------------------------------
        # Google GenAI.
        # ------------------------------------------------------------------
        if strategy == ReasoningStrategy.GOOGLE_GENAI:
            if mode_value == "effort" and effort:
                # Google uses ``thinking_level`` for models that support
                # it, and ``thinking_budget`` for token-budget models.
                # We use ``thinking_budget`` by default; models that
                # require ``thinking_level`` should be handled by the
                # adapter's ``_compile_reasoning`` override.
                return {"thinking_level": effort}
            if mode_value in ("toggle", "budget"):
                return {"thinking_budget": budget if budget else 4000}
            return None

        # ------------------------------------------------------------------
        # Ollama native.
        # ------------------------------------------------------------------
        if strategy == ReasoningStrategy.OLLAMA_NATIVE:
            if mode_value == "toggle":
                return {"think": True}
            if mode_value == "effort" and effort:
                return {"think": effort}
            if mode_value == "budget" and budget:
                return {"think": True}  # Ollama does not accept token budgets
            return None

        # ------------------------------------------------------------------
        # LM Studio native.
        # ------------------------------------------------------------------
        if strategy == ReasoningStrategy.LMSTUDIO_NATIVE:
            if mode_value == "effort" and effort:
                return {"reasoning_effort": effort}
            if mode_value in ("toggle", "budget"):
                return {"reasoning_effort": "medium"}
            return None

        return None

    # ------------------------------------------------------------------
    # Helpers — tools
    # ------------------------------------------------------------------

    def _compile_tools(
        self,
        tools: List[Dict[str, Any]],
        tool_choice: ToolChoiceSpec,
        resolved_caps: ResolvedCapabilities,
        adapter_profile: AdapterProfile,
    ) -> tuple[
        Optional[List[Dict[str, Any]]],
        Optional[Dict[str, Any]],
        Dict[str, List[Dict[str, Any]]],
    ]:
        """
        Sanitize and forward tools only when supported.

        Returns ``(tools_or_None, tool_choice_or_None, diagnostics)``.
        """
        diag: Dict[str, List[Dict[str, Any]]] = {
            "dropped": [],
            "adjusted": [],
            "included": [],
            "conflicts": [],
        }

        if not tools:
            return None, None, diag

        if not resolved_caps.should_attempt_tools():
            diag["dropped"].append(
                {
                    "field": "tools",
                    "reason": "capability_disabled",
                    "detail": resolved_caps.decisions.get(
                        "tool_calling", type("", (), {"reason": ""})()
                    ).reason
                    if resolved_caps.decisions.get("tool_calling")
                    else "Tools are not enabled by the capability "
                         "intersection.",
                    "tool_count": len(tools),
                }
            )
            return None, None, diag

        # Sanitize tools to OpenAI-compatible shape (all strategies
        # consume this shape; adapters convert to their SDK's format).
        sanitized = self._sanitize_tools(tools)
        if not sanitized:
            diag["dropped"].append(
                {
                    "field": "tools",
                    "reason": "all_tools_malformed",
                    "detail": "No tool passed schema validation.",
                }
            )
            return None, None, diag

        diag["included"].append(
            {"field": "tools", "count": len(sanitized)}
        )

        # ------------------------------------------------------------------
        # Compile tool_choice.
        # ------------------------------------------------------------------
        # [FIX-TOOL-CHOICE-SPEC] Per the OpenAI Chat Completions spec,
        # ``tool_choice`` MUST be a plain string for the three generic
        # modes (``"none"`` / ``"auto"`` / ``"required"``). Only the
        # "specific function" variant uses an object:
        #   ``{"type": "function", "function": {"name": "..."}}``
        #
        # The previous implementation emitted ``{"type": "auto"}``,
        # ``{"type": "none"}``, and ``{"type": "required"}`` — none of
        # which are valid per the spec. Strict servers (LM Studio's
        # ``/api/v0``, some OpenAI-compat proxies) rejected the request
        # with HTTP 400 — verified manually against
        # ``http://100.64.19.102:1235/api/v0/chat/completions``:
        #     {"tool_choice": {"type": "auto"}}  -> HTTP 400
        #     {"tool_choice": "auto"}            -> HTTP 200
        #
        # That 400 then triggered the "aggressive compaction" retry in
        # ``tool_call_agent.py``, which (due to a separate bug fixed
        # there) duplicated messages and failed again — producing the
        # exact "two requests in a row, second one larger" pattern
        # observed in the LM Studio server logs at 22:08:40.
        # [FIX-TOOL-CHOICE-PER-ADAPTER] Emit the legacy OpenAI-shaped
        # dict form here. The raw wire format differs per provider:
        #
        #   * LM Studio (llama.cpp) and OpenAI SDK reject the dict form
        #     for generic modes ({"type": "auto"} → HTTP 400). They
        #     want the plain string ("auto").
        #
        #   * Provider Packages shipped as ``native`` transport (GigaChat,
        #     etc.) parse ``compiled.tool_choice`` with
        #     ``tool_choice.get("type")`` internally. A plain string
        #     raises ``'str' object has no attribute 'get'`` inside the
        #     worker, which then surfaces as a PROVIDER_ERROR.
        #
        #   * Anthropic and Google adapters already convert this dict
        #     into their own wire vocabulary via ``_convert_tool_choice``.
        #
        # The correct fix is to keep ``compiled.tool_choice`` in the
        # dict form (backward-compatible for every plugin adapter that
        # was written against the pre-fix contract) and let each adapter
        # that needs the string form translate it locally:
        #
        #   * ``lmstudio_native.py::_build_body`` — dict → string
        #   * ``openai_native.py::_build_chat_completions_kwargs`` —
        #     dict → string (OpenAI SDK rejects the legacy dict shape)
        #
        # Both adapters accept BOTH forms so the translation is
        # idempotent and defensive: if a future caller passes the string
        # form directly, it is forwarded unchanged.
        compiled_choice: Optional[Dict[str, Any]] = None
        if tool_choice.mode == "auto":
            compiled_choice = {"type": "auto"}
        elif tool_choice.mode == "none":
            compiled_choice = {"type": "none"}
        elif tool_choice.mode == "required":
            if resolved_caps.should_attempt_forced_tool_choice():
                compiled_choice = {"type": "required"}
            else:
                diag["adjusted"].append(
                    {
                        "field": "tool_choice",
                        "action": "downgraded_to_auto",
                        "reason": "forced_choice_not_supported",
                    }
                )
                compiled_choice = {"type": "auto"}
        elif tool_choice.mode == "specific" and tool_choice.name:
            if resolved_caps.should_attempt_forced_tool_choice():
                compiled_choice = {
                    "type": "function",
                    "function": {"name": tool_choice.name},
                }
            else:
                diag["adjusted"].append(
                    {
                        "field": "tool_choice",
                        "action": "downgraded_to_auto",
                        "reason": "forced_choice_not_supported",
                    }
                )
                compiled_choice = {"type": "auto"}

        return sanitized, compiled_choice, diag

    @staticmethod
    def _sanitize_tools(
        tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Normalize arbitrary tool definitions to OpenAI-compatible shape.

        Every output tool has:
            ``{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}``

        where ``parameters`` is always ``{"type": "object", "properties": {...}, "required": [...]}``.
        """
        result: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if "function" in tool else tool
            if not isinstance(fn, dict):
                continue
            name = fn.get("name") or tool.get("name")
            if not name:
                continue
            description = (
                fn.get("description") or tool.get("description") or ""
            )
            parameters = fn.get("parameters") or tool.get("parameters") or {}

            # Normalize parameters to a valid JSON schema.
            clean_params: Dict[str, Any] = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            if isinstance(parameters, dict):
                if isinstance(parameters.get("properties"), dict):
                    clean_params["properties"] = parameters["properties"]
                clean_params["required"] = list(
                    parameters.get("required") or []
                )

            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(name),
                        "description": str(description),
                        "parameters": clean_params,
                    },
                }
            )
        return result

    # ------------------------------------------------------------------
    # Helpers — response format
    # ------------------------------------------------------------------

    def _compile_response_format(
        self,
        response_format: ResponseFormat,
        resolved_caps: ResolvedCapabilities,
    ) -> tuple[Optional[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        """
        Emit the response_format block or ``None``.

        When unsupported, records a diagnostic.
        """
        diag: Dict[str, List[Dict[str, Any]]] = {
            "dropped": [],
            "adjusted": [],
            "included": [],
            "conflicts": [],
        }

        if response_format.kind == "text":
            return None, diag

        if not resolved_caps.should_attempt_structured_output():
            diag["dropped"].append(
                {
                    "field": "response_format",
                    "reason": "structured_output_not_supported",
                    "detail": (
                        f"Model/transport cannot enforce response_format="
                        f"{response_format.kind}. Dropping it."
                    ),
                }
            )
            return None, diag

        if response_format.kind == "json_object":
            diag["included"].append({"field": "response_format.json_object"})
            return {"type": "json_object"}, diag

        if response_format.kind == "json_schema":
            if (
                resolved_caps.effective_structured_output_mode
                != StructuredOutputMode.JSON_SCHEMA
            ):
                diag["adjusted"].append(
                    {
                        "field": "response_format",
                        "action": "downgraded_to_json_object",
                        "reason": "json_schema_not_supported",
                    }
                )
                return {"type": "json_object"}, diag
            schema_block: Dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_format.name or "response",
                    "schema": response_format.schema_
                    or {"type": "object"},
                    "strict": bool(response_format.strict),
                },
            }
            diag["included"].append({"field": "response_format.json_schema"})
            return schema_block, diag

        return None, diag


# ---------------------------------------------------------------------------
# Convenience module-level helper
# ---------------------------------------------------------------------------


_default_compiler = RequestCompiler()


def compile_request(
    request: LLMRequest,
    adapter_profile: AdapterProfile,
    resolved_caps: Optional[ResolvedCapabilities] = None,
) -> CompiledRequest:
    """
    Convenience wrapper around ``RequestCompiler().compile()``.

    Uses the module-level singleton compiler — safe because the class
    holds no mutable state.
    """
    return _default_compiler.compile(request, adapter_profile, resolved_caps)


__all__ = [
    "CompiledRequest",
    "RequestCompiler",
    "compile_request",
]
