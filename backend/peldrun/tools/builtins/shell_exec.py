"""
PELDRUN Core Shell Execution Tool.
Executes shell commands strictly bounded within the project workspace,
enforcing directory containment policies and satisfying BaseTool abstract contracts.
"""

from __future__ import annotations

import asyncio
import os
import shlex
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
        settings: Optional[Dict[str, Any]] = None,
        default_timeout: float = 60.0,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the shell tool with optional user-supplied settings (M7.9).

        Args:
            workspace_root: Root directory for shell execution.
            settings: User-configured values from ``settings_schema``
                (``timeout_seconds``, ``max_output_chars``,
                ``working_directory``, ``inherit_env``,
                ``allowed_commands``). Missing keys fall back to the
                schema defaults, then to ``default_timeout``.
            default_timeout: Ultimate fallback when neither settings nor
                the per-call ``timeout`` argument specify a value.
                Preserved so callers using the factory ``timeout``
                parameter keep working.
        """
        super().__init__(workspace_root=workspace_root)
        self.workspace_root = workspace_root or os.getcwd()

        s: Dict[str, Any] = settings or {}

        def _as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _as_float(value: Any, default: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        self._settings_timeout: float = _as_float(
            s.get("timeout_seconds"), default_timeout
        )
        self._settings_max_output: int = _as_int(
            s.get("max_output_chars"), 20000
        )
        self._settings_working_dir: Optional[str] = (
            s.get("working_directory") or "workspace_root"
        )
        self._settings_inherit_env: bool = bool(s.get("inherit_env", False))
        raw_allow = s.get("allowed_commands")
        self._settings_allowed_commands: list[str] = (
            [str(x) for x in raw_allow if isinstance(x, str)]
            if isinstance(raw_allow, list)
            else []
        )

    async def _arun(
        self,
        command: str,
        timeout: Optional[float] = None,
        working_subdir: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Internal asynchronous shell execution satisfying BaseTool contract.

        Settings precedence (M7.9):
          * ``timeout``: per-call arg  >  settings.timeout_seconds  >
            factory default (via ``default_timeout``).
          * Working directory: per-call ``working_subdir``  >
            settings.working_directory  > workspace root.
          * Environment: if settings.inherit_env is False (default),
            a scrubbed, allow-listed env is passed so user secrets
            (API keys, tokens) never leak into subprocesses.
          * ``settings.allowed_commands`` (if non-empty): first token
            of the command MUST be in the list, else rejected.
          * ``settings.max_output_chars``: output is truncated after
            execution to prevent runaway log capture.
        """
        root_path = Path(self.workspace_root).resolve()
        target_dir = root_path

        # 1. Resolve working directory (explicit subdir wins, then settings).
        effective_subdir = working_subdir
        if effective_subdir is None and self._settings_working_dir not in (
            None,
            "",
            "workspace_root",
        ):
            effective_subdir = self._settings_working_dir

        if effective_subdir:
            candidate = (root_path / effective_subdir).resolve()
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

        # 2. Resolve timeout (per-call > settings > instance default).
        if timeout is not None:
            timeout_val = float(timeout)
        else:
            timeout_val = float(self._settings_timeout)

        # 3. Enforce the allowed-commands allowlist when configured.
        if self._settings_allowed_commands:
            try:
                tokens = shlex.split(command)
            except ValueError:
                # Malformed quoting: fall back to whitespace split.
                tokens = command.split()
            if not tokens:
                return ToolResult(
                    output="Shell execution failure: empty command.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "EMPTY_COMMAND"},
                )
            # Compare by basename so absolute paths to allowed binaries
            # (e.g. /usr/bin/python) still match "python".
            first = Path(tokens[0]).name or tokens[0]
            if first not in self._settings_allowed_commands:
                return ToolResult(
                    output=(
                        f"Command not permitted by settings allow-list: "
                        f"{tokens[0]!r}. Allowed: "
                        f"{sorted(self._settings_allowed_commands)}"
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "COMMAND_NOT_ALLOWED",
                        "command": tokens[0],
                    },
                )

        # 4. Build a scrubbed environment when inherit_env is disabled.
        #    Only toolchain-critical variables are forwarded; secrets
        #    (API keys, tokens) are dropped by construction.
        proc_env: Optional[Dict[str, str]] = None
        if not self._settings_inherit_env:
            allow_keys = {
                "PATH", "HOME", "USER", "USERNAME", "USERPROFILE",
                "LANG", "LC_ALL", "LC_CTYPE", "TERM",
                "TMP", "TEMP", "TMPDIR",
                "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
                "PYTHONPATH", "PYTHONHOME",
            }
            proc_env = {k: v for k, v in os.environ.items() if k in allow_keys}

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(target_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )

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

            # 5. Truncate to settings.max_output_chars (safety net).
            if (
                self._settings_max_output > 0
                and len(combined_output) > self._settings_max_output
            ):
                combined_output = (
                    combined_output[: self._settings_max_output]
                    + f"\n... [output truncated to {self._settings_max_output} characters]"
                )

            return ToolResult(
                output=combined_output,
                exit_code=proc.returncode if proc.returncode is not None else (1 if is_err else 0),
                is_error=is_err,
                metadata={
                    "command": command,
                    "cwd": str(target_dir),
                    "timeout_seconds": timeout_val,
                },
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
