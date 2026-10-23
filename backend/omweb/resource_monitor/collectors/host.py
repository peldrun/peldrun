# backend/omweb/resource_monitor/collectors/host.py
"""Host-level resource metric collector for PELDRUN Resource Monitor.

Collects machine-wide hardware utilization metrics including non-blocking CPU usage,
physical RAM allocation, and primary disk volume consumption.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional

from ..capabilities import ResourceMonitorCapabilities, probe_capabilities
from .base import BaseCollector

logger = logging.getLogger(__name__)

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class HostCollector(BaseCollector):
    """Collector responsible for gathering machine-wide host metrics."""

    def __init__(self, capabilities: Optional[ResourceMonitorCapabilities] = None) -> None:
        super().__init__(capabilities=capabilities)
        self._target_disk_path = self._resolve_disk_root()
        # Block briefly on the very first call to establish a valid CPU baseline
        # so that the first real sample returns accurate data instead of 0.0.
        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                psutil.cpu_percent(interval=0.1)
            except Exception:
                pass

    @property
    def collector_name(self) -> str:
        """Unique identifier name for this collector instance."""
        return "host"

    def is_available(self) -> bool:
        """Verify availability of host monitoring mechanisms."""
        if _PSUTIL_AVAILABLE and psutil is not None:
            return True
        return self._capabilities.is_linux

    def _resolve_disk_root(self) -> str:
        """Resolve the appropriate root path for disk capacity inspection."""
        if self._capabilities.is_windows:
            system_drive = os.environ.get("SystemDrive", "C:")
            return f"{system_drive}\\" if not system_drive.endswith("\\") else system_drive
        return "/"

    def _collect_internal(self) -> Dict[str, Any]:
        """Collect host metrics with non-blocking system calls.

        Returns:
            Dictionary payload compliant with HostMetrics schema.
        """
        if _PSUTIL_AVAILABLE and psutil is not None:
            return self._collect_psutil()

        if self._capabilities.is_linux:
            return self._collect_linux_proc()

        return {
            "cpu_percent": 0.0,
            "ram_total_bytes": 0,
            "ram_used_bytes": 0,
            "ram_available_bytes": 0,
            "ram_percent": 0.0,
            "disk_total_bytes": None,
            "disk_used_bytes": None,
            "disk_percent": None,
        }

    def _collect_psutil(self) -> Dict[str, Any]:
        """Gather host metrics via psutil library."""
        assert psutil is not None

        # Non-blocking CPU measurement since previous query
        cpu_percent = float(psutil.cpu_percent(interval=None))

        # RAM telemetry
        vmem = psutil.virtual_memory()
        ram_total = int(vmem.total)
        ram_available = int(vmem.available)
        ram_used = int(vmem.used)
        ram_percent = float(vmem.percent)

        # Primary storage volume telemetry
        disk_total: Optional[int] = None
        disk_used: Optional[int] = None
        disk_percent: Optional[float] = None

        if self._capabilities.can_measure_io_counters or Path(self._target_disk_path).exists():
            try:
                usage = psutil.disk_usage(self._target_disk_path)
                disk_total = int(usage.total)
                disk_used = int(usage.used)
                disk_percent = float(usage.percent)
            except (OSError, PermissionError):
                pass

        return {
            "cpu_percent": cpu_percent,
            "ram_total_bytes": ram_total,
            "ram_used_bytes": ram_used,
            "ram_available_bytes": ram_available,
            "ram_percent": ram_percent,
            "disk_total_bytes": disk_total,
            "disk_used_bytes": disk_used,
            "disk_percent": disk_percent,
        }

    def _collect_linux_proc(self) -> Dict[str, Any]:
        """Fallback collection via Linux /proc pseudofiles when psutil is unavailable."""
        ram_total = 0
        ram_available = 0

        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            try:
                for line in meminfo.read_text(encoding="utf-8").splitlines():
                    if line.startswith("MemTotal:"):
                        ram_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        ram_available = int(line.split()[1]) * 1024
            except (OSError, ValueError):
                pass

        ram_used = max(0, ram_total - ram_available)
        ram_percent = (ram_used / ram_total * 100.0) if ram_total > 0 else 0.0

        # Disk space via os.statvfs
        disk_total: Optional[int] = None
        disk_used: Optional[int] = None
        disk_percent: Optional[float] = None

        try:
            stat = os.statvfs("/")
            disk_total = stat.f_blocks * stat.f_frsize
            disk_free = stat.f_bavail * stat.f_frsize
            disk_used = disk_total - disk_free
            disk_percent = (disk_used / disk_total * 100.0) if disk_total > 0 else 0.0
        except (AttributeError, OSError):
            pass

        return {
            "cpu_percent": 0.0,
            "ram_total_bytes": ram_total,
            "ram_used_bytes": ram_used,
            "ram_available_bytes": ram_available,
            "ram_percent": ram_percent,
            "disk_total_bytes": disk_total,
            "disk_used_bytes": disk_used,
            "disk_percent": disk_percent,
        }
