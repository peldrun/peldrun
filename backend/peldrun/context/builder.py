"""
backend/peldrun/context/builder.py

Context Assembly and Prompt Synthesis Engine for peldrun-core.
Implements the Microsoft Context Contract and RFC-609 Separation Policy:
Constructs structured LLM prompt messages from session.context.json, enforcing
strict architectural isolation between persistent history and LLM active context.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

from peldrun.context.budget import TokenBudgetController
from peldrun.context.schema import ContextSidecar, CurrentExecution

logger = logging.getLogger("peldrun.context.builder")


class ContextManagerProtocol(Protocol):
    """Protocol defining the interface for context assembly engines."""

    def build_messages(
        self,
        sidecar: Optional[ContextSidecar] = None,
        current_user_message: str = "",
        system_prompt: Optional[str] = None,
        project_rules: Optional[str] = None,
        include_execution: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        ...


class ContextManager:
    """
    Central orchestrator for LLM context assembly.
    Extracts sanitized dialogue from Sidecar and ensures older execution telemetry
    is completely isolated from subsequent LLM turns.
    """

    def __init__(self, default_system_prompt: Optional[str] = None):
        self.default_system_prompt = default_system_prompt

    def _format_execution_telemetry(self, execution: Optional[CurrentExecution]) -> str:
        """
        Format active turn's execution telemetry into a concise system block.
        Only included for the ongoing turn to inform tool status.
        """
        if not execution or not execution.steps:
            return ""

        lines = ["[Current Turn Execution State]"]
        for idx, step in enumerate(execution.steps[-6:], 1):  # Keep latest 6 steps
            step_type = step.type
            name = step.name or "unknown"
            if step_type == "tool_call":
                args_summary = json.dumps(step.args or {}, ensure_ascii=False)
                if len(args_summary) > 160:
                    args_summary = args_summary[:160] + "..."
                lines.append(f"{idx}. Tool Call: {name}({args_summary})")
            elif step_type == "observation":
                res = str(step.result or "").strip()
                if len(res) > 240:
                    res = res[:240] + "... [truncated]"
                lines.append(f"{idx}. Observation: {res}")
            elif step_type == "status":
                lines.append(f"{idx}. Status: {step.result or step.error or 'running'}")

        if execution.artifacts:
            lines.append(f"Active Artifacts: {', '.join(execution.artifacts)}")

        return "\n".join(lines)

    def build_messages(
        self,
        sidecar: Optional[ContextSidecar] = None,
        current_user_message: str = "",
        system_prompt: Optional[str] = None,
        project_rules: Optional[str] = None,
        include_execution: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        Assemble the architectural context tiers in deterministic sequence:
        1. Unified System Directive (System instructions + Project rules)
        2. Compacted Context Summary (Summary slice)
        3. Sanitized Recent Dialogue (Filtered to prevent duplicating summary or current prompt)
        4. Ongoing Execution Telemetry (Omitted for new turns, included only when actively stepping)
        5. Current User Input (Current slice)
        """
        # Tier 1: Unified System Directive
        effective_system = (
            system_prompt
            if system_prompt is not None
            else self.default_system_prompt
        )

        system_blocks: List[str] = []
        if effective_system and effective_system.strip():
            system_blocks.append(effective_system.strip())

        if project_rules and project_rules.strip():
            system_blocks.append(f"[Project Rules]\n{project_rules.strip()}")

        system_msgs: List[Dict[str, Any]] = []
        if system_blocks:
            system_msgs.append({"role": "system", "content": "\n\n".join(system_blocks)})

        # Tier 2: Compacted Context Summary & Covered Turns Identification
        summary_msgs: List[Dict[str, Any]] = []
        covered_turn_ids: Set[str] = set()

        if sidecar and sidecar.summary and sidecar.summary.content.strip():
            summary_content = (
                f"[Previous Context Summary]\n{sidecar.summary.content.strip()}"
            )
            summary_msgs.append({"role": "system", "content": summary_content})
            if sidecar.summary.covers_turns:
                covered_turn_ids = set(sidecar.summary.covers_turns)

        # Tier 3: Recent Sanitized Conversation
        recent_msgs: List[Dict[str, Any]] = []
        clean_current_input = current_user_message.strip()

        if sidecar and sidecar.conversation:
            for msg in sidecar.conversation:
                # Exclude turns already compressed inside the summary tier
                if msg.turn_id and msg.turn_id in covered_turn_ids:
                    continue

                if msg.role in ("user", "assistant"):
                    clean_content = msg.content.strip()
                    # Defensive: Avoid duplicating the current user prompt if already present in history
                    if msg.role == "user" and clean_current_input and clean_content == clean_current_input:
                        continue
                    recent_msgs.append({"role": msg.role, "content": msg.content})

        # Tier 4: Execution State (Only for ongoing active turns)
        exec_msgs: List[Dict[str, Any]] = []
        if include_execution and sidecar and sidecar.current_execution:
            formatted_exec = self._format_execution_telemetry(sidecar.current_execution)
            if formatted_exec:
                exec_msgs.append({"role": "system", "content": formatted_exec})

        # Tier 5: Current User Message
        current_msgs: List[Dict[str, Any]] = []
        if clean_current_input:
            current_msgs.append({"role": "user", "content": clean_current_input})

        # Apply Token Budget Controller
        budget_spec = sidecar.budget if sidecar else None
        controller = TokenBudgetController(budget_spec)
        fitted_messages, usage = controller.fit_all(
            system_msgs=system_msgs,
            summary_msgs=summary_msgs,
            recent_msgs=recent_msgs,
            exec_msgs=exec_msgs,
            current_msgs=current_msgs,
        )

        return fitted_messages, usage
