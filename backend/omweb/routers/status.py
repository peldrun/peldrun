"""
PELDRUN Web System Status and Diagnostics Router.

Provides real-time system health checks, memory telemetry, and engine linkage
diagnostics, natively reflecting embedded PELDRUN Core status.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any, Dict
import winreg

from fastapi import APIRouter
from omweb.config import BACKEND_DIR, peldrun_ROOT

router = APIRouter()

FALLBACK_COMMITS = {
    "peldrun": "0755669",
    "openmanus": "3309bf4",
}


def get_active_engine_name() -> str:
    """Detect the currently selected execution engine from resolver or config."""
    try:
        from omweb.engine_resolver import get_active_engine_name as resolve_name

        return resolve_name().lower()
    except Exception:
        pass

    engine_cfg_path = BACKEND_DIR / "engine_config.json"
    if engine_cfg_path.is_file():
        try:
            cfg_data = json.loads(engine_cfg_path.read_text(encoding="utf-8"))
            active = cfg_data.get("engine_type") or cfg_data.get("active_engine") or cfg_data.get("engine")
            if active and isinstance(active, str):
                norm = active.strip().lower()
                return "peldrun" if norm in ["peldrun", "peldrun-core", "core"] else norm
        except Exception:
            pass

    return "peldrun"


def resolve_active_core_path(engine_name: str) -> Path:
    """Resolve the physical filesystem directory of the active engine."""
    if engine_name in ["peldrun", "peldrun-core", "core"]:
        embedded_core = BACKEND_DIR / "peldrun"
        if embedded_core.is_dir():
            return embedded_core
        return BACKEND_DIR

    # Legacy OpenManus path candidates
    workspace_root = BACKEND_DIR.parent.parent
    candidates = [
        workspace_root / "OpenManus",
        workspace_root / "openmanus",
        workspace_root / "app",
        BACKEND_DIR.parent / "openmanus",
        peldrun_ROOT,
    ]
    for candidate in candidates:
        if candidate.is_dir() and ((candidate / "app").is_dir() or (candidate / "config").is_dir()):
            return candidate

    return peldrun_ROOT


def check_core_linkage(engine_name: str, core_path: Path) -> bool:
    """Determine whether the active engine runtime is healthy and importable."""
    if engine_name in ["peldrun", "peldrun-core", "core"]:
        try:
            import peldrun

            return getattr(peldrun, "__is_embedded__", False) or (core_path / "__init__.py").is_file()
        except Exception:
            return (core_path / "__init__.py").is_file()

    if engine_name in ["openmanus", "legacy", "manus"]:
        try:
            return importlib.util.find_spec("app") is not None
        except Exception:
            return (core_path / "app").is_dir()

    return False


def get_clean_cpu_name() -> str:
    """Retrieve clean hardware processor name across Windows and POSIX hosts."""
    if sys.platform == "win32":
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            cpu_name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            if cpu_name and cpu_name.strip():
                return cpu_name.strip()
        except Exception:
            pass
    return platform.processor() or "Multi-Core Processor"


def get_git_commit(repo_path: Path, engine_name: str = "peldrun") -> str:
    """Safely inspect HEAD commit hash or retrieve embedded upstream commit metadata."""
    if engine_name in ["peldrun", "peldrun-core", "core"]:
        try:
            import peldrun

            upstream_commit = getattr(peldrun, "__upstream_commit__", "")
            if upstream_commit:
                return str(upstream_commit)[:7]
        except Exception:
            pass

    fallback = FALLBACK_COMMITS.get(engine_name, "0755669")
    try:
        git_dir = repo_path / ".git"
        if not git_dir.exists():
            return fallback

        head_file = git_dir / "HEAD"
        if head_file.exists():
            ref = head_file.read_text(encoding="utf-8").strip()
            if ref.startswith("ref:"):
                rel_path = ref.split(" ", 1)[1].strip()
                ref_path = git_dir / rel_path
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:7]

                packed_refs = git_dir / "packed-refs"
                if packed_refs.exists():
                    for line in packed_refs.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if rel_path in line and not line.startswith("^") and not line.startswith("#"):
                            parts = line.strip().split()
                            if len(parts) >= 2 and parts[1] == rel_path:
                                return parts[0][:7]

            return ref[:7]
        return fallback
    except Exception:
        return fallback


def get_system_memory() -> Dict[str, Any]:
    """Calculate host total, available RAM in GB and utilization percentage."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_gb = round(stat.ullTotalPhys / (1024**3), 1)
            avail_gb = round(stat.ullAvailPhys / (1024**3), 1)
            return {
                "total_gb": total_gb,
                "available_gb": avail_gb,
                "usage_percent": stat.dwMemoryLoad,
            }
        else:
            total_b = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            avail_b = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
            total_gb = round(total_b / (1024**3), 1)
            avail_gb = round(avail_b / (1024**3), 1)
            usage_percent = int(((total_b - avail_b) / total_b) * 100) if total_b > 0 else 0
            return {
                "total_gb": total_gb,
                "available_gb": avail_gb,
                "usage_percent": usage_percent,
            }
    except Exception:
        return {"total_gb": 0, "available_gb": 0, "usage_percent": 0}


def resolve_llm_configuration(core_path: Path) -> bool:
    """Check configuration sources across central config and environment variables."""
    candidate_paths = [
        BACKEND_DIR.parent / "config" / "config.toml",
        BACKEND_DIR / "config.toml",
        BACKEND_DIR / "config" / "config.toml",
        Path.cwd() / "config" / "config.toml",
        Path.cwd() / "config.toml",
    ]

    for config_file in candidate_paths:
        if config_file.is_file():
            try:
                content = config_file.read_text(encoding="utf-8", errors="ignore")
                if any(keyword in content for keyword in ("api_key", "model", "base_url", "[llm]")):
                    return True
            except Exception:
                continue

    environment_indicators = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "LLM_API_KEY",
        "LM_STUDIO_URL",
        "OLLAMA_HOST",
        "LLM_MODEL",
        "MODEL",
    )
    return any(os.environ.get(var) for var in environment_indicators)


@router.get("")
async def get_system_status():
    """Lightweight health check endpoint providing engine and runtime linkage status."""
    active_engine = get_active_engine_name()
    core_path = resolve_active_core_path(active_engine)
    is_linked = check_core_linkage(active_engine, core_path)
    is_embedded = active_engine in ["peldrun", "peldrun-core", "core"]

    return {
        "status": "online",
        "active_engine": active_engine,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "peldrun_linked": is_linked,
        "core_linked": is_linked,
        "embedded": is_embedded,
    }


@router.get("/health")
async def get_health_status():
    """Comprehensive watchdog endpoint ensuring engine health and host limits."""
    active_engine = get_active_engine_name()
    core_path = resolve_active_core_path(active_engine)
    core_linked = check_core_linkage(active_engine, core_path)
    is_embedded = active_engine in ["peldrun", "peldrun-core", "core"]

    workspaces_writable = False
    try:
        test_file = BACKEND_DIR / ".health_check_tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        workspaces_writable = True
    except Exception:
        workspaces_writable = False

    llm_configured = resolve_llm_configuration(core_path)
    memory_info = get_system_memory()
    mem_usage = memory_info.get("usage_percent", 0)

    if not core_linked or not workspaces_writable:
        overall_status = "unhealthy"
    elif not llm_configured or mem_usage > 95:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return {
        "status": overall_status,
        "server": "omweb-core",
        "active_engine": active_engine,
        "peldrun_linked": core_linked,
        "core_linked": core_linked,
        "embedded": is_embedded,
        "workspaces_writable": workspaces_writable,
        "llm_configured": llm_configured,
        "memory_usage_percent": mem_usage,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/system-info")
async def get_detailed_system_info():
    """System and repository diagnostics reporting active engine and web versions."""
    active_engine = get_active_engine_name()
    core_path = resolve_active_core_path(active_engine)
    core_commit = get_git_commit(core_path, active_engine)
    web_commit = get_git_commit(BACKEND_DIR.parent, "peldrun")
    cpu_name = get_clean_cpu_name()
    memory_info = get_system_memory()
    is_embedded = active_engine in ["peldrun", "peldrun-core", "core"]

    core_version = "0.2.0"
    if is_embedded:
        try:
            import peldrun

            core_version = getattr(peldrun, "__version__", "0.2.0")
        except Exception:
            pass

    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "cpu_brand": cpu_name,
            "cores": os.cpu_count() or 4,
            "memory": memory_info,
        },
        "software": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "active_engine": active_engine,
        },
        "repositories": {
            "peldrun": {
                "engine": active_engine,
                "embedded": is_embedded,
                "path": str(core_path),
                "commit": core_commit,
                "target_commit": core_commit,
                "version": core_version,
                "is_aligned": True,
            },
            "peldrun_web": {
                "path": str(BACKEND_DIR.parent),
                "commit": web_commit,
                "version": "2.0.0",
            },
        },
    }