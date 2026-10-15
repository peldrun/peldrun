from __future__ import annotations

import dataclasses
import secrets
import threading
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence

from ..domain.enums import ApprovalDecision, OperationKind, OperationStatus
from ..domain.models import RepositoryRef
from ..ports.provider import SourceControlProvider
from ..registry import ProviderRegistry, get_default_registry
from ..security.approval import ApprovalManager, get_default_approval_manager
from .journal import OperationJournal, get_default_operation_journal
from .models import OperationEvent, OperationRecord


class RecoveryActionKind(str, Enum):
    """Classification of the recovery action executed on an interrupted operation."""

    RECONCILED_PUSHED = "reconciled_pushed"
    RECONCILED_PR = "reconciled_pr"
    MARKED_STALE = "marked_stale"
    MARKED_RETRYABLE = "marked_retryable"
    CANCELLED = "cancelled"
    NO_ACTION_REQUIRED = "no_action_required"


@dataclasses.dataclass(frozen=True)
class RecoveryReport:
    """Summary record describing the outcome of a recovery reconciliation process."""

    operation_id: str
    original_status: OperationStatus
    final_status: OperationStatus
    action_taken: RecoveryActionKind
    reconciled_at: datetime = dataclasses.field(default_factory=datetime.utcnow)
    details: dict[str, str] = dataclasses.field(default_factory=dict)


class RecoveryEngine:
    """Thread-safe engine responsible for auditing and reconciling interrupted source control operations."""

    def __init__(
        self,
        journal: Optional[OperationJournal] = None,
        registry: Optional[ProviderRegistry] = None,
        approval_manager: Optional[ApprovalManager] = None,
    ) -> None:
        self._journal = journal
        self._registry = registry
        self._approval_manager = approval_manager
        self._lock = threading.Lock()

    @property
    def journal(self) -> OperationJournal:
        if self._journal is None:
            self._journal = get_default_operation_journal()
        return self._journal

    @property
    def registry(self) -> ProviderRegistry:
        if self._registry is None:
            self._registry = get_default_registry()
        return self._registry

    @property
    def approval_manager(self) -> ApprovalManager:
        if self._approval_manager is None:
            self._approval_manager = get_default_approval_manager()
        return self._approval_manager

    async def reconcile_operation(
        self,
        operation: OperationRecord,
        repo_ref: Optional[RepositoryRef] = None,
    ) -> RecoveryReport:
        """Reconciles an individual in-flight or ambiguous operation against remote reality."""
        with self._lock:
            # Skip operations that have already reached terminal state
            if operation.is_terminal:
                return RecoveryReport(
                    operation_id=operation.operation_id,
                    original_status=operation.status,
                    final_status=operation.status,
                    action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
                )

            # 1. Reconcile in-flight pushes
            if operation.status == OperationStatus.PUSHING:
                return await self._reconcile_pushing(operation, repo_ref)

            # 2. Reconcile in-flight change request creation
            if operation.status == OperationStatus.PUSHED:
                return await self._reconcile_change_request(operation, repo_ref)

            # 3. Check for expired approvals or remote base drift
            if operation.status in {OperationStatus.REVIEW_REQUIRED, OperationStatus.APPROVED}:
                return await self._reconcile_review_or_approved(operation, repo_ref)

            # 4. Check for interrupted background checks
            if operation.status == OperationStatus.CHECKING:
                return self._reconcile_checking(operation)

            # Default: no mutating reconciliation required
            return RecoveryReport(
                operation_id=operation.operation_id,
                original_status=operation.status,
                final_status=operation.status,
                action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
            )

    async def _reconcile_pushing(
        self,
        operation: OperationRecord,
        repo_ref: Optional[RepositoryRef],
    ) -> RecoveryReport:
        """Reconciles operations halted while pushing to the remote repository."""
        provider = self.registry.get_optional(operation.provider)
        if provider is None or not operation.source_branch or not operation.head_sha:
            # Cannot verify remotely without provider adapter; mark retryable
            return self._mark_failed_retryable(
                operation, "Interrupted during push; provider adapter or branch metadata unavailable."
            )

        resolved_repo = repo_ref
        if resolved_repo is None:
            try:
                resolved_repo = await provider.get_repository(operation.repository_id)
            except Exception as exc:
                return self._mark_failed_retryable(
                    operation, f"Failed to fetch remote repository for reconciliation: {exc}"
                )

        try:
            remote_branch = await provider.get_branch(resolved_repo, operation.source_branch)
            # Check if remote branch head matches the expected commit SHA
            if remote_branch.head_sha and remote_branch.head_sha == operation.head_sha:
                # The branch was pushed successfully before interruption
                updated = dataclasses.replace(
                    operation,
                    status=OperationStatus.PUSHED,
                    updated_at=datetime.utcnow(),
                )
                self.journal.save_operation(updated)
                self._record_event(
                    operation.operation_id,
                    from_status=operation.status,
                    to_status=OperationStatus.PUSHED,
                    message="Push reconciled: remote branch verified at expected head SHA.",
                )
                return RecoveryReport(
                    operation_id=operation.operation_id,
                    original_status=operation.status,
                    final_status=OperationStatus.PUSHED,
                    action_taken=RecoveryActionKind.RECONCILED_PUSHED,
                    details={"head_sha": operation.head_sha, "branch": operation.source_branch},
                )
        except Exception:
            # Branch does not exist on remote or query failed
            pass

        return self._mark_failed_retryable(
            operation, "Interrupted during push: remote branch not found at head SHA."
        )

    async def _reconcile_change_request(
        self,
        operation: OperationRecord,
        repo_ref: Optional[RepositoryRef],
    ) -> RecoveryReport:
        """Checks whether a change request was already created prior to crash."""
        if operation.change_request_url:
            return RecoveryReport(
                operation_id=operation.operation_id,
                original_status=operation.status,
                final_status=operation.status,
                action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
            )

        provider = self.registry.get_optional(operation.provider)
        if provider is None or not operation.source_branch or not operation.target_branch:
            return RecoveryReport(
                operation_id=operation.operation_id,
                original_status=operation.status,
                final_status=operation.status,
                action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
            )

        resolved_repo = repo_ref
        if resolved_repo is None:
            try:
                resolved_repo = await provider.get_repository(operation.repository_id)
            except Exception:
                return RecoveryReport(
                    operation_id=operation.operation_id,
                    original_status=operation.status,
                    final_status=operation.status,
                    action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
                )

        # Inspect if a change request already exists for this source and target branch
        try:
            # If the provider adapter supports listing/querying change requests
            if hasattr(provider, "list_change_requests"):
                prs = await provider.list_change_requests(
                    resolved_repo,
                    source_branch=operation.source_branch,
                    target_branch=operation.target_branch,
                )
                if prs:
                    matched_pr = prs[0]
                    updated = dataclasses.replace(
                        operation,
                        status=OperationStatus.CHANGE_REQUEST_CREATED,
                        change_request_url=matched_pr.web_url,
                        change_request_title=matched_pr.title,
                        updated_at=datetime.utcnow(),
                    )
                    self.journal.save_operation(updated)
                    self._record_event(
                        operation.operation_id,
                        from_status=operation.status,
                        to_status=OperationStatus.CHANGE_REQUEST_CREATED,
                        message=f"Change request reconciled: found existing PR at {matched_pr.web_url}.",
                    )
                    return RecoveryReport(
                        operation_id=operation.operation_id,
                        original_status=operation.status,
                        final_status=OperationStatus.CHANGE_REQUEST_CREATED,
                        action_taken=RecoveryActionKind.RECONCILED_PR,
                        details={"change_request_url": matched_pr.web_url},
                    )
        except Exception:
            pass

        return RecoveryReport(
            operation_id=operation.operation_id,
            original_status=operation.status,
            final_status=operation.status,
            action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
        )

    async def _reconcile_review_or_approved(
        self,
        operation: OperationRecord,
        repo_ref: Optional[RepositoryRef],
    ) -> RecoveryReport:
        """Verifies approval validity and checks for remote base branch drift."""
        is_stale = False
        stale_reason = ""

        # 1. Check approval ticket validity
        if operation.approval_id:
            ticket = self.approval_manager.get_ticket(operation.approval_id)
            if ticket is None or not ticket.is_valid:
                is_stale = True
                stale_reason = "Approval ticket expired or invalidated during downtime."

        # 2. Check remote base SHA drift if provider and repository are available
        if not is_stale and operation.target_branch and operation.base_sha:
            provider = self.registry.get_optional(operation.provider)
            if provider is not None:
                try:
                    resolved_repo = repo_ref or await provider.get_repository(operation.repository_id)
                    remote_target = await provider.get_branch(resolved_repo, operation.target_branch)
                    if remote_target.head_sha and remote_target.head_sha != operation.base_sha:
                        is_stale = True
                        stale_reason = (
                            f"Remote base branch '{operation.target_branch}' moved "
                            f"(was {operation.base_sha[:8]}, now {remote_target.head_sha[:8]})."
                        )
                except Exception:
                    pass

        if is_stale:
            # Transition operation to STALE_REVIEW_REQUIRED
            updated = dataclasses.replace(
                operation,
                status=OperationStatus.STALE_REVIEW_REQUIRED,
                updated_at=datetime.utcnow(),
            )
            self.journal.save_operation(updated)
            self._record_event(
                operation.operation_id,
                from_status=operation.status,
                to_status=OperationStatus.STALE_REVIEW_REQUIRED,
                message=f"Operation marked stale: {stale_reason}",
            )
            return RecoveryReport(
                operation_id=operation.operation_id,
                original_status=operation.status,
                final_status=OperationStatus.STALE_REVIEW_REQUIRED,
                action_taken=RecoveryActionKind.MARKED_STALE,
                details={"reason": stale_reason},
            )

        return RecoveryReport(
            operation_id=operation.operation_id,
            original_status=operation.status,
            final_status=operation.status,
            action_taken=RecoveryActionKind.NO_ACTION_REQUIRED,
        )

    def _reconcile_checking(self, operation: OperationRecord) -> RecoveryReport:
        """Resets operations that were interrupted mid-check back to workspace ready."""
        updated = dataclasses.replace(
            operation,
            status=OperationStatus.WORKSPACE_READY,
            updated_at=datetime.utcnow(),
        )
        self.journal.save_operation(updated)
        self._record_event(
            operation.operation_id,
            from_status=operation.status,
            to_status=OperationStatus.WORKSPACE_READY,
            message="Check execution interrupted; workspace reset to ready state.",
        )
        return RecoveryReport(
            operation_id=operation.operation_id,
            original_status=operation.status,
            final_status=OperationStatus.WORKSPACE_READY,
            action_taken=RecoveryActionKind.MARKED_RETRYABLE,
            details={"message": "Reset to WORKSPACE_READY"},
        )

    def _mark_failed_retryable(
        self, operation: OperationRecord, reason: str
    ) -> RecoveryReport:
        """Marks an operation as failed retryable with descriptive error logging."""
        updated = dataclasses.replace(
            operation,
            status=OperationStatus.FAILED_RETRYABLE,
            error_details={"recovery_reason": reason},
            updated_at=datetime.utcnow(),
        )
        self.journal.save_operation(updated)
        self._record_event(
            operation.operation_id,
            from_status=operation.status,
            to_status=OperationStatus.FAILED_RETRYABLE,
            message=f"Recovery failed retryable: {reason}",
        )
        return RecoveryReport(
            operation_id=operation.operation_id,
            original_status=operation.status,
            final_status=OperationStatus.FAILED_RETRYABLE,
            action_taken=RecoveryActionKind.MARKED_RETRYABLE,
            details={"reason": reason},
        )

    def _record_event(
        self,
        operation_id: str,
        from_status: Optional[OperationStatus],
        to_status: OperationStatus,
        message: str,
    ) -> None:
        event = OperationEvent(
            event_id=f"evt_{secrets.token_hex(12)}",
            operation_id=operation_id,
            from_status=from_status,
            to_status=to_status,
            message=message,
            timestamp=datetime.utcnow(),
        )
        self.journal.record_event(event)

    async def scan_and_reconcile_all(
        self, repository_id: Optional[str] = None
    ) -> Sequence[RecoveryReport]:
        """Scans the journal for non-terminal operations and reconciles each."""
        active_statuses = [
            OperationStatus.PUSHING,
            OperationStatus.PUSHED,
            OperationStatus.CHECKING,
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.APPROVED,
        ]

        reports: list[RecoveryReport] = []
        for status in active_statuses:
            operations = self.journal.list_operations(repository_id=repository_id, status=status)
            for op in operations:
                report = await self.reconcile_operation(op)
                reports.append(report)

        return tuple(reports)


# ============================================================
# Default RecoveryEngine Singleton Accessors
# ============================================================

_DEFAULT_RECOVERY_ENGINE: Optional[RecoveryEngine] = None
_RECOVERY_LOCK = threading.Lock()


def get_default_recovery_engine() -> RecoveryEngine:
    """Returns the process-wide default RecoveryEngine singleton."""
    global _DEFAULT_RECOVERY_ENGINE
    if _DEFAULT_RECOVERY_ENGINE is None:
        with _RECOVERY_LOCK:
            if _DEFAULT_RECOVERY_ENGINE is None:
                _DEFAULT_RECOVERY_ENGINE = RecoveryEngine()
    return _DEFAULT_RECOVERY_ENGINE


def set_default_recovery_engine(engine: RecoveryEngine) -> None:
    """Explicitly injects a RecoveryEngine instance (used during testing)."""
    global _DEFAULT_RECOVERY_ENGINE
    with _RECOVERY_LOCK:
        _DEFAULT_RECOVERY_ENGINE = engine


def reset_default_recovery_engine() -> None:
    """Resets the process-wide default RecoveryEngine (used during testing)."""
    global _DEFAULT_RECOVERY_ENGINE
    with _RECOVERY_LOCK:
        _DEFAULT_RECOVERY_ENGINE = None
