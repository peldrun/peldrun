# backend/omweb/resource_monitor/diagnostics.py
"""Diagnostic heuristics and anomaly detection engine for PELDRUN.

Evaluates orphaned subprocess survival, tracks steady-state baseline memory creep
(memory leaks), detects host CPU saturation, and generates structured diagnostic events.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import gc
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set
import uuid

from .config import ResourceMonitorConfig, get_default_config
from .models import (
    DiagnosticEventModel,
    DiagnosticEventType,
    DiagnosticSeverity,
    ProcessInstanceModel,
)
from .process_tracker import ProcessTracker
from .storage.repositories import MonitorRepository

logger = logging.getLogger(__name__)


class DiagnosticsEngine:
    """Detects operational resource anomalies including leaks, orphans, and hardware saturation."""

    def __init__(
        self,
        repository: MonitorRepository,
        process_tracker: ProcessTracker,
        config: Optional[ResourceMonitorConfig] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self._repo = repository
        self._process_tracker = process_tracker
        self._config = config or get_default_config()
        self._session_id = session_id or str(uuid.uuid4())

        # Set of PIDs already alerted to prevent alert flooding
        self._alerted_orphan_pids: Set[int] = set()

        # History of post-run idle RSS readings to detect memory creep
        self._idle_baseline_history: List[int] = []
        self._last_creep_alert_mono: float = 0.0

        # Monotonic timestamp marking the start of continuous CPU saturation
        self._cpu_saturation_start_mono: Optional[float] = None
        self._last_saturation_alert_mono: float = 0.0

    def evaluate_orphans(self) -> List[DiagnosticEventModel]:
        """Check process tracker for orphaned processes that have survived the grace period.

        Returns:
            List of newly generated DiagnosticEventModel instances for unalerted orphans.
        """
        active_orphans = self._process_tracker.list_active_orphans()
        active_orphan_pids = {proc.pid for proc in active_orphans}

        # Clear tracking for orphans that have finally terminated
        self._alerted_orphan_pids = {
            pid for pid in self._alerted_orphan_pids if pid in active_orphan_pids
        }

        new_events: List[DiagnosticEventModel] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for orphan in active_orphans:
            if orphan.pid in self._alerted_orphan_pids:
                continue

            event = DiagnosticEventModel(
                event_id=str(uuid.uuid4()),
                session_id=self._session_id,
                run_id=None,
                recorded_at=now_iso,
                event_type=DiagnosticEventType.ORPHAN_PROCESS,
                severity=DiagnosticSeverity.CRITICAL,
                message=(
                    f"Orphan process detected: '{orphan.name}' (PID: {orphan.pid}) "
                    f"remained active after run termination grace period."
                ),
                details_json=json.dumps({
                    "pid": orphan.pid,
                    "ppid": orphan.ppid,
                    "name": orphan.name,
                    "command_line": orphan.command_line,
                    "process_type": orphan.process_type.value,
                    "created_at": orphan.created_at,
                    "grace_period_sec": self._config.orphan_grace_period_sec,
                }),
            )

            try:
                self._repo.insert_diagnostic_event(event)
                new_events.append(event)
                self._alerted_orphan_pids.add(orphan.pid)
                logger.warning("Diagnostic alert logged: %s", event.message)
            except Exception as exc:
                logger.error("Failed recording orphan diagnostic event for PID %d: %s", orphan.pid, exc)

        return new_events

    def record_idle_baseline(self, current_peldrun_rss_bytes: int) -> Optional[DiagnosticEventModel]:
        """Record post-run idle memory and evaluate whether baseline memory creep is occurring.

        Args:
            current_peldrun_rss_bytes: Resident memory of the main process during idle.

        Returns:
            A DiagnosticEventModel if memory creep is diagnosed, otherwise None.
        """
        if current_peldrun_rss_bytes <= 0:
            return None

        # Perform a light garbage collection pass before taking baseline reading
        gc.collect()

        self._idle_baseline_history.append(current_peldrun_rss_bytes)

        # Retain last 10 baseline samples
        if len(self._idle_baseline_history) > 10:
            self._idle_baseline_history.pop(0)

        # Require at least 5 consecutive idle baseline observations
        if len(self._idle_baseline_history) < 5:
            return None

        # Throttle creep alerts to at most once every 10 minutes (600 seconds)
        now_mono = time.monotonic()
        if (now_mono - self._last_creep_alert_mono) < 600.0:
            return None

        # Heuristic: Check if each baseline strictly increases or has sustained upward creep (>25% net growth)
        recent = self._idle_baseline_history[-5:]
        initial_baseline = recent[0]
        latest_baseline = recent[-1]

        is_strictly_growing = all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))
        growth_percent = ((latest_baseline - initial_baseline) / initial_baseline) * 100.0 if initial_baseline > 0 else 0.0

        if is_strictly_growing and growth_percent >= 25.0:
            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()
            from_iso = (now_dt - timedelta(hours=2)).isoformat()

            # Retrieve recently executed tools to correlate suspect leak sources
            tool_summaries = self._repo.get_tool_consumption_summary(from_iso, now_iso)
            top_tools = [
                {"tool_name": t.tool_name, "calls": t.total_calls, "peak_rss": t.peak_rss_bytes}
                for t in tool_summaries[:5]
            ]

            event = DiagnosticEventModel(
                event_id=str(uuid.uuid4()),
                session_id=self._session_id,
                run_id=None,
                recorded_at=now_iso,
                event_type=DiagnosticEventType.MEMORY_CREEP,
                severity=DiagnosticSeverity.WARNING,
                message=(
                    f"Potential memory leak suspected: Idle memory steadily grew by "
                    f"{growth_percent:.1f}% across 5 consecutive execution cycles "
                    f"({initial_baseline // (1024 * 1024)}MB -> {latest_baseline // (1024 * 1024)}MB)."
                ),
                details_json=json.dumps({
                    "baseline_samples_bytes": recent,
                    "growth_percent": round(growth_percent, 2),
                    "suspect_tools_in_window": top_tools,
                }),
            )

            try:
                self._repo.insert_diagnostic_event(event)
                self._last_creep_alert_mono = now_mono
                logger.warning("Diagnostic alert logged: %s", event.message)
                return event
            except Exception as exc:
                logger.error("Failed recording memory creep diagnostic event: %s", exc)

        return None

    def evaluate_cpu_saturation(
        self,
        host_cpu_percent: float,
        duration_threshold_sec: float = 15.0,
    ) -> Optional[DiagnosticEventModel]:
        """Detect sustained host CPU saturation exceeding the configured throttle threshold.

        Args:
            host_cpu_percent: Current instantaneous CPU usage percentage of the host.
            duration_threshold_sec: Continuous saturation duration before generating an alert.

        Returns:
            A DiagnosticEventModel if saturation is sustained, otherwise None.
        """
        now_mono = time.monotonic()
        threshold = max(85.0, self._config.cpu_throttle_threshold_percent)

        if host_cpu_percent >= threshold:
            if self._cpu_saturation_start_mono is None:
                self._cpu_saturation_start_mono = now_mono
                return None

            saturated_duration = now_mono - self._cpu_saturation_start_mono
            # Check if saturated for longer than threshold and respect 5-minute alert cooldown
            if saturated_duration >= duration_threshold_sec and (now_mono - self._last_saturation_alert_mono) >= 300.0:
                severity = (
                    DiagnosticSeverity.CRITICAL
                    if host_cpu_percent >= 95.0
                    else DiagnosticSeverity.WARNING
                )
                now_iso = datetime.now(timezone.utc).isoformat()

                event = DiagnosticEventModel(
                    event_id=str(uuid.uuid4()),
                    session_id=self._session_id,
                    run_id=None,
                    recorded_at=now_iso,
                    event_type=DiagnosticEventType.CPU_SATURATION,
                    severity=severity,
                    message=(
                        f"Host CPU saturation detected: Usage reached {host_cpu_percent:.1f}% "
                        f"and remained above {threshold:.0f}% for {saturated_duration:.1f} seconds."
                    ),
                    details_json=json.dumps({
                        "host_cpu_percent": host_cpu_percent,
                        "threshold_percent": threshold,
                        "saturated_duration_sec": round(saturated_duration, 1),
                    }),
                )

                try:
                    self._repo.insert_diagnostic_event(event)
                    self._last_saturation_alert_mono = now_mono
                    logger.warning("Diagnostic alert logged: %s", event.message)
                    return event
                except Exception as exc:
                    logger.error("Failed recording CPU saturation diagnostic event: %s", exc)
        else:
            # Reset saturation start timer once CPU falls below threshold
            self._cpu_saturation_start_mono = None

        return None

    def evaluate_all(
        self,
        host_cpu_percent: Optional[float] = None,
        idle_peldrun_rss_bytes: Optional[int] = None,
    ) -> List[DiagnosticEventModel]:
        """Run all diagnostic evaluations synchronously."""
        events: List[DiagnosticEventModel] = []

        orphan_events = self.evaluate_orphans()
        events.extend(orphan_events)

        if idle_peldrun_rss_bytes is not None:
            creep_event = self.record_idle_baseline(idle_peldrun_rss_bytes)
            if creep_event:
                events.append(creep_event)

        if host_cpu_percent is not None:
            sat_event = self.evaluate_cpu_saturation(host_cpu_percent)
            if sat_event:
                events.append(sat_event)

        return events

    async def async_evaluate_all(
        self,
        host_cpu_percent: Optional[float] = None,
        idle_peldrun_rss_bytes: Optional[int] = None,
    ) -> List[DiagnosticEventModel]:
        """Asynchronously run all diagnostic evaluations via worker thread."""
        return await asyncio.to_thread(
            self.evaluate_all,
            host_cpu_percent,
            idle_peldrun_rss_bytes,
        )
