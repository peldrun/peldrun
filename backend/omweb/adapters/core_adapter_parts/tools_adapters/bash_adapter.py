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
from typing import Any, Awaitable, Callable, Dict, Optional

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
    # Raised from 60s to 300s (audit report item 5.1). The factory
    # default is a last-resort fallback used only when neither the
    # manifest (`bash.json → settings.defaults.timeout_seconds`) nor
    # the per-call argument supplies a value. 60s was too short for
    # legitimate long-running commands (``npm install``,
    # ``cargo build``, ``pip install``); 300s covers the vast
    # majority of real builds while still bounding runaway commands.
    # Operators who need more can raise it up to 600 from the tool
    # settings UI.
    timeout: float = 300.0,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``bash`` / ``shell_exec`` bound to
    ``workspace_dir``.

    The returned callable accepts ``command`` and optional
    ``timeout_seconds`` (overriding the factory-level default), and
    returns a canonical ``ToolResult``.

    M7.8: ``settings`` is provided by ``resolve_tool_runtime`` when the
    adapter declares ``requires_context={"settings"}``. It carries the
    user-configured values from the ``bash`` manifest
    (``timeout_seconds``, ``max_output_chars``, ``working_directory``,
    ``inherit_env``, ``allowed_commands``) and is forwarded to
    ``ShellExecTool`` so all defaults honor them.

    The factory-level ``timeout`` parameter is retained as a fallback
    for callers that bypass ``resolve_tool_runtime``. When settings
    declare a ``timeout_seconds``, that wins over the factory default.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    shell_tool = ShellExecTool(
        workspace_root=str(ws_path),
        settings=settings or {},
        default_timeout=timeout,
    )

    async def _execute_bash(
        command: str = "",
        timeout_seconds: Optional[float] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``cmd`` as an alias for ``command`` for compatibility
        # with earlier tool-call conventions.
        if not command and "cmd" in call_kwargs:
            command = str(call_kwargs.pop("cmd") or "")

        # Pass ``timeout=None`` when the caller did not specify one so
        # ShellExecTool falls back to its settings (M7.9). Do NOT
        # substitute the factory ``timeout`` here — that would override
        # user settings on every call.
        try:
            res = await shell_tool.aexecute(
                command=command,
                timeout=timeout_seconds,
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
    # M7.7: Request the "settings" context key so ``resolve_tool_runtime``
    # forwards the user-configured values loaded from the bash manifest.
    requires_context=frozenset({"settings"}),
)


__all__ = ["create_bash_executor", "ADAPTER_SPEC"]
