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

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        default_timeout: float = 60.0,
        **kwargs: Any,
    ):
        """
        Initialize the isolated shell tool with optional settings (M7.9).

        ``python_execute`` is the primary consumer: it forwards
        ``timeout_seconds`` and ``max_output_chars`` from the manifest.
        Other tools (or direct callers) may construct the tool without
        settings; in that case the classic behavior (60s timeout, no
        output truncation) is preserved.
        """
        super().__init__()
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())

        s: Dict[str, Any] = settings or {}

        def _as_float(v: Any, d: float) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        def _as_int(v: Any, d: int) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return d

        self._settings_timeout: float = _as_float(
            s.get("timeout_seconds"), default_timeout
        )
        self._settings_max_output: int = _as_int(
            s.get("max_output_chars"), 20000
        )

    async def _arun(
        self,
        command: str,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Satisfies BaseTool abstract contract by delegating to execute().

        M7.9: ``timeout=None`` (the new default) causes ``execute()`` to
        fall back to ``settings.timeout_seconds``; explicit callers keep
        full control.
        """
        return await self.execute(command=command, timeout=timeout, **kwargs)

    async def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> ToolResult:
        # M7.9: explicit timeout wins; otherwise fall back to settings.
        if timeout is not None:
            timeout_val = float(timeout)
        else:
            timeout_val = float(self._settings_timeout)

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

            # M7.9: truncate to settings.max_output_chars when configured.
            # Applied per stream so a large stdout does not hide the tail
            # of a diagnostic stderr (and vice versa).
            if self._settings_max_output > 0:
                limit = self._settings_max_output
                if len(stdout_str) > limit:
                    stdout_str = (
                        stdout_str[:limit]
                        + f"\n... [stdout truncated to {limit} chars]"
                    )
                if len(stderr_str) > limit:
                    stderr_str = (
                        stderr_str[:limit]
                        + f"\n... [stderr truncated to {limit} chars]"
                    )

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
