# backend/omweb/resource_monitor/providers/docker.py
"""Docker container inspection and resource constraint provider.

Detects whether the runtime resides inside a Docker container, extracts the
unique container ID, and probes enforced cgroups memory limits and CPU quotas.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IS_LINUX = platform.system().lower() == "linux"


class DockerProvider:
    """Provides container detection and cgroup constraint telemetry for Docker."""

    def __init__(self) -> None:
        self._is_linux = _IS_LINUX
        self._is_container: Optional[bool] = None
        self._container_id: Optional[str] = None
        self._cgroup_v2_root = Path("/sys/fs/cgroup")
        self._cgroup_v1_memory = Path("/sys/fs/cgroup/memory")
        self._cgroup_v1_cpu = Path("/sys/fs/cgroup/cpu")

    def is_supported(self) -> bool:
        """Check whether execution is running inside a Docker or OCI container."""
        if not self._is_linux:
            return False

        if self._is_container is not None:
            return self._is_container

        # 1. Standard docker marker file
        if Path("/.dockerenv").exists():
            self._is_container = True
            return True

        # 2. Environment variable indicator
        if os.environ.get("container") == "docker":
            self._is_container = True
            return True

        # 3. Cgroup signature inspection
        for cg_file in ("/proc/self/cgroup", "/proc/1/cgroup"):
            p = Path(cg_file)
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if any(sig in content for sig in ("docker", "containerd", "kubepods")):
                        self._is_container = True
                        return True
                except (OSError, PermissionError):
                    continue

        self._is_container = False
        return False

    def get_container_id(self) -> Optional[str]:
        """Extract the container ID from kernel mountinfo, cgroups, or hostname.

        Returns:
            64-character or shortened container hex ID, or None if detection fails.
        """
        if not self.is_supported():
            return None

        if self._container_id is not None:
            return self._container_id

        # 1. Search in /proc/self/cgroup
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.is_file():
            try:
                content = cgroup_path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"[0-9a-fA-F]{64}", content)
                if match:
                    self._container_id = match.group(0)
                    return self._container_id
            except (OSError, PermissionError):
                pass

        # 2. Search in /proc/self/mountinfo
        mountinfo_path = Path("/proc/self/mountinfo")
        if mountinfo_path.is_file():
            try:
                content = mountinfo_path.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"docker/containers/([0-9a-fA-F]{64})", content)
                if match:
                    self._container_id = match.group(1)
                    return self._container_id
            except (OSError, PermissionError):
                pass

        # 3. Fallback: Default Docker hostname is usually the 12-char short ID
        hostname_path = Path("/etc/hostname")
        if hostname_path.is_file():
            try:
                name = hostname_path.read_text(encoding="utf-8", errors="ignore").strip()
                if re.fullmatch(r"[0-9a-fA-F]{12}", name):
                    self._container_id = name
                    return self._container_id
            except (OSError, PermissionError):
                pass

        return None

    def get_container_resource_limits(self) -> Dict[str, Any]:
        """Extract enforced container resource constraints and allocation bounds.

        Returns:
            Dictionary containing memory limit, soft reservation, CPU quota,
            effective allocated CPU cores, and OOM event occurrences.
        """
        telemetry: Dict[str, Any] = {
            "is_docker": False,
            "container_id": None,
            "memory_limit_bytes": None,
            "memory_soft_limit_bytes": None,
            "cpu_quota_us": None,
            "cpu_period_us": None,
            "effective_cpu_count": None,
            "oom_kill_events": None,
        }

        if not self.is_supported():
            return telemetry

        telemetry["is_docker"] = True
        telemetry["container_id"] = self.get_container_id()

        # 1. Attempt cgroups v2 unified reading
        if (self._cgroup_v2_root / "cgroup.controllers").is_file():
            self._read_cgroup_v2(telemetry)
        # 2. Fallback to cgroups v1 controllers
        elif self._cgroup_v1_memory.is_dir() or self._cgroup_v1_cpu.is_dir():
            self._read_cgroup_v1(telemetry)

        # Calculate effective CPU cores allocated to container
        telemetry["effective_cpu_count"] = self._calculate_effective_cpus(
            telemetry["cpu_quota_us"],
            telemetry["cpu_period_us"],
        )

        return telemetry

    def _read_cgroup_v2(self, out: Dict[str, Any]) -> None:
        """Parse constraints from cgroups v2 unified control files."""
        # Memory limit
        mem_max = self._cgroup_v2_root / "memory.max"
        if mem_max.is_file():
            try:
                val = mem_max.read_text(encoding="utf-8").strip()
                if val and val != "max":
                    out["memory_limit_bytes"] = int(val)
            except (OSError, ValueError):
                pass

        # Memory reservation / soft limit
        mem_low = self._cgroup_v2_root / "memory.low"
        if mem_low.is_file():
            try:
                val = mem_low.read_text(encoding="utf-8").strip()
                if val and val != "max":
                    out["memory_soft_limit_bytes"] = int(val)
            except (OSError, ValueError):
                pass

        # CPU quota and period
        cpu_max = self._cgroup_v2_root / "cpu.max"
        if cpu_max.is_file():
            try:
                parts = cpu_max.read_text(encoding="utf-8").strip().split()
                if len(parts) >= 2:
                    if parts[0] != "max":
                        out["cpu_quota_us"] = int(parts[0])
                    out["cpu_period_us"] = int(parts[1])
            except (OSError, ValueError):
                pass

        # OOM kill events
        mem_events = self._cgroup_v2_root / "memory.events"
        if mem_events.is_file():
            try:
                for line in mem_events.read_text(encoding="utf-8").splitlines():
                    if line.startswith("oom_kill"):
                        parts = line.split()
                        if len(parts) >= 2:
                            out["oom_kill_events"] = int(parts[1])
            except (OSError, ValueError):
                pass

    def _read_cgroup_v1(self, out: Dict[str, Any]) -> None:
        """Parse constraints from cgroups v1 individual controllers."""
        # Memory limit
        mem_limit_file = self._cgroup_v1_memory / "memory.limit_in_bytes"
        if mem_limit_file.is_file():
            try:
                val = int(mem_limit_file.read_text(encoding="utf-8").strip())
                # cgroups v1 sets values >= (1 << 62) when no limit is enforced
                if val < (1 << 62):
                    out["memory_limit_bytes"] = val
            except (OSError, ValueError):
                pass

        # Memory soft limit
        mem_soft_file = self._cgroup_v1_memory / "memory.soft_limit_in_bytes"
        if mem_soft_file.is_file():
            try:
                val = int(mem_soft_file.read_text(encoding="utf-8").strip())
                if val < (1 << 62):
                    out["memory_soft_limit_bytes"] = val
            except (OSError, ValueError):
                pass

        # CPU quota
        cpu_quota_file = self._cgroup_v1_cpu / "cpu.cfs_quota_us"
        if cpu_quota_file.is_file():
            try:
                quota = int(cpu_quota_file.read_text(encoding="utf-8").strip())
                if quota > 0:
                    out["cpu_quota_us"] = quota
            except (OSError, ValueError):
                pass

        # CPU period
        cpu_period_file = self._cgroup_v1_cpu / "cpu.cfs_period_us"
        if cpu_period_file.is_file():
            try:
                period = int(cpu_period_file.read_text(encoding="utf-8").strip())
                if period > 0:
                    out["cpu_period_us"] = period
            except (OSError, ValueError):
                pass

        # Memory fail count
        failcnt_file = self._cgroup_v1_memory / "memory.failcnt"
        if failcnt_file.is_file():
            try:
                out["oom_kill_events"] = int(failcnt_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass

    @staticmethod
    def _calculate_effective_cpus(
        cpu_quota_us: Optional[int],
        cpu_period_us: Optional[int],
    ) -> float:
        """Calculate effective assigned CPU cores based on quota and period."""
        if cpu_quota_us is not None and cpu_period_us is not None and cpu_period_us > 0:
            return round(cpu_quota_us / cpu_period_us, 2)

        return float(os.cpu_count() or 1.0)
