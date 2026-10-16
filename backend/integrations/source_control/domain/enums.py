# backend/integrations/source_control/domain/enums.py
"""
Domain enumerations for the Source Control Integration subsystem.

This module defines all provider-neutral enums used across the domain,
ports, adapters, and application layers. Enums in this module are part
of the domain language and must remain free of any infrastructure or
provider-specific concerns.

Design rules:
    - Enum values MUST be stable identifiers (used for persistence,
      API contracts, and audit logs).
    - Enum values MUST NOT contain provider-specific quirks; provider
      translation happens in adapters.
    - Enums MUST NOT import from any module outside `domain.enums` or
      the Python standard library.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ProviderId",
    "ChangeRequestState",
    "FileChangeType",
    "OperationStatus",
    "OperationEventType",
    "SourceControlErrorCode",
    "ApprovalStatus",
    "ConnectionStatus",
    "CheckStatus",
    "CheckConclusion",
]


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------


class ProviderId(str, Enum):
    """
    Stable identifier for a supported source-control provider.

    The value MUST match the provider's canonical lowercase identifier.
    Adding a new provider requires only a new enum member and a new
    adapter implementing `ports.provider.SourceControlProvider`.
    """

    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


# ---------------------------------------------------------------------------
# Change request (PR / MR) state
# ---------------------------------------------------------------------------


class ChangeRequestState(str, Enum):
    """
    Normalized lifecycle state of a change request.

    Providers use different vocabularies:
        - GitHub Pull Request:  open, closed, merged
        - GitLab Merge Request: opened, closed, merged, locked
        - Bitbucket Pull Request: OPEN, MERGED, DECLINED, SUPERSEDED

    Adapters MUST map their provider-specific states to one of the
    values below. Unknown provider states MUST map to `UNKNOWN` and
    never raise during mapping.
    """

    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# File change classification
# ---------------------------------------------------------------------------


class FileChangeType(str, Enum):
    """
    Classification of a single file change within a diff.

    The set of values mirrors the Git diff vocabulary and MUST remain
    stable because it is used in approval snapshots and audit logs.
    """

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    UNMERGED = "unmerged"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Operation lifecycle
# ---------------------------------------------------------------------------


class OperationStatus(str, Enum):
    """
    Durable state of a source-control operation.

    The state machine is enforced by `operations.service.OperationService`.
    Only the service layer is allowed to transition between states;
    agents and tools MUST NOT set the state directly.

    Terminal states: PUSHED, CHANGE_REQUEST_CREATED, CANCELLED,
    FAILED_FINAL, REJECTED.
    """

    CREATED = "created"
    REPOSITORY_RESOLVED = "repository_resolved"
    WORKSPACE_READY = "workspace_ready"
    EDITING = "editing"
    CHECKING = "checking"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    PUSHING = "pushing"
    PUSHED = "pushed"
    CHANGE_REQUEST_CREATED = "change_request_created"

    # Failure / cancellation branches
    BLOCKED = "blocked"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    STALE_REVIEW_REQUIRED = "stale_review_required"
    REJECTED = "rejected"


class OperationEventType(str, Enum):
    """
    Type of an entry in the durable operation event journal.

    Events are append-only and immutable. Each event references the
    operation it belongs to and carries a monotonically increasing
    sequence number within that operation.
    """

    OPERATION_CREATED = "operation_created"
    STATE_TRANSITION = "state_transition"
    WORKSPACE_ATTACHED = "workspace_attached"
    FILE_CHANGED = "file_changed"
    CHECK_EXECUTED = "check_executed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    PUSH_ATTEMPTED = "push_attempted"
    PUSH_CONFIRMED = "push_confirmed"
    CHANGE_REQUEST_CREATED = "change_request_created"
    ERROR = "error"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class SourceControlErrorCode(str, Enum):
    """
    Stable error categories used across the entire subsystem.

    Values are UPPERCASE and are part of the public API contract. They
    are surfaced to the frontend, the agent, and the audit log. Do NOT
    change existing values without a migration; add new members instead.

    Adapters MUST translate provider-specific errors to one of these
    categories. Never propagate raw provider error codes.
    """

    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    REMOTE_STATE_CHANGED = "REMOTE_STATE_CHANGED"
    PROVIDER_ERROR = "PROVIDER_ERROR"

    # Local-only error categories
    WORKSPACE_DIRTY = "WORKSPACE_DIRTY"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    PATH_TRAVERSAL_DETECTED = "PATH_TRAVERSAL_DETECTED"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    COMMAND_FAILED = "COMMAND_FAILED"

    # Approval-related error categories
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"

    # Policy-related error categories
    POLICY_DENIED = "POLICY_DENIED"
    PROTECTED_BRANCH = "PROTECTED_BRANCH"
    WORKFLOW_FILE_CHANGE = "WORKFLOW_FILE_CHANGE"


# ---------------------------------------------------------------------------
# Approval lifecycle
# ---------------------------------------------------------------------------


class ApprovalStatus(str, Enum):
    """
    Lifecycle of an approval ticket.

    An approval is single-use. Once consumed (approved + executed), it
    MUST NOT be reused. Revoked or expired approvals cannot be consumed.
    """

    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    REVOKED = "revoked"


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class ConnectionStatus(str, Enum):
    """
    Lifecycle of a provider connection.

    `CONNECTED` means a credential is present in the vault and was last
    validated successfully. It does NOT imply any policy has been
    granted; policy is evaluated per operation.
    """

    DISCONNECTED = "disconnected"
    PENDING = "pending"
    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Checks (CI status)
# ---------------------------------------------------------------------------


class CheckStatus(str, Enum):
    """
    Normalized status of a CI check run.
    """

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class CheckConclusion(str, Enum):
    """
    Normalized conclusion of a completed CI check run.

    Providers that do not expose a conclusion (only a status) MUST
    map to `UNKNOWN`.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    STALE = "stale"
    UNKNOWN = "unknown"
