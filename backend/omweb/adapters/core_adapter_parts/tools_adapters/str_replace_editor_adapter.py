# backend/omweb/adapters/core_adapter_parts/tools_adapters/str_replace_editor_adapter.py
"""
String Replace Editor Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``str_replace_editor`` tool to the canonical
peldrun-core ``StrReplaceEditor`` (alias ``StrReplaceEditorTool``),
providing view / create / str_replace / insert / undo_edit operations
on workspace files.

Served tool names:
  * ``str_replace_editor``

Factory signature:
  ``create_str_replace_editor_executor(workspace_dir=None, **kwargs)``
  returns an async callable ``(command, path, ...) -> ToolResult``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional

from peldrun.tools.builtins.str_replace_editor import (
    StrReplaceEditor as StrReplaceEditorTool,
)
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.str_replace_editor")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_str_replace_editor_executor(
    workspace_dir: WorkspacePathType = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``str_replace_editor`` bound to
    ``workspace_dir``.

    The returned callable accepts ``command`` (one of ``view``,
    ``create``, ``str_replace``, ``insert``, ``undo_edit``) and the
    parameters relevant to that command. It returns a canonical
    ``ToolResult``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    editor_tool = StrReplaceEditorTool(workspace_root=str(ws_path))

    async def _execute_str_replace_editor(
        command: str = "view",
        path: str = "",
        file_text: Optional[str] = None,
        old_str: Optional[str] = None,
        new_str: Optional[str] = None,
        insert_line: Optional[int] = None,
        view_range: Optional[List[int]] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``file_path`` as an alias for ``path``.
        if not path and "file_path" in call_kwargs:
            path = str(call_kwargs.pop("file_path") or "")
        try:
            res = await editor_tool.aexecute(
                command=command,
                path=path,
                file_text=file_text,
                old_str=old_str,
                new_str=new_str,
                insert_line=insert_line,
                view_range=view_range,
                **call_kwargs,
            )
            return _ensure_tool_result(
                res, default_error_code="STR_REPLACE_EDITOR_ERROR"
            )
        except Exception as exc:
            logger.warning(
                "str_replace_editor exception in %s: %s", ws_path, exc
            )
            return ToolResult(
                output=f"Editor Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "STR_REPLACE_EDITOR_EXCEPTION"},
            )

    return _execute_str_replace_editor


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------

ADAPTER_SPEC = AdapterSpec(
    tool_names=("str_replace_editor",),
    factory=create_str_replace_editor_executor,
)


__all__ = ["create_str_replace_editor_executor", "ADAPTER_SPEC"]
