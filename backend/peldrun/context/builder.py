# Path: backend/peldrun/context/builder.py

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
        generative_ui: Optional[Dict[str, Any]] = None,
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

    @staticmethod
    def _format_generative_ui_prompt(generative_ui: Optional[Dict[str, Any]]) -> str:
        """
        Assemble concise, token-economical system instructions for Generative UI.
        Strict rule: Only include component specifications for components that are explicitly allowed.
        If disabled or no allowed components, returns an empty string (0 tokens added).
        """
        if not generative_ui or not generative_ui.get("enabled"):
            return ""

        allowed_raw = (
            generative_ui.get("allowed_components")
            or generative_ui.get("allowedComponents")
            or []
        )
        allowed = set(allowed_raw) if isinstance(allowed_raw, (list, set, tuple)) else set()

        if not allowed:
            return ""

        component_specs: Dict[str, str] = {
            "Card": "Card: <Title>\\n<Description or items>",
            "Metric": "Metric: <Label> | <Value> | <Change> | <up|down|neutral>",
            "Badge": "Badge: <Label> | <info|success|warning|error|neutral>",
            "Alert": "Alert [<info|success|warning|error>]: <Message>",
            "Progress": "Progress: <Label> | <Percentage 0-100>",
            "Table": "Table: Header1, Header2 | Row1Col1, Row1Col2 (Max 15 rows)",
            "Chart": "Chart [<bar|line>]: Label1:Val1, Label2:Val2 (Max 30 points)",
            "Form": "Form: <Title> | Field1, Field2 (Max 8 fields)",
            "TestResults": "TestResults: Passed:<N>, Failed:<N>, Skipped:<N>",
            "Accordion": "Accordion: Title1:Content1 | Title2:Content2",
            "ActionButton": "Action: <Action Label>",
        }

        active_lines: List[str] = []
        for comp_name, spec in component_specs.items():
            if comp_name in allowed:
                active_lines.append(f"- {spec}")

        if not active_lines:
            return ""

        specs_str = "\n".join(active_lines)
        return (
            "[Generative UI Mode Active]\n"
            "When responding with interactive dashboards, metrics, forms, tables, or actionable summaries, "
            "output OpenUI Lang DSL enclosed strictly inside <openui>...</openui> tags.\n"
            "Plain explanations and analysis must remain outside <openui> as standard text or Markdown.\n"
            "Allowed components catalog:\n"
            f"{specs_str}\n"
            "Rules & Guardrails:\n"
            "- Do not use any component outside the allowed list above.\n"
            "- Limit Tables to 15 rows and Charts to 30 points. For large datasets, save the full data via file tools "
            "and render an executive summary Card inside <openui>."
        )

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
        include_execution: bool = True,
        generative_ui: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        Assemble the architectural context tiers in deterministic sequence:
        1. System Directive (Tier 1A)
        2. Project Rules (Tier 1B)
        3. Generative UI Instructions (Tier 1C - Conditional)
        4. Compacted Context Summary (Tier 2)
        5. Sanitized Recent Dialogue (Tier 3)
        6. Ongoing Execution Telemetry (Tier 4)
        7. Current User Input (Tier 5)
        """
        system_msgs: List[Dict[str, Any]] = []

        # Tier 1A: Base System Directive
        effective_system = (
            system_prompt
            if system_prompt is not None
            else self.default_system_prompt
        )
        if effective_system and effective_system.strip():
            system_msgs.append({"role": "system", "content": effective_system.strip()})

        # Tier 1B: Explicit Project Rules Block
        if project_rules and project_rules.strip():
            rules_content = project_rules.strip()
            if not rules_content.startswith("[Project Rules]"):
                rules_content = f"[Project Rules]\n{rules_content}"
            system_msgs.append({"role": "system", "content": rules_content})

        # Tier 1C: Generative UI Directive (Only injected if enabled and allowed components exist)
        if generative_ui and generative_ui.get("enabled"):
            ui_directive = self._format_generative_ui_prompt(generative_ui)
            if ui_directive:
                system_msgs.append({"role": "system", "content": ui_directive})

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

        # Tier 4: Execution State (Only included when actively requested AND in non-terminal state)
        exec_msgs: List[Dict[str, Any]] = []
        if include_execution and sidecar and sidecar.current_execution:
            exec_status = getattr(sidecar.current_execution, "status", "running")
            if exec_status in ("running", "waiting_for_user"):
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
