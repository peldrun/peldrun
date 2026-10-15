from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import OperationStatus, SourceControlErrorCode
from ..errors import SourceControlError
from .models import IdempotencyRecord, OperationEvent, OperationRecord


class JournalError(SourceControlError):
    """Raised when an operation against the journal persistence fails."""

    def __init__(
        self,
        message: str,
        *,
        details: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            retryable=False,
            details=details,
        )


@runtime_checkable
class OperationJournal(Protocol):
    """Abstract port for durable persistence of operations, transition events, and idempotency records."""

    def save_operation(self, operation: OperationRecord) -> None:
        """Persists or updates an OperationRecord."""
        ...

    def get_operation(self, operation_id: str) -> Optional[OperationRecord]:
        """Retrieves an OperationRecord by ID. Returns None if not found."""
        ...

    def list_operations(
        self,
        repository_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[OperationStatus] = None,
        limit: int = 100,
    ) -> Sequence[OperationRecord]:
        """Lists operations matching filter criteria ordered by creation date descending."""
        ...

    def record_event(self, event: OperationEvent) -> None:
        """Appends an event to the transition journal."""
        ...

    def list_events(self, operation_id: str) -> Sequence[OperationEvent]:
        """Retrieves all transition events for an operation ordered chronologically."""
        ...

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        """Saves an idempotency record for mutation deduplication."""
        ...

    def get_idempotency_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Retrieves an idempotency record by key."""
        ...

    def delete_operation(self, operation_id: str) -> bool:
        """Deletes an operation and its associated events."""
        ...


class InMemoryOperationJournal:
    """Thread-safe in-memory journal intended for isolated testing and volatile executions."""

    def __init__(self) -> None:
        self._operations: dict[str, OperationRecord] = {}
        self._events: dict[str, list[OperationEvent]] = {}
        self._idempotency: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    def save_operation(self, operation: OperationRecord) -> None:
        with self._lock:
            self._operations[operation.operation_id] = operation
            if operation.operation_id not in self._events:
                self._events[operation.operation_id] = []

    def get_operation(self, operation_id: str) -> Optional[OperationRecord]:
        with self._lock:
            return self._operations.get(operation_id)

    def list_operations(
        self,
        repository_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[OperationStatus] = None,
        limit: int = 100,
    ) -> Sequence[OperationRecord]:
        with self._lock:
            matched: list[OperationRecord] = []
            for op in reversed(list(self._operations.values())):
                if repository_id and op.repository_id != repository_id:
                    continue
                if run_id and op.run_id != run_id:
                    continue
                if status and op.status != status:
                    continue
                matched.append(op)
                if len(matched) >= limit:
                    break
            return tuple(matched)

    def record_event(self, event: OperationEvent) -> None:
        with self._lock:
            if event.operation_id not in self._events:
                self._events[event.operation_id] = []
            self._events[event.operation_id].append(event)

    def list_events(self, operation_id: str) -> Sequence[OperationEvent]:
        with self._lock:
            return tuple(self._events.get(operation_id, []))

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._idempotency[record.idempotency_key] = record

    def get_idempotency_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        with self._lock:
            return self._idempotency.get(idempotency_key)

    def delete_operation(self, operation_id: str) -> bool:
        with self._lock:
            self._events.pop(operation_id, None)
            return self._operations.pop(operation_id, None) is not None

    def clear(self) -> None:
        """Clears all in-memory records (isolated test teardown helper)."""
        with self._lock:
            self._operations.clear()
            self._events.clear()
            self._idempotency.clear()


class SQLiteOperationJournal:
    """Persistent, thread-safe SQLite implementation of OperationJournal."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS sc_operations (
                            operation_id TEXT PRIMARY KEY,
                            repository_id TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            kind TEXT NOT NULL,
                            status TEXT NOT NULL,
                            run_id TEXT,
                            source_branch TEXT,
                            target_branch TEXT,
                            base_sha TEXT,
                            head_sha TEXT,
                            diff_sha256 TEXT,
                            workspace_id TEXT,
                            approval_id TEXT,
                            commit_message TEXT,
                            change_request_title TEXT,
                            change_request_body TEXT,
                            change_request_url TEXT,
                            error_details TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            completed_at TEXT,
                            metadata TEXT
                        );
                        CREATE INDEX IF NOT EXISTS idx_sc_ops_repo ON sc_operations(repository_id);
                        CREATE INDEX IF NOT EXISTS idx_sc_ops_run ON sc_operations(run_id);
                        CREATE INDEX IF NOT EXISTS idx_sc_ops_status ON sc_operations(status);

                        CREATE TABLE IF NOT EXISTS sc_operation_events (
                            event_id TEXT PRIMARY KEY,
                            operation_id TEXT NOT NULL,
                            from_status TEXT,
                            to_status TEXT NOT NULL,
                            message TEXT NOT NULL,
                            timestamp TEXT NOT NULL,
                            details TEXT,
                            FOREIGN KEY(operation_id) REFERENCES sc_operations(operation_id) ON DELETE CASCADE
                        );
                        CREATE INDEX IF NOT EXISTS idx_sc_events_op ON sc_operation_events(operation_id);

                        CREATE TABLE IF NOT EXISTS sc_idempotency (
                            idempotency_key TEXT PRIMARY KEY,
                            operation_id TEXT NOT NULL,
                            action_type TEXT NOT NULL,
                            request_hash TEXT NOT NULL,
                            response_payload TEXT,
                            created_at TEXT NOT NULL,
                            expires_at TEXT
                        );
                        CREATE INDEX IF NOT EXISTS idx_sc_idem_op ON sc_idempotency(operation_id);
                        """
                    )
            finally:
                conn.close()

    def save_operation(self, operation: OperationRecord) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO sc_operations (
                            operation_id, repository_id, provider, kind, status, run_id,
                            source_branch, target_branch, base_sha, head_sha, diff_sha256,
                            workspace_id, approval_id, commit_message, change_request_title,
                            change_request_body, change_request_url, error_details,
                            created_at, updated_at, completed_at, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(operation_id) DO UPDATE SET
                            repository_id=excluded.repository_id,
                            provider=excluded.provider,
                            kind=excluded.kind,
                            status=excluded.status,
                            run_id=excluded.run_id,
                            source_branch=excluded.source_branch,
                            target_branch=excluded.target_branch,
                            base_sha=excluded.base_sha,
                            head_sha=excluded.head_sha,
                            diff_sha256=excluded.diff_sha256,
                            workspace_id=excluded.workspace_id,
                            approval_id=excluded.approval_id,
                            commit_message=excluded.commit_message,
                            change_request_title=excluded.change_request_title,
                            change_request_body=excluded.change_request_body,
                            change_request_url=excluded.change_request_url,
                            error_details=excluded.error_details,
                            updated_at=excluded.updated_at,
                            completed_at=excluded.completed_at,
                            metadata=excluded.metadata;
                        """,
                        (
                            operation.operation_id,
                            operation.repository_id,
                            operation.provider.value,
                            operation.kind.value,
                            operation.status.value,
                            operation.run_id,
                            operation.source_branch,
                            operation.target_branch,
                            operation.base_sha,
                            operation.head_sha,
                            operation.diff_sha256,
                            operation.workspace_id,
                            operation.approval_id,
                            operation.commit_message,
                            operation.change_request_title,
                            operation.change_request_body,
                            operation.change_request_url,
                            json.dumps(operation.error_details) if operation.error_details else None,
                            operation.created_at.isoformat(),
                            operation.updated_at.isoformat(),
                            operation.completed_at.isoformat() if operation.completed_at else None,
                            json.dumps(operation.metadata),
                        ),
                    )
            finally:
                conn.close()

    def get_operation(self, operation_id: str) -> Optional[OperationRecord]:
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM sc_operations WHERE operation_id = ?;", (operation_id,)
                ).fetchone()
                if not row:
                    return None
                return self._row_to_operation(row)
            finally:
                conn.close()

    def list_operations(
        self,
        repository_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[OperationStatus] = None,
        limit: int = 100,
    ) -> Sequence[OperationRecord]:
        with self._lock:
            conn = self._get_connection()
            try:
                query = "SELECT * FROM sc_operations WHERE 1=1"
                params: list[object] = []
                if repository_id:
                    query += " AND repository_id = ?"
                    params.append(repository_id)
                if run_id:
                    query += " AND run_id = ?"
                    params.append(run_id)
                if status:
                    query += " AND status = ?"
                    params.append(status.value)
                query += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, tuple(params)).fetchall()
                return tuple(self._row_to_operation(r) for r in rows)
            finally:
                conn.close()

    def record_event(self, event: OperationEvent) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO sc_operation_events (
                            event_id, operation_id, from_status, to_status, message, timestamp, details
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            event.event_id,
                            event.operation_id,
                            event.from_status.value if event.from_status else None,
                            event.to_status.value,
                            event.message,
                            event.timestamp.isoformat(),
                            json.dumps(event.details),
                        ),
                    )
            finally:
                conn.close()

    def list_events(self, operation_id: str) -> Sequence[OperationEvent]:
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM sc_operation_events WHERE operation_id = ? ORDER BY timestamp ASC;",
                    (operation_id,),
                ).fetchall()
                events: list[OperationEvent] = []
                for r in rows:
                    events.append(
                        OperationEvent(
                            event_id=r["event_id"],
                            operation_id=r["operation_id"],
                            from_status=OperationStatus(r["from_status"]) if r["from_status"] else None,
                            to_status=OperationStatus(r["to_status"]),
                            message=r["message"],
                            timestamp=datetime.fromisoformat(r["timestamp"]),
                            details=json.loads(r["details"]) if r["details"] else {},
                        )
                    )
                return tuple(events)
            finally:
                conn.close()

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO sc_idempotency (
                            idempotency_key, operation_id, action_type, request_hash,
                            response_payload, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(idempotency_key) DO UPDATE SET
                            operation_id=excluded.operation_id,
                            action_type=excluded.action_type,
                            request_hash=excluded.request_hash,
                            response_payload=excluded.response_payload,
                            expires_at=excluded.expires_at;
                        """,
                        (
                            record.idempotency_key,
                            record.operation_id,
                            record.action_type,
                            record.request_hash,
                            json.dumps(record.response_payload) if record.response_payload else None,
                            record.created_at.isoformat(),
                            record.expires_at.isoformat() if record.expires_at else None,
                        ),
                    )
            finally:
                conn.close()

    def get_idempotency_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM sc_idempotency WHERE idempotency_key = ?;", (idempotency_key,)
                ).fetchone()
                if not row:
                    return None
                return IdempotencyRecord(
                    idempotency_key=row["idempotency_key"],
                    operation_id=row["operation_id"],
                    action_type=row["action_type"],
                    request_hash=row["request_hash"],
                    response_payload=json.loads(row["response_payload"]) if row["response_payload"] else None,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
                )
            finally:
                conn.close()

    def delete_operation(self, operation_id: str) -> bool:
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM sc_operations WHERE operation_id = ?;", (operation_id,)
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    @staticmethod
    def _row_to_operation(row: sqlite3.Row) -> OperationRecord:
        data = {
            "operation_id": row["operation_id"],
            "repository_id": row["repository_id"],
            "provider": row["provider"],
            "kind": row["kind"],
            "status": row["status"],
            "run_id": row["run_id"],
            "source_branch": row["source_branch"],
            "target_branch": row["target_branch"],
            "base_sha": row["base_sha"],
            "head_sha": row["head_sha"],
            "diff_sha256": row["diff_sha256"],
            "workspace_id": row["workspace_id"],
            "approval_id": row["approval_id"],
            "commit_message": row["commit_message"],
            "change_request_title": row["change_request_title"],
            "change_request_body": row["change_request_body"],
            "change_request_url": row["change_request_url"],
            "error_details": json.loads(row["error_details"]) if row["error_details"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
        }
        return OperationRecord.from_dict(data)


# ============================================================
# Default Journal Singleton Accessors
# ============================================================

_DEFAULT_JOURNAL: Optional[OperationJournal] = None
_JOURNAL_LOCK = threading.Lock()


def get_default_operation_journal(db_path: Optional[str] = None) -> OperationJournal:
    """Returns the process-wide default OperationJournal singleton."""
    global _DEFAULT_JOURNAL
    if _DEFAULT_JOURNAL is not None:
        return _DEFAULT_JOURNAL

    with _JOURNAL_LOCK:
        if _DEFAULT_JOURNAL is not None:
            return _DEFAULT_JOURNAL

        if os.environ.get("PELDRUN_TEST_JOURNAL") == "1" or not db_path:
            # Fallback to in-memory if explicitly requested or path not provided
            _DEFAULT_JOURNAL = InMemoryOperationJournal()
        else:
            _DEFAULT_JOURNAL = SQLiteOperationJournal(db_path)

        return _DEFAULT_JOURNAL


def set_default_operation_journal(journal: OperationJournal) -> None:
    """Explicitly injects an OperationJournal instance (used during testing)."""
    global _DEFAULT_JOURNAL
    with _JOURNAL_LOCK:
        _DEFAULT_JOURNAL = journal


def reset_default_operation_journal() -> None:
    """Resets the process-wide default OperationJournal (used during testing)."""
    global _DEFAULT_JOURNAL
    with _JOURNAL_LOCK:
        _DEFAULT_JOURNAL = None
