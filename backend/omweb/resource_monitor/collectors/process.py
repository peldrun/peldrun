# backend/omweb/resource_monitor/collectors/process.py
"""Process and subprocess tree metric collector for PELDRUN Resource Monitor.

Gathers memory, CPU utilization, thread counts, and process categorization
for the PELDRUN main process and all recursively spawned child instances.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from ..capabilities import ResourceMonitorCapabilities, probe_capabilities
from ..models import ChildProcessMetric, ProcessType
from .base import BaseCollector

logger = logging.getLogger(__name__)

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class ProcessCollector(BaseCollector):
    """Collector responsible for measuring the PELDRUN process and subprocess hierarchy."""

    def __init__(self, capabilities: Optional[ResourceMonitorCapabilities] = None) -> None:
        super().__init__(capabilities=capabilities)
        self._main_pid = os.getpid()
        self._main_process: Optional[Any] = None
        # Cache psutil.Process references to maintain continuous cpu_percent calculation baselines
        self._process_cache: Dict[int, Any] = {}

        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                self._main_process = psutil.Process(self._main_pid)
                self._main_process.cpu_percent(interval=None)
            except Exception as exc:
                logger.warning("Failed to initialize main process handle for PID %d: %s", self._main_pid, exc)

    @property
    def collector_name(self) -> str:
        """Unique identifier name for this collector instance."""
        return "process"

    def is_available(self) -> bool:
        """Verify psutil availability and process handle validity."""
        return _PSUTIL_AVAILABLE and psutil is not None and self._main_process is not None

    def _collect_internal(self) -> Dict[str, Any]:
        """Collect metrics for main process and all active child processes.

        Returns:
            Dictionary containing 'peldrun' host/tree metrics and 'active_children' list.
        """
        assert self._main_process is not None and psutil is not None

        try:
            # Query main process metrics
            with self._main_process.oneshot():
                main_cpu_percent = float(self._main_process.cpu_percent(interval=None))
                main_mem = self._main_process.memory_info()
                main_rss = int(main_mem.rss)
                main_vms = int(main_mem.vms)
                threads_count = int(self._main_process.num_threads())

            # Query all recursive children
            children_instances = self._main_process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
            logger.warning("Failed to query main PELDRUN process PID %d: %s", self._main_pid, exc)
            return self._empty_metrics()

        active_children_metrics: List[Dict[str, Any]] = []
        children_rss_bytes = 0
        current_child_pids = set()

        for child in children_instances:
            child_pid = child.pid
            current_child_pids.add(child_pid)

            # Reuse cached process instance if present to get accurate cpu_percent delta
            proc_handle = self._process_cache.get(child_pid, child)
            self._process_cache[child_pid] = proc_handle

            try:
                with proc_handle.oneshot():
                    name = proc_handle.name()
                    try:
                        cmdline_list = proc_handle.cmdline()
                        cmdline = " ".join(cmdline_list) if cmdline_list else None
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        cmdline = None

                    mem_info = proc_handle.memory_info()
                    rss = int(mem_info.rss)
                    cpu_perc = float(proc_handle.cpu_percent(interval=None))
                    ppid = int(proc_handle.ppid())
                    create_time = float(proc_handle.create_time())

                proc_type = self._classify_process(name, cmdline)
                children_rss_bytes += rss

                child_metric = ChildProcessMetric(
                    process_instance_id=None,
                    pid=child_pid,
                    ppid=ppid,
                    name=name,
                    process_type=proc_type,
                    rss_bytes=rss,
                    cpu_percent=cpu_perc,
                    create_time=create_time,
                    is_orphan=False,
                    command_line=cmdline,
                )
                active_children_metrics.append(child_metric.model_dump())

            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                # Retain partial info when restricted
                active_children_metrics.append({
                    "process_instance_id": None,
                    "pid": child_pid,
                    "ppid": self._main_pid,
                    "name": "access_denied",
                    "process_type": ProcessType.AUXILIARY.value,
                    "rss_bytes": 0,
                    "cpu_percent": 0.0,
                    "create_time": 0.0,
                    "is_orphan": False,
                    "command_line": None,
                })

        # Evict terminated processes from cache
        evict_pids = [pid for pid in self._process_cache if pid not in current_child_pids]
        for pid in evict_pids:
            self._process_cache.pop(pid, None)

        total_rss = main_rss + children_rss_bytes

        peldrun_summary = {
            "pid": self._main_pid,
            "cpu_percent": main_cpu_percent,
            "main_rss_bytes": main_rss,
            "children_rss_bytes": children_rss_bytes,
            "total_rss_bytes": total_rss,
            "total_vms_bytes": main_vms,
            "children_count": len(active_children_metrics),
            "threads_count": threads_count,
        }

        return {
            "peldrun": peldrun_summary,
            "active_children": active_children_metrics,
        }

    @staticmethod
    def _classify_process(name: str, cmdline: Optional[str]) -> ProcessType:
        """Classify a child process based on executable name and command line args."""
        target_str = f"{name} {cmdline or ''}".lower()

        # Browser processes (Playwright / Chromium / Chrome / Edge)
        if any(sig in target_str for sig in ("chrome", "chromium", "playwright", "msedge")):
            return ProcessType.BROWSER_PLAYWRIGHT

        # Shell and command interpreters
        if any(sig in target_str for sig in ("bash", "sh", "zsh", "cmd.exe", "powershell")):
            return ProcessType.BASH_WORKER

        # Isolated Python workers
        if "python" in target_str:
            return ProcessType.ISOLATED_PYTHON

        return ProcessType.AUXILIARY

    def _empty_metrics(self) -> Dict[str, Any]:
        """Return safe fallback metrics payload."""
        return {
            "peldrun": {
                "pid": self._main_pid,
                "cpu_percent": 0.0,
                "main_rss_bytes": 0,
                "children_rss_bytes": 0,
                "total_rss_bytes": 0,
                "total_vms_bytes": 0,
                "children_count": 0,
                "threads_count": 0,
            },
            "active_children": [],
        }
