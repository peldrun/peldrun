"""
Canonical tool JSON schemas for the PELDRUN core adapter layer.

These schemas guarantee that every LLM (local or remote) sees explicit,
non-empty parameter definitions, so it never attempts to call a tool
with an empty argument object.
"""

from __future__ import annotations

from typing import Any, Dict


# Standard schemas ensuring local and remote LLMs receive explicit parameters
# and never call empty functions.
CANONICAL_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "bash": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command-line instruction to execute in the workspace."
            }
        },
        "required": ["command"]
    },
    "shell_exec": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute inside the workspace sandbox."
            },
            "timeout": {
                "type": "number",
                "description": "Optional execution timeout in seconds (default: 60.0)."
            }
        },
        "required": ["command"]
    },
    "python_execute": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python source code to execute inside the workspace."
            }
        },
        "required": ["code"]
    },
    "str_replace_editor": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                "description": "The editor command to run."
            },
            "path": {
                "type": "string",
                "description": "Relative file path within workspace."
            },
            "file_text": {
                "type": "string",
                "description": "File contents for 'create' command."
            },
            "old_str": {
                "type": "string",
                "description": "String to replace for 'str_replace' command."
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string for 'str_replace' command."
            }
        },
        "required": ["command", "path"]
    },
    "web_search": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query terms to look up on the live web."
            },
            "max_results": {
                "type": "integer",
                "default": 5,
                "description": "Maximum number of search results to retrieve (default: 5)."
            }
        },
        "required": ["query"]
    },
    "browser_use": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The target website URL to visit, inspect, or extract content from."
            },
            "action": {
                "type": "string",
                "enum": ["navigate", "extract_content", "click", "scroll_down"],
                "default": "extract_content",
                "description": "The browser operation to perform on the target web page."
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector or keyword to target specific elements on the page."
            }
        },
        "required": ["url"]
    },
    "ask_human": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The question, confirmation, or clarification needed from the human user."
            },
            "input_type": {
                "type": "string",
                "enum": ["text", "confirm", "select", "multiple_choice"],
                "default": "text",
                "description": "Interaction type: 'text' (input field), 'confirm' (Yes/No buttons), 'select' (choice buttons)."
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of choice buttons for the user to select from (e.g. ['Yes', 'No'] or custom options)."
            },
            "timeout_seconds": {
                "type": "integer",
                "default": 600,
                "description": "Maximum wait time in seconds for the user to respond."
            }
        },
        "required": ["prompt"]
    }
}