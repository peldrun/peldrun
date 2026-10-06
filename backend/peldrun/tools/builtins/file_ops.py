"""
backend/peldrun/tools/builtins/file_ops.py

PELDRUN Core Scoped File Operations Tool.
Provides safe, isolated filesystem interactions strictly bounded within the project workspace.
Hardened under PR 6 (Workspace Sandbox Containment & Artifact Lifecycle):
- Enforces strict path containment preventing directory traversal.
- Declares explicit ToolExecutionPolicy with side_effects=True and max_attempts=1.
- Populates ToolResult.artifacts with relative paths on write, append, and delete actions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Type
from pydantic import BaseModel, Field

from peldrun.security.policy import SecurityPolicy, SecurityViolationError
from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionPolicy

logger = logging.getLogger("peldrun.tools.builtins.file_ops")


class FileOpsArgs(BaseModel):
    """Input argument schema for workspace-scoped file operations."""
    action: Literal["read", "write", "append", "list", "exists", "delete"] = Field(
        ...,
        description="Filesystem action: 'read', 'write', 'append', 'list', 'exists', or 'delete'"
    )
    path: str = Field(
        ...,
        description="Relative file or directory path within the bounded workspace root"
    )
    content: Optional[str] = Field(
        default=None,
        description="Text content required when action is 'write' or 'append'"
    )
    encoding: str = Field(
        default="utf-8",
        description="Character encoding for text read/write operations (default: 'utf-8')"
    )


class FileOpsTool(BaseTool):
    """Core tool for performing secure, sandboxed file operations."""

    name: str = "file_ops"
    description: str = (
        "Perform safe file operations inside the project workspace. "
        "Supported actions: 'read' (reads file text), 'write' (creates/overwrites file), "
        "'append' (adds text to file), 'list' (lists directory items), "
        "'exists' (checks existence), and 'delete' (removes a file)."
    )
    args_schema: Optional[Type[BaseModel]] = FileOpsArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=30.0,
        retryable=False,
        idempotent=False,
        side_effects=True,
    )

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        security_policy: Optional[SecurityPolicy] = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root, execution_policy=self.execution_policy)
        if security_policy:
            self.security_policy = security_policy
        elif workspace_root:
            self.security_policy = SecurityPolicy(workspace_root=Path(workspace_root).resolve())
        else:
            self.security_policy = SecurityPolicy()

    def _resolve_safe_path(self, rel_path: str) -> Path:
        """Resolve path and verify strict containment within workspace_root."""
        if not self.workspace_root:
            raise PermissionError("Workspace root is not configured. File operations are blocked.")

        root = Path(self.workspace_root).resolve()
        candidate = Path(rel_path.strip())

        if candidate.is_absolute():
            target = candidate.resolve()
        else:
            target = (root / candidate).resolve()

        try:
            target.relative_to(root)
        except ValueError:
            raise PermissionError(f"Access denied: Path '{rel_path}' resolves outside workspace root '{root}'.")

        if self.security_policy.workspace_root != root:
            self.security_policy.workspace_root = root

        return self.security_policy.check_path_access(target)

    def _sync_read(self, path: Path, encoding: str) -> ToolResult:
        if not path.is_file():
            return ToolResult(
                output=f"File not found: '{path.name}'",
                exit_code=1,
                is_error=True,
            )
        try:
            content = path.read_text(encoding=encoding, errors="replace")
            return ToolResult(
                output=content,
                exit_code=0,
                is_error=False,
                metadata={"file_size_bytes": path.stat().st_size},
            )
        except Exception as ex:
            return ToolResult(
                output=f"Failed to read file: {str(ex)}",
                exit_code=1,
                is_error=True,
            )

    def _sync_write(self, path: Path, content: str, encoding: str) -> ToolResult:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding=encoding)
            rel_name = os.path.relpath(path, self.workspace_root).replace("\\", "/")
            return ToolResult(
                output=f"Successfully written {len(content)} characters to '{rel_name}'.",
                exit_code=0,
                is_error=False,
                artifacts=[rel_name],
                metadata={"bytes_written": path.stat().st_size, "operation": "created"},
            )
        except Exception as ex:
            return ToolResult(
                output=f"Failed to write file: {str(ex)}",
                exit_code=1,
                is_error=True,
            )

    def _sync_append(self, path: Path, content: str, encoding: str) -> ToolResult:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding=encoding) as f:
                f.write(content)
            rel_name = os.path.relpath(path, self.workspace_root).replace("\\", "/")
            return ToolResult(
                output=f"Successfully appended {len(content)} characters to '{rel_name}'.",
                exit_code=0,
                is_error=False,
                artifacts=[rel_name],
                metadata={"operation": "updated"},
            )
        except Exception as ex:
            return ToolResult(
                output=f"Failed to append to file: {str(ex)}",
                exit_code=1,
                is_error=True,
            )

    def _sync_list(self, path: Path) -> ToolResult:
        if not path.exists():
            return ToolResult(
                output=f"Directory does not exist: '{path.name}'",
                exit_code=1,
                is_error=True,
            )
        if not path.is_dir():
            return ToolResult(
                output=f"Path is not a directory: '{path.name}'",
                exit_code=1,
                is_error=True,
            )

        entries = []
        for item in sorted(path.iterdir()):
            item_type = "dir" if item.is_dir() else "file"
            size = item.stat().st_size if item.is_file() else 0
            entries.append({"name": item.name, "type": item_type, "size_bytes": size})

        return ToolResult(
            output=entries,
            exit_code=0,
            is_error=False,
            metadata={"item_count": len(entries)},
        )

    def _sync_exists(self, path: Path) -> ToolResult:
        exists = path.exists()
        is_file = path.is_file() if exists else False
        is_dir = path.is_dir() if exists else False
        return ToolResult(
            output={"exists": exists, "is_file": is_file, "is_dir": is_dir},
            exit_code=0,
            is_error=False,
        )

    def _sync_delete(self, path: Path) -> ToolResult:
        if not path.exists():
            return ToolResult(
                output=f"Cannot delete: target does not exist: '{path.name}'",
                exit_code=1,
                is_error=True,
            )
        try:
            rel_name = os.path.relpath(path, self.workspace_root).replace("\\", "/")
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
            return ToolResult(
                output=f"Successfully deleted '{rel_name}'.",
                exit_code=0,
                is_error=False,
                artifacts=[rel_name],
                metadata={"operation": "deleted"},
            )
        except Exception as ex:
            return ToolResult(
                output=f"Failed to delete '{path.name}': {str(ex)}",
                exit_code=1,
                is_error=True,
            )

    async def _arun(
        self,
        action: str,
        path: str,
        content: Optional[str] = None,
        encoding: str = "utf-8",
        **kwargs: Any,
    ) -> ToolResult:
        """Execute the requested file operation non-blockingly."""
        try:
            safe_target = self._resolve_safe_path(path)
        except (PermissionError, SecurityViolationError) as sec_err:
            return ToolResult(
                output=str(sec_err),
                exit_code=1,
                is_error=True,
                metadata={"error_type": type(sec_err).__name__},
            )

        if action == "read":
            return await asyncio.to_thread(self._sync_read, safe_target, encoding)
        elif action == "write":
            if content is None:
                return ToolResult(
                    output="Action 'write' requires 'content' parameter.",
                    exit_code=1,
                    is_error=True,
                )
            return await asyncio.to_thread(self._sync_write, safe_target, content, encoding)
        elif action == "append":
            if content is None:
                return ToolResult(
                    output="Action 'append' requires 'content' parameter.",
                    exit_code=1,
                    is_error=True,
                )
            return await asyncio.to_thread(self._sync_append, safe_target, content, encoding)
        elif action == "list":
            return await asyncio.to_thread(self._sync_list, safe_target)
        elif action == "exists":
            return await asyncio.to_thread(self._sync_exists, safe_target)
        elif action == "delete":
            return await asyncio.to_thread(self._sync_delete, safe_target)
        else:
            return ToolResult(
                output=f"Unsupported file operation action: '{action}'",
                exit_code=1,
                is_error=True,
            )


__all__ = [
    "FileOpsArgs",
    "FileOpsTool",
]