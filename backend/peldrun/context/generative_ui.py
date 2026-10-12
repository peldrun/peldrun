# backend/peldrun/context/generative_ui.py
"""
Peldrun Generative UI Context Engine.

Synthesizes OpenUI Lang v0.5 system directives dynamically scoped to the user's
active component catalog. Aligns strictly with GenerativeUITab settings:
- enabled (master kill-switch)
- preset profiles (economic / balanced / full / custom)
- allowed_components filter
- max_output_tokens budget → data bounds
- max_retries (informational)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("peldrun.context.generative_ui")

# ============================================================================
# 1. Official OpenUI Lang Component Signatures (14 + 2 Layout)
# ============================================================================

COMPONENT_SPECIFICATIONS: Dict[str, str] = {
    "Card": 'Card(title: str = None, description: str = None, items: list[str] = [])',
    "Metric": 'Metric(label: str, value: str | number, change: str = None, trend: "up" | "down" | "neutral" = "neutral")',
    "Badge": 'Badge(label: str, variant: "info" | "success" | "warning" | "error" | "neutral" = "neutral")',
    "Alert": 'Alert(type: "info" | "success" | "warning" | "error", message: str)',
    "Progress": 'Progress(label: str, percentage: number 0-100)',
    "ActionButton": 'ActionButton(label: str, actionId: str)',
    "Table": 'Table(headers: list[str], rows: list[list[str | number]])',
    "TestResults": 'TestResults(passed: number, failed: number, skipped: number)',
    "Accordion": 'Accordion(items: list[{"title": str, "content": str}])',
    "Chart": 'Chart(type: "bar" | "line" | "donut" | "area", title: str, unit: str = None, data: list[{"label": str, "value": number}])',
    "Diagram": 'Diagram(title: str = None, code: str)',
    "Carousel": 'Carousel(title: str = None, slides: list[{"title": str, "description": str = None, "content": str}])',
    "Form": 'Form(title: str, fields: list[{"name": str, "label": str, "type": "text"|"number"|"email"|"password"|"select"|"checkbox", "placeholder": str = None, "required": bool = False, "options": list[str] = None}], submitLabel: str = "Submit", description: str = None)',
    "MathPlot": 'MathPlot(title: str = None, formula: str = None, equation: str = None, rangeX: [min, max] = [-10, 10])',
}

LAYOUT_SPECIFICATIONS: Dict[str, str] = {
    "Stack": 'Stack(children: list[component_id], direction: "vertical" | "horizontal" = "vertical", gap: str = "md")',
    "Grid": 'Grid(children: list[component_id], columns: number = 2)',
}

TRIGGER_DOMAINS_MAP: Dict[str, str] = {
    "Card": "executive summaries and overarching status overviews",
    "Metric": "key performance indicators, telemetry, and numerical KPIs",
    "Badge": "system health statuses and service operational states",
    "Alert": "critical warnings, security notices, or failure alerts",
    "Progress": "task progression milestones and capacity utilization",
    "ActionButton": "suggested next operational actions, confirmations, and follow-ups",
    "Table": "multi-attribute comparisons and tabular structured datasets",
    "TestResults": "test execution suites, verification reports, and pass/fail tallies",
    "Accordion": "collapsible technical deep-dives and expandable FAQ sections",
    "Chart": "trend analysis, statistical metrics, and visual distribution charts",
    "Diagram": "process workflows, sequence diagrams, and architecture flowcharts",
    "Carousel": "multi-card showcases, alternate options, and slider presentations",
    "Form": "configuration parameter forms and structured user input collection",
    "MathPlot": "mathematical curves, coordinate plots, and scientific equations",
}

# Priority order used when building the dynamic demonstration
_PRIORITY_ORDER: List[str] = [
    "Card", "Metric", "Chart", "Table", "Alert",
    "Progress", "Badge", "ActionButton", "Accordion",
    "Carousel", "TestResults", "Diagram", "MathPlot", "Form",
]


# ============================================================================
# 2. Token Budget → Data Bounds (matches GenerativeUITab slider 512-4096)
# ============================================================================

def _compute_data_bounds(max_tokens: int) -> Dict[str, int]:
    """
    Translate the maxOutputTokens budget into concrete data caps so the
    generated DSL never overflows the completion token budget.
    """
    if max_tokens <= 512:
        return {"max_chart_points": 6, "max_table_rows": 4, "max_components": 2}
    if max_tokens <= 1024:
        return {"max_chart_points": 12, "max_table_rows": 8, "max_components": 3}
    if max_tokens <= 2048:
        return {"max_chart_points": 18, "max_table_rows": 12, "max_components": 4}
    return {"max_chart_points": 25, "max_table_rows": 15, "max_components": 5}


# ============================================================================
# 3. Dynamic 1-Shot Example Builder
# ============================================================================

def _build_dynamic_example(allowed: Set[str], bounds: Dict[str, int]) -> str:
    """
    Synthesize a working OpenUI Lang demonstration using ONLY enabled components.
    The example uses correct newline handling for Diagram and single-backslash
    LaTeX for MathPlot — mirroring the rules enforced in section 7 of the prompt.
    """
    assignments: List[str] = []
    root_children: List[str] = []
    max_comp = bounds["max_components"]

    for name in _PRIORITY_ORDER:
        if name not in allowed or len(root_children) >= max_comp:
            continue

        if name == "Card":
            assignments.append(
                'c1 = Card("System Overview", "All primary services are running within nominal parameters.")'
            )
            root_children.append("c1")

        elif name == "Metric":
            assignments.append('m1 = Metric("CPU Utilization", "42.8%", "-5.2%", "down")')
            root_children.append("m1")

        elif name == "Chart":
            sample_n = min(4, bounds["max_chart_points"])
            samples = [("10:00", 24), ("10:05", 28), ("10:10", 22), ("10:15", 31)][:sample_n]
            pts = ", ".join('{"label": "%s", "value": %d}' % (lbl, val) for lbl, val in samples)
            assignments.append(f'ch1 = Chart("line", "Request Latency", "ms", [{pts}])')
            root_children.append("ch1")

        elif name == "Table":
            assignments.append(
                't1 = Table(["Service", "Latency", "Status"], '
                '[["Auth-Service", "12ms", "Healthy"], ["Vector-DB", "4.8ms", "Healthy"]])'
            )
            root_children.append("t1")

        elif name == "Alert":
            assignments.append('a1 = Alert("info", "Scheduled maintenance window at 02:00 UTC.")')
            root_children.append("a1")

        elif name == "Progress":
            assignments.append('p1 = Progress("Deployment Progress", 78)')
            root_children.append("p1")

        elif name == "Badge":
            assignments.append('b1 = Badge("Operational", "success")')
            root_children.append("b1")

        elif name == "ActionButton":
            assignments.append('btn1 = ActionButton("Refresh Telemetry", "refresh_telemetry")')
            root_children.append("btn1")

        elif name == "Accordion":
            assignments.append(
                'acc1 = Accordion([{"title": "Details", "content": "Full diagnostic information."}])'
            )
            root_children.append("acc1")

        elif name == "Carousel":
            assignments.append(
                'cr1 = Carousel("Options", ['
                '{"title": "Option A", "description": "Fast", "content": "Latency 12ms"}, '
                '{"title": "Option B", "description": "Safe", "content": "Latency 24ms"}])'
            )
            root_children.append("cr1")

        elif name == "TestResults":
            assignments.append('tr1 = TestResults(12, 0, 1)')
            root_children.append("tr1")

        elif name == "Diagram":
            # Correct form: ACTUAL newlines inside the string, NOT the two chars \\ and n
            assignments.append(
                'd1 = Diagram("Architecture", "graph TD\n  A[Client] --> B[Gateway]\n  B --> C[Server]")'
            )
            root_children.append("d1")

        elif name == "MathPlot":
            # Correct form: SINGLE backslash for LaTeX commands
            assignments.append(
                'mp1 = MathPlot("Sine Wave", "Math.sin(x)", "\\sin(x)", [-10, 10])'
            )
            root_children.append("mp1")

        elif name == "Form":
            assignments.append(
                'f1 = Form("Configuration", '
                '[{"name": "value", "label": "Value", "type": "text"}], "Submit")'
            )
            root_children.append("f1")

    # Final fallback if no component matched (should never happen in practice)
    if not root_children:
        assignments.append('c1 = Card("System Status", "Operations running normally.")')
        root_children.append("c1")

    root_line = f"root = Stack([{', '.join(root_children)}])"
    return "\n".join([root_line] + assignments)


# ============================================================================
# 4. Main Prompt Builder
# ============================================================================

def build_generative_ui_prompt(generative_ui: Optional[Dict[str, Any]]) -> str:
    """
    Synthesizes a dynamic, zero-contradiction system directive for OpenUI Lang v0.5.
    Returns empty string when the engine is disabled or no components are enabled.
    """
    # Gate 1: engine must be enabled (matches GenerativeUITab master switch)
    if not generative_ui or not generative_ui.get("enabled"):
        return ""

    # Gate 2: parse allowed_components (matches GenerativeUITab catalog checkboxes)
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

    # Gate 3: compute data bounds from max_output_tokens (matches GenerativeUITab slider)
    try:
        max_tokens = int(generative_ui.get("max_output_tokens") or 2048)
    except (TypeError, ValueError):
        max_tokens = 2048
    bounds = _compute_data_bounds(max_tokens)

    # Build catalog string (only enabled components)
    layout_specs: List[str] = [f"- {spec}" for spec in LAYOUT_SPECIFICATIONS.values()]
    component_specs: List[str] = [
        f"- {COMPONENT_SPECIFICATIONS[name]}"
        for name in COMPONENT_SPECIFICATIONS
        if name in allowed
    ]
    catalog_str = "\n".join(layout_specs + component_specs)
    allowed_names = ", ".join(sorted(allowed))

    # Build trigger clause
    trigger_domains: List[str] = [
        TRIGGER_DOMAINS_MAP[c] for c in allowed if c in TRIGGER_DOMAINS_MAP
    ]
    trigger_clause = (
        ", ".join(trigger_domains)
        if trigger_domains
        else "structured data and executive overviews"
    )

    # Build example
    example_str = _build_dynamic_example(allowed, bounds)

    return (
        "[AUTONOMOUS GENERATIVE UI ENGINE: ACTIVE - OPENUI LANG v0.5]\n"
        "You possess the native capability to render interactive user interfaces using OpenUI Lang.\n\n"
        "1. STRICT OPENUI LANG GRAMMAR (CRITICAL RULES):\n"
        "- OpenUI Lang is a strict, declarative functional grammar evaluated into an Abstract Syntax Tree (AST).\n"
        "- MANDATORY ENTRY POINT: Line 1 inside <openui> MUST ALWAYS assign the root layout container:\n"
        "  root = Stack([child1, child2, ...])  OR  root = Grid([child1, child2, ...], columns=2)\n"
        "- COMPONENT ASSIGNMENTS: Define each child component on subsequent lines assigning it to a variable:\n"
        "  variable_id = ComponentName(arg1, arg2, ...)\n"
        "- All arguments MUST be valid JSON-compatible literals (strings, numbers, booleans, lists, objects).\n"
        "- Outer container: Enclose the entire OpenUI Lang code ONLY within raw <openui> and </openui> tags.\n"
        "- Conversational reasoning, engineering text, and markdown MUST remain outside <openui>...</openui>.\n\n"
        "2. COMPONENT SELECTION RULE:\n"
        f"- Choose ONLY 1 to {bounds['max_components']} components from the catalog that directly answer the inquiry.\n"
        "- DO NOT dump every available component in every response. Select only what the inquiry demands.\n\n"
        "3. WHEN TO USE (MANDATORY TRIGGER — NOT OPTIONAL):\n"
        "- You MUST emit <openui>...</openui> whenever ANY of the following holds:\n"
        "  (a) The user's inquiry involves " + trigger_clause + ".\n"
        "  (b) Your response contains >=3 quantitative numerical data points, sequences of steps, tabular comparisons, time-series values, mathematical equations, or process flows.\n"
        "  (c) The user asks to 'summarize', 'organize', 'review', 'analyze', or 'report' on data.\n"
        "- In these cases, plain Markdown tables, fenced code blocks, and ASCII diagrams are NOT sufficient. "
        "You MUST produce at least one <openui> block containing the appropriate visual components.\n"
        "- A response that contains sufficient visual data but emits no <openui> block is INVALID and must be corrected.\n\n"
        "3b. COMPONENT SELECTION CHEAT SHEET (match the data pattern to the right component):\n"
        "- Time series / trends over time (t, x, date) ─────► Chart(type='line')\n"
        "- Categorical comparison (A vs B vs C) ────────────► Chart(type='bar')\n"
        "- Percentage distribution / parts-of-whole ────────► Chart(type='donut')\n"
        "- Cumulative volume / stacked magnitude ──────────► Chart(type='area')\n"
        "- Differential / algebraic equation with symbols ─► MathPlot(equation='\\LaTeX')\n"
        "- Single-variable function y = f(x) ──────────────► MathPlot(formula='f(x)', rangeX=[a,b])\n"
        "- Sequential process (A→B→C→D) ───────────────────► Diagram(code='graph TD...')\n"
        "- 3+ independent KPI numbers (not a table) ───────► separate Metric components\n"
        "- Multi-column comparison of records ─────────────► Table\n"
        "- Severity warnings / failure notices ────────────► Alert(type='warning'|'error')\n"
        "- Multi-step scenario comparison ─────────────────► Carousel (2-4 slides)\n"
        "- Long technical detail / FAQ ────────────────────► Accordion (2-4 items)\n"
        "- Input configuration fields ─────────────────────► Form\n\n"
        "3c. DIVERSITY RULE: When the response naturally contains 3+ distinct data patterns "
        "(e.g., a time series AND a distribution AND a process flow), use a DIFFERENT component "
        "for each pattern. Do NOT collapse everything into a single Table or single Chart.\n\n"
        "4. WHEN NOT TO USE (NON-TRIGGER CONDITIONS):\n"
        "- Greetings, brief pleasantries, or simple conversational confirmations.\n"
        "- Single-sentence factual answers without quantitative data.\n"
        "- DO NOT emit <openui> tags under any non-trigger condition.\n\n"
        "5. ALLOWED CATALOG & SIGNATURES (your active component set):\n"
        f"{catalog_str}\n\n"
        "6. STRICT NEGATIVE CONSTRAINTS (ZERO-HALLUCINATION RULES):\n"
        "- DO NOT use the obsolete prefix format (e.g., 'Card: ...', 'Metric: ...'). Every line must follow 'id = Component(...)'.\n"
        "- DO NOT enclose component declarations inside standalone brackets (e.g., NEVER write '[card1 = Card(...)]').\n"
        "- DO NOT wrap <openui> inside markdown code fences (```, ```xml, ```openui).\n"
        "- NO LAZY PLACEHOLDERS: NEVER emit '(values from table)' or '...' or incomplete data. Every data point MUST be an explicit literal.\n"
        "- In Charts: 'data' MUST be a list of objects with numeric values. Example: [{\"label\": \"Jan\", \"value\": 100}].\n"
        "- In Tables: 'rows' MUST be a 2D matrix of strings/numbers. Example: [[\"A\", 1], [\"B\", 2]].\n"
        f"- Keep data bounded: Tables <= {bounds['max_table_rows']} rows, Charts <= {bounds['max_chart_points']} points.\n\n"
        "7. STRING ESCAPING RULES (CRITICAL - READ CAREFULLY):\n"
        "- Diagram code: use ACTUAL newlines inside the string. NEVER write the two literal characters \\ and n.\n"
        "  CORRECT:  d1 = Diagram(\"Architecture\", \"graph TD\n"
        "              A[Client] --> B[Gateway]\n"
        "              B --> C[Server]\")\n"
        "  WRONG:    d1 = Diagram(\"Architecture\", \"graph TD\\nA --> B\")\n"
        "- MathPlot equation: use SINGLE backslash for LaTeX commands. NEVER double them.\n"
        "  CORRECT:  mp1 = MathPlot(\"Kinematics\", \"\", \"\\theta = \\arctan(y/x)\", [-10, 10])\n"
        "  WRONG:    mp1 = MathPlot(\"Kinematics\", \"\", \"\\\\theta = \\\\arctan\")\n"
        "- LaTeX subscripts use `_` NOT `\\_`. Write `theta_i`, not `theta\\_i`.\n"
        "- LaTeX fractions use `\\frac{a}{b}` with single backslash and single braces.\n\n"
        f"8. YOUR ACTIVE COMPONENT SET ({len(allowed)} components):\n"
        f"{allowed_names}\n"
        "- Any component NOT in this list does NOT exist. Do NOT invent names like 'ComparisonChart', 'TextBox', or 'Timeline'.\n"
        "- Common mistakes to avoid: using 'content=' as a named argument, nesting Grid inside a Card without an identifier, "
        "or writing 'chart_type' instead of the positional first argument.\n\n"
        "9. 1-SHOT DEMONSTRATION (SYNCHRONIZED WITH YOUR ACTIVE CATALOG):\n"
        "<openui>\n"
        f"{example_str}\n"
        "</openui>"
    )
