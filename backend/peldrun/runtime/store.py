"""
backend/peldrun/runtime/store.py

PELDRUN Core Durable Execution Storage Engine.
Implements the central RunStore abstraction using SQLite with thread-safe operations.
Provides durable persistence for:
- Run lifecycle states and metadata
- ExecutionState checkpoints
- Canonical runtime events log
- HumanInputRequest approvals and idempotency tracking
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Union

from peldrun.engine.checkpoint import StateCheckpoint
from peldrun.engine.state import ExecutionState, ExecutionStatus
from peldrun.events.schema import PeldrunEvent
from peldrun.runtime.contract import HumanInputRequest, HumanInputStatus, RunRequest, RunStatus

logger = logging.getLogger("peldrun.runtime.store")


class RunStore(Protocol):
    """Protocol defining durable persistence requirements for PELDRUN runtime."""

    async def create_run(self, request: RunRequest) -> None: ...
    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]: ...
    async def update_run_status(self, run_id: str, status: RunStatus, metadata: Optional[Dict[str, Any]] = None) -> None: ...
    async def save_state(self, run_id: str, state: ExecutionState) -> None: ...
    async def load_state(self, run_id: str) -> Optional[ExecutionState]: ...
    async def save_checkpoint(self, checkpoint: StateCheckpoint, run_id: str) -> None: ...
    async def get_checkpoints(self, run_id: str) -> List[StateCheckpoint]: ...
    async def append_event(self, event: PeldrunEvent) -> None: ...
    async def get_events_after(self, run_id: str, sequence: int = 0) -> List[Dict[str, Any]]: ...
    async def save_human_request(self, request: HumanInputRequest) -> None: ...
    async def get_human_request(self, request_id: str) -> Optional[HumanInputRequest]: ...
    async def list_pending_human_requests(self, run_id: Optional[str] = None) -> List[HumanInputRequest]: ...
    async def resolve_human_request(self, request_id: str, answer: Any) -> bool: ...


class SqliteRunStore:
    """Thread-safe SQLite-backed durable storage engine for PELDRUN Core Runtime."""

    _instance: Optional[SqliteRunStore] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            # Default to storage root if available, otherwise local storage folder
            storage_dir = Path("storage").resolve()
            storage_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = storage_dir / "peldrun_runtime.db"
        else:
            self.db_path = Path(db_path).resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection configured with WAL mode."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize database schema tables if not existing."""
        conn = self._get_connection()
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    agent_name TEXT,
                    current_step INTEGER DEFAULT 0,
                    max_steps INTEGER DEFAULT 30,
                    state_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_job_id ON runs(job_id);

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    step INTEGER DEFAULT 0,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_run_seq ON run_events(run_id, sequence);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    label TEXT,
                    state_dump_json TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id ON checkpoints(run_id);

                CREATE TABLE IF NOT EXISTS human_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT,
                    tool_call_id TEXT,
                    question TEXT NOT NULL,
                    input_type TEXT DEFAULT 'text',
                    options_json TEXT DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    answer_json TEXT,
                    metadata_json TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_human_requests_run_status ON human_requests(run_id, status);
            """)

    # --------------------------------------------------------------------- Runs
    async def create_run(self, request: RunRequest) -> None:
        await asyncio.to_thread(self._sync_create_run, request)

    def _sync_create_run(self, request: RunRequest) -> None:
        conn = self._get_connection()
        now = time.time()
        run_id_str = str(request.run_id)
        with conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, job_id, status, prompt, agent_name, max_steps, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    prompt = excluded.prompt,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id_str,
                    request.job_id,
                    RunStatus.STARTING.value,
                    request.prompt,
                    request.agent_spec.name or request.agent_spec.id,
                    request.agent_spec.max_steps,
                    now,
                    now,
                ),
            )

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_get_run, run_id)

    def _sync_get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM runs WHERE run_id = ? OR job_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id, run_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)

    async def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        await asyncio.to_thread(self._sync_update_run_status, run_id, status, metadata)

    def _sync_update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        conn = self._get_connection()
        now = time.time()
        with conn:
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ? OR job_id = ?",
                (status.value, now, run_id, run_id),
            )

    async def save_state(self, run_id: str, state: ExecutionState) -> None:
        await asyncio.to_thread(self._sync_save_state, run_id, state)

    def _sync_save_state(self, run_id: str, state: ExecutionState) -> None:
        conn = self._get_connection()
        dump_str = json.dumps(state.to_snapshot_dict(), ensure_ascii=False)
        now = time.time()
        with conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, current_step = ?, state_json = ?, updated_at = ?
                WHERE run_id = ? OR job_id = ?
                """,
                (state.status.value, state.current_step, dump_str, now, run_id, run_id),
            )

    async def load_state(self, run_id: str) -> Optional[ExecutionState]:
        return await asyncio.to_thread(self._sync_load_state, run_id)

    def _sync_load_state(self, run_id: str) -> Optional[ExecutionState]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT state_json FROM runs WHERE run_id = ? OR job_id = ? ORDER BY updated_at DESC LIMIT 1",
            (run_id, run_id),
        )
        row = cursor.fetchone()
        if not row or not row["state_json"]:
            return None
        try:
            data = json.loads(row["state_json"])
            return ExecutionState.from_snapshot_dict(data)
        except Exception as ex:
            logger.error("Failed to reconstruct ExecutionState for run %s: %s", run_id, ex)
            return None

    # -------------------------------------------------------------- Checkpoints
    async def save_checkpoint(self, checkpoint: StateCheckpoint, run_id: str) -> None:
        await asyncio.to_thread(self._sync_save_checkpoint, checkpoint, run_id)

    def _sync_save_checkpoint(self, checkpoint: StateCheckpoint, run_id: str) -> None:
        conn = self._get_connection()
        now = time.time()
        dump_str = json.dumps(checkpoint.state_data, ensure_ascii=False)
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints (checkpoint_id, run_id, step, label, state_dump_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (checkpoint.id, run_id, checkpoint.step, checkpoint.label, dump_str, now),
            )

    async def get_checkpoints(self, run_id: str) -> List[StateCheckpoint]:
        return await asyncio.to_thread(self._sync_get_checkpoints, run_id)

    def _sync_get_checkpoints(self, run_id: str) -> List[StateCheckpoint]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY step ASC, timestamp ASC",
            (run_id,),
        )
        records: List[StateCheckpoint] = []
        for row in cursor.fetchall():
            records.append(
                StateCheckpoint(
                    id=row["checkpoint_id"],
                    step=row["step"],
                    label=row["label"] or "",
                    state_data=json.loads(row["state_dump_json"]),
                )
            )
        return records

    # ------------------------------------------------------------------- Events
    async def append_event(self, event: PeldrunEvent) -> None:
        await asyncio.to_thread(self._sync_append_event, event)

    def _sync_append_event(self, event: PeldrunEvent) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO run_events (event_id, run_id, sequence, step, type, payload_json, metadata_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(event.run_id),
                    event.sequence,
                    event.step,
                    event.type.value,
                    json.dumps(event.payload, ensure_ascii=False),
                    json.dumps(event.metadata, ensure_ascii=False),
                    event.timestamp,
                ),
            )

    async def get_events_after(self, run_id: str, sequence: int = 0) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_get_events_after, run_id, sequence)

    def _sync_get_events_after(self, run_id: str, sequence: int = 0) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM run_events
            WHERE (run_id = ? OR run_id IN (SELECT run_id FROM runs WHERE job_id = ?))
              AND sequence > ?
            ORDER BY sequence ASC
            """,
            (run_id, run_id, sequence),
        )
        events: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            events.append({
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "sequence": row["sequence"],
                "step": row["step"],
                "type": row["type"],
                "payload": json.loads(row["payload_json"]),
                "metadata": json.loads(row["metadata_json"]),
                "timestamp": row["timestamp"],
            })
        return events

    # ------------------------------------------------------------- Human Input
    async def save_human_request(self, request: HumanInputRequest) -> None:
        await asyncio.to_thread(self._sync_save_human_request, request)

    def _sync_save_human_request(self, request: HumanInputRequest) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO human_requests (
                    request_id, run_id, step_id, tool_call_id, question, input_type,
                    options_json, status, created_at, expires_at, answer_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.run_id,
                    request.step_id,
                    request.tool_call_id,
                    request.question,
                    request.input_type,
                    json.dumps(request.options, ensure_ascii=False),
                    request.status.value,
                    request.created_at,
                    request.expires_at,
                    json.dumps(request.answer, ensure_ascii=False) if request.answer is not None else None,
                    json.dumps(request.metadata, ensure_ascii=False),
                ),
            )

    async def get_human_request(self, request_id: str) -> Optional[HumanInputRequest]:
        return await asyncio.to_thread(self._sync_get_human_request, request_id)

    def _sync_get_human_request(self, request_id: str) -> Optional[HumanInputRequest]:
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM human_requests WHERE request_id = ?", (request_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_human_request(row)

    async def list_pending_human_requests(self, run_id: Optional[str] = None) -> List[HumanInputRequest]:
        return await asyncio.to_thread(self._sync_list_pending_human_requests, run_id)

    def _sync_list_pending_human_requests(self, run_id: Optional[str] = None) -> List[HumanInputRequest]:
        conn = self._get_connection()
        if run_id:
            cursor = conn.execute(
                """
                SELECT * FROM human_requests
                WHERE (run_id = ? OR run_id IN (SELECT run_id FROM runs WHERE job_id = ?))
                  AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (run_id, run_id),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM human_requests WHERE status = 'pending' ORDER BY created_at ASC"
            )
        return [self._row_to_human_request(row) for row in cursor.fetchall()]

    async def resolve_human_request(self, request_id: str, answer: Any) -> bool:
        """Atomically resolve a pending human input request with idempotent validation."""
        return await asyncio.to_thread(self._sync_resolve_human_request, request_id, answer)

    def _sync_resolve_human_request(self, request_id: str, answer: Any) -> bool:
        conn = self._get_connection()
        answer_dump = json.dumps(answer, ensure_ascii=False)
        with conn:
            # Atomic update enforcing condition: must be in 'pending' status
            cursor = conn.execute(
                """
                UPDATE human_requests
                SET status = 'answered', answer_json = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (answer_dump, request_id),
            )
            return cursor.rowcount > 0

    def _row_to_human_request(self, row: sqlite3.Row) -> HumanInputRequest:
        ans = None
        if row["answer_json"] is not None:
            try:
                ans = json.loads(row["answer_json"])
            except Exception:
                ans = row["answer_json"]

        return HumanInputRequest(
            request_id=row["request_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            tool_call_id=row["tool_call_id"],
            question=row["question"],
            input_type=row["input_type"],
            options=json.loads(row["options_json"] or "[]"),
            status=HumanInputStatus(row["status"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            answer=ans,
            metadata=json.loads(row["metadata_json"] or "{}"),
        )


def get_run_store(db_path: Optional[Union[str, Path]] = None) -> SqliteRunStore:
    """Singleton provider returning global thread-safe SqliteRunStore instance."""
    with SqliteRunStore._lock:
        if SqliteRunStore._instance is None:
            SqliteRunStore._instance = SqliteRunStore(db_path=db_path)
        return SqliteRunStore._instance