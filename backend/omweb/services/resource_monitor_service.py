# backend/omweb/services/resource_monitor_service.py
"""Central facade service integrating the Resource Monitor with OMWeb.

Coordinates hardware collectors, SQLite repositories, lifecycle transitions,
concurrency attribution, and diagnostics across FastAPI and execution engines.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ..resource_monitor.attribution import AttributionEngine
from ..resource_monitor.capabilities import ResourceMonitorCapabilities, probe_capabilities
from ..resource_monitor.collectors import (
    DiskCollector,
    HostCollector,
    NetworkCollector,
    ProcessCollector,
)
from ..resource_monitor.config import ResourceMonitorConfig, get_default_config
from ..resource_monitor.lifecycle import LifecycleCoordinator
from ..resource_monitor.models import (
    ChildProcessMetric,
    CurrentResourceSnapshot,
    DiagnosticEventModel,
    DiagnosticEventType,
    DiagnosticSeverity,
    EnvironmentSnapshotModel,
    ExecutionRunModel,
    ExecutionSpanModel,
    HostMetrics,
    MeasurementConfidence,
    MonitorSessionModel,
    MonitorSettingsUpdate,
    PeldrunProcessMetrics,
    ProcessInstanceModel,
    ResourceRollupModel,
    ResourceSampleModel,
    RollupPeriodType,
    RunStatus,
    SpanStatus,
    SpanType,
    TerminateOrphanResponse,
    TimeseriesPoint,
    TimeseriesResponse,
    ToolConsumptionSummary,
)
from ..resource_monitor.process_tracker import ProcessTracker
from ..resource_monitor.storage import (
    MigrationManager,
    MonitorDatabase,
    MonitorRepository,
    RetentionManager,
    RollupEngine,
)

logger = logging.getLogger(__name__)

# Graceful optional imports for future scheduler and diagnostics integration
try:
    from ..resource_monitor.sampling_scheduler import SamplingScheduler
except (ImportError, AttributeError):
    SamplingScheduler = None  # type: ignore

try:
    from ..resource_monitor.diagnostics import DiagnosticsEngine
except (ImportError, AttributeError):
    DiagnosticsEngine = None  # type: ignore


class ResourceMonitorService:
    """Unified service facade managing the PELDRUN Resource Monitor subsystem."""

    def __init__(self, config: Optional[ResourceMonitorConfig] = None) -> None:
        self._config = config or get_default_config()
        self._capabilities = probe_capabilities()
        self._session_id = str(uuid.uuid4())

        # Storage components
        self._db = MonitorDatabase(config=self._config)
        self._migrations = MigrationManager(self._db)
        self._repo = MonitorRepository(self._db)
        self._retention = RetentionManager(self._db, config=self._config)
        self._rollups = RollupEngine(self._db, repository=self._repo)

        # Core tracking and attribution engines
        self._attribution = AttributionEngine()
        self._process_tracker = ProcessTracker(
            session_id=self._session_id,
            config=self._config,
            capabilities=self._capabilities,
        )
        self._lifecycle = LifecycleCoordinator(
            repository=self._repo,
            attribution_engine=self._attribution,
            process_tracker=self._process_tracker,
            config=self._config,
            session_id=self._session_id,
        )

        # Telemetry collectors
        self._host_collector = HostCollector(capabilities=self._capabilities)
        self._process_collector = ProcessCollector(capabilities=self._capabilities)
        self._disk_collector = DiskCollector(capabilities=self._capabilities)
        self._network_collector = NetworkCollector(capabilities=self._capabilities)

        # Auxiliary subsystems
        self._scheduler: Optional[Any] = None
        self._diagnostics: Optional[Any] = None
        self._is_started: bool = False

    @property
    def session_id(self) -> str:
        """Unique identifier of the active monitoring session."""
        return self._session_id

    @property
    def config(self) -> ResourceMonitorConfig:
        """Current runtime configuration."""
        return self._config

    # ========================================================================
    # 1. Startup and Shutdown Handshake
    # ========================================================================

    async def start(self, app_version: str = "1.0.0") -> None:
        """Initialize database, run migrations, record hardware, and start session."""
        if self._is_started:
            return

        logger.info("Starting PELDRUN Resource Monitor service...")

        # 1. Apply database schema migrations asynchronously
        await self._migrations.async_run_migrations()

        # 2. Initialize monitoring session and environment snapshot
        self._lifecycle.start_session(app_version=app_version)

        # 3. Instantiate and start adaptive sampling scheduler if available
        if SamplingScheduler is not None:
            self._scheduler = SamplingScheduler(
                service=self,
                config=self._config,
            )
            await self._scheduler.start()

        # 4. Instantiate diagnostics engine if available
        if DiagnosticsEngine is not None:
            self._diagnostics = DiagnosticsEngine(
                repository=self._repo,
                process_tracker=self._process_tracker,
                config=self._config,
                session_id=self._session_id,
            )

        self._is_started = True
        logger.info("PELDRUN Resource Monitor service started successfully (Session: %s).", self._session_id)

    async def shutdown(self) -> None:
        """Conclude active session, flush telemetry queues, and perform clean shutdown."""
        if not self._is_started:
            return

        logger.info("Shutting down PELDRUN Resource Monitor service...")

        # 1. Stop background sampling scheduler
        if self._scheduler is not None:
            await self._scheduler.stop()

        # 2. Conclude session cleanly
        self._lifecycle.close_session(clean_exit=True)

        self._is_started = False
        logger.info("PELDRUN Resource Monitor service shutdown complete.")

    # ========================================================================
    # 2. Run and Span Lifecycle Hooks
    # ========================================================================

    def create_run(
        self,
        run_id: str,
        job_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        engine_type: Optional[str] = None,
    ) -> ExecutionRunModel:
        """Register a new execution run in PENDING status."""
        return self._lifecycle.create_run(
            run_id=run_id,
            job_id=job_id,
            chat_id=chat_id,
            agent_id=agent_id,
            engine_type=engine_type,
        )

    def start_run(self, run_id: str) -> None:
        """Transition execution run to RUNNING and capture memory baseline."""
        self._lifecycle.start_run(run_id)

    def complete_run(self, run_id: str) -> None:
        """Conclude an execution run successfully."""
        self._lifecycle.complete_run(run_id)

    def fail_run(self, run_id: str, error_message: Optional[str] = None) -> None:
        """Conclude an execution run with FAILED status."""
        self._lifecycle.fail_run(run_id, error_message=error_message)

    def cancel_run(self, run_id: str) -> None:
        """Conclude an execution run with CANCELLED status."""
        self._lifecycle.cancel_run(run_id)

    def start_span(
        self,
        span_id: str,
        run_id: str,
        target_name: str,
        span_type: SpanType = SpanType.TOOL_CALL,
        parent_span_id: Optional[str] = None,
        process_instance_id: Optional[str] = None,
    ) -> ExecutionSpanModel:
        """Begin tracking an individual tool call, step, or reasoning span."""
        return self._lifecycle.start_span(
            span_id=span_id,
            run_id=run_id,
            target_name=target_name,
            span_type=span_type,
            parent_span_id=parent_span_id,
            process_instance_id=process_instance_id,
        )

    def end_span(
        self,
        span_id: str,
        status: SpanStatus = SpanStatus.COMPLETED,
        cpu_time_consumed_ms: Optional[int] = None,
    ) -> Optional[ExecutionSpanModel]:
        """Conclude an individual execution span and calculate consumption deltas."""
        return self._lifecycle.end_span(
            span_id=span_id,
            status=status,
            cpu_time_consumed_ms=cpu_time_consumed_ms,
        )

    def associate_child_pid(self, run_id: str, pid: int) -> None:
        """Explicitly attribute a dedicated subprocess PID to an execution run."""
        self._attribution.associate_child_pid(run_id=run_id, pid=pid)
        self._process_tracker.associate_run_process(run_id=run_id, pid=pid)

    # ========================================================================
    # 3. Real-Time Telemetry and Sampling Operations
    # ========================================================================

    def _collect_raw_telemetry(self) -> Tuple[ResourceSampleModel, List[ChildProcessMetric]]:
        """Collect instantaneous host and process tree telemetry without modifying state."""
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        now_epoch_ms = int(now_dt.timestamp() * 1000)

        # 1. Query host hardware
        host_dict = self._host_collector.collect() or {
            "cpu_percent": 0.0,
            "ram_total_bytes": 0,
            "ram_used_bytes": 0,
            "ram_available_bytes": 0,
        }

        # 2. Query PELDRUN process and child tree
        proc_dict = self._process_collector.collect() or {
            "peldrun": {
                "rss_bytes": 0,
                "vms_bytes": 0,
                "cpu_percent": 0.0,
                "children_rss_bytes": 0,
            },
            "active_children": [],
        }

        peldrun_info = proc_dict.get("peldrun", {})
        raw_children = proc_dict.get("active_children", [])
        children_metrics = [
            ChildProcessMetric(**c) if isinstance(c, dict) else c
            for c in raw_children
        ]

        sample = ResourceSampleModel(
            session_id=self._session_id,
            recorded_at=now_iso,
            recorded_at_epoch_ms=now_epoch_ms,
            host_cpu_percent=host_dict.get("cpu_percent", 0.0),
            host_ram_used_bytes=host_dict.get("ram_used_bytes", 0),
            host_ram_available_bytes=host_dict.get("ram_available_bytes", 0),
            peldrun_rss_bytes=peldrun_info.get("main_rss_bytes", 0),
            peldrun_vms_bytes=peldrun_info.get("total_vms_bytes", 0),
            peldrun_cpu_percent=peldrun_info.get("cpu_percent", 0.0),
            peldrun_children_rss_bytes=peldrun_info.get("children_rss_bytes", 0),
            active_runs_count=self._attribution.active_run_count,
        )

        return sample, children_metrics

    def sample_now(self) -> Tuple[ResourceSampleModel, List[ChildProcessMetric]]:
        """Collect telemetry and update tracker/attribution state (stateful)."""
        sample, children_metrics = self._collect_raw_telemetry()

        # 1. Sync process tracker with active children observations
        self._process_tracker.scan_and_update(children_metrics)

        # 2. Partition telemetry across active execution runs
        self._attribution.process_sample(sample, children_metrics)

        return sample, children_metrics

    def get_current_snapshot(self) -> CurrentResourceSnapshot:
        """Generate a complete real-time resource snapshot for the dashboard (stateless)."""
        sample, children = self._collect_raw_telemetry()

        # Probe storage volume for host summary
        disk_dict = self._disk_collector.collect() or {}

        host_metrics = HostMetrics(
            cpu_percent=sample.host_cpu_percent,
            ram_total_bytes=sample.host_ram_used_bytes + sample.host_ram_available_bytes,
            ram_used_bytes=sample.host_ram_used_bytes,
            ram_available_bytes=sample.host_ram_available_bytes,
            ram_percent=round(
                (sample.host_ram_used_bytes / (sample.host_ram_used_bytes + sample.host_ram_available_bytes) * 100.0)
                if (sample.host_ram_used_bytes + sample.host_ram_available_bytes) > 0 else 0.0,
                1,
            ),
            disk_total_bytes=disk_dict.get("workspace_total_bytes"),
            disk_used_bytes=disk_dict.get("workspace_used_bytes"),
            disk_percent=disk_dict.get("workspace_used_percent"),
        )

        peldrun_metrics = PeldrunProcessMetrics(
            pid=os.getpid(),
            cpu_percent=sample.peldrun_cpu_percent,
            main_rss_bytes=sample.peldrun_rss_bytes,
            children_rss_bytes=sample.peldrun_children_rss_bytes,
            total_rss_bytes=sample.peldrun_rss_bytes + sample.peldrun_children_rss_bytes,
            total_vms_bytes=sample.peldrun_vms_bytes,
            children_count=len(children),
            threads_count=0,
        )

        has_active_diagnostics = any(c.is_orphan for c in children)

        return CurrentResourceSnapshot(
            recorded_at=sample.recorded_at,
            recorded_at_epoch_ms=sample.recorded_at_epoch_ms,
            host=host_metrics,
            peldrun=peldrun_metrics,
            active_runs_count=sample.active_runs_count,
            active_children=children,
            has_active_diagnostics=has_active_diagnostics,
        )

    # ========================================================================
    # 4. API and Dashboard Query Facade
    # ========================================================================

    def get_environment(self) -> Optional[EnvironmentSnapshotModel]:
        """Fetch the hardware and OS profile recorded for this session."""
        return self._repo.get_latest_environment_snapshot(self._session_id)

    def list_processes(self, include_terminated: bool = False) -> List[ProcessInstanceModel]:
        """List tracked child processes with orphan indicators."""
        if include_terminated:
            query = "SELECT * FROM process_instances WHERE session_id = ? ORDER BY created_at DESC;"
            rows = self._db.execute_read(query, (self._session_id,))
            return [ProcessInstanceModel(**r) for r in rows]

        return self._repo.list_active_processes(session_id=self._session_id)

    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[RunStatus] = None,
        agent_id: Optional[str] = None,
        include_all_sessions: bool = False,
    ) -> List[ExecutionRunModel]:
        """Query execution runs with pagination and filtering."""
        session_filter = None if include_all_sessions else self._session_id
        return self._repo.list_execution_runs(
            session_id=session_filter,
            limit=limit,
            offset=offset,
            status=status,
            agent_id=agent_id,
        )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve execution run details along with its granular tool spans."""
        run = self._repo.get_execution_run(run_id)
        if not run:
            return None

        spans = self._repo.list_spans_by_run(run_id)
        return {
            "run": run,
            "spans": spans,
        }

    def get_run_timeline(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve execution run details, spans, and timeseries bounded by run lifecycle."""
        run_data = self.get_run(run_id)
        if not run_data:
            return None

        run = run_data["run"]
        spans = run_data["spans"]

        start_epoch_ms = 0
        end_epoch_ms = 0

        try:
            if run.started_at:
                start_dt = datetime.fromisoformat(run.started_at)
                start_epoch_ms = int(start_dt.timestamp() * 1000)

            if run.ended_at:
                end_dt = datetime.fromisoformat(run.ended_at)
                end_epoch_ms = int(end_dt.timestamp() * 1000)
            else:
                # If the run is still active, use current time to get up-to-date telemetry
                end_epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        except Exception:
            pass

        points = []
        if start_epoch_ms > 0 and end_epoch_ms >= start_epoch_ms:
            # 1-second resolution is precise enough for a single run's duration
            points = self._repo.query_timeseries(
                from_epoch_ms=start_epoch_ms,
                to_epoch_ms=end_epoch_ms,
                resolution_seconds=1,
            )

        return {
            "run": run,
            "spans": spans,
            "timeseries": points,
        }

    def get_run_timeline(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve execution run details, spans, and timeseries bounded by run lifecycle."""
        run_data = self.get_run(run_id)
        if not run_data:
            return None

        run = run_data["run"]
        spans = run_data["spans"]

        start_epoch_ms = 0
        end_epoch_ms = 0

        try:
            if run.started_at:
                start_dt = datetime.fromisoformat(run.started_at)
                start_epoch_ms = int(start_dt.timestamp() * 1000)

            if run.ended_at:
                end_dt = datetime.fromisoformat(run.ended_at)
                end_epoch_ms = int(end_dt.timestamp() * 1000)
            else:
                # If the run is still active, use current time to get up-to-date telemetry
                end_epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        except Exception:
            pass

        points = []
        if start_epoch_ms > 0 and end_epoch_ms >= start_epoch_ms:
            # 1-second resolution is precise enough for a single run's duration
            points = self._repo.query_timeseries(
                from_epoch_ms=start_epoch_ms,
                to_epoch_ms=end_epoch_ms,
                resolution_seconds=1,
            )

        return {
            "run": run,
            "spans": spans,
            "timeseries": points,
        }

    def query_timeseries(
        self,
        from_epoch_ms: int,
        to_epoch_ms: int,
        resolution_seconds: int = 1,
    ) -> TimeseriesResponse:
        """Query downsampled timeseries points for dashboard graphs."""
        points = self._repo.query_timeseries(
            from_epoch_ms=from_epoch_ms,
            to_epoch_ms=to_epoch_ms,
            resolution_seconds=resolution_seconds,
        )
        return TimeseriesResponse(
            from_time=datetime.fromtimestamp(from_epoch_ms / 1000.0, tz=timezone.utc).isoformat(),
            to_time=datetime.fromtimestamp(to_epoch_ms / 1000.0, tz=timezone.utc).isoformat(),
            resolution_seconds=resolution_seconds,
            points=points,
        )

    def get_tool_consumption_summary(
        self,
        from_time: str,
        to_time: str,
    ) -> List[ToolConsumptionSummary]:
        """Aggregate resource consumption grouped by tool name."""
        return self._repo.get_tool_consumption_summary(from_time, to_time)

    def list_diagnostic_events(
        self,
        severity: Optional[DiagnosticSeverity] = None,
        event_type: Optional[DiagnosticEventType] = None,
        limit: int = 50,
        include_resolved: bool = False,
    ) -> List[DiagnosticEventModel]:
        """Fetch diagnostic events and anomalies."""
        return self._repo.list_diagnostic_events(
            severity=severity,
            event_type=event_type,
            limit=limit,
            include_resolved=include_resolved,
        )

    def resolve_diagnostic_event(self, event_id: str) -> bool:
        """Manually resolve a diagnostic event."""
        return self._repo.resolve_diagnostic_event(event_id)

    def terminate_orphan(self, pid: int, force: bool = False) -> TerminateOrphanResponse:
        """Terminate an orphaned subprocess safely."""
        success, message = self._process_tracker.terminate_process(pid=pid, force=force)
        return TerminateOrphanResponse(
            pid=pid,
            terminated=success,
            message=message,
        )

    def get_settings(self) -> Dict[str, Any]:
        """Retrieve current configuration settings and persisted overrides."""
        persisted = self._repo.get_all_settings()
        return {
            "sampling_idle_interval_sec": float(persisted.get("sampling_idle_interval_sec", self._config.sampling_idle_interval_sec)),
            "sampling_active_interval_sec": float(persisted.get("sampling_active_interval_sec", self._config.sampling_active_interval_sec)),
            "retention_days": int(persisted.get("retention_days", self._config.raw_samples_retention_days)),
            "orphan_grace_period_sec": float(persisted.get("orphan_grace_period_sec", self._config.orphan_grace_period_sec)),
            "cpu_throttle_threshold_percent": float(persisted.get("cpu_throttle_threshold_percent", self._config.cpu_throttle_threshold_percent)),
        }

    def update_settings(self, updates: MonitorSettingsUpdate) -> Dict[str, Any]:
        """Update runtime settings and persist overrides to database."""
        if updates.sampling_rate_idle_sec is not None:
            self._config.sampling_idle_interval_sec = float(updates.sampling_rate_idle_sec)
            self._repo.set_setting("sampling_idle_interval_sec", str(updates.sampling_rate_idle_sec))

        if updates.sampling_rate_active_sec is not None:
            self._config.sampling_active_interval_sec = float(updates.sampling_rate_active_sec)
            self._repo.set_setting("sampling_active_interval_sec", str(updates.sampling_rate_active_sec))

        if updates.retention_days is not None:
            self._config.raw_samples_retention_days = updates.retention_days
            self._repo.set_setting("retention_days", str(updates.retention_days))

        if updates.orphan_grace_period_sec is not None:
            self._config.orphan_grace_period_sec = float(updates.orphan_grace_period_sec)
            self._repo.set_setting("orphan_grace_period_sec", str(updates.orphan_grace_period_sec))

        if updates.cpu_throttle_threshold_percent is not None:
            self._config.cpu_throttle_threshold_percent = updates.cpu_throttle_threshold_percent
            self._repo.set_setting("cpu_throttle_threshold_percent", str(updates.cpu_throttle_threshold_percent))

        logger.info("Monitor settings updated: %s", updates.model_dump(exclude_unset=True))
        return self.get_settings()

    def export_data(
        self,
        format_type: str = "json",
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
    ) -> Union[str, Dict[str, Any]]:
        """Export telemetry and run history in JSON or CSV format."""
        runs = self._repo.list_execution_runs(limit=1000)

        if format_type.lower() == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "run_id", "session_id", "agent_id", "status", "started_at",
                "ended_at", "duration_ms", "peak_rss_bytes", "cumulative_cpu_time_ms",
                "attributed_children_count", "measurement_confidence",
            ])
            for r in runs:
                writer.writerow([
                    r.run_id, r.session_id, r.agent_id, r.status.value, r.started_at,
                    r.ended_at, r.duration_ms, r.peak_rss_bytes, r.cumulative_cpu_time_ms,
                    r.attributed_children_count, r.measurement_confidence.value,
                ])
            return output.getvalue()

        # Default JSON export
        return {
            "session_id": self._session_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "runs": [r.model_dump() for r in runs],
        }


# Global singleton instance
_MONITOR_SERVICE: Optional[ResourceMonitorService] = None


def get_resource_monitor_service() -> ResourceMonitorService:
    """Retrieve or initialize the global ResourceMonitorService singleton."""
    global _MONITOR_SERVICE
    if _MONITOR_SERVICE is None:
        _MONITOR_SERVICE = ResourceMonitorService()
    return _MONITOR_SERVICE
