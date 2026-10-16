# backend/integrations/source_control/operations/recovery.py
"""
Recovery service for the Source Control Integration subsystem.

The recovery service is responsible for bringing the operation
journal to a consistent state after an application restart. It
scans the journal for operations that were in flight when the
process stopped and decides what to do with each one, using the
following principles:

    - Operations in a terminal status are ignored.
    - Operations waiting for the user (`REVIEW_REQUIRED`, `BLOCKED`,
      `APPROVED`) are left as-is. The user resumes them.
    - Operations in `PUSHING` are reconciled with the remote when a
      reconciler is available. Without one, they are marked as
      `STALE_REVIEW_REQUIRED` so that the user re-verifies.
    - Operations in `EDITING`, `CHECKING`, `WORKSPACE_READY`, or
      `REPOSITORY_RESOLVED` are marked `FAILED_RETRYABLE` because the
      workspace may be inconsistent.
    - Operations in `CANCEL_REQUESTED` are completed to `CANCELLED`.

The recovery service does NOT:
    - Delete branches, workspaces, or remote refs.
    - Rewrite commits.
    - Approve operations on behalf of the user.
    - Push or fetch anything on its own.

The service can operate in `dry_run` mode, in which case it produces
a plan without applying it. This is used by the UI to show the user
what recovery would do.

Design rules:
    - The service MUST NOT raise for a single failed recovery; it
      records the failure in the report and continues.
    - Every recovery action MUST be idempotent: running recovery
      twice on the same journal produces the same result.
    - The service holds no secret-bearing state. All callbacks are
      provided by the application at startup.
    - The service is thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
)

from ..domain.enums import OperationStatus
from ..domain.models import OperationRecord, _utc_now
from ..errors import ValidationError
from .idempotency import (
    IdempotencyRegistry,
    generate_key_from_operation,
    get_default_idempotency_registry,
)
from .journal import (
    OperationJournal,
    get_default_operation_journal,
)
from .models import is_terminal_status
from .service import (
    OperationService,
    get_default_operation_service,
)

__all__ = [
    "OperationRecoveryService",
    "RecoveryAction",
    "RecoveryOutcome",
    "RecoveryReport",
    "PushReconciler",
    "RemoteShaReader",
    "get_default_recovery_service",
    "set_default_recovery_service",
    "reset_default_recovery_service",
    "MAX_OPERATIONS_TO_RECOVER",
]


logger = logging.getLogger(__name__)


#: Maximum number of operations processed in a single recovery pass.
#: Prevents unbounded work if the journal grew unexpectedly.
MAX_OPERATIONS_TO_RECOVER: int = 1_000


# ---------------------------------------------------------------------------
# Callback contracts
# ---------------------------------------------------------------------------


#: Reconcile a push that may or may not have succeeded.
#:
#: The callback receives the operation record. It is expected to
#: return a tuple `(accepted, remote_sha, reason)`:
#:
#:     - `accepted=True` means the push succeeded (or was already
#:       applied); the operation may proceed to `PUSHED`.
#:     - `accepted=False` means the push did not happen; the operation
#:       may be returned to `APPROVED`.
#:     - `remote_sha` is the current remote SHA, or `None` if the
#:       reconciliation could not determine it.
#:     - `reason` is an optional human-readable explanation.
PushReconciler = Callable[
    [OperationRecord],
    Awaitable["PushReconciliationResult"],
]


#: Read the current SHA of a remote branch.
RemoteShaReader = Callable[
    [OperationRecord],
    Awaitable[Optional[str]],
]


@dataclass(frozen=True)
class PushReconciliationResult:
    """
    Outcome of a push reconciliation attempt.

    Fields:
        accepted:    True when the push is known to have succeeded.
        remote_sha:  The current remote SHA, or `None` when unknown.
        reason:      Optional human-readable explanation.
    """

    accepted: bool
    remote_sha: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Enums and value objects
# ---------------------------------------------------------------------------


class RecoveryOutcome(str):
    """
    Result of recovering a single operation.

    Defined as a `str` subclass so that it serializes transparently
    and compares to literal strings without importing this module.
    """

    #: The operation was reconciled with the remote and advanced.
    RECONCILED = "reconciled"
    #: The operation was marked `FAILED_RETRYABLE` for the user.
    REQUEUED = "requeued"
    #: The operation was marked failed permanently.
    MARKED_FAILED = "marked_failed"
    #: The operation was left as-is; the user will resume it.
    LEFT_AS_IS = "left_as_is"
    #: The operation was skipped because it is terminal or invalid.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RecoveryAction:
    """
    A single recovery action applied (or planned) for an operation.

    Fields:
        operation_id: The affected operation.
        outcome:      The classification of the recovery.
        from_status:  The status observed at the start of recovery.
        to_status:    The resulting status, when the operation
                      transitioned.
        reason:       Human-readable explanation.
        metadata:     Extension bag (no secrets).
        error:        Error message when the action failed.
    """

    operation_id: str
    outcome: str
    from_status: OperationStatus
    to_status: Optional[OperationStatus] = None
    reason: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the report and for logs."""
        return {
            "operation_id": self.operation_id,
            "outcome": self.outcome,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value if self.to_status else None,
            "reason": self.reason,
            "metadata": dict(self.metadata),
            "error": self.error,
        }


@dataclass(frozen=True)
class RecoveryReport:
    """
    Summary of a recovery pass.

    Fields:
        started_at:     When the pass began.
        completed_at:   When the pass finished.
        duration_ms:    Total duration in milliseconds.
        dry_run:        True when no actions were applied.
        actions:        The actions taken or planned.
        total_scanned:  How many records were inspected.
        errors:         Errors encountered during recovery.
    """

    started_at: Any
    completed_at: Any
    duration_ms: int
    dry_run: bool
    actions: Sequence[RecoveryAction] = field(default_factory=tuple)
    total_scanned: int = 0
    errors: Sequence[str] = field(default_factory=tuple)

    def count(self, outcome: str) -> int:
        """Return the number of actions with the given outcome."""
        return sum(1 for action in self.actions if action.outcome == outcome)

    @property
    def total_recovered(self) -> int:
        """Number of operations that were actively recovered."""
        return sum(
            1
            for action in self.actions
            if action.outcome
            in (
                RecoveryOutcome.RECONCILED,
                RecoveryOutcome.REQUEUED,
                RecoveryOutcome.MARKED_FAILED,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the API and for startup logs."""
        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
            "total_scanned": self.total_scanned,
            "total_recovered": self.total_recovered,
            "counts": {
                "reconciled": self.count(RecoveryOutcome.RECONCILED),
                "requeued": self.count(RecoveryOutcome.REQUEUED),
                "marked_failed": self.count(RecoveryOutcome.MARKED_FAILED),
                "left_as_is": self.count(RecoveryOutcome.LEFT_AS_IS),
                "skipped": self.count(RecoveryOutcome.SKIPPED),
            },
            "actions": [a.to_dict() for a in self.actions],
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Recovery service
# ---------------------------------------------------------------------------


class OperationRecoveryService:
    """
    Bring the operation journal to a consistent state after restart.

    The service is stateless with respect to operations; it reads the
    journal, decides what to do, and delegates every transition to
    the `OperationService`. All remote interaction is delegated to
    callbacks that the application registers at startup.
    """

    def __init__(
        self,
        *,
        service: Optional[OperationService] = None,
        journal: Optional[OperationJournal] = None,
        idempotency: Optional[IdempotencyRegistry] = None,
    ) -> None:
        self._service = service or get_default_operation_service()
        self._journal = journal or get_default_operation_journal()
        self._idempotency = idempotency or get_default_idempotency_registry()

        self._push_reconciler: Optional[PushReconciler] = None
        self._remote_sha_reader: Optional[RemoteShaReader] = None
        self._callbacks_lock = threading.RLock()

    # --- Callback registration -----------------------------------------

    def set_push_reconciler(
        self, reconciler: Optional[PushReconciler]
    ) -> None:
        """
        Register the callback used to reconcile in-flight pushes.

        When not registered, `PUSHING` operations are marked
        `STALE_REVIEW_REQUIRED` so that the user re-verifies.
        """
        if reconciler is not None and not callable(reconciler):
            raise ValidationError("reconciler must be callable or None.")
        with self._callbacks_lock:
            self._push_reconciler = reconciler

    def set_remote_sha_reader(
        self, reader: Optional[RemoteShaReader]
    ) -> None:
        """
        Register the callback used to read a remote SHA.

        When not registered, stale-review detection is skipped.
        """
        if reader is not None and not callable(reader):
            raise ValidationError("reader must be callable or None.")
        with self._callbacks_lock:
            self._remote_sha_reader = reader

    # --- Public API -----------------------------------------------------

    async def recover_all(
        self,
        *,
        dry_run: bool = False,
        max_operations: int = MAX_OPERATIONS_TO_RECOVER,
    ) -> RecoveryReport:
        """
        Recover every non-terminal operation.

        Args:
            dry_run: When True, produces a plan without applying it.
            max_operations: Cap on the number of records scanned.

        Returns:
            A `RecoveryReport` describing the outcome.
        """
        started_at = _utc_now()
        started_monotonic = time.monotonic()

        # Prune expired idempotency entries before scanning. This is
        # safe even in dry-run mode: pruning does not affect the
        # journal, and it keeps the registry bounded.
        if not dry_run:
            try:
                self._idempotency.prune_expired()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Idempotency pruning failed: %s",
                    type(exc).__name__,
                )

        try:
            records = await self._journal.list_records(limit=max_operations)
        except Exception as exc:  # noqa: BLE001
            completed_at = _utc_now()
            return RecoveryReport(
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=int(
                    (time.monotonic() - started_monotonic) * 1000
                ),
                dry_run=dry_run,
                actions=(),
                total_scanned=0,
                errors=[f"Failed to list records: {type(exc).__name__}"],
            )

        actions: List[RecoveryAction] = []
        errors: List[str] = []

        for record in records:
            try:
                action = await self._recover_record(
                    record=record, dry_run=dry_run
                )
                if action is not None:
                    actions.append(action)
            except Exception as exc:  # noqa: BLE001
                # One failure MUST NOT stop the whole pass.
                error_message = (
                    f"Operation '{record.operation_id}' recovery failed: "
                    f"{type(exc).__name__}"
                )
                logger.warning(error_message)
                errors.append(error_message)
                actions.append(
                    RecoveryAction(
                        operation_id=record.operation_id,
                        outcome=RecoveryOutcome.SKIPPED,
                        from_status=record.status,
                        reason="Recovery raised an exception.",
                        error=str(exc),
                    )
                )

        completed_at = _utc_now()
        return RecoveryReport(
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
            dry_run=dry_run,
            actions=tuple(actions),
            total_scanned=len(records),
            errors=tuple(errors),
        )

    async def recover_operation(
        self,
        operation_id: str,
        *,
        dry_run: bool = False,
    ) -> Optional[RecoveryAction]:
        """
        Recover a single operation by ID.

        Returns the `RecoveryAction` describing what was done, or
        `None` when the operation does not exist.
        """
        if not operation_id:
            raise ValidationError("operation_id must not be empty.")
        record = await self._journal.get_record(operation_id)
        if record is None:
            return None
        return await self._recover_record(record=record, dry_run=dry_run)

    # --- Internal: classification and dispatch -------------------------

    async def _recover_record(
        self,
        *,
        record: OperationRecord,
        dry_run: bool,
    ) -> Optional[RecoveryAction]:
        """
        Decide and apply the recovery for a single record.

        Returns the `RecoveryAction`, or `None` when the record was
        skipped without producing an action.
        """
        status = record.status

        if is_terminal_status(status):
            return None

        if status == OperationStatus.PUSHING:
            return await self._recover_pushing(record=record, dry_run=dry_run)

        if status == OperationStatus.CANCEL_REQUESTED:
            return await self._recover_cancel_requested(
                record=record, dry_run=dry_run
            )

        if status in (
            OperationStatus.CREATED,
            OperationStatus.REPOSITORY_RESOLVED,
            OperationStatus.WORKSPACE_READY,
            OperationStatus.EDITING,
            OperationStatus.CHECKING,
        ):
            return await self._recover_editing_state(
                record=record, dry_run=dry_run
            )

        if status == OperationStatus.REVIEW_REQUIRED:
            return await self._recover_review_required(
                record=record, dry_run=dry_run
            )

        if status == OperationStatus.STALE_REVIEW_REQUIRED:
            return self._left_as_is(
                record=record,
                reason="Operation is already awaiting a fresh review.",
            )

        if status == OperationStatus.APPROVED:
            return self._left_as_is(
                record=record,
                reason=(
                    "Operation is approved and ready to be resumed by "
                    "the user."
                ),
            )

        if status == OperationStatus.FAILED_RETRYABLE:
            return self._left_as_is(
                record=record,
                reason="Operation is retryable; the user may resume it.",
            )

        if status == OperationStatus.BLOCKED:
            return self._left_as_is(
                record=record,
                reason="Operation is blocked pending user action.",
            )

        # Should be unreachable, but we return a diagnostic action
        # rather than raising.
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.SKIPPED,
            from_status=status,
            reason=f"No recovery rule for status '{status.value}'.",
        )

    # --- Internal: per-state recovery ----------------------------------

    async def _recover_pushing(
        self,
        *,
        record: OperationRecord,
        dry_run: bool,
    ) -> RecoveryAction:
        """
        Reconcile a `PUSHING` operation.

        When a reconciler is registered, it is invoked. When it
        reports success, the operation is advanced to `PUSHED`. When
        it reports failure, the operation is returned to `APPROVED`
        so that the user may retry. When reconciliation is
        inconclusive, the operation is marked
        `STALE_REVIEW_REQUIRED`.
        """
        with self._callbacks_lock:
            reconciler = self._push_reconciler

        if reconciler is None:
            if dry_run:
                return RecoveryAction(
                    operation_id=record.operation_id,
                    outcome=RecoveryOutcome.LEFT_AS_IS,
                    from_status=record.status,
                    reason=(
                        "No push reconciler is registered; the operation "
                        "would be marked STALE_REVIEW_REQUIRED."
                    ),
                )
            return await self._mark_stale(
                record=record,
                reason=(
                    "Push status could not be verified after restart."
                ),
                metadata={"recovered_from": "pushing_no_reconciler"},
            )

        try:
            result = await reconciler(record)
        except Exception as exc:  # noqa: BLE001
            if dry_run:
                return RecoveryAction(
                    operation_id=record.operation_id,
                    outcome=RecoveryOutcome.LEFT_AS_IS,
                    from_status=record.status,
                    reason="Reconciler raised an exception.",
                    error=str(exc),
                )
            return await self._mark_stale(
                record=record,
                reason=(
                    "Push reconciliation failed; the user must "
                    "re-verify."
                ),
                metadata={"reconciler_error": type(exc).__name__},
            )

        if result is None:
            if dry_run:
                return RecoveryAction(
                    operation_id=record.operation_id,
                    outcome=RecoveryOutcome.LEFT_AS_IS,
                    from_status=record.status,
                    reason="Reconciler returned no result.",
                )
            return await self._mark_stale(
                record=record,
                reason="Push reconciliation returned no result.",
            )

        if result.accepted:
            if dry_run:
                return RecoveryAction(
                    operation_id=record.operation_id,
                    outcome=RecoveryOutcome.RECONCILED,
                    from_status=record.status,
                    to_status=OperationStatus.PUSHED,
                    reason="Reconciliation confirms the push succeeded.",
                    metadata={
                        "remote_sha": result.remote_sha,
                        "dry_run": True,
                    },
                )
            await self._service.transition(
                record.operation_id,
                OperationStatus.PUSHED,
                reason="Reconciled after restart.",
                metadata={
                    "remote_sha": result.remote_sha,
                    "reconciled": True,
                },
                actor="recovery",
            )
            return RecoveryAction(
                operation_id=record.operation_id,
                outcome=RecoveryOutcome.RECONCILED,
                from_status=record.status,
                to_status=OperationStatus.PUSHED,
                reason="Reconciliation confirms the push succeeded.",
                metadata={"remote_sha": result.remote_sha},
            )

        # Not accepted: the push did not happen. Return the operation
        # to APPROVED so that the user may retry.
        if dry_run:
            return RecoveryAction(
                operation_id=record.operation_id,
                outcome=RecoveryOutcome.REQUEUED,
                from_status=record.status,
                to_status=OperationStatus.APPROVED,
                reason="Reconciliation shows the push did not happen.",
                metadata={"remote_sha": result.remote_sha, "dry_run": True},
            )
        await self._service.transition(
            record.operation_id,
            OperationStatus.APPROVED,
            reason="Reconciled: push did not happen.",
            metadata={
                "remote_sha": result.remote_sha,
                "reconciled": True,
            },
            actor="recovery",
        )
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.REQUEUED,
            from_status=record.status,
            to_status=OperationStatus.APPROVED,
            reason="Reconciliation shows the push did not happen.",
            metadata={"remote_sha": result.remote_sha},
        )

    async def _recover_cancel_requested(
        self,
        *,
        record: OperationRecord,
        dry_run: bool,
    ) -> RecoveryAction:
        """
        Complete a `CANCEL_REQUESTED` operation to `CANCELLED`.
        """
        if dry_run:
            return RecoveryAction(
                operation_id=record.operation_id,
                outcome=RecoveryOutcome.REQUEUED,
                from_status=record.status,
                to_status=OperationStatus.CANCELLED,
                reason="Cancellation was requested before restart.",
                metadata={"dry_run": True},
            )
        await self._service.transition(
            record.operation_id,
            OperationStatus.CANCELLED,
            reason="Recovered: cancellation completed after restart.",
            actor="recovery",
        )
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.REQUEUED,
            from_status=record.status,
            to_status=OperationStatus.CANCELLED,
            reason="Cancellation was requested before restart.",
        )

    async def _recover_editing_state(
        self,
        *,
        record: OperationRecord,
        dry_run: bool,
    ) -> RecoveryAction:
        """
        Mark an in-progress editing operation as `FAILED_RETRYABLE`.

        The workspace may be inconsistent after a crash; the user is
        the best judge of whether to retry, discard, or start over.
        """
        if dry_run:
            return RecoveryAction(
                operation_id=record.operation_id,
                outcome=RecoveryOutcome.REQUEUED,
                from_status=record.status,
                to_status=OperationStatus.FAILED_RETRYABLE,
                reason=(
                    "Operation was interrupted before completion; would "
                    "be marked retryable."
                ),
                metadata={"dry_run": True},
            )
        await self._service.mark_failed(
            record.operation_id,
            error_code="RECOVERY_INTERRUPTED",
            error_message=(
                "Operation was interrupted by an application restart."
            ),
            retryable=True,
            actor="recovery",
        )
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.REQUEUED,
            from_status=record.status,
            to_status=OperationStatus.FAILED_RETRYABLE,
            reason=(
                "Operation was interrupted before completion; marked "
                "retryable."
            ),
        )

    async def _recover_review_required(
        self,
        *,
        record: OperationRecord,
        dry_run: bool,
    ) -> RecoveryAction:
        """
        Handle a `REVIEW_REQUIRED` operation.

        When a `remote_sha_reader` is registered and the remote base
        SHA has changed since the operation was prepared, the
        operation is marked `STALE_REVIEW_REQUIRED` so that the user
        reviews the updated diff. Otherwise the operation is left
        as-is; the user still needs to approve.
        """
        with self._callbacks_lock:
            reader = self._remote_sha_reader

        if reader is None or not record.base_sha:
            return self._left_as_is(
                record=record,
                reason="Operation awaits user approval.",
            )

        try:
            remote_sha = await reader(record)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Remote SHA reader failed for operation '%s': %s",
                record.operation_id,
                type(exc).__name__,
            )
            return self._left_as_is(
                record=record,
                reason="Could not verify remote state; leaving as-is.",
            )

        if remote_sha is None or remote_sha == record.base_sha:
            return self._left_as_is(
                record=record,
                reason="Operation awaits user approval.",
            )

        if dry_run:
            return RecoveryAction(
                operation_id=record.operation_id,
                outcome=RecoveryOutcome.REQUEUED,
                from_status=record.status,
                to_status=OperationStatus.STALE_REVIEW_REQUIRED,
                reason=(
                    "Remote base SHA changed while the operation was "
                    "waiting; would mark stale."
                ),
                metadata={"remote_sha": remote_sha, "dry_run": True},
            )

        await self._service.transition(
            record.operation_id,
            OperationStatus.STALE_REVIEW_REQUIRED,
            reason="Remote base changed while the operation was waiting.",
            metadata={
                "previous_base_sha": record.base_sha,
                "remote_sha": remote_sha,
            },
            actor="recovery",
        )
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.REQUEUED,
            from_status=record.status,
            to_status=OperationStatus.STALE_REVIEW_REQUIRED,
            reason="Remote base changed while the operation was waiting.",
            metadata={
                "previous_base_sha": record.base_sha,
                "remote_sha": remote_sha,
            },
        )

    # --- Internal: helpers ---------------------------------------------

    async def _mark_stale(
        self,
        *,
        record: OperationRecord,
        reason: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> RecoveryAction:
        """
        Transition the operation to `STALE_REVIEW_REQUIRED`.
        """
        await self._service.transition(
            record.operation_id,
            OperationStatus.STALE_REVIEW_REQUIRED,
            reason=reason,
            metadata=dict(metadata or {}),
            actor="recovery",
        )
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.LEFT_AS_IS,
            from_status=record.status,
            to_status=OperationStatus.STALE_REVIEW_REQUIRED,
            reason=reason,
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _left_as_is(
        *,
        record: OperationRecord,
        reason: str,
    ) -> RecoveryAction:
        """Build an action that leaves the operation unchanged."""
        return RecoveryAction(
            operation_id=record.operation_id,
            outcome=RecoveryOutcome.LEFT_AS_IS,
            from_status=record.status,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Default service singleton
# ---------------------------------------------------------------------------


_DEFAULT_SERVICE: Optional[OperationRecoveryService] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_recovery_service() -> OperationRecoveryService:
    """
    Return the process-wide default recovery service, creating it on
    first use.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = OperationRecoveryService()
        return _DEFAULT_SERVICE


def set_default_recovery_service(
    service: OperationRecoveryService,
) -> None:
    """
    Replace the process-wide default recovery service.

    Intended for application startup and for tests.
    """
    if not isinstance(service, OperationRecoveryService):
        raise TypeError(
            "set_default_recovery_service expects an "
            "OperationRecoveryService."
        )
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = service


def reset_default_recovery_service() -> OperationRecoveryService:
    """
    Replace the default service with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = OperationRecoveryService()
        return _DEFAULT_SERVICE
