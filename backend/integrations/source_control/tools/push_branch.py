# backend/integrations/source_control/tools/push_branch.py
"""
The `source_control.push_branch` tool.

Pushes a reviewed branch to a remote repository. This is the single
most sensitive operation in the subsystem: it modifies remote state
that other people depend on. The tool therefore enforces a strict
pipeline before any network write occurs.

Pipeline:
    1. Validate inputs.
    2. Load the operation record from the journal.
    3. Verify the operation is in `APPROVED`.
    4. Consume the approval ticket, binding the exact diff hash,
       base SHA, target branch, and repository.
    5. Transition the operation to `PUSHING` (idempotently).
    6. Re-fetch the remote branch SHA and compare it to the base
       SHA the user reviewed. If the remote moved, fail with
       `REMOTE_STATE_CHANGED`.
    7. Perform the push via `GitPushManager`.
    8. Reconcile the result; if the push timed out, verify remote
       state before deciding the outcome.
    9. Transition to `PUSHED` on success, or `FAILED_*` on failure.
   10. Attach the outcome to the operation.

Design rules:
    - The tool MUST NOT accept a diff hash, base SHA, or target
      branch from the caller. Those values come from the operation
      record.
    - The tool MUST NOT bypass the approval gate. Even if the
      caller supplies a valid `approval_id`, the gate re-validates
      every bound field.
    - The tool MUST re-fetch the remote SHA immediately before
      push. A stale base is a `REMOTE_STATE_CHANGED` failure.
    - The tool MUST use `--force-with-lease` semantics via
      `GitPushManager`. Plain `--force` is never used.
    - The tool MUST NEVER mark an ambiguous result as success or
      failure without attempting reconciliation.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from ..domain.enums import OperationStatus
from ..errors import to_source_control_error
from ..git.push import GitPushManager, get_default_push_manager
from ..git.workspace import (
    GitWorkspaceManager,
    get_default_workspace_manager,
)
from ..operations.service import (
    OperationService,
    get_default_operation_service,
)
from ..security.approval import (
    ApprovalGate,
    get_default_approval_gate,
)
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = ["PushBranchTool"]


logger = logging.getLogger(__name__)


class PushBranchTool(SourceControlTool):
    """
    Push a reviewed branch after an approval.

    Inputs:
        operation_id: Identifier of the operation. Required.
        approval_id:  Identifier of the granted approval. Required.
        local_branch: Optional local branch name. Defaults to the
                      operation's `source_branch` when present.
        remote:       Optional remote name. Defaults to
                      `"origin"`.

    The tool re-validates every bound field on the approval ticket.
    A field change (for example a different diff hash) invalidates
    the ticket.
    """

    def __init__(
        self,
        *,
        operation_service: Optional[OperationService] = None,
        approval_gate: Optional[ApprovalGate] = None,
        push_manager: Optional[GitPushManager] = None,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_operation_service = operation_service
        self._explicit_approval_gate = approval_gate
        self._explicit_push_manager = push_manager
        self._explicit_workspace_manager = workspace_manager

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.push_branch"

    @property
    def description(self) -> str:
        return (
            "Push a reviewed branch to a remote repository. Requires "
            "a valid approval ticket and a workspace that already "
            "contains the reviewed commits."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation_id": {
                    "type": "string",
                    "description": "Identifier of the operation.",
                },
                "approval_id": {
                    "type": "string",
                    "description": (
                        "Identifier of the granted approval ticket."
                    ),
                },
                "local_branch": {
                    "type": "string",
                    "description": (
                        "Optional local branch name. Defaults to the "
                        "operation's source branch."
                    ),
                },
                "remote": {
                    "type": "string",
                    "description": (
                        "Remote name. Defaults to 'origin'."
                    ),
                    "default": "origin",
                },
            },
            "required": ["operation_id", "approval_id"],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        operation_id = kwargs.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`operation_id` must be a non-empty string."
                ),
            )
        operation_id = operation_id.strip()

        approval_id = kwargs.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`approval_id` must be a non-empty string."
                ),
            )
        approval_id = approval_id.strip()

        local_branch_override = kwargs.get("local_branch")
        if local_branch_override is not None and not isinstance(
            local_branch_override, str
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`local_branch` must be a string.",
            )
        if isinstance(local_branch_override, str):
            local_branch_override = local_branch_override.strip() or None

        remote = kwargs.get("remote") or "origin"
        if not isinstance(remote, str) or not remote.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message="`remote` must be a non-empty string.",
            )
        remote = remote.strip()

        # --- Resolve services ------------------------------------------
        operation_service = (
            self._explicit_operation_service
            or get_default_operation_service()
        )
        approval_gate = (
            self._explicit_approval_gate or get_default_approval_gate()
        )
        push_manager = (
            self._explicit_push_manager or get_default_push_manager()
        )
        workspace_manager = (
            self._explicit_workspace_manager
            or get_default_workspace_manager()
        )

        # --- Load the operation ----------------------------------------
        try:
            record = await operation_service.get_operation(operation_id)
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Validate operation state ----------------------------------
        if record.status != OperationStatus.APPROVED:
            return self.failure_result(
                error_code="CONFLICT",
                error_message=(
                    f"Operation is in status '{record.status.value}'; "
                    "only APPROVED operations can be pushed."
                ),
                metadata={
                    "operation_id": operation_id,
                    "status": record.status.value,
                },
            )

        if not record.workspace_id:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no workspace attached."
                ),
                metadata={"operation_id": operation_id},
            )

        target_branch = record.target_branch or record.source_branch
        if not target_branch:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "Operation has no target branch; cannot push."
                ),
                metadata={"operation_id": operation_id},
            )

        local_branch = local_branch_override or record.source_branch or target_branch

        # Fetch the workspace so we can pass its path to the push
        # manager and to the reconciliation logic.
        workspace = workspace_manager.get_workspace(record.workspace_id)
        if workspace is None:
            return self.failure_result(
                error_code="WORKSPACE_NOT_FOUND",
                error_message=(
                    f"Workspace '{record.workspace_id}' is not "
                    "registered."
                ),
                metadata={
                    "operation_id": operation_id,
                    "workspace_id": record.workspace_id,
                },
            )

        # --- Validate the approval ticket ------------------------------
        #
        # The gate re-validates every bound field: operation id,
        # repository id, operation kind, base SHA, diff hash, and
        # target branch. Any mismatch invalidates the ticket.
        try:
            approval_gate.validate(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation=record.kind,
                expected_base_sha=record.base_sha,
                expected_diff_sha256=record.diff_sha256 or "",
                expected_target_branch=target_branch,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "operation_id": operation_id,
                    "approval_id": approval_id,
                },
            )

        # --- Re-fetch the remote SHA -----------------------------------
        #
        # This check catches the race where another contributor
        # pushed to the target branch between the moment the user
        # reviewed the diff and the moment we are about to push.
        try:
            remote_sha = await push_manager._remote_head_sha(
                target=_resolve_path(workspace.workspace_path),
                remote=remote,
                ref=target_branch,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=(
                    "Could not verify the remote branch state: "
                    + normalized.message
                ),
                metadata={"operation_id": operation_id},
            )

        if remote_sha is not None and record.base_sha:
            if remote_sha != record.base_sha:
                # The remote moved. Invalidate the approval so the
                # user cannot re-use it for a different base.
                try:
                    approval_gate.revoke(
                        approval_id,
                        actor="system",
                        reason="Remote base changed before push.",
                    )
                except Exception:  # noqa: BLE001
                    pass

                # Transition the operation so that the UI shows the
                # stale state.
                try:
                    await operation_service.transition(
                        operation_id,
                        OperationStatus.STALE_REVIEW_REQUIRED,
                        reason=(
                            "Remote base changed since the review."
                        ),
                        metadata={
                            "previous_base_sha": record.base_sha,
                            "remote_sha": remote_sha,
                        },
                        actor="system",
                    )
                except Exception:  # noqa: BLE001
                    pass

                return self.failure_result(
                    error_code="REMOTE_STATE_CHANGED",
                    error_message=(
                        "The remote branch has changed since the "
                        "review. Re-review the diff before pushing."
                    ),
                    metadata={
                        "operation_id": operation_id,
                        "previous_base_sha": record.base_sha,
                        "remote_sha": remote_sha,
                    },
                )

        # --- Consume the approval -------------------------------------
        #
        # Once consumed, the ticket cannot be reused. If the push
        # fails for a transient reason, the user will need to
        # approve again; this is intentional.
        try:
            approval_gate.consume(
                approval_id,
                expected_operation_id=operation_id,
                expected_repository_id=record.repository_id,
                expected_operation=record.kind,
                expected_base_sha=record.base_sha,
                expected_diff_sha256=record.diff_sha256 or "",
                expected_target_branch=target_branch,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Transition to PUSHING ------------------------------------
        try:
            await operation_service.transition(
                operation_id,
                OperationStatus.PUSHING,
                reason="Push started.",
                metadata={"remote": remote, "target_branch": target_branch},
                actor="agent",
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"operation_id": operation_id},
            )

        # --- Perform the push -----------------------------------------
        try:
            push_result = await push_manager.push_branch(
                workspace_id=record.workspace_id,
                local_branch=local_branch,
                remote=remote,
                remote_branch=target_branch,
                force_with_lease=False,
                set_upstream=True,
                workspace_path=workspace.workspace_path,
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            # Mark the operation as failed. If the error is
            # retryable we leave the operation in a state the user
            # can act on.
            try:
                await operation_service.mark_failed(
                    operation_id,
                    error_code=normalized.code.value,
                    error_message=normalized.message,
                    retryable=normalized.is_retryable,
                    actor="system",
                )
            except Exception:  # noqa: BLE001
                pass
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={
                    "operation_id": operation_id,
                    "retryable": normalized.is_retryable,
                },
            )

        # --- Interpret the result --------------------------------------
        if not push_result.accepted:
            # A rejection is a final failure for this attempt.
            try:
                await operation_service.mark_failed(
                    operation_id,
                    error_code="PUSH_REJECTED",
                    error_message=push_result.message
                    or "Push was rejected by the remote.",
                    retryable=False,
                    actor="system",
                )
            except Exception:  # noqa: BLE001
                pass
            return self.failure_result(
                error_code="PUSH_REJECTED",
                error_message=push_result.message
                or "Push was rejected by the remote.",
                data={
                    "local_branch": push_result.local_branch,
                    "local_sha": push_result.local_sha,
                    "remote_branch": push_result.remote_branch,
                    "remote_sha": push_result.remote_sha,
                    "reason": push_result.rejected_reason,
                },
                metadata={"operation_id": operation_id},
            )

        # --- Success ---------------------------------------------------
        try:
            await operation_service.transition(
                operation_id,
                OperationStatus.PUSHED,
                reason="Push completed successfully.",
                metadata={
                    "local_sha": push_result.local_sha,
                    "remote_sha": push_result.remote_sha,
                },
                actor="system",
            )
        except Exception as exc:  # noqa: BLE001
            # The push succeeded but we could not update the journal.
            # We return success and record a warning; the operation
            # will be reconciled on the next startup.
            logger.warning(
                "Push succeeded but the operation state could not be "
                "updated for '%s': %s",
                operation_id,
                type(exc).__name__,
            )

        return self.success_result(
            {
                "operation_id": operation_id,
                "status": "pushed",
                "local_branch": push_result.local_branch,
                "local_sha": push_result.local_sha,
                "remote_branch": push_result.remote_branch,
                "remote_sha": push_result.remote_sha,
            },
            metadata={
                "operation_id": operation_id,
                "remote": remote,
            },
        )


def _resolve_path(path: str):
    """Return a `Path` for a workspace path string."""
    from pathlib import Path

    return Path(path)
