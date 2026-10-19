# backend/integrations/source_control/tools/request_approval.py
"""
The `source_control.request_approval` tool.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..domain.enums import OperationStatus
from ..errors import to_source_control_error
from ..git.diff import get_default_diff_engine
from ..git.workspace import get_default_workspace_manager
from ..operations.service import (
    OperationService,
    get_default_operation_service,
)
from ..security.approval import (
    ApprovalGate,
    get_default_approval_gate,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "RequestApprovalTool",
    "SUPPORTED_OPERATIONS",
    "DEFAULT_TTL_SECONDS",
]

logger = logging.getLogger(__name__)

SUPPORTED_OPERATIONS: tuple[str, ...] = (
    "push_branch",
    "create_change_request",
    "delete_remote_branch",
    "merge_change_request",
)

DEFAULT_TTL_SECONDS: int = 15 * 60


class RequestApprovalTool(SourceControlTool):
    """
    Create a pending approval ticket for a remote side effect.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        approval_gate: Optional[ApprovalGate] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service
        self._explicit_approval_gate = approval_gate

    @property
    def name(self) -> str:
        return "source_control.request_approval"

    @property
    def description(self) -> str:
        return (
            "Create a pending approval request for a remote side effect. "
            "The approval is NOT granted by this tool; the user must grant it through the PELDRUN UI."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Identifier of the workspace (optional if operation_id is provided)."
                },
                "operation_id": {
                    "type": "string",
                    "description": "Identifier of the operation (optional if workspace_id is provided)."
                },
                "operation": {
                    "type": "string",
                    "enum": list(SUPPORTED_OPERATIONS),
                    "default": "push_branch",
                    "description": "The remote side effect the approval covers."
                },
                "target_branch": {
                    "type": "string",
                    "description": "The remote branch the operation targets."
                },
                "ttl_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional TTL for the approval, in seconds."
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        workspace_id = kwargs.get("workspace_id")
        if isinstance(workspace_id, str):
            workspace_id = workspace_id.strip()
            if workspace_id.lower() in ("none", "null", ""):
                workspace_id = None

        operation_id = kwargs.get("operation_id")
        if isinstance(operation_id, str):
            operation_id = operation_id.strip()
            if operation_id.lower() in ("none", "null", ""):
                operation_id = None

        if not operation_id and not workspace_id:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="Either `operation_id` or `workspace_id` must be provided."
            )

        # Accept common aliases and fall back to push_branch
        raw_op = str(kwargs.get("operation") or "push_branch").strip().lower()
        operation_aliases = {
            "commit": "push_branch",
            "push": "push_branch",
            "pr": "create_change_request",
            "pull_request": "create_change_request",
            "merge": "merge_change_request",
        }
        operation = operation_aliases.get(raw_op, raw_op)
        if operation not in SUPPORTED_OPERATIONS:
            operation = "push_branch" # Safest default

        # Ignore title/body (they belong to create_change_request)
        kwargs.pop("title", None)
        kwargs.pop("body", None)
        kwargs.pop("draft", None)

        # Default target_branch to "main" if missing
        target_branch = kwargs.get("target_branch")
        if not isinstance(target_branch, str) or not target_branch.strip():
            target_branch = "main"
        target_branch = target_branch.strip()

        ttl_raw = kwargs.get("ttl_seconds")
        ttl_seconds: Optional[int] = None
        if ttl_raw is not None and ttl_raw != "":
            try:
                ttl_seconds = int(ttl_raw)
            except (TypeError, ValueError):
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message="`ttl_seconds` must be an integer.",
                )
            if ttl_seconds < 1:
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message="`ttl_seconds` must be at least 1.",
                )

        # --- Resolve services ------------------------------------------
        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )
        approval_gate = (
            self._explicit_approval_gate or get_default_approval_gate()
        )

        # --- Create operation if only workspace_id is provided ---------
        if not operation_id and workspace_id:
            ws_manager = get_default_workspace_manager()
            ws = ws_manager.get_workspace(workspace_id)
            if not ws:
                return self.failure_result(
                    error_code="WORKSPACE_NOT_FOUND",
                    error_message=f"Workspace '{workspace_id}' not found.",
                )

            try:
                # Auto-commit any uncommitted changes before diff
                import subprocess
                try:
                    subprocess.run(
                        ["git", "add", "-A"],
                        cwd=ws.workspace_path,
                        check=False,
                        capture_output=True,
                        timeout=30,
                    )
                    subprocess.run(
                        ["git", "-c", "user.name=PELDRUN", "-c", "user.email=peldrun@localhost",
                         "commit", "-m", f"PELDRUN: {operation}"],
                        cwd=ws.workspace_path,
                        check=False,
                        capture_output=True,
                        timeout=30,
                    )
                except Exception:
                    pass

                # 1. Create operation dynamically
                record = await operation_service.create_operation(
                    repository_id=ws.repository_id,
                    kind=operation,
                    provider=ws.provider,
                    target_branch=target_branch,
                    workspace_id=workspace_id,
                )
                operation_id = record.operation_id

                # 2. Advance the state machine correctly (CREATED -> REVIEW_REQUIRED)
                # This prevents the ConflictError that was causing the infinite loop.
                for state in (
                    OperationStatus.REPOSITORY_RESOLVED,
                    OperationStatus.WORKSPACE_READY,
                    OperationStatus.EDITING,
                ):
                    try:
                        await operation_service.transition(operation_id, state, actor="system")
                    except Exception:
                        pass # Ignore if already in that state

                # 3. Compute and attach the diff
                # We use the SHA-1 base_sha for Git diff.
                import hashlib
                git_base_sha = ws.base_sha or None
                diff_engine = get_default_diff_engine()
                summary = await diff_engine.compute_diff(
                    workspace_id=workspace_id,
                    base_sha=git_base_sha,
                    include_patch=True,
                    workspace_path=ws.workspace_path,
                )
                diff_hash = summary.sha256()

                # The ApprovalGate requires a 64-character SHA-256 digest
                # for base_sha. We hash the SHA-1 value we used for Git.
                sha256_base = hashlib.sha256((summary.base_sha or "").encode()).hexdigest()

                await operation_service.attach_diff(
                    operation_id=operation_id,
                    diff_sha256=diff_hash,
                    base_sha=sha256_base,
                )

                # 4. Transition to REVIEW_REQUIRED
                await operation_service.transition(
                    operation_id,
                    OperationStatus.REVIEW_REQUIRED,
                    actor="system",
                )
            except Exception as exc:
                normalized = to_source_control_error(exc)
                return self.failure_result(
                    error_code=normalized.code.value,
                    error_message=normalized.message,
                    metadata={"workspace_id": workspace_id},
                )

        # --- Fetch the operation record --------------------------------
        try:
            record = await operation_service.get_operation(operation_id)
        except Exception as exc:
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Validate the operation state ------------------------------
        if record.status in {
            OperationStatus.CANCELLED,
            OperationStatus.FAILED_FINAL,
            OperationStatus.REJECTED,
        }:
            return self.failure_result(
                error_code="CONFLICT",
                error_message=f"Operation is in status '{record.status.value}' and cannot request approval.",
                metadata={"operation_id": operation_id},
            )

        if not record.base_sha or not record.diff_sha256:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="Operation has no base SHA or diff attached.",
                metadata={"operation_id": operation_id},
            )

        # --- Create the ticket -----------------------------------------
        try:
            # The record.base_sha is now the SHA-256 digest (stored
            # via attach_diff above). It is already 64 characters.
            ticket = approval_gate.create_ticket(
                operation_id=operation_id,
                repository_id=record.repository_id,
                operation=operation,
                base_sha=record.base_sha,
                diff_sha256=record.diff_sha256,
                target_branch=target_branch,
                ttl_seconds=ttl_seconds,
            )
        except Exception as exc:
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id, "operation": operation},
            )

        # --- Attach the approval to the operation ----------------------
        try:
            await operation_service.attach_approval(operation_id, ticket.approval_id)
        except Exception:
            pass

        # --- Return success and instruct the agent to stop -------------
        return self.success_result(
            {
                "approval_id": ticket.approval_id,
                "operation_id": ticket.operation_id,
                "operation": ticket.operation,
                "target_branch": ticket.target_branch,
                "status": ticket.status.value,
                "expires_at": ticket.expires_at.isoformat(),
                "message": "Approval ticket created. Please wait for the user to review and approve in the UI. DO NOT ask the user manually.",
            },
            metadata={"operation_id": operation_id, "operation": operation},
        )
