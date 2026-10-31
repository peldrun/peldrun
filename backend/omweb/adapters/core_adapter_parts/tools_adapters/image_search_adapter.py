# backend/omweb/adapters/core_adapter_parts/tools_adapters/image_search_adapter.py
"""
Image Search Adapter for PELDRUN Web-to-Core Layer.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from peldrun.tools.builtins.image_search import ImageSearchTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.image_search")


def create_image_search_executor(
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
    Create an async executor for ``image_search`` bound to
    ``workspace_dir`` and the given execution context.

    The returned callable accepts ``query`` and optional ``max_results``
    (omitted => policy defaults apply) and returns a canonical
    ``ToolResult``.

    Engineering guarantees:
      1. Preserves ``max_results=None`` when omitted by the tool call so
         that user settings (``search.image_search.default_max_results``,
         ``search.image_search.max_results_cap``) govern execution,
         matching the WebSearchTool contract.
      2. Forwards ``search_config`` and execution-context identifiers
         (run_id, job_id, session_id, chat_id, project_id) to the Core
         ImageSearchTool so image usage accounting and diagnostics stay
         in parity with text-based web_search.
      3. All provider dispatch happens through
         ``SearchProviderFactory.search_images`` inside the Core tool;
         this adapter never bypasses factory policy.
    """
    ws_root_str = (
        str(_resolve_workspace_path(workspace_dir))
        if workspace_dir is not None
        else None
    )
    core_tool = ImageSearchTool(
        workspace_root=ws_root_str,
        search_config=search_config,
    )

    async def _execute_image_search(
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
                output="Image Search Error: Query cannot be empty.",
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
            res = await core_tool.aexecute(
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
                res, default_error_code="IMAGE_SEARCH_ERROR"
            )
        except Exception as exc:
            logger.warning(
                "Image Search execution error for query %r: %s",
                clean_q,
                exc,
            )
            return ToolResult(
                output=f"Image Search Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "IMAGE_SEARCH_EXECUTOR_EXCEPTION",
                    "requested_max_results": max_results,
                },
            )

    return _execute_image_search


ADAPTER_SPEC = AdapterSpec(
    tool_names=("image_search",),
    factory=create_image_search_executor,
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
