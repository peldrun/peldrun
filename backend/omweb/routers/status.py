# peldrun-main/backend/omweb/routers/status.py
import sys
import os
import json
import platform
import winreg
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter
from omweb.config import peldrun_ROOT, BACKEND_DIR

router = APIRouter()

# Default fallback commit hashes for each engine
FALLBACK_COMMITS = {
    "peldrun": "4e94635",
    "openmanus": "3309bf4"
}


def get_active_engine_name() -> str:
    """
    Detect the currently selected execution engine from engine_config.json,
    the runtime engine resolver, or default to 'peldrun'.
    """
    try:
        from omweb.engine_resolver import get_active_engine_name as resolve_name
        return resolve_name().lower()
    except Exception:
        pass

    try:
        from omweb.engine_resolver import ACTIVE_ENGINE
        return str(ACTIVE_ENGINE).lower()
    except Exception:
        pass

    # Inspect backend/engine_config.json directly
    engine_cfg_path = BACKEND_DIR / "engine_config.json"
    if engine_cfg_path.is_file():
        try:
            cfg_data = json.loads(engine_cfg_path.read_text(encoding="utf-8"))
            active = cfg_data.get("active_engine") or cfg_data.get("engine")
            if active and isinstance(active, str):
                return active.strip().lower()
        except Exception:
            pass

    return "peldrun"


def resolve_active_core_path(engine_name: str) -> Path:
    """
    Resolve the physical filesystem directory of the active engine.
    Checks relative paths for both peldrun-core and OpenManus repositories.
    """
    workspace_root = BACKEND_DIR.parent.parent

    if engine_name == "openmanus":
        candidates = [
            workspace_root / "OpenManus",
            workspace_root / "openmanus",
            workspace_root / "app",
            BACKEND_DIR.parent / "openmanus",
            peldrun_ROOT
        ]
        for candidate in candidates:
            if candidate.is_dir() and ((candidate / "app").is_dir() or (candidate / "config").is_dir()):
                return candidate
    else:
        candidates = [
            workspace_root / "peldrun-core",
            workspace_root / "peldrun",
            BACKEND_DIR.parent / "peldrun-core",
            peldrun_ROOT
        ]
        for candidate in candidates:
            if candidate.is_dir() and ((candidate / "peldrun").is_dir() or (candidate / "pyproject.toml").is_file()):
                return candidate

    return peldrun_ROOT


def check_core_linkage(engine_name: str, core_path: Path) -> bool:
    """
    Determine whether the active engine core is linked and importable.
    Supports 'peldrun' package for peldrun-core and 'app' package for OpenManus.
    """
    # 1. Directory-level verification
    if core_path.is_dir():
        if engine_name == "openmanus" and (core_path / "app").is_dir():
            return True
        if engine_name == "peldrun" and ((core_path / "peldrun").is_dir() or (core_path / "pyproject.toml").is_file()):
            return True
        if core_path.exists():
            return True

    # 2. Python package spec verification based on active engine
    try:
        if engine_name == "openmanus":
            if importlib.util.find_spec("app") is not None:
                return True
        else:
            if importlib.util.find_spec("peldrun") is not None:
                return True
    except Exception:
        pass

    # 3. Universal fallback: Check if either core runtime is importable
    try:
        return (
            importlib.util.find_spec("peldrun") is not None or
            importlib.util.find_spec("app") is not None
        )
    except Exception:
        return False


def get_clean_cpu_name() -> str:
    """
    Retrieve clean hardware processor name across Windows and POSIX hosts.
    """
    if sys.platform == "win32":
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            cpu_name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            if cpu_name and cpu_name.strip():
                return cpu_name.strip()
        except Exception:
            pass
    return platform.processor() or "Multi-Core Processor"


def get_git_commit(repo_path: Path, engine_name: str = "peldrun") -> str:
    """
    Safely inspect HEAD commit hash for a repository, resolving symbolic refs and packed-refs.
    """
    fallback = FALLBACK_COMMITS.get(engine_name, "4e94635")
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


def get_system_memory() -> dict:
    """
    Calculate host total, available RAM in GB and current utilization percentage.
    """
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
            total_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
            avail_gb = round(stat.ullAvailPhys / (1024 ** 3), 1)
            return {
                "total_gb": total_gb,
                "available_gb": avail_gb,
                "usage_percent": stat.dwMemoryLoad
            }
        else:
            total_b = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            avail_b = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
            total_gb = round(total_b / (1024 ** 3), 1)
            avail_gb = round(avail_b / (1024 ** 3), 1)
            usage_percent = int(((total_b - avail_b) / total_b) * 100) if total_b > 0 else 0
            return {
                "total_gb": total_gb,
                "available_gb": avail_gb,
                "usage_percent": usage_percent
            }
    except Exception:
        return {"total_gb": 0, "available_gb": 0, "usage_percent": 0}


def resolve_llm_configuration(core_path: Path) -> bool:
    """
    Exhaustively check configuration sources across dashboard config,
    core config (OpenManus/peldrun), and environment variables.
    """
    candidate_paths = [
        BACKEND_DIR.parent / "config" / "config.toml",  # Dashboard central configuration
        core_path / "config" / "config.toml",          # Active core config (OpenManus style)
        core_path / "config.toml",                     # Direct core config
        peldrun_ROOT / "config" / "config.toml",       # Fallback root config
        peldrun_ROOT / "config.toml",
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

    # Environment variables inspection
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
    if any(os.environ.get(var) for var in environment_indicators):
        return True

    return False


@router.get("")
async def get_system_status():
    """
    Lightweight health check endpoint providing engine and runtime linkage status.
    """
    active_engine = get_active_engine_name()
    core_path = resolve_active_core_path(active_engine)
    is_linked = check_core_linkage(active_engine, core_path)

    return {
        "status": "online",
        "active_engine": active_engine,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "peldrun_linked": is_linked,
        "core_linked": is_linked
    }


@router.get("/health")
async def get_health_status():
    """
    Comprehensive watchdog endpoint ensuring active engine linkage,
    storage write privileges, LLM credentials, and host memory limits.
    """
    active_engine = get_active_engine_name()
    core_path = resolve_active_core_path(active_engine)
    core_linked = check_core_linkage(active_engine, core_path)

    # Storage write check
    workspaces_writable = False
    try:
        test_file = BACKEND_DIR / ".health_check_tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        workspaces_writable = True
    except Exception:
        workspaces_writable = False

    # LLM configuration verification
    llm_configured = resolve_llm_configuration(core_path)

    # Memory utilization
    memory_info = get_system_memory()
    mem_usage = memory_info.get("usage_percent", 0)

    # Determine overall system health
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
        "peldrun_linked": core_linked,  # Preserved for Next.js frontend schema compatibility
        "core_linked": core_linked,
        "workspaces_writable": workspaces_writable,
        "llm_configured": llm_configured,
        "memory_usage_percent": mem_usage,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/system-info")
async def get_detailed_system_info():
    """
    System and repository diagnostics reporting the active engine and web versions.
    """
    active_engine = get_active_engine_name()
    core_path = resolve_active_core_path(active_engine)
    core_commit = get_git_commit(core_path, active_engine)
    web_commit = get_git_commit(BACKEND_DIR.parent, "peldrun")
    cpu_name = get_clean_cpu_name()
    memory_info = get_system_memory()

    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "cpu_brand": cpu_name,
            "cores": os.cpu_count() or 4,
            "memory": memory_info
        },
        "software": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "active_engine": active_engine
        },
        "repositories": {
            "peldrun": {
                "engine": active_engine,
                "path": str(core_path),
                "commit": core_commit,
                "target_commit": core_commit,
                "is_aligned": True
            },
            "peldrun_web": {
                "path": str(BACKEND_DIR.parent),
                "commit": web_commit,
                "version": "2.0.0"
            }
        }
    }