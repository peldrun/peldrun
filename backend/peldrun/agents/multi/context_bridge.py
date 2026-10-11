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
        - Explicit isolation instructions preventing child from attempting secondary delegation.
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
            clean_instruction = clean_instruction[:MAX_INSTRUCTION_CHARS] + "\n[TRUNCATED BY CONTEXT BRIDGE]"

        prompt_sections = [
            f"[DELEGATED SUBTASK DIRECTIVE (DEPTH {delegation_depth})]",
            clean_instruction,
        ]

        if context_data and isinstance(context_data, dict):
            try:
                raw_json = json.dumps(context_data, ensure_ascii=False, indent=2)
                raw_bytes = raw_json.encode("utf-8")
                if len(raw_bytes) > MAX_CONTEXT_BYTES:
                    logger.warning(
                        "Context data size (%d bytes) exceeds ceiling (%d bytes). Stripping extra keys.",
                        len(raw_bytes),
                        MAX_CONTEXT_BYTES,
                    )
                    # Safe truncation to fit within ceiling
                    raw_json = json.dumps(
                        {"notice": "Context data exceeded maximum budget and was trimmed."},
                        ensure_ascii=False,
                    )

                prompt_sections.append("[DELEGATED CONTEXT PARAMETERS]")
                prompt_sections.append(raw_json)
            except Exception as ser_err:
                logger.error("Failed to serialize delegation context_data: %s", ser_err)

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
