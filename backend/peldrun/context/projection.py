# Path: backend/peldrun/context/projection.py
"""
PELDRUN Core Context Projection.

Centralizes the "what does the LLM actually see this turn?" question.
Reads ``ExecutionState.messages`` (the durable audit trail), applies
budget-aware compaction, enforces the system-prompt invariant, and
returns a NEW list of LLM-ready dicts. NEVER mutates the source state.

This module exists to fix B7 in the PELDRUN execution repair plan:

  ``ToolCallAgent.think()`` had an inline overflow path that built a
  local ``compact_msgs`` list, retried the LLM call with it, and then
  DISCARDED it. On the next ``think()`` call the full (uncompacted)
  history was reloaded from ``self.messages`` and the same overflow
  recurred. Every step paid the 400-error + compact-retry penalty;
  long runs never converged on a stable, budget-fitting view.

Design invariants
-----------------

1. **Projection, not mutation.**
   ``build()`` reads from the state and returns a NEW list. The
   state's ``messages`` are untouched, so the audit trail, checkpoint
   payloads, and post-mortem tooling always see the full history. The
   projection is the LLM's *view*, not the state's *truth*.

2. **Deterministic and idempotent.**
   Same ``state`` + same ``system_prompt`` → byte-identical output.
   This is what makes B7's fix work: ``think()`` can call
   ``build()`` on every LLM request and get the same compaction
   every time. No "which compacted version did I use last time?"
   bookkeeping is needed.

3. **Unit-aware compaction.**
   Messages are grouped into atomic "units":

       [system]                          (own unit)
       [user]                            (own unit)
       [assistant, tool, tool, ...]      (one batch unit)

   Elision drops whole units only. This guarantees that an assistant
   message's ``tool_calls`` are never separated from their ``tool``
   responses — the exact pairing most provider APIs enforce.

4. **Required units are never elided.**
   The following units are unconditionally preserved:

       * every leading system unit (typically one);
       * the FIRST user unit (the original task);
       * the last N units (``keep_recent_units``);
       * the LAST user unit (the current turn's intent).

   Everything between these anchors may be elided when over budget.

5. **Marker injected into the system prompt.**
   When compaction occurs, a marker text is appended to the system
   prompt before the final ``ensure_system_prompt`` step. This
   guarantees the model knows "some history was elided" without
   producing illegal mid-conversation system messages (which several
   providers reject). Models that "forget" earlier context are
   prompted by the marker to re-fetch via tools when needed.

6. **System prompt invariant enforced at the boundary.**
   The final output always routes through
   ``peldrun.agents.message_guard.ensure_system_prompt`` — so even if
   the compacted list somehow ends up system-less or has a displaced
   system message, the invariant is restored before the LLM sees it.

7. **Budget resolution is layered.**
   Precedence (highest → lowest):

       1. ``budget_override`` argument
       2. ``ProjectionConfig.max_total_tokens_override``
       3. ``state.metadata["context_budget"]["effective_limit"]``
          (populated by ``CoreRuntimeExecutor`` from
          ``ContextManager.build_messages``)
       4. ``fallback_max_tokens - fallback_max_output_tokens``

   The fallback exists so a projection built for a non-runtime caller
   (a unit test, a diagnostic tool) still produces sensible output.

8. **No external imports at module load.**
   Only ``peldrun.agents.message_guard`` is imported (a leaf module
   with stdlib-only dependencies). The ``ExecutionState`` type is
   read duck-typed, so this module can be imported before
   ``peldrun.engine.state`` if the load order happens to be unusual.

See also
--------

    * ``backend/peldrun/agents/message_guard.py``     — the system-prompt
                                                          invariant that the
                                                          final step enforces
    * ``backend/peldrun/agents/tool_call_agent.py``   — Phase D caller;
                                                          ``think()`` will use
                                                          ``rebuild_for_llm``
    * ``backend/peldrun/context/builder.py``          — the OLDER
                                                          ContextManager (Phase
                                                          D will also route
                                                          its output through
                                                          this projection)
    * ``backend/peldrun/runtime/executor.py``         — populates
                                                          ``state.metadata``
                                                          with the budget
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from peldrun.agents.message_guard import ensure_system_prompt

logger = logging.getLogger("peldrun.context.projection")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEM_ROLE: str = "system"
USER_ROLE: str = "user"
ASSISTANT_ROLE: str = "assistant"
TOOL_ROLE: str = "tool"

#: Characters-per-token heuristic. Matches the estimator used by
#: ``peldrun.context.sidecar.estimate_string_tokens`` so projections
#: and sidecar-derived budgets stay consistent.
CHARS_PER_TOKEN: int = 4

#: Conservative per-image token estimate. Real provider tokenizers vary
#: (OpenAI's "low" detail ≈ 85 tokens, "high" ≈ 765-1105), but the
#: number of images in a run is small and being conservative is
#: preferable to under-budgeting and hitting a 400.
IMAGE_TOKEN_ESTIMATE: int = 1000


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ProjectionConfig:
    """
    Tuning knobs for ``ContextProjection``. All fields have sensible
    defaults; callers normally supply nothing.

    Attributes
    ----------
    fallback_max_tokens
        Context-window ceiling used when no runtime budget is
        available. Ignored when ``state.metadata["context_budget"]``
        carries an ``effective_limit``.
    fallback_max_output_tokens
        Reserved-output ceiling for the fallback budget. The effective
        input budget is ``fallback_max_tokens -
        fallback_max_output_tokens``.
    keep_recent_units
        Number of trailing units (assistant+tool batches, user
        messages) always preserved verbatim. Six covers the typical
        "recent reasoning horizon" without bloating the tail.
    keep_first_user
        When True, the first user message (the original task prompt)
        is ALWAYS preserved, regardless of how far back it is.
    keep_last_user
        When True, the last user message is ALWAYS preserved — even
        if the model has since generated many tool-calling steps.
    compaction_threshold_ratio
        Fraction of the budget at which compaction is triggered.
        1.0 = compact only when over budget. 0.9 = compact proactively
        at 90% to leave headroom for the next message.
    max_total_tokens_override
        Hard cap that overrides both the runtime budget and the
        fallback. Used by tests and by callers who know the target
        provider's true limit better than the metadata does.
    compaction_marker_template
        Format string for the notice appended to the system prompt
        when compaction occurs. Must contain ``{count}``.
    """

    fallback_max_tokens: int = 8192
    fallback_max_output_tokens: int = 2048
    keep_recent_units: int = 6
    keep_first_user: bool = True
    keep_last_user: bool = True
    compaction_threshold_ratio: float = 1.0
    max_total_tokens_override: Optional[int] = None
    compaction_marker_template: str = (
        "[Context notice: {count} earlier messages were elided to fit "
        "the model's budget. The original task, the current turn, and "
        "the most recent steps are preserved verbatim. If you need "
        "details from earlier steps, re-fetch them via tools instead "
        "of guessing.]"
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass
class ProjectionStats:
    """
    Diagnostics emitted alongside every projection.

    Safe to embed in log lines, SSE payloads, and diagnostics. Never
    contains message content — only counts and boolean flags — so it
    cannot leak user data.
    """

    original_messages: int = 0
    projected_messages: int = 0
    original_tokens: int = 0
    projected_tokens: int = 0
    was_compacted: bool = False
    elided_message_count: int = 0
    elided_unit_count: int = 0
    budget: Optional[int] = None
    system_prompt_present: bool = False
    system_prompt_changed: bool = False

    def summary(self) -> str:
        """Compact one-line summary for log lines."""
        if not self.was_compacted:
            return (
                f"no-compaction "
                f"({self.projected_messages} msgs, "
                f"~{self.projected_tokens} tokens, "
                f"budget={self.budget})"
            )
        return (
            f"compacted "
            f"({self.original_messages}→{self.projected_messages} msgs, "
            f"~{self.original_tokens}→~{self.projected_tokens} tokens, "
            f"elided={self.elided_message_count}, "
            f"budget={self.budget})"
        )


@dataclass
class ProjectionResult:
    """Return value of ``ContextProjection.build``."""

    messages: List[Dict[str, Any]] = field(default_factory=list)
    stats: ProjectionStats = field(default_factory=ProjectionStats)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _estimate_message_tokens(message: Dict[str, Any]) -> int:
    """
    Estimate the token cost of a single message.

    Text-only and string-content messages are estimated via the
    ``len // 4`` heuristic. Multimodal content lists are summed:
    text parts contribute ``len(text) // 4``, image parts contribute a
    fixed ``IMAGE_TOKEN_ESTIMATE``.

    Empty content (assistant message with only ``tool_calls``)
    contributes zero — the tool_call payload itself is not counted
    here because its size is bounded by the schema and rarely large
    enough to matter. Tool RESULTS are counted fully via their
    ``content`` field.
    """
    content = message.get("content")
    if content is None:
        return 0
    if isinstance(content, str):
        return max(1, len(content) // CHARS_PER_TOKEN)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text = part.get("text", "")
                if text:
                    total += max(1, len(text) // CHARS_PER_TOKEN)
            elif ptype == "image_url":
                total += IMAGE_TOKEN_ESTIMATE
        return total
    return max(1, len(str(content)) // CHARS_PER_TOKEN)


def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Sum of per-message token estimates."""
    return sum(_estimate_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# Unit grouping
# ---------------------------------------------------------------------------


def _group_into_units(
    messages: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """
    Group a flat message list into atomic units.

    Grouping rule:

      * ``system`` / ``user`` / ``assistant`` START a new unit.
      * ``tool`` and any unrecognized role APPEND to the current
        unit (or start one when there is no current unit).

    This guarantees that an assistant message and its tool responses
    are always in the SAME unit. Eliding a unit therefore elides the
    whole batch atomically — no provider will see an assistant
    ``tool_calls`` without a matching ``tool`` response.
    """
    units: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role in (SYSTEM_ROLE, USER_ROLE, ASSISTANT_ROLE):
            if current:
                units.append(current)
            current = [msg]
        else:
            if current:
                current.append(msg)
            else:
                current = [msg]
    if current:
        units.append(current)
    return units


def _compute_preserved_indices(
    units: List[List[Dict[str, Any]]],
    config: ProjectionConfig,
) -> set:
    """
    Return the set of unit indices that MUST be preserved.

    Order of evaluation (highest priority first, but all are
    accumulated into the same set):

      1. Leading system units (indices 0..K where every unit's first
         message is ``system``).
      2. First user unit (when ``config.keep_first_user``).
      3. Last ``config.keep_recent_units`` units.
      4. Last user unit (when ``config.keep_last_user``).
    """
    preserved: set = set()
    n = len(units)

    # 1. Leading system units.
    for i in range(n):
        if units[i][0].get("role") == SYSTEM_ROLE:
            preserved.add(i)
        else:
            break

    # 2. First user unit.
    if config.keep_first_user:
        for i in range(n):
            if i in preserved:
                continue
            if units[i][0].get("role") == USER_ROLE:
                preserved.add(i)
                break

    # 3. Last N units.
    tail_start = max(0, n - config.keep_recent_units)
    for i in range(tail_start, n):
        preserved.add(i)

    # 4. Last user unit.
    if config.keep_last_user:
        for i in range(n - 1, -1, -1):
            if units[i][0].get("role") == USER_ROLE:
                preserved.add(i)
                break

    return preserved


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def _compact(
    messages: List[Dict[str, Any]],
    config: ProjectionConfig,
) -> "tuple[List[Dict[str, Any]], int, int]":
    """
    Return ``(compacted_messages, elided_message_count, elided_unit_count)``.

    When nothing needs to be elided (``preserved == all units``),
    returns the input list unchanged (same list object — callers may
    treat it as read-only).

    The returned list never contains any marker message. The caller
    injects the marker into the system prompt instead (see
    ``ContextProjection._project``). This keeps the compacted message
    list a valid, provider-legal sequence at every stage.
    """
    units = _group_into_units(messages)
    preserved = _compute_preserved_indices(units, config)

    if len(preserved) >= len(units):
        # Nothing to elide — return input untouched.
        return list(messages), 0, 0

    result: List[Dict[str, Any]] = []
    elided_messages = 0
    elided_units = 0
    for i, unit in enumerate(units):
        if i in preserved:
            result.extend(unit)
        else:
            elided_messages += len(unit)
            elided_units += 1

    return result, elided_messages, elided_units


# ---------------------------------------------------------------------------
# System-prompt change detection (for diagnostics only)
# ---------------------------------------------------------------------------


def _has_matching_system_prompt(
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str],
) -> bool:
    """
    Return True if the messages already carry a system message that
    matches ``system_prompt`` (after stripping).

    Used only to populate the ``system_prompt_changed`` stat — this
    result does not affect the projection logic itself (which always
    routes through ``ensure_system_prompt``).

    Semantics:

      * ``system_prompt is None`` → ``True`` (caller has no
        expectation; nothing "changed").
      * No system message present → ``False``.
      * First system message present and matches → ``True``.
      * First system message present and differs → ``False``.
    """
    if system_prompt is None:
        return True
    target = str(system_prompt).strip()
    for msg in messages:
        if msg.get("role") == SYSTEM_ROLE:
            content = msg.get("content")
            if isinstance(content, str) and content.strip() == target:
                return True
            return False
    return False


# ---------------------------------------------------------------------------
# The projection engine
# ---------------------------------------------------------------------------


class ContextProjection:
    """
    Builds the LLM-facing message list from an execution state.

    Instances are stateless after construction: every ``build`` call
    uses the same immutable ``ProjectionConfig`` and produces output
    solely from its arguments. Safe for concurrent use.
    """

    def __init__(self, config: Optional[ProjectionConfig] = None) -> None:
        self._config = config or ProjectionConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        state: Any,
        system_prompt: Optional[str] = None,
        *,
        budget_override: Optional[int] = None,
        config_override: Optional[ProjectionConfig] = None,
    ) -> ProjectionResult:
        """
        Build the LLM-facing message list from ``state.messages``.

        Parameters
        ----------
        state
            An ``ExecutionState`` (or any object with a
            ``messages: List[ChatMessage]`` attribute). ``None`` is
            tolerated and returns an empty projection —
            ``ToolCallAgent`` calls this defensively before the
            initial state is set.
        system_prompt
            The authoritative system directive. When non-empty it
            OVERWRITES any existing system message. When empty the
            state's existing system message (if any) is preserved.
        budget_override
            Per-call budget ceiling. Beats both the runtime metadata
            and the config-level override.
        config_override
            Per-call ``ProjectionConfig``. The most aggressive
            retry path in ``ToolCallAgent.think()`` uses this to
            request "even more compaction" without reinstantiating
            the projection.
        """
        raw_messages = self._extract_messages(state)
        return self._project(
            raw_messages=raw_messages,
            system_prompt=system_prompt,
            budget_override=budget_override,
            config_override=config_override,
            state=state,
        )

    def build_from_messages(
        self,
        messages: List[Any],
        system_prompt: Optional[str] = None,
        *,
        budget_override: Optional[int] = None,
        config_override: Optional[ProjectionConfig] = None,
    ) -> ProjectionResult:
        """
        Same as ``build`` but takes a raw message list instead of an
        ``ExecutionState``. Accepts a mix of dicts and ``ChatMessage``
        instances. Chat-mode callers and unit tests use this path.
        """
        raw_messages = [self._to_dict(m) for m in (messages or [])]
        return self._project(
            raw_messages=raw_messages,
            system_prompt=system_prompt,
            budget_override=budget_override,
            config_override=config_override,
            state=None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(message: Any) -> Dict[str, Any]:
        """
        Coerce a message-shaped object into a plain dict.

        Handles ``dict`` (pass-through) and ``ChatMessage`` (via
        ``to_llm_dict``). Anything else degrades to a user message
        with the object stringified — preserving the "never crash on
        malformed input" contract.
        """
        if isinstance(message, dict):
            return message
        if hasattr(message, "to_llm_dict") and callable(message.to_llm_dict):
            return message.to_llm_dict()
        return {"role": USER_ROLE, "content": str(message)}

    def _extract_messages(self, state: Any) -> List[Dict[str, Any]]:
        """Pull the message list out of ``state`` and coerce to dicts."""
        if state is None:
            return []
        raw = getattr(state, "messages", None)
        if raw is None:
            return []
        return [self._to_dict(m) for m in raw]

    def _resolve_budget(
        self,
        state: Any,
        override: Optional[int],
        config: ProjectionConfig,
    ) -> Optional[int]:
        """
        Resolve the effective token ceiling.

        Precedence documented on the module docstring. Returns ``None``
        when no budget can be resolved AND the config explicitly
        disables the fallback (``fallback_max_tokens <= 0``).
        """
        if override is not None:
            try:
                val = int(override)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass

        if config.max_total_tokens_override is not None:
            try:
                val = int(config.max_total_tokens_override)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass

        if state is not None:
            metadata = getattr(state, "metadata", None)
            if isinstance(metadata, dict):
                ctx_budget = metadata.get("context_budget")
                if isinstance(ctx_budget, dict):
                    limit = ctx_budget.get("effective_limit")
                    if isinstance(limit, int) and limit > 0:
                        return limit

        if config.fallback_max_tokens > 0:
            return max(
                100,
                config.fallback_max_tokens - config.fallback_max_output_tokens,
            )

        return None

    def _project(
        self,
        *,
        raw_messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        budget_override: Optional[int],
        config_override: Optional[ProjectionConfig],
        state: Any,
    ) -> ProjectionResult:
        """The shared implementation behind ``build`` and ``build_from_messages``."""
        config = config_override or self._config
        budget = self._resolve_budget(state, budget_override, config)

        original_tokens = _estimate_tokens(raw_messages)
        system_changed = not _has_matching_system_prompt(
            raw_messages, system_prompt
        )

        # Decide whether compaction is required.
        needs_compaction = (
            budget is not None
            and original_tokens > budget * config.compaction_threshold_ratio
        )

        elided_messages = 0
        elided_units = 0
        was_compacted = False

        if needs_compaction:
            compacted, elided_messages, elided_units = _compact(
                raw_messages, config
            )
            was_compacted = elided_messages > 0
        else:
            compacted = list(raw_messages)

        # Augment the system prompt with the elision marker. We do NOT
        # insert a mid-conversation system message — that would be
        # rejected by several providers (Anthropic, Google) and would
        # be silently dropped by ``ensure_system_prompt`` anyway.
        effective_system = system_prompt
        if was_compacted and elided_messages > 0:
            marker = config.compaction_marker_template.format(
                count=elided_messages
            )
            if effective_system and str(effective_system).strip():
                effective_system = f"{effective_system}\n\n{marker}"
            else:
                effective_system = marker

        # Final invariant: exactly one system message at index 0.
        final_messages = ensure_system_prompt(compacted, effective_system)

        projected_tokens = _estimate_tokens(final_messages)

        if budget is not None and projected_tokens > budget:
            # This should be rare — the compaction drops whole units,
            # and if we still don't fit, there is no further reduction
            # available without losing required context. Log and let
            # the LLM call either succeed (provider accepts our
            # estimate was pessimistic) or fail with a provider-side
            # error that the retry path in ``ToolCallAgent.think()``
            # will handle via a more aggressive config override.
            logger.warning(
                "Projection still over budget after compaction: "
                "%d > %d tokens (elided %d messages across %d units).",
                projected_tokens,
                budget,
                elided_messages,
                elided_units,
            )

        stats = ProjectionStats(
            original_messages=len(raw_messages),
            projected_messages=len(final_messages),
            original_tokens=original_tokens,
            projected_tokens=projected_tokens,
            was_compacted=was_compacted,
            elided_message_count=elided_messages,
            elided_unit_count=elided_units,
            budget=budget,
            system_prompt_present=bool(effective_system),
            system_prompt_changed=system_changed,
        )

        return ProjectionResult(messages=final_messages, stats=stats)


# ---------------------------------------------------------------------------
# Convenience wrapper (what ToolCallAgent.think() will call)
# ---------------------------------------------------------------------------


def rebuild_for_llm(
    state: Any,
    system_prompt: Optional[str] = None,
    *,
    config: Optional[ProjectionConfig] = None,
    budget_override: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return the LLM-ready message list for ``state``.

    Drop-in replacement for the pre-Phase-D

        self.messages = [msg.to_llm_dict() for msg in state.messages]

    pattern (which produced B1) and for the pre-Phase-D

        compact_msgs = self.messages[:2] + self.messages[-3:]

    pattern (which produced B7).

    The result:

      * Always has a single system message at index 0.
      * Is budget-compacted when the state exceeds the model's
        effective context limit.
      * Is deterministic — the same state produces the same output
        on every call, so repeated ``think()`` invocations within a
        step (retries, re-prompts) see a consistent view.
      * Never mutates ``state.messages``.

    Phase D will wire ``ToolCallAgent.think()`` to call this instead
    of building the message list by hand.
    """
    projection = ContextProjection(config)
    return projection.build(
        state,
        system_prompt=system_prompt,
        budget_override=budget_override,
    ).messages


__all__ = [
    # Constants (exported for tests and downstream callers)
    "CHARS_PER_TOKEN",
    "IMAGE_TOKEN_ESTIMATE",
    # Configuration + diagnostics
    "ProjectionConfig",
    "ProjectionResult",
    "ProjectionStats",
    # Engine
    "ContextProjection",
    # Convenience entry point
    "rebuild_for_llm",
]
