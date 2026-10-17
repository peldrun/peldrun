# backend/omweb/adapters/core_adapter_parts/tools_adapters/python_execute_adapter.py
"""
Python Execution Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``python_execute`` tool to the canonical
peldrun-core ``IsolatedShellTool`` by transparently materializing the
Python source into a hidden workspace file and invoking it via the
current interpreter.

Served tool names:
  * ``python_execute``

Factory signature:
  ``create_python_executor(workspace_dir=None, timeout=60.0, **kwargs)``
  returns an async callable ``(code, timeout_seconds=None, **kwargs)
  -> ToolResult``.

Design notes (preserved verbatim from the original inline adapter):
  * A temp file is used instead of ``python -c "..."`` because inline
    code is subject to cross-platform shell escaping hazards
    (Windows cmd.exe and POSIX sh have incompatible quoting rules).
  * The temp file is named with a leading dot
    (``.peldrun_py_exec_<rand>.py``) so it is automatically excluded
    from artifact detection, which skips any path component starting
    with a dot.
  * ``sys.executable`` guarantees the same interpreter that runs the
    backend is used, so all installed packages are available.
  * The generated command is ``"<python_exe>" "<script_name>"`` without
    an extra outer pair of quotes on Windows — cmd.exe misparses the
    executable name otherwise.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any, Awaitable, Callable, Optional

from peldrun.tools.builtins.isolated_shell import (
    IsolatedShellTool as IsolatedPythonTool,
)
from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

from ..terminal_utils import sanitize_terminal_output
from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.python_execute")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_python_executor(
    workspace_dir: WorkspacePathType = None,
    timeout: float = 60.0,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``python_execute`` bound to
    ``workspace_dir``.

    The returned callable accepts ``code`` (Python source) and optional
    ``timeout_seconds``, and returns a canonical ``ToolResult``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    py_tool = IsolatedPythonTool(workspace_root=str(ws_path))
    python_exe = sys.executable or "python"

    async def _execute_python(
        code: str = "",
        timeout_seconds: Optional[float] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``script`` as an alias for ``code``.
        if not code and "script" in call_kwargs:
            code = str(call_kwargs.pop("script") or "")

        if not code.strip():
            return ToolResult(
                output="Python Execution Error: empty code payload.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "PYTHON_EMPTY_CODE"},
            )

        eff_timeout = float(
            timeout_seconds if timeout_seconds is not None else timeout
        )

        # Give the shell tool a slightly larger budget than the requested
        # timeout so its own outer wait_for does not fire first.
        try:
            py_tool.set_execution_policy(
                ToolExecutionPolicy(
                    max_attempts=1,
                    timeout_seconds=eff_timeout + 5.0,
                    retryable=False,
                    idempotent=False,
                    side_effects=True,
                )
            )
        except Exception:
            # Non-fatal: if policy mutation fails, fall back to defaults.
            pass

        # Hidden filename → skipped by artifact detection (leading dot).
        script_name = f".peldrun_py_exec_{uuid.uuid4().hex[:8]}.py"
        script_path = ws_path / script_name

        try:
            script_path.write_text(code, encoding="utf-8")
        except Exception as write_err:
            return ToolResult(
                output=(
                    f"Python Execution Error: failed to write script: "
                    f"{write_err}"
                ),
                exit_code=1,
                is_error=True,
                metadata={"error_code": "PYTHON_WRITE_FAILED"},
            )

        try:
            # Both cmd.exe and POSIX sh handle ``"<exe>" "<script>"``
            # natively. An additional outer pair of quotes makes
            # cmd.exe misparse the executable name and return
            # exit_code=1 without running anything.
            command = f'"{python_exe}" "{script_name}"'

            res = await py_tool.aexecute(
                command=command,
                timeout=eff_timeout,
                **call_kwargs,
            )
            tool_res = _ensure_tool_result(
                res, default_error_code="PYTHON_EXECUTION_ERROR"
            )
            if tool_res.output:
                tool_res.output = sanitize_terminal_output(str(tool_res.output))
            return tool_res
        except Exception as exc:
            logger.warning(
                "Python tool execution exception in %s: %s", ws_path, exc
            )
            return ToolResult(
                output=f"Python Execution Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "PYTHON_EXECUTION_EXCEPTION"},
            )
        finally:
            # Best-effort cleanup — never let this mask the real result.
            try:
                script_path.unlink(missing_ok=True)
            except Exception:
                pass

    return _execute_python


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------

ADAPTER_SPEC = AdapterSpec(
    tool_names=("python_execute",),
    factory=create_python_executor,
)


__all__ = ["create_python_executor", "ADAPTER_SPEC"]
