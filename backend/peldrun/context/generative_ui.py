"""
Peldrun Generative UI Context Engine.

Decoupled prompt synthesis engine responsible for instructing LLMs on the
line-oriented OpenUI DSL syntax, dynamic catalog restrictions, autonomous
trigger rules, negative constraints, and contextual 1-shot demonstrations.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("peldrun.context.generative_ui")

# 1. Complete Component DSL Specification Registry (All 14 components)
# NOTE: Placeholders use single square brackets [ ] instead of double brackets [[ ]]
# or angle brackets < > to strictly prevent LLMs from hallucinating XML/HTML tags
# or confusing delimiters with markdown wiki-links.
COMPONENT_SPECIFICATIONS: Dict[str, str] = {
    "Card": "Card: [Title]\n[Executive summary or description text]",
    "Metric": "Metric: [Label] | [Value] | [Change] | [up|down|neutral]",
    "Badge": "Badge: [Label] | [success|warning|error|neutral]",
    "Alert": "Alert [info|success|warning|error]: [Message]",
    "Progress": "Progress: [Label] | [Percentage 0-100]",
    "Table": "Table:\nHeader1 | Header2 | Header3\nRow1Col1 | Row1Col2 | Row1Col3",
    "Chart": "Chart [bar|line|donut|area]: Label1:Number1, Label2:Number2 | Title: [Title] | Unit: [Unit]",
    "Diagram": "Diagram: [Title]\ngraph TD\n  A[Node1] --> B[Node2]",
    "Carousel": "Carousel: [Title]\nSlide: Title1 | Desc1 | Content1\nSlide: Title2 | Desc2 | Content2",
    "TestResults": "TestResults: Passed:[N], Failed:[N], Skipped:[N]",
    "Accordion": "Accordion: Title1:Content1 | Title2:Content2",
    "Form": "Form: [Title]\nField: [Label] ([text|number|email|password])",
    "MathPlot": "Plot: y = f(x) | range=[min, max] | title: [Title]  OR  Equation: [LaTeX string]",
    "ActionButton": "ActionButton: [Action Label]",
}

TRIGGER_DOMAINS_MAP: Dict[str, str] = {
    "Card": "executive summaries and overarching status overviews",
    "Metric": "key performance indicators, telemetry, and numerical KPIs",
    "Badge": "system health statuses and service operational states",
    "Alert": "critical warnings, security notices, or failure alerts",
    "Progress": "task progression milestones and capacity utilization",
    "Table": "multi-attribute comparisons and tabular structured datasets",
    "Chart": "trend analysis, statistical metrics, and visual distribution charts",
    "Diagram": "process workflows, sequence diagrams, and architecture flowcharts",
    "Carousel": "multi-card showcases, alternate options, and slider presentations",
    "TestResults": "test execution suites, verification reports, and pass/fail tallies",
    "Accordion": "collapsible technical deep-dives and expandable FAQ sections",
    "Form": "configuration parameter forms and structured user input collection",
    "MathPlot": "mathematical curves, coordinate plots, and scientific equations",
    "ActionButton": "suggested next operational actions, confirmations, and follow-ups",
}


def _build_dynamic_example(allowed: Set[str]) -> str:
    """
    Synthesizes a concise, realistic demonstration with 2 to 3 components
    to prevent LLMs from copying and emitting every component at once.
    """
    example_lines: List[str] = []

    if "Card" in allowed:
        example_lines.append("Card: Cluster Telemetry Overview")
        example_lines.append("Infrastructure workload healthy across primary cluster nodes.")

    if "Metric" in allowed:
        example_lines.append("Metric: CPU Utilization | 42.8% | -5.2% | down")
        example_lines.append("Metric: Memory Allocation | 64.1% | +1.8% | up")
    elif "Progress" in allowed:
        example_lines.append("Progress: Resource Capacity | 78")

    if "Alert" in allowed and len(example_lines) < 3:
        example_lines.append("Alert [info]: Scheduled maintenance window set for Sunday at 02:00 UTC.")

    if "Table" in allowed and "Metric" not in allowed:
        example_lines.append("Table:\nService | Latency | Status\nAuth-Service | 12ms | Healthy\nVector-DB | 4.8ms | Healthy")

    if "ActionButton" in allowed:
        example_lines.append("ActionButton: Refresh Telemetry")

    if not example_lines:
        first_comp = next(iter(allowed))
        example_lines.append(f"Card: System Status\nAll operations running normally for {first_comp}.")

    return "\n".join(example_lines)


def build_generative_ui_prompt(generative_ui: Optional[Dict[str, Any]]) -> str:
    """
    Synthesizes a dynamic, zero-contradiction system directive for Generative UI.
    """
    if not generative_ui or not generative_ui.get("enabled"):
        return ""

    allowed_raw = (
        generative_ui.get("allowed_components")
        or generative_ui.get("allowedComponents")
        or []
    )
    if not isinstance(allowed_raw, (list, set, tuple)):
        return ""

    allowed: Set[str] = {c for c in allowed_raw if c in COMPONENT_SPECIFICATIONS}
    if not allowed:
        return ""

    active_specs: List[str] = [
        f"- {COMPONENT_SPECIFICATIONS[comp_name]}"
        for comp_name in COMPONENT_SPECIFICATIONS
        if comp_name in allowed
    ]
    specs_str = "\n".join(active_specs)

    trigger_domains: List[str] = [
        TRIGGER_DOMAINS_MAP[comp] for comp in allowed if comp in TRIGGER_DOMAINS_MAP
    ]
    trigger_clause = (
        ", ".join(trigger_domains)
        if trigger_domains
        else "structured data and executive overviews"
    )

    example_str = _build_dynamic_example(allowed)

    return (
        "[AUTONOMOUS GENERATIVE UI ENGINE: ACTIVE]\n"
        "You possess the native capability to render interactive user interfaces.\n\n"
        "1. STRICT SYNTAX FORMAT (CRITICAL - DO NOT USE XML TAGS):\n"
        "- DO NOT emit XML or HTML tags like <Title>, </Title>, <Table>, </Table>, <Metric>, <Chart>, or </Chart>.\n"
        "- Every component is written as plain text starting with its keyword prefix on a new line.\n"
        "- The only XML tags allowed are the outer container <openui> and </openui>.\n"
        "- Conversational reasoning and markdown text MUST remain outside <openui>.\n\n"
        "2. COMPONENT SELECTION RULE:\n"
        "- Choose ONLY 1 to 3 components from the catalog that directly answer the inquiry. DO NOT dump every component in every response.\n\n"
        "3. WHEN TO USE (TRIGGER CONDITIONS):\n"
        f"Whenever the user's inquiry naturally involves {trigger_clause}—or whenever your response naturally "
        "contains >=3 quantitative numerical data points, tabular comparisons, or workflows—"
        "accompany your conversational response with an interactive layout enclosed inside <openui>...</openui> tags.\n\n"
        "4. WHEN NOT TO USE (NON-TRIGGER CONDITIONS):\n"
        "- Greetings, brief pleasantries, or simple conversational confirmations.\n"
        "- Single-sentence factual answers without quantitative data.\n"
        "- DO NOT emit <openui> tags under any non-trigger condition.\n\n"
        "5. ALLOWED COMPONENTS CATALOG:\n"
        f"{specs_str}\n\n"
        "6. STRICT NEGATIVE CONSTRAINTS & FORMATTING RULES:\n"
        "- ONLY components listed under the ALLOWED CATALOG above exist. If a visual structure is NOT enabled, format it as standard Markdown OUTSIDE <openui>.\n"
        "- In Charts, values MUST be strictly numerical numbers (e.g. '10am:180, 11am:260'). NEVER use words as values.\n"
        "- Delimiter escaping: In text fields containing '|', escape it as '\\|'.\n"
        "- Action Buttons: Always use 'ActionButton: [Label]'.\n"
        "- Output <openui> directly as raw text. NEVER enclose <openui> inside markdown code fences (``` or ```xml).\n"
        "- Keep data bounded: Tables <= 15 rows, Charts <= 25 points.\n\n"
        "7. 1-SHOT DEMONSTRATION (SYNCHRONIZED WITH YOUR ACTIVE CATALOG):\n"
        "<openui>\n"
        f"{example_str}\n"
        "</openui>"
    )
