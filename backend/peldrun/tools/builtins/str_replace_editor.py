"""
backend/peldrun/tools/builtins/str_replace_editor.py

PELDRUN Core Hardened String Replace File Editor Tool.
Provides fine-grained file inspection, creation, exact string replacement, insertion, and undo.
Hardened under PR 6 (Workspace Sandbox Containment & Artifact Lifecycle):
- Enforces strict workspace containment; blocks directory traversal and absolute path escapes.
- Declares explicit ToolExecutionPolicy with side_effects=True and max_attempts=1.
- Emits created and updated file deliverables directly in ToolResult.artifacts.
- Provides backward-compatible alias StrReplaceEditorTool.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionPolicy


class EditorArgs(BaseModel):
    """Pydantic schema defining arguments for StrReplaceEditor."""
    command: str = Field(
        ...,
        description="The editing command to perform: 'view', 'create', 'str_replace', 'insert', or 'undo'.",
    )
    path: str = Field(
        ...,
        description="Relative path to the file within the workspace.",
    )
    file_text: Optional[str] = Field(
        default=None,
        description="Required for 'create'. The content of the file to create.",
    )
    old_str: Optional[str] = Field(
        default=None,
        description="Required for 'str_replace'. The exact text to be replaced.",
    )
    new_str: Optional[str] = Field(
        default=None,
        description="Optional for 'str_replace' and 'insert'. The text to replace with or insert.",
    )
    insert_line: Optional[int] = Field(
        default=None,
        description="Required for 'insert'. The line number after which to insert the text.",
    )
    view_range: Optional[List[int]] = Field(
        default=None,
        description="Optional for 'view'. Tuple/list of [start_line, end_line].",
    )


# Backward-compatibility aliases
EditorParameters = EditorArgs


class StrReplaceEditor(BaseTool):
    """File editing tool supporting view, create, string replacement, and insertion."""

    name: str = "str_replace_editor"
    description: str = "Custom file editor for viewing, creating, and editing files with exact string replacement."
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
                f"Access denied: Path '{path}' resolves to '{resolved}', which is outside workspace root '{root}'."
            )

        if resolved == root:
            raise PermissionError("File operations directly targeting the workspace root directory are forbidden.")

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
                    metadata={"operation": "created", "path": rel_str, "size_bytes": size_bytes},
                )

            # 2. FILE EXISTENCE CHECK FOR REMAINING COMMANDS
            if not target_path.is_file():
                return ToolResult(
                    output=f"File not found: '{rel_str}'",
                    exit_code=1,
                    is_error=True,
                )

            content = target_path.read_text(encoding="utf-8")

            # 3. VIEW COMMAND
            if command == "view":
                lines = content.splitlines(keepends=True)
                if view_range and len(view_range) == 2:
                    start, end = max(1, view_range[0]), min(len(lines), view_range[1])
                    selected = lines[start - 1 : end]
                    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=start)]
                    return ToolResult(
                        output="".join(numbered),
                        exit_code=0,
                        is_error=False,
                        metadata={"operation": "view", "path": rel_str},
                    )
                numbered = [f"{i}: {line}" for i, line in enumerate(lines, start=1)]
                return ToolResult(
                    output="".join(numbered),
                    exit_code=0,
                    is_error=False,
                    metadata={"operation": "view", "path": rel_str},
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
                        output=f"Target string appears {count} times in '{rel_str}'. Provide unique contextual lines.",
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
                    metadata={"operation": "updated", "path": rel_str, "size_bytes": size_bytes},
                )

            # 5. INSERT COMMAND
            elif command == "insert":
                if insert_line is None or new_str is None:
                    return ToolResult(
                        output="Parameters 'insert_line' and 'new_str' are required for 'insert'.",
                        exit_code=1,
                        is_error=True,
                    )
                lines = content.splitlines(keepends=True)
                if insert_line < 0 or insert_line > len(lines):
                    return ToolResult(
                        output=f"Invalid insert_line: {insert_line}. File has {len(lines)} lines.",
                        exit_code=1,
                        is_error=True,
                    )

                self._history.setdefault(path_key, []).append(content)
                insert_text = new_str if new_str.endswith("\n") else new_str + "\n"
                lines.insert(insert_line, insert_text)
                target_path.write_text("".join(lines), encoding="utf-8")
                size_bytes = target_path.stat().st_size
                return ToolResult(
                    output=f"Successfully inserted text at line {insert_line} in '{rel_str}'.",
                    exit_code=0,
                    is_error=False,
                    artifacts=[rel_str],
                    metadata={"operation": "updated", "path": rel_str, "size_bytes": size_bytes},
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
                    metadata={"operation": "updated", "path": rel_str, "size_bytes": size_bytes},
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