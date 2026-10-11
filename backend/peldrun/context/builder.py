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
        Synthesizes a completely dynamic, zero-contradiction system directive for Generative UI.
        - Strict Rule 1: If disabled or no allowed components, returns an empty string (0 tokens).
        - Strict Rule 2: Trigger domains are dynamically constructed strictly from enabled components.
        - Strict Rule 3: Explicit negative fallback rule enforces standard Markdown for disabled items.
        - Strict Rule 4: The 1-Shot example is synthesized ON-THE-FLY using ONLY active components.
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

        # 1. Full Component Specifications Registry
        component_specs: Dict[str, str] = {
            "Card": "Card: <Title>\\n<Executive summary or description text>",
            "Metric": "Metric: <Label> | <Value> | <Change> | <up|down|neutral>",
            "Badge": "Badge: <Label> | <success|warning|error|neutral>",
            "Alert": "Alert [<info|success|warning|error>]: <Message>",
            "Progress": "Progress: <Label> | <Percentage 0-100>",
            "Table": "Table:\\nHeader1 | Header2 | Header3\\nRow1Col1 | Row1Col2 | Row1Col3 (Max 15 rows)",
            "Chart": "Chart [<area|bar|line|donut>]: Label1:Number1, Label2:Number2 | Title: <Title> | Unit: <Unit>",
            "Diagram": "Diagram: <Title>\\ngraph TD\\n  A[Node1] --> B[Node2]",
            "Carousel": "Carousel: <Title>\\nSlide: Title1 | Desc1 | Content1\\nSlide: Title2 | Desc2 | Content2",
            "TestResults": "TestResults: Passed:<N>, Failed:<N>, Skipped:<N>",
            "Accordion": "Accordion: Title1:Content1 | Title2:Content2",
            "Form": "Form: <Title>\\nField: Field1\\nField: Field2",
            "MathPlot": "Plot: <y = f(x)> | range=[min, max]  OR  Equation: <LaTeX string>",
            "ActionButton": "Action: <Action Label>",
        }

        # 2. Extract active specs
        active_specs: List[str] = []
        for comp_name, spec in component_specs.items():
            if comp_name in allowed:
                active_specs.append(f"- {spec}")

        if not active_specs:
            return ""

        specs_str = "\n".join(active_specs)

        # 3. Dynamically build Trigger Domains
        trigger_domains: List[str] = []
        if "Card" in allowed:
            trigger_domains.append("executive summaries")
        if "Metric" in allowed:
            trigger_domains.append("key performance indicators & metrics")
        if "Badge" in allowed:
            trigger_domains.append("service health states")
        if "Table" in allowed:
            trigger_domains.append("tabular comparisons")
        if "Chart" in allowed:
            trigger_domains.append("trend analytics & visual charts")
        if "Diagram" in allowed:
            trigger_domains.append("process workflows & architectural flowcharts")
        if "Carousel" in allowed:
            trigger_domains.append("multi-option showcases & sliders")
        if "TestResults" in allowed:
            trigger_domains.append("test suite outcomes")
        if "Form" in allowed:
            trigger_domains.append("parameter configuration forms")
        if "MathPlot" in allowed:
            trigger_domains.append("mathematical curves & equations")

        trigger_clause = (
            ", ".join(trigger_domains)
            if trigger_domains
            else "structured executive summaries"
        )

        # 4. Synthesize Dynamic 1-Shot Example (strictly using ONLY active components)
        example_lines: List[str] = []

        if "Card" in allowed:
            example_lines.append("Card: Operational Overview")
            example_lines.append("All primary systems operating within nominal boundaries.")

        if "Metric" in allowed:
            example_lines.append("Metric: CPU Utilization | 42% | -6.5% | down")
        elif "Progress" in allowed:
            example_lines.append("Progress: Task Velocity | 85")

        if "Badge" in allowed:
            example_lines.append("Badge: Primary Gateway | success")

        if "Table" in allowed and "Metric" not in allowed:
            example_lines.append("Table:\nEngine | Latency | Type\nRedis | 0.8ms | In-Memory\nPostgres | 4.2ms | Disk")

        if "Chart" in allowed and "Metric" not in allowed and "Table" not in allowed:
            example_lines.append("Chart [bar]: 10am:180, 11am:260, 12pm:420 | Title: Hourly Throughput | Unit: MB/s")

        if "Diagram" in allowed and len(example_lines) < 3:
            example_lines.append("Diagram: Flow Logic\ngraph LR\n  A[Input] --> B[Output]")

        if "ActionButton" in allowed:
            example_lines.append("Action: Confirm Deployment")

        if not example_lines:
            first_active = next(iter(allowed))
            if first_active in component_specs:
                example_lines.append(component_specs[first_active].replace("<Title>", "Summary").replace("<Label>", "Status"))

        example_str = "\n".join(example_lines)

        return (
            "[AUTONOMOUS GENERATIVE UI ENGINE: ACTIVE]\n"
            "You possess the native capability to render interactive user interfaces.\n"
            f"AUTONOMOUS TRIGGER RULE: Whenever the user's inquiry naturally involves {trigger_clause}—even if they do not explicitly request a UI—"
            "you MUST accompany your plain conversational answer with an interactive dashboard enclosed inside <openui>...</openui> tags.\n"
            "Conversational reasoning and explanations MUST remain outside <openui> as standard markdown text.\n\n"
            "ALLOWED COMPONENTS CATALOG:\n"
            f"{specs_str}\n\n"
            "STRICT NEGATIVE CONSTRAINT & FALLBACK RULES:\n"
            "1. ONLY components listed under the ALLOWED CATALOG above exist. If the user asks for a visual layout "
            "(such as a chart, table, diagram, or metric) that is NOT listed in the allowed catalog, you MUST format "
            "that data as standard Markdown text/tables/lists OUTSIDE <openui>. NEVER invent tags or emit disabled components inside <openui>.\n"
            "2. For Charts, values MUST be numerical numbers (e.g. '10am:180, 11am:260' or 'Qdrant:15, Milvus:8'). NEVER use words as values.\n"
            "3. Output <openui> directly as raw text. NEVER enclose <openui> inside markdown code fences (``` or ```xml).\n"
            "4. Keep data bounded (Tables <= 15 rows, Charts <= 30 points).\n\n"
            "1-SHOT EXAMPLE (SYNCHRONIZED WITH YOUR ACTIVE CATALOG):\n"
            "<openui>\n"
            f"{example_str}\n"
            "</openui>"
        )

    def _format_execution_telemetry(self, execution: Optional[CurrentExecution]) -> str:
        """
        Format active turn's execution telemetry into a concise system block.
        Only included for the ongoing turn to inform tool status.
        """
        if not execution or not execution.steps:
            return ""

        lines = ["[Current Turn Execution State]"]
        for idx, step in enumerate(execution.steps[-6:], 1):
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
        3. Generative UI Instructions (Tier 1C - Conditional & Adaptive)
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
                if msg.turn_id and msg.turn_id in covered_turn_ids:
                    continue

                if msg.role in ("user", "assistant"):
                    clean_content = msg.content.strip()
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
