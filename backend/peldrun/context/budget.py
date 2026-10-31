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

    def fit_all(
        self,
        system_msgs: List[Dict[str, Any]],
        summary_msgs: List[Dict[str, Any]],
        recent_msgs: List[Dict[str, Any]],
        exec_msgs: List[Dict[str, Any]],
        current_msgs: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        Assemble and fit all sections according to dynamic mathematical bounds.
        Returns the combined message list along with telemetry token usage.
        """
        fitted_sys = self.fit_system_section(system_msgs)
        fitted_sum = self.fit_summary_section(summary_msgs)
        fitted_rec = self.fit_recent_conversation(recent_msgs)
        fitted_exe = self.fit_execution_section(exec_msgs)

        # Current user message is mandatory and takes top priority
        fitted_cur = current_msgs

        combined = fitted_sys + fitted_sum + fitted_rec + fitted_exe + fitted_cur
        total_tokens = count_messages_total(combined)

        # Global safety fallback: if total still exceeds effective limit, trim recent further
        if total_tokens > self.effective_limit:
            overflow = total_tokens - self.effective_limit
            reduced_quota = max(50, self.sections.recent - overflow)
            self.sections.recent = reduced_quota
            fitted_rec = self.fit_recent_conversation(fitted_rec)
            combined = fitted_sys + fitted_sum + fitted_rec + fitted_exe + fitted_cur
            total_tokens = count_messages_total(combined)

        usage_breakdown = {
            "system_tokens": count_messages_total(fitted_sys),
            "summary_tokens": count_messages_total(fitted_sum),
            "recent_tokens": count_messages_total(fitted_rec),
            "execution_tokens": count_messages_total(fitted_exe),
            "current_tokens": count_messages_total(fitted_cur),
            "total_tokens": total_tokens,
            "effective_limit": self.effective_limit,
        }

        return combined, usage_breakdown
