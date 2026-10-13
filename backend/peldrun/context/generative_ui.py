"""Peldrun rendering directive builder.

Builds a compact system directive describing which output formats the model
is allowed to use. The directive covers Markdown, Mermaid, LaTeX, and a
bounded JSON chart format. It intentionally does not describe any UI component
system; the frontend renders everything through a single Markdown pipeline.

The module exposes the same public surface as the previous OpenUI builder so
that `peldrun.context.builder` and both the chat and agent subsystems can call
it without modification.

Design goals:
    * Compact output (~250-350 tokens) suitable for small context windows.
    * Feature-gated: each format is only described when enabled.
    * Idempotent: re-running on the same settings yields the same string.
    * Forward-compatible: unknown keys in the settings dict are ignored.

Template note:
    `string.Template` is used instead of `str.format` for sections that
    contain literal `{` and `}` characters (JSON examples in the chart
    rules). Using `str.format` would raise a KeyError on those braces.
"""

from __future__ import annotations

import logging
from string import Template
from typing import Any, Dict, List, Optional

logger = logging.getLogger("peldrun.context.generative_ui")

# ---------------------------------------------------------------------------
# Limits and defaults
# ---------------------------------------------------------------------------

MIN_OUTPUT_TOKENS = 512
MAX_OUTPUT_TOKENS = 8192
DEFAULT_OUTPUT_TOKENS = 4096

MIN_RETRIES = 0
MAX_RETRIES = 5
DEFAULT_RETRIES = 1

# Default per-format limits. These can be overridden per-request via the
# `limits` sub-object of the settings dictionary.
DEFAULT_FEATURES: Dict[str, bool] = {
    "mermaid": True,
    "latex": True,
    "charts": True,
    "code_highlighting": True,
}

DEFAULT_LIMITS: Dict[str, int] = {
    "max_mermaid_nodes": 40,
    "max_chart_points": 30,
    "max_table_rows": 50,
    "max_output_tokens": DEFAULT_OUTPUT_TOKENS,
}

# Feature key aliases. Accept both snake_case and camelCase from callers.
FEATURE_ALIASES: Dict[str, str] = {
    "mermaid": "mermaid",
    "diagrams": "mermaid",
    "latex": "latex",
    "math": "latex",
    "charts": "charts",
    "chart": "charts",
    "code": "code_highlighting",
    "code_highlighting": "code_highlighting",
}

LIMIT_ALIASES: Dict[str, str] = {
    "max_mermaid_nodes": "max_mermaid_nodes",
    "maxMermaidNodes": "max_mermaid_nodes",
    "max_chart_points": "max_chart_points",
    "maxChartPoints": "max_chart_points",
    "max_table_rows": "max_table_rows",
    "maxTableRows": "max_table_rows",
    "max_output_tokens": "max_output_tokens",
    "maxOutputTokens": "max_output_tokens",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp_integer(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """Convert a value to an int and clamp it to [minimum, maximum]."""
    if isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_max_tokens(value: Any) -> int:
    """Return a safe output-token budget for the model call layer."""
    return _clamp_integer(
        value,
        minimum=MIN_OUTPUT_TOKENS,
        maximum=MAX_OUTPUT_TOKENS,
        default=DEFAULT_OUTPUT_TOKENS,
    )


def _is_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    """Return True only when the feature flag is explicitly enabled."""
    if not isinstance(settings, dict):
        return False
    return bool(settings.get("enabled"))


def _normalize_features(settings: Dict[str, Any]) -> Dict[str, bool]:
    """Resolve the effective feature flags, applying aliases and defaults."""
    raw = settings.get("features")
    if not isinstance(raw, dict):
        raw = {}

    resolved: Dict[str, bool] = dict(DEFAULT_FEATURES)
    for key, value in raw.items():
        canonical = FEATURE_ALIASES.get(key)
        if canonical is None:
            continue
        resolved[canonical] = bool(value)
    return resolved


def _normalize_limits(settings: Dict[str, Any]) -> Dict[str, int]:
    """Resolve the effective per-format limits, applying aliases and defaults."""
    raw = settings.get("limits")
    if not isinstance(raw, dict):
        raw = {}

    resolved: Dict[str, int] = dict(DEFAULT_LIMITS)
    for key, value in raw.items():
        canonical = LIMIT_ALIASES.get(key)
        if canonical is None:
            continue
        if canonical == "max_output_tokens":
            resolved[canonical] = _clamp_max_tokens(value)
        else:
            resolved[canonical] = _clamp_integer(
                value,
                minimum=1,
                maximum=500,
                default=DEFAULT_LIMITS[canonical],
            )

    # Top-level max_output_tokens overrides the nested value for backward
    # compatibility with the previous settings shape.
    if "max_output_tokens" in settings:
        resolved["max_output_tokens"] = _clamp_max_tokens(
            settings["max_output_tokens"]
        )

    return resolved


def _has_any_feature(features: Dict[str, bool]) -> bool:
    """True when at least one non-default rendering feature is active."""
    return any(
        features.get(key, False)
        for key in ("mermaid", "latex", "charts")
    )


# ---------------------------------------------------------------------------
# Directive sections
# ---------------------------------------------------------------------------

_BASE_RULES = (
    "[RENDERING POLICY]\n"
    "Respond in Markdown. Use the additional formats described below only "
    "when they materially improve the answer.\n"
    "\n"
    "General rules:\n"
    "- Never emit raw HTML tags such as <div>, <span>, <style>, <table>, "
    "or <svg>.\n"
    "- Do not use placeholder text or references such as \"see the table "
    "above\".\n"
    "- Never fabricate data to fill a visual. Use only values supplied by "
    "the user or available in tool results.\n"
    "- Keep headings, lists, and tables within Markdown conventions."
)

# Uses $max_nodes as a Template placeholder.
# Do not use {max_nodes}; braces are reserved for literal text.
_MERMAID_RULES = (
    "[MERMAID DIAGRAMS]\n"
    "- Emit diagrams inside fenced code blocks with the `mermaid` language "
    "tag.\n"
    "- Prefer the modern `flowchart LR` or `flowchart TD` directive.\n"
    "- Avoid the legacy `graph` directive, `classDef`, `class`, and "
    "`linkStyle`.\n"
    "- Node identifiers must be plain ASCII (letters, digits, underscore).\n"
    "- Do not embed HTML, JavaScript, or external URLs in node labels.\n"
    "- Supported diagram types: flowchart, sequenceDiagram, gantt, "
    "stateDiagram, classDiagram, erDiagram, journey, timeline, pie, mindmap.\n"
    "- Limit total nodes to at most $max_nodes."
)

_LATEX_RULES = (
    "[LATEX MATH]\n"
    "- Inline math: wrap expressions in single dollar signs, e.g. $E = mc^2$.\n"
    "- Display math: wrap expressions in double dollar signs on their own "
    "lines.\n"
    "- Never use \\[...\\] or \\(...\\) delimiters; they are not supported.\n"
    "- Avoid custom macros and \\newcommand definitions.\n"
    "- Prefer plain LaTeX primitives (\\frac, \\sum, \\int, \\left, \\right)."
)

# Uses $max_points as a Template placeholder.
# The JSON examples below contain literal { and } characters; they are NOT
# placeholders and must survive templating unchanged.
_CHART_RULES = (
    "[CHARTS]\n"
    "Two options are available. Choose the simpler one when possible.\n"
    "\n"
    "Option 1 - Mermaid xychart (recommended for simple line and bar data):\n"
    "```mermaid\n"
    "xychart-beta\n"
    "    title \"Monthly Revenue\"\n"
    "    x-axis [Jan, Feb, Mar, Apr]\n"
    "    y-axis \"USD (millions)\" 0 --> 100\n"
    "    bar [42, 55, 61, 74]\n"
    "```\n"
    "\n"
    "Option 2 - JSON chart (for multi-series, area, pie, or donut):\n"
    "```chart\n"
    "{\n"
    "  \"type\": \"line\",\n"
    "  \"title\": \"Revenue vs Cost\",\n"
    "  \"unit\": \"USD (millions)\",\n"
    "  \"data\": [\n"
    "    {\"label\": \"Q1\", \"revenue\": 42, \"cost\": 30},\n"
    "    {\"label\": \"Q2\", \"revenue\": 55, \"cost\": 35}\n"
    "  ],\n"
    "  \"xAxisKey\": \"label\",\n"
    "  \"seriesKeys\": [\"revenue\", \"cost\"]\n"
    "}\n"
    "```\n"
    "\n"
    "JSON chart constraints:\n"
    "- `type` must be one of: bar, line, area, donut, pie.\n"
    "- `data` must be a non-empty array of objects.\n"
    "- Limit total data points to at most $max_points."
)

_CODE_RULES = (
    "[CODE BLOCKS]\n"
    "- Always include a language tag on fenced code blocks.\n"
    "- For plain text content that must preserve whitespace (for example a "
    "cover page layout), use the `text` language tag or no tag at all.\n"
    "- Do not wrap ordinary prose in a code block."
)


def _build_sections(
    features: Dict[str, bool],
    limits: Dict[str, int],
) -> List[str]:
    """Assemble the directive sections based on the active features.

    Uses `string.Template` instead of `str.format` because the chart rules
    contain literal `{` and `}` characters (JSON examples) that would be
    interpreted as format placeholders by `str.format` and raise a
    KeyError at runtime.
    """
    sections: List[str] = [_BASE_RULES]

    if features.get("mermaid"):
        template = Template(_MERMAID_RULES)
        sections.append(
            template.safe_substitute(
                max_nodes=limits["max_mermaid_nodes"],
            )
        )

    if features.get("latex"):
        sections.append(_LATEX_RULES)

    if features.get("charts"):
        template = Template(_CHART_RULES)
        sections.append(
            template.safe_substitute(
                max_points=limits["max_chart_points"],
            )
        )

    if features.get("code_highlighting"):
        sections.append(_CODE_RULES)

    return sections


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_max_output_tokens(settings: Optional[Dict[str, Any]]) -> int:
    """Return the effective output-token budget for the current request."""
    if not _is_enabled(settings):
        return DEFAULT_OUTPUT_TOKENS
    assert isinstance(settings, dict)
    limits = _normalize_limits(settings)
    return limits["max_output_tokens"]


def get_max_retries(settings: Optional[Dict[str, Any]]) -> int:
    """Return the bounded retry count for the caller's parse policy."""
    if not _is_enabled(settings):
        return 0
    assert isinstance(settings, dict)
    return _clamp_integer(
        settings.get("max_retries"),
        minimum=MIN_RETRIES,
        maximum=MAX_RETRIES,
        default=DEFAULT_RETRIES,
    )


def build_generative_ui_prompt(settings: Optional[Dict[str, Any]]) -> str:
    """Build the rendering directive for the active configuration.

    Returns an empty string when the feature is disabled or when no
    rendering feature is enabled. The output is intentionally compact so it
    can be prepended to the system message without crowding the context
    window on small models.

    The function is safe to call from both the chat subsystem and the agent
    subsystem; it has no side effects and does not depend on request state
    beyond the settings dictionary.
    """
    if not _is_enabled(settings):
        return ""

    assert isinstance(settings, dict)
    features = _normalize_features(settings)
    limits = _normalize_limits(settings)

    if not _has_any_feature(features):
        # Feature toggle is on but every rendering feature is off; emit
        # nothing so the model defaults to plain Markdown.
        logger.debug("Rendering directive requested but no feature is active")
        return ""

    sections = _build_sections(features, limits)
    return "\n\n".join(sections)


__all__ = [
    "DEFAULT_FEATURES",
    "DEFAULT_LIMITS",
    "MAX_OUTPUT_TOKENS",
    "MIN_OUTPUT_TOKENS",
    "build_generative_ui_prompt",
    "get_max_output_tokens",
    "get_max_retries",
]
