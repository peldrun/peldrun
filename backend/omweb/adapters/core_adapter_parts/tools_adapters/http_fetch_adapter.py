# backend/omweb/adapters/core_adapter_parts/tools_adapters/http_fetch_adapter.py
"""
HTTP Fetch Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``http_fetch`` tool to the canonical peldrun-core
``HttpFetchTool``. Provides a single-capability path: fetch a static
HTML page over HTTP(S) and return its visible text content.

Served tool names:
  * ``http_fetch``

Factory signature:
  ``create_http_fetch_executor(workspace_dir=None, **kwargs)``
  returns an async callable ``(url, max_chars=8000, **kwargs)
  -> ToolResult``.

This adapter is intentionally thin: all validation, fetching, HTML
parsing, and error canonicalization live in ``HttpFetchTool._arun``.
The adapter's only job is to construct the Core tool bound to the
correct workspace and expose its canonical ``aexecute`` entrypoint.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from peldrun.tools.builtins.http_fetch import HttpFetchTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.http_fetch")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_http_fetch_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``http_fetch`` bound to
    ``workspace_dir``.

    The returned callable accepts ``url`` (required) and optional
    ``max_chars``, and returns a canonical ``ToolResult``.

    M7.8: ``settings`` is provided by ``resolve_tool_runtime`` when the
    adapter declares ``requires_context={"settings"}``. It carries the
    user-configured values from the tool's ``settings_schema`` and is
    forwarded to ``HttpFetchTool`` so request defaults honor them.

    Note: ``http_fetch`` does not use the workspace for fetching, but
    the Core ``HttpFetchTool`` is still constructed with a workspace
    root so it composes cleanly with the rest of the adapter layer.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = HttpFetchTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    async def _execute_http_fetch(
        url: Optional[str] = None,
        max_chars: Optional[int] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``target_url`` as an alias for ``url`` for symmetry
        # with the retired http_fetchadapter.
        target_url = (
            url or call_kwargs.pop("target_url", None) or ""
        ).strip()

        try:
            res = await core_tool.aexecute(
                url=target_url,
                max_chars=max_chars,
            )
            return _ensure_tool_result(
                res, default_error_code="HTTP_FETCH_ERROR"
            )
        except Exception as exc:
            logger.warning(
                "HTTP fetch execution exception: %s", exc
            )
            return ToolResult(
                output=f"HTTP Fetch Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "HTTP_FETCH_EXCEPTION"},
            )

    return _execute_http_fetch


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------

ADAPTER_SPEC = AdapterSpec(
    tool_names=("http_fetch",),
    factory=create_http_fetch_executor,
    # M7.7: Request the "settings" context key so ``resolve_tool_runtime``
    # forwards the user-configured values loaded from the tool's
    # settings_schema. Adapters that do not declare this key are
    # unaffected (``matches_context`` filters the context dict).
    requires_context=frozenset({"settings"}),
)


__all__ = ["create_http_fetch_executor", "ADAPTER_SPEC"]
