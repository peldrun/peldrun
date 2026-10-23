# backend/omweb/resource_monitor/lifecycle.py
"""Lifecycle coordinator for monitoring sessions, execution runs, and tool spans.

Orchestrates startup/shutdown session handshakes, records baseline telemetry,
binds execution runs with attribution engines, tracks granular execution spans,
and recovers lingering runs from ungraceful application restarts.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .attribution import AttributionEngine
from .config import ResourceMonitorConfig, get_default_config
from .environment import EnvironmentInspector
from .models import (
    ExecutionRunModel,
    ExecutionSpanModel,
    MeasurementConfidence,
    MonitorSessionModel,
    ProcessInstanceModel,
    RunStatus,
    SpanStatus,
    SpanType,
)
from .process_tracker import ProcessTracker
from .storage.repositories import MonitorRepository

logger = logging.getLogger(__name__)

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class LifecycleCoordinator:
    """Coordinates lifecycle transitions across sessions, execution runs, and spans."""

    def __init__(
        self,
        repository: MonitorRepository,
        attribution_engine: AttributionEngine,
        process_tracker: ProcessTracker,
        config: Optional[ResourceMonitorConfig] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self._repo = repository
        self._attribution = attribution_engine
        self._process_tracker = process_tracker
        self._config = config or get_default_config()
        self._session_id = session_id or str(uuid.uuid4())
        self._main_pid = os.getpid()

        # In-memory index of active spans: span_id -> ExecutionSpanModel
        self._active_spans: Dict[str, ExecutionSpanModel] = {}
        # Monotonic start time per span: span_id -> monotonic_float
        self._span_start_times: Dict[str, float] = {}

    @property
    def session_id(self) -> str:
        """Unique identifier of the currently active monitor session."""
        return self._session_id

    # ========================================================================
    # 1. Application Session Management
    # ========================================================================

    def start_session(self, app_version: str = "1.0.0") -> MonitorSessionModel:
        """Initialize the monitoring session, profile environment, and recover dangling runs."""
        now_iso = datetime.now(timezone.utc).isoformat()
        python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        inspector = EnvironmentInspector()
        node_ver = inspector.probe_node_version()

        session = MonitorSessionModel(
            session_id=self._session_id,
            started_at=now_iso,
            ended_at=None,
            app_version=app_version,
            python_version=python_ver,
            node_version=node_ver,
            pid=self._main_pid,
            clean_exit=False,
        )

        # 1. Persist session record
        self._repo.insert_session(session)

        # 2. Record hardware and environment snapshot
        env_snapshot = inspector.inspect(self._session_id)
        self._repo.insert_environment_snapshot(env_snapshot)

        # 3. Recover any lingering runs from previous ungraceful exits
        self.recover_interrupted_runs()

        logger.info(
            "Resource Monitor session initialized: ID=%s, PID=%d, Python=%s, Node=%s",
            self._session_id,
            self._main_pid,
            python_ver,
            node_ver,
        )
        return session

    def close_session(self, clean_exit: bool = True) -> None:
        """Conclude the active monitoring session with clean termination status."""
        self._repo.close_session(self._session_id, clean_exit=clean_exit)
        logger.info("Resource Monitor session closed: ID=%s (clean_exit=%s)", self._session_id, clean_exit)

    def recover_interrupted_runs(self) -> int:
        """Identify and finalize runs left in PENDING or RUNNING status from earlier sessions."""
        now_iso = datetime.now(timezone.utc).isoformat()
        lingering_runs = self._repo.list_execution_runs(status=RunStatus.RUNNING, limit=100)
        lingering_runs += self._repo.list_execution_runs(status=RunStatus.PENDING, limit=100)

        recovered_count = 0
        for run in lingering_runs:
            # Only recover runs belonging to prior sessions or if PID rebooted
            if run.session_id != self._session_id:
                self._repo.update_execution_run(
                    run_id=run.run_id,
                    status=RunStatus.UNEXPECTED_TERMINATION,
                    ended_at=now_iso,
                )
                recovered_count += 1

        if recovered_count > 0:
            logger.warning(
                "Recovered %d dangling execution runs from previous unclosed sessions.",
                recovered_count,
            )
        return recovered_count

    # ========================================================================
    # 2. Execution Run Transitions
    # ========================================================================

    def create_run(
        self,
        run_id: str,
        job_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        engine_type: Optional[str] = None,
    ) -> ExecutionRunModel:
        """Register a new run in PENDING status prior to task scheduling."""
        run = ExecutionRunModel(
            run_id=run_id,
            session_id=self._session_id,
            job_id=job_id or run_id,
            chat_id=chat_id,
            agent_id=agent_id,
            engine_type=engine_type,
            status=RunStatus.PENDING,
            started_at=datetime.now(timezone.utc).isoformat(),
            ended_at=None,
            duration_ms=None,
            peak_rss_bytes=0,
            cumulative_cpu_time_ms=0,
            attributed_children_count=0,
            measurement_confidence=MeasurementConfidence.DIRECT,
        )
        self._repo.insert_execution_run(run)
        logger.debug("Execution run created in PENDING status: %s", run_id)
        return run

    def start_run(self, run_id: str) -> None:
        """Mark an execution run as RUNNING and capture initial memory baseline."""
        baseline_rss = self._get_current_main_rss()
        self._attribution.on_run_started(run_id, baseline_rss_bytes=baseline_rss)

        self._repo.update_execution_run(
            run_id=run_id,
            status=RunStatus.RUNNING,
            peak_rss_bytes=baseline_rss,
        )
        logger.debug("Execution run marked RUNNING: %s (baseline_rss=%d bytes)", run_id, baseline_rss)

    def complete_run(self, run_id: str) -> None:
        """Finalize an execution run successfully."""
        self._finalize_run(run_id, status=RunStatus.COMPLETED)

    def fail_run(self, run_id: str, error_message: Optional[str] = None) -> None:
        """Finalize an execution run with FAILED status."""
        self._finalize_run(run_id, status=RunStatus.FAILED)

    def cancel_run(self, run_id: str) -> None:
        """Finalize an execution run with CANCELLED status."""
        self._finalize_run(run_id, status=RunStatus.CANCELLED)

    def _finalize_run(self, run_id: str, status: RunStatus) -> None:
        """Internal helper to calculate final metrics, update database, and notify trackers."""
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        # Query existing record to calculate total duration
        run = self._repo.get_execution_run(run_id)
        duration_ms = None
        if run and run.started_at:
            try:
                start_dt = datetime.fromisoformat(run.started_at)
                duration_ms = int((now_dt - start_dt).total_seconds() * 1000)
            except Exception:
                pass

        # Query associated spans to inform final peak/CPU calculations
        spans = self._repo.list_spans_by_run(run_id)

        # Resolve final metrics BEFORE ending to ensure complete data capture
        final_metrics = self._attribution.resolve_final_metrics(run_id, spans=spans)

        # Conclude in-memory attribution
        self._attribution.on_run_ended(run_id)

        # Persist final execution run status
        self._repo.update_execution_run(
            run_id=run_id,
            status=status,
            ended_at=now_iso,
            duration_ms=duration_ms,
            peak_rss_bytes=final_metrics["peak_rss_bytes"],
            cumulative_cpu_time_ms=final_metrics["cumulative_cpu_time_ms"],
            attributed_children_count=final_metrics["attributed_children_count"],
            measurement_confidence=final_metrics["measurement_confidence"],
        )

        # Notify process tracker for orphan grace period evaluations
        self._process_tracker.on_run_ended(run_id)

        # Evict concluded attribution state
        self._attribution.cleanup_run(run_id)

        logger.info(
            "Execution run finalized: ID=%s, Status=%s, Duration=%sms, PeakRSS=%d bytes, Confidence=%s",
            run_id,
            status.value,
            duration_ms,
            final_metrics["peak_rss_bytes"],
            final_metrics["measurement_confidence"].value,
        )

    # ========================================================================
    # 3. Execution Span Tracking
    # ========================================================================

    def start_span(
        self,
        span_id: str,
        run_id: str,
        target_name: str,
        span_type: SpanType = SpanType.TOOL_CALL,
        parent_span_id: Optional[str] = None,
        process_instance_id: Optional[str] = None,
    ) -> ExecutionSpanModel:
        """Begin tracking a granular execution span (e.g. tool execution)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        current_rss = self._get_current_main_rss()

        span = ExecutionSpanModel(
            span_id=span_id,
            run_id=run_id,
            parent_span_id=parent_span_id,
            span_type=span_type,
            target_name=target_name,
            started_at=now_iso,
            ended_at=None,
            duration_ms=None,
            process_instance_id=process_instance_id,
            rss_start_bytes=current_rss,
            rss_end_bytes=None,
            rss_peak_bytes=current_rss,
            cpu_time_consumed_ms=0,
            status=SpanStatus.COMPLETED,
        )

        self._active_spans[span_id] = span
        self._span_start_times[span_id] = time.monotonic()
        self._repo.insert_execution_span(span)

        logger.debug("Span started: ID=%s, Target=%s, Run=%s", span_id, target_name, run_id)
        return span

    def end_span(
        self,
        span_id: str,
        status: SpanStatus = SpanStatus.COMPLETED,
        cpu_time_consumed_ms: Optional[int] = None,
    ) -> Optional[ExecutionSpanModel]:
        """Conclude an execution span and persist its duration and resource bounds."""
        span = self._active_spans.pop(span_id, None)
        start_mono = self._span_start_times.pop(span_id, None)

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        current_rss = self._get_current_main_rss()

        duration_ms = 0
        if start_mono is not None:
            duration_ms = int((time.monotonic() - start_mono) * 1000)

        peak_rss = max(current_rss, span.rss_start_bytes if span else 0)

        self._repo.update_execution_span(
            span_id=span_id,
            ended_at=now_iso,
            duration_ms=duration_ms,
            status=status,
            rss_end_bytes=current_rss,
            rss_peak_bytes=peak_rss,
            cpu_time_consumed_ms=cpu_time_consumed_ms or 0,
        )

        if span:
            span.ended_at = now_iso
            span.duration_ms = duration_ms
            span.status = status
            span.rss_end_bytes = current_rss
            span.rss_peak_bytes = peak_rss
            span.cpu_time_consumed_ms = cpu_time_consumed_ms or 0

        logger.debug("Span ended: ID=%s, Duration=%sms, PeakRSS=%d bytes", span_id, duration_ms, peak_rss)
        return span

    # ========================================================================
    # 4. Helper Utilities
    # ========================================================================

    def _get_current_main_rss(self) -> int:
        """Safely probe resident memory for the main PELDRUN process."""
        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                return int(psutil.Process(self._main_pid).memory_info().rss)
            except Exception:
                pass
        return 0
