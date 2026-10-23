# backend/omweb/resource_monitor/storage/database.py
"""Dedicated SQLite connection manager and asynchronous execution engine.

Manages connections to the isolated peldrun_monitor.db database with Write-Ahead Logging
(WAL) enabled, enforcing non-blocking query execution via asyncio worker threads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Union

from ..config import ResourceMonitorConfig, get_default_config

logger = logging.getLogger(__name__)


class MonitorDatabase:
    """Manages dedicated SQLite database storage for resource monitoring."""

    def __init__(
        self,
        db_path: Optional[Union[Path, str]] = None,
        config: Optional[ResourceMonitorConfig] = None,
    ) -> None:
        self._config = config or get_default_config()
        self._db_path = Path(db_path) if db_path else self._config.db_path
        self._busy_timeout_ms = self._config.sqlite_busy_timeout_ms
        self._is_initialized: bool = False

    @property
    def db_path(self) -> Path:
        """Filesystem path to the SQLite database file."""
        return self._db_path

    def initialize(self) -> None:
        """Create parent storage directories and verify basic database connectivity."""
        if self._is_initialized:
            return

        parent_dir = self._db_path.parent
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)

        # Connect once to enforce PRAGMAs and verify database writability
        with self.get_connection() as conn:
            self._apply_pragmas(conn)

        self._is_initialized = True
        logger.debug("Initialized monitor database at: %s", self._db_path)

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """Enforce high-performance and consistency pragmas."""
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms};")
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("PRAGMA temp_store = MEMORY;")
        cursor.close()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide a contextual SQLite connection configured with standard pragmas.

        Yields:
            An active sqlite3.Connection instance with sqlite3.Row row_factory.
        """
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=self._busy_timeout_ms / 1000.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        try:
            yield conn
        finally:
            conn.close()

    def execute_write(self, query: str, params: Tuple[Any, ...] = ()) -> int:
        """Synchronously execute an INSERT, UPDATE, or DELETE query within a transaction.

        Returns:
            The rowid of the last inserted row, or affected rows count.
        """
        with self.get_connection() as conn:
            with conn:
                cursor = conn.execute(query, params)
                return cursor.lastrowid or cursor.rowcount

    def execute_write_many(self, query: str, seq_of_params: List[Tuple[Any, ...]]) -> int:
        """Synchronously execute batch operations via executemany within a transaction.

        Returns:
            The number of processed row parameter sets.
        """
        if not seq_of_params:
            return 0

        with self.get_connection() as conn:
            with conn:
                cursor = conn.executemany(query, seq_of_params)
                return cursor.rowcount

    def execute_read(self, query: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        """Synchronously execute a read query and return rows as dictionaries.

        Returns:
            List of dictionaries representing matched database records.
        """
        with self.get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    async def async_execute_write(self, query: str, params: Tuple[Any, ...] = ()) -> int:
        """Asynchronously execute a write query without blocking the asyncio loop."""
        return await asyncio.to_thread(self.execute_write, query, params)

    async def async_execute_write_many(
        self, query: str, seq_of_params: List[Tuple[Any, ...]]
    ) -> int:
        """Asynchronously execute batch writes without blocking the asyncio loop."""
        return await asyncio.to_thread(self.execute_write_many, query, seq_of_params)

    async def async_execute_read(
        self, query: str, params: Tuple[Any, ...] = ()
    ) -> List[Dict[str, Any]]:
        """Asynchronously execute a read query without blocking the asyncio loop."""
        return await asyncio.to_thread(self.execute_read, query, params)

    def vacuum(self) -> None:
        """Reclaim unused pages and optimize the SQLite database file."""
        with self.get_connection() as conn:
            conn.execute("VACUUM;")
        logger.info("Executed VACUUM on monitor database: %s", self._db_path)

    async def async_vacuum(self) -> None:
        """Asynchronously vacuum the database file."""
        await asyncio.to_thread(self.vacuum)
