# backend/omweb/resource_monitor/providers/windows.py
"""Windows-specific platform provider for PELDRUN Resource Monitor.

Utilizes native Win32 APIs via ctypes to probe memory status, determine if
the process resides within a Windows Job Object, and extract job-level resource limits.
"""

from __future__ import annotations

import ctypes
import logging
import platform
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system().lower() == "windows"

if _IS_WINDOWS:
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        """Win32 MEMORYSTATUSEX structure for host physical and virtual memory telemetry."""
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    class IO_COUNTERS(ctypes.Structure):
        """Win32 IO_COUNTERS structure representing process I/O operations."""
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        """Win32 basic limit information structure for Windows Job Objects."""
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        """Win32 extended limit information structure for Windows Job Objects."""
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    # Limit flags definitions from winnt.h
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JobObjectExtendedLimitInformation = 9


class WindowsProvider:
    """Provides low-level Windows host telemetry and Job Object bounds."""

    def __init__(self) -> None:
        self._is_windows = _IS_WINDOWS
        self._kernel32: Optional[Any] = None

        if self._is_windows:
            try:
                self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            except Exception as exc:
                logger.debug("Failed to load kernel32.dll on Windows: %s", exc)

    def is_supported(self) -> bool:
        """Check whether Windows native API querying is supported."""
        return self._is_windows and self._kernel32 is not None

    def get_global_memory_status(self) -> Optional[Dict[str, Any]]:
        """Query host memory telemetry using GlobalMemoryStatusEx.

        Returns:
            Dictionary containing detailed physical and virtual memory counters,
            or None if querying failed or is unsupported.
        """
        if not self.is_supported():
            return None

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)

        try:
            success = self._kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            if not success:
                return None

            return {
                "memory_load_percent": stat.dwMemoryLoad,
                "total_physical_bytes": stat.ullTotalPhys,
                "available_physical_bytes": stat.ullAvailPhys,
                "total_pagefile_bytes": stat.ullTotalPageFile,
                "available_pagefile_bytes": stat.ullAvailPageFile,
                "total_virtual_bytes": stat.ullTotalVirtual,
                "available_virtual_bytes": stat.ullAvailVirtual,
            }
        except Exception as exc:
            logger.debug("GlobalMemoryStatusEx invocation failed: %s", exc)
            return None

    def get_job_object_limits(self) -> Optional[Dict[str, Any]]:
        """Determine if current process runs in a Job Object and query its memory limits.

        Returns:
            Dictionary containing job memory constraints, or None if not assigned
            to a Job Object or if querying is unsupported.
        """
        if not self.is_supported():
            return None

        try:
            h_process = self._kernel32.GetCurrentProcess()
            is_in_job = wintypes.BOOL()

            res = self._kernel32.IsProcessInJob(h_process, None, ctypes.byref(is_in_job))
            if not res or not is_in_job.value:
                return {
                    "is_in_job": False,
                    "job_memory_limit_bytes": None,
                    "process_memory_limit_bytes": None,
                }

            extended_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            query_res = self._kernel32.QueryInformationJobObject(
                None,
                JobObjectExtendedLimitInformation,
                ctypes.byref(extended_info),
                ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
                None,
            )

            if not query_res:
                return {
                    "is_in_job": True,
                    "job_memory_limit_bytes": None,
                    "process_memory_limit_bytes": None,
                }

            flags = extended_info.BasicLimitInformation.LimitFlags
            job_limit = None
            proc_limit = None

            if flags & JOB_OBJECT_LIMIT_JOB_MEMORY:
                job_limit = int(extended_info.JobMemoryLimit)
            if flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY:
                proc_limit = int(extended_info.ProcessMemoryLimit)

            return {
                "is_in_job": True,
                "job_memory_limit_bytes": job_limit,
                "process_memory_limit_bytes": proc_limit,
                "peak_job_memory_bytes": int(extended_info.PeakJobMemoryLimit),
                "peak_process_memory_bytes": int(extended_info.PeakProcessMemoryLimit),
            }
        except Exception as exc:
            logger.debug("Failed querying Windows Job Object limits: %s", exc)
            return None
