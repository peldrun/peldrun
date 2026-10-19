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

State machine (single source of truth for `_transition`):

    PENDING   -> GRANTED | REJECTED | REVOKED | EXPIRED
    GRANTED   -> CONSUMED | REJECTED | REVOKED | EXPIRED
    REJECTED  -> REJECTED | REVOKED          (terminal, cleanup only)
    EXPIRED   -> EXPIRED  | REJECTED | REVOKED (terminal, cleanup only)
    REVOKED   -> REVOKED                     (terminal)
    CONSUMED  -> CONSUMED                    (terminal)

Any transition not listed above raises `ApprovalInvalidError` (or
`ApprovalExpiredError` / `ApprovalConsumedError` where those specific
errors are more informative).

The `ApprovalTicket` model is defined in `domain.models` and is the
single source of truth. This module MUST NOT redefine it.

CONSUMPTION PATH (single, mandatory):
    `consume(approval_id, expected_*)` — the ONLY way to consume a
    ticket. Every bound field is re-validated against the caller's
    expectation before the ticket is marked consumed. Any mismatch
    raises `ApprovalInvalidError`.

REMOVED PATH (was a security hole):
    `consume_approval(approval_id)` — previously marked the ticket
    as consumed without comparing bound fields, which allowed an
    approved diff to be swapped for a different diff at execution
    time. It now raises `NotImplementedError`. Any caller still
    using it MUST migrate to `consume`.

DEPRECATED ALIASES (still functional, will be removed):
    - `approve_ticket(...)` — alias for `grant(...)`. Emits
      `DeprecationWarning`.
    - `reject_ticket(...)` — alias for `reject(...)`. Emits
      `DeprecationWarning`.

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

import hashlib
import json
import logging
import re
import secrets
import threading
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
    "ApprovalManager",
    "ApprovalTicket",
    "ApprovalValidationError",
    "compute_diff_sha256",
    "get_default_approval_gate",
    "set_default_approval_gate",
    "reset_default_approval_gate",
    "get_default_approval_manager",
    "set_default_approval_manager",
    "reset_default_approval_manager",
]


logger = logging.getLogger(__name__)


#: Default TTL for a new ticket if no policy is provided.
DEFAULT_APPROVAL_TTL_SECONDS: int = 15 * 60  # 15 minutes

#: Default maximum number of tickets retained in memory.
DEFAULT_MAX_TICKETS: int = 10_000

#: Pattern for a valid SHA-256 hex digest (64 lowercase hex chars).
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

#: Pattern for a valid SHA-1 hex digest (40 lowercase hex chars).
#:
#: Git still produces SHA-1 object IDs on every supported version
#: (and will continue to for existing repositories even after SHA-256
#: rollout). The `base_sha` field of an approval ticket records the
#: commit the diff was computed against, so accepting SHA-1 here is
#: required for the ticket to bind to a real commit. The previous
#: implementation accepted SHA-256 only, which made every ticket
#: creation fail with a `ValidationError` whenever the workspace
#: carried a normal Git SHA-1. The diff hash itself is validated
#: separately by `_validate_diff_sha256` and remains SHA-256-only.
_SHA1_PATTERN = re.compile(r"^[a-f0-9]{40}$")

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
# Diff hash computation
# ---------------------------------------------------------------------------


def compute_diff_sha256(diff: Any) -> str:
    """
    Compute the canonical SHA-256 of a diff.

    Accepts either:

        - An object with a callable `sha256()` method (for example
          `domain.models.DiffSummary`). The method's return value is
          used directly, after validation.
        - A mapping of diff fields. The mapping is canonicalized
          (sorted keys, compact separators) and hashed.
        - A string. The string is encoded as UTF-8 and hashed; this
          is provided for callers that already have a canonical
          textual form.

    The function never raises for well-formed inputs. For unexpected
    input types, a `ValidationError` is raised with a clear message
    so that programming errors are caught early rather than producing
    a misleading hash.

    Args:
        diff: The diff to hash. See above for accepted shapes.

    Returns:
        A lowercase 64-character hex digest.
    """
    # Object with a native `sha256()` method.
    if hasattr(diff, "sha256") and callable(getattr(diff, "sha256")):
        try:
            digest = diff.sha256()
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(
                "Diff.sha256() raised an exception.",
                details={"reason": type(exc).__name__},
                cause=exc,
            ) from exc
        if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
            raise ValidationError(
                "Diff.sha256() returned an invalid digest.",
                details={
                    "length": len(digest) if isinstance(digest, str) else None
                },
            )
        return digest

    # String: assume it is already canonical.
    if isinstance(diff, str):
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()

    # Bytes: hash directly.
    if isinstance(diff, (bytes, bytearray)):
        return hashlib.sha256(bytes(diff)).hexdigest()

    # Mapping: canonicalize + hash.
    if isinstance(diff, Mapping):
        try:
            canonical = json.dumps(
                dict(diff),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "Diff mapping could not be serialized.",
                details={"reason": type(exc).__name__},
                cause=exc,
            ) from exc
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    raise ValidationError(
        "Unsupported diff type for hashing.",
        details={"type": type(diff).__name__},
    )


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
        self._audit = (
            audit_logger
            if audit_logger is not None
            else get_default_audit_logger()
        )
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
              SHA-1 hex digest (40 chars, the normal Git object ID),
              a SHA-256 hex digest (64 chars, forward compatibility),
              or one of `{"HEAD", "UNKNOWN", "UNBORN"}`.
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
                When provided and already in use, the call fails with
                `ApprovalInvalidError` rather than silently
                overwriting the existing ticket.

        Returns:
            A new `ApprovalTicket` in `PENDING` status.

        Raises:
            ValidationError: If any input is invalid.
            ApprovalInvalidError: If an explicit `approval_id` is
                already in use.
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
            # Reject duplicate explicit IDs rather than silently
            # overwriting an existing ticket. Random IDs generated
            # internally have negligible collision probability, but
            # the check is unconditional for defense in depth.
            if ticket.approval_id in self._tickets:
                raise ApprovalInvalidError(
                    f"Approval ticket '{ticket.approval_id}' already exists.",
                    details={"approval_id": ticket.approval_id},
                )
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
            ApprovalInvalidError: If the ticket does not exist, was
                already rejected, or was already revoked.
            ApprovalExpiredError: If the ticket has expired (either
                by time or by prior status).
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
        The only further transition permitted is to `REVOKED` for
        cleanup purposes.
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

    # --- Deprecated facade-facing aliases ------------------------------
    #
    # These aliases keep older callers working but emit a
    # DeprecationWarning. They are safe (they delegate to the
    # canonical methods) but should be migrated away from in new
    # code. They will be removed in a future release.

    def approve_ticket(
        self,
        approval_id: str,
        *,
        user_id: str = "user",
        notes: Optional[str] = None,
    ) -> ApprovalTicket:
        """
        DEPRECATED: use `grant()` instead.

        Alias for `grant`. The `user_id` argument is mapped to the
        `actor` field of the audit log; `notes` is recorded on the
        audit event.
        """
        warnings.warn(
            "ApprovalGate.approve_ticket() is deprecated; use "
            "ApprovalGate.grant() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if notes:
            # Reject notes that embed an obvious secret; they will be
            # redacted anyway, but a hard failure here surfaces a
            # caller bug earlier.
            from .redaction import contains_secret

            if contains_secret(notes):
                raise ValidationError(
                    "Approval notes appear to contain a secret.",
                    details={"approval_id": approval_id},
                )
        return self.grant(approval_id, actor=user_id or "user")

    def reject_ticket(
        self,
        approval_id: str,
        *,
        user_id: str = "user",
        reason: Optional[str] = None,
    ) -> ApprovalTicket:
        """
        DEPRECATED: use `reject()` instead.

        Alias for `reject`. The `user_id` argument is mapped to the
        `actor` field of the audit log.
        """
        warnings.warn(
            "ApprovalGate.reject_ticket() is deprecated; use "
            "ApprovalGate.reject() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.reject(
            approval_id,
            actor=user_id or "user",
            reason=reason,
        )

    def consume_approval(self, approval_id: str) -> ApprovalTicket:
        """
        DISABLED: legacy consumption path.

        This method previously marked a ticket as consumed after
        verifying only that it was consumable (granted, not expired,
        not already used). It did NOT compare the ticket's bound
        fields against the caller's expected values, which meant an
        attacker or buggy caller could approve one diff and execute a
        different one.

        The method is retained as a stub that raises
        `NotImplementedError` with a clear migration message. Every
        caller MUST migrate to:

            ApprovalGate.consume(
                approval_id,
                expected_operation_id=...,
                expected_repository_id=...,
                expected_operation=...,
                expected_base_sha=...,
                expected_diff_sha256=...,
                expected_target_branch=...,
            )

        Raises:
            NotImplementedError: Always. This method is disabled.
        """
        self._validate_non_empty(approval_id, "approval_id")
        raise NotImplementedError(
            "ApprovalGate.consume_approval() has been disabled: it did "
            "not validate the ticket's bound fields. Use "
            "ApprovalGate.consume(approval_id, expected_operation_id=..., "
            "expected_repository_id=..., expected_operation=..., "
            "expected_base_sha=..., expected_diff_sha256=..., "
            "expected_target_branch=...) instead."
        )

    # --- Canonical consumption -----------------------------------------

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
        CANONICAL: consume a ticket, marking it as used.

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
            ApprovalInvalidError: If the ticket does not exist, or if
                any bound field does not match.
            ApprovalExpiredError: If the ticket has expired.
            ApprovalConsumedError: If the ticket was already consumed.
            ApprovalRequiredError: If the ticket has not been granted.
        """
        # Validate inputs first (fail fast on caller bugs).
        self._validate_non_empty(
            expected_operation_id, "expected_operation_id"
        )
        self._validate_non_empty(
            expected_repository_id, "expected_repository_id"
        )
        self._validate_non_empty(expected_operation, "expected_operation")
        self._validate_non_empty(
            expected_target_branch, "expected_target_branch"
        )
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
        self._validate_non_empty(
            expected_operation_id, "expected_operation_id"
        )
        self._validate_non_empty(
            expected_repository_id, "expected_repository_id"
        )
        self._validate_non_empty(expected_operation, "expected_operation")
        self._validate_non_empty(
            expected_target_branch, "expected_target_branch"
        )
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

        The state machine is exhaustive and monotonic; any
        transition not listed below is refused:

            PENDING   -> GRANTED | REJECTED | REVOKED | EXPIRED
            GRANTED   -> CONSUMED | REJECTED | REVOKED | EXPIRED
            REJECTED  -> REJECTED | REVOKED          (cleanup only)
            EXPIRED   -> EXPIRED  | REJECTED | REVOKED (cleanup only)
            REVOKED   -> REVOKED                     (idempotent)
            CONSUMED  -> (terminal, no transition)

        In addition, the following time-based rule is enforced:
        a ticket whose ``expires_at`` has passed cannot be granted,
        even if its status is still PENDING or GRANTED. Such a grant
        attempt flips the status to EXPIRED before raising
        ``ApprovalExpiredError``.
        """
        with self._lock:
            ticket = self._get_ticket_locked(approval_id)

            # --- Terminal state: CONSUMED ---------------------------
            if ticket.status == ApprovalStatus.CONSUMED:
                raise ApprovalConsumedError(
                    "Cannot modify a consumed approval ticket.",
                    details={"approval_id": approval_id},
                )

            # --- Terminal state: REVOKED ----------------------------
            if ticket.status == ApprovalStatus.REVOKED:
                if target_status != ApprovalStatus.REVOKED:
                    raise ApprovalInvalidError(
                        "Cannot modify a revoked approval ticket.",
                        details={
                            "approval_id": approval_id,
                            "requested_status": target_status.value,
                        },
                    )

            # --- Terminal state: REJECTED ---------------------------
            # A rejected ticket cannot be re-granted or re-rejected
            # in a meaningful way; only cleanup to REVOKED is
            # permitted.
            if ticket.status == ApprovalStatus.REJECTED:
                if target_status not in (
                    ApprovalStatus.REJECTED,
                    ApprovalStatus.REVOKED,
                ):
                    raise ApprovalInvalidError(
                        "Cannot modify a rejected approval ticket.",
                        details={
                            "approval_id": approval_id,
                            "requested_status": target_status.value,
                        },
                    )

            # --- Time-based expiry, and status=EXPIRED --------------
            # A ticket can be EXPIRED either because its wall-clock
            # expires_at has passed, or because a previous grant
            # attempt already flipped its status. Both cases must
            # block further grants.
            if (
                (
                    ticket.status == ApprovalStatus.EXPIRED
                    or ticket.expires_at <= _utc_now()
                )
                and target_status == ApprovalStatus.GRANTED
            ):
                if ticket.status != ApprovalStatus.EXPIRED:
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
        """
        Validate a `base_sha` value.

        Accepted shapes:

            - A 40-character lowercase hex SHA-1 (the object ID that
              `git rev-parse HEAD` actually produces; the value is
              passed verbatim to `git diff`, `git push`, and any
              other Git operation that consumes it).
            - A 64-character lowercase hex SHA-256 (accepted so that
              callers migrating to a future SHA-256 object format
              can still create tickets; the value is stored as-is).
            - One of the allowlisted placeholders (`HEAD`,
              `UNKNOWN`, `UNBORN`) used by tests and by operations
              that do not yet have a concrete commit SHA.

        The diff hash is validated separately by
        `_validate_diff_sha256`, which is SHA-256-only by design:
        the diff hash is a content-addressed contract, not a Git
        object ID, so it must not accept SHA-1.
        """
        if not base_sha or not isinstance(base_sha, str):
            raise ValidationError(
                "base_sha must be a non-empty string.",
                details={"field": "base_sha"},
            )
        if base_sha in _BASE_SHA_ALLOWLIST:
            return
        if _SHA1_PATTERN.match(base_sha) or _SHA256_PATTERN.match(base_sha):
            return
        raise ValidationError(
            "base_sha must be a 40-character SHA-1 digest, a "
            "64-character SHA-256 digest, or one of the allowlisted "
            "placeholders.",
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
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
#
# The package `__init__` and the HTTP facade were written against the
# names below. They refer to the SAME class and the SAME singleton,
# respectively, so `isinstance` and identity checks continue to work.

#: Alias for `ApprovalGate`.
ApprovalManager = ApprovalGate


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


# Rebind the aliases now that the canonical functions exist.
get_default_approval_manager = get_default_approval_gate
set_default_approval_manager = set_default_approval_gate
reset_default_approval_manager = reset_default_approval_gate
