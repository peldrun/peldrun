# backend/omweb/adapters/core_adapter_parts/tools_adapters/source_control_adapter.py
"""
Source Control Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``source_control`` tool to the canonical
peldrun-core ``SourceControlTool`` dispatcher. The Core tool is a
unified dispatcher: the model submits an ``action`` plus optional
``params`` and the Core tool routes the call to the appropriate
integration sub-tool (list_repositories, read_file, create_branch,
inspect_diff, run_checks, request_approval, push_branch,
create_change_request).

Served tool names:
  * ``source_control``

Factory signature:
  ``create_source_control_executor(workspace_dir=None, **kwargs)``
  returns an async callable ``(action, params=None, **kwargs) ->
  ToolResult``.

Design invariants:
  * The Core dispatcher (``SourceControlTool._arun``) already converts
    every sub-tool failure into a canonical ``ToolResult``. The
    adapter does NOT re-classify errors; it only normalizes the
    output shape via ``_ensure_tool_result``.
  * ``None`` values in ``call_kwargs`` are filtered out so that the
    Core dispatcher's ``extra="allow"`` schema does not treat them as
    explicit overrides of sub-tool defaults.
  * The adapter is stateless across calls; the ``SourceControlTool``
    instance is created once per factory invocation and reused.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from peldrun.tools.builtins.source_control_tool import SourceControlTool
from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import (
    WorkspacePathType,
    _ensure_tool_result,
    _resolve_workspace_path,
)

logger = logging.getLogger("omweb.adapters.tools_adapters.source_control")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_source_control_executor(
    workspace_dir: WorkspacePathType = None,
    *,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    session_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``source_control`` bound to
    ``workspace_dir``.

    The returned callable accepts ``action`` (required) plus any of
    the parameters declared in the JSON manifest, either at the top
    level or nested inside ``params``. It returns a canonical
    ``ToolResult``.

    Notes on the ``workspace_dir`` argument
    ---------------------------------------
    The Core ``SourceControlTool`` stores the workspace root on
    itself, but sub-tools resolve the workspace through their own
    process-wide default managers (``get_default_workspace_manager``
    and friends). Passing ``workspace_dir`` here therefore ensures
    that the tool is constructed with the right workspace context,
    but the actual workspace resolution happens in the sub-tools
    via their ``workspace_id`` argument. This is by design: the
    workspace registry is a shared resource populated by the
    application layer (through ``WorkspaceManager.create_workspace``
    calls), and sub-tools look up by stable ``workspace_id``, not by
    filesystem path.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    core_tool = SourceControlTool(workspace_root=str(ws_path))

    # Build the context metadata forwarded to every sub-tool through
    # the dispatcher. This is how chat-scoped operations (notably
    # create_workspace) learn the correct chat and project IDs so that
    # their files are placed inside the chat's session directory
    # instead of the global workspace root.
    forwarded_metadata: Dict[str, str] = {}
    if run_id:
        forwarded_metadata["run_id"] = run_id
    if job_id:
        forwarded_metadata["job_id"] = job_id
    if session_id:
        forwarded_metadata["session_id"] = session_id
    if chat_id:
        forwarded_metadata["chat_id"] = chat_id
    if project_id:
        forwarded_metadata["project_id"] = project_id

    # ws_path is derived from `workspace_dir`, which the engine sets to
    # the chat session's files directory (project_dir). Expose it so that
    # create_workspace places its Git repository exactly where the agent's
    # editing tools operate; otherwise the diff would be computed against
    # a directory the agent never touched.
    forwarded_metadata["project_dir"] = str(ws_path)

    async def _execute_source_control(
        action: str,
        params: Optional[Dict[str, Any]] = None,
        **call_kwargs: Any,
    ) -> ToolResult:
        """
        Dispatch a single source-control action via the Core tool.

        ``action`` is required by the JSON schema and by
        ``SourceControlArgs``. ``params``, when supplied, is merged
        with the top-level extras by the Core dispatcher.
        """
        await_args: Dict[str, Any] = {"action": action}

        # Only forward ``params`` when the caller actually supplied one,
        # so that the Core dispatcher's default (``None``) is preserved
        # for the no-params case.
        if params is not None:
            await_args["params"] = params

        # Filter out ``None`` extras so that sub-tools see only the
        # parameters the caller explicitly set. Without this, a
        # caller passing ``repository_id=None`` would override a
        # sub-tool default instead of leaving it untouched.
        await_args.update(
            {key: value for key, value in call_kwargs.items() if value is not None}
        )

        # Inject the forwarded context metadata. The Core dispatcher
        # pops "context_metadata" into ToolContext.metadata and
        # forwards "run_id" as ToolContext.run_id. Both are required:
        #   - run_id: identifies the current agent run.
        #   - chat_id / project_id / session_id / job_id: identify the
        #     chat session so that workspace-scoped tools (e.g.
        #     create_workspace) place their files alongside the
        #     session's existing files.
        if forwarded_metadata:
            await_args["context_metadata"] = dict(forwarded_metadata)
        if run_id:
            await_args["run_id"] = run_id

        try:
            res = await core_tool.aexecute(**await_args)
            return _ensure_tool_result(
                res, default_error_code="SOURCE_CONTROL_ERROR"
            )
        except Exception as exc:  # noqa: BLE001
            # The Core dispatcher already catches domain errors, so
            # reaching here means something unexpected happened before
            # the dispatcher could run (e.g., argument validation in
            # BaseTool raised a non-ValueError). Wrap it so the
            # Web layer always sees a ToolResult.
            logger.warning(
                "source_control exception in %s: %s",
                ws_path,
                exc,
            )
            return ToolResult(
                output=f"Source Control Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "SOURCE_CONTROL_EXCEPTION"},
            )

    return _execute_source_control


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------

ADAPTER_SPEC = AdapterSpec(
    tool_names=("source_control",),
    factory=create_source_control_executor,
    # Declare the context keys the factory accepts. Without this
    # declaration, AdapterSpec.matches_context would return an empty
    # dict and the adapter would never receive the chat-scoped IDs
    # it needs to place workspaces inside the session directory.
    requires_context=frozenset(
        {
            "run_id",
            "job_id",
            "session_id",
            "chat_id",
            "project_id",
        }
    ),
)


__all__ = ["create_source_control_executor", "ADAPTER_SPEC"]
