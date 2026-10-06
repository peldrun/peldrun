"""
PELDRUN Core Shell Execution Tool.
Executes shell commands strictly bounded within the project workspace,
enforcing directory containment policies and satisfying BaseTool abstract contracts.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult


class ShellExecArgs(BaseModel):
    """Pydantic schema defining arguments for ShellExecTool."""
    command: str = Field(..., description="The command-line instruction to execute.")
    timeout: Optional[float] = Field(default=60.0, description="Execution timeout in seconds.")
    working_subdir: Optional[str] = Field(default=None, description="Optional relative directory to execute within.")


# Backward-compatibility alias
ShellExecParameters = ShellExecArgs


class ShellExecTool(BaseTool):
    """
    Executes shell commands safely bounded within the project workspace root.
    Implements abstract _arun method from BaseTool.
    """

    name: str = "shell_exec"
    description: str = "Executes command-line instructions safely bounded within the project workspace."
    args_schema = ShellExecArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.workspace_root = workspace_root or os.getcwd()

    async def _arun(
        self,
        command: str,
        timeout: Optional[float] = 60.0,
        working_subdir: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Internal asynchronous shell execution satisfying BaseTool contract.
        Verifies that working directory cannot escape workspace boundary.
        """
        root_path = Path(self.workspace_root).resolve()
        target_dir = root_path

        if working_subdir:
            candidate = (root_path / working_subdir).resolve()
            try:
                candidate.relative_to(root_path)
                target_dir = candidate
            except ValueError:
                return ToolResult(
                    output="Access Denied: Working directory escapes workspace boundary.",
                    exit_code=1,
                    is_error=True,
                )

        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(target_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timeout_val = float(timeout) if timeout else 60.0
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_val
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return ToolResult(
                    output=f"Shell command execution timed out after {timeout_val} seconds.",
                    exit_code=1,
                    is_error=True,
                )

            stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()

            is_err = proc.returncode != 0
            combined_output = stdout_str if stdout_str else stderr_str

            return ToolResult(
                output=combined_output,
                exit_code=proc.returncode if proc.returncode is not None else (1 if is_err else 0),
                is_error=is_err,
                metadata={"command": command, "cwd": str(target_dir)},
            )

        except Exception as exc:
            return ToolResult(
                output=f"Shell execution failure: {str(exc)}",
                exit_code=1,
                is_error=True,
            )

    async def execute(self, command: str, timeout: Optional[float] = 60.0, **kwargs: Any) -> ToolResult:
        """Public alias maintaining backward compatibility."""
        return await self._arun(command=command, timeout=timeout, **kwargs)


__all__ = [
    "ShellExecArgs",
    "ShellExecParameters",
    "ShellExecTool",
]