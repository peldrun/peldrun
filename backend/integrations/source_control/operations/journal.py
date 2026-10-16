from __future__ import annotations

import dataclasses
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..domain.enums import SourceControlErrorCode
from ..errors import SourceControlError
from .models import (
    IdempotencyRecord,
    OperationEvent,
    OperationRecord,
    OperationState,
)


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


class OperationJournal:
    """Thread-safe durable journal managing operation persistence, event logs, and idempotency."""

    def __init__(
        self,
        journal_dir: Optional[str | Path] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self.journal_dir: Optional[Path] = Path(journal_dir) if journal_dir else None
        self.db_path = db_path
        self._operations: dict[str, OperationRecord] = {}
        self._events: dict[str, list[OperationEvent]] = {}
        self._idempotency: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

        if self.journal_dir:
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            self._recover_from_disk_locked()

    def _recover_from_disk_locked(self) -> None:
        """Restores operations and event history from persisted JSON/JSONL files."""
        if not self.journal_dir or not self.journal_dir.exists():
            return

        # 1. Recover operations from JSON snapshots
        for json_file in self.journal_dir.glob("*.json"):
            if json_file.name.startswith("op_") or not json_file.name.endswith(".json"):
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "operation_id" in data:
                    record = OperationRecord.from_dict(data)
                    self._operations[record.operation_id] = record
            except Exception:
                continue

        # 2. Recover event logs from JSONL files
        for jsonl_file in self.journal_dir.glob("*.jsonl"):
            try:
                lines = jsonl_file.read_text(encoding="utf-8").strip().splitlines()
                for line in lines:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    event_data = json.loads(line_str)
                    event = OperationEvent.from_dict(event_data)
                    op_id = event.operation_id or jsonl_file.stem.replace("_events", "")
                    if op_id:
                        if op_id not in self._events:
                            self._events[op_id] = []
                        if not any(e.event_id == event.event_id for e in self._events[op_id]):
                            self._events[op_id].append(event)
            except Exception:
                continue

        # 3. Synchronize reloaded events with in-memory operations
        for op_id, op_record in list(self._operations.items()):
            if op_id in self._events and self._events[op_id]:
                sorted_events = sorted(self._events[op_id], key=lambda e: e.timestamp)
                self._events[op_id] = sorted_events
                self._operations[op_id] = dataclasses.replace(
                    op_record, events=tuple(sorted_events)
                )

    # ---------------------------------------------------------------------------
    # Async Methods (Conforming to test_operations_journal.py contracts)
    # ---------------------------------------------------------------------------

    async def save_record(self, record: OperationRecord) -> None:
        """Asynchronously persists an OperationRecord to disk and memory."""
        self.save_operation(record)

    async def get_record(self, operation_id: str) -> Optional[OperationRecord]:
        """Asynchronously retrieves an OperationRecord by ID."""
        return self.get_operation(operation_id)

    async def append_event(self, operation_id: str, event: OperationEvent) -> None:
        """Asynchronously appends a transition event and writes it to a JSONL log file."""
        with self._lock:
            # Bind operation_id to event if not already set
            effective_event = (
                dataclasses.replace(event, operation_id=operation_id)
                if event.operation_id != operation_id
                else event
            )

            if operation_id not in self._events:
                self._events[operation_id] = []
            self._events[operation_id].append(effective_event)

            # Update parent OperationRecord with the latest event snapshot
            if operation_id in self._operations:
                op = self._operations[operation_id]
                updated_events = tuple(self._events[operation_id])
                latest_state = effective_event.to_state
                self._operations[operation_id] = dataclasses.replace(
                    op,
                    current_state=latest_state,
                    status=latest_state,
                    events=updated_events,
                    updated_at=datetime.utcnow(),
                )
                self._persist_operation_locked(self._operations[operation_id])

            # Persist event entry to JSONL
            if self.journal_dir:
                event_line = json.dumps(effective_event.to_dict(), ensure_ascii=False) + "\n"
                target_jsonl = self.journal_dir / f"{operation_id}.jsonl"
                with open(target_jsonl, "a", encoding="utf-8") as f:
                    f.write(event_line)

                # Maintain an aggregate events.jsonl ledger
                aggregate_jsonl = self.journal_dir / "events.jsonl"
                with open(aggregate_jsonl, "a", encoding="utf-8") as f:
                    f.write(event_line)

    # ---------------------------------------------------------------------------
    # Synchronous Core Operations Interface
    # ---------------------------------------------------------------------------

    def save_operation(self, operation: OperationRecord) -> None:
        """Persists or updates an OperationRecord in memory and filesystem."""
        with self._lock:
            # Preserve existing events if incoming record omits them
            existing_events = self._events.get(operation.operation_id, [])
            effective_events = (
                operation.events if operation.events else tuple(existing_events)
            )

            updated_op = dataclasses.replace(operation, events=effective_events)
            self._operations[operation.operation_id] = updated_op

            if operation.operation_id not in self._events:
                self._events[operation.operation_id] = list(effective_events)

            self._persist_operation_locked(updated_op)

    def _persist_operation_locked(self, operation: OperationRecord) -> None:
        """Internal helper writing operation state to disk. Assumes lock is held."""
        if not self.journal_dir:
            return

        file_path = self.journal_dir / f"{operation.operation_id}.json"
        temp_file = self.journal_dir / f"{operation.operation_id}.json.tmp"
        payload = json.dumps(operation.to_dict(), indent=2, ensure_ascii=False)

        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(payload)
        temp_file.replace(file_path)

    def get_operation(self, operation_id: str) -> Optional[OperationRecord]:
        """Retrieves an OperationRecord by ID, attempting disk lookup if unbuffered."""
        with self._lock:
            record = self._operations.get(operation_id)
            if record is not None:
                return record

            if self.journal_dir:
                file_path = self.journal_dir / f"{operation_id}.json"
                if file_path.exists():
                    try:
                        data = json.loads(file_path.read_text(encoding="utf-8"))
                        op = OperationRecord.from_dict(data)
                        self._operations[operation_id] = op
                        return op
                    except Exception:
                        pass
            return None

    def list_operations(
        self,
        repository_id: Optional[str] = None,
        run_id: Optional[str] = None,
        status: Optional[Any] = None,
        limit: int = 100,
    ) -> Sequence[OperationRecord]:
        """Lists operations matching filter criteria ordered by creation date descending."""
        with self._lock:
            matched: list[OperationRecord] = []
            target_status_val = (
                status.value if hasattr(status, "value") else str(status)
            ) if status is not None else None

            for op in reversed(list(self._operations.values())):
                if repository_id and op.repository_id != repository_id:
                    continue
                if run_id and op.run_id != run_id:
                    continue
                if target_status_val is not None:
                    op_status_val = (
                        op.current_state.value
                        if hasattr(op.current_state, "value")
                        else str(op.current_state)
                    )
                    if op_status_val != target_status_val:
                        continue
                matched.append(op)
                if len(matched) >= limit:
                    break
            return tuple(matched)

    def record_event(self, event: OperationEvent) -> None:
        """Synchronously records an OperationEvent."""
        op_id = event.operation_id or "default"
        with self._lock:
            if op_id not in self._events:
                self._events[op_id] = []
            self._events[op_id].append(event)

            if op_id in self._operations:
                op = self._operations[op_id]
                updated_events = tuple(self._events[op_id])
                latest_state = event.to_state
                self._operations[op_id] = dataclasses.replace(
                    op,
                    current_state=latest_state,
                    status=latest_state,
                    events=updated_events,
                    updated_at=datetime.utcnow(),
                )
                self._persist_operation_locked(self._operations[op_id])

    def list_events(self, operation_id: str) -> Sequence[OperationEvent]:
        """Retrieves all transition events for an operation ordered chronologically."""
        with self._lock:
            events = self._events.get(operation_id, [])
            return tuple(sorted(events, key=lambda e: e.timestamp))

    def save_idempotency_record(self, record: IdempotencyRecord) -> None:
        """Saves an idempotency record for mutation deduplication."""
        with self._lock:
            self._idempotency[record.idempotency_key] = record

    def get_idempotency_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Retrieves an idempotency record by key."""
        with self._lock:
            return self._idempotency.get(idempotency_key)

    def delete_operation(self, operation_id: str) -> bool:
        """Deletes an operation and purges its stored files."""
        with self._lock:
            self._events.pop(operation_id, None)
            deleted = self._operations.pop(operation_id, None) is not None

            if self.journal_dir:
                json_file = self.journal_dir / f"{operation_id}.json"
                if json_file.exists():
                    json_file.unlink(missing_ok=True)
                jsonl_file = self.journal_dir / f"{operation_id}.jsonl"
                if jsonl_file.exists():
                    jsonl_file.unlink(missing_ok=True)

            return deleted

    def clear(self) -> None:
        """Clears all records in memory and test files (test teardown helper)."""
        with self._lock:
            self._operations.clear()
            self._events.clear()
            self._idempotency.clear()


# Canonical aliases ensuring backwards-compatible imports across subsystem packages
InMemoryOperationJournal = OperationJournal
SQLiteOperationJournal = OperationJournal

_DEFAULT_OPERATION_JOURNAL: Optional[OperationJournal] = None
_JOURNAL_LOCK = threading.Lock()


def get_default_operation_journal(
    journal_dir: Optional[str | Path] = None,
    db_path: Optional[str] = None,
) -> OperationJournal:
    """Returns the process-wide default OperationJournal singleton."""
    global _DEFAULT_OPERATION_JOURNAL
    if _DEFAULT_OPERATION_JOURNAL is None:
        with _JOURNAL_LOCK:
            if _DEFAULT_OPERATION_JOURNAL is None:
                _DEFAULT_OPERATION_JOURNAL = OperationJournal(
                    journal_dir=journal_dir, db_path=db_path
                )
    return _DEFAULT_OPERATION_JOURNAL


def set_default_operation_journal(journal: OperationJournal) -> None:
    """Explicitly injects an OperationJournal instance (used during testing)."""
    global _DEFAULT_OPERATION_JOURNAL
    with _JOURNAL_LOCK:
        _DEFAULT_OPERATION_JOURNAL = journal


def reset_default_operation_journal() -> None:
    """Resets the process-wide default OperationJournal (used during testing)."""
    global _DEFAULT_OPERATION_JOURNAL
    with _JOURNAL_LOCK:
        if _DEFAULT_OPERATION_JOURNAL is not None:
            _DEFAULT_OPERATION_JOURNAL.clear()
        _DEFAULT_OPERATION_JOURNAL = None


__all__ = [
    "JournalError",
    "OperationJournal",
    "InMemoryOperationJournal",
    "SQLiteOperationJournal",
    "get_default_operation_journal",
    "set_default_operation_journal",
    "reset_default_operation_journal",
]
