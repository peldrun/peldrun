from __future__ import annotations

import dataclasses
import secrets
import threading
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ..domain.enums import (
    ApprovalDecision,
    OperationKind,
    OperationStatus,
    ProviderId,
    SourceControlErrorCode,
)
from ..domain.models import ApprovalTicket
from ..errors import (
    ApprovalInvalidError,
    ApprovalRequiredError,
    ConflictError,
    PermissionDeniedError,
    SourceControlError,
    ValidationError,
)
from ..registry import ProviderRegistry, get_default_registry
from ..security.approval import (
    ApprovalManager,
    get_default_approval_manager,
)
from ..security.policy_engine import (
    PolicyEngine,
    get_default_policy_engine,
)
from .idempotency import IdempotencyManager, get_default_idempotency_manager
from .journal import OperationJournal, get_default_operation_journal
from .models import OperationEvent, OperationRecord
from .recovery import RecoveryEngine, RecoveryReport, get_default_recovery_engine


class OperationService:
    """Thread-safe core service orchestrating operation lifecycles, approvals, transitions, and journal persistence."""

    def __init__(
        self,
        journal: Optional[OperationJournal] = None,
        approval_manager: Optional[ApprovalManager] = None,
        policy_engine: Optional[PolicyEngine] = None,
        idempotency_manager: Optional[IdempotencyManager] = None,
        recovery_engine: Optional[RecoveryEngine] = None,
        registry: Optional[ProviderRegistry] = None,
    ) -> None:
        self._journal = journal
        self._approval_manager = approval_manager
        self._policy_engine = policy_engine
        self._idempotency_manager = idempotency_manager
        self._recovery_engine = recovery_engine
        self._registry = registry
        self._lock = threading.Lock()

    @property
    def journal(self) -> OperationJournal:
        if self._journal is None:
            self._journal = get_default_operation_journal()
        return self._journal

    @property
    def approval_manager(self) -> ApprovalManager:
        if self._approval_manager is None:
            self._approval_manager = get_default_approval_manager()
        return self._approval_manager

    @property
    def policy_engine(self) -> PolicyEngine:
        if self._policy_engine is None:
            self._policy_engine = get_default_policy_engine()
        return self._policy_engine

    @property
    def idempotency_manager(self) -> IdempotencyManager:
        if self._idempotency_manager is None:
            self._idempotency_manager = get_default_idempotency_manager(journal=self.journal)
        return self._idempotency_manager

    @property
    def recovery_engine(self) -> RecoveryEngine:
        if self._recovery_engine is None:
            self._recovery_engine = get_default_recovery_engine()
        return self._recovery_engine

    @property
    def registry(self) -> ProviderRegistry:
        if self._registry is None:
            self._registry = get_default_registry()
        return self._registry

    def create_operation(
        self,
        repository_id: str,
        provider: ProviderId,
        kind: OperationKind,
        *,
        run_id: Optional[str] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """Initializes and persists a new durable source control operation."""
        if not repository_id or not repository_id.strip():
            raise ValidationError("Repository ID cannot be empty.")

        operation_id = f"sc_op_{secrets.token_hex(16)}"
        now = datetime.utcnow()

        operation = OperationRecord(
            operation_id=operation_id,
            repository_id=repository_id.strip(),
            provider=provider,
            kind=kind,
            status=OperationStatus.CREATED,
            run_id=run_id,
            source_branch=source_branch,
            target_branch=target_branch,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata) if metadata else {},
        )

        with self._lock:
            self.journal.save_operation(operation)
            self._record_transition_event(
                operation_id=operation_id,
                from_status=None,
                to_status=OperationStatus.CREATED,
                message=f"Operation '{operation_id}' initialized for kind '{kind.value}'.",
            )

        return operation

    def get_operation(self, operation_id: str) -> OperationRecord:
        """Retrieves an operation by ID. Raises ValidationError if not found."""
        clean_id = operation_id.strip()
        op = self.journal.get_operation(clean_id)
        if op is None:
            raise ValidationError(f"Source control operation '{clean_id}' does not exist.")
        return op

    def list_operations(
        self,
        repository_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[OperationStatus] = None,
        limit: int = 100,
    ) -> Sequence[OperationRecord]:
        """Lists operations matching filter criteria."""
        return self.journal.list_operations(
            repository_id=repository_id,
            run_id=run_id,
            status=status,
            limit=limit,
        )

    def transition_status(
        self,
        operation_id: str,
        target_status: OperationStatus,
        message: str = "",
        *,
        error_details: Optional[Mapping[str, Any]] = None,
        metadata_patch: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """Atomically transitions an operation to a valid target status."""
        with self._lock:
            op = self.get_operation(operation_id)

            if op.is_terminal:
                raise ConflictError(
                    f"Cannot transition terminal operation '{op.operation_id}' (status: {op.status.value})."
                )

            if op.status == target_status:
                return op

            if not op.can_transition_to(target_status):
                raise ConflictError(
                    f"Illegal state transition from '{op.status.value}' to '{target_status.value}' "
                    f"for operation '{op.operation_id}'."
                )

            now = datetime.utcnow()
            completed_at = now if target_status in {
                OperationStatus.COMPLETED,
                OperationStatus.CANCELLED,
                OperationStatus.FAILED_FINAL,
            } else op.completed_at

            updated_metadata = dict(op.metadata)
            if metadata_patch:
                updated_metadata.update(metadata_patch)

            merged_errors = dict(error_details) if error_details else op.error_details

            updated_op = dataclasses.replace(
                op,
                status=target_status,
                updated_at=now,
                completed_at=completed_at,
                metadata=updated_metadata,
                error_details=merged_errors,
            )

            self.journal.save_operation(updated_op)
            self._record_transition_event(
                operation_id=operation_id,
                from_status=op.status,
                to_status=target_status,
                message=message or f"Transitioned to {target_status.value}.",
                details=dict(error_details) if error_details else {},
            )

            return updated_op

    def attach_workspace(
        self,
        operation_id: str,
        workspace_id: str,
        source_branch: str,
        base_sha: str,
    ) -> OperationRecord:
        """Attaches an isolated Git workspace reference and transitions to WORKSPACE_READY."""
        with self._lock:
            op = self.get_operation(operation_id)
            updated_op = dataclasses.replace(
                op,
                workspace_id=workspace_id,
                source_branch=source_branch,
                base_sha=base_sha,
                updated_at=datetime.utcnow(),
            )
            self.journal.save_operation(updated_op)
            return self.transition_status(
                operation_id=operation_id,
                target_status=OperationStatus.WORKSPACE_READY,
                message=f"Workspace '{workspace_id}' bound on branch '{source_branch}' (base SHA: {base_sha[:8]}).",
            )

    def attach_diff(
        self,
        operation_id: str,
        head_sha: str,
        diff_sha256: str,
        total_files: int,
        diff_bytes: int,
    ) -> OperationRecord:
        """Attaches head SHA and verified diff fingerprint after policy limit validation."""
        self.policy_engine.validate_diff_limits(
            file_count=total_files,
            diff_bytes=diff_bytes,
            repository_id=self.get_operation(operation_id).repository_id,
        )

        with self._lock:
            op = self.get_operation(operation_id)
            updated_op = dataclasses.replace(
                op,
                head_sha=head_sha,
                diff_sha256=diff_sha256,
                updated_at=datetime.utcnow(),
            )
            self.journal.save_operation(updated_op)
            return self.transition_status(
                operation_id=operation_id,
                target_status=OperationStatus.REVIEW_REQUIRED,
                message=f"Diff generated (files: {total_files}, fingerprint: {diff_sha256[:12]}).",
            )

    def propose_approval(
        self,
        operation_id: str,
        *,
        commit_message: str = "",
        change_request_title: str = "",
        change_request_body: str = "",
        ttl_minutes: Optional[int] = None,
    ) -> ApprovalTicket:
        """Creates a cryptographically bound pending approval ticket for the operation."""
        op = self.get_operation(operation_id)

        if not op.base_sha or not op.diff_sha256 or not op.target_branch:
            raise ValidationError(
                f"Operation '{operation_id}' is not in an inspectable state (missing base_sha, diff_sha256, or target_branch)."
            )

        # Assert policy allows proposed mutation
        self.policy_engine.assert_permitted(
            operation=op.kind,
            repository_id=op.repository_id,
            target_branch=op.target_branch,
        )

        ticket = self.approval_manager.create_ticket(
            operation_id=op.operation_id,
            repository_id=op.repository_id,
            operation_kind=op.kind,
            base_sha=op.base_sha,
            diff_sha256=op.diff_sha256,
            source_branch=op.source_branch or "",
            target_branch=op.target_branch,
            commit_message=commit_message or op.commit_message or "",
            change_request_title=change_request_title or op.change_request_title or "",
            change_request_body=change_request_body or op.change_request_body or "",
            ttl_minutes=ttl_minutes,
        )

        with self._lock:
            updated_op = dataclasses.replace(
                op,
                approval_id=ticket.approval_id,
                commit_message=commit_message or op.commit_message,
                change_request_title=change_request_title or op.change_request_title,
                change_request_body=change_request_body or op.change_request_body,
                updated_at=datetime.utcnow(),
            )
            self.journal.save_operation(updated_op)
            self._record_transition_event(
                operation_id=operation_id,
                from_status=op.status,
                to_status=op.status,
                message=f"Approval ticket '{ticket.approval_id}' generated and waiting for user review.",
            )

        return ticket

    def approve_operation(
        self,
        operation_id: str,
        approval_id: str,
        approved_by: str = "user",
    ) -> OperationRecord:
        """Marks approval ticket as approved and advances operation state machine to APPROVED."""
        self.approval_manager.approve_ticket(approval_id, approved_by=approved_by)
        return self.transition_status(
            operation_id=operation_id,
            target_status=OperationStatus.APPROVED,
            message=f"Approval ticket '{approval_id}' approved by '{approved_by}'.",
        )

    def reject_operation(
        self,
        operation_id: str,
        approval_id: str,
        reason: Optional[str] = None,
    ) -> OperationRecord:
        """Rejects approval ticket and transitions operation to CANCELLED or BLOCKED."""
        self.approval_manager.reject_ticket(approval_id, rejection_reason=reason)
        return self.transition_status(
            operation_id=operation_id,
            target_status=OperationStatus.CANCELLED,
            message=f"Operation rejected by user. Reason: {reason or 'No reason provided'}.",
        )

    def assert_ready_to_push_and_consume(
        self,
        operation_id: str,
        approval_id: str,
        current_base_sha: str,
        current_diff_sha256: str,
    ) -> ApprovalTicket:
        """Validates that operation is approved, verifies live cryptographic hashes, and consumes ticket atomically."""
        op = self.get_operation(operation_id)

        if op.status != OperationStatus.APPROVED:
            raise ApprovalRequiredError(
                f"Operation '{operation_id}' is not in APPROVED state (current: {op.status.value}).",
                operation_id=operation_id,
            )

        consumed_ticket = self.approval_manager.validate_and_consume(
            approval_id=approval_id,
            operation_id=op.operation_id,
            repository_id=op.repository_id,
            operation_kind=op.kind,
            current_base_sha=current_base_sha,
            current_diff_sha256=current_diff_sha256,
            target_branch=op.target_branch or "",
            source_branch=op.source_branch,
        )

        self.transition_status(
            operation_id=operation_id,
            target_status=OperationStatus.PUSHING,
            message="Approval consumed successfully; remote push in progress.",
        )
        return consumed_ticket

    def record_pushed(
        self,
        operation_id: str,
        remote_branch: str,
        head_commit_sha: str,
    ) -> OperationRecord:
        """Records successful remote push and advances operation to PUSHED."""
        with self._lock:
            op = self.get_operation(operation_id)
            updated_op = dataclasses.replace(
                op,
                source_branch=remote_branch,
                head_sha=head_commit_sha,
                updated_at=datetime.utcnow(),
            )
            self.journal.save_operation(updated_op)
            return self.transition_status(
                operation_id=operation_id,
                target_status=OperationStatus.PUSHED,
                message=f"Branch '{remote_branch}' successfully pushed to remote at commit {head_commit_sha[:8]}.",
            )

    def record_change_request_created(
        self,
        operation_id: str,
        change_request_url: str,
        change_request_title: str,
    ) -> OperationRecord:
        """Records change request creation and advances operation to CHANGE_REQUEST_CREATED."""
        with self._lock:
            op = self.get_operation(operation_id)
            updated_op = dataclasses.replace(
                op,
                change_request_url=change_request_url,
                change_request_title=change_request_title,
                updated_at=datetime.utcnow(),
            )
            self.journal.save_operation(updated_op)
            return self.transition_status(
                operation_id=operation_id,
                target_status=OperationStatus.CHANGE_REQUEST_CREATED,
                message=f"Pull request created: {change_request_url}",
            )

    def complete_operation(
        self, operation_id: str, summary_message: str = "Operation completed successfully."
    ) -> OperationRecord:
        """Marks the operation as COMPLETED."""
        return self.transition_status(
            operation_id=operation_id,
            target_status=OperationStatus.COMPLETED,
            message=summary_message,
        )

    def fail_operation(
        self,
        operation_id: str,
        error_message: str,
        *,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """Marks the operation as failed (retryable or final)."""
        target_status = OperationStatus.FAILED_RETRYABLE if retryable else OperationStatus.FAILED_FINAL
        err = {"message": error_message}
        if details:
            err.update(details)

        return self.transition_status(
            operation_id=operation_id,
            target_status=target_status,
            message=f"Operation failed: {error_message}",
            error_details=err,
        )

    async def reconcile_operation(self, operation_id: str) -> RecoveryReport:
        """Triggers recovery reconciliation on an operation against remote reality."""
        op = self.get_operation(operation_id)
        return await self.recovery_engine.reconcile_operation(op)

    def _record_transition_event(
        self,
        operation_id: str,
        from_status: Optional[OperationStatus],
        to_status: OperationStatus,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        event = OperationEvent(
            event_id=f"evt_{secrets.token_hex(12)}",
            operation_id=operation_id,
            from_status=from_status,
            to_status=to_status,
            message=message,
            timestamp=datetime.utcnow(),
            details=details or {},
        )
        self.journal.record_event(event)


# ============================================================
# Default OperationService Singleton Accessors
# ============================================================

_DEFAULT_OPERATION_SERVICE: Optional[OperationService] = None
_SERVICE_LOCK = threading.Lock()


def get_default_operation_service() -> OperationService:
    """Returns the process-wide default OperationService singleton."""
    global _DEFAULT_OPERATION_SERVICE
    if _DEFAULT_OPERATION_SERVICE is None:
        with _SERVICE_LOCK:
            if _DEFAULT_OPERATION_SERVICE is None:
                _DEFAULT_OPERATION_SERVICE = OperationService()
    return _DEFAULT_OPERATION_SERVICE


def set_default_operation_service(service: OperationService) -> None:
    """Explicitly injects an OperationService instance (used during testing)."""
    global _DEFAULT_OPERATION_SERVICE
    with _SERVICE_LOCK:
        _DEFAULT_OPERATION_SERVICE = service


def reset_default_operation_service() -> None:
    """Resets the process-wide default OperationService (used during testing)."""
    global _DEFAULT_OPERATION_SERVICE
    with _SERVICE_LOCK:
        _DEFAULT_OPERATION_SERVICE = None
