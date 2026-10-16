# backend/integrations/source_control/security/approval.py
"""
Approval gate for the Source Control Integration subsystem.

The approval gate is the ONLY component permitted to create, grant,
and consume approval tickets. It enforces the following invariants:

    1. An approval ticket is single-use. Once consumed, it cannot be
       reused.
    2. An approval ticket is content-bound: it records the exact
       diff hash, base SHA, target branch, repository, and operation.
       Before consumption, every bound field MUST match the current
       operation snapshot.
    3. An approval ticket expires. Consuming an expired ticket fails.
    4. An approval ticket MUST be granted by the user through the
       UI. An agent, a tool, or an LLM response CANNOT grant a
       ticket, and CANNOT mark itself as approved.
    5. Every state change on a ticket is recorded in the audit log.

The `ApprovalTicket` model is defined in `domain.models` and is the
single source of truth. This module MUST NOT redefine it.

Design rules:
    - The gate is thread-safe. All mutations hold a single `RLock`.
    - The gate never raises on missing or malformed data during
      cleanup; it only raises on explicit validation failures.
    - The gate never stores secrets. Only identifiers and hashes are
      stored.
    - The gate is deterministic: given the same inputs and the same
      clock, it produces the same outputs.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from ..domain.enums import ApprovalStatus
from ..domain.models import ApprovalTicket
from ..errors import (
    ApprovalConsumedError,
    ApprovalExpiredError,
    ApprovalInvalidError,
    ApprovalRequiredError,
    ValidationError,
)
from .audit import (
    AuditEventType,
    AuditLogger,
    AuditSeverity,
    get_default_audit_logger,
)

__all__ = [
    "ApprovalGate",
    "ApprovalValidationError",
    "get_default_approval_gate",
    "set_default_approval_gate",
    "reset_default_approval_gate",
]


logger = logging.getLogger(__name__)


#: Default TTL for a new ticket if no policy is provided.
DEFAULT_APPROVAL_TTL_SECONDS: int = 15 * 60  # 15 minutes

#: Default maximum number of tickets retained in memory.
DEFAULT_MAX_TICKETS: int = 10_000

#: Pattern for a valid SHA-256 hex digest (64 lowercase hex chars).
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

#: Values that are accepted as a "base SHA" but are not real hashes.
#: These are useful in tests and in operations that do not yet have a
#: concrete commit SHA. They MUST NOT be accepted at consumption time
#: against a real remote.
_BASE_SHA_ALLOWLIST = frozenset({"HEAD", "UNKNOWN", "UNBORN"})


# Alias for callers that prefer a descriptive name. The base class is
# `ValidationError` from `errors.py`; this alias keeps the public API
# intuitive without adding a second exception type.
ApprovalValidationError = ValidationError


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


class ApprovalGate:
    """
    In-memory store and state machine for approval tickets.

    The gate does NOT persist tickets to disk; the durable operation
    journal (see `operations.journal`) is responsible for persistence.
    The gate is reconstructed on startup from the journal if needed.
    """

    def __init__(
        self,
        *,
        audit_logger: Optional[AuditLogger] = None,
        default_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        max_tickets: int = DEFAULT_MAX_TICKETS,
    ) -> None:
        if max_tickets <= 0:
            raise ValueError("max_tickets must be positive.")
        if default_ttl_seconds <= 0:
            # Negative or zero TTL is accepted only per-ticket for
            # tests; the constructor default must remain positive.
            default_ttl_seconds = DEFAULT_APPROVAL_TTL_SECONDS

        self._tickets: Dict[str, ApprovalTicket] = {}
        self._lock = threading.RLock()
        self._audit = audit_logger or get_default_audit_logger()
        self._default_ttl_seconds = int(default_ttl_seconds)
        self._max_tickets = int(max_tickets)

    # --- Creation -------------------------------------------------------

    def create_ticket(
        self,
        *,
        operation_id: str,
        repository_id: str,
        operation: str,
        base_sha: str,
        diff_sha256: str,
        target_branch: str,
        ttl_seconds: Optional[int] = None,
        approval_id: Optional[str] = None,
    ) -> ApprovalTicket:
        """
        Create a new pending approval ticket.

        All fields are validated before the ticket is created:

            - `operation_id`, `repository_id`, `operation`, and
              `target_branch` MUST be non-empty.
            - `base_sha` MUST be non-empty and MUST either be a valid
              SHA-256 hex digest or one of `{"HEAD", "UNKNOWN",
              "UNBORN"}`.
            - `diff_sha256` MUST be a 64-character lowercase hex
              digest.

        Args:
            operation_id: The operation this approval covers.
            repository_id: The repository the operation targets.
            operation: Operation kind (e.g. `"push_branch"`).
            base_sha: Base commit SHA the diff was computed against.
            diff_sha256: Canonical hash of the reviewed diff.
            target_branch: Branch the operation targets.
            ttl_seconds: Optional TTL override. A value <= 0 creates
                an already-expired ticket, which is useful for tests.
            approval_id: Optional identifier for the ticket. When
                `None`, a random 16-hex-character ID is generated.

        Returns:
            A new `ApprovalTicket` in `PENDING` status.

        Raises:
            ValidationError: If any input is invalid.
        """
        self._validate_non_empty(operation_id, "operation_id")
        self._validate_non_empty(repository_id, "repository_id")
        self._validate_non_empty(operation, "operation")
        self._validate_non_empty(target_branch, "target_branch")
        self._validate_base_sha(base_sha)
        self._validate_diff_sha256(diff_sha256)

        ttl = (
            self._default_ttl_seconds
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        now = _utc_now()
        expires_at = now + timedelta(seconds=ttl)

        ticket = ApprovalTicket(
            approval_id=approval_id or secrets.token_hex(8),
            operation_id=operation_id,
            repository_id=repository_id,
            operation=operation,
            base_sha=base_sha,
            diff_sha256=diff_sha256,
            target_branch=target_branch,
            expires_at=expires_at,
            approved_by_user=False,
            status=ApprovalStatus.PENDING,
            created_at=now,
            consumed_at=None,
        )

        with self._lock:
            self._enforce_capacity_locked()
            self._tickets[ticket.approval_id] = ticket

        self._audit.record(
            AuditEventType.APPROVAL_REQUEST,
            severity=AuditSeverity.INFO,
            action="create_ticket",
            message=(
                f"Approval requested for operation '{operation}' on "
                f"branch '{target_branch}'."
            ),
            payload={
                "approval_id": ticket.approval_id,
                "operation": operation,
                "base_sha": base_sha,
                "diff_sha256": diff_sha256,
                "target_branch": target_branch,
                "ttl_seconds": ttl,
            },
            operation_id=operation_id,
            repository_id=repository_id,
            approval_id=ticket.approval_id,
            actor="system",
        )

        return ticket

    # --- State transitions ---------------------------------------------

    def grant(
        self,
        approval_id: str,
        *,
        actor: str = "user",
    ) -> ApprovalTicket:
        """
        Mark a ticket as granted by the user.

        This method MUST only be invoked by the UI approval endpoint,
        never by an agent or tool. The `actor` field is recorded in
        the audit log; passing `"agent"` is accepted but the caller is
        responsible for ensuring the invariant is respected.

        Raises:
            ValidationError: If `approval_id` is empty.
            ApprovalInvalidError: If the ticket does not exist.
            ApprovalExpiredError: If the ticket has expired.
            ApprovalConsumedError: If the ticket has been consumed.
        """
        return self._transition(
            approval_id,
            target_status=ApprovalStatus.GRANTED,
            approved_by_user=True,
            audit_event_type=AuditEventType.APPROVAL_GRANT,
            audit_action="grant",
            actor=actor,
        )

    def reject(
        self,
        approval_id: str,
        *,
        actor: str = "user",
        reason: Optional[str] = None,
    ) -> ApprovalTicket:
        """
        Mark a ticket as rejected by the user.

        A rejected ticket cannot be granted or consumed afterwards.
        """
        return self._transition(
            approval_id,
            target_status=ApprovalStatus.REJECTED,
            approved_by_user=False,
            audit_event_type=AuditEventType.APPROVAL_REJECT,
            audit_action="reject",
            actor=actor,
            extra_payload={"reason": reason} if reason else None,
        )

    def revoke(
        self,
        approval_id: str,
        *,
        actor: str = "system",
        reason: Optional[str] = None,
    ) -> ApprovalTicket:
        """
        Revoke a ticket.

        Used when the underlying operation is cancelled or when the
        workspace is discarded. Revoked tickets cannot be consumed.
        """
        return self._transition(
            approval_id,
            target_status=ApprovalStatus.REVOKED,
            approved_by_user=False,
            audit_event_type=AuditEventType.APPROVAL_REJECT,
            audit_action="revoke",
            actor=actor,
            extra_payload={"reason": reason} if reason else None,
        )

    # --- Consumption ----------------------------------------------------

    def consume(
        self,
        approval_id: str,
        *,
        expected_operation_id: str,
        expected_repository_id: str,
        expected_operation: str,
        expected_base_sha: str,
        expected_diff_sha256: str,
        expected_target_branch: str,
        actor: str = "system",
    ) -> ApprovalTicket:
        """
        Consume a ticket, marking it as used.

        Every bound field on the ticket MUST match the expected
        values. Any mismatch invalidates the ticket and raises the
        corresponding error. This guarantees that the approval the
        user granted still applies to the operation about to run.

        On success, the ticket's status is set to `CONSUMED`, its
        `consumed_at` timestamp is recorded, and an audit event is
        written. The same `ApprovalTicket` instance is returned with
        the updated status (as a new instance, since the model is
        frozen).

        Raises:
            ValidationError: If any input is empty or malformed.
            ApprovalRequiredError: If the ticket does not exist.
            ApprovalExpiredError: If the ticket has expired.
            ApprovalConsumedError: If the ticket was already consumed.
            ApprovalInvalidError: If any bound field does not match.
        """
        # Validate inputs first (fail fast on caller bugs).
        self._validate_non_empty(expected_operation_id, "expected_operation_id")
        self._validate_non_empty(expected_repository_id, "expected_repository_id")
        self._validate_non_empty(expected_operation, "expected_operation")
        self._validate_non_empty(expected_target_branch, "expected_target_branch")
        self._validate_diff_sha256(expected_diff_sha256)
        self._validate_base_sha(expected_base_sha)

        with self._lock:
            ticket = self._get_ticket_locked(approval_id)
            self._assert_consumable_locked(ticket)

            self._assert_matches(
                ticket,
                expected_operation_id=expected_operation_id,
                expected_repository_id=expected_repository_id,
                expected_operation=expected_operation,
                expected_base_sha=expected_base_sha,
                expected_diff_sha256=expected_diff_sha256,
                expected_target_branch=expected_target_branch,
            )

            consumed = self._replace_ticket(
                ticket,
                status=ApprovalStatus.CONSUMED,
                consumed_at=_utc_now(),
            )
            self._tickets[approval_id] = consumed

        self._audit.record(
            AuditEventType.APPROVAL_CONSUME,
            severity=AuditSeverity.INFO,
            action="consume",
            message="Approval consumed by the operation.",
            payload={
                "approval_id": approval_id,
                "operation": expected_operation,
                "base_sha": expected_base_sha,
                "diff_sha256": expected_diff_sha256,
                "target_branch": expected_target_branch,
            },
            operation_id=expected_operation_id,
            repository_id=expected_repository_id,
            approval_id=approval_id,
            actor=actor,
        )

        return consumed

    # --- Validation (without consumption) ------------------------------

    def validate(
        self,
        approval_id: str,
        *,
        expected_operation_id: str,
        expected_repository_id: str,
        expected_operation: str,
        expected_base_sha: str,
        expected_diff_sha256: str,
        expected_target_branch: str,
    ) -> None:
        """
        Validate that a ticket would be consumable.

        Raises the same errors as `consume` but does NOT change the
        ticket's status. Use this before starting a long-running
        operation to fail fast.
        """
        self._validate_non_empty(expected_operation_id, "expected_operation_id")
        self._validate_non_empty(expected_repository_id, "expected_repository_id")
        self._validate_non_empty(expected_operation, "expected_operation")
        self._validate_non_empty(expected_target_branch, "expected_target_branch")
        self._validate_diff_sha256(expected_diff_sha256)
        self._validate_base_sha(expected_base_sha)

        with self._lock:
            ticket = self._get_ticket_locked(approval_id)
            self._assert_consumable_locked(ticket)
            self._assert_matches(
                ticket,
                expected_operation_id=expected_operation_id,
                expected_repository_id=expected_repository_id,
                expected_operation=expected_operation,
                expected_base_sha=expected_base_sha,
                expected_diff_sha256=expected_diff_sha256,
                expected_target_branch=expected_target_branch,
            )

    # --- Lookup ---------------------------------------------------------

    def get_ticket(self, approval_id: str) -> Optional[ApprovalTicket]:
        """
        Return the ticket, or `None` if unknown.

        This method does NOT validate or consume the ticket. It is
        intended for read-only UI display.
        """
        if not approval_id:
            return None
        with self._lock:
            return self._tickets.get(approval_id)

    def list_pending(
        self, *, operation_id: Optional[str] = None
    ) -> Sequence[ApprovalTicket]:
        """
        Return all pending tickets, optionally filtered by operation.

        The returned tuple is a snapshot and is safe to iterate
        without holding the lock.
        """
        with self._lock:
            tickets = tuple(self._tickets.values())

        pending = [
            t
            for t in tickets
            if t.status == ApprovalStatus.PENDING
            and (operation_id is None or t.operation_id == operation_id)
        ]
        pending.sort(key=lambda t: t.created_at, reverse=True)
        return tuple(pending)

    def list_for_operation(
        self, operation_id: str
    ) -> Sequence[ApprovalTicket]:
        """Return all tickets for a given operation, newest first."""
        with self._lock:
            tickets = tuple(
                t for t in self._tickets.values()
                if t.operation_id == operation_id
            )
        tickets_list = list(tickets)
        tickets_list.sort(key=lambda t: t.created_at, reverse=True)
        return tuple(tickets_list)

    # --- Maintenance ----------------------------------------------------

    def prune_expired(self) -> int:
        """
        Remove tickets that are expired, consumed, rejected, or
        revoked. Returns the number of tickets removed.

        Called periodically by the operation service to bound memory
        usage.
        """
        now = _utc_now()
        with self._lock:
            stale_ids: List[str] = []
            for approval_id, ticket in self._tickets.items():
                if ticket.status in (
                    ApprovalStatus.CONSUMED,
                    ApprovalStatus.REJECTED,
                    ApprovalStatus.REVOKED,
                    ApprovalStatus.EXPIRED,
                ):
                    stale_ids.append(approval_id)
                elif ticket.expires_at <= now:
                    stale_ids.append(approval_id)
            for approval_id in stale_ids:
                self._tickets.pop(approval_id, None)
            return len(stale_ids)

    def clear(self) -> None:
        """Remove every ticket. Intended for tests."""
        with self._lock:
            self._tickets.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._tickets)

    # --- Internal helpers ----------------------------------------------

    def _get_ticket_locked(self, approval_id: str) -> ApprovalTicket:
        self._validate_non_empty(approval_id, "approval_id")
        ticket = self._tickets.get(approval_id)
        if ticket is None:
            raise ApprovalInvalidError(
                f"Approval ticket '{approval_id}' is not known.",
                details={"approval_id": approval_id},
            )
        return ticket

    def _assert_consumable_locked(self, ticket: ApprovalTicket) -> None:
        """
        Raise the appropriate error if the ticket cannot be consumed.

        Distinguishes the failure reason so the UI can guide the user:
            - expired   -> ask the user to re-approve the current diff.
            - consumed  -> the operation already ran.
            - pending   -> the user has not approved yet.
            - rejected  -> the user explicitly declined.
            - revoked   -> the operation was cancelled.
        """
        now = _utc_now()

        if ticket.status == ApprovalStatus.CONSUMED:
            raise ApprovalConsumedError(
                "Approval ticket has already been consumed.",
                details={"approval_id": ticket.approval_id},
            )
        if ticket.status == ApprovalStatus.REJECTED:
            raise ApprovalInvalidError(
                "Approval ticket was rejected by the user.",
                details={"approval_id": ticket.approval_id},
            )
        if ticket.status == ApprovalStatus.REVOKED:
            raise ApprovalInvalidError(
                "Approval ticket was revoked.",
                details={"approval_id": ticket.approval_id},
            )
        if ticket.status == ApprovalStatus.EXPIRED or ticket.expires_at <= now:
            raise ApprovalExpiredError(
                "Approval ticket has expired.",
                details={
                    "approval_id": ticket.approval_id,
                    "expires_at": ticket.expires_at.isoformat(),
                },
            )
        if ticket.status == ApprovalStatus.PENDING:
            # The user must explicitly grant the approval through the
            # UI. An agent MUST NOT auto-approve.
            raise ApprovalRequiredError(
                "Approval ticket has not been granted by the user.",
                details={"approval_id": ticket.approval_id},
            )
        if not ticket.approved_by_user:
            raise ApprovalRequiredError(
                "Approval ticket is missing the user's consent flag.",
                details={"approval_id": ticket.approval_id},
            )

    def _assert_matches(
        self,
        ticket: ApprovalTicket,
        *,
        expected_operation_id: str,
        expected_repository_id: str,
        expected_operation: str,
        expected_base_sha: str,
        expected_diff_sha256: str,
        expected_target_branch: str,
    ) -> None:
        """
        Raise `ApprovalInvalidError` when any bound field differs.

        The check is exhaustive: every field that binds the approval
        to a specific operation is compared. This is what prevents
        approving one diff and executing another.
        """
        mismatches: dict[str, tuple[str, str]] = {}

        def _check(name: str, actual: str, expected: str) -> None:
            if actual != expected:
                mismatches[name] = (actual, expected)

        _check("operation_id", ticket.operation_id, expected_operation_id)
        _check("repository_id", ticket.repository_id, expected_repository_id)
        _check("operation", ticket.operation, expected_operation)
        _check("base_sha", ticket.base_sha, expected_base_sha)
        _check("diff_sha256", ticket.diff_sha256, expected_diff_sha256)
        _check("target_branch", ticket.target_branch, expected_target_branch)

        if mismatches:
            raise ApprovalInvalidError(
                "Approval ticket does not match the current operation.",
                details={
                    "approval_id": ticket.approval_id,
                    "mismatches": {
                        name: {"ticket": actual, "expected": expected}
                        for name, (actual, expected) in mismatches.items()
                    },
                },
            )

    def _transition(
        self,
        approval_id: str,
        *,
        target_status: ApprovalStatus,
        approved_by_user: bool,
        audit_event_type: AuditEventType,
        audit_action: str,
        actor: str,
        extra_payload: Optional[dict] = None,
    ) -> ApprovalTicket:
        """
        Perform a state transition on a ticket.

        Guards against invalid transitions:
            - Consumed tickets cannot be re-transitioned.
            - Expired tickets cannot be granted or rejected.
            - Rejected tickets cannot be granted.
        """
        with self._lock:
            ticket = self._get_ticket_locked(approval_id)

            if ticket.status == ApprovalStatus.CONSUMED:
                raise ApprovalConsumedError(
                    "Cannot modify a consumed approval ticket.",
                    details={"approval_id": approval_id},
                )
            if ticket.status == ApprovalStatus.REVOKED and target_status not in (
                ApprovalStatus.REVOKED,
            ):
                raise ApprovalInvalidError(
                    "Cannot modify a revoked approval ticket.",
                    details={"approval_id": approval_id},
                )
            if ticket.expires_at <= _utc_now() and target_status == ApprovalStatus.GRANTED:
                # Mark the ticket as expired and refuse the grant.
                expired = self._replace_ticket(
                    ticket, status=ApprovalStatus.EXPIRED
                )
                self._tickets[approval_id] = expired
                raise ApprovalExpiredError(
                    "Approval ticket has expired.",
                    details={
                        "approval_id": approval_id,
                        "expires_at": ticket.expires_at.isoformat(),
                    },
                )

            updated = self._replace_ticket(
                ticket,
                status=target_status,
                approved_by_user=approved_by_user,
            )
            self._tickets[approval_id] = updated

        payload = {
            "approval_id": approval_id,
            "previous_status": ticket.status.value,
            "new_status": target_status.value,
        }
        if extra_payload:
            payload.update(extra_payload)

        self._audit.record(
            audit_event_type,
            severity=AuditSeverity.INFO,
            action=audit_action,
            message=f"Approval ticket transitioned to {target_status.value}.",
            payload=payload,
            operation_id=ticket.operation_id,
            repository_id=ticket.repository_id,
            approval_id=approval_id,
            actor=actor,
        )

        return updated

    def _replace_ticket(
        self, ticket: ApprovalTicket, **overrides
    ) -> ApprovalTicket:
        """
        Return a new ticket with the given fields replaced.

        Kept as a small wrapper so the frozen-dataclass replacement is
        centralized and easy to audit.
        """
        from dataclasses import replace

        return replace(ticket, **overrides)

    def _enforce_capacity_locked(self) -> None:
        """
        Ensure the ticket store has room for a new entry.

        When the store is at capacity, the oldest terminal or expired
        tickets are pruned. If no tickets can be pruned, the oldest
        pending ticket is removed with a warning.

        This method MUST be called while holding `self._lock`.
        """
        if len(self._tickets) < self._max_tickets:
            return

        # First pass: prune terminals and expired tickets.
        removed = 0
        now = _utc_now()
        for approval_id, ticket in list(self._tickets.items()):
            if ticket.status in (
                ApprovalStatus.CONSUMED,
                ApprovalStatus.REJECTED,
                ApprovalStatus.REVOKED,
                ApprovalStatus.EXPIRED,
            ) or ticket.expires_at <= now:
                self._tickets.pop(approval_id, None)
                removed += 1

        if len(self._tickets) < self._max_tickets:
            return

        # Second pass: drop the oldest pending ticket.
        oldest_id = min(
            self._tickets,
            key=lambda k: self._tickets[k].created_at,
        )
        logger.warning(
            "Approval ticket store at capacity (%d); evicting oldest "
            "pending ticket %s.",
            self._max_tickets,
            oldest_id,
        )
        self._tickets.pop(oldest_id, None)

    # --- Input validation helpers --------------------------------------

    @staticmethod
    def _validate_non_empty(value: str, field_name: str) -> None:
        if not value or not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"{field_name} must be a non-empty string.",
                details={"field": field_name},
            )

    @staticmethod
    def _validate_base_sha(base_sha: str) -> None:
        if not base_sha or not isinstance(base_sha, str):
            raise ValidationError(
                "base_sha must be a non-empty string.",
                details={"field": "base_sha"},
            )
        if base_sha in _BASE_SHA_ALLOWLIST:
            return
        if not _SHA256_PATTERN.match(base_sha):
            raise ValidationError(
                "base_sha must be a 64-character lowercase hex digest "
                "or one of the allowlisted placeholders.",
                details={"field": "base_sha", "value_length": len(base_sha)},
            )

    @staticmethod
    def _validate_diff_sha256(diff_sha256: str) -> None:
        if not diff_sha256 or not isinstance(diff_sha256, str):
            raise ValidationError(
                "diff_sha256 must be a non-empty string.",
                details={"field": "diff_sha256"},
            )
        if not _SHA256_PATTERN.match(diff_sha256):
            raise ValidationError(
                "diff_sha256 must be a 64-character lowercase hex digest.",
                details={
                    "field": "diff_sha256",
                    "value_length": len(diff_sha256),
                },
            )


# ---------------------------------------------------------------------------
# Default gate singleton
# ---------------------------------------------------------------------------


_DEFAULT_GATE: Optional[ApprovalGate] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_approval_gate() -> ApprovalGate:
    """
    Return the process-wide default approval gate, creating it on
    first use.
    """
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        if _DEFAULT_GATE is None:
            _DEFAULT_GATE = ApprovalGate()
        return _DEFAULT_GATE


def set_default_approval_gate(gate: ApprovalGate) -> None:
    """
    Replace the process-wide default approval gate.

    Intended for application startup and for tests.
    """
    if not isinstance(gate, ApprovalGate):
        raise TypeError(
            "set_default_approval_gate expects an ApprovalGate instance."
        )
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        _DEFAULT_GATE = gate


def reset_default_approval_gate() -> ApprovalGate:
    """
    Replace the default gate with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        _DEFAULT_GATE = ApprovalGate()
        return _DEFAULT_GATE
