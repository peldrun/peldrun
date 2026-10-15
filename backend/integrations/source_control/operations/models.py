from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from ..domain.enums import OperationKind, OperationStatus, ProviderId

# Explicit valid state machine transitions
VALID_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.CREATED: frozenset({
        OperationStatus.REPOSITORY_RESOLVED,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.REPOSITORY_RESOLVED: frozenset({
        OperationStatus.WORKSPACE_READY,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_RETRYABLE,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.WORKSPACE_READY: frozenset({
        OperationStatus.EDITING,
        OperationStatus.CHECKING,
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_RETRYABLE,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.EDITING: frozenset({
        OperationStatus.CHECKING,
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.WORKSPACE_READY,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_RETRYABLE,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.CHECKING: frozenset({
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.EDITING,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_RETRYABLE,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.REVIEW_REQUIRED: frozenset({
        OperationStatus.APPROVED,
        OperationStatus.STALE_REVIEW_REQUIRED,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.STALE_REVIEW_REQUIRED: frozenset({
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.APPROVED: frozenset({
        OperationStatus.PUSHING,
        OperationStatus.STALE_REVIEW_REQUIRED,
        OperationStatus.BLOCKED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.PUSHING: frozenset({
        OperationStatus.PUSHED,
        OperationStatus.FAILED_RETRYABLE,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.PUSHED: frozenset({
        OperationStatus.CHANGE_REQUEST_CREATED,
        OperationStatus.COMPLETED,
        OperationStatus.FAILED_RETRYABLE,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.CHANGE_REQUEST_CREATED: frozenset({
        OperationStatus.COMPLETED,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.FAILED_RETRYABLE: frozenset({
        OperationStatus.REPOSITORY_RESOLVED,
        OperationStatus.WORKSPACE_READY,
        OperationStatus.CHECKING,
        OperationStatus.PUSHING,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
    }),
    OperationStatus.CANCEL_REQUESTED: frozenset({
        OperationStatus.CANCELLED,
        OperationStatus.FAILED_FINAL,
    }),
    # Terminal states have no outbound transitions
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.BLOCKED: frozenset({
        OperationStatus.REVIEW_REQUIRED,
        OperationStatus.CANCEL_REQUESTED,
        OperationStatus.CANCELLED,
    }),
    OperationStatus.CANCELLED: frozenset(),
    OperationStatus.FAILED_FINAL: frozenset(),
}


@dataclass(frozen=True)
class OperationEvent:
    """Immutable transition record in the durable operation journal."""

    event_id: str
    operation_id: str
    from_status: Optional[OperationStatus]
    to_status: OperationStatus
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the operation event to a JSON-compatible dictionary."""
        return {
            "event_id": self.event_id,
            "operation_id": self.operation_id,
            "from_status": self.from_status.value if self.from_status else None,
            "to_status": self.to_status.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationEvent:
        """Constructs an OperationEvent from a dictionary record."""
        return cls(
            event_id=str(data["event_id"]),
            operation_id=str(data["operation_id"]),
            from_status=OperationStatus(data["from_status"]) if data.get("from_status") else None,
            to_status=OperationStatus(data["to_status"]),
            message=str(data.get("message", "")),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.utcnow(),
            details=dict(data.get("details", {})),
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
    repository_id: str
    provider: ProviderId
    kind: OperationKind
    status: OperationStatus = OperationStatus.CREATED
    run_id: Optional[str] = None
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    diff_sha256: Optional[str] = None
    workspace_id: Optional[str] = None
    approval_id: Optional[str] = None
    commit_message: Optional[str] = None
    change_request_title: Optional[str] = None
    change_request_body: Optional[str] = None
    change_request_url: Optional[str] = None
    error_details: Optional[dict[str, Any]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """Returns True if the operation has reached a final, immutable lifecycle state."""
        return self.status in {
            OperationStatus.COMPLETED,
            OperationStatus.CANCELLED,
            OperationStatus.FAILED_FINAL,
        }

    def can_transition_to(self, target_status: OperationStatus) -> bool:
        """Validates whether a transition from current status to target status is permitted."""
        allowed = VALID_TRANSITIONS.get(self.status, frozenset())
        return target_status in allowed

    def to_dict(self) -> dict[str, Any]:
        """Serializes the OperationRecord to a JSON-compatible dictionary."""
        return {
            "operation_id": self.operation_id,
            "repository_id": self.repository_id,
            "provider": self.provider.value,
            "kind": self.kind.value,
            "status": self.status.value,
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
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationRecord:
        """Constructs an OperationRecord from a dictionary."""
        return cls(
            operation_id=str(data["operation_id"]),
            repository_id=str(data["repository_id"]),
            provider=ProviderId(data["provider"]),
            kind=OperationKind(data["kind"]),
            status=OperationStatus(data.get("status", OperationStatus.CREATED.value)),
            run_id=data.get("run_id"),
            source_branch=data.get("source_branch"),
            target_branch=data.get("target_branch"),
            base_sha=data.get("base_sha"),
            head_sha=data.get("head_sha"),
            diff_sha256=data.get("diff_sha256"),
            workspace_id=data.get("workspace_id"),
            approval_id=data.get("approval_id"),
            commit_message=data.get("commit_message"),
            change_request_title=data.get("change_request_title"),
            change_request_body=data.get("change_request_body"),
            change_request_url=data.get("change_request_url"),
            error_details=dict(data["error_details"]) if data.get("error_details") else None,
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.utcnow(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if "updated_at" in data else datetime.utcnow(),
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            metadata=dict(data.get("metadata", {})),
        )
