# backend/omweb/adapters/core_adapter_parts/tools_adapters/file_ops_adapter.py
"""
File Operations Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``file_saver`` and ``file_reader`` tools to the
canonical peldrun-core ``FileOpsTool``. A single adapter module serves
both tools because they share the same underlying Core class and the
same defensive invocation bridge (``_invoke_file_ops``).

Served tool names:
  * ``file_saver``   — write / overwrite a workspace file.
  * ``file_reader``  — read a workspace file.

This module exports ``ADAPTER_SPECS`` (a list of two specs) instead of
a single ``ADAPTER_SPEC``. The discovery routine in ``executors.py``
supports both forms.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional

from peldrun.tools.builtins.file_ops import FileOpsTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import (
    WorkspacePathType,
    _ensure_tool_result,
    _invoke_file_ops,
    _resolve_workspace_path,
)

logger = logging.getLogger("omweb.adapters.tools_adapters.file_ops")


# ---------------------------------------------------------------------------
# File Saver — write / overwrite a workspace file
# ---------------------------------------------------------------------------

def create_file_saver_executor(
    workspace_dir: WorkspacePathType = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``file_saver`` bound to
    ``workspace_dir``.

    The returned callable accepts ``file_path``, ``content``, and
    optional ``mode`` (``"w"`` to overwrite, ``"a"`` to append). It
    returns a canonical ``ToolResult``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    file_ops = FileOpsTool(workspace_root=str(ws_path))

    async def _execute_file_saver(
        file_path: Optional[str] = None,
        content: str = "",
        mode: str = "w",
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``path`` and ``filename`` as aliases for ``file_path``.
        target_path = (
            file_path
            or call_kwargs.pop("path", None)
            or call_kwargs.pop("filename", None)
            or ""
        )
        if not str(target_path).strip():
            return ToolResult(
                output="File Saver Error: file_path is required.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "FILE_SAVER_MISSING_PATH"},
            )
        try:
            res = await _invoke_file_ops(
                file_ops,
                operation="write",
                file_path=str(target_path),
                content=content,
                mode=mode,
                **call_kwargs,
            )
            return _ensure_tool_result(
                res, default_error_code="FILE_SAVER_ERROR"
            )
        except Exception as exc:
            logger.warning("File saver exception in %s: %s", ws_path, exc)
            return ToolResult(
                output=f"File Saver Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "FILE_SAVER_EXCEPTION"},
            )

    return _execute_file_saver


# ---------------------------------------------------------------------------
# File Reader — read a workspace file
# ---------------------------------------------------------------------------

def create_file_reader_executor(
    workspace_dir: WorkspacePathType = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``file_reader`` bound to
    ``workspace_dir``.

    The returned callable accepts ``file_path`` (or aliases ``path`` /
    ``filename``) and returns a canonical ``ToolResult``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    file_ops = FileOpsTool(workspace_root=str(ws_path))

    async def _execute_file_reader(
        file_path: Optional[str] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``path`` and ``filename`` as aliases for ``file_path``.
        target_path = (
            file_path
            or call_kwargs.pop("path", None)
            or call_kwargs.pop("filename", None)
            or ""
        )
        if not str(target_path).strip():
            return ToolResult(
                output="File Read Error: file_path is required.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "FILE_READER_MISSING_PATH"},
            )
        try:
            res = await _invoke_file_ops(
                file_ops,
                operation="read",
                file_path=str(target_path),
                **call_kwargs,
            )
            return _ensure_tool_result(
                res, default_error_code="FILE_READER_ERROR"
            )
        except Exception as exc:
            return ToolResult(
                output=f"File Read Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "FILE_READER_EXCEPTION"},
            )

    return _execute_file_reader


# ---------------------------------------------------------------------------
# Adapter declarations — multi-spec module
# ---------------------------------------------------------------------------
#
# This module serves two tools that share the same underlying Core
# class (FileOpsTool) and the same defensive invocation bridge
# (_invoke_file_ops). It therefore exports ADAPTER_SPECS (plural), a
# list containing one AdapterSpec per tool.

ADAPTER_SPECS: List[AdapterSpec] = [
    AdapterSpec(
        tool_names=("file_saver",),
        factory=create_file_saver_executor,
    ),
    AdapterSpec(
        tool_names=("file_reader",),
        factory=create_file_reader_executor,
    ),
]


__all__ = [
    "create_file_saver_executor",
    "create_file_reader_executor",
    "ADAPTER_SPECS",
]
