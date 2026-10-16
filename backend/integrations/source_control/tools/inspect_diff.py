# backend/integrations/source_control/tools/inspect_diff.py
"""
The `source_control.get_diff` tool.

Computes the diff of a workspace against a base commit and returns
a normalized summary to the agent. The summary includes the
canonical SHA-256 of the diff, which is the value bound into
approval tickets.

This tool is entirely local. It does not contact the provider and
does not require a credential.

Design rules:
    - The tool operates on a workspace identifier, not on an
      arbitrary path. The workspace path is resolved from the
      workspace manager.
    - The tool computes the diff using the same engine that the
      approval flow uses, so that the SHA the agent sees and the
      SHA bound into an approval ticket are always identical.
    - The tool bounds the size of the returned payload. Very large
      diffs are summarized; full patches are truncated by the
      engine itself.
    - The tool is read-only and requires no approval.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from ..domain.models import DiffSummary, FileDiff, WorkspaceRef
from ..errors import to_source_control_error
from ..git.diff import (
    GitDiffEngine,
    MAX_PATCH_SIZE,
    get_default_diff_engine,
)
from ..git.workspace import (
    GitWorkspaceManager,
    get_default_workspace_manager,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "InspectDiffTool",
    "DEFAULT_MAX_FILES_IN_RESULT",
]


logger = logging.getLogger(__name__)


#: Maximum number of file entries included in the result. The full
#: diff is still hashed; only the visible list is bounded.
DEFAULT_MAX_FILES_IN_RESULT: int = 500


class InspectDiffTool(SourceControlTool):
    """
    Compute the diff of a workspace.

    Inputs:
        workspace_id:   The workspace to inspect. Required.
        base_sha:       Optional base commit. When omitted, the
                        engine compares against the workspace's
                        current HEAD.
        include_patch:  When True (default), each file carries its
                        unified patch text. When False, only
                        metadata is returned (faster, smaller).
    """

    def __init__(
        self,
        *,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        diff_engine: Optional[GitDiffEngine] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_workspace_manager = workspace_manager
        self._explicit_diff_engine = diff_engine

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.get_diff"

    @property
    def description(self) -> str:
        return (
            "Compute the diff of a workspace against a base commit, "
            "returning the list of changed files and the canonical "
            "diff hash."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace to inspect.",
                },
                "base_sha": {
                    "type": "string",
                    "description": (
                        "Optional base commit SHA. Defaults to the "
                        "workspace's current HEAD."
                    ),
                },
                "include_patch": {
                    "type": "boolean",
                    "description": (
                        "When true (default), include each file's "
                        "unified patch text."
                    ),
                    "default": True,
                },
            },
            "required": ["workspace_id"],
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

        base_sha = kwargs.get("base_sha")
        if base_sha is not None and not isinstance(base_sha, str):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`base_sha` must be a string.",
            )
        if isinstance(base_sha, str):
            base_sha = base_sha.strip() or None

        include_patch = bool(kwargs.get("include_patch", True))

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
                error_message="Workspace record is malformed.",
            )

        # --- Compute the diff ------------------------------------------
        diff_engine = (
            self._explicit_diff_engine or get_default_diff_engine()
        )

        try:
            summary = await diff_engine.compute_diff(
                workspace_id=workspace_id,
                base_sha=base_sha,
                include_patch=include_patch,
                max_file_size_bytes=MAX_PATCH_SIZE,
                workspace_path=workspace.workspace_path,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "workspace_id": workspace_id,
                    "retryable": normalized.is_retryable,
                },
            )

        if not isinstance(summary, DiffSummary):
            return self.failure_result(
                error_code="PROVIDER_ERROR",
                error_message=(
                    "Diff engine returned an unexpected payload."
                ),
            )

        # --- Serialize ------------------------------------------------
        diff_sha256 = summary.sha256()
        files_payload = self._serialize_files(
            summary.files,
            limit=DEFAULT_MAX_FILES_IN_RESULT,
            include_patch=include_patch,
        )

        return self.success_result(
            {
                "workspace_id": workspace_id,
                "base_sha": summary.base_sha,
                "head_sha": summary.head_sha,
                "diff_sha256": diff_sha256,
                "total_files": len(summary.files),
                "total_additions": summary.total_additions,
                "total_deletions": summary.total_deletions,
                "files": files_payload,
                "files_truncated": len(summary.files)
                > DEFAULT_MAX_FILES_IN_RESULT,
            },
            metadata={
                "workspace_id": workspace_id,
                "include_patch": include_patch,
            },
        )

    # --- Serialization --------------------------------------------------

    @staticmethod
    def _serialize_files(
        files: Any,
        *,
        limit: int,
        include_patch: bool,
    ) -> List[Dict[str, Any]]:
        """
        Serialize up to `limit` file entries.

        The order is the order produced by the engine, which is
        already stable (sorted by path).
        """
        serialized: List[Dict[str, Any]] = []
        for item in files:
            if not isinstance(item, FileDiff):
                continue
            serialized.append(
                {
                    "path": item.path,
                    "change_type": item.change_type.value,
                    "additions": item.additions,
                    "deletions": item.deletions,
                    "old_path": item.old_path,
                    "is_binary": item.is_binary,
                    "is_truncated": item.is_truncated,
                    "patch": item.patch if include_patch else None,
                }
            )
            if len(serialized) >= limit:
                break
        return serialized
