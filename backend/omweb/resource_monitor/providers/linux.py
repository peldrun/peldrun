# backend/omweb/resource_monitor/providers/linux.py
"""Linux-specific platform provider for PELDRUN Resource Monitor.

Directly reads Linux kernel pseudofiles (/proc and /sys/fs/cgroup) to extract
host hardware metrics, memory limits, and container cgroups v1/v2 constraints.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_IS_LINUX = platform.system().lower() == "linux"


class LinuxProvider:
    """Provides direct low-level Linux metrics via /proc and cgroups hierarchy."""

    def __init__(self) -> None:
        self._is_linux = _IS_LINUX
        self._cgroup_v2_root = Path("/sys/fs/cgroup")
        self._cgroup_v1_memory = Path("/sys/fs/cgroup/memory")
        self._cgroup_v1_cpu = Path("/sys/fs/cgroup/cpu")
        self._cgroup_v1_cpuacct = Path("/sys/fs/cgroup/cpuacct")

    def is_supported(self) -> bool:
        """Check whether Linux native inspection is available."""
        return self._is_linux

    def get_cgroup_limits(self) -> Dict[str, Any]:
        """Detect cgroups version and extract enforced container resource constraints.

        Returns:
            Dictionary containing cgroups version, memory limit, current memory,
            CPU quota, and CPU period in microseconds.
        """
        results: Dict[str, Any] = {
            "cgroup_version": None,
            "memory_limit_bytes": None,
            "memory_current_bytes": None,
            "cpu_quota_us": None,
            "cpu_period_us": None,
            "cpu_usage_usec": None,
        }

        if not self.is_supported():
            return results

        # 1. Attempt cgroups v2 unified hierarchy
        if (self._cgroup_v2_root / "cgroup.controllers").is_file():
            results["cgroup_version"] = 2
            self._read_cgroups_v2(results)
            return results

        # 2. Fallback to cgroups v1 legacy controllers
        if self._cgroup_v1_memory.is_dir() or self._cgroup_v1_cpu.is_dir():
            results["cgroup_version"] = 1
            self._read_cgroups_v1(results)
            return results

        return results

    def _read_cgroups_v2(self, out: Dict[str, Any]) -> None:
        """Read constraints from cgroups v2 unified control files."""
        # Memory limit and current usage
        mem_max_file = self._cgroup_v2_root / "memory.max"
        if mem_max_file.is_file():
            try:
                content = mem_max_file.read_text(encoding="utf-8").strip()
                if content and content != "max":
                    out["memory_limit_bytes"] = int(content)
            except (OSError, ValueError):
                pass

        mem_cur_file = self._cgroup_v2_root / "memory.current"
        if mem_cur_file.is_file():
            try:
                content = mem_cur_file.read_text(encoding="utf-8").strip()
                if content:
                    out["memory_current_bytes"] = int(content)
            except (OSError, ValueError):
                pass

        # CPU quota and period
        cpu_max_file = self._cgroup_v2_root / "cpu.max"
        if cpu_max_file.is_file():
            try:
                content = cpu_max_file.read_text(encoding="utf-8").strip()
                parts = content.split()
                if len(parts) >= 2:
                    if parts[0] != "max":
                        out["cpu_quota_us"] = int(parts[0])
                    out["cpu_period_us"] = int(parts[1])
            except (OSError, ValueError):
                pass

        # Cumulative CPU usage
        cpu_stat_file = self._cgroup_v2_root / "cpu.stat"
        if cpu_stat_file.is_file():
            try:
                for line in cpu_stat_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("usage_usec"):
                        parts = line.split()
                        if len(parts) >= 2:
                            out["cpu_usage_usec"] = int(parts[1])
            except (OSError, ValueError):
                pass

    def _read_cgroups_v1(self, out: Dict[str, Any]) -> None:
        """Read constraints from cgroups v1 individual controllers."""
        # Memory limit and usage
        mem_limit_file = self._cgroup_v1_memory / "memory.limit_in_bytes"
        if mem_limit_file.is_file():
            try:
                val = int(mem_limit_file.read_text(encoding="utf-8").strip())
                # Ignore sentinel values representing unlimited memory (~9e18)
                if val < (1 << 62):
                    out["memory_limit_bytes"] = val
            except (OSError, ValueError):
                pass

        mem_usage_file = self._cgroup_v1_memory / "memory.usage_in_bytes"
        if mem_usage_file.is_file():
            try:
                out["memory_current_bytes"] = int(mem_usage_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass

        # CPU quota and period
        cfs_quota_file = self._cgroup_v1_cpu / "cpu.cfs_quota_us"
        if cfs_quota_file.is_file():
            try:
                quota = int(cfs_quota_file.read_text(encoding="utf-8").strip())
                if quota > 0:
                    out["cpu_quota_us"] = quota
            except (OSError, ValueError):
                pass

        cfs_period_file = self._cgroup_v1_cpu / "cpu.cfs_period_us"
        if cfs_period_file.is_file():
            try:
                out["cpu_period_us"] = int(cfs_period_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass

        # Cumulative CPU usage via cpuacct
        cpuacct_usage_file = self._cgroup_v1_cpuacct / "cpuacct.usage"
        if cpuacct_usage_file.is_file():
            try:
                # Value is stored in nanoseconds, convert to microseconds
                nanosecs = int(cpuacct_usage_file.read_text(encoding="utf-8").strip())
                out["cpu_usage_usec"] = nanosecs // 1000
            except (OSError, ValueError):
                pass

    def get_proc_meminfo(self) -> Dict[str, int]:
        """Parse /proc/meminfo into a byte-denominated metrics dictionary.

        Returns:
            Dictionary containing total, free, available, buffers, cached, and swap bytes.
        """
        metrics: Dict[str, int] = {
            "total_bytes": 0,
            "free_bytes": 0,
            "available_bytes": 0,
            "buffers_bytes": 0,
            "cached_bytes": 0,
            "swap_total_bytes": 0,
            "swap_free_bytes": 0,
        }

        if not self.is_supported():
            return metrics

        meminfo_path = Path("/proc/meminfo")
        if not meminfo_path.is_file():
            return metrics

        key_mapping = {
            "MemTotal": "total_bytes",
            "MemFree": "free_bytes",
            "MemAvailable": "available_bytes",
            "Buffers": "buffers_bytes",
            "Cached": "cached_bytes",
            "SwapTotal": "swap_total_bytes",
            "SwapFree": "swap_free_bytes",
        }

        try:
            for line in meminfo_path.read_text(encoding="utf-8").splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    raw_key = parts[0].strip()
                    if raw_key in key_mapping:
                        val_str = parts[1].strip().split()[0]
                        # Values in /proc/meminfo are presented in kB
                        metrics[key_mapping[raw_key]] = int(val_str) * 1024
        except (OSError, ValueError):
            pass

        return metrics

    def get_system_load_averages(self) -> Tuple[float, float, float]:
        """Read 1, 5, and 15-minute system load averages from /proc/loadavg.

        Returns:
            Tuple of (load_1m, load_5m, load_15m).
        """
        if not self.is_supported():
            return 0.0, 0.0, 0.0

        loadavg_path = Path("/proc/loadavg")
        if not loadavg_path.is_file():
            return 0.0, 0.0, 0.0

        try:
            content = loadavg_path.read_text(encoding="utf-8").strip()
            parts = content.split()
            if len(parts) >= 3:
                return float(parts[0]), float(parts[1]), float(parts[2])
        except (OSError, ValueError):
            pass

        return 0.0, 0.0, 0.0
