from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional
from pydantic import BaseModel, Field

from ..domain.enums import ProviderId
from ..domain.models import DiffSummary
from ..domain.policies import is_protected_branch, is_workflow_path
from ..errors import (
    NotFoundError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..git.diff import GitDiffEngine
from ..git.path_policy import validate_branch_name
from ..git.workspace import GitWorkspaceManager
from ..security.approval import (
    ApprovalGate,
    ApprovalTicket,
    compute_diff_sha256,
)
from ..security.policy_engine import PolicyEngine, get_default_policy_engine
from ..security.redaction import redact_text

# Resilient import for default approval gate and operation service
try:
    from ..security.approval import get_default_approval_gate
except ImportError:
    def get_default_approval_gate() -> ApprovalGate:
        return ApprovalGate()

try:
    from ..operations.service import OperationService, get_default_operation_service
except ImportError:
    OperationService = None  # type: ignore[assignment, misc]
    def get_default_operation_service() -> Any:
        return None


class RequestApprovalArgs(BaseModel):
    """Input argument schema for the request approval tool."""

    operation_type: str = Field(
        ...,
        description="The remote operation requiring approval (e.g., 'push_branch', 'create_change_request').",
    )
    target_branch: str = Field(
        ...,
        description="Target branch on the remote repository (e.g., 'feature/my-task' or 'main').",
    )
    workspace_id: Optional[str] = Field(
        default=None,
        description="Unique identifier of the isolated Git workspace containing the changes.",
    )
    workspace_path: Optional[str] = Field(
        default=None,
        description="Local directory path of the git repository/workspace (alternative to workspace_id).",
    )
    operation_id: Optional[str] = Field(
        default=None,
        description="Optional durable operation identifier to bind this approval request to.",
    )
    commit_message: Optional[str] = Field(
        default=None,
        description="Proposed commit message for the changes.",
    )
    title: Optional[str] = Field(
        default=None,
        description="Proposed title for the pull/merge request.",
    )
    description: Optional[str] = Field(
        default=None,
        description="Summary or rationale explaining the proposed modifications.",
    )
    base_ref: Optional[str] = Field(
        default=None,
        description="Optional base Git revision or branch to compare against. Defaults to workspace base or HEAD.",
    )


class RequestApprovalTool:
    """Agent tool that creates a cryptographically bound approval ticket for human review."""

    name: str = "source_control_request_approval"
    description: str = (
        "Submits local workspace changes for explicit human review and approval before any remote write. "
        "Calculates the cryptographic diff SHA-256 fingerprint, freezes the proposal, and halts execution. "
        "The agent cannot self-approve; execution pauses until the user approves the ticket via the UI."
    )
    args_schema: type[BaseModel] = RequestApprovalArgs

    def __init__(
        self,
        approval_gate: Optional[ApprovalGate] = None,
        policy_engine: Optional[PolicyEngine] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        diff_engine: Optional[GitDiffEngine] = None,
        operation_service: Optional[Any] = None,
        default_workspace_dir: Optional[str | Path] = None,
    ) -> None:
        base_dir = Path(default_workspace_dir or os.getcwd()).resolve()
        self.approval_gate = approval_gate or get_default_approval_gate()
        self.policy_engine = policy_engine or get_default_policy_engine()
        self.workspace_manager = workspace_manager or GitWorkspaceManager(base_workspace_dir=base_dir)
        self.diff_engine = diff_engine or GitDiffEngine()
        self.operation_service = operation_service or get_default_operation_service()

    async def _arun(
        self,
        operation_type: str,
        target_branch: str,
        workspace_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        operation_id: Optional[str] = None,
        commit_message: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        base_ref: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Asynchronously computes the diff fingerprint and generates a pending approval ticket."""
        # 1. Normalize and validate operation and target branch syntax
        clean_op = operation_type.strip().lower()
        if clean_op not in {"push_branch", "create_change_request", "merge_change_request"}:
            raise ValidationError(
                f"Unsupported operation type '{operation_type}'. Must be 'push_branch' or 'create_change_request'."
            )

        clean_target_branch = validate_branch_name(target_branch)

        # 2. Resolve workspace directory
        target_dir: Optional[Path] = None
        resolved_base: Optional[str] = base_ref.strip() if base_ref and base_ref.strip() else None
        repository_id: str = "local"

        if workspace_id and workspace_id.strip():
            clean_ws_id = workspace_id.strip()
            ws_ref = self.workspace_manager.get_workspace(clean_ws_id)
            if ws_ref is None:
                raise NotFoundError(f"Workspace with ID '{clean_ws_id}' was not found.")
            target_dir = Path(ws_ref.workspace_path)
            repository_id = ws_ref.repository_id
            if not resolved_base:
                resolved_base = ws_ref.base_sha
        elif workspace_path and workspace_path.strip():
            raw_path = Path(workspace_path.strip())
            if not raw_path.exists():
                raise NotFoundError(f"Specified workspace directory '{workspace_path}' does not exist.")
            target_dir = raw_path.resolve()
        else:
            raise ValidationError("Either 'workspace_id' or 'workspace_path' must be provided.")

        if not target_dir.is_dir():
            raise ValidationError(f"Target workspace path '{target_dir}' is not a directory.")

        # 3. Compute unified diff and cryptographic fingerprint
        effective_base = resolved_base or "HEAD"
        try:
            diff_summary: DiffSummary = await self.diff_engine.compute_diff(
                cwd=target_dir,
                base_ref=effective_base,
            )
        except SourceControlError:
            raise
        except Exception as exc:
            raise SourceControlError(
                f"Failed to inspect diff for approval in '{target_dir}': {exc}"
            ) from exc

        if diff_summary.total_files == 0:
            raise ValidationError(
                f"No changes detected in workspace '{target_dir}' compared to '{effective_base}'. "
                "Cannot create an approval request for an empty diff."
            )

        # 4. Enforce policy and detect elevated security risks
        touches_workflow = self.diff_engine.contains_workflow_changes(diff_summary)
        is_protected = is_protected_branch(clean_target_branch)

        allowed, reason = self.policy_engine.is_operation_allowed(
            operation=clean_op,
            target_branch=clean_target_branch,
            workspace=str(target_dir),
        )
        if not allowed:
            raise PermissionDeniedError(
                f"Policy prohibits requesting approval for '{clean_op}': {reason}",
                details={
                    "operation": clean_op,
                    "target_branch": clean_target_branch,
                    "reason": reason,
                },
            )

        # 5. Sanitize text payloads
        clean_commit_msg = redact_text(commit_message.strip()) if commit_message else ""
        clean_title = redact_text(title.strip()) if title else ""
        clean_desc = redact_text(description.strip()) if description else ""

        # 6. Create cryptographic approval ticket
        bound_op_id = operation_id.strip() if operation_id and operation_id.strip() else f"op_{os.urandom(6).hex()}"

        try:
            ticket: ApprovalTicket = self.approval_gate.create_ticket(
                operation_id=bound_op_id,
                repository_id=repository_id,
                operation=clean_op,
                base_sha=diff_summary.base_sha,
                diff_sha256=diff_summary.diff_sha256,
                target_branch=clean_target_branch,
            )
        except Exception as exc:
            raise SourceControlError(f"Failed to register approval ticket: {exc}") from exc

        # 7. Transition durable operation if service is wired
        if self.operation_service and operation_id:
            try:
                from ..operations.models import OperationState

                await self.operation_service.transition_state(
                    operation_id=bound_op_id,
                    target_state=OperationState.REVIEW_REQUIRED,
                    metadata={
                        "approval_id": ticket.approval_id,
                        "diff_sha256": ticket.diff_sha256,
                        "base_sha": ticket.base_sha,
                        "target_branch": ticket.target_branch,
                        "commit_message": clean_commit_msg,
                        "title": clean_title,
                        "description": clean_desc,
                        "touches_workflow_files": touches_workflow,
                    },
                )
            except Exception:
                # Do not block approval ticket creation if operation journaling fails non-fatally
                pass

        return {
            "status": "pending_approval",
            "approval_id": ticket.approval_id,
            "operation_id": bound_op_id,
            "operation_type": clean_op,
            "repository_id": repository_id,
            "target_branch": clean_target_branch,
            "base_sha": ticket.base_sha,
            "diff_sha256": ticket.diff_sha256,
            "total_files": diff_summary.total_files,
            "insertions": diff_summary.insertions,
            "deletions": diff_summary.deletions,
            "touches_workflow_files": touches_workflow,
            "is_protected_target": is_protected,
            "expires_at": ticket.expires_at.isoformat(),
            "message": (
                f"Approval ticket '{ticket.approval_id}' created successfully. "
                "Autonomous agent execution is now paused. A human user must review the diff "
                "and explicitly approve this ticket before any remote push or change request can be performed."
            ),
        }

    async def aexecute(self, **kwargs: Any) -> dict[str, Any]:
        """Public async execution adapter conforming to standard agent tool calling contracts."""
        validated_args = self.args_schema(**kwargs)
        return await self._arun(**validated_args.model_dump())

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Synchronous wrapper raising an error to enforce non-blocking execution."""
        raise NotImplementedError(
            "RequestApprovalTool only supports asynchronous execution via aexecute() or _arun()."
        )
