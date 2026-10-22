# backend/omweb/adapters/core_adapter_parts/tools_adapters/browser_interactive_adapter.py
"""
Interactive Browser Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``browser_interactive`` tool to the canonical
peldrun-core ``BrowserInteractiveTool``. Provides a real Chromium
browser with per-run isolation, token-efficient accessibility
snapshots, and SSRF protection.

Served tool names:
  * ``browser_interactive``

Factory signature:
  ``create_browser_interactive_executor(workspace_dir=None, run_id=None,
  **kwargs)`` returns an async callable ``(action, **params)
  -> ToolResult``.

Context requirement:
  This adapter declares ``run_id`` in ``requires_context``. The
  ``resolve_tool_runtime`` layer forwards the active run's ID, which is
  used by the session manager to isolate BrowserContexts. Without a
  run_id, the tool refuses to execute (each call would otherwise share
  state with unrelated runs).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional

from peldrun.tools.builtins.browser_interactive import BrowserInteractiveTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.browser_interactive")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_browser_interactive_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    run_id: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``browser_interactive`` bound to
    ``workspace_dir`` and the active ``run_id``.

    The returned callable accepts a raw dict of action parameters
    (``action``, ``url``, ``ref``, ...) and returns a canonical
    ``ToolResult``.

    M7.8: ``settings`` is provided by ``resolve_tool_runtime`` when the
    adapter declares ``requires_context={"settings"}``. It carries
    ``default_max_chars``, ``navigation_timeout_seconds``,
    ``action_timeout_seconds``, ``user_agent``, ``viewport``, and
    ``screenshot_dir`` from the manifest.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = BrowserInteractiveTool(
        workspace_root=str(ws_path),
        settings=settings or {},
    )

    if run_id:
        core_tool.set_run_id(run_id)
    else:
        logger.warning(
            "browser_interactive adapter created without run_id. "
            "The tool will refuse to execute until a run_id is bound."
        )

    async def _execute_browser_interactive(
        action: str = "",
        **call_kwargs: Any,
    ) -> ToolResult:
        try:
            res = await core_tool.aexecute(action=action, **call_kwargs)
            return _ensure_tool_result(
                res, default_error_code="BROWSER_INTERACTIVE_ERROR"
            )
        except Exception as exc:
            logger.exception(
                "browser_interactive execution exception (action=%r): %s",
                action,
                exc,
            )
            return ToolResult(
                output=f"Browser Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "BROWSER_INTERACTIVE_EXCEPTION",
                    "exception_type": type(exc).__name__,
                },
            )

    return _execute_browser_interactive


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------
#
# ``run_id`` is declared in ``requires_context`` so that
# ``resolve_tool_runtime`` forwards the active run's ID at construction
# time. This is the only context parameter this adapter accepts.

ADAPTER_SPEC = AdapterSpec(
    tool_names=("browser_interactive",),
    factory=create_browser_interactive_executor,
    # ``run_id`` isolates BrowserContexts per run.
    # ``settings`` (M7.7) forwards user-configured values from the
    # manifest: max_chars, navigation/action timeouts, user_agent,
    # viewport, and screenshot_dir.
    requires_context=frozenset({"run_id", "settings"}),
)


__all__ = ["create_browser_interactive_executor", "ADAPTER_SPEC"]
