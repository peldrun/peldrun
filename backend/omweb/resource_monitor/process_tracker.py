# backend/omweb/resource_monitor/process_tracker.py
"""Process lifecycle tracker and orphan detection engine for PELDRUN Resource Monitor.

Maintains an indexed catalog of running subprocesses, tracks process parentage,
correlates processes with execution runs, detects orphaned subprocesses after
grace periods, and safely executes termination routines.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .capabilities import ResourceMonitorCapabilities, probe_capabilities
from .config import ResourceMonitorConfig, get_default_config
from .models import ChildProcessMetric, ProcessInstanceModel, ProcessType

logger = logging.getLogger(__name__)

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class ProcessTracker:
    """Tracks operating system subprocesses and isolates orphaned background workers."""

    def __init__(
        self,
        session_id: str,
        config: Optional[ResourceMonitorConfig] = None,
        capabilities: Optional[ResourceMonitorCapabilities] = None,
    ) -> None:
        self._session_id = session_id
        self._config = config or get_default_config()
        self._capabilities = capabilities or probe_capabilities()
        self._main_pid = os.getpid()

        # In-memory index of tracked process instances keyed by internal UUID
        self._instances_by_id: Dict[str, ProcessInstanceModel] = {}
        # Mapping from PID to process_instance_id
        self._pid_to_instance_id: Dict[int, str] = {}
        # Mapping from run_id to set of associated process_instance_ids
        self._run_processes: Dict[str, Set[str]] = {}
        # Tracking ended runs awaiting orphan grace period evaluation: run_id -> ended_monotonic_timestamp
        self._pending_orphan_evaluations: Dict[str, float] = {}

    def register_child_process(
        self,
        pid: int,
        run_id: Optional[str] = None,
        process_type: ProcessType = ProcessType.AUXILIARY,
        command_line: Optional[str] = None,
        name: Optional[str] = None,
    ) -> ProcessInstanceModel:
        """Register a newly spawned subprocess into the tracker index.

        Args:
            pid: The operating system process identifier.
            run_id: Optional execution run identifier creating this process.
            process_type: Classification category for the process.
            command_line: Invocation command line if known.
            name: Human-readable process executable name.

        Returns:
            The created ProcessInstanceModel.
        """
        # Return existing tracked instance if already cataloged
        if pid in self._pid_to_instance_id:
            existing_id = self._pid_to_instance_id[pid]
            instance = self._instances_by_id[existing_id]
            if run_id:
                self._run_processes.setdefault(run_id, set()).add(existing_id)
            return instance

        resolved_name = name or f"process_{pid}"
        resolved_ppid = self._main_pid
        resolved_cmdline = command_line

        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                proc = psutil.Process(pid)
                resolved_name = proc.name()
                resolved_ppid = proc.ppid()
                if not resolved_cmdline:
                    cmd_list = proc.cmdline()
                    resolved_cmdline = " ".join(cmd_list) if cmd_list else None
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        instance_id = str(uuid.uuid4())
        instance = ProcessInstanceModel(
            process_instance_id=instance_id,
            session_id=self._session_id,
            pid=pid,
            ppid=resolved_ppid,
            name=resolved_name,
            command_line=resolved_cmdline,
            process_type=process_type,
            created_at=datetime.now(timezone.utc).isoformat(),
            terminated_at=None,
            exit_code=None,
            is_orphan=False,
        )

        self._instances_by_id[instance_id] = instance
        self._pid_to_instance_id[pid] = instance_id

        if run_id:
            self._run_processes.setdefault(run_id, set()).add(instance_id)

        logger.debug(
            "Registered subprocess: PID %d, ID %s, Type: %s, Run: %s",
            pid,
            instance_id,
            process_type.value,
            run_id,
        )
        return instance

    def associate_run_process(self, run_id: str, pid: int) -> None:
        """Associate an existing or new PID with an execution run."""
        instance_id = self._pid_to_instance_id.get(pid)
        if not instance_id:
            instance = self.register_child_process(pid=pid, run_id=run_id)
            instance_id = instance.process_instance_id

        self._run_processes.setdefault(run_id, set()).add(instance_id)

    def on_run_ended(self, run_id: str) -> None:
        """Mark an execution run as concluded and schedule orphan grace period check."""
        if run_id in self._run_processes:
            self._pending_orphan_evaluations[run_id] = time.monotonic()
            logger.debug(
                "Run %s ended. Scheduled orphan grace period evaluation (%s seconds).",
                run_id,
                self._config.orphan_grace_period_sec,
            )

    def scan_and_update(self, current_children_metrics: List[ChildProcessMetric]) -> List[ProcessInstanceModel]:
        """Synchronize the tracker with real-time collector observations.

        Detects newly spawned processes, updates terminated processes, and evaluates
        pending orphan classifications.

        Args:
            current_children_metrics: Live metrics list gathered by ProcessCollector.

        Returns:
            List of currently active ProcessInstanceModels.
        """
        active_pids: Set[int] = {c.pid for c in current_children_metrics}
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Register any newly observed PIDs from collector
        for child_metric in current_children_metrics:
            if child_metric.pid not in self._pid_to_instance_id:
                instance = self.register_child_process(
                    pid=child_metric.pid,
                    process_type=child_metric.process_type,
                    command_line=child_metric.command_line,
                    name=child_metric.name,
                )
                child_metric.process_instance_id = instance.process_instance_id
            else:
                inst_id = self._pid_to_instance_id[child_metric.pid]
                child_metric.process_instance_id = inst_id
                child_metric.is_orphan = self._instances_by_id[inst_id].is_orphan

        # 2. Mark disappearance of previously active processes
        for pid, instance_id in list(self._pid_to_instance_id.items()):
            if pid not in active_pids:
                instance = self._instances_by_id[instance_id]
                if instance.terminated_at is None:
                    instance.terminated_at = now_iso
                    # Attempt to probe exit code if psutil is available
                    if _PSUTIL_AVAILABLE and psutil is not None:
                        try:
                            proc = psutil.Process(pid)
                            instance.exit_code = proc.wait(timeout=0.01)
                        except (psutil.NoSuchProcess, psutil.TimeoutExpired, Exception):
                            pass

                    logger.debug("Subprocess PID %d (ID %s) terminated.", pid, instance_id)

        # 3. Evaluate pending orphan grace periods
        self._evaluate_orphans()

        # Return list of currently alive process instances
        alive_instances: List[ProcessInstanceModel] = []
        for pid in active_pids:
            inst_id = self._pid_to_instance_id.get(pid)
            if inst_id and inst_id in self._instances_by_id:
                alive_instances.append(self._instances_by_id[inst_id])

        return alive_instances

    def _evaluate_orphans(self) -> None:
        """Identify processes remaining alive after their associated run has concluded."""
        now_mono = time.monotonic()
        grace_period = self._config.orphan_grace_period_sec
        evaluated_runs: List[str] = []

        for run_id, ended_timestamp in self._pending_orphan_evaluations.items():
            if (now_mono - ended_timestamp) >= grace_period:
                evaluated_runs.append(run_id)
                associated_instance_ids = self._run_processes.get(run_id, set())

                for inst_id in associated_instance_ids:
                    instance = self._instances_by_id.get(inst_id)
                    if not instance or instance.terminated_at is not None:
                        continue

                    # Verify if the process is physically still alive
                    is_alive = False
                    if _PSUTIL_AVAILABLE and psutil is not None:
                        try:
                            p = psutil.Process(instance.pid)
                            is_alive = p.is_running() and p.status() != psutil.STATUS_ZOMBIE
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            is_alive = False
                    else:
                        is_alive = True

                    if is_alive and not instance.is_orphan:
                        instance.is_orphan = True
                        logger.warning(
                            "Orphan process detected! PID: %d, Name: %s, Associated Run: %s (Survived > %ds)",
                            instance.pid,
                            instance.name,
                            run_id,
                            int(grace_period),
                        )

        for run_id in evaluated_runs:
            self._pending_orphan_evaluations.pop(run_id, None)

    def terminate_process(self, pid: int, force: bool = False) -> tuple[bool, str]:
        """Terminate an orphaned or rogue subprocess safely.

        Args:
            pid: The process identifier to terminate.
            force: When True, issues immediate SIGKILL (or kill()); otherwise attempts graceful SIGTERM.

        Returns:
            Tuple of (success_boolean, human_readable_status_message).
        """
        # Guard against terminating the main PELDRUN server process
        if pid == self._main_pid:
            return False, "Cannot terminate PELDRUN main process"

        if not _PSUTIL_AVAILABLE or psutil is None:
            return False, "psutil library unavailable for process management"

        try:
            target_proc = psutil.Process(pid)

            # Safety check: Verify the target is indeed a child of the main process or tracked
            if pid not in self._pid_to_instance_id:
                try:
                    parent = target_proc.parent()
                    if parent is None or parent.pid != self._main_pid:
                        return False, f"Process {pid} is not a child of PELDRUN"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return False, f"Process {pid} cannot be verified as a child"

            if force:
                target_proc.kill()
                msg = f"Process {pid} killed forcefully."
            else:
                target_proc.terminate()
                try:
                    target_proc.wait(timeout=2.0)
                    msg = f"Process {pid} terminated gracefully."
                except psutil.TimeoutExpired:
                    target_proc.kill()
                    msg = f"Process {pid} did not terminate within timeout; killed forcefully."

            # Update tracked instance status
            inst_id = self._pid_to_instance_id.get(pid)
            if inst_id and inst_id in self._instances_by_id:
                self._instances_by_id[inst_id].terminated_at = datetime.now(timezone.utc).isoformat()
                self._instances_by_id[inst_id].is_orphan = False

            return True, msg

        except psutil.NoSuchProcess:
            return True, f"Process {pid} has already exited."
        except psutil.AccessDenied as exc:
            return False, f"Access denied terminating process {pid}: {exc}"
        except Exception as exc:
            return False, f"Unexpected error terminating process {pid}: {exc}"

    def get_tracked_instance(self, instance_id: str) -> Optional[ProcessInstanceModel]:
        """Retrieve a tracked process instance by its ID."""
        return self._instances_by_id.get(instance_id)

    def get_instance_by_pid(self, pid: int) -> Optional[ProcessInstanceModel]:
        """Retrieve a tracked process instance by its operating system PID."""
        inst_id = self._pid_to_instance_id.get(pid)
        return self._instances_by_id.get(inst_id) if inst_id else None

    def list_active_orphans(self) -> List[ProcessInstanceModel]:
        """Return all tracked processes marked as active orphans."""
        return [
            inst for inst in self._instances_by_id.values()
            if inst.is_orphan and inst.terminated_at is None
        ]
