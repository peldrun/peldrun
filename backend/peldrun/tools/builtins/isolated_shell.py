from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult


class ShellParameters(BaseModel):
    command: str = Field(
        ...,
        description="The shell command line string to execute.",
    )
    timeout: Optional[int] = Field(
        default=60,
        description="Command timeout in seconds.",
    )


class IsolatedShellTool(BaseTool):
    """Executes shell commands strictly scoped to the designated workspace directory."""

    name: str = "isolated_shell"
    description: str = "Executes command line instructions strictly within the workspace directory boundaries."
    parameters: Dict[str, Any] = ShellParameters.model_json_schema()

    def __init__(self, workspace_root: Optional[str] = None):
        super().__init__()
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())

    async def execute(self, command: str, timeout: Optional[int] = 60, **kwargs: Any) -> ToolResult:
        timeout_val = float(timeout) if timeout else 60.0

        if not os.path.isdir(self.workspace_root):
            return ToolResult(
                output="",
                error=f"Workspace root directory does not exist: {self.workspace_root}",
                exit_code=1,
            )

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.workspace_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_val,
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return ToolResult(
                    output="",
                    error=f"Command execution timed out after {timeout_val} seconds.",
                    exit_code=124,
                )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            return ToolResult(
                output=stdout_str,
                error=stderr_str,
                exit_code=process.returncode if process.returncode is not None else 0,
            )

        except Exception as exc:
            return ToolResult(
                output="",
                error=f"Shell execution failure: {str(exc)}",
                exit_code=1,
            )