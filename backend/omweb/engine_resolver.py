"""
backend/omweb/engine_resolver.py

PELDRUN Universal Engine Resolver (Single Execution Authority).

Manages execution engine selection, explicit validation, and runtime resolution.
Treats PELDRUN Core as the native embedded runtime package in `backend/peldrun/`.
Hardened under PR 3: Completely decoupled from engine_config.json, rejecting all legacy identifiers.
"""

from __future__ import annotations

from enum import Enum
import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class EngineType(str, Enum):
    """Supported agent execution engine architectures."""

    CORE = "peldrun"


ACTIVE_ENGINE = EngineType.CORE.value
LEGACY_ENGINE_IDS = {"openmanus", "legacy", "manus"}
VALID_CORE_ALIASES = {"peldrun", "peldrun-core", "core"}


class EngineConfigurationError(RuntimeError):
    """Raised when engine configuration identifier is empty or invalid."""

    pass


class UnknownEngineError(ValueError):
    """Raised when an unrecognized execution engine identifier is provided."""

    pass


def is_valid_core_dir(target_path: Optional[Path]) -> bool:
    """Validate if target path represents a valid PELDRUN Core package directory."""
    if not target_path or not target_path.exists() or not target_path.is_dir():
        return False
    return (
        (target_path / "__init__.py").is_file()
        or (target_path / "peldrun" / "__init__.py").is_file()
    )


def is_valid_legacy_dir(target_path: Optional[Path]) -> bool:
    """Legacy engines are no longer supported. Always returns False."""
    return False


def is_valid_peldrun_dir(target_path: Optional[Path]) -> bool:
    """Validate if target path represents a valid PELDRUN Core package directory."""
    return is_valid_core_dir(target_path)


def is_core_engine_available() -> bool:
    """Check if the native embedded PELDRUN Core runtime package is available."""
    try:
        import peldrun

        return bool(getattr(peldrun, "__is_embedded__", False)) or (
            importlib.util.find_spec("peldrun") is not None
        )
    except Exception:
        try:
            return importlib.util.find_spec("peldrun") is not None
        except Exception:
            return False


def is_legacy_engine_available() -> bool:
    """Legacy OpenManus runtime has been removed completely. Always returns False."""
    return False


def get_persisted_engine_config() -> Dict[str, Any]:
    """Return in-memory static engine configuration without filesystem reads."""
    return {"engine_type": EngineType.CORE.value}


def get_persisted_engine_path() -> Optional[Path]:
    """Retrieve engine path defaulting safely to embedded core package."""
    return resolve_active_engine_path()


def save_engine_path(engine_path: Path, engine_type: Optional[str] = None) -> None:
    """No-op persistence adapter kept for backward compatibility."""
    if engine_type:
        validate_engine_id(str(engine_type))


def get_active_engine_type(allow_default: bool = True) -> EngineType:
    """Return the authoritative single engine type (PELDRUN Core)."""
    return EngineType.CORE


def get_active_engine_name() -> str:
    """Return the normalized active engine identifier string."""
    return EngineType.CORE.value


def validate_engine_id(engine_id: str) -> str:
    """
    Validate that an engine identifier or alias is registered and supported.
    Explicitly rejects OpenManus identifiers.
    """
    if not engine_id or not str(engine_id).strip():
        raise EngineConfigurationError("Engine identifier cannot be empty.")

    normalized = str(engine_id).strip().lower()

    if normalized in LEGACY_ENGINE_IDS:
        raise ValueError(
            "OpenManus is no longer supported. PELDRUN Core is the only execution engine."
        )

    if normalized in VALID_CORE_ALIASES:
        return EngineType.CORE.value

    raise UnknownEngineError(
        f"Execution engine '{engine_id}' is not recognized. Supported engine: {EngineType.CORE.value}"
    )


def set_active_engine_type(engine_type: Union[EngineType, str]) -> None:
    """Validate engine identifier before acknowledging active authority."""
    raw_val = engine_type.value if isinstance(engine_type, EngineType) else str(engine_type)
    validate_engine_id(raw_val)


def detect_potential_engine_paths() -> List[Dict[str, Any]]:
    """Return embedded PELDRUN Core as the sole engine candidate."""
    embedded_core = (BACKEND_DIR / "peldrun").resolve()
    return [
        {
            "path": str(embedded_core),
            "is_valid": is_valid_core_dir(embedded_core),
            "is_embedded": True,
            "engine_type": EngineType.CORE.value,
        }
    ]


def resolve_active_engine_path() -> Path:
    """Resolve active engine filesystem path deterministically."""
    embedded_path = BACKEND_DIR / "peldrun"
    if embedded_path.is_dir():
        return embedded_path
    return BACKEND_DIR


def inject_engine_to_syspath() -> Path:
    """Embedded core requires no external sys.path injection."""
    return resolve_active_engine_path()


def get_engine_status() -> Dict[str, Any]:
    """Provide runtime engine health diagnostics."""
    core_ver = "unavailable"
    core_available = is_core_engine_available()

    if core_available:
        try:
            import peldrun

            core_ver = getattr(peldrun, "__version__", "0.2.0")
        except Exception:
            core_ver = "installed"

    return {
        "active_engine": EngineType.CORE.value,
        "core_available": core_available,
        "core_embedded": True,
        "core_version": core_ver,
        "legacy_available": False,
        "engine_path": str(resolve_active_engine_path()),
    }


__all__ = [
    "PROJECT_ROOT",
    "BACKEND_DIR",
    "EngineType",
    "ACTIVE_ENGINE",
    "EngineConfigurationError",
    "UnknownEngineError",
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
    "validate_engine_id",
    "set_active_engine_type",
    "detect_potential_engine_paths",
    "resolve_active_engine_path",
    "inject_engine_to_syspath",
    "get_engine_status",
]
