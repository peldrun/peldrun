"""
backend/omweb/engine_resolver.py

PELDRUN Universal Engine Resolver (Decoupled Architecture - P1-01).

Manages execution engine selection, explicit validation, and runtime resolution.
Treats PELDRUN Core as a first-class native embedded runtime package in `backend/peldrun/`.
Enforces strict fail-fast semantics for unknown or missing engine configurations.
Completely eliminates heuristic runtime filesystem probing.
"""

from __future__ import annotations

from enum import Enum
import importlib.util
import json
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


class EngineConfigurationError(RuntimeError):
    """Raised when engine configuration is missing, empty, or unreadable."""

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
    """Validate if directory contains legacy OpenManus source files."""
    if not target_path or not target_path.exists() or not target_path.is_dir():
        return False
    has_agent = (
        (target_path / "app" / "agent" / "peldrun.py").is_file()
        or (target_path / "app" / "agent" / "manus.py").is_file()
    )
    has_config = (
        (target_path / "config" / "config.toml").is_file()
        or (target_path / "config" / "config.example.toml").is_file()
    )
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
    """
    Check if legacy OpenManus engine is available via explicit import or configured path.
    
    Eliminates heuristic crawling of parent and user home directories.
    """
    try:
        if importlib.util.find_spec("app.agent.peldrun") or importlib.util.find_spec("app.agent.manus"):
            return True
    except Exception:
        pass

    # Check explicit configured path
    config = get_persisted_engine_config()
    custom_openmanus = config.get("openmanus_path")
    if custom_openmanus:
        custom_path = Path(custom_openmanus).resolve()
        if is_valid_legacy_dir(custom_path):
            return True

    # Check designated in-project legacy directory
    for project_cand in [
        PROJECT_ROOT / "engine" / "peldrun",
        PROJECT_ROOT / "engine" / "openmanus",
    ]:
        if is_valid_legacy_dir(project_cand.resolve()):
            return True

    return False


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
    embedded_core = BACKEND_DIR / "peldrun"
    if embedded_core.is_dir():
        return embedded_core

    config = get_persisted_engine_config()
    custom_path = config.get("peldrun_path")
    if custom_path:
        p = Path(custom_path).resolve()
        if is_valid_peldrun_dir(p):
            return p

    return None


def save_engine_path(engine_path: Path, engine_type: Optional[str] = None) -> None:
    """Persist active engine configuration for backward-compatibility callers."""
    config = get_persisted_engine_config()
    if engine_type:
        norm_type = str(engine_type).strip().lower()
        if norm_type in [EngineType.CORE.value, "peldrun-core", "core"]:
            config["engine_type"] = EngineType.CORE.value
        elif norm_type in [EngineType.LEGACY.value, "legacy", "manus"]:
            config["engine_type"] = EngineType.LEGACY.value
        else:
            raise UnknownEngineError(f"Unsupported engine type '{engine_type}'.")

    # Sanitize obsolete external path
    config.pop("peldrun_path", None)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_active_engine_type(allow_default: bool = False) -> EngineType:
    """
    Determine the active engine type prioritizing explicit configuration.
    
    Enforces strict fail-fast semantics:
    - Missing configuration raises EngineConfigurationError (unless allow_default=True).
    - Unknown engine ID raises UnknownEngineError.
    """
    config = get_persisted_engine_config()
    raw_type = config.get("engine_type")

    if raw_type is None or not str(raw_type).strip():
        if allow_default:
            return EngineType.CORE
        raise EngineConfigurationError(
            "Execution engine is not configured in engine_config.json. "
            "Explicit engine selection ('peldrun' or 'openmanus') is required."
        )

    normalized = str(raw_type).strip().lower()
    if normalized in [EngineType.CORE.value, "peldrun-core", "core"]:
        return EngineType.CORE
    if normalized in [EngineType.LEGACY.value, "legacy", "manus"]:
        return EngineType.LEGACY

    valid_engines = [e.value for e in EngineType]
    raise UnknownEngineError(
        f"Unknown engine '{raw_type}' specified in engine configuration. "
        f"Supported engines: {', '.join(valid_engines)}"
    )


def get_active_engine_name() -> str:
    """Return the normalized active engine identifier string."""
    return get_active_engine_type().value


def validate_engine_id(engine_id: str) -> str:
    """
    Validate that an engine identifier or alias is registered and supported.
    
    Returns canonical primary engine ID string.
    Raises EngineConfigurationError if empty, or UnknownEngineError if unsupported.
    """
    if not engine_id or not str(engine_id).strip():
        raise EngineConfigurationError("Engine identifier cannot be empty.")

    normalized = str(engine_id).strip().lower()
    if normalized in [EngineType.CORE.value, "peldrun-core", "core"]:
        return EngineType.CORE.value
    if normalized in [EngineType.LEGACY.value, "legacy", "manus"]:
        return EngineType.LEGACY.value

    # Cross-validate against authoritative EngineRegistry if loaded
    try:
        from omweb.engines.registry import engine_registry

        if engine_registry.has(normalized):
            return normalized
        available = engine_registry.list_engines()
        raise UnknownEngineError(
            f"Execution engine '{engine_id}' is not registered. Registered engines: {', '.join(available)}"
        )
    except ImportError:
        pass

    valid_engines = [e.value for e in EngineType]
    raise UnknownEngineError(
        f"Execution engine '{engine_id}' is not recognized. Supported engines: {', '.join(valid_engines)}"
    )


def set_active_engine_type(engine_type: Union[EngineType, str]) -> None:
    """
    Persist active engine selection after validating against supported engines.
    
    Eliminates silent fallback and clears obsolete legacy paths.
    """
    canonical_id = validate_engine_id(
        engine_type.value if isinstance(engine_type, EngineType) else str(engine_type)
    )

    config = get_persisted_engine_config()
    config["engine_type"] = canonical_id
    config.pop("peldrun_path", None)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def detect_potential_engine_paths() -> List[Dict[str, Any]]:
    """
    Probe project workspace for available engines (offline discovery utility only).
    
    Strictly isolated from runtime execution resolution to prevent performance overhead.
    """
    results: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # 1. Native embedded PELDRUN Core
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

    # 2. Configured legacy path if explicitly set
    config = get_persisted_engine_config()
    custom_openmanus = config.get("openmanus_path")
    if custom_openmanus:
        cand_path = Path(custom_openmanus).resolve()
        path_str = str(cand_path)
        if path_str not in seen:
            seen.add(path_str)
            results.append({
                "path": path_str,
                "is_valid": is_valid_legacy_dir(cand_path),
                "is_embedded": False,
                "engine_type": EngineType.LEGACY.value,
            })

    # 3. Project-local engine directories
    for cand in [
        (PROJECT_ROOT / "engine" / "peldrun").resolve(),
        (PROJECT_ROOT / "engine" / "openmanus").resolve(),
    ]:
        path_str = str(cand)
        if path_str in seen:
            continue
        seen.add(path_str)
        if is_valid_legacy_dir(cand):
            results.append({
                "path": path_str,
                "is_valid": True,
                "is_embedded": True,
                "engine_type": EngineType.LEGACY.value,
            })

    return results


def resolve_active_engine_path() -> Path:
    """
    Resolve active engine filesystem path deterministically without external heuristic probing.
    
    Raises FileNotFoundError if active legacy engine is unconfigured or invalid.
    """
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
        custom_p = Path(custom_openmanus).resolve()
        if is_valid_legacy_dir(custom_p):
            return custom_p
        raise FileNotFoundError(
            f"Configured legacy engine path '{custom_openmanus}' does not exist or is not a valid OpenManus directory."
        )

    # In-project legacy candidate paths
    project_legacy = (PROJECT_ROOT / "engine" / "peldrun").resolve()
    if is_valid_legacy_dir(project_legacy):
        return project_legacy

    project_openmanus = (PROJECT_ROOT / "engine" / "openmanus").resolve()
    if is_valid_legacy_dir(project_openmanus):
        return project_openmanus

    raise FileNotFoundError(
        "Legacy OpenManus engine was selected, but no valid installation was found in the project. "
        "Please specify an explicit 'openmanus_path' in engine_config.json."
    )


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
    """Provide runtime engine health diagnostics and explicit configuration status."""
    active_name = "unconfigured"
    try:
        active_type = get_active_engine_type(allow_default=True)
        active_name = active_type.value
    except Exception as exc:
        active_name = f"error: {exc}"

    core_ver = "unavailable"
    core_available = is_core_engine_available()

    if core_available:
        try:
            import peldrun

            core_ver = getattr(peldrun, "__version__", "0.2.0")
        except Exception:
            core_ver = "installed"

    try:
        resolved_path = str(resolve_active_engine_path())
    except Exception as exc:
        resolved_path = f"unresolved: {exc}"

    return {
        "active_engine": active_name,
        "core_available": core_available,
        "core_embedded": True,
        "core_version": core_ver,
        "legacy_available": is_legacy_engine_available(),
        "engine_path": resolved_path,
    }


__all__ = [
    "PROJECT_ROOT",
    "BACKEND_DIR",
    "CONFIG_FILE",
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