# backend/integrations/source_control/operations/service.py
"""
Operation service for the Source Control Integration subsystem.

The operation service is the ONLY component permitted to mutate the
state of an operation. It enforces the state machine defined in
`operations.models`, persists every change through the operation
journal, and records every transition in the audit log.

The service is deliberately policy-agnostic: it does NOT decide
whether a transition should be allowed by policy. Policy decisions
are made by `security.policy_engine` before the service is invoked.
The service only enforces the structural rules of the state machine.

Design rules:
    - Every state transition MUST go through `transition`. Direct
      mutation of an `OperationRecord` is not possible because the
      model is frozen; the service is the only component that creates
      new instances with an updated status.
    - Every transition writes exactly one `OperationEvent`. Event
      sequence numbers are monotonically increasing per operation and
      cached in memory for performance.
    - Terminal statuses cannot be left. Attempting to transition away
      from a terminal status raises `ConflictError`.
    - The service is thread-safe. Sequence caching and per-operation
      locks are used to prevent races on the same operation.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..domain.enums import (
    OperationEventType,
    OperationStatus,
    ProviderId,
)
from ..domain.models import (
    ChangeRequestRef,
    OperationEvent,
    OperationRecord,
    _utc_now,
)
from ..errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from ..security.approval import (
    ApprovalGate,
    get_default_approval_gate,
)
from ..security.audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)
from ..security.policy_engine import (
    PolicyEngine,
    get_default_policy_engine,
)
from .journal import (
    MAX_RECORDS_IN_MEMORY,
    OperationJournal,
    get_default_operation_journal,
)
from .models import (
    OperationTransition,
    is_terminal_status,
    is_valid_transition,
)

__all__ = [
    "OperationService",
    "get_default_operation_service",
    "set_default_operation_service",
    "reset_default_operation_service",
    "generate_operation_id",
]


logger = logging.getLogger(__name__)


#: Prefix used for every generated operation identifier.
_OPERATION_ID_PREFIX = "op_"


def generate_operation_id() -> str:
    """
    Return a fresh, opaque operation identifier.

    The identifier is URL-safe and filesystem-safe. It begins with
    `op_` so that journal files are easy to identify.
    """
    return f"{_OPERATION_ID_PREFIX}{secrets.token_hex(12)}"


def _validate_operation_id(operation_id: str) -> str:
    """Return a validated operation ID or raise `ValidationError`."""
    if not operation_id or not isinstance(operation_id, str):
        raise ValidationError("operation_id must be a non-empty string.")
    trimmed = operation_id.strip()
    if not trimmed:
        raise ValidationError("operation_id must not be blank.")
    return trimmed


# ---------------------------------------------------------------------------
# Operation service
# ---------------------------------------------------------------------------


class OperationService:
    """
    Central state machine for source-control operations.

    The service owns the lifecycle of every operation. It writes to
    the journal on every mutation and emits an audit event on every
    transition. It does NOT own policy, approval, or credential
    decisions; those are handled by the corresponding services.
    """

    def __init__(
        self,
        *,
        journal: Optional[OperationJournal] = None,
        policy_engine: Optional[PolicyEngine] = None,
        approval_gate: Optional[ApprovalGate] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self._journal = (
            journal
            if journal is not None
            else get_default_operation_journal()
        )
        self._policy = (
            policy_engine
            if policy_engine is not None
            else get_default_policy_engine()
        )
        self._approvals = (
            approval_gate
            if approval_gate is not None
            else get_default_approval_gate()
        )
        self._audit = (
            audit_logger
            if audit_logger is not None
            else get_default_audit_logger()
        )

        # Cache of the last used event sequence per operation.
        # Populated lazily; invalidated on service restart.
        self._event_sequences: Dict[str, int] = {}

        # Per-operation async locks used to serialize state
        # mutations (transition, field update, delete) across
        # coroutines.
        #
        # Bug 9 extension (lost update on concurrent transitions):
        #     The previous implementation used `threading.RLock`
        #     here. `RLock` is re-entrant within the same OS
        #     thread, and asyncio runs every coroutine in the same
        #     thread by default. A coroutine that awaited inside
        #     the critical section (which both
        #     `_transition_internal` and `_update_record` do) did
        #     NOT exclude other coroutines: they re-entered the
        #     lock as soon as the event loop yielded to them. Two
        #     concurrent transitions on the same operation could
        #     therefore read the same starting record, both
        #     compute a new state, and the second `save_record`
        #     would silently overwrite the first — a classic lost
        #     update. The recorded `from_status` in the
        #     transition event could also be stale.
        #
        #     `asyncio.Lock` is held across `await` points, which
        #     is the correct semantics for coroutine-level mutual
        #     exclusion.
        self._operation_locks: Dict[str, asyncio.Lock] = {}

        # Per-operation async locks used to serialize event
        # emission (sequence allocation + journal append).
        #
        # Bug 9 (duplicate event sequences):
        #     The previous implementation allocated a sequence
        #     number and appended the event as two independent
        #     steps. Under concurrency, two coroutines could
        #     interleave between the read of `_event_sequences`
        #     and the write-back, producing the same sequence
        #     number for two different events. They could also
        #     interleave between the allocation and the append,
        #     so an event with a HIGHER sequence number could be
        #     written to the journal BEFORE an event with a
        #     LOWER sequence number. Both symptoms corrupt the
        #     audit trail and the state-machine history.
        #
        # `asyncio.Lock` properly serializes coroutines: the lock
        # is held across `await` points, so only one coroutine
        # per operation can be inside the emission critical
        # section at a time. This guarantees that:
        #
        #   1. Sequence numbers are unique per operation.
        #   2. Events are appended to the journal in sequence
        #      order (no reordering under concurrency).
        self._event_locks: Dict[str, asyncio.Lock] = {}

        self._locks_lock = threading.RLock()

    # --- Accessors ------------------------------------------------------

    @property
    def journal(self) -> OperationJournal:
        """Return the underlying journal."""
        return self._journal

    @property
    def policy_engine(self) -> PolicyEngine:
        """Return the policy engine used by the service."""
        return self._policy

    @property
    def approval_gate(self) -> ApprovalGate:
        """Return the approval gate used by the service."""
        return self._approvals

    # --- Lifecycle: create ---------------------------------------------

    async def create_operation(
        self,
        *,
        repository_id: str,
        kind: str,
        run_id: str = "",
        provider: Optional[ProviderId] = None,
        base_sha: str = "",
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        workspace_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """
        Create a new operation in the `CREATED` status.

        All parameters are keyword-only. This signature is stable
        across future revisions; new fields are added with defaults.

        Args:
            repository_id: Stable repository identifier.
            kind: Operation kind (e.g. `"push_branch"`).
            run_id: Optional agent run identifier.
            provider: Optional provider identifier. Defaults to
                GitHub, matching the first-release scope.
            base_sha: Optional base commit SHA.
            source_branch: Optional source branch name.
            target_branch: Optional target branch name.
            workspace_id: Optional workspace identifier.
            metadata: Optional extension bag. MUST NOT contain
                secrets.

        Returns:
            The newly created `OperationRecord`.

        Raises:
            ValidationError: If `repository_id` or `kind` is empty.
        """
        if not repository_id or not isinstance(repository_id, str):
            raise ValidationError(
                "repository_id must be a non-empty string."
            )
        if not kind or not isinstance(kind, str):
            raise ValidationError("kind must be a non-empty string.")

        resolved_provider = provider or ProviderId.GITHUB
        now = _utc_now()

        record = OperationRecord(
            operation_id=generate_operation_id(),
            run_id=run_id or "",
            provider=resolved_provider,
            repository_id=repository_id.strip(),
            kind=kind.strip(),
            status=OperationStatus.CREATED,
            base_sha=base_sha or "",
            source_branch=source_branch,
            target_branch=target_branch,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

        await self._journal.save_record(record)

        # Seed the sequence cache for this operation.
        with self._locks_lock:
            self._event_sequences[record.operation_id] = 0

        await self._emit_event(
            operation_id=record.operation_id,
            event_type=OperationEventType.OPERATION_CREATED,
            payload={
                "kind": record.kind,
                "repository_id": record.repository_id,
                "provider": record.provider.value,
            },
        )

        self._audit.record(
            AuditEventType.OPERATION_CREATE,
            severity=AuditSeverity.INFO,
            action="create_operation",
            message=f"Operation '{record.kind}' created.",
            payload={
                "operation_id": record.operation_id,
                "kind": record.kind,
                "repository_id": record.repository_id,
                "provider": record.provider.value,
            },
            operation_id=record.operation_id,
            repository_id=record.repository_id,
            provider=record.provider.value,
            actor="agent",
        )

        return record

    # --- Lifecycle: read -----------------------------------------------

    async def get_operation(
        self, operation_id: str
    ) -> OperationRecord:
        """
        Return the operation record.

        Raises:
            ValidationError: If `operation_id` is malformed.
            NotFoundError: If the operation does not exist.
        """
        key = _validate_operation_id(operation_id)
        record = await self._journal.get_record(key)
        if record is None:
            raise NotFoundError(
                f"Operation '{key}' was not found.",
                details={"operation_id": key},
            )
        return record

    async def try_get_operation(
        self, operation_id: str
    ) -> Optional[OperationRecord]:
        """
        Return the operation record, or `None` when absent.

        Never raises for a missing operation. Raises `ValidationError`
        only for malformed IDs.
        """
        try:
            key = _validate_operation_id(operation_id)
        except ValidationError:
            return None
        return await self._journal.get_record(key)

    async def list_operations(
        self, *, limit: int = 100
    ) -> Sequence[OperationRecord]:
        """
        Return up to `limit` operations, newest first.
        """
        if limit <= 0:
            return ()
        if limit > MAX_RECORDS_IN_MEMORY:
            limit = MAX_RECORDS_IN_MEMORY
        return await self._journal.list_records(limit=limit)

    async def list_events(
        self, operation_id: str
    ) -> Sequence[OperationEvent]:
        """
        Return every event for the operation, oldest first.
        """
        key = _validate_operation_id(operation_id)
        return await self._journal.list_events(key)

    async def delete_operation(self, operation_id: str) -> bool:
        """
        Remove an operation and its events from the journal.

        Returns True if the operation existed. This is a destructive
        operation; the caller is responsible for ensuring the user
        has approved it.

        Side effect:
            Also evicts the per-operation locks from the in-memory
            caches. This prevents unbounded growth of `_event_locks`
            and `_operation_locks` when many operations are created
            and deleted over a long-running process lifetime.
        """
        key = _validate_operation_id(operation_id)
        # Acquire the per-operation async lock so that the delete
        # cannot interleave with an in-flight transition or field
        # update. Without this, a transition that had already
        # read the record before the delete could re-create it on
        # `save_record`, leaving the journal with a record the
        # caller believes was deleted.
        async with self._operation_lock(key):
            existed = await self._journal.delete_record(key)
        # No `await` between the lock release above and the
        # synchronous cache eviction below, so any coroutine that
        # was waiting on the OLD lock acquires it right after we
        # release, calls `get_record`, and sees `None` (the
        # record is gone) — the correct behavior for a deleted
        # operation. A coroutine that calls `_operation_lock`
        # AFTER the pop creates a fresh lock; both it and the
        # OLD-lock holder fail at `get_record` because the record
        # is gone, so no lost update can occur even though the
        # two coroutines hold distinct lock objects.
        with self._locks_lock:
            self._event_sequences.pop(key, None)
            self._operation_locks.pop(key, None)
            self._event_locks.pop(key, None)
        return existed

    # --- Transition validation -----------------------------------------

    async def can_transition_to(
        self, operation_id: str, to_status: OperationStatus
    ) -> bool:
        """
        Return True if the operation may transition to `to_status`.

        Performs no side effects: no journal write, no event, no
        audit entry. Use `transition` to actually perform the change.
        """
        if not isinstance(to_status, OperationStatus):
            return False
        record = await self.try_get_operation(operation_id)
        if record is None:
            return False
        return is_valid_transition(record.status, to_status)

    # --- Transition -----------------------------------------------------

    async def transition(
        self,
        operation_id: str,
        to_status: OperationStatus,
        *,
        reason: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        actor: str = "system",
    ) -> OperationRecord:
        """
        Move an operation to `to_status`.

        The following invariants are enforced:

            - The transition MUST be valid per `ALLOWED_TRANSITIONS`.
            - A terminal operation MUST NOT transition further.
            - The record is written to the journal before the event,
              so the state is authoritative even if the process
              crashes between the two writes.
            - A `STATE_TRANSITION` event is appended for every
              successful transition.
            - The transition is recorded in the audit log.

        Args:
            operation_id: Operation identifier.
            to_status: Destination status.
            reason: Optional human-readable explanation.
            metadata: Optional payload for the event.
            actor: `"user"`, `"agent"`, or `"system"`.

        Returns:
            The updated `OperationRecord`.

        Raises:
            ValidationError: If inputs are malformed.
            NotFoundError: If the operation does not exist.
            ConflictError: If the transition is invalid or the
                operation is already terminal.
        """
        return await self._transition_internal(
            operation_id,
            to_status,
            reason=reason,
            metadata=metadata,
            actor=actor,
        )

    async def _transition_internal(
        self,
        operation_id: str,
        to_status: OperationStatus,
        *,
        reason: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        actor: str = "system",
        extra_fields: Optional[Mapping[str, Any]] = None,
    ) -> OperationRecord:
        """
        Real implementation of `transition`, with support for extra
        record fields that must be applied atomically with the status
        change.

        The public `transition` method delegates here without extra
        fields. `mark_failed` also delegates here, passing
        `error_code`/`error_message` so that the failure fields and
        the status change are persisted in a single journal write.

        Why this matters: the previous implementation of `mark_failed`
        saved the failure fields (via `save_record`) BEFORE the state
        machine had a chance to validate the transition. When
        `transition` subsequently raised `ConflictError` (for
        example, because the operation was already in a terminal
        status), the record was left carrying `error_code` and
        `error_message` while its `status` remained unchanged — an
        operation that never failed appeared to have failed. Merging
        both updates into a single journal write eliminates that
        window entirely.

        `extra_fields`, when provided, are merged into the same
        `dataclasses.replace` call as the status change. They MUST
        NOT contain `status` or `updated_at`; those are owned by
        this method.
        """
        if not isinstance(to_status, OperationStatus):
            raise ValidationError(
                "to_status must be an OperationStatus.",
                details={"type": type(to_status).__name__},
            )

        key = _validate_operation_id(operation_id)
        # Hold the async lock across the full check-then-act
        # sequence. `asyncio.Lock` is held across `await` points,
        # which is what prevents two concurrent transitions on the
        # same operation from reading the same starting record and
        # overwriting each other's `save_record` (lost update).
        async with self._operation_lock(key):
            record = await self._journal.get_record(key)
            if record is None:
                raise NotFoundError(
                    f"Operation '{key}' was not found.",
                    details={"operation_id": key},
                )

            if record.status == to_status:
                raise ConflictError(
                    f"Operation is already in status '{to_status.value}'.",
                    details={"operation_id": key, "status": to_status.value},
                )

            if is_terminal_status(record.status):
                raise ConflictError(
                    f"Operation is in terminal status "
                    f"'{record.status.value}' and cannot transition.",
                    details={
                        "operation_id": key,
                        "from_status": record.status.value,
                        "to_status": to_status.value,
                    },
                )

            if not is_valid_transition(record.status, to_status):
                raise ConflictError(
                    f"Invalid transition from '{record.status.value}' "
                    f"to '{to_status.value}'.",
                    details={
                        "operation_id": key,
                        "from_status": record.status.value,
                        "to_status": to_status.value,
                        "allowed": sorted(
                            s.value for s in _allowed_next(record.status)
                        ),
                    },
                )

            # Apply the status change AND any extra fields in a
            # single `replace` call. The journal write that follows
            # is therefore atomic with respect to the state machine:
            # either both the status and the extra fields are
            # persisted, or neither is.
            update_kwargs: Dict[str, Any] = {
                "status": to_status,
                "updated_at": _utc_now(),
            }
            if extra_fields:
                for field_name, field_value in extra_fields.items():
                    if field_name in ("status", "updated_at"):
                        raise ValidationError(
                            f"extra_fields must not override "
                            f"'{field_name}'.",
                            details={"field": field_name},
                        )
                    update_kwargs[field_name] = field_value

            updated = replace(record, **update_kwargs)
            await self._journal.save_record(updated)

            transition = OperationTransition(
                operation_id=key,
                from_status=record.status,
                to_status=to_status,
                occurred_at=updated.updated_at,
                reason=reason,
                metadata=dict(metadata or {}),
            )

            await self._emit_event(
                operation_id=key,
                event_type=OperationEventType.STATE_TRANSITION,
                payload=transition.to_dict(),
            )

        # Audit outside the per-operation lock to keep the lock short.
        self._audit.record(
            AuditEventType.OPERATION_TRANSITION,
            severity=AuditSeverity.INFO,
            action="transition",
            message=(
                f"Operation transitioned from '{record.status.value}' "
                f"to '{to_status.value}'."
            ),
            payload={
                "operation_id": key,
                "from_status": record.status.value,
                "to_status": to_status.value,
                "reason": reason,
            },
            operation_id=key,
            repository_id=record.repository_id,
            provider=record.provider.value,
            actor=actor,
        )

        return updated

    # --- Convenience transitions ---------------------------------------

    async def cancel_operation(
        self,
        operation_id: str,
        *,
        reason: Optional[str] = None,
        actor: str = "user",
    ) -> OperationRecord:
        """
        Request cancellation of an operation.

        Moves the operation to `CANCEL_REQUESTED`. The caller MUST
        call `transition(CANCELLED)` once cleanup has completed.
        """
        return await self.transition(
            operation_id,
            OperationStatus.CANCEL_REQUESTED,
            reason=reason or "User requested cancellation.",
            actor=actor,
        )

    async def mark_failed(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        actor: str = "system",
    ) -> OperationRecord:
        """
        Move the operation to a failure status.

        `retryable=True` selects `FAILED_RETRYABLE`; otherwise
        `FAILED_FINAL`. The `error_code` and `error_message` are
        persisted on the record in the SAME journal write as the
        status change, so a failed transition (e.g. the operation
        is already terminal) leaves the record completely
        untouched — no orphan `error_code` with an unchanged status.

        See `_transition_internal` for the rationale.
        """
        target = (
            OperationStatus.FAILED_RETRYABLE
            if retryable
            else OperationStatus.FAILED_FINAL
        )
        return await self._transition_internal(
            operation_id,
            target,
            reason=error_message,
            metadata={"error_code": error_code, "retryable": retryable},
            actor=actor,
            extra_fields={
                "error_code": error_code,
                "error_message": error_message,
            },
        )

    # --- Side updates (no status change) -------------------------------

    async def attach_workspace(
        self,
        operation_id: str,
        workspace_id: str,
    ) -> OperationRecord:
        """
        Record the workspace attached to the operation.

        Does NOT change the status. The caller decides when to move
        the operation to `WORKSPACE_READY`.
        """
        if not workspace_id:
            raise ValidationError("workspace_id must not be empty.")
        return await self._update_record(
            operation_id,
            event_type=OperationEventType.WORKSPACE_ATTACHED,
            event_payload={"workspace_id": workspace_id},
            fields={"workspace_id": workspace_id},
        )

    async def attach_diff(
        self,
        operation_id: str,
        *,
        diff_sha256: str,
        base_sha: str,
    ) -> OperationRecord:
        """
        Record the reviewed diff hash and the base SHA.

        Does NOT change the status.
        """
        if not diff_sha256:
            raise ValidationError("diff_sha256 must not be empty.")
        if not base_sha:
            raise ValidationError("base_sha must not be empty.")
        return await self._update_record(
            operation_id,
            event_type=OperationEventType.FILE_CHANGED,
            event_payload={
                "diff_sha256": diff_sha256,
                "base_sha": base_sha,
            },
            fields={
                "diff_sha256": diff_sha256,
                "base_sha": base_sha,
            },
        )

    async def attach_approval(
        self,
        operation_id: str,
        approval_id: str,
    ) -> OperationRecord:
        """
        Record the approval ID that was consumed for the operation.
        """
        if not approval_id:
            raise ValidationError("approval_id must not be empty.")
        return await self._update_record(
            operation_id,
            event_type=OperationEventType.APPROVAL_GRANTED,
            event_payload={"approval_id": approval_id},
            fields={"approval_id": approval_id},
        )

    async def attach_change_request(
        self,
        operation_id: str,
        change_request: ChangeRequestRef,
    ) -> OperationRecord:
        """
        Record the change request created by the operation.

        Does NOT change the status. The caller decides when to move
        the operation to `CHANGE_REQUEST_CREATED`.
        """
        if not isinstance(change_request, ChangeRequestRef):
            raise ValidationError(
                "change_request must be a ChangeRequestRef."
            )
        return await self._update_record(
            operation_id,
            event_type=OperationEventType.CHANGE_REQUEST_CREATED,
            event_payload={
                "number_or_iid": change_request.number_or_iid,
                "web_url": change_request.web_url,
            },
            fields={"change_request": change_request},
        )

    # --- Maintenance ----------------------------------------------------

    async def clear(self) -> None:
        """
        Forget every cached sequence and delegate `clear` to the
        journal.

        Intended for tests. The journal's `clear` may or may not
        delete durable files depending on the implementation.
        """
        with self._locks_lock:
            self._event_sequences.clear()
            self._operation_locks.clear()
            self._event_locks.clear()
        await self._journal.clear()

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"<OperationService journal={self._journal.backend_name!r} "
            f"cached_sequences={len(self._event_sequences)} "
            f"event_locks={len(self._event_locks)}>"
        )

    # --- Internal: record updates --------------------------------------

    async def _update_record(
        self,
        operation_id: str,
        *,
        event_type: OperationEventType,
        event_payload: Mapping[str, Any],
        fields: Mapping[str, Any],
    ) -> OperationRecord:
        """
        Apply a set of field updates to an operation record and emit
        a corresponding event.

        This is the single place where a record is mutated without a
        status change. It is safe with respect to the state machine:
        the status is never altered here.
        """
        key = _validate_operation_id(operation_id)
        # Same locking rationale as `_transition_internal`: the
        # check-then-act sequence includes `await` calls, so the
        # lock MUST be an `asyncio.Lock` to actually exclude other
        # coroutines.
        async with self._operation_lock(key):
            record = await self._journal.get_record(key)
            if record is None:
                raise NotFoundError(
                    f"Operation '{key}' was not found.",
                    details={"operation_id": key},
                )

            if is_terminal_status(record.status):
                raise ConflictError(
                    f"Cannot update a terminal operation "
                    f"('{record.status.value}').",
                    details={"operation_id": key},
                )

            updated = replace(
                record,
                updated_at=_utc_now(),
                **fields,
            )
            await self._journal.save_record(updated)

            await self._emit_event(
                operation_id=key,
                event_type=event_type,
                payload=dict(event_payload),
            )

        return updated

    # --- Internal: event sequencing ------------------------------------

    def _operation_lock(self, operation_id: str) -> asyncio.Lock:
        """
        Return the per-operation async lock, creating it on first
        use.

        The lock serializes every state mutation for a given
        operation (`_transition_internal`, `_update_record`,
        `delete_operation`). It MUST be held across the entire
        check-then-act critical section, including every `await`
        inside it; otherwise the lost-update race described in the
        `__init__` comment reappears.

        Lock ordering (never reversed anywhere in the service):

            _operation_lock(key) -> _event_lock(key)

        `_emit_event` is called from inside `_operation_lock` and
        acquires `_event_lock` in turn. No code path acquires the
        two locks in the opposite order, so the ordering is
        deadlock-free.

        `_locks_lock` (a `threading.RLock`) is used ONLY to guard
        the dictionary that maps operation IDs to locks. Dictionary
        access is synchronous and await-free, so `threading.RLock`
        is appropriate there and does not suffer from the same
        re-entrancy issue.
        """
        with self._locks_lock:
            lock = self._operation_locks.get(operation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._operation_locks[operation_id] = lock
            return lock

    def _event_lock(self, operation_id: str) -> asyncio.Lock:
        """
        Return the per-operation async lock for event emission.

        The lock serializes sequence allocation and journal append
        across coroutines. `threading.RLock` cannot do this because
        every coroutine runs in the same OS thread, so an RLock held
        by one coroutine does not exclude another; `asyncio.Lock`
        yields the event loop while waiting, which is the correct
        semantics.
        """
        with self._locks_lock:
            lock = self._event_locks.get(operation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._event_locks[operation_id] = lock
            return lock

    async def _emit_event(
        self,
        *,
        operation_id: str,
        event_type: OperationEventType,
        payload: Mapping[str, Any],
    ) -> OperationEvent:
        """
        Append an event to the journal with the next sequence number.

        Bug 9 fix (duplicate sequences under concurrency):
            Sequence allocation and journal append are now executed
            while holding the per-operation `_event_lock`, so:

                1. Two events for the same operation can never
                   receive the same sequence number.
                2. Event N is always appended to the journal before
                   event N+1, even when multiple coroutines emit
                   events concurrently.

            The previous implementation performed the allocation
            under a `threading.RLock`, which -- because asyncio runs
            every coroutine in the same OS thread -- allowed two
            coroutines to interleave at `await` points and race on
            the cached sequence counter.

            The lock is acquired per-operation, so emitting events
            for different operations remains fully concurrent.
        """
        key = _validate_operation_id(operation_id)
        async with self._event_lock(key):
            next_seq = await self._next_event_sequence_locked(key)

            event = OperationEvent(
                operation_id=key,
                sequence=next_seq,
                event_type=event_type,
                occurred_at=_utc_now(),
                payload=dict(payload),
            )
            await self._journal.append_event(event)
            return event

    async def _next_event_sequence_locked(
        self, operation_id: str
    ) -> int:
        """
        Return the next event sequence number for the operation.

        CALLER MUST HOLD `self._event_lock(operation_id)`.

        The `_locked` suffix follows the project-wide convention for
        functions that assume the caller holds a lock; it makes the
        concurrency contract visible at every call site. Callers
        outside this class MUST NOT invoke it directly.

        The sequence cache is consulted first. On a cache miss (for
        example after a service restart), the journal is queried for
        the highest existing sequence, which is why a slow journal
        read here blocks only events for the SAME operation -- not
        events for other operations, which have their own locks.
        """
        with self._locks_lock:
            cached = self._event_sequences.get(operation_id)
        if cached is not None:
            next_seq = cached + 1
            with self._locks_lock:
                self._event_sequences[operation_id] = next_seq
            return next_seq

        # Cache miss: read the highest existing sequence from the
        # journal. The await happens while the caller holds the
        # per-operation async lock, so no other coroutine can emit
        # an event for this operation until the allocation completes.
        events = await self._journal.list_events(operation_id)
        last_seq = max((e.sequence for e in events), default=0)
        next_seq = last_seq + 1
        with self._locks_lock:
            self._event_sequences[operation_id] = next_seq
        return next_seq


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allowed_next(status: OperationStatus) -> Sequence[OperationStatus]:
    """
    Return the set of statuses reachable from `status`.

    Wrapped so that the service does not depend on the exact shape of
    `ALLOWED_TRANSITIONS`.
    """
    from .models import allowed_next_statuses

    return tuple(allowed_next_statuses(status))


# ---------------------------------------------------------------------------
# Default service singleton
# ---------------------------------------------------------------------------


_DEFAULT_SERVICE: Optional[OperationService] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_operation_service() -> OperationService:
    """
    Return the process-wide default `OperationService`, creating it
    on first use.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = OperationService()
        return _DEFAULT_SERVICE


def set_default_operation_service(service: OperationService) -> None:
    """
    Replace the process-wide default operation service.

    Intended for application startup and for tests.
    """
    if not isinstance(service, OperationService):
        raise TypeError(
            "set_default_operation_service expects an OperationService."
        )
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = service


def reset_default_operation_service() -> OperationService:
    """
    Replace the default service with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = OperationService()
        return _DEFAULT_SERVICE
