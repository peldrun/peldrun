# backend/omweb/resource_monitor/providers/macos.py
"""macOS (Darwin) platform provider for PELDRUN Resource Monitor.

Extracts Darwin-specific hardware and memory telemetry using vm_stat
and sysctl inspection with graceful cross-platform safety wrappers.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IS_DARWIN = platform.system().lower() == "darwin"


class MacOSProvider:
    """Provides native macOS hardware and virtual memory telemetry."""

    def __init__(self) -> None:
        self._is_darwin = _IS_DARWIN
        self._vm_stat_path = shutil.which("vm_stat") if self._is_darwin else None
        self._sysctl_path = shutil.which("sysctl") if self._is_darwin else None

    def is_supported(self) -> bool:
        """Verify whether macOS platform telemetry tools are available."""
        return self._is_darwin and (self._vm_stat_path is not None or self._sysctl_path is not None)

    def get_memory_statistics(self) -> Dict[str, Any]:
        """Parse vm_stat output into structured byte counters.

        Returns:
            Dictionary containing active, inactive, wired, free, and compressed memory bytes.
        """
        metrics: Dict[str, Any] = {
            "page_size_bytes": 4096,
            "free_bytes": 0,
            "active_bytes": 0,
            "inactive_bytes": 0,
            "wired_bytes": 0,
            "compressed_bytes": 0,
            "used_bytes": 0,
        }

        if not self.is_supported() or not self._vm_stat_path:
            return metrics

        try:
            result = subprocess.run(
                [self._vm_stat_path],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if result.returncode != 0 or not result.stdout:
                return metrics

            # Parse page size from header: "Mach Virtual Memory Statistics: (page size of 4096 bytes)"
            page_size = 4096
            page_size_match = re.search(r"page size of (\d+) bytes", result.stdout, re.IGNORECASE)
            if page_size_match:
                page_size = int(page_size_match.group(1))
            metrics["page_size_bytes"] = page_size

            # Parse page counts
            for line in result.stdout.splitlines():
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key = key.strip().lower()
                clean_val = val.strip().rstrip(".").replace(" ", "")
                if not clean_val.isdigit():
                    continue

                page_count = int(clean_val)
                byte_value = page_count * page_size

                if "pages free" in key:
                    metrics["free_bytes"] = byte_value
                elif "pages active" in key:
                    metrics["active_bytes"] = byte_value
                elif "pages inactive" in key:
                    metrics["inactive_bytes"] = byte_value
                elif "pages wired down" in key:
                    metrics["wired_bytes"] = byte_value
                elif "pages occupied by compressor" in key:
                    metrics["compressed_bytes"] = byte_value

            metrics["used_bytes"] = (
                metrics["active_bytes"]
                + metrics["inactive_bytes"]
                + metrics["wired_bytes"]
                + metrics["compressed_bytes"]
            )
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            logger.debug("Failed to extract macOS vm_stat metrics: %s", exc)

        return metrics

    def get_sysctl_hardware_info(self) -> Dict[str, Optional[int]]:
        """Query physical RAM and CPU core counts via sysctl.

        Returns:
            Dictionary containing total physical memory and core counts.
        """
        info: Dict[str, Optional[int]] = {
            "total_physical_bytes": None,
            "logical_cpu_count": None,
            "physical_cpu_count": None,
        }

        if not self.is_supported() or not self._sysctl_path:
            return info

        keys = {
            "hw.memsize": "total_physical_bytes",
            "hw.logicalcpu": "logical_cpu_count",
            "hw.physicalcpu": "physical_cpu_count",
        }

        for sysctl_key, target_field in keys.items():
            try:
                res = subprocess.run(
                    [self._sysctl_path, "-n", sysctl_key],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip().isdigit():
                    info[target_field] = int(res.stdout.strip())
            except (subprocess.SubprocessError, OSError, ValueError):
                continue

        return info
