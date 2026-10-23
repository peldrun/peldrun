# backend/omweb/resource_monitor/collectors/disk.py
"""Disk volume and I/O rate metric collector for PELDRUN Resource Monitor.

Measures disk volume capacity, workspace folder consumption, and system/process
disk I/O read/write throughput rates.
"""

from __future__ import annotations

import logging
import os
import time
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


class DiskCollector(BaseCollector):
    """Collector responsible for storage capacity and I/O throughput measurements."""

    def __init__(
        self,
        capabilities: Optional[ResourceMonitorCapabilities] = None,
        workspace_path: Optional[str] = None,
    ) -> None:
        super().__init__(capabilities=capabilities)
        self._workspace_path = workspace_path or os.getcwd()
        self._last_io_counters: Optional[Any] = None
        self._last_io_timestamp: Optional[float] = None

        # Seed baseline disk I/O metrics if available
        if _PSUTIL_AVAILABLE and psutil is not None and self._capabilities.can_measure_io_counters:
            try:
                self._last_io_counters = psutil.disk_io_counters()
                self._last_io_timestamp = time.monotonic()
            except Exception as exc:
                logger.debug("Failed to initialize baseline disk I/O counters: %s", exc)

    @property
    def collector_name(self) -> str:
        """Unique identifier name for this collector instance."""
        return "disk"

    def is_available(self) -> bool:
        """Verify storage telemetry availability."""
        return True

    def _collect_internal(self) -> Dict[str, Any]:
        """Collect storage capacity and rate metrics safely.

        Returns:
            Dictionary containing storage usage and I/O throughput metrics.
        """
        now = time.monotonic()
        workspace_usage = self._get_workspace_usage()
        io_rates = self._get_disk_io_rates(now)

        return {
            "workspace_path": self._workspace_path,
            "workspace_total_bytes": workspace_usage["total_bytes"],
            "workspace_used_bytes": workspace_usage["used_bytes"],
            "workspace_free_bytes": workspace_usage["free_bytes"],
            "workspace_used_percent": workspace_usage["used_percent"],
            "read_bytes_per_sec": io_rates["read_bytes_per_sec"],
            "write_bytes_per_sec": io_rates["write_bytes_per_sec"],
            "read_count_per_sec": io_rates["read_count_per_sec"],
            "write_count_per_sec": io_rates["write_count_per_sec"],
        }

    def _get_workspace_usage(self) -> Dict[str, Any]:
        """Inspect storage volume capacity for the workspace path."""
        fallback = {
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "used_percent": 0.0,
        }

        target_path = self._workspace_path if Path(self._workspace_path).exists() else "."

        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                usage = psutil.disk_usage(target_path)
                return {
                    "total_bytes": int(usage.total),
                    "used_bytes": int(usage.used),
                    "free_bytes": int(usage.free),
                    "used_percent": float(usage.percent),
                }
            except (OSError, PermissionError):
                pass

        # Platform fallback using os.statvfs (POSIX)
        try:
            stat = os.statvfs(target_path)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            percent = (used / total * 100.0) if total > 0 else 0.0
            return {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_percent": round(percent, 2),
            }
        except (AttributeError, OSError):
            pass

        return fallback

    def _get_disk_io_rates(self, current_time: float) -> Dict[str, float]:
        """Compute system-wide disk I/O rates since the previous sample."""
        rates = {
            "read_bytes_per_sec": 0.0,
            "write_bytes_per_sec": 0.0,
            "read_count_per_sec": 0.0,
            "write_count_per_sec": 0.0,
        }

        if not _PSUTIL_AVAILABLE or psutil is None or not self._capabilities.can_measure_io_counters:
            return rates

        try:
            current_counters = psutil.disk_io_counters()
            if current_counters is None:
                return rates

            if self._last_io_counters is not None and self._last_io_timestamp is not None:
                delta_t = current_time - self._last_io_timestamp
                if delta_t > 0.05:  # Avoid division by micro-deltas
                    delta_read_bytes = current_counters.read_bytes - self._last_io_counters.read_bytes
                    delta_write_bytes = current_counters.write_bytes - self._last_io_counters.write_bytes
                    delta_read_count = current_counters.read_count - self._last_io_counters.read_count
                    delta_write_count = current_counters.write_count - self._last_io_counters.write_count

                    rates["read_bytes_per_sec"] = max(0.0, float(delta_read_bytes / delta_t))
                    rates["write_bytes_per_sec"] = max(0.0, float(delta_write_bytes / delta_t))
                    rates["read_count_per_sec"] = max(0.0, float(delta_read_count / delta_t))
                    rates["write_count_per_sec"] = max(0.0, float(delta_write_count / delta_t))

            self._last_io_counters = current_counters
            self._last_io_timestamp = current_time
        except (AttributeError, NotImplementedError, OSError, PermissionError) as exc:
            logger.debug("Failed to read disk I/O counters: %s", exc)

        return rates
