from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class OperationState(str, Enum):
    """Authoritative lifecycle states for source control operations."""

    CREATED = "created"
    REPOSITORY_RESOLVED = "repository_resolved"
    WORKSPACE_READY = "workspace_ready"
    EDITING = "editing"
    CHECKING = "checking"
    REVIEW_REQUIRED = "review_required"
    STALE_REVIEW_REQUIRED = "stale_review_required"
    APPROVED = "approved"
    PUSHING = "pushing"
    PUSHED = "pushed"
    CHANGE_REQUEST_CREATED = "change_request_created"
    FAILED_RETRYABLE = "failed_retryable"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    FAILED_FINAL = "failed_final"


# Canonical alias ensuring backwards compatibility across subsystems
OperationStatus = OperationState

# Valid state machine transitions governing operation lifecycle
VALID_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.CREATED: frozenset({
        OperationState.REPOSITORY_RESOLVED,
        OperationState.WORKSPACE_READY,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.REPOSITORY_RESOLVED: frozenset({
        OperationState.WORKSPACE_READY,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_RETRYABLE,
        OperationState.FAILED_FINAL,
    }),
    OperationState.WORKSPACE_READY: frozenset({
        OperationState.EDITING,
        OperationState.CHECKING,
        OperationState.REVIEW_REQUIRED,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_RETRYABLE,
        OperationState.FAILED_FINAL,
    }),
    OperationState.EDITING: frozenset({
        OperationState.CHECKING,
        OperationState.REVIEW_REQUIRED,
        OperationState.WORKSPACE_READY,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_RETRYABLE,
        OperationState.FAILED_FINAL,
    }),
    OperationState.CHECKING: frozenset({
        OperationState.REVIEW_REQUIRED,
        OperationState.EDITING,
        OperationState.WORKSPACE_READY,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_RETRYABLE,
        OperationState.FAILED_FINAL,
    }),
    OperationState.REVIEW_REQUIRED: frozenset({
        OperationState.APPROVED,
        OperationState.STALE_REVIEW_REQUIRED,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.STALE_REVIEW_REQUIRED: frozenset({
        OperationState.REVIEW_REQUIRED,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.APPROVED: frozenset({
        OperationState.PUSHING,
        OperationState.PUSHED,
        OperationState.STALE_REVIEW_REQUIRED,
        OperationState.BLOCKED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.PUSHING: frozenset({
        OperationState.PUSHED,
        OperationState.FAILED_RETRYABLE,
        OperationState.FAILED_FINAL,
    }),
    OperationState.PUSHED: frozenset({
        OperationState.CHANGE_REQUEST_CREATED,
        OperationState.COMPLETED,
        OperationState.FAILED_RETRYABLE,
        OperationState.FAILED_FINAL,
    }),
    OperationState.CHANGE_REQUEST_CREATED: frozenset({
        OperationState.COMPLETED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.FAILED_RETRYABLE: frozenset({
        OperationState.REPOSITORY_RESOLVED,
        OperationState.WORKSPACE_READY,
        OperationState.CHECKING,
        OperationState.PUSHING,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.CANCEL_REQUESTED: frozenset({
        OperationState.CANCELLED,
        OperationState.FAILED_FINAL,
    }),
    OperationState.COMPLETED: frozenset(),
    OperationState.BLOCKED: frozenset({
        OperationState.REVIEW_REQUIRED,
        OperationState.CANCEL_REQUESTED,
        OperationState.CANCELLED,
    }),
    OperationState.CANCELLED: frozenset(),
    OperationState.FAILED_FINAL: frozenset(),
}


@dataclass(frozen=True)
class OperationEvent:
    """Immutable transition record in the durable operation journal."""

    event_id: str
    to_state: OperationState = OperationState.CREATED
    from_state: Optional[OperationState] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_id: Optional[str] = None
    message: str = ""
    from_status: Optional[OperationState] = None
    to_status: Optional[OperationState] = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        effective_to = self.to_status if self.to_status is not None else self.to_state
        object.__setattr__(self, "to_state", effective_to)
        object.__setattr__(self, "to_status", effective_to)

        effective_from = self.from_status if self.from_status is not None else self.from_state
        object.__setattr__(self, "from_state", effective_from)
        object.__setattr__(self, "from_status", effective_from)

        merged_meta = dict(self.details) if self.details else dict(self.metadata)
        object.__setattr__(self, "metadata", merged_meta)
        object.__setattr__(self, "details", merged_meta)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the event record to a JSON-safe dictionary."""
        to_val = self.to_state.value if hasattr(self.to_state, "value") else str(self.to_state)
        from_val = (
            self.from_state.value
            if self.from_state and hasattr(self.from_state, "value")
            else (str(self.from_state) if self.from_state else None)
        )
        return {
            "event_id": self.event_id,
            "operation_id": self.operation_id,
            "from_state": from_val,
            "to_state": to_val,
            "from_status": from_val,
            "to_status": to_val,
            "state": to_val,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp),
            "metadata": self.metadata,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationEvent:
        """Constructs an OperationEvent from a dictionary record."""
        raw_to = data.get("to_state") or data.get("to_status") or data.get("state") or "created"
        raw_from = data.get("from_state") or data.get("from_status")
        try:
            to_state = OperationState(str(raw_to))
        except ValueError:
            to_state = OperationState.CREATED

        from_state = None
        if raw_from:
            try:
                from_state = OperationState(str(raw_from))
            except ValueError:
                from_state = None

        ts = (
            datetime.fromisoformat(data["timestamp"])
            if "timestamp" in data and isinstance(data["timestamp"], str)
            else (data.get("timestamp") or datetime.utcnow())
        )
        meta = dict(data.get("metadata") or data.get("details") or {})

        return cls(
            event_id=str(data["event_id"]),
            to_state=to_state,
            from_state=from_state,
            timestamp=ts,
            metadata=meta,
            operation_id=data.get("operation_id"),
            message=str(data.get("message", "")),
            details=meta,
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    """Tracks idempotency keys for mutations to prevent duplicate execution across retries."""

    idempotency_key: str
    operation_id: str
    action_type: str
    request_hash: str
    response_payload: Optional[dict[str, Any]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        """Serializes the idempotency record to a dictionary."""
        return {
            "idempotency_key": self.idempotency_key,
            "operation_id": self.operation_id,
            "action_type": self.action_type,
            "request_hash": self.request_hash,
            "response_payload": self.response_payload,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IdempotencyRecord:
        """Constructs an IdempotencyRecord from a dictionary."""
        return cls(
            idempotency_key=str(data["idempotency_key"]),
            operation_id=str(data["operation_id"]),
            action_type=str(data["action_type"]),
            request_hash=str(data["request_hash"]),
            response_payload=dict(data["response_payload"]) if data.get("response_payload") else None,
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.utcnow(),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
        )


@dataclass(frozen=True)
class OperationRecord:
    """Primary durable model representing an active or completed source control operation."""

    operation_id: str
    repository_id: str = ""
    operation_type: str = "push_branch"
    provider: Any = "github"
    target_branch: str = ""
    current_state: OperationState = OperationState.CREATED
    workspace_id: Optional[str] = None
    run_id: Optional[str] = None
    source_branch: Optional[str] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    diff_sha256: Optional[str] = None
    approval_id: Optional[str] = None
    commit_message: Optional[str] = None
    change_request_title: Optional[str] = None
    change_request_body: Optional[str] = None
    change_request_url: Optional[str] = None
    error_details: Optional[dict[str, Any]] = None
    events: tuple[OperationEvent, ...] = ()
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: Optional[Any] = None
    status: Optional[OperationState] = None

    def __post_init__(self) -> None:
        effective_state = self.status if self.status is not None else self.current_state
        object.__setattr__(self, "current_state", effective_state)
        object.__setattr__(self, "status", effective_state)

        effective_kind = self.kind if self.kind is not None else self.operation_type
        kind_str = effective_kind.value if hasattr(effective_kind, "value") else str(effective_kind)
        object.__setattr__(self, "operation_type", kind_str)
        object.__setattr__(self, "kind", effective_kind)

        if isinstance(self.events, list):
            object.__setattr__(self, "events", tuple(self.events))

    @property
    def is_terminal(self) -> bool:
        """Returns True if the operation has reached a final, immutable lifecycle state."""
        return self.current_state in {
            OperationState.COMPLETED,
            OperationState.CANCELLED,
            OperationState.FAILED_FINAL,
        }

    def can_transition_to(self, target_state: OperationState) -> bool:
        """Validates whether a transition from current state to target state is permitted."""
        allowed = VALID_TRANSITIONS.get(self.current_state, frozenset())
        return target_state in allowed

    def to_dict(self) -> dict[str, Any]:
        """Serializes the OperationRecord to a JSON-compatible dictionary."""
        provider_val = self.provider.value if hasattr(self.provider, "value") else str(self.provider)
        state_val = self.current_state.value if hasattr(self.current_state, "value") else str(self.current_state)

        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "repository_id": self.repository_id,
            "provider": provider_val,
            "kind": self.operation_type,
            "current_state": state_val,
            "status": state_val,
            "run_id": self.run_id,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "diff_sha256": self.diff_sha256,
            "workspace_id": self.workspace_id,
            "approval_id": self.approval_id,
            "commit_message": self.commit_message,
            "change_request_title": self.change_request_title,
            "change_request_body": self.change_request_body,
            "change_request_url": self.change_request_url,
            "error_details": self.error_details,
            "events": [e.to_dict() if hasattr(e, "to_dict") else dict(e) for e in self.events],
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else str(self.created_at),
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else str(self.updated_at),
            "completed_at": self.completed_at.isoformat() if isinstance(self.completed_at, datetime) else (str(self.completed_at) if self.completed_at else None),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationRecord:
        """Constructs an OperationRecord from a dictionary."""
        state_str = str(data.get("current_state") or data.get("status") or "created")
        try:
            state = OperationState(state_str)
        except ValueError:
            state = OperationState.CREATED

        raw_events = data.get("events", [])
        events_list: list[OperationEvent] = []
        if isinstance(raw_events, (list, tuple)):
            for ev in raw_events:
                if isinstance(ev, OperationEvent):
                    events_list.append(ev)
                elif isinstance(ev, dict):
                    events_list.append(OperationEvent.from_dict(ev))

        created_at = (
            datetime.fromisoformat(data["created_at"])
            if "created_at" in data and isinstance(data["created_at"], str)
            else (data.get("created_at") or datetime.utcnow())
        )
        updated_at = (
            datetime.fromisoformat(data["updated_at"])
            if "updated_at" in data and isinstance(data["updated_at"], str)
            else (data.get("updated_at") or datetime.utcnow())
        )
        completed_at = (
            datetime.fromisoformat(data["completed_at"])
            if data.get("completed_at") and isinstance(data["completed_at"], str)
            else data.get("completed_at")
        )

        return cls(
            operation_id=str(data["operation_id"]),
            repository_id=str(data.get("repository_id", "")),
            operation_type=str(data.get("operation_type") or data.get("kind") or "push_branch"),
            provider=str(data.get("provider", "github")),
            target_branch=str(data.get("target_branch", "")),
            current_state=state,
            workspace_id=data.get("workspace_id"),
            source_branch=data.get("source_branch"),
            base_sha=data.get("base_sha"),
            head_sha=data.get("head_sha"),
            diff_sha256=data.get("diff_sha256"),
            approval_id=data.get("approval_id"),
            commit_message=data.get("commit_message"),
            change_request_title=data.get("change_request_title"),
            change_request_body=data.get("change_request_body"),
            change_request_url=data.get("change_request_url"),
            error_details=dict(data["error_details"]) if data.get("error_details") else None,
            events=tuple(events_list),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
            metadata=dict(data.get("metadata", {})),
        )


__all__ = [
    "OperationState",
    "OperationStatus",
    "VALID_TRANSITIONS",
    "OperationRecord",
    "OperationEvent",
    "IdempotencyRecord",
]
