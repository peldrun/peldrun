# backend/peldrun/agents/multi/context_bridge.py
"""
PELDRUN Multi-Agent Context Bridge.
Constructs bounded, sanitized prompt directives for child specialists.
Prevents context window exhaustion and prompt injection across agent delegation boundaries.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("peldrun.agents.multi.context_bridge")

# Hard upper limits to prevent prompt bloat and token consumption runaways
MAX_CONTEXT_BYTES: int = 64 * 1024  # 64 KB
MAX_INSTRUCTION_CHARS: int = 16_000  # ~4,000 tokens


class ContextBridge:
    """Constructs scoped task prompts and filters contextual data across agent delegation tiers."""

# [P1-15 FIX — Priority-based context retention]
# Priority key fragments (lowercase substring matches). When the
# full context_data exceeds MAX_CONTEXT_BYTES, keys whose name
# contains any of these fragments are retained FIRST, in declaration
# order. Everything else is added afterwards, budget permitting.
#
# The order deliberately mirrors the plan's stated priority:
#     task -> tools -> files -> summary -> other
#
# Substring matching (not exact) is used because callers emit
# variations: ``task``, ``task_description``, ``task_objective``;
# ``tools``, ``allowed_tools``, ``authorized_tools``;
# ``files``, ``file_paths``, ``filenames``; and so on. Matching the
# whole set keeps the fix robust against naming conventions without
# a schema.
_PRIORITY_KEY_PATTERNS: tuple[str, ...] = (
    "task",
    "instruction",
    "objective",
    "goal",
    "tools",
    "allowed_tools",
    "authorized_tools",
    "files",
    "file_paths",
    "paths",
    "filenames",
    "summary",
    "context",
    "background",
)


def _is_priority_key(key: str) -> bool:
    """Return True when ``key`` matches any priority fragment."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(pattern in lowered for pattern in _PRIORITY_KEY_PATTERNS)


class ContextBridge:
    """Constructs scoped task prompts and filters contextual data across agent delegation tiers."""

    @staticmethod
    def format_child_prompt(
        instruction: str,
        context_data: Optional[Dict[str, Any]] = None,
        project_rules: Optional[str] = None,
        delegation_depth: int = 1,
    ) -> str:
        """
        Assemble a structured prompt for a child specialist agent.

        Enforces:
        - Instruction presence and length capping.
        - JSON-encoded context data bounded by byte limits.
        - Explicit isolation instructions preventing child from
          attempting secondary delegation.

        [P1-15 FIX — Priority-based context retention]
        When ``context_data`` exceeds ``MAX_CONTEXT_BYTES``, the
        historical implementation replaced the ENTIRE payload with a
        single ``{"notice": "..."}`` dict. That discarded every
        priority key (task, tools, files, summary) along with the
        low-value bulk the budget was actually meant to exclude —
        the child received NO context at all, and silently produced
        incomplete work.

        The fixed behavior:
          1. Try full serialization. Fits → use it unchanged.
          2. Otherwise → partition keys into priority vs others by
             ``_PRIORITY_KEY_PATTERNS``.
          3. Add priority keys first, in declaration order, until
             the budget is exhausted.
          4. Add other keys next, in their original order.
          5. A ``_context_bridge_notice`` key lists every dropped
             key so the child can ask the parent for a reduced
             payload if needed.
        """
        clean_instruction = (instruction or "").strip()
        if not clean_instruction:
            raise ValueError("Child task instruction must not be empty.")

        if len(clean_instruction) > MAX_INSTRUCTION_CHARS:
            logger.warning(
                "Task instruction exceeds %d chars (%d chars). Truncating instruction.",
                MAX_INSTRUCTION_CHARS,
                len(clean_instruction),
            )
            clean_instruction = (
                clean_instruction[:MAX_INSTRUCTION_CHARS]
                + "\n[TRUNCATED BY CONTEXT BRIDGE]"
            )

        prompt_sections = [
            f"[DELEGATED SUBTASK DIRECTIVE (DEPTH {delegation_depth})]",
            clean_instruction,
        ]

        if context_data and isinstance(context_data, dict):
            try:
                full_json = json.dumps(
                    context_data, ensure_ascii=False, indent=2
                )
                full_bytes = full_json.encode("utf-8")

                if len(full_bytes) <= MAX_CONTEXT_BYTES:
                    # Fast path — everything fits.
                    prompt_sections.append(
                        "[DELEGATED CONTEXT PARAMETERS]"
                    )
                    prompt_sections.append(full_json)
                else:
                    # [P1-15 FIX] Priority-based retention.
                    logger.warning(
                        "Context data size (%d bytes) exceeds ceiling "
                        "(%d bytes). Retaining priority keys and "
                        "dropping the rest.",
                        len(full_bytes),
                        MAX_CONTEXT_BYTES,
                    )

                    # Reserve headroom for the notice suffix so the
                    # final payload (retained + notice) never exceeds
                    # MAX_CONTEXT_BYTES.
                    _NOTICE_RESERVE_BYTES = 512
                    _usable_budget = max(
                        0, MAX_CONTEXT_BYTES - _NOTICE_RESERVE_BYTES
                    )

                    priority_items: Dict[str, Any] = {}
                    other_items: Dict[str, Any] = {}
                    for key, value in context_data.items():
                        if _is_priority_key(str(key)):
                            priority_items[key] = value
                        else:
                            other_items[key] = value

                    retained: Dict[str, Any] = {}
                    dropped: list[str] = []
                    used_bytes = 0

                    # Helper: attempt to add one key if it fits.
                    def _try_add(k: str, v: Any) -> bool:
                        nonlocal used_bytes
                        try:
                            fragment = json.dumps(
                                {k: v}, ensure_ascii=False, indent=2
                            )
                        except Exception:
                            # Un-serializable value — drop it silently
                            # rather than crash the prompt build.
                            return False
                        fragment_bytes = len(
                            fragment.encode("utf-8")
                        )
                        if used_bytes + fragment_bytes <= _usable_budget:
                            retained[k] = v
                            used_bytes += fragment_bytes
                            return True
                        return False

                    # Phase 1 — priority keys first.
                    for k, v in priority_items.items():
                        if not _try_add(k, v):
                            dropped.append(k)

                    # Phase 2 — remaining keys next.
                    for k, v in other_items.items():
                        if not _try_add(k, v):
                            dropped.append(k)

                    if dropped:
                        retained["_context_bridge_notice"] = (
                            f"Context data exceeded the "
                            f"{MAX_CONTEXT_BYTES}-byte budget. "
                            f"Priority keys (task, tools, files, "
                            f"summary) were retained where possible. "
                            f"The following keys were dropped: "
                            f"{dropped}. If any of this information "
                            f"is required, ask the parent agent to "
                            f"reduce context_data."
                        )

                    retained_json = json.dumps(
                        retained, ensure_ascii=False, indent=2
                    )
                    prompt_sections.append(
                        "[DELEGATED CONTEXT PARAMETERS]"
                    )
                    prompt_sections.append(retained_json)
            except Exception as ser_err:
                logger.error(
                    "Failed to serialize delegation context_data: %s",
                    ser_err,
                )

        if project_rules and isinstance(project_rules, str):
            clean_rules = project_rules.strip()
            if clean_rules:
                prompt_sections.append("[PROJECT EXECUTION RULES]")
                prompt_sections.append(clean_rules[:2000])

        prompt_sections.append(
            "[SYSTEM BOUNDARY]\n"
            "You are executing as an autonomous specialist sub-agent in an isolated workspace sandbox. "
            "Focus exclusively on completing the directive above and producing clean deliverables."
        )

        return "\n\n".join(prompt_sections)
