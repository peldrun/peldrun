# backend/integrations/source_control/tools/create_branch.py
"""
The `source_control.create_branch` tool.

Creates a local branch inside an existing workspace. The workspace
MUST already exist; use `source_control.create_workspace` first if
it does not.

This tool is entirely local. It does not contact the provider, does
not require a credential, and does not modify any remote state. It
is safe to run without approval.

Design rules:
    - The tool operates on a workspace identifier, not on an
      arbitrary path. The workspace path is resolved by the
      workspace manager, which enforces the configured workspace
      root via the path policy.
    - The tool NEVER accepts a filesystem path from the caller. All
      paths are resolved server-side.
    - The tool uses `GitBranchManager`, which validates branch
      names before invoking Git. Invalid names produce a clear
      `VALIDATION_ERROR`.
    - The tool is idempotent only in the `force=True` sense: by
      default, creating a branch that already exists is a conflict.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..domain.models import BranchRef, WorkspaceRef
from ..errors import to_source_control_error
from ..git.branch import GitBranchManager, get_default_branch_manager
from ..git.workspace import GitWorkspaceManager, get_default_workspace_manager
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = ["CreateBranchTool"]


logger = logging.getLogger(__name__)


class CreateBranchTool(SourceControlTool):
    """
    Create a local branch inside a workspace.

    Inputs:
        workspace_id: The workspace to operate on. Required.
        branch_name:  Name of the branch to create. Required.
        from_sha:     Optional starting commit. When omitted, the
                      workspace's current HEAD is used.
        force:        When True, reset an existing branch to the
                      new starting point. Defaults to False.
    """

    def __init__(
        self,
        *,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        branch_manager: Optional[GitBranchManager] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_workspace_manager = workspace_manager
        self._explicit_branch_manager = branch_manager

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.create_branch"

    @property
    def description(self) -> str:
        return (
            "Create a local Git branch inside an existing workspace, "
            "optionally starting from a specific commit."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Identifier of the workspace to operate on."
                    ),
                },
                "branch_name": {
                    "type": "string",
                    "description": "Name of the branch to create.",
                },
                "from_sha": {
                    "type": "string",
                    "description": (
                        "Optional starting commit. Defaults to the "
                        "workspace's current HEAD."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "When true, reset an existing branch to the "
                        "new starting point."
                    ),
                    "default": False,
                },
                "checkout": {
                    "type": "boolean",
                    "description": (
                        "When true (default), switch the workspace "
                        "checkout to the newly created branch."
                    ),
                    "default": True,
                },
            },
            "required": ["branch_name"],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        workspace_id = kwargs.get("workspace_id")
        if isinstance(workspace_id, str):
            workspace_id = workspace_id.strip()
            if workspace_id.lower() in ("none", "null", ""):
                workspace_id = None
        else:
            workspace_id = None

        # Auto-resolve workspace_id from the most recent workspace
        # created in this chat session. Mirrors the identical fallback
        # in `request_approval.py`: small LLMs (including
        # GigaChat-3-Pro) succeed at `create_workspace` but forget to
        # forward the returned `workspace_id` to subsequent tool
        # calls. Without this, the tool loops on
        # "workspace_id must be a non-empty string" until the agent
        # gives up and asks the human — even though the workspace
        # demonstrably exists and is bound to this chat.
        #
        # Safety: the fallback only activates when the caller omits
        # `workspace_id`. It picks the workspace with the highest
        # `created_at` among workspaces whose `metadata.chat_id`
        # matches the current session's chat_id. If the caller
        # provides `workspace_id`, the fallback is skipped entirely.
        if not workspace_id:
            chat_id_ctx = ""
            if isinstance(context.metadata, dict):
                raw_chat = context.metadata.get("chat_id")
                if isinstance(raw_chat, str):
                    chat_id_ctx = raw_chat.strip()
            if chat_id_ctx:
                try:
                    mgr = get_default_workspace_manager()
                    candidates = [
                        ws
                        for ws in mgr.list_workspaces()
                        if isinstance(ws.metadata, dict)
                        and ws.metadata.get("chat_id") == chat_id_ctx
                    ]
                    if candidates:
                        workspace_id = candidates[-1].workspace_id
                        logger.info(
                            "create_branch: auto-resolved "
                            "workspace_id=%r from the most recent "
                            "workspace for chat_id=%r",
                            workspace_id,
                            chat_id_ctx,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "create_branch: auto-resolve of "
                        "workspace_id failed (%s); the caller will "
                        "receive the standard validation error.",
                        type(exc).__name__,
                    )

        if not workspace_id:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`workspace_id` is required and could not be "
                    "auto-resolved from the current chat. The value "
                    "is returned by the preceding `create_workspace` "
                    "call; pass it here explicitly."
                ),
            )

        branch_name = kwargs.get("branch_name")
        if not isinstance(branch_name, str) or not branch_name.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`branch_name` must be a non-empty string."
                ),
            )
        branch_name = branch_name.strip()

        from_sha = kwargs.get("from_sha")
        if from_sha is not None and not isinstance(from_sha, str):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`from_sha` must be a string.",
            )
        if isinstance(from_sha, str):
            from_sha = from_sha.strip() or None

        force = bool(kwargs.get("force", False))
        checkout = bool(kwargs.get("checkout", True))

        # --- Resolve workspace -----------------------------------------
        workspace_manager = (
            self._explicit_workspace_manager
            or get_default_workspace_manager()
        )

        workspace = workspace_manager.get_workspace(workspace_id)
        if workspace is None:
            return self.failure_result(
                error_code="WORKSPACE_NOT_FOUND",
                error_message=(
                    f"Workspace '{workspace_id}' is not registered."
                ),
                metadata={"workspace_id": workspace_id},
            )

        if not isinstance(workspace, WorkspaceRef):
            return self.failure_result(
                error_code="PROVIDER_ERROR",
                error_message=(
                    "Workspace record is malformed."
                ),
            )

        # --- Create the branch -----------------------------------------
        branch_manager = (
            self._explicit_branch_manager
            or get_default_branch_manager()
        )

        try:
            branch = await branch_manager.create_local_branch(
                workspace_id=workspace_id,
                branch_name=branch_name,
                from_sha=from_sha,
                workspace_path=workspace.workspace_path,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "workspace_id": workspace_id,
                    "branch_name": branch_name,
                    "retryable": normalized.is_retryable,
                },
            )

        if not isinstance(branch, BranchRef):
            return self.failure_result(
                error_code="PROVIDER_ERROR",
                error_message=(
                    "Branch manager returned an unexpected payload."
                ),
            )

        # --- Synchronize the workspace's tracked branch ---------------
        # `create_local_branch` above already performed `git checkout
        # -b <branch_name>`, which switches the workspace to the new
        # branch. The previous implementation additionally called
        # `checkout_branch`, which performs a dirty-workspace
        # preflight check and ALWAYS rejects in the typical agent
        # flow (the agent edits files BEFORE creating a branch). The
        # result was a hard failure that left the branch created but
        # the tool reporting failure — and the agent looping on
        # "branch already exists" on every retry.
        #
        # We no longer re-checkout. We only keep the workspace
        # registry's `current_branch` field in sync so that
        # subsequent operations (request_approval, push_branch) see
        # the correct current branch without a `git rev-parse`
        # round-trip.
        if checkout:
            try:
                if hasattr(workspace_manager, "set_current_branch"):
                    workspace_manager.set_current_branch(
                        workspace_id, branch_name
                    )
                elif (
                    hasattr(workspace_manager, "_workspaces")
                    and isinstance(
                        workspace_manager._workspaces, dict
                    )
                    and workspace_id in workspace_manager._workspaces
                ):
                    from dataclasses import replace as _dc_replace
                    current_ws = workspace_manager._workspaces[
                        workspace_id
                    ]
                    workspace_manager._workspaces[workspace_id] = (
                        _dc_replace(
                            current_ws,
                            current_branch=branch_name,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                # Best-effort sync: if the registry cannot be
                # updated, log and continue. The `git checkout -b`
                # already succeeded inside `create_local_branch`, so
                # the on-disk state is correct even if the in-memory
                # registry is momentarily stale.
                logger.warning(
                    "create_branch: could not sync "
                    "workspace.current_branch for %r: %s",
                    workspace_id,
                    type(exc).__name__,
                )

        return self.success_result(
            {
                "workspace_id": workspace_id,
                "branch_name": branch.name,
                "head_sha": branch.head_sha,
                "is_default": branch.is_default,
                "is_protected": branch.is_protected,
                "checked_out": checkout,
            },
            metadata={
                "workspace_id": workspace_id,
                "from_sha": from_sha,
                "force": force,
                "checkout": checkout,
            },
        )
