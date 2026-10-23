# backend/integrations/source_control/security/approval.py
"""
Approval gate for the Source Control Integration subsystem.

The approval gate is the ONLY component permitted to create, grant,
and consume approval tickets. It enforces the following invariants:

    1. An approval ticket is single-use per action scope (e.g. push_branch,
       create_change_request, or default). Once a scope is consumed, it
       cannot be reused for that scope.
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

CONSUMPTION PATHS:
    `consume(approval_id, expected_*, action_scope=...)` — Canonical way to
    consume a ticket with exhaustive field validation and scoped lifecycle.
    `consume_approval(approval_id, expected_diff_sha256=..., action_scope=...)` —
    Streamlined scoped consumption for facade and agent tools.

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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

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
_SHA1_PATTERN = re.compile(r"^[a-f0-9]{40}$")

#: Values that are accepted as a "base SHA" but are not real hashes.
_BASE_SHA_ALLOWLIST = frozenset({"HEAD", "UNKNOWN", "UNBORN"})

#: Operations that participate in the multi-step source control lifecycle
#: under the same cryptographic approval ticket.
_COMPATIBLE_LIFECYCLE_OPERATIONS = frozenset(
    {
        "push_branch",
        "create_change_request",
        "source_control",
        "push_and_pr",
        "request_approval",
    }
)


# Alias for callers that prefer a descriptive name.
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

    Normalizes line endings (CRLF -> LF) to guarantee deterministic hashes
    across Windows and POSIX platforms.

    Accepts:
        - An object with a callable `sha256()` method (e.g. DiffSummary).
        - A mapping of diff fields.
        - A string or bytes representation of a patch/diff.

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

    # String: normalize CRLF to LF for cross-platform fingerprint stability.
    if isinstance(diff, str):
        normalized = diff.replace("\r\n", "\n")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    # Bytes: normalize CRLF bytes to LF bytes.
    if isinstance(diff, (bytes, bytearray)):
        normalized_bytes = bytes(diff).replace(b"\r\n", b"\n")
        return hashlib.sha256(normalized_bytes).hexdigest()

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
        normalized_canonical = canonical.replace("\r\n", "\n")
        return hashlib.sha256(normalized_canonical.encode("utf-8")).hexdigest()

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

    Supports scoped consumption so that a single user-granted ticket
    can authorize both `push_branch` and `create_change_request` without
    requiring redundant approvals or permitting diff-swap attacks.
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
            default_ttl_seconds = DEFAULT_APPROVAL_TTL_SECONDS

        self._tickets: Dict[str, ApprovalTicket] = {}
        self._consumed_scopes: Set[str] = set()
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
        approval_scope: str = "push_branch",
    ) -> ApprovalTicket:
        """
        Create a new pending approval ticket.

        Args:
            approval_scope: What the user is being asked to approve.
                Two values are recognized:

                    - "push_branch" (default): a normal push to a
                      non-protected branch. The push flow will apply
                      the historical protection rules and may refuse
                      the push if the target turns out to be
                      protected.

                    - "push_to_protected": the user explicitly
                      consents to pushing to a protected branch. This
                      scope is granted by the application layer ONLY
                      when the user has configured
                      `allow_push_to_protected = "ask"` in the
                      settings, so the extra approval is explicit
                      rather than implicit.
        """
        self._validate_non_empty(operation_id, "operation_id")
        self._validate_non_empty(repository_id, "repository_id")
        self._validate_non_empty(operation, "operation")
        self._validate_non_empty(target_branch, "target_branch")
        self._validate_base_sha(base_sha)
        self._validate_diff_sha256(diff_sha256)
        if approval_scope not in ("push_branch", "push_to_protected"):
            raise ValidationError(
                f"Unknown approval_scope '{approval_scope}'. Must be "
                f"'push_branch' or 'push_to_protected'.",
                details={"approval_scope": approval_scope},
            )

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
            approval_scope=approval_scope,
        )

        with self._lock:
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
        """Mark a ticket as granted by the user."""
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
        """Mark a ticket as rejected by the user."""
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
        """Revoke a ticket."""
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

    def approve_ticket(
        self,
        approval_id: str,
        *,
        user_id: str = "user",
        notes: Optional[str] = None,
    ) -> ApprovalTicket:
        """DEPRECATED: use `grant()` instead."""
        warnings.warn(
            "ApprovalGate.approve_ticket() is deprecated; use "
            "ApprovalGate.grant() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if notes:
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
        """DEPRECATED: use `reject()` instead."""
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

    # --- Scoped consumption --------------------------------------------

    def consume_approval(
        self,
        approval_id: str,
        *,
        expected_diff_sha256: Optional[str] = None,
        action_scope: str = "default",
        actor: str = "system",
    ) -> ApprovalTicket:
        """
        Consume an approval ticket for a specific action scope.

        Enforces scope isolation: allows the same approved ticket to be used
        for `push_branch` and subsequently for `create_change_request` without
        prematurely burning the ticket, while preventing double execution of
        the same action scope.
        """
        self._validate_non_empty(approval_id, "approval_id")
        self._validate_non_empty(action_scope, "action_scope")
        if expected_diff_sha256:
            self._validate_diff_sha256(expected_diff_sha256)

        with self._lock:
            ticket = self._get_ticket_locked(approval_id)
            self._assert_consumable_locked(ticket, action_scope=action_scope)

            if expected_diff_sha256 and ticket.diff_sha256 != expected_diff_sha256:
                raise ApprovalInvalidError(
                    "Workspace diff fingerprint changed after approval was granted.",
                    details={
                        "approval_id": approval_id,
                        "expected": expected_diff_sha256,
                        "actual": ticket.diff_sha256,
                    },
                )

            consumption_key = f"{approval_id}:{action_scope}"
            if consumption_key in self._consumed_scopes:
                raise ApprovalConsumedError(
                    f"Approval ticket '{approval_id}' already consumed for action '{action_scope}'.",
                    details={"approval_id": approval_id, "action_scope": action_scope},
                )
            self._consumed_scopes.add(consumption_key)

            is_terminal = (
                action_scope == "default"
                or action_scope == "create_change_request"
            )
            consumed = self._replace_ticket(
                ticket,
                status=ApprovalStatus.CONSUMED if is_terminal else ticket.status,
                consumed_at=ticket.consumed_at or _utc_now(),
            )
            self._tickets[approval_id] = consumed

        self._audit.record(
            AuditEventType.APPROVAL_CONSUME,
            severity=AuditSeverity.INFO,
            action="consume_approval",
            message=f"Approval consumed for scope '{action_scope}'.",
            payload={
                "approval_id": approval_id,
                "action_scope": action_scope,
                "operation": ticket.operation,
                "diff_sha256": ticket.diff_sha256,
            },
            operation_id=ticket.operation_id,
            repository_id=ticket.repository_id,
            approval_id=approval_id,
            actor=actor,
        )

        return consumed

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
        action_scope: str = "default",
    ) -> ApprovalTicket:
        """
        CANONICAL: consume a ticket for a given action scope.

        Every bound field on the ticket MUST match the expected
        values. If `action_scope` is provided, consumption is scoped
        so multiple coordinated lifecycle steps can be authorized
        by a single ticket without allowing diff tampering.
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
        self._validate_non_empty(action_scope, "action_scope")
        self._validate_diff_sha256(expected_diff_sha256)
        self._validate_base_sha(expected_base_sha)

        with self._lock:
            ticket = self._get_ticket_locked(approval_id)
            self._assert_consumable_locked(ticket, action_scope=action_scope)

            self._assert_matches(
                ticket,
                expected_operation_id=expected_operation_id,
                expected_repository_id=expected_repository_id,
                expected_operation=expected_operation,
                expected_base_sha=expected_base_sha,
                expected_diff_sha256=expected_diff_sha256,
                expected_target_branch=expected_target_branch,
            )

            consumption_key = f"{approval_id}:{action_scope}"
            if consumption_key in self._consumed_scopes:
                raise ApprovalConsumedError(
                    f"Approval ticket '{approval_id}' already consumed for action '{action_scope}'.",
                    details={
                        "approval_id": approval_id,
                        "action_scope": action_scope,
                    },
                )
            self._consumed_scopes.add(consumption_key)

            is_terminal = (
                action_scope == "default"
                or action_scope == "create_change_request"
            )
            consumed = self._replace_ticket(
                ticket,
                status=ApprovalStatus.CONSUMED if is_terminal else ticket.status,
                consumed_at=ticket.consumed_at or _utc_now(),
            )
            self._tickets[approval_id] = consumed

        self._audit.record(
            AuditEventType.APPROVAL_CONSUME,
            severity=AuditSeverity.INFO,
            action="consume",
            message=f"Approval consumed by the operation for scope '{action_scope}'.",
            payload={
                "approval_id": approval_id,
                "action_scope": action_scope,
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
        action_scope: str = "default",
    ) -> None:
        """Validate that a ticket would be consumable for the requested scope."""
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
        self._validate_non_empty(action_scope, "action_scope")
        self._validate_diff_sha256(expected_diff_sha256)
        self._validate_base_sha(expected_base_sha)

        with self._lock:
            ticket = self._get_ticket_locked(approval_id)
            self._assert_consumable_locked(ticket, action_scope=action_scope)
            self._assert_matches(
                ticket,
                expected_operation_id=expected_operation_id,
                expected_repository_id=expected_repository_id,
                expected_operation=expected_operation,
                expected_base_sha=expected_base_sha,
                expected_diff_sha256=expected_diff_sha256,
                expected_target_branch=expected_target_branch,
            )

    # --- Scoped Inspection Queries -------------------------------------

    def is_scope_consumed(self, approval_id: str, action_scope: str) -> bool:
        """Return True if the ticket has been consumed for the given action scope."""
        with self._lock:
            return f"{approval_id}:{action_scope}" in self._consumed_scopes

    def get_consumed_scopes(self, approval_id: str) -> Sequence[str]:
        """Return all action scopes consumed for the given ticket."""
        prefix = f"{approval_id}:"
        with self._lock:
            return tuple(
                k[len(prefix):]
                for k in self._consumed_scopes
                if k.startswith(prefix)
            )

    # --- Lookup ---------------------------------------------------------

    def get_ticket(self, approval_id: str) -> Optional[ApprovalTicket]:
        """Return the ticket, or `None` if unknown."""
        if not approval_id:
            return None
        with self._lock:
            return self._tickets.get(approval_id)

    def register_ticket(self, ticket: ApprovalTicket) -> None:
        """Register or restore an existing approval ticket into the gate."""
        if not ticket or not isinstance(ticket, ApprovalTicket):
            return
        with self._lock:
            self._tickets[ticket.approval_id] = ticket

    def restore_ticket(
        self,
        *,
        approval_id: str,
        operation_id: str,
        repository_id: str,
        operation: str,
        base_sha: str,
        diff_sha256: str,
        target_branch: str,
        status: Optional[ApprovalStatus] = None,
        approved_by_user: bool = False,
        expires_at: Optional[datetime] = None,
        created_at: Optional[datetime] = None,
        approval_scope: str = "push_branch",
    ) -> ApprovalTicket:
        """
        Restore an approval ticket from persistent operation storage.
        Enables state reconstruction across application restarts without
        invalidating granted tickets.

        The `approval_scope` argument defaults to "push_branch" when
        absent. This is the correct default for older builds that did
        not yet support "push_to_protected": a restored ticket whose
        scope was lost during the upgrade will still be usable for
        normal pushes, but the user will be re-prompted to approve if
        the target turns out to be protected. That is the safe
        behavior — it never silently grants a protected-branch push
        on the basis of a missing field.
        """
        now = _utc_now()
        resolved_status = status or (
            ApprovalStatus.GRANTED if approved_by_user else ApprovalStatus.PENDING
        )
        if approval_scope not in ("push_branch", "push_to_protected"):
            logger.warning(
                "restore_ticket: unknown approval_scope '%s'; "
                "falling back to 'push_branch'.",
                approval_scope,
            )
            approval_scope = "push_branch"
        ticket = ApprovalTicket(
            approval_id=approval_id,
            operation_id=operation_id,
            repository_id=repository_id,
            operation=operation,
            base_sha=base_sha,
            diff_sha256=diff_sha256,
            target_branch=target_branch,
            expires_at=expires_at or (now + timedelta(seconds=self._default_ttl_seconds)),
            approved_by_user=approved_by_user,
            status=resolved_status,
            created_at=created_at or now,
            consumed_at=None,
            approval_scope=approval_scope,
        )
        with self._lock:
            self._tickets[ticket.approval_id] = ticket
        return ticket

    def list_pending(
        self, *, operation_id: Optional[str] = None
    ) -> Sequence[ApprovalTicket]:
        """Return all pending tickets, optionally filtered by operation."""
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
        Remove stale tickets and clean up their consumed scopes.
        Returns the number of tickets removed.
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

            if stale_ids:
                stale_prefixes = tuple(f"{sid}:" for sid in stale_ids)
                self._consumed_scopes = {
                    k for k in self._consumed_scopes
                    if not k.startswith(stale_prefixes)
                }

            return len(stale_ids)

    def clear(self) -> None:
        """Remove every ticket and scope record. Intended for tests."""
        with self._lock:
            self._tickets.clear()
            self._consumed_scopes.clear()

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

    def _assert_consumable_locked(
        self,
        ticket: ApprovalTicket,
        action_scope: str = "default",
    ) -> None:
        """Raise the appropriate error if the ticket cannot be consumed."""
        now = _utc_now()

        consumption_key = f"{ticket.approval_id}:{action_scope}"
        if consumption_key in self._consumed_scopes:
            raise ApprovalConsumedError(
                f"Approval ticket '{ticket.approval_id}' already consumed for action '{action_scope}'.",
                details={
                    "approval_id": ticket.approval_id,
                    "action_scope": action_scope,
                },
            )

        if f"{ticket.approval_id}:default" in self._consumed_scopes:
            raise ApprovalConsumedError(
                "Approval ticket has already been consumed.",
                details={"approval_id": ticket.approval_id},
            )

        if action_scope == "default" and any(
            k.startswith(f"{ticket.approval_id}:")
            for k in self._consumed_scopes
        ):
            raise ApprovalConsumedError(
                "Approval ticket has already been partially consumed by scoped actions.",
                details={"approval_id": ticket.approval_id},
            )

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
        """Raise `ApprovalInvalidError` when any bound field differs."""
        mismatches: dict[str, tuple[str, str]] = {}

        def _check(name: str, actual: str, expected: str) -> None:
            if actual != expected:
                mismatches[name] = (actual, expected)

        _check("operation_id", ticket.operation_id, expected_operation_id)
        _check("repository_id", ticket.repository_id, expected_repository_id)

        if ticket.operation != expected_operation:
            if not (
                ticket.operation in _COMPATIBLE_LIFECYCLE_OPERATIONS
                and expected_operation in _COMPATIBLE_LIFECYCLE_OPERATIONS
            ):
                mismatches["operation"] = (ticket.operation, expected_operation)

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
        """Perform a state transition on a ticket."""
        with self._lock:
            ticket = self._get_ticket_locked(approval_id)

            if ticket.status == ApprovalStatus.CONSUMED:
                raise ApprovalConsumedError(
                    "Cannot modify a consumed approval ticket.",
                    details={"approval_id": approval_id},
                )

            if ticket.status == ApprovalStatus.REVOKED:
                if target_status != ApprovalStatus.REVOKED:
                    raise ApprovalInvalidError(
                        "Cannot modify a revoked approval ticket.",
                        details={
                            "approval_id": approval_id,
                            "requested_status": target_status.value,
                        },
                    )

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
        """Return a new ticket with the given fields replaced."""
        from dataclasses import replace

        return replace(ticket, **overrides)

    def _enforce_capacity_locked(self) -> None:
        """Ensure the ticket store has room for a new entry."""
        if len(self._tickets) < self._max_tickets:
            return

        removed_ids: List[str] = []
        now = _utc_now()
        for approval_id, ticket in list(self._tickets.items()):
            if ticket.status in (
                ApprovalStatus.CONSUMED,
                ApprovalStatus.REJECTED,
                ApprovalStatus.REVOKED,
                ApprovalStatus.EXPIRED,
            ) or ticket.expires_at <= now:
                self._tickets.pop(approval_id, None)
                removed_ids.append(approval_id)

        if removed_ids:
            stale_prefixes = tuple(f"{sid}:" for sid in removed_ids)
            self._consumed_scopes = {
                k for k in self._consumed_scopes
                if not k.startswith(stale_prefixes)
            }

        if len(self._tickets) < self._max_tickets:
            return

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
        prefix = f"{oldest_id}:"
        self._consumed_scopes = {
            k for k in self._consumed_scopes if not k.startswith(prefix)
        }

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
        """Validate a `base_sha` value."""
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

ApprovalManager = ApprovalGate


# ---------------------------------------------------------------------------
# Default gate singleton
# ---------------------------------------------------------------------------


_DEFAULT_GATE: Optional[ApprovalGate] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_approval_gate() -> ApprovalGate:
    """Return the process-wide default approval gate, creating it on first use."""
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        if _DEFAULT_GATE is None:
            _DEFAULT_GATE = ApprovalGate()
        return _DEFAULT_GATE


def set_default_approval_gate(gate: ApprovalGate) -> None:
    """Replace the process-wide default approval gate."""
    if not isinstance(gate, ApprovalGate):
        raise TypeError(
            "set_default_approval_gate expects an ApprovalGate instance."
        )
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        _DEFAULT_GATE = gate


def reset_default_approval_gate() -> ApprovalGate:
    """Replace the default gate with a fresh one."""
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        _DEFAULT_GATE = ApprovalGate()
        return _DEFAULT_GATE


get_default_approval_manager = get_default_approval_gate
set_default_approval_manager = set_default_approval_gate
reset_default_approval_manager = reset_default_approval_gate
