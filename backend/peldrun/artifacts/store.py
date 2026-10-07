"""
backend/peldrun/artifacts/store.py

PELDRUN Core Durable Artifact Storage Engine.

Provides persistent, thread-safe SQLite storage for workspace deliverables:
- Tracks cumulative ArtifactManifest state per workspace/chat.
- Implements immutable, append-only artifact_history audit logging.
- Persists content checksums (SHA-256), monotonically increasing revisions, and source tools.
- Guarantees complete artifact state recovery across process restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from peldrun.artifacts.manifest import ArtifactManifest, ArtifactRef, ArtifactType

logger = logging.getLogger("peldrun.artifacts.store")


class SqliteArtifactStore:
    """Thread-safe SQLite storage repository for workspace artifact manifests and revisions."""

    _instance: Optional[SqliteArtifactStore] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
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
        """Initialize artifact tables and performance composite indexes."""
        conn = self._get_connection()
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS artifact_manifests (
                    workspace_id TEXT PRIMARY KEY,
                    workspace_root TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifact_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT,
                    job_id TEXT,
                    chat_id TEXT,
                    name TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    source_tool TEXT,
                    sha256 TEXT,
                    metadata_json TEXT DEFAULT '{}',
                    timestamp REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_artifact_history_workspace 
                    ON artifact_history(workspace_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_artifact_history_chat 
                    ON artifact_history(chat_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_artifact_history_path 
                    ON artifact_history(workspace_id, relative_path);
                CREATE INDEX IF NOT EXISTS idx_artifact_history_run 
                    ON artifact_history(run_id);
            """)

    # ---------------------------------------------------------------- Manifest State
    async def save_manifest(self, workspace_id: str, manifest: ArtifactManifest) -> None:
        """Persist or update the cumulative artifact manifest for a workspace."""
        await asyncio.to_thread(self._sync_save_manifest, workspace_id, manifest)

    def _sync_save_manifest(self, workspace_id: str, manifest: ArtifactManifest) -> None:
        conn = self._get_connection()
        manifest_str = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False)
        now = time.time()
        with conn:
            conn.execute(
                """
                INSERT INTO artifact_manifests (workspace_id, workspace_root, manifest_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    workspace_root = excluded.workspace_root,
                    manifest_json = excluded.manifest_json,
                    updated_at = excluded.updated_at
                """,
                (workspace_id, manifest.workspace_root, manifest_str, now),
            )

    async def load_manifest(self, workspace_id: str) -> Optional[ArtifactManifest]:
        """Reconstruct the canonical ArtifactManifest from SQLite across process restarts."""
        return await asyncio.to_thread(self._sync_load_manifest, workspace_id)

    def _sync_load_manifest(self, workspace_id: str) -> Optional[ArtifactManifest]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT manifest_json FROM artifact_manifests WHERE workspace_id = ?",
            (workspace_id,),
        )
        row = cursor.fetchone()
        if not row or not row["manifest_json"]:
            return None
        try:
            data = json.loads(row["manifest_json"])
            return ArtifactManifest.model_validate(data)
        except Exception as exc:
            logger.error("Failed to deserialize ArtifactManifest for workspace %s: %s", workspace_id, exc)
            return None

    # ---------------------------------------------------------------- Revision History
    async def record_artifact_event(
        self,
        artifact: ArtifactRef,
        workspace_id: str,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> int:
        """Append an immutable mutation fact to the artifact_history ledger."""
        return await asyncio.to_thread(
            self._sync_record_artifact_event, artifact, workspace_id, run_id, job_id, chat_id
        )

    def _sync_record_artifact_event(
        self,
        artifact: ArtifactRef,
        workspace_id: str,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> int:
        conn = self._get_connection()
        meta_str = json.dumps(artifact.metadata, ensure_ascii=False)
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO artifact_history (
                    artifact_id, workspace_id, run_id, job_id, chat_id,
                    name, relative_path, artifact_type, mime_type,
                    size_bytes, revision, operation, source_tool,
                    sha256, metadata_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    workspace_id,
                    run_id,
                    job_id,
                    chat_id,
                    artifact.name,
                    artifact.relative_path,
                    artifact.artifact_type.value,
                    artifact.mime_type,
                    artifact.size_bytes,
                    artifact.revision,
                    artifact.operation,
                    artifact.source_tool,
                    artifact.sha256,
                    meta_str,
                    artifact.updated_at or time.time(),
                ),
            )
            return int(cursor.lastrowid or 0)

    async def list_artifact_history(
        self,
        workspace_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        relative_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve audit history of artifact revisions matching given filters."""
        return await asyncio.to_thread(
            self._sync_list_artifact_history, workspace_id, chat_id, relative_path
        )

    def _sync_list_artifact_history(
        self,
        workspace_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        relative_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        conditions = ["1=1"]
        params: List[Any] = []

        if workspace_id:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if chat_id:
            conditions.append("chat_id = ?")
            params.append(chat_id)
        if relative_path:
            norm_path = relative_path.replace("\\", "/").strip("/")
            conditions.append("relative_path = ?")
            params.append(norm_path)

        query = f"""
            SELECT * FROM artifact_history
            WHERE {' AND '.join(conditions)}
            ORDER BY timestamp ASC, history_id ASC
        """
        cursor = conn.execute(query, params)
        records = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
            records.append(item)
        return records


def get_artifact_store(db_path: Optional[Union[str, Path]] = None) -> SqliteArtifactStore:
    """Singleton provider returning global thread-safe SqliteArtifactStore instance."""
    with SqliteArtifactStore._lock:
        if SqliteArtifactStore._instance is None:
            SqliteArtifactStore._instance = SqliteArtifactStore(db_path=db_path)
        return SqliteArtifactStore._instance


__all__ = ["SqliteArtifactStore", "get_artifact_store"]