# backend/integrations/source_control/security/audit.py
"""
Audit logging for the Source Control Integration subsystem.

Every security-relevant event (authentication, policy decisions,
approvals, pushes, credential changes) is recorded as an immutable
`AuditEvent`. Events are append-only and MUST NOT contain secrets:
the constructor applies `redact_mapping` to the payload before the
event is created, so secrets never enter the in-memory or on-disk
representation.

Three implementations are provided:

    - `InMemoryAuditLogger` : bounded ring buffer for tests and for
                              short-lived runs.
    - `FileAuditLogger`     : JSONL file on disk with rotation. This
                              is the production implementation.
    - `NullAuditLogger`     : discards every event. Used when the
                              audit subsystem is disabled.

Design rules:
    - Audit logging MUST NOT raise into the caller. Failures are
      logged at WARNING level and swallowed.
    - Audit events MUST be safe to serialize and to display in the UI.
    - Audit events MUST NOT be used as an authoritative state store;
      they are observational only.
    - The default logger is a process-wide singleton. Tests reset it
      via `reset_default_audit_logger`.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Mapping, Optional, Sequence

from .redaction import redact_mapping, redact_text

__all__ = [
    "AuditEvent",
    "AuditEventType",
    "AuditSeverity",
    "AuditLogger",
    "InMemoryAuditLogger",
    "FileAuditLogger",
    "NullAuditLogger",
    "get_default_audit_logger",
    "set_default_audit_logger",
    "reset_default_audit_logger",
]


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AuditEventType(str, Enum):
    """
    High-level category of an audit event.

    New types SHOULD be added conservatively: the audit log is a
    contract with the UI and with external tooling. When in doubt, use
    `OTHER` with a descriptive `action` field rather than introducing
    a new enum member.
    """

    AUTH_BEGIN = "auth_begin"
    AUTH_COMPLETE = "auth_complete"
    AUTH_FAILURE = "auth_failure"
    AUTH_REVOKE = "auth_revoke"

    CONNECTION_VALIDATE = "connection_validate"
    CONNECTION_DISCONNECT = "connection_disconnect"

    POLICY_DECISION = "policy_decision"
    POLICY_DENIED = "policy_denied"

    APPROVAL_REQUEST = "approval_request"
    APPROVAL_GRANT = "approval_grant"
    APPROVAL_REJECT = "approval_reject"
    APPROVAL_EXPIRE = "approval_expire"
    APPROVAL_CONSUME = "approval_consume"

    OPERATION_CREATE = "operation_create"
    OPERATION_TRANSITION = "operation_transition"
    OPERATION_CANCEL = "operation_cancel"
    OPERATION_FAILURE = "operation_failure"

    WORKSPACE_CREATE = "workspace_create"
    WORKSPACE_DELETE = "workspace_delete"
    WORKSPACE_DIRTY = "workspace_dirty"

    PUSH_ATTEMPT = "push_attempt"
    PUSH_SUCCESS = "push_success"
    PUSH_FAILURE = "push_failure"
    PUSH_REJECTED = "push_rejected"

    CHANGE_REQUEST_CREATE = "change_request_create"
    CHANGE_REQUEST_MERGE = "change_request_merge"

    CREDENTIAL_STORE = "credential_store"
    CREDENTIAL_DELETE = "credential_delete"
    CREDENTIAL_REFRESH = "credential_refresh"

    SECRET_DETECTED = "secret_detected"
    PATH_TRAVERSAL_BLOCKED = "path_traversal_blocked"

    ERROR = "error"
    OTHER = "other"


class AuditSeverity(str, Enum):
    """
    Severity level of an audit event.

    Used by the UI to filter and highlight events. `ERROR` and
    `CRITICAL` events SHOULD be surfaced prominently.
    """

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


#: Severity ordering, used for filtering (`>= min_severity`).
_SEVERITY_ORDER: dict[AuditSeverity, int] = {
    AuditSeverity.DEBUG: 10,
    AuditSeverity.INFO: 20,
    AuditSeverity.WARNING: 30,
    AuditSeverity.ERROR: 40,
    AuditSeverity.CRITICAL: 50,
}


# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """
    An immutable audit event.

    The `payload` mapping is redacted at construction time. It MUST
    NOT contain secrets. The `event_id` is cryptographically random
    and unique within the process lifetime.

    Fields:
        event_id:     Random identifier (hex, 16 chars).
        event_type:   High-level category.
        severity:     Severity level.
        timestamp:    Timezone-aware UTC timestamp.
        action:       Short, human-readable action verb.
        message:      Optional short description. Redacted.
        operation_id: Related operation, when applicable.
        run_id:       Related agent run, when applicable.
        provider:     Related provider, when applicable.
        repository_id: Related repository, when applicable.
        workspace_id: Related workspace, when applicable.
        approval_id:  Related approval ticket, when applicable.
        actor:        Who triggered the event (e.g. "user", "agent").
        payload:      Redacted structured details.
        error_code:   `SourceControlErrorCode` value, when applicable.
        redacted:     True if the payload was modified by redaction.
    """

    event_type: AuditEventType
    severity: AuditSeverity = AuditSeverity.INFO
    action: str = ""
    message: Optional[str] = None
    operation_id: Optional[str] = None
    run_id: Optional[str] = None
    provider: Optional[str] = None
    repository_id: Optional[str] = None
    workspace_id: Optional[str] = None
    approval_id: Optional[str] = None
    actor: str = "system"
    payload: Mapping[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    event_id: str = field(default_factory=lambda: secrets.token_hex(8))
    timestamp: datetime = field(default_factory=_utc_now)
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the event to a JSON-safe mapping.

        The returned mapping is safe to log, to persist, and to send
        to the frontend.
        """
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "message": self.message,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "repository_id": self.repository_id,
            "workspace_id": self.workspace_id,
            "approval_id": self.approval_id,
            "actor": self.actor,
            "payload": dict(self.payload),
            "error_code": self.error_code,
            "redacted": self.redacted,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuditEvent":
        """
        Reconstruct an `AuditEvent` from a mapping produced by
        `to_dict`. The mapping is NOT re-redacted; callers are
        responsible for ensuring that persisted events are already
        redacted.
        """
        timestamp_raw = data.get("timestamp")
        if isinstance(timestamp_raw, str):
            try:
                timestamp = datetime.fromisoformat(timestamp_raw)
            except ValueError:
                timestamp = _utc_now()
        elif isinstance(timestamp_raw, datetime):
            timestamp = timestamp_raw
        else:
            timestamp = _utc_now()

        return cls(
            event_id=str(data.get("event_id") or secrets.token_hex(8)),
            event_type=AuditEventType(
                data.get("event_type", AuditEventType.OTHER.value)
            ),
            severity=AuditSeverity(
                data.get("severity", AuditSeverity.INFO.value)
            ),
            timestamp=timestamp,
            action=str(data.get("action") or ""),
            message=data.get("message"),
            operation_id=data.get("operation_id"),
            run_id=data.get("run_id"),
            provider=data.get("provider"),
            repository_id=data.get("repository_id"),
            workspace_id=data.get("workspace_id"),
            approval_id=data.get("approval_id"),
            actor=str(data.get("actor") or "system"),
            payload=dict(data.get("payload") or {}),
            error_code=data.get("error_code"),
            redacted=bool(data.get("redacted", False)),
        )


def _make_event(
    *,
    event_type: AuditEventType,
    severity: AuditSeverity = AuditSeverity.INFO,
    action: str = "",
    message: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    actor: str = "system",
    **context: Any,
) -> AuditEvent:
    """
    Build a redacted `AuditEvent` from raw inputs.

    The payload is passed through `redact_mapping` and the message
    through `redact_text` before the event is constructed. A flag is
    set on the event when redaction actually modified the input, so
    that downstream consumers can highlight suspicious events.
    """
    raw_payload = dict(payload or {})
    safe_payload = redact_mapping(raw_payload)
    safe_message = redact_text(message) if message else None

    # Detect whether redaction actually changed anything.
    was_redacted = safe_payload != raw_payload or (
        message is not None and safe_message != message
    )

    return AuditEvent(
        event_type=event_type,
        severity=severity,
        action=action,
        message=safe_message,
        payload=safe_payload,
        actor=actor,
        redacted=was_redacted,
        **context,
    )


# ---------------------------------------------------------------------------
# Audit logger interface
# ---------------------------------------------------------------------------


class AuditLogger(ABC):
    """
    Abstract audit logger.

    Implementations MUST be safe to call from multiple threads.
    Implementations MUST NOT raise from `record`; failures are logged
    at WARNING level and swallowed.
    """

    backend_name: str = "unknown"

    @abstractmethod
    def _record(self, event: AuditEvent) -> None:
        """Backend-specific storage. Called by `record`."""
        raise NotImplementedError

    def record(
        self,
        event_type: AuditEventType,
        *,
        severity: AuditSeverity = AuditSeverity.INFO,
        action: str = "",
        message: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        actor: str = "system",
        **context: Any,
    ) -> AuditEvent:
        """
        Create and store an audit event.

        Returns the created event so that callers may reference its
        ID. Never raises; on backend failure, a warning is logged and
        the event is still returned.
        """
        event = _make_event(
            event_type=event_type,
            severity=severity,
            action=action,
            message=message,
            payload=payload,
            actor=actor,
            **context,
        )
        try:
            self._record(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Audit logger '%s' failed to record event %s: %s",
                self.backend_name,
                event.event_id,
                type(exc).__name__,
            )
        return event

    def query(
        self,
        *,
        event_type: Optional[AuditEventType] = None,
        min_severity: Optional[AuditSeverity] = None,
        operation_id: Optional[str] = None,
        run_id: Optional[str] = None,
        provider: Optional[str] = None,
        repository_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 200,
    ) -> Sequence[AuditEvent]:
        """
        Return the events matching the given filters, newest first.

        The default implementation returns an empty tuple. Loggers
        that store events override this method.
        """
        return ()

    def clear(self) -> None:
        """
        Remove all stored events.

        Intended for tests. Production loggers MUST either implement
        this as a no-op or refuse; they MUST NOT delete on-disk
        events.
        """
        return None

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<{type(self).__name__} backend={self.backend_name!r}>"


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryAuditLogger(AuditLogger):
    """
    In-memory audit logger with a bounded ring buffer.

    When `maxlen` is reached, the oldest event is discarded. This is
    acceptable for tests and short-lived runs but MUST NOT be used as
    the sole audit store in production.
    """

    backend_name = "memory"

    def __init__(self, maxlen: int = 10_000) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be a positive integer.")
        self._events: Deque[AuditEvent] = deque(maxlen=maxlen)
        self._lock = threading.RLock()

    def _record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def query(
        self,
        *,
        event_type: Optional[AuditEventType] = None,
        min_severity: Optional[AuditSeverity] = None,
        operation_id: Optional[str] = None,
        run_id: Optional[str] = None,
        provider: Optional[str] = None,
        repository_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 200,
    ) -> Sequence[AuditEvent]:
        with self._lock:
            snapshot = tuple(self._events)

        return _filter_events(
            snapshot,
            event_type=event_type,
            min_severity=min_severity,
            operation_id=operation_id,
            run_id=run_id,
            provider=provider,
            repository_id=repository_id,
            since=since,
            until=until,
            limit=limit,
        )

    def all_events(self) -> Sequence[AuditEvent]:
        """Return a snapshot of all stored events, oldest first."""
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


# ---------------------------------------------------------------------------
# File-based implementation
# ---------------------------------------------------------------------------


class FileAuditLogger(AuditLogger):
    """
    Append-only JSONL audit logger.

    Each event is written as a single-line JSON object. When the file
    exceeds `max_bytes`, it is rotated: the current file is renamed to
    `<name>.1` and a new empty file is created. Older rotated files
    beyond `backup_count` are removed.

    The logger is safe for concurrent use within a single process.
    Cross-process concurrent writes are not supported and would require
    an OS file lock. For the local-first PELDRUN model this is
    acceptable.
    """

    backend_name = "file"

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = 10 * 1024 * 1024,  # 10 MiB
        backup_count: int = 5,
        create_parents: bool = True,
    ) -> None:
        self._path = Path(path).expanduser()
        self._max_bytes = max(1, int(max_bytes))
        self._backup_count = max(0, int(backup_count))
        self._lock = threading.RLock()
        if create_parents:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """Return the absolute path of the active audit file."""
        return self._path

    def _record(self, event: AuditEvent) -> None:
        line = json.dumps(event.to_dict(), sort_keys=True) + "\n"
        with self._lock:
            self._rotate_if_needed()
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync may fail on some filesystems; not fatal.
                    pass

    def _rotate_if_needed(self) -> None:
        if self._backup_count == 0:
            # Rotation disabled: truncate instead of growing forever.
            if self._path.exists() and self._path.stat().st_size >= self._max_bytes:
                self._path.write_text("", encoding="utf-8")
            return

        if not self._path.exists():
            return
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return

        # Shift rotated files: name.N -> name.N+1
        for index in range(self._backup_count - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{index}")
            dst = self._path.with_name(f"{self._path.name}.{index + 1}")
            if src.exists():
                try:
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
                except OSError:
                    logger.warning(
                        "Audit log rotation failed for %s", src
                    )

        # Rename active file to .1
        first_backup = self._path.with_name(f"{self._path.name}.1")
        try:
            if first_backup.exists():
                first_backup.unlink()
            self._path.rename(first_backup)
        except OSError:
            logger.warning(
                "Audit log rotation failed for %s", self._path
            )

    def query(
        self,
        *,
        event_type: Optional[AuditEventType] = None,
        min_severity: Optional[AuditSeverity] = None,
        operation_id: Optional[str] = None,
        run_id: Optional[str] = None,
        provider: Optional[str] = None,
        repository_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 200,
    ) -> Sequence[AuditEvent]:
        """
        Return matching events, newest first, up to `limit`.

        The current file is read line by line; rotated backups are
        not consulted. Callers that need a full historical view
        SHOULD read the rotated files directly.
        """
        if not self._path.exists():
            return ()

        events: list[AuditEvent] = []
        try:
            with self._lock, self._path.open(
                "r", encoding="utf-8"
            ) as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    try:
                        events.append(AuditEvent.from_dict(data))
                    except Exception:  # noqa: BLE001
                        continue
        except OSError:
            return ()

        return _filter_events(
            events,
            event_type=event_type,
            min_severity=min_severity,
            operation_id=operation_id,
            run_id=run_id,
            provider=provider,
            repository_id=repository_id,
            since=since,
            until=until,
            limit=limit,
        )

    def clear(self) -> None:
        """
        Truncate the active audit file.

        Rotated backups are left intact. This is intended for tests;
        production loggers SHOULD NOT call it.
        """
        with self._lock:
            if self._path.exists():
                self._path.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Null implementation
# ---------------------------------------------------------------------------


class NullAuditLogger(AuditLogger):
    """
    Audit logger that discards every event.

    Used when the audit subsystem is intentionally disabled, for
    example in unit tests that do not care about audit events.
    """

    backend_name = "null"

    def _record(self, event: AuditEvent) -> None:
        # Intentionally do nothing. Keep the reference alive to avoid
        # unused-variable warnings without side effects.
        _ = event


# ---------------------------------------------------------------------------
# Shared filtering
# ---------------------------------------------------------------------------


def _filter_events(
    events: Sequence[AuditEvent],
    *,
    event_type: Optional[AuditEventType] = None,
    min_severity: Optional[AuditSeverity] = None,
    operation_id: Optional[str] = None,
    run_id: Optional[str] = None,
    provider: Optional[str] = None,
    repository_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 200,
) -> Sequence[AuditEvent]:
    """
    Apply the standard filters to a sequence of events and return the
    newest `limit` matches.

    The comparison is inclusive for `since` and exclusive for `until`.
    """
    min_rank = (
        _SEVERITY_ORDER[min_severity] if min_severity is not None else None
    )

    matches: list[AuditEvent] = []
    for event in events:
        if event_type is not None and event.event_type != event_type:
            continue
        if min_rank is not None and _SEVERITY_ORDER[event.severity] < min_rank:
            continue
        if operation_id and event.operation_id != operation_id:
            continue
        if run_id and event.run_id != run_id:
            continue
        if provider and event.provider != provider:
            continue
        if repository_id and event.repository_id != repository_id:
            continue
        if since is not None and event.timestamp < since:
            continue
        if until is not None and event.timestamp >= until:
            continue
        matches.append(event)

    matches.sort(key=lambda e: e.timestamp, reverse=True)
    if limit > 0:
        matches = matches[:limit]
    return tuple(matches)


# ---------------------------------------------------------------------------
# Default logger singleton
# ---------------------------------------------------------------------------


_DEFAULT_LOGGER: Optional[AuditLogger] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_audit_logger() -> AuditLogger:
    """
    Return the process-wide default audit logger, creating it on first
    use.

    The default backend is `InMemoryAuditLogger`. Applications that
    need persistence SHOULD call `set_default_audit_logger` with a
    `FileAuditLogger` at startup.
    """
    global _DEFAULT_LOGGER
    with _DEFAULT_LOCK:
        if _DEFAULT_LOGGER is None:
            _DEFAULT_LOGGER = InMemoryAuditLogger()
        return _DEFAULT_LOGGER


def set_default_audit_logger(audit_logger: AuditLogger) -> None:
    """
    Replace the process-wide default audit logger.

    Intended for application startup and for tests.
    """
    if not isinstance(audit_logger, AuditLogger):
        raise TypeError(
            "set_default_audit_logger expects an AuditLogger instance."
        )
    global _DEFAULT_LOGGER
    with _DEFAULT_LOCK:
        _DEFAULT_LOGGER = audit_logger


def reset_default_audit_logger(
    maxlen: int = 10_000,
) -> AuditLogger:
    """
    Replace the default logger with a fresh in-memory logger.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_LOGGER
    with _DEFAULT_LOCK:
        _DEFAULT_LOGGER = InMemoryAuditLogger(maxlen=maxlen)
        return _DEFAULT_LOGGER
