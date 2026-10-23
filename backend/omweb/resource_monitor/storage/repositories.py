# backend/omweb/resource_monitor/storage/repositories.py
"""Data repositories and timeseries query engine for PELDRUN Resource Monitor.

Provides strongly typed CRUD operations, batch timeseries persistence,
downsampled aggregation queries, and tool consumption analysis.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    DiagnosticEventModel,
    DiagnosticEventType,
    DiagnosticSeverity,
    EnvironmentSnapshotModel,
    ExecutionRunModel,
    ExecutionSpanModel,
    MeasurementConfidence,
    MonitorSessionModel,
    MonitorSettingModel,
    ProcessInstanceModel,
    ProcessType,
    ResourceRollupModel,
    ResourceSampleModel,
    RollupPeriodType,
    RunStatus,
    SpanStatus,
    SpanType,
    TimeseriesPoint,
    ToolConsumptionSummary,
)
from .database import MonitorDatabase

logger = logging.getLogger(__name__)


class MonitorRepository:
    """Repository providing data access for all resource monitor tables."""

    def __init__(self, database: MonitorDatabase) -> None:
        self._db = database

    # ========================================================================
    # 1. Sessions Repository
    # ========================================================================

    def insert_session(self, session: MonitorSessionModel) -> None:
        """Insert a newly started monitoring session."""
        query = """
            INSERT INTO monitor_sessions (
                session_id, started_at, ended_at, app_version,
                python_version, node_version, pid, clean_exit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            session.session_id,
            session.started_at,
            session.ended_at,
            session.app_version,
            session.python_version,
            session.node_version,
            session.pid,
            1 if session.clean_exit else 0,
        )
        self._db.execute_write(query, params)

    def close_session(self, session_id: str, clean_exit: bool = True) -> None:
        """Mark an active session as concluded."""
        query = """
            UPDATE monitor_sessions
            SET ended_at = ?, clean_exit = ?
            WHERE session_id = ?;
        """
        params = (
            datetime.now(timezone.utc).isoformat(),
            1 if clean_exit else 0,
            session_id,
        )
        self._db.execute_write(query, params)

    def get_session(self, session_id: str) -> Optional[MonitorSessionModel]:
        """Fetch session details by unique session_id."""
        query = "SELECT * FROM monitor_sessions WHERE session_id = ?;"
        rows = self._db.execute_read(query, (session_id,))
        if not rows:
            return None
        r = rows[0]
        return MonitorSessionModel(
            session_id=r["session_id"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            app_version=r["app_version"],
            python_version=r["python_version"],
            node_version=r["node_version"],
            pid=r["pid"],
            clean_exit=bool(r["clean_exit"]),
        )

    # ========================================================================
    # 2. Environment Snapshots Repository
    # ========================================================================

    def insert_environment_snapshot(self, snapshot: EnvironmentSnapshotModel) -> None:
        """Record a hardware specification and container profile snapshot."""
        query = """
            INSERT INTO environment_snapshots (
                snapshot_id, session_id, recorded_at, os_name, os_release,
                architecture, is_docker, is_wsl, cpu_count_logical,
                cpu_count_physical, total_physical_ram_bytes,
                cgroup_memory_limit_bytes, cgroup_cpu_quota_us, capabilities_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            snapshot.snapshot_id,
            snapshot.session_id,
            snapshot.recorded_at,
            snapshot.os_name,
            snapshot.os_release,
            snapshot.architecture,
            1 if snapshot.is_docker else 0,
            1 if snapshot.is_wsl else 0,
            snapshot.cpu_count_logical,
            snapshot.cpu_count_physical,
            snapshot.total_physical_ram_bytes,
            snapshot.cgroup_memory_limit_bytes,
            snapshot.cgroup_cpu_quota_us,
            snapshot.capabilities_json,
        )
        self._db.execute_write(query, params)

    def get_latest_environment_snapshot(self, session_id: Optional[str] = None) -> Optional[EnvironmentSnapshotModel]:
        """Query the most recent hardware environment profile."""
        if session_id:
            query = "SELECT * FROM environment_snapshots WHERE session_id = ? ORDER BY recorded_at DESC LIMIT 1;"
            rows = self._db.execute_read(query, (session_id,))
        else:
            query = "SELECT * FROM environment_snapshots ORDER BY recorded_at DESC LIMIT 1;"
            rows = self._db.execute_read(query)

        if not rows:
            return None
        r = rows[0]
        return EnvironmentSnapshotModel(
            snapshot_id=r["snapshot_id"],
            session_id=r["session_id"],
            recorded_at=r["recorded_at"],
            os_name=r["os_name"],
            os_release=r["os_release"],
            architecture=r["architecture"],
            is_docker=bool(r["is_docker"]),
            is_wsl=bool(r["is_wsl"]),
            cpu_count_logical=r["cpu_count_logical"],
            cpu_count_physical=r["cpu_count_physical"],
            total_physical_ram_bytes=r["total_physical_ram_bytes"],
            cgroup_memory_limit_bytes=r["cgroup_memory_limit_bytes"],
            cgroup_cpu_quota_us=r["cgroup_cpu_quota_us"],
            capabilities_json=r["capabilities_json"],
        )

    # ========================================================================
    # 3. Execution Runs Repository
    # ========================================================================

    def insert_execution_run(self, run: ExecutionRunModel) -> None:
        """Register an execution run record."""
        query = """
            INSERT INTO execution_runs (
                run_id, session_id, job_id, chat_id, agent_id, engine_type,
                status, started_at, ended_at, duration_ms, peak_rss_bytes,
                cumulative_cpu_time_ms, attributed_children_count,
                measurement_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            run.run_id,
            run.session_id,
            run.job_id,
            run.chat_id,
            run.agent_id,
            run.engine_type,
            run.status.value,
            run.started_at,
            run.ended_at,
            run.duration_ms,
            run.peak_rss_bytes,
            run.cumulative_cpu_time_ms,
            run.attributed_children_count,
            run.measurement_confidence.value,
        )
        self._db.execute_write(query, params)

    def update_execution_run(
        self,
        run_id: str,
        status: RunStatus,
        ended_at: Optional[str] = None,
        duration_ms: Optional[int] = None,
        peak_rss_bytes: Optional[int] = None,
        cumulative_cpu_time_ms: Optional[int] = None,
        attributed_children_count: Optional[int] = None,
        measurement_confidence: Optional[MeasurementConfidence] = None,
    ) -> None:
        """Update metrics and final status of an execution run."""
        fields: List[str] = ["status = ?"]
        params: List[Any] = [status.value]

        if ended_at is not None:
            fields.append("ended_at = ?")
            params.append(ended_at)
        if duration_ms is not None:
            fields.append("duration_ms = ?")
            params.append(duration_ms)
        if peak_rss_bytes is not None:
            fields.append("peak_rss_bytes = MAX(peak_rss_bytes, ?)")
            params.append(peak_rss_bytes)
        if cumulative_cpu_time_ms is not None:
            fields.append("cumulative_cpu_time_ms = ?")
            params.append(cumulative_cpu_time_ms)
        if attributed_children_count is not None:
            fields.append("attributed_children_count = ?")
            params.append(attributed_children_count)
        if measurement_confidence is not None:
            fields.append("measurement_confidence = ?")
            params.append(measurement_confidence.value)

        params.append(run_id)
        query = f"UPDATE execution_runs SET {', '.join(fields)} WHERE run_id = ?;"
        self._db.execute_write(query, tuple(params))

    def get_execution_run(self, run_id: str) -> Optional[ExecutionRunModel]:
        """Query execution run by run_id."""
        query = "SELECT * FROM execution_runs WHERE run_id = ?;"
        rows = self._db.execute_read(query, (run_id,))
        if not rows:
            return None
        r = rows[0]
        return ExecutionRunModel(
            run_id=r["run_id"],
            session_id=r["session_id"],
            job_id=r["job_id"],
            chat_id=r["chat_id"],
            agent_id=r["agent_id"],
            engine_type=r["engine_type"],
            status=RunStatus(r["status"]),
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            duration_ms=r["duration_ms"],
            peak_rss_bytes=r["peak_rss_bytes"],
            cumulative_cpu_time_ms=r["cumulative_cpu_time_ms"],
            attributed_children_count=r["attributed_children_count"],
            measurement_confidence=MeasurementConfidence(r["measurement_confidence"]),
        )

    def list_execution_runs(
        self,
        session_id: Optional[str] = None,
        status: Optional[RunStatus] = None,
        agent_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ExecutionRunModel]:
        """List execution runs with optional filtering and pagination."""
        conditions: List[str] = []
        params: List[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT * FROM execution_runs
            {where_clause}
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?;
        """
        params.extend([limit, offset])

        rows = self._db.execute_read(query, tuple(params))
        return [
            ExecutionRunModel(
                run_id=r["run_id"],
                session_id=r["session_id"],
                job_id=r["job_id"],
                chat_id=r["chat_id"],
                agent_id=r["agent_id"],
                engine_type=r["engine_type"],
                status=RunStatus(r["status"]),
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                duration_ms=r["duration_ms"],
                peak_rss_bytes=r["peak_rss_bytes"],
                cumulative_cpu_time_ms=r["cumulative_cpu_time_ms"],
                attributed_children_count=r["attributed_children_count"],
                measurement_confidence=MeasurementConfidence(r["measurement_confidence"]),
            )
            for r in rows
        ]

    # ========================================================================
    # 4. Process Instances Repository
    # ========================================================================

    def insert_process_instance(self, process: ProcessInstanceModel) -> None:
        """Insert a newly discovered or registered process instance."""
        query = """
            INSERT INTO process_instances (
                process_instance_id, session_id, pid, ppid, name,
                command_line, process_type, created_at, terminated_at,
                exit_code, is_orphan
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            process.process_instance_id,
            process.session_id,
            process.pid,
            process.ppid,
            process.name,
            process.command_line,
            process.process_type.value,
            process.created_at,
            process.terminated_at,
            process.exit_code,
            1 if process.is_orphan else 0,
        )
        self._db.execute_write(query, params)

    def update_process_instance(
        self,
        process_instance_id: str,
        terminated_at: Optional[str] = None,
        exit_code: Optional[int] = None,
        is_orphan: Optional[bool] = None,
    ) -> None:
        """Update termination facts or orphan status of a process instance."""
        fields: List[str] = []
        params: List[Any] = []

        if terminated_at is not None:
            fields.append("terminated_at = ?")
            params.append(terminated_at)
        if exit_code is not None:
            fields.append("exit_code = ?")
            params.append(exit_code)
        if is_orphan is not None:
            fields.append("is_orphan = ?")
            params.append(1 if is_orphan else 0)

        if not fields:
            return

        params.append(process_instance_id)
        query = f"UPDATE process_instances SET {', '.join(fields)} WHERE process_instance_id = ?;"
        self._db.execute_write(query, tuple(params))

    def list_active_processes(self, session_id: Optional[str] = None) -> List[ProcessInstanceModel]:
        """Query processes that have not yet terminated."""
        if session_id:
            query = "SELECT * FROM process_instances WHERE session_id = ? AND terminated_at IS NULL ORDER BY created_at ASC;"
            rows = self._db.execute_read(query, (session_id,))
        else:
            query = "SELECT * FROM process_instances WHERE terminated_at IS NULL ORDER BY created_at ASC;"
            rows = self._db.execute_read(query)

        return [
            ProcessInstanceModel(
                process_instance_id=r["process_instance_id"],
                session_id=r["session_id"],
                pid=r["pid"],
                ppid=r["ppid"],
                name=r["name"],
                command_line=r["command_line"],
                process_type=ProcessType(r["process_type"]),
                created_at=r["created_at"],
                terminated_at=r["terminated_at"],
                exit_code=r["exit_code"],
                is_orphan=bool(r["is_orphan"]),
            )
            for r in rows
        ]

    # ========================================================================
    # 5. Resource Samples Repository (High-Frequency Timeseries)
    # ========================================================================

    def insert_samples_batch(self, samples: List[ResourceSampleModel]) -> int:
        """Persist a batch of resource samples within a single database transaction.

        Returns:
            The number of records processed.
        """
        if not samples:
            return 0

        query = """
            INSERT INTO resource_samples (
                session_id, recorded_at, recorded_at_epoch_ms,
                host_cpu_percent, host_ram_used_bytes, host_ram_available_bytes,
                peldrun_rss_bytes, peldrun_vms_bytes, peldrun_cpu_percent,
                peldrun_children_rss_bytes, active_runs_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params_list = [
            (
                s.session_id,
                s.recorded_at,
                s.recorded_at_epoch_ms,
                s.host_cpu_percent,
                s.host_ram_used_bytes,
                s.host_ram_available_bytes,
                s.peldrun_rss_bytes,
                s.peldrun_vms_bytes,
                s.peldrun_cpu_percent,
                s.peldrun_children_rss_bytes,
                s.active_runs_count,
            )
            for s in samples
        ]
        return self._db.execute_write_many(query, params_list)

    def query_timeseries(
        self,
        from_epoch_ms: int,
        to_epoch_ms: int,
        resolution_seconds: int = 1,
    ) -> List[TimeseriesPoint]:
        """Query timeseries points with optional database-side bucket aggregation.

        Args:
            from_epoch_ms: Window start timestamp in milliseconds.
            to_epoch_ms: Window end timestamp in milliseconds.
            resolution_seconds: Aggregation bucket resolution in seconds.
        """
        if resolution_seconds <= 1:
            query = """
                SELECT
                    recorded_at_epoch_ms,
                    host_cpu_percent,
                    host_ram_used_bytes,
                    peldrun_rss_bytes,
                    peldrun_children_rss_bytes,
                    (peldrun_rss_bytes + peldrun_children_rss_bytes) AS peldrun_total_rss_bytes,
                    active_runs_count
                FROM resource_samples
                WHERE recorded_at_epoch_ms >= ? AND recorded_at_epoch_ms <= ?
                ORDER BY recorded_at_epoch_ms ASC;
            """
            rows = self._db.execute_read(query, (from_epoch_ms, to_epoch_ms))
            return [
                TimeseriesPoint(
                    timestamp_epoch_ms=r["recorded_at_epoch_ms"],
                    host_cpu_percent=round(r["host_cpu_percent"], 2),
                    host_ram_used_bytes=r["host_ram_used_bytes"],
                    peldrun_rss_bytes=r["peldrun_rss_bytes"],
                    peldrun_children_rss_bytes=r["peldrun_children_rss_bytes"],
                    peldrun_total_rss_bytes=r["peldrun_total_rss_bytes"],
                    active_runs_count=r["active_runs_count"],
                )
                for r in rows
            ]

        # Downsample with SQL bucket grouping to avoid overwhelming client
        bucket_size_ms = resolution_seconds * 1000
        query = f"""
            SELECT
                (recorded_at_epoch_ms / {bucket_size_ms}) * {bucket_size_ms} AS bucket_epoch_ms,
                ROUND(AVG(host_cpu_percent), 2) AS host_cpu_percent,
                CAST(AVG(host_ram_used_bytes) AS INTEGER) AS host_ram_used_bytes,
                CAST(AVG(peldrun_rss_bytes) AS INTEGER) AS peldrun_rss_bytes,
                CAST(AVG(peldrun_children_rss_bytes) AS INTEGER) AS peldrun_children_rss_bytes,
                CAST(AVG(peldrun_rss_bytes + peldrun_children_rss_bytes) AS INTEGER) AS peldrun_total_rss_bytes,
                MAX(active_runs_count) AS active_runs_count
            FROM resource_samples
            WHERE recorded_at_epoch_ms >= ? AND recorded_at_epoch_ms <= ?
            GROUP BY bucket_epoch_ms
            ORDER BY bucket_epoch_ms ASC;
        """
        rows = self._db.execute_read(query, (from_epoch_ms, to_epoch_ms))
        return [
            TimeseriesPoint(
                timestamp_epoch_ms=r["bucket_epoch_ms"],
                host_cpu_percent=r["host_cpu_percent"],
                host_ram_used_bytes=r["host_ram_used_bytes"],
                peldrun_rss_bytes=r["peldrun_rss_bytes"],
                peldrun_children_rss_bytes=r["peldrun_children_rss_bytes"],
                peldrun_total_rss_bytes=r["peldrun_total_rss_bytes"],
                active_runs_count=r["active_runs_count"],
            )
            for r in rows
        ]

    # ========================================================================
    # 6. Execution Spans Repository
    # ========================================================================

    def insert_execution_span(self, span: ExecutionSpanModel) -> None:
        """Insert a newly opened execution span."""
        query = """
            INSERT INTO execution_spans (
                span_id, run_id, parent_span_id, span_type, target_name,
                started_at, ended_at, duration_ms, process_instance_id,
                rss_start_bytes, rss_end_bytes, rss_peak_bytes,
                cpu_time_consumed_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            span.span_id,
            span.run_id,
            span.parent_span_id,
            span.span_type.value,
            span.target_name,
            span.started_at,
            span.ended_at,
            span.duration_ms,
            span.process_instance_id,
            span.rss_start_bytes,
            span.rss_end_bytes,
            span.rss_peak_bytes,
            span.cpu_time_consumed_ms,
            span.status.value,
        )
        self._db.execute_write(query, params)

    def update_execution_span(
        self,
        span_id: str,
        ended_at: str,
        duration_ms: int,
        status: SpanStatus,
        rss_end_bytes: Optional[int] = None,
        rss_peak_bytes: Optional[int] = None,
        cpu_time_consumed_ms: Optional[int] = None,
    ) -> None:
        """Conclude an execution span with consumption metrics."""
        query = """
            UPDATE execution_spans
            SET ended_at = ?, duration_ms = ?, status = ?,
                rss_end_bytes = ?, rss_peak_bytes = ?, cpu_time_consumed_ms = ?
            WHERE span_id = ?;
        """
        params = (
            ended_at,
            duration_ms,
            status.value,
            rss_end_bytes,
            rss_peak_bytes,
            cpu_time_consumed_ms,
            span_id,
        )
        self._db.execute_write(query, params)

    def list_spans_by_run(self, run_id: str) -> List[ExecutionSpanModel]:
        """Fetch all execution spans correlated with a given execution run."""
        query = "SELECT * FROM execution_spans WHERE run_id = ? ORDER BY started_at ASC;"
        rows = self._db.execute_read(query, (run_id,))
        return [
            ExecutionSpanModel(
                span_id=r["span_id"],
                run_id=r["run_id"],
                parent_span_id=r["parent_span_id"],
                span_type=SpanType(r["span_type"]),
                target_name=r["target_name"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                duration_ms=r["duration_ms"],
                process_instance_id=r["process_instance_id"],
                rss_start_bytes=r["rss_start_bytes"],
                rss_end_bytes=r["rss_end_bytes"],
                rss_peak_bytes=r["rss_peak_bytes"],
                cpu_time_consumed_ms=r["cpu_time_consumed_ms"],
                status=SpanStatus(r["status"]),
            )
            for r in rows
        ]

    def get_tool_consumption_summary(
        self,
        from_time: str,
        to_time: str,
    ) -> List[ToolConsumptionSummary]:
        """Calculate aggregated resource consumption grouped by tool name."""
        query = """
            SELECT
                target_name AS tool_name,
                COUNT(*) AS total_calls,
                ROUND(AVG(COALESCE(duration_ms, 0)), 2) AS avg_duration_ms,
                CAST(AVG(COALESCE(rss_peak_bytes, 0)) AS INTEGER) AS avg_rss_bytes,
                MAX(COALESCE(rss_peak_bytes, 0)) AS peak_rss_bytes,
                SUM(COALESCE(cpu_time_consumed_ms, 0)) AS cumulative_cpu_time_ms
            FROM execution_spans
            WHERE span_type = 'TOOL_CALL'
              AND started_at >= ? AND started_at <= ?
            GROUP BY target_name
            ORDER BY cumulative_cpu_time_ms DESC;
        """
        rows = self._db.execute_read(query, (from_time, to_time))
        return [
            ToolConsumptionSummary(
                tool_name=r["tool_name"],
                total_calls=r["total_calls"],
                avg_duration_ms=r["avg_duration_ms"],
                avg_rss_bytes=r["avg_rss_bytes"],
                peak_rss_bytes=r["peak_rss_bytes"],
                cumulative_cpu_time_ms=r["cumulative_cpu_time_ms"],
            )
            for r in rows
        ]

    # ========================================================================
    # 7. Diagnostic Events Repository
    # ========================================================================

    def insert_diagnostic_event(self, event: DiagnosticEventModel) -> None:
        """Persist a diagnostic anomaly or alert event."""
        query = """
            INSERT INTO diagnostic_events (
                event_id, session_id, run_id, recorded_at,
                event_type, severity, message, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            event.event_id,
            event.session_id,
            event.run_id,
            event.recorded_at,
            event.event_type.value,
            event.severity.value,
            event.message,
            event.details_json,
        )
        self._db.execute_write(query, params)

    def list_diagnostic_events(
        self,
        severity: Optional[DiagnosticSeverity] = None,
        event_type: Optional[DiagnosticEventType] = None,
        limit: int = 50,
    ) -> List[DiagnosticEventModel]:
        """Query diagnostic alert events with optional filters."""
        conditions: List[str] = []
        params: List[Any] = []

        if severity:
            conditions.append("severity = ?")
            params.append(severity.value)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type.value)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT * FROM diagnostic_events
            {where_clause}
            ORDER BY recorded_at DESC
            LIMIT ?;
        """
        params.append(limit)

        rows = self._db.execute_read(query, tuple(params))
        return [
            DiagnosticEventModel(
                event_id=r["event_id"],
                session_id=r["session_id"],
                run_id=r["run_id"],
                recorded_at=r["recorded_at"],
                event_type=DiagnosticEventType(r["event_type"]),
                severity=DiagnosticSeverity(r["severity"]),
                message=r["message"],
                details_json=r["details_json"],
            )
            for r in rows
        ]

    # ========================================================================
    # 8. Resource Rollups Repository
    # ========================================================================

    def insert_rollup(self, rollup: ResourceRollupModel) -> None:
        """Insert an aggregated periodic summary rollup."""
        query = """
            INSERT INTO resource_rollups (
                rollup_id, period_type, period_start, period_end,
                total_runs_count, avg_run_duration_ms, avg_rss_bytes,
                peak_rss_bytes, total_cpu_time_ms, orphan_events_count,
                top_consuming_tools_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            rollup.rollup_id,
            rollup.period_type.value,
            rollup.period_start,
            rollup.period_end,
            rollup.total_runs_count,
            rollup.avg_run_duration_ms,
            rollup.avg_rss_bytes,
            rollup.peak_rss_bytes,
            rollup.total_cpu_time_ms,
            rollup.orphan_events_count,
            rollup.top_consuming_tools_json,
        )
        self._db.execute_write(query, params)

    def query_rollups(
        self,
        period_type: RollupPeriodType,
        from_date: str,
        to_date: str,
    ) -> List[ResourceRollupModel]:
        """Query rollups for a specific period type between start and end bounds."""
        query = """
            SELECT * FROM resource_rollups
            WHERE period_type = ? AND period_start >= ? AND period_end <= ?
            ORDER BY period_start ASC;
        """
        rows = self._db.execute_read(query, (period_type.value, from_date, to_date))
        return [
            ResourceRollupModel(
                rollup_id=r["rollup_id"],
                period_type=RollupPeriodType(r["period_type"]),
                period_start=r["period_start"],
                period_end=r["period_end"],
                total_runs_count=r["total_runs_count"],
                avg_run_duration_ms=r["avg_run_duration_ms"],
                avg_rss_bytes=r["avg_rss_bytes"],
                peak_rss_bytes=r["peak_rss_bytes"],
                total_cpu_time_ms=r["total_cpu_time_ms"],
                orphan_events_count=r["orphan_events_count"],
                top_consuming_tools_json=r["top_consuming_tools_json"],
            )
            for r in rows
        ]

    # ========================================================================
    # 9. Monitor Settings Repository
    # ========================================================================

    def get_setting(self, key: str) -> Optional[str]:
        """Fetch a specific monitor configuration setting."""
        query = "SELECT setting_value FROM monitor_settings WHERE setting_key = ?;"
        rows = self._db.execute_read(query, (key,))
        return rows[0]["setting_value"] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        """Upsert a monitor configuration setting."""
        query = """
            INSERT INTO monitor_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at;
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        self._db.execute_write(query, (key, value, now_iso))

    def get_all_settings(self) -> Dict[str, str]:
        """Retrieve all persisted monitor configuration settings."""
        query = "SELECT setting_key, setting_value FROM monitor_settings;"
        rows = self._db.execute_read(query)
        return {r["setting_key"]: r["setting_value"] for r in rows}
