# backend/omweb/resource_monitor/capabilities.py
"""Capability probing and feature matrix for PELDRUN Resource Monitor.

This module determines what hardware metrics, operating system hooks,
and container constraints can be reliably gathered in the current runtime
environment without causing crashes or permission exceptions.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class ResourceMonitorCapabilities(BaseModel):
    """Matrix of environmental and telemetry capabilities detected on the host."""
    model_config = ConfigDict(from_attributes=True)

    # Core collector library status
    has_psutil: bool = Field(default=False, description="Whether psutil is installed and functional")

    # Operating system and platform flags
    is_windows: bool = Field(default=False, description="Running on Windows OS")
    is_linux: bool = Field(default=False, description="Running on Linux OS")
    is_darwin: bool = Field(default=False, description="Running on macOS")
    is_docker: bool = Field(default=False, description="Running inside a Docker container")
    is_wsl: bool = Field(default=False, description="Running inside Windows Subsystem for Linux")

    # Cgroup capabilities (Linux & Containerization)
    has_cgroups_v1: bool = Field(default=False, description="Linux cgroups v1 hierarchy available")
    has_cgroups_v2: bool = Field(default=False, description="Linux cgroups v2 unified hierarchy available")
    can_read_cgroup_memory: bool = Field(default=False, description="Container memory limit is inspectable")
    can_read_cgroup_cpu: bool = Field(default=False, description="Container CPU quota is inspectable")

    # Process tree and resource introspection
    can_track_subprocesses: bool = Field(default=False, description="Subprocess tree traversal is supported")
    can_measure_cpu_times: bool = Field(default=False, description="Process user/kernel CPU times available")
    can_measure_memory_rss: bool = Field(default=False, description="Resident Set Size (RSS) memory query supported")
    can_measure_memory_vms: bool = Field(default=False, description="Virtual Memory Size (VMS) query supported")
    can_measure_io_counters: bool = Field(default=False, description="Per-process or host Disk I/O counters supported")
    can_measure_net_counters: bool = Field(default=False, description="Host Network I/O counters supported")

    # Administrative and signal actions
    can_terminate_processes: bool = Field(default=False, description="Process termination permissions available")


class CapabilitiesProber:
    """Probes runtime system features and caches detection outcomes."""

    def __init__(self) -> None:
        self._cached_capabilities: Optional[ResourceMonitorCapabilities] = None

    def probe(self, force_refresh: bool = False) -> ResourceMonitorCapabilities:
        """Probe all host and container capabilities safely.

        Args:
            force_refresh: When True, bypasses memoized cache and probes afresh.

        Returns:
            ResourceMonitorCapabilities populated with verified flags.
        """
        if self._cached_capabilities is not None and not force_refresh:
            return self._cached_capabilities

        system_name = platform.system().lower()
        is_windows = system_name == "windows"
        is_linux = system_name == "linux"
        is_darwin = system_name == "darwin"

        is_docker = self._probe_docker(is_linux)
        is_wsl = self._probe_wsl(is_linux)

        cgroups_v1, cgroups_v2, cg_mem, cg_cpu = self._probe_cgroups(is_linux)
        process_caps = self._probe_process_caps()

        capabilities = ResourceMonitorCapabilities(
            has_psutil=_PSUTIL_AVAILABLE,
            is_windows=is_windows,
            is_linux=is_linux,
            is_darwin=is_darwin,
            is_docker=is_docker,
            is_wsl=is_wsl,
            has_cgroups_v1=cgroups_v1,
            has_cgroups_v2=cgroups_v2,
            can_read_cgroup_memory=cg_mem,
            can_read_cgroup_cpu=cg_cpu,
            can_track_subprocesses=process_caps["track_subprocesses"],
            can_measure_cpu_times=process_caps["cpu_times"],
            can_measure_memory_rss=process_caps["memory_rss"],
            can_measure_memory_vms=process_caps["memory_vms"],
            can_measure_io_counters=process_caps["io_counters"],
            can_measure_net_counters=process_caps["net_counters"],
            can_terminate_processes=process_caps["terminate"],
        )

        self._cached_capabilities = capabilities
        return capabilities

    @staticmethod
    def _probe_docker(is_linux: bool) -> bool:
        """Detect whether process is executed inside Docker container."""
        if not is_linux:
            return False

        # Indicator 1: Standard .dockerenv marker file
        if Path("/.dockerenv").exists():
            return True

        # Indicator 2: Explicit container environment variables
        if os.environ.get("container") == "docker":
            return True

        # Indicator 3: Cgroup control file inspection
        cgroup_paths = ["/proc/self/cgroup", "/proc/1/cgroup"]
        for cg_path in cgroup_paths:
            p = Path(cg_path)
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if any(signature in content for signature in ("docker", "containerd", "kubepods")):
                        return True
                except (OSError, PermissionError):
                    continue

        return False

    @staticmethod
    def _probe_wsl(is_linux: bool) -> bool:
        """Detect whether execution is running inside Windows Subsystem for Linux."""
        if not is_linux:
            return False

        # Indicator 1: WSL distinct environment markers
        if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
            return True

        # Indicator 2: Kernel version string inspection
        version_path = Path("/proc/version")
        if version_path.is_file():
            try:
                version_text = version_path.read_text(encoding="utf-8", errors="ignore").lower()
                if "microsoft" in version_text or "wsl" in version_text:
                    return True
            except (OSError, PermissionError):
                pass

        return False

    @staticmethod
    def _probe_cgroups(is_linux: bool) -> tuple[bool, bool, bool, bool]:
        """Probe cgroups version availability and quota readability.

        Returns:
            Tuple of (has_v1, has_v2, can_read_memory, can_read_cpu)
        """
        if not is_linux:
            return False, False, False, False

        has_v1 = False
        has_v2 = False
        can_mem = False
        can_cpu = False

        # Check cgroups v2 unified mount
        cg2_controllers = Path("/sys/fs/cgroup/cgroup.controllers")
        cg2_memory_max = Path("/sys/fs/cgroup/memory.max")
        if cg2_controllers.is_file() or cg2_memory_max.is_file():
            has_v2 = True
            if cg2_memory_max.is_file():
                try:
                    cg2_memory_max.read_text(encoding="utf-8", errors="ignore")
                    can_mem = True
                except (OSError, PermissionError):
                    pass

            cg2_cpu_max = Path("/sys/fs/cgroup/cpu.max")
            if cg2_cpu_max.is_file():
                try:
                    cg2_cpu_max.read_text(encoding="utf-8", errors="ignore")
                    can_cpu = True
                except (OSError, PermissionError):
                    pass

        # Check cgroups v1 hierarchy
        cg1_mem_limit = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if cg1_mem_limit.is_file():
            has_v1 = True
            try:
                cg1_mem_limit.read_text(encoding="utf-8", errors="ignore")
                can_mem = True
            except (OSError, PermissionError):
                pass

        cg1_cpu_quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        if cg1_cpu_quota.is_file():
            has_v1 = True
            try:
                cg1_cpu_quota.read_text(encoding="utf-8", errors="ignore")
                can_cpu = True
            except (OSError, PermissionError):
                pass

        return has_v1, has_v2, can_mem, can_cpu

    @staticmethod
    def _probe_process_caps() -> Dict[str, bool]:
        """Probe psutil process-level inspection capabilities safely."""
        caps = {
            "track_subprocesses": False,
            "cpu_times": False,
            "memory_rss": False,
            "memory_vms": False,
            "io_counters": False,
            "net_counters": False,
            "terminate": False,
        }

        if not _PSUTIL_AVAILABLE or psutil is None:
            return caps

        try:
            current_proc = psutil.Process(os.getpid())

            # Test subprocess tree querying
            try:
                current_proc.children(recursive=True)
                caps["track_subprocesses"] = True
            except (AttributeError, Exception):
                pass

            # Test CPU times
            try:
                cpu_t = current_proc.cpu_times()
                if hasattr(cpu_t, "user") and hasattr(cpu_t, "system"):
                    caps["cpu_times"] = True
            except (AttributeError, Exception):
                pass

            # Test memory RSS and VMS
            try:
                mem_info = current_proc.memory_info()
                if hasattr(mem_info, "rss"):
                    caps["memory_rss"] = True
                if hasattr(mem_info, "vms"):
                    caps["memory_vms"] = True
            except (AttributeError, Exception):
                pass

            # Test I/O counters (Not implemented on all platforms such as macOS)
            try:
                current_proc.io_counters()
                caps["io_counters"] = True
            except (AttributeError, NotImplementedError, Exception):
                pass

            # Test Host Network counters
            try:
                psutil.net_io_counters()
                caps["net_counters"] = True
            except (AttributeError, NotImplementedError, Exception):
                pass

            # Process termination capability
            caps["terminate"] = True

        except Exception:
            # Entire process probing failed safely
            pass

        return caps


# Global instance for shared use
_DEFAULT_PROBER = CapabilitiesProber()


def probe_capabilities(force_refresh: bool = False) -> ResourceMonitorCapabilities:
    """Convenience function returning the global system capabilities."""
    return _DEFAULT_PROBER.probe(force_refresh=force_refresh)
