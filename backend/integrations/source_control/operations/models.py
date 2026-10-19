# backend/integrations/source_control/operations/models.py
"""
Operation models for the Source Control Integration subsystem.

This module is deliberately thin. The canonical definitions of
`OperationRecord`, `OperationEvent`, `OperationStatus`, and
`OperationEventType` live in `domain.models` and `domain.enums`, and
are re-exported here so that the `operations` package has a single
import surface.

Beyond re-exports, this module adds:

    - `OperationTransition` : a small value object describing a single
      state transition, used by the audit trail and by the recovery
      machinery.
    - `TERMINAL_STATUSES`   : the set of statuses that end an
      operation.
    - `ALLOWED_TRANSITIONS` : the state-machine definition that
      governs valid transitions.
    - `is_terminal_status`  : predicate for terminal statuses.
    - `is_valid_transition` : predicate for valid transitions.

Design rules:
    - This module MUST NOT redefine `OperationRecord`,
      `OperationEvent`, `OperationStatus`, or `OperationEventType`.
      All such definitions live in `domain`.
    - The state machine defined here is enforced by
      `operations.service.OperationService`. The service MUST reject
      any transition not present in `ALLOWED_TRANSITIONS`.
    - Terminal statuses MUST NOT appear as keys in
      `ALLOWED_TRANSITIONS`, except to declare self-loops that are
      explicitly forbidden elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, FrozenSet, Mapping, Optional

# Canonical definitions: re-exported for the operations package.
from ..domain.enums import (  # noqa: F401 (re-exported)
    OperationEventType,
    OperationStatus,
)
from ..domain.enums import ProviderId  # noqa: F401 (re-exported)
from ..domain.models import (  # noqa: F401 (re-exported)
    OperationEvent,
    OperationRecord,
    _utc_now,
)

__all__ = [
    # Re-exports
    "OperationRecord",
    "OperationEvent",
    "OperationStatus",
    "OperationState",
    "OperationEventType",
    "IdempotencyRecord",
    "_utc_now",
    # Operation-specific value objects
    "OperationTransition",
    # State-machine constants and predicates
    "TERMINAL_STATUSES",
    "ALLOWED_TRANSITIONS",
    "VALID_TRANSITIONS",
    "is_terminal_status",
    "is_valid_transition",
    "allowed_next_statuses",
]


# ---------------------------------------------------------------------------
# Terminal statuses
# ---------------------------------------------------------------------------


#: Statuses that end an operation. Once an operation reaches one of
#: these, no further transitions are permitted and no metadata can be
#: updated on the record.
#:
#: IMPORTANT: `PUSHED` is deliberately NOT in this set. It is an
#: intermediate state from which `CHANGE_REQUEST_CREATED` is the
#: canonical successor (see `ALLOWED_TRANSITIONS` below, which
#: explicitly permits `PUSHED -> CHANGE_REQUEST_CREATED`). Treating
#: `PUSHED` as terminal would block `attach_change_request` and the
#: subsequent transition, making `create_change_request` unusable on
#: a successfully pushed operation.
TERMINAL_STATUSES: FrozenSet[OperationStatus] = frozenset(
    {
        OperationStatus.CHANGE_REQUEST_CREATED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
        OperationStatus.REJECTED,
    }
)


# ---------------------------------------------------------------------------
# State machine definition
# ---------------------------------------------------------------------------


#: Allowed transitions from each non-terminal status.
#:
#: The keys are the source statuses. The values are the sets of
#: statuses that the operation service is permitted to transition to.
#: Any transition not present here MUST be rejected by the service.
#:
#: Notes on specific transitions:
#:
#:     - `CREATED -> REPOSITORY_RESOLVED`: the first successful step
#:       after the operation is accepted.
#:     - `* -> FAILED_RETRYABLE | FAILED_FINAL`: any non-terminal
#:       status can fail. `FAILED_RETRYABLE` is used for transient
#:       errors; `FAILED_FINAL` for terminal ones.
#:     - `* -> BLOCKED`: the operation cannot proceed until the user
#:       acts (e.g. missing credentials).
#:     - `* -> CANCEL_REQUESTED`: the user requested cancellation.
#:     - `REVIEW_REQUIRED -> STALE_REVIEW_REQUIRED`: the remote base
#:       moved after the review was prepared.
#:     - `STALE_REVIEW_REQUIRED -> REVIEW_REQUIRED`: the user chose to
#:       re-review the updated diff.
ALLOWED_TRANSITIONS: Dict[OperationStatus, FrozenSet[OperationStatus]] = {
    OperationStatus.CREATED: frozenset(
        {
            OperationStatus.REPOSITORY_RESOLVED,
            OperationStatus.BLOCKED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.REPOSITORY_RESOLVED: frozenset(
        {
            OperationStatus.WORKSPACE_READY,
            OperationStatus.BLOCKED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.WORKSPACE_READY: frozenset(
        {
            OperationStatus.EDITING,
            OperationStatus.CHECKING,
            OperationStatus.BLOCKED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.EDITING: frozenset(
        {
            OperationStatus.CHECKING,
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.BLOCKED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.CHECKING: frozenset(
        {
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.EDITING,  # user chose to fix issues
            OperationStatus.BLOCKED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.REVIEW_REQUIRED: frozenset(
        {
            OperationStatus.APPROVED,
            OperationStatus.REJECTED,
            OperationStatus.STALE_REVIEW_REQUIRED,
            OperationStatus.BLOCKED,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.STALE_REVIEW_REQUIRED: frozenset(
        {
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.REJECTED,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.FAILED_FINAL,
        }
    ),
    OperationStatus.APPROVED: frozenset(
        {
            OperationStatus.PUSHING,
            # Terminal successor for operations whose kind is
            # `create_change_request`. The push flow that shares
            # `APPROVED` as an entry point never takes this edge
            # (it goes APPROVED -> PUSHING -> PUSHED); only the
            # create_change_request flow does, and it does so in a
            # single step after the approval is consumed. Without
            # this edge, every change-request creation raises
            # `ConflictError` at the final transition and the record
            # is stuck in APPROVED — while the ticket has already
            # been consumed and the remote PR has already been
            # created, so the record and reality disagree.
            OperationStatus.CHANGE_REQUEST_CREATED,
            OperationStatus.BLOCKED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.PUSHING: frozenset(
        {
            OperationStatus.PUSHED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
            # Ambiguous timeout path: the manager returns to APPROVED
            # after reconciliation shows the push did not happen.
            OperationStatus.APPROVED,
        }
    ),
    # NOTE: PUSHED is NOT a terminal status. It is the last state of
    # the local happy path, from which the operation may either
    # create a change request (CHANGE_REQUEST_CREATED) or stop. It
    # may also fail transiently or terminally. See TERMINAL_STATUSES
    # above for the canonical list of terminal states.
    OperationStatus.PUSHED: frozenset(
        {
            OperationStatus.CHANGE_REQUEST_CREATED,
            OperationStatus.FAILED_RETRYABLE,
            OperationStatus.FAILED_FINAL,
        }
    ),
    OperationStatus.BLOCKED: frozenset(
        {
            # A blocked operation can be unblocked by the user, which
            # returns it to the previous working state. We do not
            # track the "previous" status here; the service decides.
            OperationStatus.CREATED,
            OperationStatus.REPOSITORY_RESOLVED,
            OperationStatus.WORKSPACE_READY,
            OperationStatus.EDITING,
            OperationStatus.CHECKING,
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.APPROVED,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.FAILED_RETRYABLE: frozenset(
        {
            # A retryable failure may be retried by returning to the
            # appropriate earlier status, or it may become terminal.
            OperationStatus.CREATED,
            OperationStatus.REPOSITORY_RESOLVED,
            OperationStatus.WORKSPACE_READY,
            OperationStatus.EDITING,
            OperationStatus.CHECKING,
            OperationStatus.REVIEW_REQUIRED,
            OperationStatus.APPROVED,
            OperationStatus.PUSHING,
            OperationStatus.FAILED_FINAL,
            OperationStatus.CANCEL_REQUESTED,
        }
    ),
    OperationStatus.CANCEL_REQUESTED: frozenset(
        {
            OperationStatus.CANCELLED,
            OperationStatus.FAILED_FINAL,
        }
    ),
    # Terminal statuses have no outgoing transitions.
    OperationStatus.CHANGE_REQUEST_CREATED: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
    OperationStatus.FAILED_FINAL: frozenset(),
    OperationStatus.REJECTED: frozenset(),
}


# ---------------------------------------------------------------------------
# Transition record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationTransition:
    """
    A single state transition on an operation.

    This value object is used by the operation journal and the audit
    log to record exactly what happened. It is intentionally
    independent from `OperationEvent`; the journal stores events, and
    a transition is one kind of event whose payload is this shape.

    Fields:
        operation_id: Identifier of the operation.
        from_status:  Source status before the transition.
        to_status:    Destination status after the transition.
        occurred_at:  Timezone-aware timestamp.
        reason:       Optional human-readable explanation.
        metadata:     Extension bag. MUST NOT contain secrets.
    """

    operation_id: str
    from_status: OperationStatus
    to_status: OperationStatus
    occurred_at: datetime = field(default_factory=_utc_now)
    reason: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def is_noop(self) -> bool:
        """Return True if the transition does not change the status."""
        return self.from_status == self.to_status

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the journal and audit log."""
        return {
            "operation_id": self.operation_id,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "occurred_at": self.occurred_at.isoformat(),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# State-machine predicates
# ---------------------------------------------------------------------------


def is_terminal_status(status: OperationStatus) -> bool:
    """Return True if `status` is a terminal status."""
    return status in TERMINAL_STATUSES


def is_valid_transition(
    from_status: OperationStatus,
    to_status: OperationStatus,
) -> bool:
    """
    Return True if transitioning from `from_status` to `to_status` is
    permitted by the state machine.

    A transition to the same status is never valid; callers that need
    to record a "no-op" MUST filter it out before calling this
    predicate.
    """
    if from_status == to_status:
        return False
    allowed = ALLOWED_TRANSITIONS.get(from_status)
    if allowed is None:
        return False
    return to_status in allowed


def allowed_next_statuses(
    status: OperationStatus,
) -> FrozenSet[OperationStatus]:
    """
    Return the set of statuses reachable from `status`.

    Empty for terminal statuses.
    """
    return ALLOWED_TRANSITIONS.get(status, frozenset())


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------

#: Alias for `OperationStatus`.
#:
#: The HTTP facade and the schemas were written against an
#: `OperationState` vocabulary. This alias keeps them working without
#: renaming the canonical enum.
OperationState = OperationStatus

#: Alias for `ALLOWED_TRANSITIONS`.
#:
#: Some callers prefer the name "valid transitions" when inspecting
#: the state machine directly.
VALID_TRANSITIONS: Dict[OperationStatus, FrozenSet[OperationStatus]] = (
    ALLOWED_TRANSITIONS
)


# ---------------------------------------------------------------------------
# Idempotency record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyRecord:
    """
    Lightweight snapshot of an idempotency entry.

    This is a mirror of `operations.idempotency.IdempotencyEntry`
    exposed through the `operations.models` namespace so that
    `operations/__init__.py` can re-export a stable name without
    creating a circular import between `models` and `idempotency`.

    The canonical class remains `IdempotencyEntry`. This record is
    intentionally schema-light: it carries only the fields that
    callers typically read when listing or auditing idempotent
    requests.

    Fields:
        key:         The 64-character hex digest of the request.
        operation_id: The operation that produced the entry.
        outcome:     Current outcome (`in_progress`, `succeeded`,
                     `failed`, or `ambiguous`).
        created_at:  When the entry was created.
        updated_at:  When the entry was last updated.
        expires_at:  When the entry becomes stale.
        remote_ref:  Optional remote reference produced by the
                     operation (e.g. a change request URL).
        error_code:  Optional error code when the outcome is
                     `failed`.
        message:     Optional human-readable detail.
    """

    key: str
    operation_id: str
    outcome: str = "in_progress"
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    expires_at: Optional[datetime] = None
    remote_ref: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
