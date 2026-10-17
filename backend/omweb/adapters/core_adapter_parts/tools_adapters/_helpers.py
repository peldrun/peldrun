# backend/omweb/adapters/core_adapter_parts/tools_adapters/_helpers.py
"""
Shared helper utilities for PELDRUN Web-to-Core tool adapters.

These helpers are consumed by multiple adapter modules and are
intentionally kept free of any tool-specific logic. Each adapter
module imports only the helpers it needs.

Contents:
  * ``WorkspacePathType``        — type alias for workspace_dir arguments.
  * ``_resolve_workspace_path``  — normalizes workspace_dir to a resolved Path.
  * ``_ensure_tool_result``      — canonicalizes arbitrary tool outputs into
                                   ``ToolResult`` while preserving error state.
  * ``_invoke_file_ops``         — defensive invocation bridge across the
                                   possible FileOpsTool interface shapes.

Design rules:
  * No adapter-specific code belongs here.
  * Helpers must remain dependency-free beyond the core contract types.
  * Any helper used by exactly one adapter should live in that adapter's
    module, not here.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

from peldrun.tools.builtins.file_ops import FileOpsTool
from peldrun.tools.contract import ToolResult

logger = logging.getLogger("omweb.adapters.tools_adapters.helpers")


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

WorkspacePathType = Optional[Union[Path, str]]


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _resolve_workspace_path(workspace_dir: WorkspacePathType) -> Path:
    """
    Normalize a ``workspace_dir`` argument to an existing resolved ``Path``.

    Accepts ``None`` (defaults to current working directory), a ``Path``
    instance, or a string. The resulting directory is created if absent
    (``parents=True``) so downstream file operations can rely on it.
    """
    if workspace_dir is None:
        resolved = Path.cwd().resolve()
    elif isinstance(workspace_dir, Path):
        resolved = workspace_dir.resolve()
    else:
        resolved = Path(str(workspace_dir)).resolve()

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


# ---------------------------------------------------------------------------
# ToolResult canonicalization
# ---------------------------------------------------------------------------

def _ensure_tool_result(
    raw: Any,
    *,
    default_error_code: Optional[str] = None,
) -> ToolResult:
    """
    Ensure the returned object is a canonical Core ``ToolResult`` while
    preserving any existing fields (``is_error``, ``exit_code``,
    ``metadata``, ``artifacts``).

    Behavior by input type:

      * ``ToolResult``  — returned as-is after defensive normalization:
          - If ``__pydantic_extra__['error']`` is populated and ``output``
            is empty, the error text is promoted into ``output``.
          - ``is_error`` is aligned with ``exit_code`` (non-zero ⇒ error).
          - ``default_error_code`` is attached to ``metadata`` when the
            result is an error and no ``error_code`` is present.
      * ``dict``        — converted to ``ToolResult``; ``error``/
                        ``is_error`` are unified, and ``output`` falls back
                        to ``error`` when missing.
      * anything else   — wrapped as a successful ``ToolResult`` whose
                        output is ``str(raw)`` (or ``""`` for ``None``).
    """
    if isinstance(raw, ToolResult):
        # Surface captured stderr into output when the process failed.
        try:
            extra = getattr(raw, "__pydantic_extra__", None) or {}
            err_text = extra.get("error") if isinstance(extra, dict) else None
        except Exception:
            err_text = None

        if err_text and not str(raw.output or "").strip():
            raw.output = str(err_text)

        # Align is_error with exit_code.
        if raw.exit_code != 0 and not raw.is_error:
            raw.is_error = True

        # Attach the default error code when applicable.
        if raw.is_error and default_error_code and "error_code" not in raw.metadata:
            raw.metadata["error_code"] = default_error_code

        return raw

    if isinstance(raw, dict):
        is_err = bool(raw.get("is_error") or raw.get("error"))
        exit_code = int(raw.get("exit_code", 1 if is_err else 0))
        output_val = raw.get("output")
        if output_val is None:
            output_val = raw.get("error") or ""
        meta = dict(raw.get("metadata") or {})
        if is_err and default_error_code and "error_code" not in meta:
            meta["error_code"] = default_error_code
        return ToolResult(
            output=str(output_val),
            exit_code=exit_code,
            is_error=is_err,
            metadata=meta,
            artifacts=list(raw.get("artifacts") or []),
        )

    return ToolResult(
        output="" if raw is None else str(raw),
        exit_code=0,
        is_error=False,
    )


# ---------------------------------------------------------------------------
# FileOpsTool defensive bridge
# ---------------------------------------------------------------------------

async def _invoke_file_ops(
    tool: FileOpsTool,
    *,
    operation: str,
    file_path: str,
    content: str = "",
    mode: str = "w",
    **extra: Any,
) -> Any:
    """
    Defensive invocation of ``FileOpsTool`` across possible interface shapes:

      * dedicated methods: ``read()``/``write()``, ``read_file()``/
        ``write_file()``, ``load()``/``save()``, ``get()``/``put()``.
      * unified ``aexecute(action=..., path=..., content=...)``.
      * unified ``aexecute(command=..., path=..., content=...)``.

    The first shape that resolves is used. Parameters are matched by
    name from the target method's signature so the bridge adapts to
    signature drift without raising.
    """
    if operation not in ("read", "write"):
        raise ValueError(f"Unsupported file op: {operation!r}")

    candidate_methods = {
        "read": ("read", "read_file", "load", "get"),
        "write": ("write", "write_file", "save", "put"),
    }[operation]

    # Path 1: dedicated methods on the tool instance
    for name in candidate_methods:
        method = getattr(tool, name, None)
        if callable(method):
            sig = inspect.signature(method)
            kwargs: Dict[str, Any] = {}
            for p in sig.parameters:
                if p == "self":
                    continue
                if p in ("file_path", "path", "filename", "name"):
                    kwargs[p] = file_path
                elif p in ("content", "text", "data", "value"):
                    kwargs[p] = content
                elif p == "mode":
                    kwargs[p] = mode
            kwargs.update(extra)
            res = method(**kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res

    # Path 2: unified aexecute/execute with detected parameters
    executor = getattr(tool, "aexecute", None) or getattr(tool, "execute", None)
    if callable(executor):
        sig = inspect.signature(executor)
        kwargs = {}
        for p in sig.parameters:
            if p == "self":
                continue
            if p in ("file_path", "path", "filename", "name"):
                kwargs[p] = file_path
            elif p in ("content", "text", "data"):
                kwargs[p] = content
            elif p == "mode":
                kwargs[p] = mode
            elif p in ("action", "operation", "command", "op"):
                kwargs[p] = operation
        kwargs.update(extra)
        res = executor(**kwargs)
        if inspect.isawaitable(res):
            res = await res
        return res

    exposed = [
        a for a in dir(tool)
        if not a.startswith("_") and callable(getattr(tool, a, None))
    ]
    raise AttributeError(
        f"FileOpsTool exposes no usable read/write interface. "
        f"Candidates: {exposed}"
    )


__all__ = [
    "WorkspacePathType",
    "_resolve_workspace_path",
    "_ensure_tool_result",
    "_invoke_file_ops",
]
