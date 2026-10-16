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
            },
            "required": ["workspace_id", "branch_name"],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        workspace_id = kwargs.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`workspace_id` must be a non-empty string."
                ),
            )
        workspace_id = workspace_id.strip()

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

        return self.success_result(
            {
                "workspace_id": workspace_id,
                "branch_name": branch.name,
                "head_sha": branch.head_sha,
                "is_default": branch.is_default,
                "is_protected": branch.is_protected,
            },
            metadata={
                "workspace_id": workspace_id,
                "from_sha": from_sha,
                "force": force,
            },
        )
