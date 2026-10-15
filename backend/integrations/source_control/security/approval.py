from __future__ import annotations

import dataclasses
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional, Sequence

from ..domain.enums import ApprovalDecision, OperationKind
from ..domain.models import ApprovalTicket
from ..errors import (
    ApprovalInvalidError,
    ApprovalRequiredError,
    RemoteStateChangedError,
)


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


class ApprovalManager:
    """Thread-safe manager enforcing human approval lifecycles and cryptographic diff validation."""

    def __init__(self, default_ttl_minutes: int = 60) -> None:
        self.default_ttl_minutes = default_ttl_minutes
        self._tickets: dict[str, ApprovalTicket] = {}
        self._consumed_ticket_ids: set[str] = set()
        self._lock = threading.Lock()

    def create_ticket(
        self,
        operation_id: str,
        repository_id: str,
        operation_kind: OperationKind,
        base_sha: str,
        diff_sha256: str,
        source_branch: str,
        target_branch: str,
        commit_message: str = "",
        change_request_title: str = "",
        change_request_body: str = "",
        ttl_minutes: Optional[int] = None,
    ) -> ApprovalTicket:
        """Generates a cryptographically unguessable, single-use pending approval ticket."""
        approval_id = f"appr_{secrets.token_hex(16)}"
        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl_minutes
        now = datetime.utcnow()
        expires_at = now + timedelta(minutes=ttl)

        ticket = ApprovalTicket(
            approval_id=approval_id,
            operation_id=operation_id,
            repository_id=repository_id,
            operation_kind=operation_kind,
            base_sha=base_sha,
            diff_sha256=diff_sha256,
            source_branch=source_branch,
            target_branch=target_branch,
            commit_message=commit_message,
            change_request_title=change_request_title,
            change_request_body=change_request_body,
            decision=ApprovalDecision.PENDING,
            created_at=now,
            expires_at=expires_at,
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
        self, approval_id: str, approved_by: str = "user"
    ) -> ApprovalTicket:
        """Approves a pending ticket.

        Raises ApprovalInvalidError if ticket is missing, consumed, or expired.
        """
        now = datetime.utcnow()
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                raise ApprovalInvalidError(f"Approval ticket '{approval_id}' not found.")

            if approval_id in self._consumed_ticket_ids:
                raise ApprovalInvalidError(f"Approval ticket '{approval_id}' has already been consumed.")

            if ticket.decision != ApprovalDecision.PENDING:
                raise ApprovalInvalidError(
                    f"Cannot approve ticket in state '{ticket.decision.value}'."
                )

            if ticket.expires_at is not None and now >= ticket.expires_at:
                expired_ticket = dataclasses.replace(
                    ticket, decision=ApprovalDecision.EXPIRED
                )
                self._tickets[approval_id] = expired_ticket
                raise ApprovalInvalidError("Approval ticket has expired.")

            approved_ticket = dataclasses.replace(
                ticket,
                decision=ApprovalDecision.APPROVED,
                decided_at=now,
                approved_by=approved_by,
            )
            self._tickets[approval_id] = approved_ticket
            return approved_ticket

    def reject_ticket(
        self, approval_id: str, rejection_reason: Optional[str] = None
    ) -> ApprovalTicket:
        """Rejects a pending or approved ticket."""
        now = datetime.utcnow()
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                raise ApprovalInvalidError(f"Approval ticket '{approval_id}' not found.")

            if approval_id in self._consumed_ticket_ids:
                raise ApprovalInvalidError(f"Approval ticket '{approval_id}' has already been consumed.")

            rejected_ticket = dataclasses.replace(
                ticket,
                decision=ApprovalDecision.REJECTED,
                decided_at=now,
                rejection_reason=rejection_reason,
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
                        decision=ApprovalDecision.INVALIDATED,
                        rejection_reason=reason,
                    )
                    count += 1
        return count

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
        """Atomically validates the ticket against live execution state and consumes it.

        Raises ApprovalRequiredError, RemoteStateChangedError, or ApprovalInvalidError
        on any mismatch.
        """
        now = datetime.utcnow()
        with self._lock:
            ticket = self._tickets.get(approval_id)
            if ticket is None:
                raise ApprovalRequiredError(
                    f"Approval ticket '{approval_id}' does not exist.",
                    operation_id=operation_id,
                )

            if approval_id in self._consumed_ticket_ids:
                raise ApprovalInvalidError(
                    f"Approval ticket '{approval_id}' has already been consumed and cannot be reused."
                )

            if ticket.decision == ApprovalDecision.PENDING:
                raise ApprovalRequiredError(
                    "Operation has not yet received explicit user approval.",
                    operation_id=operation_id,
                )

            if ticket.decision != ApprovalDecision.APPROVED:
                raise ApprovalInvalidError(
                    f"Approval ticket is in state '{ticket.decision.value}'. Operation denied."
                )

            if ticket.expires_at is not None and now >= ticket.expires_at:
                self._tickets[approval_id] = dataclasses.replace(
                    ticket, decision=ApprovalDecision.EXPIRED
                )
                raise ApprovalInvalidError("Approval ticket has expired. Re-review required.")

            if ticket.operation_id != operation_id:
                raise ApprovalInvalidError(
                    f"Ticket operation ID mismatch. Expected '{operation_id}', got '{ticket.operation_id}'."
                )

            if ticket.repository_id != repository_id:
                raise ApprovalInvalidError(
                    f"Ticket repository ID mismatch. Expected '{repository_id}', got '{ticket.repository_id}'."
                )

            if ticket.operation_kind != operation_kind:
                raise ApprovalInvalidError(
                    f"Ticket operation kind mismatch. Expected '{operation_kind.value}', got '{ticket.operation_kind.value}'."
                )

            if ticket.target_branch != target_branch:
                raise ApprovalInvalidError(
                    f"Target branch mismatch. Approved for '{ticket.target_branch}', but targeting '{target_branch}'."
                )

            if source_branch is not None and ticket.source_branch != source_branch:
                raise ApprovalInvalidError(
                    f"Source branch mismatch. Approved for '{ticket.source_branch}', but targeting '{source_branch}'."
                )

            if current_base_sha != ticket.base_sha:
                # Mark ticket invalidated due to remote drift
                self._tickets[approval_id] = dataclasses.replace(
                    ticket,
                    decision=ApprovalDecision.INVALIDATED,
                    rejection_reason="Remote base SHA changed",
                )
                raise RemoteStateChangedError(
                    message=(
                        f"Remote branch state has moved since approval was granted. "
                        f"Approved base SHA: {ticket.base_sha[:8]}, current remote SHA: {current_base_sha[:8]}."
                    ),
                    expected_sha=ticket.base_sha,
                    actual_sha=current_base_sha,
                )

            if current_diff_sha256 != ticket.diff_sha256:
                # Mark ticket invalidated due to content modification
                self._tickets[approval_id] = dataclasses.replace(
                    ticket,
                    decision=ApprovalDecision.INVALIDATED,
                    rejection_reason="Local diff fingerprint changed",
                )
                raise ApprovalInvalidError(
                    "Local diff content was modified after user approval was granted. "
                    "A fresh review and approval are mandatory."
                )

            # Consume ticket atomically to prevent replay attacks
            self._consumed_ticket_ids.add(approval_id)
            consumed_ticket = dataclasses.replace(
                ticket,
                decision=ApprovalDecision.INVALIDATED,
                rejection_reason="Consumed successfully for execution",
            )
            self._tickets[approval_id] = consumed_ticket
            return ticket

    def clear(self) -> None:
        """Clears all stored tickets (isolated test teardown helper)."""
        with self._lock:
            self._tickets.clear()
            self._consumed_ticket_ids.clear()


# ============================================================
# ApprovalManager Singleton Accessors
# ============================================================

_DEFAULT_APPROVAL_MANAGER: Optional[ApprovalManager] = None
_APPROVAL_LOCK = threading.Lock()


def get_default_approval_manager() -> ApprovalManager:
    """Returns the process-wide default ApprovalManager singleton."""
    global _DEFAULT_APPROVAL_MANAGER
    if _DEFAULT_APPROVAL_MANAGER is None:
        with _APPROVAL_LOCK:
            if _DEFAULT_APPROVAL_MANAGER is None:
                _DEFAULT_APPROVAL_MANAGER = ApprovalManager()
    return _DEFAULT_APPROVAL_MANAGER


def set_default_approval_manager(manager: ApprovalManager) -> None:
    """Explicitly injects an ApprovalManager instance (used during testing)."""
    global _DEFAULT_APPROVAL_MANAGER
    with _APPROVAL_LOCK:
        _DEFAULT_APPROVAL_MANAGER = manager


def reset_default_approval_manager() -> None:
    """Resets the process-wide default ApprovalManager (used during testing)."""
    global _DEFAULT_APPROVAL_MANAGER
    with _APPROVAL_LOCK:
        if _DEFAULT_APPROVAL_MANAGER is not None:
            _DEFAULT_APPROVAL_MANAGER.clear()
        _DEFAULT_APPROVAL_MANAGER = None
