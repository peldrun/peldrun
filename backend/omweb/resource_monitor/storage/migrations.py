# backend/omweb/resource_monitor/storage/migrations.py
"""Schema migrations and table initializers for PELDRUN Resource Monitor.

Applies deterministic DDL migrations to the dedicated peldrun_monitor.db
database, provisions indexes, and validates structural schema integrity.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from .database import MonitorDatabase

logger = logging.getLogger(__name__)

# Complete initial DDL definition for schema version 1
MIGRATION_V1_SQL = """
-- 1. Schema migration version tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

-- 2. Sessions: Application startup and shutdown lifecycle
CREATE TABLE IF NOT EXISTS monitor_sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    app_version TEXT NOT NULL,
    python_version TEXT NOT NULL,
    node_version TEXT,
    pid INTEGER NOT NULL,
    clean_exit BOOLEAN DEFAULT 0
);

-- 3. Environment Snapshots: Hardware specifications, container limits, and OS profile
CREATE TABLE IF NOT EXISTS environment_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    os_name TEXT NOT NULL,
    os_release TEXT NOT NULL,
    architecture TEXT NOT NULL,
    is_docker BOOLEAN NOT NULL DEFAULT 0,
    is_wsl BOOLEAN NOT NULL DEFAULT 0,
    cpu_count_logical INTEGER NOT NULL,
    cpu_count_physical INTEGER,
    total_physical_ram_bytes INTEGER NOT NULL,
    cgroup_memory_limit_bytes INTEGER,
    cgroup_cpu_quota_us INTEGER,
    capabilities_json TEXT NOT NULL
);

-- 4. Execution Runs: Run metrics correlated with OMWeb and Peldrun-core
CREATE TABLE IF NOT EXISTS execution_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    job_id TEXT,
    chat_id TEXT,
    agent_id TEXT,
    engine_type TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    peak_rss_bytes INTEGER DEFAULT 0,
    cumulative_cpu_time_ms INTEGER DEFAULT 0,
    attributed_children_count INTEGER DEFAULT 0,
    measurement_confidence TEXT DEFAULT 'DIRECT'
);

-- 5. Process Instances: Tracked child and auxiliary processes
CREATE TABLE IF NOT EXISTS process_instances (
    process_instance_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    pid INTEGER NOT NULL,
    ppid INTEGER NOT NULL,
    name TEXT NOT NULL,
    command_line TEXT,
    process_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    terminated_at TEXT,
    exit_code INTEGER,
    is_orphan BOOLEAN DEFAULT 0
);

-- 6. Resource Samples: High-frequency timeseries measurements
CREATE TABLE IF NOT EXISTS resource_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    recorded_at_epoch_ms INTEGER NOT NULL,
    host_cpu_percent REAL NOT NULL,
    host_ram_used_bytes INTEGER NOT NULL,
    host_ram_available_bytes INTEGER NOT NULL,
    peldrun_rss_bytes INTEGER NOT NULL,
    peldrun_vms_bytes INTEGER NOT NULL,
    peldrun_cpu_percent REAL NOT NULL,
    peldrun_children_rss_bytes INTEGER DEFAULT 0,
    active_runs_count INTEGER NOT NULL DEFAULT 0
);

-- 7. Execution Spans: Granular tool, reasoning, and sandbox execution metrics
CREATE TABLE IF NOT EXISTS execution_spans (
    span_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES execution_runs(run_id) ON DELETE CASCADE,
    parent_span_id TEXT,
    span_type TEXT NOT NULL,
    target_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    process_instance_id TEXT REFERENCES process_instances(process_instance_id),
    rss_start_bytes INTEGER,
    rss_end_bytes INTEGER,
    rss_peak_bytes INTEGER,
    cpu_time_consumed_ms INTEGER,
    status TEXT NOT NULL
);

-- 8. Diagnostic Events: Alerts, memory creep, and orphaned process events
CREATE TABLE IF NOT EXISTS diagnostic_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    run_id TEXT REFERENCES execution_runs(run_id) ON DELETE SET NULL,
    recorded_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);

-- 9. Resource Rollups: Historical aggregated summaries (Hourly, Daily, Monthly)
CREATE TABLE IF NOT EXISTS resource_rollups (
    rollup_id TEXT PRIMARY KEY,
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    total_runs_count INTEGER NOT NULL,
    avg_run_duration_ms INTEGER NOT NULL,
    avg_rss_bytes INTEGER NOT NULL,
    peak_rss_bytes INTEGER NOT NULL,
    total_cpu_time_ms INTEGER NOT NULL,
    orphan_events_count INTEGER NOT NULL DEFAULT 0,
    top_consuming_tools_json TEXT
);

-- 10. Monitor Settings: Runtime configuration overrides
CREATE TABLE IF NOT EXISTS monitor_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Indexes for high-performance telemetry dashboard queries
CREATE INDEX IF NOT EXISTS idx_samples_time ON resource_samples(recorded_at_epoch_ms);
CREATE INDEX IF NOT EXISTS idx_runs_session ON execution_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON execution_runs(status);
CREATE INDEX IF NOT EXISTS idx_spans_run ON execution_spans(run_id);
CREATE INDEX IF NOT EXISTS idx_spans_tool ON execution_spans(target_name);
CREATE INDEX IF NOT EXISTS idx_processes_pid ON process_instances(pid);
CREATE INDEX IF NOT EXISTS idx_diagnostics_time ON diagnostic_events(recorded_at);
CREATE INDEX IF NOT EXISTS idx_rollups_period ON resource_rollups(period_type, period_start);
"""


MIGRATION_V2_SQL = """
-- Add resolved_at column to track when a diagnostic event's condition was cleared
ALTER TABLE diagnostic_events ADD COLUMN resolved_at TEXT;

-- Index for fast lookups of active vs resolved events
CREATE INDEX IF NOT EXISTS idx_diagnostics_resolved ON diagnostic_events(resolved_at);
"""


class MigrationManager:
    """Manages database schema initialization and migrations."""

    def __init__(self, database: MonitorDatabase) -> None:
        self._db = database

    def get_applied_versions(self) -> List[int]:
        """Query applied migration versions from schema_migrations table."""
        try:
            with self._db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
                )
                if not cursor.fetchone():
                    return []

                cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
                rows = cursor.fetchall()
                return [int(row["version"]) for row in rows]
        except Exception as exc:
            logger.debug("Failed querying applied migrations: %s", exc)
            return []

    def run_migrations(self) -> None:
        """Apply all pending migrations sequentially within managed transactions."""
        self._db.initialize()
        applied = self.get_applied_versions()

        # Version 1 Migration
        if 1 not in applied:
            logger.info("Applying Resource Monitor schema migration v1...")
            with self._db.get_connection() as conn:
                with conn:
                    conn.executescript(MIGRATION_V1_SQL)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                        (
                            1,
                            datetime.now(timezone.utc).isoformat(),
                            "Initial 9-table schema and indexes for Resource Monitor",
                        ),
                    )
            logger.info("Resource Monitor schema migration v1 applied successfully.")
            applied.append(1)

        # Version 2 Migration
        if 2 not in applied:
            logger.info("Applying Resource Monitor schema migration v2...")
            with self._db.get_connection() as conn:
                with conn:
                    conn.executescript(MIGRATION_V2_SQL)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                        (
                            2,
                            datetime.now(timezone.utc).isoformat(),
                            "Add resolved_at column to diagnostic_events for lifecycle tracking",
                        ),
                    )
            logger.info("Resource Monitor schema migration v2 applied successfully.")

    async def async_run_migrations(self) -> None:
        """Asynchronously execute pending migrations via worker thread."""
        await asyncio.to_thread(self.run_migrations)

    def verify_schema_integrity(self) -> bool:
        """Verify that all required core tables exist in the database."""
        required_tables = [
            "schema_migrations",
            "monitor_sessions",
            "environment_snapshots",
            "execution_runs",
            "process_instances",
            "resource_samples",
            "execution_spans",
            "diagnostic_events",
            "resource_rollups",
            "monitor_settings",
        ]

        try:
            with self._db.get_connection() as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
                existing_tables = {row["name"] for row in cursor.fetchall()}
                missing = [tbl for tbl in required_tables if tbl not in existing_tables]

                if missing:
                    logger.error("Missing required Resource Monitor tables: %s", missing)
                    return False

                return True
        except Exception as exc:
            logger.error("Failed verifying schema integrity: %s", exc)
            return False
