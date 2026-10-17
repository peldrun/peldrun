# backend/omweb/adapters/core_adapter_parts/tools_adapters/web_search_adapter.py
"""
Web Search Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``web_search`` tool to the canonical peldrun-core
``WebSearchTool``, backed exclusively by ``SearchProviderFactory``.

Served tool names:
  * ``web_search``

Factory signature:
  ``create_web_search_executor(workspace_dir=None, *, search_config=None,
  run_id=None, job_id=None, session_id=None, chat_id=None,
  project_id=None, **factory_kwargs)``
  returns an async callable ``(query, max_results=None, **kwargs)
  -> ToolResult``.

Engineering guarantees (preserved verbatim from the original adapter):
  1. Returns the original Core ToolResult preserving is_error,
     exit_code, metadata, and artifacts.
  2. Preserves ``max_results=None`` when omitted by the tool call so
     user settings (``search.default_max_results`` and
     ``search.<provider>.max_results``) govern execution.
  3. Never performs a hidden direct DuckDuckGo Lite HTTP request
     outside ``SearchProviderFactory`` policy.
  4. Propagates execution context IDs (run_id, job_id, session_id,
     chat_id, project_id) for run-scoped circuit breaker isolation
     and usage accounting.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional

from peldrun.tools.builtins.web_search import WebSearchTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.web_search")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_web_search_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    search_config: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    session_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
    **factory_kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``web_search`` bound to
    ``workspace_dir`` and the given execution context.

    The returned callable accepts ``query`` and optional
    ``max_results`` (omitted ⇒ policy defaults apply). It returns a
    canonical ``ToolResult``.
    """
    ws_root_str = (
        str(_resolve_workspace_path(workspace_dir))
        if workspace_dir is not None
        else None
    )
    core_search = WebSearchTool(
        workspace_root=ws_root_str,
        search_config=search_config,
        run_id=run_id,
        job_id=job_id,
        session_id=session_id,
        chat_id=chat_id,
        project_id=project_id,
    )

    async def _execute_web_search(
        query: Optional[str] = None,
        max_results: Optional[int] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``q`` / ``search_query`` / ``input`` as aliases for
        # ``query``.
        raw_query = query
        if raw_query is None:
            for alias in ("q", "search_query", "input"):
                if alias in call_kwargs:
                    raw_query = call_kwargs.pop(alias)
                    break

        clean_q = str(raw_query or "").strip()
        if not clean_q:
            return ToolResult(
                output="Search Error: Query cannot be empty.",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "EMPTY_SEARCH_QUERY",
                    "requested_max_results": max_results,
                },
            )

        # Forward per-call context overrides if supplied, otherwise use
        # factory-bound context.
        eff_run_id = call_kwargs.pop("run_id", None) or run_id
        eff_job_id = call_kwargs.pop("job_id", None) or job_id
        eff_session_id = call_kwargs.pop("session_id", None) or session_id
        eff_chat_id = call_kwargs.pop("chat_id", None) or chat_id
        eff_project_id = call_kwargs.pop("project_id", None) or project_id
        eff_search_cfg = call_kwargs.pop("search_config", None) or search_config

        try:
            res = await core_search.aexecute(
                query=clean_q,
                max_results=max_results,
                search_config=eff_search_cfg,
                run_id=eff_run_id,
                job_id=eff_job_id,
                session_id=eff_session_id,
                chat_id=eff_chat_id,
                project_id=eff_project_id,
                **call_kwargs,
            )
            return _ensure_tool_result(
                res, default_error_code="SEARCH_EXECUTION_ERROR"
            )
        except Exception as exc:
            logger.warning(
                "Core WebSearchTool execution error for query %r: %s",
                clean_q,
                exc,
            )
            return ToolResult(
                output=f"Search Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "WEB_SEARCH_EXECUTOR_EXCEPTION",
                    "requested_max_results": max_results,
                },
            )

    return _execute_web_search


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------
#
# ``requires_context`` declares which keyword arguments the factory
# accepts from the caller's execution context. ``resolve_tool_runtime``
# uses this to filter the context dict before invoking the factory,
# avoiding signature introspection at call time.

ADAPTER_SPEC = AdapterSpec(
    tool_names=("web_search",),
    factory=create_web_search_executor,
    requires_context=frozenset(
        {
            "search_config",
            "run_id",
            "job_id",
            "session_id",
            "chat_id",
            "project_id",
        }
    ),
)


__all__ = ["create_web_search_executor", "ADAPTER_SPEC"]
