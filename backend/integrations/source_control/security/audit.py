from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .redaction import redact_data


@dataclass(frozen=True)
class SecurityAuditEvent:
    """Immutable record capturing a security-sensitive event in the source control subsystem."""

    event_id: str
    event_type: str
    action: str
    actor: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    repository_id: Optional[str] = None
    operation_id: Optional[str] = None
    approval_id: Optional[str] = None
    provider: Optional[str] = None
    success: bool = True
    diagnostic_code: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the audit event to a sanitized, JSON-safe dictionary."""
        payload = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "action": self.action,
            "actor": self.actor,
            "timestamp": self.timestamp.isoformat(),
            "repository_id": self.repository_id,
            "operation_id": self.operation_id,
            "approval_id": self.approval_id,
            "provider": self.provider,
            "success": self.success,
            "diagnostic_code": self.diagnostic_code,
            "details": redact_data(self.details),
        }
        return payload


@runtime_checkable
class AuditSink(Protocol):
    """Abstract port for writing and querying security audit events."""

    def record_event(self, event: SecurityAuditEvent) -> None:
        """Appends an event to the audit store."""
        ...

    def query_events(
        self,
        event_type: Optional[str] = None,
        repository_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[SecurityAuditEvent]:
        """Queries stored audit events matching filter criteria."""
        ...


class InMemoryAuditSink:
    """Thread-safe in-memory audit sink intended for isolated testing and local fast queries."""

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._events: list[SecurityAuditEvent] = []
        self._lock = threading.Lock()

    def record_event(self, event: SecurityAuditEvent) -> None:
        with self._lock:
            if len(self._events) >= self.capacity:
                self._events.pop(0)
            self._events.append(event)

    def query_events(
        self,
        event_type: Optional[str] = None,
        repository_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[SecurityAuditEvent]:
        with self._lock:
            matched: list[SecurityAuditEvent] = []
            for ev in reversed(self._events):
                if event_type and ev.event_type != event_type:
                    continue
                if repository_id and ev.repository_id != repository_id:
                    continue
                if operation_id and ev.operation_id != operation_id:
                    continue
                matched.append(ev)
                if len(matched) >= limit:
                    break
            return tuple(matched)

    def clear(self) -> None:
        """Clears recorded events (test teardown helper)."""
        with self._lock:
            self._events.clear()


class FileAppendOnlyAuditSink:
    """Persistent, append-only JSON Lines audit file sink."""

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self._lock = threading.Lock()
        log_dir = os.path.dirname(os.path.abspath(log_path))
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    def record_event(self, event: SecurityAuditEvent) -> None:
        serialized = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()

    def query_events(
        self,
        event_type: Optional[str] = None,
        repository_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[SecurityAuditEvent]:
        if not os.path.exists(self.log_path):
            return ()

        matched: list[SecurityAuditEvent] = []
        with self._lock:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        for line in reversed(lines):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                raw = json.loads(line_str)
                if event_type and raw.get("event_type") != event_type:
                    continue
                if repository_id and raw.get("repository_id") != repository_id:
                    continue
                if operation_id and raw.get("operation_id") != operation_id:
                    continue

                event = SecurityAuditEvent(
                    event_id=raw["event_id"],
                    event_type=raw["event_type"],
                    action=raw["action"],
                    actor=raw["actor"],
                    timestamp=datetime.fromisoformat(raw["timestamp"]),
                    repository_id=raw.get("repository_id"),
                    operation_id=raw.get("operation_id"),
                    approval_id=raw.get("approval_id"),
                    provider=raw.get("provider"),
                    success=raw.get("success", True),
                    diagnostic_code=raw.get("diagnostic_code"),
                    details=raw.get("details", {}),
                )
                matched.append(event)
                if len(matched) >= limit:
                    break
            except Exception:
                continue

        return tuple(matched)


class SecurityAuditLogger:
    """High-level audit logging service coordinating security event emission and sink dispatch."""

    def __init__(self, sink: Optional[AuditSink] = None) -> None:
        self.sink: AuditSink = sink or InMemoryAuditSink()

    def log(
        self,
        event_type: str,
        action: str,
        actor: str = "system",
        *,
        repository_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        approval_id: Optional[str] = None,
        provider: Optional[str] = None,
        success: bool = True,
        diagnostic_code: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> SecurityAuditEvent:
        """Records a scrubbed security audit event."""
        clean_details = dict(redact_data(dict(details))) if details else {}
        event = SecurityAuditEvent(
            event_id=f"audit_{secrets.token_hex(12)}",
            event_type=event_type,
            action=action,
            actor=actor,
            timestamp=datetime.utcnow(),
            repository_id=repository_id,
            operation_id=operation_id,
            approval_id=approval_id,
            provider=provider,
            success=success,
            diagnostic_code=diagnostic_code,
            details=clean_details,
        )
        self.sink.record_event(event)
        return event

    def log_auth_attempt(
        self, provider: str, actor: str, success: bool, details: Optional[Mapping[str, Any]] = None
    ) -> SecurityAuditEvent:
        return self.log(
            event_type="authentication",
            action="oauth_exchange",
            actor=actor,
            provider=provider,
            success=success,
            details=details,
        )

    def log_auth_revocation(
        self, provider: str, actor: str, details: Optional[Mapping[str, Any]] = None
    ) -> SecurityAuditEvent:
        return self.log(
            event_type="authentication",
            action="token_revoked",
            actor=actor,
            provider=provider,
            success=True,
            details=details,
        )

    def log_approval_decision(
        self,
        approval_id: str,
        operation_id: str,
        decision: str,
        actor: str,
        repository_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> SecurityAuditEvent:
        return self.log(
            event_type="approval",
            action=f"approval_{decision.lower()}",
            actor=actor,
            approval_id=approval_id,
            operation_id=operation_id,
            repository_id=repository_id,
            success=True,
            details=details,
        )

    def log_policy_denial(
        self,
        operation_kind: str,
        reason: str,
        actor: str = "agent",
        repository_id: Optional[str] = None,
        target_branch: Optional[str] = None,
    ) -> SecurityAuditEvent:
        return self.log(
            event_type="policy",
            action="policy_enforcement_denied",
            actor=actor,
            repository_id=repository_id,
            success=False,
            diagnostic_code="PERMISSION_DENIED",
            details={
                "operation_kind": operation_kind,
                "denial_reason": reason,
                "target_branch": target_branch,
            },
        )

    def log_remote_write(
        self,
        action: str,
        provider: str,
        repository_id: str,
        operation_id: str,
        approval_id: str,
        success: bool,
        details: Optional[Mapping[str, Any]] = None,
    ) -> SecurityAuditEvent:
        return self.log(
            event_type="remote_write",
            action=action,
            actor="agent_runner",
            provider=provider,
            repository_id=repository_id,
            operation_id=operation_id,
            approval_id=approval_id,
            success=success,
            details=details,
        )


# ============================================================
# SecurityAuditLogger Singleton Accessors
# ============================================================

_DEFAULT_AUDIT_LOGGER: Optional[SecurityAuditLogger] = None
_AUDIT_LOCK = threading.Lock()


def get_default_audit_logger() -> SecurityAuditLogger:
    """Returns the process-wide default SecurityAuditLogger singleton."""
    global _DEFAULT_AUDIT_LOGGER
    if _DEFAULT_AUDIT_LOGGER is None:
        with _AUDIT_LOCK:
            if _DEFAULT_AUDIT_LOGGER is None:
                _DEFAULT_AUDIT_LOGGER = SecurityAuditLogger()
    return _DEFAULT_AUDIT_LOGGER


def set_default_audit_logger(logger: SecurityAuditLogger) -> None:
    """Explicitly injects a SecurityAuditLogger instance (used during testing)."""
    global _DEFAULT_AUDIT_LOGGER
    with _AUDIT_LOCK:
        _DEFAULT_AUDIT_LOGGER = logger


def reset_default_audit_logger() -> None:
    """Resets the process-wide default SecurityAuditLogger (used during testing)."""
    global _DEFAULT_AUDIT_LOGGER
    with _AUDIT_LOCK:
        _DEFAULT_AUDIT_LOGGER = None
