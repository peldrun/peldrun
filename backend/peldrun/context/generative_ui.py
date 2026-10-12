"""Peldrun Generative UI context builder.

Builds an optional OpenUI Lang v0.5 system directive from the user's active
Generative UI settings. Markdown remains the default response format.

This module builds prompt context and exposes token-budget helpers. It does not
validate or render model-generated OpenUI. The caller and renderer must enforce
the active component allowlist and handle parsing failures, retries, and fallback.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("peldrun.context.generative_ui")

MIN_OUTPUT_TOKENS = 512
MAX_OUTPUT_TOKENS = 4096
DEFAULT_OUTPUT_TOKENS = 2048
MIN_RETRIES = 0
MAX_RETRIES = 5
DEFAULT_RETRIES = 2

# Component signatures exposed to the model. Keep these synchronized with the
# frontend catalog, OpenUI parser, and renderer.
COMPONENT_SPECIFICATIONS: Dict[str, str] = {
    "Card": 'Card(title: str = None, description: str = None, items: list[str] = [])',
    "Metric": 'Metric(label: str, value: str | number, change: str = None, trend: "up" | "down" | "neutral" = "neutral")',
    "Badge": 'Badge(label: str, variant: "info" | "success" | "warning" | "error" | "neutral" = "neutral")',
    "Alert": 'Alert(type: "info" | "success" | "warning" | "error", message: str)',
    "Progress": "Progress(label: str, percentage: number from 0 to 100)",
    "ActionButton": "ActionButton(label: str, actionId: str)",
    "Table": "Table(headers: list[str], rows: list[list[str | number]])",
    "TestResults": "TestResults(passed: number, failed: number, skipped: number)",
    "Accordion": 'Accordion(items: list[{"title": str, "content": str}])',
    "Chart": 'Chart(type: "bar" | "line" | "donut" | "area", title: str, unit: str = None, data: list[{"label": str, "value": number}])',
    "Diagram": "Diagram(title: str = None, code: str)",
    "Carousel": 'Carousel(title: str = None, slides: list[{"title": str, "description": str = None, "content": str}])',
    "Form": 'Form(title: str, fields: list[{"name": str, "label": str, "type": "text" | "number" | "email" | "password" | "select" | "checkbox", "placeholder": str = None, "required": bool = False, "options": list[str] = None}], submitLabel: str = "Submit", description: str = None)',
    "MathPlot": "MathPlot(title: str = None, formula: str = None, equation: str = None, rangeX: [min, max] = [-10, 10])",
}

LAYOUT_SPECIFICATIONS: Dict[str, str] = {
    "Stack": 'Stack(children: list[component_id], direction: "vertical" | "horizontal" = "vertical", gap: str = "md")',
    "Grid": "Grid(children: list[component_id], columns: number = 2)",
}

TRIGGER_DOMAINS_MAP: Dict[str, str] = {
    "Card": "summaries and status overviews",
    "Metric": "key performance indicators and numerical KPIs",
    "Badge": "concise status labels",
    "Alert": "warnings, security notices, and failures",
    "Progress": "task progress and capacity utilization",
    "ActionButton": "supported user actions and follow-up operations",
    "Table": "structured datasets and multi-attribute comparisons",
    "TestResults": "test-suite pass, fail, and skipped counts",
    "Accordion": "long technical details and expandable FAQs",
    "Chart": "trends and numerical distributions",
    "Diagram": "process workflows and architecture diagrams",
    "Carousel": "small sets of alternatives or scenarios",
    "Form": "structured user input and configuration",
    "MathPlot": "mathematical functions and equations",
}


def _clamp_integer(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """Convert a setting to an integer and clamp it to a supported range."""
    if isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_max_tokens(value: Any) -> int:
    """Return a safe output-token budget in the supported 512–4096 range."""
    return _clamp_integer(
        value,
        minimum=MIN_OUTPUT_TOKENS,
        maximum=MAX_OUTPUT_TOKENS,
        default=DEFAULT_OUTPUT_TOKENS,
    )


def _compute_data_bounds(max_tokens: int) -> Dict[str, int]:
    """Return conservative component and data limits for a token budget."""
    tokens = _clamp_max_tokens(max_tokens)
    if tokens <= 512:
        return {"max_chart_points": 6, "max_table_rows": 4, "max_components": 2}
    if tokens <= 1024:
        return {"max_chart_points": 12, "max_table_rows": 8, "max_components": 3}
    if tokens <= 2048:
        return {"max_chart_points": 18, "max_table_rows": 12, "max_components": 4}
    return {"max_chart_points": 25, "max_table_rows": 15, "max_components": 5}


def get_max_output_tokens(generative_ui: Optional[Dict[str, Any]]) -> int:
    """Return the output-token budget for the model call layer."""
    if not isinstance(generative_ui, dict) or not generative_ui.get("enabled"):
        return DEFAULT_OUTPUT_TOKENS
    return _clamp_max_tokens(generative_ui.get("max_output_tokens"))


def get_max_retries(generative_ui: Optional[Dict[str, Any]]) -> int:
    """Return a bounded retry count for the caller's parse/fallback policy."""
    if not isinstance(generative_ui, dict) or not generative_ui.get("enabled"):
        return 0
    return _clamp_integer(
        generative_ui.get("max_retries"),
        minimum=MIN_RETRIES,
        maximum=MAX_RETRIES,
        default=DEFAULT_RETRIES,
    )


def get_allowed_components(generative_ui: Optional[Dict[str, Any]]) -> Set[str]:
    """Return known, enabled component names from either supported setting key."""
    if not isinstance(generative_ui, dict):
        return set()

    raw = generative_ui.get("allowed_components")
    if raw is None:
        raw = generative_ui.get("allowedComponents")
    if not isinstance(raw, (list, tuple, set)):
        return set()

    allowed = {
        item
        for item in raw
        if isinstance(item, str) and item in COMPONENT_SPECIFICATIONS
    }
    return allowed


def _build_catalog(allowed: Set[str]) -> str:
    """Build a stable catalog containing only enabled components."""
    lines = [f"- {spec}" for spec in LAYOUT_SPECIFICATIONS.values()]
    lines.extend(
        f"- {COMPONENT_SPECIFICATIONS[name]}"
        for name in COMPONENT_SPECIFICATIONS
        if name in allowed
    )
    return "\n".join(lines)


def build_generative_ui_prompt(generative_ui: Optional[Dict[str, Any]]) -> str:
    """Build optional OpenUI instructions scoped to the active component catalog.

    Returns an empty string when the feature is disabled or no recognized
    components are enabled. No demonstration payload is appended: examples can
    prime the model to emit OpenUI even for ordinary Markdown conversations.
    """
    if not isinstance(generative_ui, dict) or not generative_ui.get("enabled"):
        return ""

    allowed = get_allowed_components(generative_ui)
    if not allowed:
        logger.debug("Generative UI is enabled but no recognized components are active")
        return ""

    max_tokens = _clamp_max_tokens(generative_ui.get("max_output_tokens"))
    bounds = _compute_data_bounds(max_tokens)
    catalog_str = _build_catalog(allowed)
    allowed_names = ", ".join(sorted(allowed))

    return (
        "[GENERATIVE UI ENGINE — OPTIONAL RENDERING POLICY]\n"
        "OpenUI Lang v0.5 is an optional visual format. Normal Markdown is the default.\n\n"
        "1. RESPONSE FORMAT POLICY\n"
        "- Answer in Markdown for ordinary conversation, explanations, summaries, code review, debugging, and factual questions.\n"
        "- Do not emit an <openui> block merely because this capability is enabled.\n"
        "- Use Markdown plus OpenUI only when both prose and a visual representation materially improve the answer.\n"
        "- If the user explicitly requests text-only output, do not emit OpenUI.\n"
        "- If the user explicitly requests a dashboard, chart, plot, diagram, visual workflow, or supported form, use OpenUI when the required component is enabled.\n"
        "- If the requested visual cannot be built with enabled components, explain the limitation in Markdown rather than inventing a component.\n"
        "- Never fabricate measurements, test results, system status, or other data to fill a visual. Use only data supported by the conversation or available tool results.\n\n"
        "2. OPENUI LANG STRUCTURE\n"
        "- Enclose OpenUI source only inside raw <openui> and </openui> tags. Do not use Markdown code fences around the block.\n"
        "- The first source line inside the block must assign a root layout: root = Stack([child1, child2]) or root = Grid([child1, child2], columns=2).\n"
        "- Define each child on a separate subsequent line using variable_id = ComponentName(arguments).\n"
        "- Every identifier referenced by root must be assigned exactly once in the same block.\n"
        "- Use only Stack, Grid, and the enabled component names listed in the active catalog below.\n"
        "- Arguments must follow the listed component signatures and use literal strings, numbers, booleans, lists, or objects. Do not use variables, expressions, imports, function calls, or executable code as argument values.\n"
        "- Follow the parser's string-escaping rules: encode quotes, backslashes, and line breaks using valid escaped string content. For diagram source, use escaped newline sequences (\\n) inside the string, not raw line breaks within a quoted string. For LaTeX backslashes, escape the backslash according to the string grammar accepted by the parser.\n"
        "- Keep explanation, reasoning, caveats, and Markdown outside the OpenUI block.\n\n"
        "3. COMPONENT AND DATA LIMITS\n"
        f"- Use no more than {bounds['max_components']} child components in one root layout.\n"
        f"- Tables must contain no more than {bounds['max_table_rows']} data rows, excluding the header.\n"
        f"- Charts must contain no more than {bounds['max_chart_points']} data points.\n"
        "- Use the minimum number of components needed. Do not render the entire catalog.\n"
        "- Every displayed value must be explicit and grounded. Never use ellipses, placeholder text, or references such as 'values from the table'.\n"
        "- Chart data must be a list of objects with a string label and numeric value. Table rows must be a two-dimensional list matching the header width.\n"
        "- For actions, use ActionButton only when its actionId is supported by the application. Never imply an action was executed just because a button was rendered.\n\n"
        "4. COMPONENT SELECTION GUIDANCE\n"
        "- Trends over time: Chart(type='line').\n"
        "- Categorical comparisons: Chart(type='bar') or Table, depending on the data.\n"
        "- Parts of a whole: Chart(type='donut').\n"
        "- Cumulative trends: Chart(type='area').\n"
        "- Process or architecture flow: Diagram.\n"
        "- Three or more independent key indicators: separate Metric components when useful.\n"
        "- Multi-attribute records: Table.\n"
        "- Warnings and failures: Alert.\n"
        "- Test summaries: TestResults, only when actual test counts are available.\n"
        "- Long structured details: Accordion, when supported by the renderer.\n"
        "- User input: Form, only when the application supports the resulting form behavior.\n"
        "- Mathematical functions or equations: MathPlot, when the renderer supports the requested form.\n"
        "- Use different components for genuinely different data patterns only when each adds value. Do not add components for visual variety alone.\n\n"
        "5. DO NOT USE OPENUI FOR\n"
        "- Greetings, short confirmations, or simple factual answers.\n"
        "- Simple code explanations, debugging, or conceptual analysis that does not benefit from a visual.\n"
        "- A request to explain, analyze, review, summarize, or report when ordinary Markdown is sufficient.\n"
        "- Content whose data is missing, speculative, or not supported by the available context.\n\n"
        "6. ACTIVE COMPONENT CATALOG\n"
        f"Enabled components ({len(allowed)}): {allowed_names}\n"
        "Any component not named in this enabled list is unavailable. Do not invent names such as ComparisonChart, TextBox, or Timeline.\n\n"
        f"Supported signatures:\n{catalog_str}\n\n"
        "7. OUTPUT VALIDITY\n"
        "- Use assignment syntax, not obsolete prefixes such as 'Card: ...' or 'Metric: ...'.\n"
        "- Do not wrap assignments in a standalone list or put Markdown fences inside the OpenUI tags.\n"
        "- Do not use unsupported named arguments or invent properties not present in the signatures.\n"
        "- If a valid visual cannot be produced confidently within the enabled catalog and limits, answer in Markdown.\n"
    )


__all__ = [
    "COMPONENT_SPECIFICATIONS",
    "LAYOUT_SPECIFICATIONS",
    "TRIGGER_DOMAINS_MAP",
    "build_generative_ui_prompt",
    "get_allowed_components",
    "get_max_output_tokens",
    "get_max_retries",
]
