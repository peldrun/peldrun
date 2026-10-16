from __future__ import annotations

import dataclasses
import secrets
import threading
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ..domain.enums import (
    ApprovalDecision,
    OperationKind,
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
    ApprovalGate,
    get_default_approval_gate,
)
from ..security.policy_engine import (
    PolicyEngine,
    get_default_policy_engine,
)
from .idempotency import IdempotencyManager, get_default_idempotency_manager
from .journal import OperationJournal, get_default_operation_journal
from .models import (
    OperationEvent,
    OperationRecord,
    OperationState,
    OperationStatus,
)
from .recovery import RecoveryEngine, RecoveryReport, get_default_recovery_engine


class OperationService:
    """Thread-safe core service orchestrating operation lifecycles, approvals, transitions, and journal persistence."""

    def __init__(
        self,
        journal: Optional[OperationJournal] = None,
        approval_manager: Optional[ApprovalGate] = None,
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
    def approval_manager(self) -> ApprovalGate:
        if self._approval_manager is None:
            self._approval_manager = get_default_approval_gate()
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

    async def create_operation(
        self,
        repository_id: Optional[str] = None,
        provider: Optional[Any] = None,
        kind: Optional[Any] = None,
        *,
        operation_type: Optional[str] = None,
        run_id: Optional[str] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        workspace_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """Initializes, persists, and journals a new durable source control operation."""
        clean_repo = (repository_id or "").strip()
        effective_kind = operation_type or (kind.value if hasattr(kind, "value") else str(kind)) if kind else "push_branch"
        effective_provider = (provider.value if hasattr(provider, "value") else str(provider)) if provider else "github"
        effective_target = (target_branch or "").strip()

        operation_id = f"sc_op_{secrets.token_hex(16)}"
        now = datetime.utcnow()

        initial_event = OperationEvent(
            event_id=f"evt_{secrets.token_hex(12)}",
            operation_id=operation_id,
            from_state=None,
            to_state=OperationState.CREATED,
            message=f"Operation '{operation_id}' initialized for kind '{effective_kind}'.",
            timestamp=now,
            metadata=dict(metadata) if metadata else {},
        )

        operation = OperationRecord(
            operation_id=operation_id,
            repository_id=clean_repo,
            operation_type=effective_kind,
            provider=effective_provider,
            target_branch=effective_target,
            workspace_id=workspace_id,
            run_id=run_id,
            source_branch=source_branch,
            current_state=OperationState.CREATED,
            status=OperationState.CREATED,
            events=(initial_event,),
            created_at=now,
            updated_at=now,
            metadata=dict(metadata) if metadata else {},
        )

        with self._lock:
            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(operation)
            else:
                self.journal.save_operation(operation)

            if hasattr(self.journal, "append_event"):
                await self.journal.append_event(operation_id, initial_event)
            else:
                self.journal.record_event(initial_event)

        return operation

    async def get_operation(self, operation_id: str) -> Optional[OperationRecord]:
        """Retrieves an operation record by ID, returning None if not found."""
        clean_id = (operation_id or "").strip()
        if not clean_id:
            return None

        if hasattr(self.journal, "get_record"):
            return await self.journal.get_record(clean_id)
        return self.journal.get_operation(clean_id)

    async def transition_state(
        self,
        operation_id: str,
        target_state: OperationState | str,
        message: str = "",
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        error_details: Optional[Mapping[str, Any]] = None,
        metadata_patch: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """Atomically transitions an operation to a valid target state and appends journal event."""
        with self._lock:
            op = await self.get_operation(operation_id)
            if op is None:
                raise ValidationError(f"Source control operation '{operation_id}' does not exist.")

            effective_target = (
                target_state
                if isinstance(target_state, OperationState)
                else OperationState(str(target_state))
            )

            # Idempotent re-transition returns active record unmodified
            if op.current_state == effective_target:
                return op

            if op.is_terminal:
                raise ConflictError(
                    f"Cannot transition terminal operation '{op.operation_id}' (state: {op.current_state.value})."
                )

            if not op.can_transition_to(effective_target):
                raise ConflictError(
                    f"Illegal state transition from '{op.current_state.value}' to '{effective_target.value}' "
                    f"for operation '{op.operation_id}'."
                )

            now = datetime.utcnow()
            completed_at = now if effective_target in {
                OperationState.COMPLETED,
                OperationState.CANCELLED,
                OperationState.FAILED_FINAL,
            } else op.completed_at

            merged_meta = dict(op.metadata)
            if metadata:
                merged_meta.update(metadata)
            if metadata_patch:
                merged_meta.update(metadata_patch)

            merged_errors = dict(error_details) if error_details else op.error_details

            transition_event = OperationEvent(
                event_id=f"evt_{secrets.token_hex(12)}",
                operation_id=operation_id,
                from_state=op.current_state,
                to_state=effective_target,
                message=message or f"Transitioned to {effective_target.value}.",
                timestamp=now,
                metadata=dict(metadata) if metadata else (dict(metadata_patch) if metadata_patch else {}),
            )

            updated_events = op.events + (transition_event,)

            updated_op = dataclasses.replace(
                op,
                current_state=effective_target,
                status=effective_target,
                events=updated_events,
                updated_at=now,
                completed_at=completed_at,
                metadata=merged_meta,
                error_details=merged_errors,
            )

            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(updated_op)
            else:
                self.journal.save_operation(updated_op)

            if hasattr(self.journal, "append_event"):
                await self.journal.append_event(operation_id, transition_event)
            else:
                self.journal.record_event(transition_event)

            return updated_op

    # Canonical alias ensuring backwards-compatible imports across subsystem packages
    transition_status = transition_state

    async def list_operations(
        self,
        repository_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> Sequence[OperationRecord]:
        """Lists operations matching filter criteria."""
        return self.journal.list_operations(
            repository_id=repository_id,
            run_id=run_id,
            status=status,
            limit=limit,
        )

    async def attach_workspace(
        self,
        operation_id: str,
        workspace_id: str,
        source_branch: str,
        base_sha: str,
    ) -> OperationRecord:
        """Attaches an isolated Git workspace reference and transitions to WORKSPACE_READY."""
        with self._lock:
            op = await self.get_operation(operation_id)
            if op is None:
                raise ValidationError(f"Operation '{operation_id}' not found.")

            updated_op = dataclasses.replace(
                op,
                workspace_id=workspace_id,
                source_branch=source_branch,
                base_sha=base_sha,
                updated_at=datetime.utcnow(),
            )
            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(updated_op)
            else:
                self.journal.save_operation(updated_op)

            return await self.transition_state(
                operation_id=operation_id,
                target_state=OperationState.WORKSPACE_READY,
                message=f"Workspace '{workspace_id}' bound on branch '{source_branch}' (base SHA: {base_sha[:8]}).",
            )

    async def attach_diff(
        self,
        operation_id: str,
        head_sha: str,
        diff_sha256: str,
        total_files: int,
        diff_bytes: int,
    ) -> OperationRecord:
        """Attaches head SHA and verified diff fingerprint after policy limit validation."""
        op = await self.get_operation(operation_id)
        if op is None:
            raise ValidationError(f"Operation '{operation_id}' not found.")

        self.policy_engine.validate_diff_limits(
            file_count=total_files,
            diff_bytes=diff_bytes,
            repository_id=op.repository_id,
        )

        with self._lock:
            updated_op = dataclasses.replace(
                op,
                head_sha=head_sha,
                diff_sha256=diff_sha256,
                updated_at=datetime.utcnow(),
            )
            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(updated_op)
            else:
                self.journal.save_operation(updated_op)

            return await self.transition_state(
                operation_id=operation_id,
                target_state=OperationState.REVIEW_REQUIRED,
                message=f"Diff generated (files: {total_files}, fingerprint: {diff_sha256[:12]}).",
            )

    async def propose_approval(
        self,
        operation_id: str,
        *,
        commit_message: str = "",
        change_request_title: str = "",
        change_request_body: str = "",
        ttl_minutes: Optional[int] = None,
    ) -> ApprovalTicket:
        """Creates a cryptographically bound pending approval ticket for the operation."""
        op = await self.get_operation(operation_id)
        if op is None:
            raise ValidationError(f"Operation '{operation_id}' not found.")

        if not op.base_sha or not op.diff_sha256 or not op.target_branch:
            raise ValidationError(
                f"Operation '{operation_id}' is not in an inspectable state (missing base_sha, diff_sha256, or target_branch)."
            )

        ticket = self.approval_manager.create_ticket(
            operation_id=op.operation_id,
            repository_id=op.repository_id,
            operation=op.operation_type,
            base_sha=op.base_sha,
            diff_sha256=op.diff_sha256,
            target_branch=op.target_branch,
            source_branch=op.source_branch or "",
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
            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(updated_op)
            else:
                self.journal.save_operation(updated_op)

        return ticket

    async def approve_operation(
        self,
        operation_id: str,
        approval_id: str,
        approved_by: str = "user",
    ) -> OperationRecord:
        """Marks approval ticket as approved and advances operation state machine to APPROVED."""
        self.approval_manager.approve_ticket(approval_id, user_id=approved_by)
        return await self.transition_state(
            operation_id=operation_id,
            target_state=OperationState.APPROVED,
            message=f"Approval ticket '{approval_id}' approved by '{approved_by}'.",
        )

    async def reject_operation(
        self,
        operation_id: str,
        approval_id: str,
        reason: Optional[str] = None,
    ) -> OperationRecord:
        """Rejects approval ticket and transitions operation to CANCELLED."""
        self.approval_manager.reject_ticket(approval_id, user_id="user", reason=reason)
        return await self.transition_state(
            operation_id=operation_id,
            target_state=OperationState.CANCELLED,
            message=f"Operation rejected by user. Reason: {reason or 'No reason provided'}.",
        )

    async def assert_ready_to_push_and_consume(
        self,
        operation_id: str,
        approval_id: str,
        current_base_sha: str,
        current_diff_sha256: str,
    ) -> ApprovalTicket:
        """Validates that operation is approved, verifies live cryptographic hashes, and consumes ticket."""
        op = await self.get_operation(operation_id)
        if op is None:
            raise ValidationError(f"Operation '{operation_id}' not found.")

        if op.current_state != OperationState.APPROVED:
            raise ApprovalRequiredError(
                f"Operation '{operation_id}' is not in APPROVED state (current: {op.current_state.value}).",
                operation_id=operation_id,
            )

        consumed_ticket = self.approval_manager.consume_approval(approval_id)

        if consumed_ticket.diff_sha256 != current_diff_sha256:
            raise ApprovalInvalidError(
                "Local diff content was modified after user approval was granted. A fresh review is mandatory."
            )

        await self.transition_state(
            operation_id=operation_id,
            target_state=OperationState.PUSHING,
            message="Approval consumed successfully; remote push in progress.",
        )
        return consumed_ticket

    async def record_pushed(
        self,
        operation_id: str,
        remote_branch: str,
        head_commit_sha: str,
    ) -> OperationRecord:
        """Records successful remote push and advances operation to PUSHED."""
        with self._lock:
            op = await self.get_operation(operation_id)
            if op is None:
                raise ValidationError(f"Operation '{operation_id}' not found.")

            updated_op = dataclasses.replace(
                op,
                source_branch=remote_branch,
                head_sha=head_commit_sha,
                updated_at=datetime.utcnow(),
            )
            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(updated_op)
            else:
                self.journal.save_operation(updated_op)

            return await self.transition_state(
                operation_id=operation_id,
                target_state=OperationState.PUSHED,
                message=f"Branch '{remote_branch}' successfully pushed at commit {head_commit_sha[:8]}.",
            )

    async def record_change_request_created(
        self,
        operation_id: str,
        change_request_url: str,
        change_request_title: str,
    ) -> OperationRecord:
        """Records change request creation and advances operation to CHANGE_REQUEST_CREATED."""
        with self._lock:
            op = await self.get_operation(operation_id)
            if op is None:
                raise ValidationError(f"Operation '{operation_id}' not found.")

            updated_op = dataclasses.replace(
                op,
                change_request_url=change_request_url,
                change_request_title=change_request_title,
                updated_at=datetime.utcnow(),
            )
            if hasattr(self.journal, "save_record"):
                await self.journal.save_record(updated_op)
            else:
                self.journal.save_operation(updated_op)

            return await self.transition_state(
                operation_id=operation_id,
                target_state=OperationState.CHANGE_REQUEST_CREATED,
                message=f"Pull request created: {change_request_url}",
            )

    async def complete_operation(
        self, operation_id: str, summary_message: str = "Operation completed successfully."
    ) -> OperationRecord:
        """Marks the operation as COMPLETED."""
        return await self.transition_state(
            operation_id=operation_id,
            target_state=OperationState.COMPLETED,
            message=summary_message,
        )

    async def fail_operation(
        self,
        operation_id: str,
        error_message: str,
        *,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """Marks the operation as failed (retryable or final)."""
        target_state = OperationState.FAILED_RETRYABLE if retryable else OperationState.FAILED_FINAL
        err = {"message": error_message}
        if details:
            err.update(details)

        return await self.transition_state(
            operation_id=operation_id,
            target_state=target_state,
            message=f"Operation failed: {error_message}",
            error_details=err,
        )

    async def reconcile_operation(self, operation_id: str) -> RecoveryReport:
        """Triggers recovery reconciliation on an operation against remote reality."""
        op = await self.get_operation(operation_id)
        if op is None:
            raise ValidationError(f"Operation '{operation_id}' not found.")
        return await self.recovery_engine.reconcile_operation(op)


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


__all__ = [
    "OperationService",
    "get_default_operation_service",
    "set_default_operation_service",
    "reset_default_operation_service",
]
