"""
PELDRUN Core String Replace File Editor Tool.
Provides fine-grained file inspection, creation, string replacement, insertion, and undo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult


class EditorArgs(BaseModel):
    """Pydantic schema defining arguments for StrReplaceEditor."""
    command: str = Field(
        ...,
        description="The editing command to perform: 'view', 'create', 'str_replace', 'insert', or 'undo'.",
    )
    path: str = Field(
        ...,
        description="Relative or absolute path to the file.",
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


# Backward-compatibility alias
EditorParameters = EditorArgs


class StrReplaceEditor(BaseTool):
    """File editing tool supporting view, create, string replacement, and insertion."""

    name: str = "str_replace_editor"
    description: str = "Custom file editor for viewing, creating, and editing files with exact string replacement."
    args_schema = EditorArgs

    def __init__(self, workspace_root: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(workspace_root=workspace_root)
        self.workspace_root = workspace_root or os.getcwd()
        self._history: Dict[str, List[str]] = {}

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            full_path = path
        else:
            full_path = os.path.join(self.workspace_root or os.getcwd(), path)
        return os.path.abspath(full_path)

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
        """Internal asynchronous implementation satisfying BaseTool abstract contract."""
        target_path = self._resolve_path(path)

        try:
            if command == "create":
                if file_text is None:
                    return ToolResult(output="", exit_code=1, is_error=True, metadata={"error": "file_text required"})
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(file_text)
                return ToolResult(output=f"File created successfully at {path}", exit_code=0, is_error=False)

            if not os.path.isfile(target_path):
                return ToolResult(output=f"File not found: {path}", exit_code=1, is_error=True)

            with open(target_path, "r", encoding="utf-8") as f:
                content = f.read()

            if command == "view":
                lines = content.splitlines(keepends=True)
                if view_range and len(view_range) == 2:
                    start, end = max(1, view_range[0]), min(len(lines), view_range[1])
                    selected = lines[start - 1 : end]
                    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=start)]
                    return ToolResult(output="".join(numbered), exit_code=0, is_error=False)
                numbered = [f"{i}: {line}" for i, line in enumerate(lines, start=1)]
                return ToolResult(output="".join(numbered), exit_code=0, is_error=False)

            elif command == "str_replace":
                if old_str is None or new_str is None:
                    return ToolResult(output="old_str and new_str required for str_replace", exit_code=1, is_error=True)
                count = content.count(old_str)
                if count == 0:
                    return ToolResult(output=f"Target string not found in {path}", exit_code=1, is_error=True)
                if count > 1:
                    return ToolResult(output=f"Target string appears {count} times. Provide unique context.", exit_code=1, is_error=True)

                self._history.setdefault(target_path, []).append(content)
                new_content = content.replace(old_str, new_str, 1)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                return ToolResult(output=f"Successfully replaced string in {path}", exit_code=0, is_error=False)

            elif command == "insert":
                if insert_line is None or new_str is None:
                    return ToolResult(output="insert_line and new_str required for insert", exit_code=1, is_error=True)
                lines = content.splitlines(keepends=True)
                if insert_line < 0 or insert_line > len(lines):
                    return ToolResult(output=f"Invalid insert_line: {insert_line}", exit_code=1, is_error=True)

                self._history.setdefault(target_path, []).append(content)
                insert_text = new_str if new_str.endswith("\n") else new_str + "\n"
                lines.insert(insert_line, insert_text)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                return ToolResult(output=f"Successfully inserted text at line {insert_line} in {path}", exit_code=0, is_error=False)

            elif command == "undo":
                history = self._history.get(target_path, [])
                if not history:
                    return ToolResult(output=f"No edit history available for {path}", exit_code=1, is_error=True)
                prev = history.pop()
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(prev)
                return ToolResult(output=f"Successfully reverted last edit in {path}", exit_code=0, is_error=False)

            return ToolResult(output=f"Unsupported command '{command}'", exit_code=1, is_error=True)

        except Exception as exc:
            return ToolResult(output=f"Editor error: {str(exc)}", exit_code=1, is_error=True)


__all__ = [
    "EditorArgs",
    "EditorParameters",
    "StrReplaceEditor",
]