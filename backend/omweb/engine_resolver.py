"""
PELDRUN Universal Engine Resolver.

Manages execution engine selection, lifecycle discovery, and runtime resolution.
Treats PELDRUN Core as a first-class native embedded runtime package and
OpenManus as an optional secondary external adapter.
Maintains full backward-compatible interfaces for legacy setup and router callers.
"""

from __future__ import annotations

from enum import Enum
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Set, Union

CONFIG_FILE = Path(__file__).resolve().parent.parent / "engine_config.json"
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class EngineType(str, Enum):
    """Supported agent execution engine architectures."""

    CORE = "peldrun"
    LEGACY = "openmanus"


ACTIVE_ENGINE = EngineType.CORE.value


def is_valid_core_dir(target_path: Optional[Path]) -> bool:
    """Validate if target path represents a valid PELDRUN Core package directory."""
    if not target_path or not target_path.exists() or not target_path.is_dir():
        return False
    return (target_path / "__init__.py").is_file() or (target_path / "peldrun" / "__init__.py").is_file()


def is_valid_legacy_dir(target_path: Optional[Path]) -> bool:
    """Validate if directory contains legacy OpenManus source files."""
    if not target_path or not target_path.exists() or not target_path.is_dir():
        return False
    has_agent = (target_path / "app" / "agent" / "peldrun.py").is_file() or (
        target_path / "app" / "agent" / "manus.py"
    ).is_file()
    has_config = (target_path / "config" / "config.toml").is_file() or (
        target_path / "config" / "config.example.toml"
    ).is_file()
    return has_agent and has_config


def is_valid_peldrun_dir(target_path: Optional[Path]) -> bool:
    """Backward-compatible validator accepting embedded core or legacy directory layout."""
    return is_valid_core_dir(target_path) or is_valid_legacy_dir(target_path)


def is_core_engine_available() -> bool:
    """Check if the native embedded PELDRUN Core runtime package is available."""
    try:
        import peldrun

        return getattr(peldrun, "__is_embedded__", False) or (
            importlib.util.find_spec("peldrun") is not None
        )
    except Exception:
        try:
            return importlib.util.find_spec("peldrun") is not None
        except Exception:
            return False


def is_legacy_engine_available() -> bool:
    """Check if legacy OpenManus engine is available locally without global pollution."""
    try:
        if importlib.util.find_spec("app.agent.peldrun") or importlib.util.find_spec("app.agent.manus"):
            return True
    except Exception:
        pass

    candidates = [
        PROJECT_ROOT / "engine" / "peldrun",
        PROJECT_ROOT.parent / "peldrun",
        Path.home() / "peldrun",
    ]
    return any(is_valid_legacy_dir(c.resolve()) for c in candidates)


def get_persisted_engine_config() -> Dict[str, Any]:
    """Retrieve engine configuration payload from persistent storage."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_persisted_engine_path() -> Optional[Path]:
    """Retrieve persisted custom engine path, defaulting safely to embedded core package."""
    config = get_persisted_engine_config()
    custom_path = config.get("peldrun_path")
    if custom_path:
        p = Path(custom_path).resolve()
        if is_valid_peldrun_dir(p):
            return p

    embedded_core = BACKEND_DIR / "peldrun"
    if embedded_core.is_dir():
        return embedded_core
    return None


def save_engine_path(engine_path: Path, engine_type: Optional[str] = None) -> None:
    """Persist active engine path for backward-compatibility callers."""
    config = get_persisted_engine_config()
    if engine_type:
        norm_type = str(engine_type).strip().lower()
        config["engine_type"] = (
            EngineType.CORE.value if norm_type in ["peldrun", "peldrun-core", "core"] else norm_type
        )

    # Sanitize obsolete external path
    config.pop("peldrun_path", None)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_active_engine_type() -> EngineType:
    """Determine the active engine type prioritizing explicit user selection."""
    config = get_persisted_engine_config()
    saved_type = str(config.get("engine_type", "")).lower().strip()

    if saved_type in [EngineType.LEGACY.value, "legacy", "manus"]:
        return EngineType.LEGACY

    # Default natively to embedded PELDRUN Core
    return EngineType.CORE


def get_active_engine_name() -> str:
    """Return the normalized active engine identifier string."""
    return get_active_engine_type().value


def set_active_engine_type(engine_type: Union[EngineType, str]) -> None:
    """Persist active engine selection while sanitizing obsolete path attributes."""
    resolved = (
        engine_type.value if isinstance(engine_type, EngineType) else str(engine_type).strip().lower()
    )
    if resolved in ["peldrun-core", "core"]:
        resolved = EngineType.CORE.value

    config = get_persisted_engine_config()
    config["engine_type"] = resolved
    config.pop("peldrun_path", None)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def detect_potential_engine_paths() -> List[Dict[str, Any]]:
    """Probe environment and filesystem for available PELDRUN Core and Legacy installations.

    Guarantees backward compatibility for setup wizards and discovery routers.
    """
    results: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # 1. Primary: Native embedded PELDRUN Core
    embedded_core = (BACKEND_DIR / "peldrun").resolve()
    if embedded_core.is_dir() and is_valid_core_dir(embedded_core):
        path_str = str(embedded_core)
        seen.add(path_str)
        results.append({
            "path": path_str,
            "is_valid": True,
            "is_embedded": True,
            "engine_type": EngineType.CORE.value,
        })

    # 2. Legacy OpenManus engine candidates
    legacy_candidates = [
        (PROJECT_ROOT / "engine" / "peldrun").resolve(),
        (PROJECT_ROOT.parent / "OpenManus").resolve(),
        (PROJECT_ROOT.parent / "openmanus").resolve(),
        (PROJECT_ROOT.parent / "peldrun").resolve(),
        (Path.home() / "peldrun").resolve(),
    ]

    for cand in legacy_candidates:
        path_str = str(cand)
        if path_str in seen:
            continue
        seen.add(path_str)

        is_legacy = is_valid_legacy_dir(cand)
        if is_legacy:
            results.append({
                "path": path_str,
                "is_valid": True,
                "is_embedded": (
                    cand.is_relative_to(PROJECT_ROOT)
                    if hasattr(cand, "is_relative_to")
                    else False
                ),
                "engine_type": EngineType.LEGACY.value,
            })

    return results


def resolve_active_engine_path() -> Path:
    """Resolve active engine filesystem path without external core probing."""
    active_type = get_active_engine_type()

    if active_type == EngineType.CORE:
        embedded_path = BACKEND_DIR / "peldrun"
        if embedded_path.is_dir():
            return embedded_path
        return BACKEND_DIR

    # Legacy OpenManus engine resolution
    config = get_persisted_engine_config()
    custom_openmanus = config.get("openmanus_path")
    if custom_openmanus:
        p = Path(custom_openmanus).resolve()
        if is_valid_legacy_dir(p):
            return p

    legacy_candidates = [
        (PROJECT_ROOT / "engine" / "peldrun").resolve(),
        (PROJECT_ROOT.parent / "OpenManus").resolve(),
    ]
    for cand in legacy_candidates:
        if is_valid_legacy_dir(cand):
            return cand

    return (PROJECT_ROOT / "engine" / "peldrun").resolve()


def inject_engine_to_syspath() -> Path:
    """Safely inject path for legacy engines only; embedded core requires no injection."""
    active_type = get_active_engine_type()
    engine_path = resolve_active_engine_path()

    if active_type == EngineType.LEGACY:
        engine_str = str(engine_path)
        if is_valid_legacy_dir(engine_path) and engine_str not in sys.path:
            sys.path.insert(0, engine_str)

    return engine_path


def get_engine_status() -> Dict[str, Any]:
    """Provide runtime engine health and native embedded status diagnostics."""
    active_type = get_active_engine_type()
    core_ver = "unavailable"
    core_available = is_core_engine_available()

    if core_available:
        try:
            import peldrun

            core_ver = getattr(peldrun, "__version__", "0.2.0")
        except Exception:
            core_ver = "installed"

    return {
        "active_engine": active_type.value,
        "core_available": core_available,
        "core_embedded": True,
        "core_version": core_ver,
        "legacy_available": is_legacy_engine_available(),
        "engine_path": str(resolve_active_engine_path()),
    }


__all__ = [
    "PROJECT_ROOT",
    "BACKEND_DIR",
    "CONFIG_FILE",
    "EngineType",
    "ACTIVE_ENGINE",
    "is_core_engine_available",
    "is_legacy_engine_available",
    "is_valid_core_dir",
    "is_valid_legacy_dir",
    "is_valid_peldrun_dir",
    "get_persisted_engine_config",
    "get_persisted_engine_path",
    "save_engine_path",
    "get_active_engine_type",
    "get_active_engine_name",
    "set_active_engine_type",
    "detect_potential_engine_paths",
    "resolve_active_engine_path",
    "inject_engine_to_syspath",
    "get_engine_status",
]