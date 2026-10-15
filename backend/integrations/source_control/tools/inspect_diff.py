from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from pydantic import BaseModel, Field

from ..domain.enums import FileChangeType, ProviderId
from ..domain.models import DiffSummary, FileDiff
from ..domain.policies import is_workflow_path
from ..errors import (
    NotFoundError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..git.diff import GitDiffEngine
from ..git.path_policy import secure_resolve_path
from ..git.workspace import GitWorkspaceManager
from ..security.approval import compute_diff_sha256
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.redaction import redact_text


class InspectDiffArgs(BaseModel):
    """Input argument schema for the inspect diff tool."""

    workspace_id: Optional[str] = Field(
        default=None,
        description="Unique identifier of the isolated Git workspace to inspect.",
    )
    workspace_path: Optional[str] = Field(
        default=None,
        description="Local directory path of the git repository/workspace to inspect (alternative to workspace_id).",
    )
    base_ref: Optional[str] = Field(
        default=None,
        description="Optional base Git revision, branch, or commit SHA to compare against. Defaults to workspace base_sha or HEAD.",
    )
    target_ref: Optional[str] = Field(
        default=None,
        description="Optional target Git revision or branch. Defaults to current working tree.",
    )
    max_diff_lines: int = Field(
        default=300,
        ge=10,
        le=1000,
        description="Maximum number of unified diff lines to include in the output sample. Defaults to 300.",
    )


class InspectDiffTool:
    """Agent tool that computes unified diffs, extracts file metrics, and calculates cryptographic fingerprints."""

    name: str = "source_control_inspect_diff"
    description: str = (
        "Computes unified diffs and file change summaries for a local workspace or repository. "
        "Produces a cryptographic SHA-256 fingerprint for approval gating, detects CI/CD workflow modifications, "
        "and flags binary files. Never requires or accepts raw credentials."
    )
    args_schema: type[BaseModel] = InspectDiffArgs

    def __init__(
        self,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        diff_engine: Optional[GitDiffEngine] = None,
        policy_engine: Optional[PolicyEngine] = None,
        default_workspace_dir: Optional[str | Path] = None,
    ) -> None:
        base_dir = Path(default_workspace_dir or os.getcwd()).resolve()
        self.workspace_manager = workspace_manager or GitWorkspaceManager(base_workspace_dir=base_dir)
        self.diff_engine = diff_engine or GitDiffEngine()
        self.policy_engine = policy_engine or get_default_policy_engine()

    async def _arun(
        self,
        workspace_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        base_ref: Optional[str] = None,
        target_ref: Optional[str] = None,
        max_diff_lines: int = 300,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously computes unified diff and metrics for the specified workspace."""
        target_dir: Optional[Path] = None
        resolved_base: Optional[str] = base_ref.strip() if base_ref and base_ref.strip() else None

        # 1. Resolve workspace directory from ID or explicit path
        if workspace_id and workspace_id.strip():
            clean_id = workspace_id.strip()
            ws_ref = self.workspace_manager.get_workspace(clean_id)
            if ws_ref is None:
                raise NotFoundError(f"Workspace with ID '{clean_id}' was not found.")
            target_dir = Path(ws_ref.workspace_path)
            if not resolved_base:
                resolved_base = ws_ref.base_sha
        elif workspace_path and workspace_path.strip():
            raw_path = Path(workspace_path.strip())
            # Enforce absolute resolution and existence
            if not raw_path.exists():
                raise NotFoundError(f"Specified workspace directory '{workspace_path}' does not exist.")
            target_dir = raw_path.resolve()
        else:
            raise ValidationError("Either 'workspace_id' or 'workspace_path' must be provided.")

        if not target_dir.is_dir():
            raise ValidationError(f"Target path '{target_dir}' is not a directory.")

        # 2. Enforce operational policy
        allowed, reason = self.policy_engine.is_operation_allowed(
            operation="inspect_diff",
            workspace=str(target_dir),
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits inspecting diff on workspace '{target_dir}': {reason}",
                details={"workspace": str(target_dir), "reason": reason},
            )

        # 3. Default base_ref if unresolved
        effective_base = resolved_base or "HEAD"
        effective_target = target_ref.strip() if target_ref and target_ref.strip() else None

        # 4. Compute structured diff metrics and cryptographic fingerprint
        try:
            diff_summary: DiffSummary = await self.diff_engine.compute_diff(
                cwd=target_dir,
                base_ref=effective_base,
                target_ref=effective_target,
            )
            raw_diff: str = await self.diff_engine.get_raw_diff(
                cwd=target_dir,
                base_ref=effective_base,
                target_ref=effective_target,
            )
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to compute diff in workspace '{target_dir}': {exc}"
            ) from exc

        # 5. Inspect for CI/CD workflow files and binary assets
        touches_workflow = self.diff_engine.contains_workflow_changes(diff_summary)
        workflow_files = list(self.diff_engine.get_workflow_files(diff_summary))
        has_binary = self.diff_engine.contains_binary_files(diff_summary)

        # 6. Apply secret redaction and budget-friendly line truncation
        clean_raw_diff = redact_text(raw_diff)
        diff_lines = clean_raw_diff.splitlines()
        clamped_max_lines = max(10, min(max_diff_lines, 1000))

        truncated = False
        if len(diff_lines) > clamped_max_lines:
            truncated = True
            diff_sample = "\n".join(diff_lines[:clamped_max_lines]) + (
                f"\n\n... [Diff truncated: showing {clamped_max_lines} of {len(diff_lines)} lines] ..."
            )
        else:
            diff_sample = clean_raw_diff

        # 7. Format structured file metrics list
        files_summary = [
            {
                "path": f.path,
                "change_type": f.change_type.value,
                "insertions": f.insertions,
                "deletions": f.deletions,
                "old_path": f.old_path,
                "is_binary": f.is_binary,
                "is_workflow": is_workflow_path(f.path),
            }
            for f in diff_summary.files
        ]

        return {
            "status": "success",
            "workspace_id": workspace_id,
            "workspace_path": str(target_dir),
            "base_sha": diff_summary.base_sha,
            "head_sha": diff_summary.head_sha,
            "diff_sha256": diff_summary.diff_sha256,
            "total_files": diff_summary.total_files,
            "insertions": diff_summary.insertions,
            "deletions": diff_summary.deletions,
            "touches_workflow_files": touches_workflow,
            "workflow_files": workflow_files,
            "has_binary_files": has_binary,
            "is_truncated": truncated,
            "files": files_summary,
            "diff_sample": diff_sample,
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "InspectDiffTool only supports asynchronous execution via aexecute() or _arun()."
        )
