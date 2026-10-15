"""
backend/peldrun/tools/builtins/str_replace_editor.py

PELDRUN Core Hardened String Replace File Editor Tool.
Provides fine-grained file inspection, creation, exact string replacement, insertion, and undo.
Hardened under PR 6 (Workspace Sandbox Containment & Artifact Lifecycle):
- Enforces strict workspace containment; blocks directory traversal and absolute path escapes.
- Declares explicit ToolExecutionPolicy with side_effects=True and max_attempts=1.
- Emits created and updated file deliverables directly in ToolResult.artifacts.
- Provides backward-compatible alias StrReplaceEditorTool.

Hardened under LLM-Robustness pass:
- [FIX] Sanitizes malformed 'view_range' values produced by non-Anthropic LLMs
        (e.g. GigaChat emitting '[1, 5000000000000...], ' as a string).
- [FIX] Interprets view_range end = -1 as "read until end of file" (Anthropic convention),
        instead of silently dropping the last line.
- [FIX] Coerces 'insert_line' from string/float into int when the LLM mis-types it.
- [FIX] Enriched field descriptions with explicit examples to reduce schema violations.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionPolicy


# Sentinel used internally for "read until end of file"
_READ_TO_EOF = -1
_MAX_REASONABLE_LINE = 10_000_000  # anything above this is treated as EOF sentinel


class EditorArgs(BaseModel):
    """Pydantic schema defining arguments for StrReplaceEditor."""

    command: str = Field(
        ...,
        description=(
            "The editing command to perform. Must be exactly one of: "
            "'view', 'create', 'str_replace', 'insert', 'undo'."
        ),
    )
    path: str = Field(
        ...,
        description=(
            "Relative path to the file within the workspace. "
            "Example: 'report.md' or 'notes/todo.txt'. "
            "Do NOT use absolute paths or '..'."
        ),
    )
    file_text: Optional[str] = Field(
        default=None,
        description="Required for 'create'. The full content of the new file.",
    )
    old_str: Optional[str] = Field(
        default=None,
        description=(
            "Required for 'str_replace'. The exact text to replace; "
            "it must appear exactly once in the file."
        ),
    )
    new_str: Optional[str] = Field(
        default=None,
        description="Optional for 'str_replace' and 'insert'. The replacement or inserted text.",
    )
    insert_line: Optional[int] = Field(
        default=None,
        description=(
            "Required for 'insert'. The line number AFTER which the text is inserted. "
            "Must be an integer between 0 and the file's line count. "
            "Example: insert_line=0 inserts at the very top of the file."
        ),
    )
    view_range: Optional[List[int]] = Field(
        default=None,
        description=(
            "Optional, ONLY for 'view'. A list of EXACTLY TWO INTEGERS: [start_line, end_line]. "
            "Lines are 1-indexed. Use end_line = -1 to view until end of file. "
            "Integers only — never strings, never very large numbers. "
            "Examples: [1, 50]  |  [10, -1]  |  [1, -1] (entire file)."
        ),
    )

    # ------------------------------------------------------------------ #
    # [FIX] Defensive coercion of LLM-produced malformed arguments.      #
    # ------------------------------------------------------------------ #

    @field_validator("view_range", mode="before")
    @classmethod
    def _coerce_view_range(cls, v: Any) -> Any:
        """
        Sanitize malformed view_range values.

        Handles cases seen in production:
          - "[1, 5000000000000...], " (string with noise)
          - [1, "5000"]                (list with string)
          - ["1", "-1"]                (all strings)
          - 5                          (single number instead of list)
          - None                       (passthrough)
        """
        if v is None:
            return None

        # Case 1: whole value arrived as a raw string
        if isinstance(v, str):
            nums = re.findall(r"-?\d+", v)
            if not nums:
                return None
            parsed = [int(n) for n in nums[:2]]
            return cls._normalize_range(parsed)

        # Case 2: single number (not wrapped in a list)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return cls._normalize_range([int(v)])

        # Case 3: list / tuple with mixed element types
        if isinstance(v, (list, tuple)):
            cleaned: List[int] = []
            for item in v:
                try:
                    cleaned.append(int(item))
                except (ValueError, TypeError):
                    # Broken element → treat as "until EOF"
                    cleaned.append(_READ_TO_EOF)
            return cls._normalize_range(cleaned)

        return v

    @staticmethod
    def _normalize_range(nums: List[int]) -> List[int]:
        """Apply consistent bounds and sentinel rules to a 2-element range."""
        if not nums:
            return [_READ_TO_EOF, _READ_TO_EOF]

        if len(nums) == 1:
            nums = [nums[0], _READ_TO_EOF]

        nums = nums[:2]

        # Start must be >= 1
        if nums[0] < 1:
            nums[0] = 1

        # Absurd / negative end values collapse to "until EOF"
        if nums[1] > _MAX_REASONABLE_LINE or nums[1] < -1:
            nums[1] = _READ_TO_EOF

        return nums

    @field_validator("insert_line", mode="before")
    @classmethod
    def _coerce_insert_line(cls, v: Any) -> Any:
        """Coerce insert_line from string/float into int when the LLM mis-types it."""
        if v is None:
            return None
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, str):
            m = re.search(r"-?\d+", v)
            return int(m.group()) if m else None
        if isinstance(v, float):
            return int(v)
        return v


# Backward-compatibility aliases
EditorParameters = EditorArgs


class StrReplaceEditor(BaseTool):
    """File editing tool supporting view, create, string replacement, and insertion."""

    name: str = "str_replace_editor"
    description: str = (
        "Custom file editor for viewing, creating, and editing files with exact string replacement."
    )
    args_schema = EditorArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=30.0,
        retryable=False,
        idempotent=False,
        side_effects=True,
    )

    def __init__(self, workspace_root: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(workspace_root=workspace_root, execution_policy=self.execution_policy)
        self.workspace_root = workspace_root or os.getcwd()
        self._history: Dict[str, List[str]] = {}

    def _resolve_safe_path(self, path: str) -> Path:
        """
        Strictly resolve and validate path containment within workspace_root.
        Rejects directory traversal (../) and absolute drive path escapes.
        """
        if not path or not path.strip():
            raise ValueError("Parameter 'path' cannot be empty.")

        root = Path(self.workspace_root or os.getcwd()).resolve()
        raw_path = path.strip()

        # Handle absolute paths vs relative paths safely
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (root / raw_path).resolve()

        # Enforce containment: resolved target must be strictly inside root
        try:
            resolved.relative_to(root)
        except ValueError:
            raise PermissionError(
                f"Access denied: Path '{path}' resolves to '{resolved}', "
                f"which is outside workspace root '{root}'."
            )

        if resolved == root:
            raise PermissionError(
                "File operations directly targeting the workspace root directory are forbidden."
            )

        return resolved

    async def _arun(
        self,
        command: str,
        path: str,
        file_text: Optional[str] = None,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        insert_line: Optional[int] = None,
        view_range: Optional[List[int]] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute sandboxed editor operations and record mutated artifact paths."""
        try:
            target_path = self._resolve_safe_path(path)
        except (ValueError, PermissionError) as path_err:
            return ToolResult(
                output=str(path_err),
                exit_code=1,
                is_error=True,
                metadata={"error_type": type(path_err).__name__},
            )

        root = Path(self.workspace_root or os.getcwd()).resolve()
        rel_str = str(target_path.relative_to(root)).replace("\\", "/")
        path_key = str(target_path)

        try:
            # 1. CREATE COMMAND
            if command == "create":
                if file_text is None:
                    return ToolResult(
                        output="Parameter 'file_text' is required for 'create' command.",
                        exit_code=1,
                        is_error=True,
                    )
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(file_text, encoding="utf-8")
                size_bytes = target_path.stat().st_size
                return ToolResult(
                    output=f"File created successfully at {rel_str} ({size_bytes} bytes).",
                    exit_code=0,
                    is_error=False,
                    artifacts=[rel_str],
                    metadata={
                        "operation": "created",
                        "path": rel_str,
                        "size_bytes": size_bytes,
                    },
                )

            # 2. FILE EXISTENCE CHECK FOR REMAINING COMMANDS
            if not target_path.is_file():
                return ToolResult(
                    output=f"File not found: '{rel_str}'",
                    exit_code=1,
                    is_error=True,
                )

            content = target_path.read_text(encoding="utf-8")

            # 3. VIEW COMMAND  [FIX] now honors end = -1 as "until EOF"
            if command == "view":
                lines = content.splitlines(keepends=True)
                total = len(lines)

                if view_range and len(view_range) == 2:
                    start_raw, end_raw = view_range[0], view_range[1]

                    start = max(1, start_raw)

                    if end_raw == _READ_TO_EOF:
                        end = total
                    else:
                        end = min(total, end_raw)

                    # Guard against inverted ranges produced by the LLM
                    if end < start:
                        end = start

                    selected = lines[start - 1 : end]
                    numbered = [
                        f"{i}: {line}"
                        for i, line in enumerate(selected, start=start)
                    ]
                    return ToolResult(
                        output="".join(numbered),
                        exit_code=0,
                        is_error=False,
                        metadata={
                            "operation": "view",
                            "path": rel_str,
                            "start": start,
                            "end": end,
                            "total_lines": total,
                        },
                    )

                # No range provided → return the whole file
                numbered = [
                    f"{i}: {line}" for i, line in enumerate(lines, start=1)
                ]
                return ToolResult(
                    output="".join(numbered),
                    exit_code=0,
                    is_error=False,
                    metadata={
                        "operation": "view",
                        "path": rel_str,
                        "total_lines": total,
                    },
                )

            # 4. STRING REPLACE COMMAND
            elif command == "str_replace":
                if old_str is None or new_str is None:
                    return ToolResult(
                        output="Parameters 'old_str' and 'new_str' are required for 'str_replace'.",
                        exit_code=1,
                        is_error=True,
                    )
                count = content.count(old_str)
                if count == 0:
                    return ToolResult(
                        output=f"Target string not found in '{rel_str}'.",
                        exit_code=1,
                        is_error=True,
                    )
                if count > 1:
                    return ToolResult(
                        output=(
                            f"Target string appears {count} times in '{rel_str}'. "
                            "Provide unique contextual lines."
                        ),
                        exit_code=1,
                        is_error=True,
                    )

                self._history.setdefault(path_key, []).append(content)
                new_content = content.replace(old_str, new_str, 1)
                target_path.write_text(new_content, encoding="utf-8")
                size_bytes = target_path.stat().st_size
                return ToolResult(
                    output=f"Successfully replaced string in '{rel_str}'.",
                    exit_code=0,
                    is_error=False,
                    artifacts=[rel_str],
                    metadata={
                        "operation": "updated",
                        "path": rel_str,
                        "size_bytes": size_bytes,
                    },
                )

            # 5. INSERT COMMAND
            elif command == "insert":
                if insert_line is None or new_str is None:
                    return ToolResult(
                        output="Parameters 'insert_line' and 'new_str' are required for 'insert'.",
                        exit_code=1,
                        is_error=True,
                    )

                # Defensive: insert_line may still arrive as None or bad int
                try:
                    insert_line_int = int(insert_line)
                except (ValueError, TypeError):
                    return ToolResult(
                        output=f"Invalid insert_line value: {insert_line!r}",
                        exit_code=1,
                        is_error=True,
                    )

                lines = content.splitlines(keepends=True)
                if insert_line_int < 0 or insert_line_int > len(lines):
                    return ToolResult(
                        output=(
                            f"Invalid insert_line: {insert_line_int}. "
                            f"File has {len(lines)} lines."
                        ),
                        exit_code=1,
                        is_error=True,
                    )

                self._history.setdefault(path_key, []).append(content)
                insert_text = new_str if new_str.endswith("\n") else new_str + "\n"
                lines.insert(insert_line_int, insert_text)
                target_path.write_text("".join(lines), encoding="utf-8")
                size_bytes = target_path.stat().st_size
                return ToolResult(
                    output=f"Successfully inserted text at line {insert_line_int} in '{rel_str}'.",
                    exit_code=0,
                    is_error=False,
                    artifacts=[rel_str],
                    metadata={
                        "operation": "updated",
                        "path": rel_str,
                        "size_bytes": size_bytes,
                    },
                )

            # 6. UNDO COMMAND
            elif command == "undo":
                history = self._history.get(path_key, [])
                if not history:
                    return ToolResult(
                        output=f"No edit history available to undo for '{rel_str}'.",
                        exit_code=1,
                        is_error=True,
                    )
                prev = history.pop()
                target_path.write_text(prev, encoding="utf-8")
                size_bytes = target_path.stat().st_size
                return ToolResult(
                    output=f"Successfully reverted last edit in '{rel_str}'.",
                    exit_code=0,
                    is_error=False,
                    artifacts=[rel_str],
                    metadata={
                        "operation": "updated",
                        "path": rel_str,
                        "size_bytes": size_bytes,
                    },
                )

            return ToolResult(
                output=f"Unsupported editor command: '{command}'",
                exit_code=1,
                is_error=True,
            )

        except Exception as exc:
            return ToolResult(
                output=f"Editor execution error on '{rel_str}': {str(exc)}",
                exit_code=1,
                is_error=True,
                metadata={"error_type": type(exc).__name__},
            )


# Backward-compatibility alias
StrReplaceEditorTool = StrReplaceEditor

__all__ = [
    "EditorArgs",
    "EditorParameters",
    "StrReplaceEditor",
    "StrReplaceEditorTool",
]
