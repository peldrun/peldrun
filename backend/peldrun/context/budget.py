"""
Token Budgeting and Section Fitting Engine for peldrun-core.

Implements mathematical policy constraints derived from per-model user configurations.
Enforces strict token quotas across System, Summary, Recent, Execution, and Current slices.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from peldrun.context.schema import BudgetMetadata, BudgetSections, ConversationMessage


def count_tokens_heuristic(text: str) -> int:
    """
    Fast character-heuristic token counter (~4 characters per token).
    Used as an ultra-fast fallback when tokenizers are unavailable.
    """
    if not text:
        return 0
    clean = text.strip()
    return max(1, len(clean) // 4)


def _count_content_parts_tokens(content: List[Any]) -> int:
    """
    Count tokens across a multimodal ``content`` list.

    Tries to delegate to the canonical parts-aware counter in
    ``peldrun.llm.tokenizer`` so that both subsystems produce identical
    numbers for the same message. Falls back to an inline heuristic when
    the tokenizer module is unavailable (e.g. text-only deployments
    where the ``openai`` package is not installed and
    ``peldrun.llm.__init__`` cannot be imported).

    Fallback constants mirror the tokenizer module so drift is
    bounded: 500 tokens per image (the ``auto`` estimate) plus the
    local text heuristic for text parts.
    """
    try:
        from peldrun.llm.tokenizer import count_content_parts_tokens

        return count_content_parts_tokens(content)
    except Exception:
        # Inline fallback — never let a tokenizer import failure break
        # budgeting. Numbers stay within the same order of magnitude as
        # the tokenizer module's output.
        total = 0
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text":
                    total += count_tokens_heuristic(
                        part.get("text") or ""
                    )
                elif ptype == "image_url":
                    total += 500
            else:
                ptype = getattr(part, "type", None)
                if ptype == "text":
                    total += count_tokens_heuristic(
                        getattr(part, "text", "") or ""
                    )
                elif ptype == "image_url":
                    total += 500
        return total


def count_message_tokens(message: Dict[str, Any]) -> int:
    """
    Calculate the token weight of a single chat message payload.

    Plain text messages use the fast character heuristic unchanged.
    Multimodal messages (``content`` is a list of parts) are dispatched
    to ``_count_content_parts_tokens`` so that image parts contribute
    their estimated baseline instead of being stringified into their
    Python repr — which would otherwise inflate the count by orders of
    magnitude and cause aggressive over-eviction in the sliding window.

    ``tool_calls`` are not counted by this function; when a message
    carries them, its ``content`` is typically empty, so the returned
    baseline is the correct minimum.
    """
    content = message.get("content")
    role = message.get("role") or ""

    if isinstance(content, list):
        content_tokens = _count_content_parts_tokens(content)
    else:
        content_tokens = count_tokens_heuristic(str(content or ""))

    # Standard overhead: 4 tokens for metadata framing + content tokens
    return 4 + content_tokens + count_tokens_heuristic(str(role))


def count_messages_total(messages: List[Dict[str, Any]]) -> int:
    """Aggregate total tokens across a list of message dictionaries."""
    return sum(count_message_tokens(m) for m in messages)


class TokenBudgetController:
    """
    Controls dynamic context allocation and trims sections proportionally
    based on the active model's interactive budget settings.
    """

    def __init__(self, budget: BudgetMetadata):
        self.budget = budget
        self.effective_limit = budget.effective_limit
        self.sections = budget.sections

    def fit_system_section(self, system_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensure system prompts fit within the allocated system slice."""
        quota = self.sections.system
        current_tokens = count_messages_total(system_messages)

        if current_tokens <= quota or quota <= 0:
            return system_messages

        # If system prompt exceeds quota, truncate trailing non-essential rules
        fitted: List[Dict[str, Any]] = []
        for msg in system_messages:
            tokens = count_message_tokens(msg)
            if tokens <= quota:
                fitted.append(msg)
                quota -= tokens
            else:
                # Truncate content length safely
                max_chars = quota * 4
                truncated_text = str(msg.get("content", ""))[:max_chars].rstrip() + "\n...[truncated]"
                fitted.append({**msg, "content": truncated_text})
                break
        return fitted

    def fit_summary_section(self, summary_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Bound historical context summary within the allocated summary slice."""
        quota = self.sections.summary
        current_tokens = count_messages_total(summary_messages)

        if current_tokens <= quota or quota <= 0:
            return summary_messages

        fitted: List[Dict[str, Any]] = []
        for msg in summary_messages:
            max_chars = quota * 4
            content = str(msg.get("content", ""))
            if len(content) > max_chars:
                truncated = content[:max_chars].rstrip() + " ...[summary truncated]"
                fitted.append({**msg, "content": truncated})
            else:
                fitted.append(msg)
        return fitted

    def fit_recent_conversation(
        self, recent_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Preserve literal conversation turns, evicting oldest turns first
        if total conversation tokens exceed the recent slice quota.
        """
        quota = self.sections.recent
        if not recent_messages or quota <= 0:
            return []

        # Iterate from newest to oldest to preserve the most recent dialogue
        kept_reversed: List[Dict[str, Any]] = []
        running_tokens = 0

        for msg in reversed(recent_messages):
            msg_tokens = count_message_tokens(msg)
            if running_tokens + msg_tokens <= quota:
                kept_reversed.append(msg)
                running_tokens += msg_tokens
            else:
                # Quota reached; stop including older turns
                break

        # Restore original chronological order
        return list(reversed(kept_reversed))

    def fit_execution_section(
        self, execution_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Fit ongoing turn's tool execution telemetry into the execution slice.
        Truncates verbose command outputs while keeping tool status intact.
        """
        quota = self.sections.execution
        current_tokens = count_messages_total(execution_messages)

        if current_tokens <= quota or quota <= 0:
            return execution_messages

        fitted: List[Dict[str, Any]] = []
        for msg in execution_messages:
            tokens = count_message_tokens(msg)
            if tokens <= quota:
                fitted.append(msg)
                quota -= tokens
            else:
                max_chars = quota * 4
                raw = str(msg.get("content", ""))
                # Compress middle of verbose command output
                if len(raw) > max_chars:
                    half = max(10, (max_chars - 40) // 2)
                    pruned = f"{raw[:half]}\n... [output pruned for budget] ...\n{raw[-half:]}"
                    fitted.append({**msg, "content": pruned})
                else:
                    fitted.append(msg)
                break
        return fitted

    def fit_memory_section(
        self, memory_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """[P1.5] Fit the memory block within its allocated slice quota.

        The memory block is produced upstream by
        ``peldrun.memory.services.context_builder.format_memory_context``
        and is ALREADY bounded by
        ``MemoryTokenBudget.max_tokens`` (P1.4) -- the memory
        pipeline drops entries it cannot fit before this method
        ever runs.

        Consequently, with ``sections.memory == 0`` (the Phase 1
        default -- see ``BudgetRatios``'s docstring for why), this
        method returns the input UNCHANGED. That is not a no-op
        courtesy -- it is the load-bearing invariant: a non-zero
        memory slice would silently truncate the memory block a
        SECOND time, after the memory pipeline already decided what
        survived. That double truncation would (a) discard memory
        entries the memory budget considered fit, and (b) produce a
        MemoryContext whose ``used_tokens`` no longer matches what
        actually reached the model. See the note on
        ``format_memory_context`` in ``peldrun.memory.services``
        for why ``used_tokens`` MUST equal the emitted block size.

        When a future Phase 2 promotes ``sections.memory`` to a
        positive value, the truncation branch (identical in shape to
        ``fit_execution_section``) fires and enforces that stricter
        ceiling. The empty-input fast path always returns [].
        """
        if not memory_messages:
            return []

        quota = self.sections.memory
        current_tokens = count_messages_total(memory_messages)

        # Quota <= 0 means "no dedicated memory slice" (Phase 1).
        # Return the input untouched -- see the docstring for why
        # this is the correct behavior, not a shortcut.
        if current_tokens <= quota or quota <= 0:
            return memory_messages

        # Phase 2 path: a positive memory slice is configured.
        # Truncate the LAST message's content (the memory block is
        # always a single ``system`` message in practice). The loop
        # keeps this total for a hypothetical multi-message input.
        fitted: List[Dict[str, Any]] = []
        for msg in memory_messages:
            tokens = count_message_tokens(msg)
            if tokens <= quota:
                fitted.append(msg)
                quota -= tokens
            else:
                max_chars = quota * 4
                content = str(msg.get("content", ""))
                truncated = (
                    content[:max_chars].rstrip()
                    + "\n...[memory truncated by budget]"
                )
                fitted.append({**msg, "content": truncated})
                break
        return fitted

    def fit_all(
        self,
        system_msgs: List[Dict[str, Any]],
        summary_msgs: List[Dict[str, Any]],
        recent_msgs: List[Dict[str, Any]],
        exec_msgs: List[Dict[str, Any]],
        current_msgs: List[Dict[str, Any]],
        memory_msgs: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """Assemble and fit all sections according to dynamic mathematical bounds.

        [P1.5] The ``memory_msgs`` parameter carries the
        ``[RETRIEVED MEMORY]`` block as a separate list so the fitter
        can (a) apply the optional ``sections.memory`` quota to it in
        isolation, and (b) record its token cost as a distinct
        telemetry field. After fitting, the memory content is MERGED
        into the first system message -- the output therefore
        contains exactly ONE system message for Tier 1 (base + rules
        + GenUI + memory), preserving the small-model compatibility
        invariant documented in ``ContextManager.build_messages``.

        Backward compatibility
        ----------------------
        ``memory_msgs`` is optional and defaults to ``None``. Callers
        that do not pass it (any call site that predates P1.5, or a
        P1.5 call site with no memory this turn) receive IDENTICAL
        behavior to the pre-P1.5 code: no memory content is merged,
        ``usage_breakdown["memory_tokens"]`` is 0, and every other
        field is byte-identical.

        Accounting semantics
        --------------------
        ``usage_breakdown`` reports BOTH:

          * ``system_tokens`` -- PRE-merge count of the base system
            message(s), NOT including memory.
          * ``memory_tokens`` -- PRE-merge count of the memory
            block, NOT including the base system.

        Summing all six sections therefore approximates
        ``total_tokens`` (the only divergence is the small
        ``\\n\\n`` separator the merge inserts, typically 1-2
        tokens -- reported by ``total_tokens`` but not attributable
        to any single section).

        Returns
        -------
        Tuple[List[Dict[str, Any]], Dict[str, int]]
            The combined message list and a breakdown dict containing
            ``system_tokens``, ``summary_tokens``, ``recent_tokens``,
            ``execution_tokens``, ``memory_tokens``,
            ``current_tokens``, ``total_tokens``, and
            ``effective_limit``.
        """
        fitted_sys = self.fit_system_section(system_msgs)
        fitted_sum = self.fit_summary_section(summary_msgs)
        fitted_rec = self.fit_recent_conversation(recent_msgs)
        fitted_exe = self.fit_execution_section(exec_msgs)
        fitted_mem = self.fit_memory_section(memory_msgs or [])

        # [P1.5] Measure PRE-merge token counts for the breakdown so
        # ``system_tokens`` and ``memory_tokens`` are attributable
        # slices rather than overlapping totals.
        system_tokens_pre = count_messages_total(fitted_sys)
        memory_tokens_pre = count_messages_total(fitted_mem)

        # Merge fitted memory into the first system message. This
        # preserves the single-system-message Tier-1 invariant
        # documented in ``ContextManager.build_messages`` -- small
        # models (<=14B) read only the first system message, so
        # emitting memory as a second system message would silently
        # discard it on those targets.
        merged_sys = self._merge_memory_into_system(fitted_sys, fitted_mem)

        # Current user message is mandatory and takes top priority.
        fitted_cur = current_msgs

        combined = merged_sys + fitted_sum + fitted_rec + fitted_exe + fitted_cur
        total_tokens = count_messages_total(combined)

        # Global safety fallback: if the total still exceeds the
        # effective limit, trim recent further. Memory is NOT a trim
        # candidate here -- it is bounded upstream by
        # MemoryTokenBudget.max_tokens, and re-trimming it would
        # invalidate the ``used_tokens`` invariant the memory
        # pipeline depends on.
        if total_tokens > self.effective_limit:
            overflow = total_tokens - self.effective_limit
            reduced_quota = max(50, self.sections.recent - overflow)
            self.sections.recent = reduced_quota
            fitted_rec = self.fit_recent_conversation(fitted_rec)
            combined = (
                merged_sys + fitted_sum + fitted_rec + fitted_exe + fitted_cur
            )
            total_tokens = count_messages_total(combined)

        usage_breakdown = {
            "system_tokens": system_tokens_pre,
            "summary_tokens": count_messages_total(fitted_sum),
            "recent_tokens": count_messages_total(fitted_rec),
            "execution_tokens": count_messages_total(fitted_exe),
            "memory_tokens": memory_tokens_pre,
            "current_tokens": count_messages_total(fitted_cur),
            "total_tokens": total_tokens,
            "effective_limit": self.effective_limit,
        }

        return combined, usage_breakdown

    @staticmethod
    def _merge_memory_into_system(
        system_msgs: List[Dict[str, Any]],
        memory_msgs: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """[P1.5] Return ``system_msgs`` with memory content merged into
        the first system message's content.

        Behavior
        --------
        * When ``memory_msgs`` is empty: returns ``system_msgs``
          UNCHANGED (same object -- no allocation, no copy).
        * When ``memory_msgs`` has content but ``system_msgs`` is
          empty: returns a NEW list containing ONE system message
          whose content is the joined memory content.
        * When both have content: returns a NEW list whose first
          element is a copy of the first system message with its
          content extended by ``"\\n\\n" + <memory content>``. Any
          additional system messages are preserved in place.

        The merge is the ENFORCEMENT POINT for the "one system
        message" invariant at the Tier-1 level. Do not relax it
        without also updating ``ContextManager.build_messages``'s
        compatibility note.
        """
        if not memory_msgs:
            return system_msgs

        # Concatenate every non-empty memory message's content into
        # a single block. In practice ``memory_msgs`` is always
        # exactly one message; the loop keeps the merge total even
        # if a future caller passes several.
        memory_parts: List[str] = []
        for m in memory_msgs:
            c = m.get("content")
            if c is None:
                continue
            s = str(c)
            if s.strip():
                memory_parts.append(s)
        memory_content = "\n\n".join(memory_parts)
        if not memory_content.strip():
            return system_msgs

        if not system_msgs:
            return [{"role": "system", "content": memory_content}]

        first = system_msgs[0]
        first_content = str(first.get("content", "")).rstrip()
        if first_content:
            merged_content = first_content + "\n\n" + memory_content
        else:
            merged_content = memory_content
        merged_first = {**first, "content": merged_content}
        return [merged_first] + system_msgs[1:]
