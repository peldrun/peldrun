# backend/omweb/resource_monitor/providers/__init__.py
"""Platform and environment-specific telemetry providers for PELDRUN Resource Monitor.

Exports native OS, container, and virtualization hooks for Windows, Linux,
macOS, Docker, and Windows Subsystem for Linux (WSL).
"""

from __future__ import annotations

from .docker import DockerProvider
from .linux import LinuxProvider
from .macos import MacOSProvider
from .windows import WindowsProvider
from .wsl import WSLProvider

__all__ = [
    "DockerProvider",
    "LinuxProvider",
    "MacOSProvider",
    "WindowsProvider",
    "WSLProvider",
]
