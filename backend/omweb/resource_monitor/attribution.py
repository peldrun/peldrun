# backend/omweb/resource_monitor/attribution.py
"""Resource attribution and concurrency partitioning engine for PELDRUN.

Correlates machine-wide and process-tree telemetry with execution runs and tool
spans, preventing double-counting and determining measurement confidence levels
(DIRECT vs PROPORTIONAL vs ESTIMATED) under concurrent agent loads.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field

from .models import (
    ChildProcessMetric,
    ExecutionRunModel,
    ExecutionSpanModel,
    MeasurementConfidence,
    ResourceSampleModel,
)

logger = logging.getLogger(__name__)


class RunAttributionState(BaseModel):
    """In-memory active accounting tracker for a specific execution run."""
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    start_epoch_ms: int
    baseline_rss_bytes: int = 0
    peak_rss_bytes: int = 0
    cumulative_cpu_time_ms: int = 0
    attributed_pids: Set[int] = Field(default_factory=set)
    peak_concurrent_runs: int = 1
    had_proportional_attribution: bool = False
    is_active: bool = True


class AttributionEngine:
    """Computes run-level resource attribution and partitions shared process consumption."""

    def __init__(self) -> None:
        # Active runs catalog keyed by run_id
        self._active_runs: Dict[str, RunAttributionState] = {}
        # Reverse mapping: PID -> run_id for dedicated child subprocesses
        self._pid_to_run: Dict[int, str] = {}
        # Monotonic timestamp of the last sample processed per run for CPU rate calculations
        self._last_sample_mono: Optional[float] = None

    def on_run_started(self, run_id: str, baseline_rss_bytes: int = 0) -> RunAttributionState:
        """Register a newly commenced execution run for resource tracking.

        Args:
            run_id: Unique identifier of the execution run.
            baseline_rss_bytes: Initial resident memory of PELDRUN at run start.

        Returns:
            The initialized RunAttributionState.
        """
        now_epoch_ms = int(time.time() * 1000)
        current_active_count = len(self._active_runs) + 1

        state = RunAttributionState(
            run_id=run_id,
            start_epoch_ms=now_epoch_ms,
            baseline_rss_bytes=baseline_rss_bytes,
            peak_rss_bytes=baseline_rss_bytes,
            cumulative_cpu_time_ms=0,
            attributed_pids=set(),
            peak_concurrent_runs=current_active_count,
            had_proportional_attribution=(current_active_count > 1),
            is_active=True,
        )

        # Update existing active runs concurrency peak
        for existing_state in self._active_runs.values():
            if current_active_count > existing_state.peak_concurrent_runs:
                existing_state.peak_concurrent_runs = current_active_count
            existing_state.had_proportional_attribution = True

        self._active_runs[run_id] = state
        logger.debug(
            "Attribution registered for run %s (baseline_rss=%d bytes, active_concurrency=%d)",
            run_id,
            baseline_rss_bytes,
            current_active_count,
        )
        return state

    def on_run_ended(self, run_id: str) -> Optional[RunAttributionState]:
        """Conclude tracking for an execution run while retaining its state for final metrics."""
        state = self._active_runs.get(run_id)
        if not state:
            return None

        state.is_active = False

        # Disassociate dedicated child PIDs owned by this run
        pids_to_remove = [pid for pid, r_id in self._pid_to_run.items() if r_id == run_id]
        for pid in pids_to_remove:
            self._pid_to_run.pop(pid, None)

        logger.debug(
            "Attribution finalized for run %s (peak_rss=%d bytes, cumulative_cpu=%d ms)",
            run_id,
            state.peak_rss_bytes,
            state.cumulative_cpu_time_ms,
        )
        return state

    def associate_child_pid(self, run_id: str, pid: int) -> None:
        """Explicitly map a dedicated child process PID to a specific run."""
        state = self._active_runs.get(run_id)
        if state:
            state.attributed_pids.add(pid)
            self._pid_to_run[pid] = run_id
            logger.debug("Mapped child PID %d directly to run %s", pid, run_id)

    def process_sample(
        self,
        sample: ResourceSampleModel,
        active_children: List[ChildProcessMetric],
        current_time_mono: Optional[float] = None,
    ) -> None:
        """Partition live telemetry across all currently active runs.

        Args:
            sample: Latest high-frequency resource sample.
            active_children: List of currently running child process metrics.
            current_time_mono: Optional monotonic timestamp for precise CPU delta.
        """
        active_states = [s for s in self._active_runs.values() if s.is_active]
        active_count = len(active_states)
        if active_count == 0:
            self._last_sample_mono = current_time_mono or time.monotonic()
            return

        now_mono = current_time_mono or time.monotonic()
        time_delta_sec = 0.0
        if self._last_sample_mono is not None:
            time_delta_sec = max(0.0, now_mono - self._last_sample_mono)
        self._last_sample_mono = now_mono

        # Map children RSS and CPU by run
        children_rss_by_run: Dict[str, int] = {s.run_id: 0 for s in active_states}
        children_cpu_by_run: Dict[str, float] = {s.run_id: 0.0 for s in active_states}
        unattributed_children_rss = 0
        unattributed_children_cpu = 0.0

        for child in active_children:
            owner_run = self._pid_to_run.get(child.pid)
            if owner_run and owner_run in children_rss_by_run:
                children_rss_by_run[owner_run] += child.rss_bytes
                children_cpu_by_run[owner_run] += child.cpu_percent
            else:
                unattributed_children_rss += child.rss_bytes
                unattributed_children_cpu += child.cpu_percent

        # Main PELDRUN process delta partitioning
        main_rss = sample.peldrun_rss_bytes
        main_cpu = sample.peldrun_cpu_percent

        # If unattributed children exist, divide them evenly across active runs
        split_unattributed_rss = unattributed_children_rss // active_count
        split_unattributed_cpu = unattributed_children_cpu / active_count

        for state in active_states:
            r_id = state.run_id

            if active_count == 1:
                # Single active run: 100% direct attribution of main process + its children
                run_sample_rss = (
                    main_rss
                    + children_rss_by_run.get(r_id, 0)
                    + unattributed_children_rss
                )
                run_sample_cpu = (
                    main_cpu
                    + children_cpu_by_run.get(r_id, 0.0)
                    + unattributed_children_cpu
                )
            else:
                # Multiple concurrent runs: shared main process is proportionally partitioned
                shared_main_rss = main_rss // active_count
                shared_main_cpu = main_cpu / active_count

                run_sample_rss = (
                    shared_main_rss
                    + children_rss_by_run.get(r_id, 0)
                    + split_unattributed_rss
                )
                run_sample_cpu = (
                    shared_main_cpu
                    + children_cpu_by_run.get(r_id, 0.0)
                    + split_unattributed_cpu
                )
                state.had_proportional_attribution = True

            # Track peak resident memory
            if run_sample_rss > state.peak_rss_bytes:
                state.peak_rss_bytes = run_sample_rss

            # Calculate and accumulate CPU time in milliseconds
            if time_delta_sec > 0.0 and run_sample_cpu > 0.0:
                # CPU percent to consumed milliseconds: (cpu_percent / 100.0) * delta_sec * 1000
                delta_cpu_ms = int((run_sample_cpu / 100.0) * time_delta_sec * 1000.0)
                state.cumulative_cpu_time_ms += delta_cpu_ms

    def resolve_final_metrics(
        self,
        run_id: str,
        spans: Optional[List[ExecutionSpanModel]] = None,
    ) -> Dict[str, Any]:
        """Compile verified attribution summary for database finalization.

        Args:
            run_id: The run identifier being finalized.
            spans: Optional execution spans associated with this run.

        Returns:
            Dictionary containing peak_rss_bytes, cumulative_cpu_time_ms,
            attributed_children_count, and measurement_confidence.
        """
        state = self._active_runs.get(run_id)

        # Baseline defaults if state was never recorded
        peak_rss = 0
        cumulative_cpu_ms = 0
        children_count = 0
        confidence = MeasurementConfidence.DIRECT

        if state:
            peak_rss = state.peak_rss_bytes
            cumulative_cpu_ms = state.cumulative_cpu_time_ms
            children_count = len(state.attributed_pids)

            # Determine measurement confidence level
            if state.had_proportional_attribution or state.peak_concurrent_runs > 1:
                confidence = MeasurementConfidence.PROPORTIONAL
            else:
                confidence = MeasurementConfidence.DIRECT

        # Cross-reference with granular tool spans if provided
        if spans:
            spans_peak_rss = max((s.rss_peak_bytes or 0 for s in spans), default=0)
            spans_cpu_ms = sum((s.cpu_time_consumed_ms or 0 for s in spans))

            if spans_peak_rss > peak_rss:
                peak_rss = spans_peak_rss

            # If timeseries was too sparse, use accumulated tool span CPU times
            if spans_cpu_ms > cumulative_cpu_ms:
                cumulative_cpu_ms = spans_cpu_ms

        return {
            "peak_rss_bytes": peak_rss,
            "cumulative_cpu_time_ms": cumulative_cpu_ms,
            "attributed_children_count": children_count,
            "measurement_confidence": confidence,
        }

    @property
    def active_run_count(self) -> int:
        """Return the number of currently active execution runs."""
        return sum(1 for s in self._active_runs.values() if s.is_active)

    def get_run_state(self, run_id: str) -> Optional[RunAttributionState]:
        """Query in-memory attribution state for a run."""
        return self._active_runs.get(run_id)

    def cleanup_run(self, run_id: str) -> None:
        """Evict concluded run state from in-memory cache."""
        self._active_runs.pop(run_id, None)
