# backend/omweb/resource_monitor/environment.py
"""Environment discovery and hardware profiling for PELDRUN Resource Monitor.

Inspects host hardware specifications, container memory/CPU limits (cgroups),
operating system release info, and runtime versions (Python/Node.js) to generate
an authoritative EnvironmentSnapshotModel for session telemetry.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .capabilities import ResourceMonitorCapabilities, probe_capabilities
from .models import EnvironmentSnapshotModel

# Graceful optional import of psutil
try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


class EnvironmentInspector:
    """Discovers host hardware properties, container bounds, and runtime contexts."""

    def __init__(self, capabilities: Optional[ResourceMonitorCapabilities] = None) -> None:
        self._capabilities = capabilities or probe_capabilities()

    def inspect(self, session_id: str) -> EnvironmentSnapshotModel:
        """Execute a full hardware and environment inspection.

        Args:
            session_id: Unique identifier of the current monitor session.

        Returns:
            EnvironmentSnapshotModel populated with verified hardware facts.
        """
        logical_cpus, physical_cpus = self._resolve_cpu_counts()
        total_ram = self._resolve_total_physical_ram()
        cgroup_mem, cgroup_cpu = self._resolve_cgroup_limits()

        caps_dict = self._capabilities.model_dump()

        return EnvironmentSnapshotModel(
            snapshot_id=str(uuid.uuid4()),
            session_id=session_id,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            os_name=platform.system(),
            os_release=platform.release(),
            architecture=platform.machine(),
            is_docker=self._capabilities.is_docker,
            is_wsl=self._capabilities.is_wsl,
            cpu_count_logical=logical_cpus,
            cpu_count_physical=physical_cpus,
            total_physical_ram_bytes=total_ram,
            cgroup_memory_limit_bytes=cgroup_mem,
            cgroup_cpu_quota_us=cgroup_cpu,
            capabilities_json=json.dumps(caps_dict),
        )

    def probe_node_version(self) -> Optional[str]:
        """Safely probe the system Node.js version if installed."""
        node_bin = shutil.which("node")
        if not node_bin:
            return None

        try:
            result = subprocess.run(
                [node_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass

        return None

    def _resolve_cpu_counts(self) -> tuple[int, Optional[int]]:
        """Resolve physical and logical CPU core counts with fallbacks."""
        logical: Optional[int] = None
        physical: Optional[int] = None

        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                logical = psutil.cpu_count(logical=True)
                physical = psutil.cpu_count(logical=False)
            except Exception:
                pass

        if logical is None:
            logical = os.cpu_count() or 1

        return logical, physical

    def _resolve_total_physical_ram(self) -> int:
        """Resolve total physical system RAM in bytes safely."""
        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                return int(psutil.virtual_memory().total)
            except Exception:
                pass

        # Linux fallback via /proc/meminfo
        if self._capabilities.is_linux:
            meminfo_path = Path("/proc/meminfo")
            if meminfo_path.is_file():
                try:
                    for line in meminfo_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("MemTotal:"):
                            parts = line.split()
                            if len(parts) >= 2:
                                return int(parts[1]) * 1024  # kB to bytes
                except (OSError, ValueError):
                    pass

        # Minimal fallback if detection is completely unavailable
        return 0

    def _resolve_cgroup_limits(self) -> tuple[Optional[int], Optional[int]]:
        """Read container cgroup memory limits and CPU quotas if applied.

        Returns:
            Tuple of (cgroup_memory_limit_bytes, cgroup_cpu_quota_us)
        """
        if not self._capabilities.is_linux:
            return None, None

        mem_limit: Optional[int] = None
        cpu_quota: Optional[int] = None

        # 1. Inspect cgroups v2 unified hierarchy
        if self._capabilities.has_cgroups_v2:
            mem2_path = Path("/sys/fs/cgroup/memory.max")
            if mem2_path.is_file():
                try:
                    content = mem2_path.read_text(encoding="utf-8").strip()
                    if content and content != "max":
                        mem_limit = int(content)
                except (OSError, ValueError):
                    pass

            cpu2_path = Path("/sys/fs/cgroup/cpu.max")
            if cpu2_path.is_file():
                try:
                    content = cpu2_path.read_text(encoding="utf-8").strip()
                    parts = content.split()
                    if parts and parts[0] != "max":
                        cpu_quota = int(parts[0])
                except (OSError, ValueError):
                    pass

        # 2. Inspect cgroups v1 hierarchy if v2 did not supply limits
        if self._capabilities.has_cgroups_v1:
            if mem_limit is None:
                mem1_path = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
                if mem1_path.is_file():
                    try:
                        val = int(mem1_path.read_text(encoding="utf-8").strip())
                        # cgroups v1 sets a huge sentinel number (~9e18) when unlimited
                        if val < (1 << 62):
                            mem_limit = val
                    except (OSError, ValueError):
                        pass

            if cpu_quota is None:
                cpu1_quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
                if cpu1_quota_path.is_file():
                    try:
                        val = int(cpu1_quota_path.read_text(encoding="utf-8").strip())
                        if val > 0:
                            cpu_quota = val
                    except (OSError, ValueError):
                        pass

        return mem_limit, cpu_quota


def capture_environment_snapshot(session_id: str) -> EnvironmentSnapshotModel:
    """Convenience function to capture environment state for a session."""
    inspector = EnvironmentInspector()
    return inspector.inspect(session_id)
