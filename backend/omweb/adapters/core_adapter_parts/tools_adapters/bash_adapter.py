# backend/omweb/adapters/core_adapter_parts/tools_adapters/bash_adapter.py
"""
Bash / Shell Command Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``bash`` (and alias ``shell_exec``) tool to the
canonical peldrun-core ``ShellExecTool``, applying execution policy,
terminal output sanitization, and error-code canonicalization.

Served tool names:
  * ``bash``
  * ``shell_exec``

Factory signature:
  ``create_bash_executor(workspace_dir=None, timeout=60.0, **kwargs)``
  returns an async callable ``(command, timeout_seconds=None, **kwargs)
  -> ToolResult``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from peldrun.tools.builtins.shell_exec import ShellExecTool
from peldrun.tools.contract import ToolResult

from ..terminal_utils import sanitize_terminal_output
from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.bash")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_bash_executor(
    workspace_dir: WorkspacePathType = None,
    timeout: float = 60.0,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``bash`` / ``shell_exec`` bound to
    ``workspace_dir``.

    The returned callable accepts ``command`` and optional
    ``timeout_seconds`` (overriding the factory-level default), and
    returns a canonical ``ToolResult``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    shell_tool = ShellExecTool(workspace_root=str(ws_path))

    async def _execute_bash(
        command: str = "",
        timeout_seconds: Optional[float] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``cmd`` as an alias for ``command`` for compatibility
        # with earlier tool-call conventions.
        if not command and "cmd" in call_kwargs:
            command = str(call_kwargs.pop("cmd") or "")

        eff_timeout = float(
            timeout_seconds if timeout_seconds is not None else timeout
        )
        try:
            res = await shell_tool.aexecute(
                command=command,
                timeout=eff_timeout,
                **call_kwargs,
            )
            tool_res = _ensure_tool_result(
                res, default_error_code="BASH_EXECUTION_ERROR"
            )
            if tool_res.output:
                tool_res.output = sanitize_terminal_output(str(tool_res.output))
            return tool_res
        except Exception as exc:
            logger.warning(
                "Bash tool execution exception in %s: %s", ws_path, exc
            )
            return ToolResult(
                output=f"Shell Execution Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "BASH_EXECUTION_EXCEPTION"},
            )

    return _execute_bash


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------

ADAPTER_SPEC = AdapterSpec(
    tool_names=("bash", "shell_exec"),
    factory=create_bash_executor,
)


__all__ = ["create_bash_executor", "ADAPTER_SPEC"]
