from __future__ import annotations

import dataclasses
import hashlib
import secrets
import string
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from ..domain.enums import ApprovalDecision, OperationKind
from ..errors import (
    ApprovalInvalidError,
    ApprovalRequiredError,
    PermissionDeniedError,
    RemoteStateChangedError,
    ValidationError,
)

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def compute_diff_sha256(diff_text: str | bytes) -> str:
    """Computes a canonical SHA-256 hex digest of patch diff content.

    Normalizes Windows (CRLF) and Unix (LF) line endings before hashing
    to ensure deterministic cross-platform verification.
    """
    if isinstance(diff_text, bytes):
        raw = diff_text.decode("utf-8", "replace")
    else:
        raw = str(diff_text)

    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ApprovalTicket:
    """Immutable representation of a human approval authorization ticket."""

    approval_id: str
    operation_id: str
    repository_id: str
    operation: str = "push_branch"
    base_sha: str = ""
    diff_sha256: str = ""
    target_branch: str = ""
    source_branch: Optional[str] = None
    expires_at: Optional[datetime] = None
    approved_by_user: bool = False
    user_id: Optional[str] = None
    notes: Optional[str] = None
    rejection_reason: Optional[str] = None
    is_consumed: bool = False
    decision: ApprovalDecision = ApprovalDecision.PENDING
    operation_kind: Optional[OperationKind] = None
    created_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    commit_message: str = ""
    change_request_title: str = ""
    change_request_body: str = ""
    total_files: int = 0
    touches_workflow_files: bool = False
    is_protected_target: bool = False


class ApprovalGate:
    """Thread-safe manager enforcing human approval lifecycles and cryptographic diff validation."""

    def __init__(self, default_ttl_minutes: int = 60) -> None:
        self.default_ttl_minutes = default_ttl_minutes
        self._tickets: dict[str, ApprovalTicket] = {}
        self._consumed_ticket_ids: set[str] = set()
        self._rejected_ticket_ids: set[str] = set()
        self._lock = threading.Lock()

    def create_ticket(
        self,
        operation_id: str,
        repository_id: str,
        operation: Optional[str] = None,
        base_sha: str = "",
        diff_sha256: str = "",
        target_branch: str = "",
        *,
        operation_kind: Optional[OperationKind] = None,
        source_branch: Optional[str] = None,
        commit_message: str = "",
        change_request_title: str = "",
        change_request_body: str = "",
        ttl_seconds: Optional[int] = None,
        ttl_minutes: Optional[int] = None,
    ) -> ApprovalTicket:
        """Generates a cryptographically unguessable, single-use pending approval ticket."""
        clean_op_id = (operation_id or "").strip()
        if not clean_op_id:
            raise ValidationError("operation_id cannot be empty.")

        clean_repo_id = (repository_id or "").strip()
        if not clean_repo_id:
            raise ValidationError("repository_id cannot be empty.")

        effective_op = operation
        if not effective_op and operation_kind is not None:
            effective_op = operation_kind.value if hasattr(operation_kind, "value") else str(operation_kind)
        effective_op = (effective_op or "").strip()
        if not effective_op:
            raise ValidationError("operation cannot be empty.")

        clean_base_sha = (base_sha or "").strip()
        if not clean_base_sha:
            raise ValidationError("base_sha cannot be empty.")

        clean_target_branch = (target_branch or "").strip()
        if not clean_target_branch:
            raise ValidationError("target_branch cannot be empty.")

        clean_diff_hash = (diff_sha256 or "").strip()
        if len(clean_diff_hash) != 64 or not all(c in _HEX_CHARS for c in clean_diff_hash):
            raise ValidationError(
                f"diff_sha256 must be a 64-character hex string, got '{diff_sha256}'."
            )

        now = datetime.utcnow()
        if ttl_seconds is not None:
            expires_at = now + timedelta(seconds=ttl_seconds)
        elif ttl_minutes is not None:
            expires_at = now + timedelta(minutes=ttl_minutes)
        else:
            expires_at = now + timedelta(minutes=self.default_ttl_minutes)

        approval_id = f"appr_{secrets.token_hex(16)}"

        ticket = ApprovalTicket(
            approval_id=approval_id,
            operation_id=clean_op_id,
            repository_id=clean_repo_id,
            operation=effective_op,
            base_sha=clean_base_sha,
            diff_sha256=clean_diff_hash,
            target_branch=clean_target_branch,
            source_branch=source_branch,
            expires_at=expires_at,
            approved_by_user=False,
            created_at=now,
            decision=ApprovalDecision.PENDING,
            operation_kind=operation_kind,
            commit_message=commit_message,
            change_request_title=change_request_title,
            change_request_body=change_request_body,
        )

        with self._lock:
            self._tickets[approval_id] = ticket

        return ticket

    def get_ticket(self, approval_id: str) -> Optional[ApprovalTicket]:
        """Retrieves a ticket by its ID."""
        with self._lock:
            return self._tickets.get(approval_id)

    def list_tickets(
        self, operation_id: Optional[str] = None
    ) -> Sequence[ApprovalTicket]:
        """Returns an immutable snapshot of tickets, optionally filtered by operation ID."""
        with self._lock:
            if operation_id is not None:
                return tuple(
                    t for t in self._tickets.values() if t.operation_id == operation_id
                )
            return tuple(self._tickets.values())

    def approve_ticket(
        self,
        approval_id: str,
        user_id: str = "user",
        *,
        notes: Optional[str] = None,
        approved_by: Optional[str] = None,
    ) -> ApprovalTicket:
        """Approves a pending ticket.

        Raises PermissionDeniedError or ApprovalInvalidError if ticket is missing, consumed, or expired.
        """
        now = datetime.utcnow()
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                raise PermissionDeniedError(f"Approval ticket '{approval_id}' not found.")

            if approval_id in self._consumed_ticket_ids:
                raise PermissionDeniedError(f"Approval ticket '{approval_id}' has already been consumed.")

            if approval_id in self._rejected_ticket_ids or ticket.decision == ApprovalDecision.REJECTED:
                raise PermissionDeniedError(f"Approval ticket '{approval_id}' was rejected.")

            if ticket.expires_at is not None and now >= ticket.expires_at:
                expired_ticket = dataclasses.replace(
                    ticket, decision=ApprovalDecision.EXPIRED
                )
                self._tickets[approval_id] = expired_ticket
                raise PermissionDeniedError("Approval ticket has expired.")

            actor = user_id or approved_by or "user"
            approved_ticket = dataclasses.replace(
                ticket,
                approved_by_user=True,
                user_id=actor,
                approved_by=actor,
                notes=notes,
                decided_at=now,
                decision=ApprovalDecision.APPROVED,
            )
            self._tickets[approval_id] = approved_ticket
            return approved_ticket

    def reject_ticket(
        self,
        approval_id: str,
        user_id: str = "user",
        *,
        reason: Optional[str] = None,
        rejection_reason: Optional[str] = None,
    ) -> ApprovalTicket:
        """Rejects a pending or approved ticket."""
        now = datetime.utcnow()
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                raise PermissionDeniedError(f"Approval ticket '{approval_id}' not found.")

            if approval_id in self._consumed_ticket_ids:
                raise PermissionDeniedError(f"Approval ticket '{approval_id}' has already been consumed.")

            effective_reason = reason or rejection_reason or "rejected"
            self._rejected_ticket_ids.add(approval_id)
            rejected_ticket = dataclasses.replace(
                ticket,
                approved_by_user=False,
                user_id=user_id,
                rejection_reason=effective_reason,
                decided_at=now,
                decision=ApprovalDecision.REJECTED,
            )
            self._tickets[approval_id] = rejected_ticket
            return rejected_ticket

    def invalidate_ticket(
        self, approval_id: str, reason: str = "invalidated"
    ) -> Optional[ApprovalTicket]:
        """Invalidates an active ticket."""
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                return None

            invalidated = dataclasses.replace(
                ticket,
                approved_by_user=False,
                decision=ApprovalDecision.INVALIDATED,
                rejection_reason=reason,
            )
            self._tickets[approval_id] = invalidated
            return invalidated

    def invalidate_operation_tickets(
        self, operation_id: str, reason: str = "operation_state_changed"
    ) -> int:
        """Invalidates all pending or approved tickets for an operation."""
        count = 0
        with self._lock:
            for approval_id, ticket in list(self._tickets.items()):
                if ticket.operation_id == operation_id and ticket.decision in {
                    ApprovalDecision.PENDING,
                    ApprovalDecision.APPROVED,
                }:
                    self._tickets[approval_id] = dataclasses.replace(
                        ticket,
                        approved_by_user=False,
                        decision=ApprovalDecision.INVALIDATED,
                        rejection_reason=reason,
                    )
                    count += 1
        return count

    def consume_approval(self, approval_id: str) -> ApprovalTicket:
        """Consumes an approved ticket for execution, guaranteeing single-use semantics."""
        now = datetime.utcnow()
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                raise PermissionDeniedError(f"Approval ticket '{approval_id}' not found.")

            if approval_id in self._consumed_ticket_ids or ticket.is_consumed:
                raise PermissionDeniedError(
                    f"Approval ticket '{approval_id}' has already been consumed."
                )

            if approval_id in self._rejected_ticket_ids or ticket.decision == ApprovalDecision.REJECTED:
                raise PermissionDeniedError(
                    f"Approval ticket '{approval_id}' is rejected or not approved."
                )

            if ticket.expires_at is not None and now >= ticket.expires_at:
                self._tickets[approval_id] = dataclasses.replace(
                    ticket, decision=ApprovalDecision.EXPIRED
                )
                raise PermissionDeniedError("Approval ticket has expired.")

            if not ticket.approved_by_user:
                raise PermissionDeniedError(
                    f"Approval ticket '{approval_id}' is not approved by user. Explicit human consent required."
                )

            self._consumed_ticket_ids.add(approval_id)
            consumed_ticket = dataclasses.replace(
                ticket,
                is_consumed=True,
                decision=ApprovalDecision.INVALIDATED,
                rejection_reason="Consumed successfully for execution",
            )
            self._tickets[approval_id] = consumed_ticket
            return ticket

    def validate_and_consume(
        self,
        approval_id: str,
        operation_id: str,
        repository_id: str,
        operation_kind: OperationKind,
        current_base_sha: str,
        current_diff_sha256: str,
        target_branch: str,
        source_branch: Optional[str] = None,
    ) -> ApprovalTicket:
        """Atomically validates the ticket against live execution state and consumes it."""
        ticket = self.consume_approval(approval_id)

        if ticket.operation_id != operation_id:
            raise ApprovalInvalidError(
                f"Ticket operation ID mismatch. Expected '{operation_id}', got '{ticket.operation_id}'."
            )

        if ticket.repository_id != repository_id:
            raise ApprovalInvalidError(
                f"Ticket repository ID mismatch. Expected '{repository_id}', got '{ticket.repository_id}'."
            )

        if current_base_sha != ticket.base_sha:
            raise RemoteStateChangedError(
                message=(
                    f"Remote branch state has moved since approval was granted. "
                    f"Approved base SHA: {ticket.base_sha[:8]}, current remote SHA: {current_base_sha[:8]}."
                ),
                expected_sha=ticket.base_sha,
                actual_sha=current_base_sha,
            )

        if current_diff_sha256 != ticket.diff_sha256:
            raise ApprovalInvalidError(
                "Local diff content was modified after user approval was granted. "
                "A fresh review and approval are mandatory."
            )

        return ticket

    def clear(self) -> None:
        """Clears all stored tickets (isolated test teardown helper)."""
        with self._lock:
            self._tickets.clear()
            self._consumed_ticket_ids.clear()
            self._rejected_ticket_ids.clear()


# Canonical alias ensuring backwards-compatible imports across subsystem packages
ApprovalManager = ApprovalGate

_DEFAULT_APPROVAL_GATE: Optional[ApprovalGate] = None
_APPROVAL_LOCK = threading.Lock()


def get_default_approval_gate() -> ApprovalGate:
    """Returns the process-wide default ApprovalGate singleton."""
    global _DEFAULT_APPROVAL_GATE
    if _DEFAULT_APPROVAL_GATE is None:
        with _APPROVAL_LOCK:
            if _DEFAULT_APPROVAL_GATE is None:
                _DEFAULT_APPROVAL_GATE = ApprovalGate()
    return _DEFAULT_APPROVAL_GATE


get_default_approval_manager = get_default_approval_gate


def set_default_approval_gate(gate: ApprovalGate) -> None:
    """Explicitly injects an ApprovalGate instance (used during testing)."""
    global _DEFAULT_APPROVAL_GATE
    with _APPROVAL_LOCK:
        _DEFAULT_APPROVAL_GATE = gate


set_default_approval_manager = set_default_approval_gate


def reset_default_approval_gate() -> None:
    """Resets the process-wide default ApprovalGate (used during testing)."""
    global _DEFAULT_APPROVAL_GATE
    with _APPROVAL_LOCK:
        if _DEFAULT_APPROVAL_GATE is not None:
            _DEFAULT_APPROVAL_GATE.clear()
        _DEFAULT_APPROVAL_GATE = None


reset_default_approval_manager = reset_default_approval_gate

__all__ = [
    "compute_diff_sha256",
    "ApprovalTicket",
    "ApprovalGate",
    "ApprovalManager",
    "get_default_approval_gate",
    "get_default_approval_manager",
    "set_default_approval_gate",
    "set_default_approval_manager",
    "reset_default_approval_gate",
    "reset_default_approval_manager",
]
