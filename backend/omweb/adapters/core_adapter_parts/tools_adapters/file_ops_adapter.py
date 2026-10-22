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
from typing import Any, Awaitable, Callable, Dict, List, Optional

from peldrun.tools.builtins.file_ops import FileOpsTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import (
    WorkspacePathType,
    _ensure_tool_result,
    _resolve_workspace_path,
)

logger = logging.getLogger("omweb.adapters.tools_adapters.file_ops")


# ---------------------------------------------------------------------------
# File Saver — write / overwrite a workspace file
# ---------------------------------------------------------------------------

def create_file_saver_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``file_saver`` bound to
    ``workspace_dir``.

    The returned callable accepts ``file_path``, ``content``, and
    optional ``mode`` (``"w"`` to overwrite, ``"a"`` to append). It
    returns a canonical ``ToolResult``.

    M7.8: ``settings`` carries ``max_file_size_kb``,
    ``overwrite_policy``, and ``default_encoding`` from the manifest
    and is forwarded to ``FileOpsTool``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    file_ops = FileOpsTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    async def _execute_file_saver(
        file_path: Optional[str] = None,
        content: str = "",
        mode: str = "w",
        encoding: Optional[str] = None,
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

        # Map the legacy ``mode`` argument to FileOpsTool's action:
        #   "w" -> "write"  (create or overwrite, subject to settings)
        #   "a" -> "append"
        action = "append" if str(mode).lower().startswith("a") else "write"

        try:
            # Call FileOpsTool.aexecute directly with the exact field
            # names declared in FileOpsArgs (action / path / content /
            # encoding). ``encoding=None`` lets the tool fall back to
            # settings.default_encoding.
            res = await file_ops.aexecute(
                action=action,
                path=str(target_path),
                content=content,
                encoding=encoding,
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
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``file_reader`` bound to
    ``workspace_dir``.

    The returned callable accepts ``file_path`` (or aliases ``path`` /
    ``filename``) and returns a canonical ``ToolResult``.

    M7.8: ``settings`` is shared with ``file_saver`` since both serve
    the same underlying ``FileOpsTool``. ``default_encoding`` in
    particular affects how ``file_reader`` decodes files that were
    written by ``file_saver``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    file_ops = FileOpsTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    async def _execute_file_reader(
        file_path: Optional[str] = None,
        encoding: Optional[str] = None,
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
            # Direct call bypasses ``_invoke_file_ops`` which could not
            # introspect a VAR_KEYWORD-only ``aexecute`` signature.
            res = await file_ops.aexecute(
                action="read",
                path=str(target_path),
                encoding=encoding,
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
        # M7.7: forward user settings (size cap, overwrite policy,
        # encoding) to the tool at construction time.
        requires_context=frozenset({"settings"}),
    ),
    AdapterSpec(
        tool_names=("file_reader",),
        factory=create_file_reader_executor,
        # M7.7: same underlying FileOpsTool — share the settings.
        requires_context=frozenset({"settings"}),
    ),
]


__all__ = [
    "create_file_saver_executor",
    "create_file_reader_executor",
    "ADAPTER_SPECS",
]
