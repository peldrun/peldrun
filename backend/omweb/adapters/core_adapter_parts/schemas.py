# backend/omweb/adapters/core_adapter_parts/schemas.py
"""
Canonical JSON Schemas for PELDRUN Web-to-Core Builtin Tools.

Synchronized with peldrun-core tool argument contracts (Phase 3 — SR-301..SR-305):
- `web_search.properties.max_results` is strictly optional (`required: ["query"]`),
  has no hard-coded `"default": 5`, and uses the centralized bounds
  `MIN_RESULTS_LIMIT` (1) .. `MAX_RESULTS_HARD_CAP` (20).
- `http_fetch` replaces the deprecated `browser_use`. It declares a
  single-capability contract: fetch a URL and return visible text.
  It does not declare `action`, `click`, `scroll`, or `screenshot`,
  because those capabilities are NOT implemented by the tool.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

from peldrun.tools.builtins.search_providers.factory import (
    MAX_RESULTS_HARD_CAP,
    MIN_RESULTS_LIMIT,
)

CANONICAL_TOOL_DESCRIPTIONS: Dict[str, str] = {
    "python_execute": (
        "Execute Python code in an isolated workspace environment and return stdout/stderr."
    ),
    "bash": (
        "Execute a bash or shell command inside the active workspace directory."
    ),
    "file_saver": (
        "Create or overwrite a file inside the active workspace directory."
    ),
    "file_reader": (
        "Read the contents of a file from the active workspace directory."
    ),
    "str_replace_editor": (
        "View, create, and edit files in the workspace using exact string replacements or line insertions."
    ),
    "web_search": (
        "Search the web for up-to-date information, documentation, news, and factual verification. "
        "Omit max_results to use PELDRUN Search Settings."
    ),
    "http_fetch": (
        "Fetch static HTML content from a URL and extract its readable text. "
        "Performs a single HTTP GET, follows redirects, strips scripts and styles, "
        "and returns the visible text. Does NOT execute JavaScript or interact "
        "with the page. Use 'browser_interactive' for dynamic pages."
    ),
    "ask_human": (
        "Ask the human operator a clarifying question or request approval when blocked."
    ),
}

CANONICAL_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "python_execute": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Valid Python source code to execute in the workspace.",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 600,
                "description": "Optional execution timeout in seconds.",
            },
        },
        "required": ["code"],
    },
    "bash": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute inside the workspace.",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 600,
                "description": "Optional command timeout in seconds.",
            },
        },
        "required": ["command"],
    },
    "file_saver": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Relative path of the file inside the workspace.",
            },
            "content": {
                "type": "string",
                "description": "Full text content to write to the file.",
            },
            "mode": {
                "type": "string",
                "enum": ["w", "a"],
                "description": "Write mode: 'w' to overwrite (default) or 'a' to append.",
            },
        },
        "required": ["file_path", "content"],
    },
    "file_reader": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Relative path of the workspace file to read.",
            },
        },
        "required": ["file_path"],
    },
    "str_replace_editor": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                "description": "Editor operation to perform.",
            },
            "path": {
                "type": "string",
                "description": "Relative file or directory path inside the workspace.",
            },
            "file_text": {
                "type": "string",
                "description": "Full file content when command is 'create'.",
            },
            "old_str": {
                "type": "string",
                "description": "Exact string in the file to replace when command is 'str_replace'.",
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string for 'str_replace' or text to insert for 'insert'.",
            },
            "insert_line": {
                "type": "integer",
                "minimum": 0,
                "description": "Line number after which new_str is inserted when command is 'insert'.",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Optional [start_line, end_line] range when command is 'view'.",
            },
        },
        "required": ["command", "path"],
    },
    "web_search": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Search query string to look up on the web.",
            },
            "max_results": {
                "type": "integer",
                "minimum": MIN_RESULTS_LIMIT,
                "maximum": MAX_RESULTS_HARD_CAP,
                "description": (
                    "Optional desired maximum number of results. Omit this argument to use "
                    "PELDRUN Search Settings. The configured provider ceiling and provider "
                    "capability maximum are always enforced."
                ),
            },
        },
        "required": ["query"],
    },
    "http_fetch": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "format": "uri",
                "description": "Target HTTP or HTTPS URL to fetch.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 500,
                "maximum": 50000,
                "description": (
                    "Optional maximum number of visible text characters to return. "
                    "Defaults to 8000; range 500..50000."
                ),
            },
        },
        "required": ["url"],
    },
    "ask_human": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "description": "Clear question or confirmation prompt presented to the user.",
            },
        },
        "required": ["question"],
    },
}


def get_canonical_tool_schema(tool_name: str) -> Dict[str, Any]:
    """Return a deep copy of the canonical JSON schema for a builtin tool."""
    key = (tool_name or "").strip().lower()
    schema = CANONICAL_TOOL_SCHEMAS.get(key)
    if schema is None:
        return {"type": "object", "properties": {}, "required": []}
    return copy.deepcopy(schema)


def get_canonical_tool_description(tool_name: str) -> str:
    """Return the canonical description for a builtin tool."""
    key = (tool_name or "").strip().lower()
    return CANONICAL_TOOL_DESCRIPTIONS.get(key, f"Execute {key} tool.")


__all__ = [
    "CANONICAL_TOOL_DESCRIPTIONS",
    "CANONICAL_TOOL_SCHEMAS",
    "get_canonical_tool_description",
    "get_canonical_tool_schema",
]
