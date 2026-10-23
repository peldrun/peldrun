# backend/omweb/resource_monitor/providers/wsl.py
"""Windows Subsystem for Linux (WSL) platform provider for PELDRUN Resource Monitor.

Detects WSL runtime presence, identifies WSL1 vs WSL2 architecture, inspects
virtualized VM memory boundaries, and identifies cross-filesystem I/O bottlenecks.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IS_LINUX = platform.system().lower() == "linux"


class WSLProvider:
    """Provides WSL virtualized environment detection and memory boundary telemetry."""

    def __init__(self) -> None:
        self._is_linux = _IS_LINUX
        self._is_wsl: Optional[bool] = None
        self._wsl_version: Optional[int] = None
        self._distro_name: Optional[str] = None

    def is_supported(self) -> bool:
        """Check whether the runtime is hosted inside Windows Subsystem for Linux."""
        if not self._is_linux:
            return False

        if self._is_wsl is not None:
            return self._is_wsl

        # 1. Environment variable indicators
        if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
            self._is_wsl = True
            self._distro_name = os.environ.get("WSL_DISTRO_NAME")
            return True

        # 2. Kernel release and proc version inspection
        proc_ver = Path("/proc/version")
        if proc_ver.is_file():
            try:
                ver_text = proc_ver.read_text(encoding="utf-8", errors="ignore").lower()
                if "microsoft" in ver_text or "wsl" in ver_text:
                    self._is_wsl = True
                    return True
            except (OSError, PermissionError):
                pass

        self._is_wsl = False
        return False

    def get_wsl_version(self) -> Optional[int]:
        """Determine whether running on WSL1 or WSL2 architecture.

        Returns:
            1 for WSL1, 2 for WSL2, or None if not executing within WSL.
        """
        if not self.is_supported():
            return None

        if self._wsl_version is not None:
            return self._wsl_version

        # WSL2 includes dedicated hypervisor kernel signatures in /proc/version
        proc_ver = Path("/proc/version")
        if proc_ver.is_file():
            try:
                ver_text = proc_ver.read_text(encoding="utf-8", errors="ignore").lower()
                if "wsl2" in ver_text or "microsoft-standard" in ver_text:
                    self._wsl_version = 2
                    return 2
            except (OSError, PermissionError):
                pass

        # Check for WSL2 interop socket indicator
        if "WSL_INTEROP" in os.environ:
            self._wsl_version = 2
            return 2

        # Fallback to WSL1 translation layer
        self._wsl_version = 1
        return 1

    def is_cross_filesystem_path(self, target_path: Optional[str] = None) -> bool:
        """Determine if target path resides on a mounted Windows drive (e.g. /mnt/c).

        Accessing /mnt/* in WSL2 traverses 9P/drvfs, introducing substantial I/O latency.
        """
        if not self.is_supported():
            return False

        path_str = os.path.abspath(target_path or os.getcwd())
        return path_str.startswith("/mnt/")

    def get_wsl_telemetry(self) -> Dict[str, Any]:
        """Extract virtual machine memory boundaries and filesystem placement in WSL.

        Returns:
            Dictionary containing WSL architecture version, distribution name,
            allocated VM memory, available memory, swap allocation, and I/O locality.
        """
        telemetry: Dict[str, Any] = {
            "is_wsl": False,
            "wsl_version": None,
            "distro_name": None,
            "vm_memory_total_bytes": None,
            "vm_memory_available_bytes": None,
            "vm_swap_total_bytes": None,
            "is_cross_filesystem_workspace": False,
        }

        if not self.is_supported():
            return telemetry

        telemetry["is_wsl"] = True
        telemetry["wsl_version"] = self.get_wsl_version()
        telemetry["distro_name"] = self._distro_name or os.environ.get("WSL_DISTRO_NAME")
        telemetry["is_cross_filesystem_workspace"] = self.is_cross_filesystem_path()

        # Parse memory boundaries assigned to the WSL VM
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            try:
                for line in meminfo.read_text(encoding="utf-8").splitlines():
                    if line.startswith("MemTotal:"):
                        telemetry["vm_memory_total_bytes"] = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        telemetry["vm_memory_available_bytes"] = int(line.split()[1]) * 1024
                    elif line.startswith("SwapTotal:"):
                        telemetry["vm_swap_total_bytes"] = int(line.split()[1]) * 1024
            except (OSError, ValueError):
                pass

        return telemetry
