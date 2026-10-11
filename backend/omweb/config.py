"""
backend/omweb/config.py

PELDRUN Web Central System and Storage Configuration.

Manages persistent storage paths, workspace anchoring, and execution engine roots.
Natively anchors embedded PELDRUN Core as the authoritative execution runtime.
Hardened under PR 3: Completely decoupled from engine_config.json and legacy resolver logic.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional

# Core directory anchors established centrally
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

STORAGE_ROOT = (PROJECT_ROOT / "storage").resolve()
CHATS_DIR = STORAGE_ROOT / "chats"
PROJECTS_DIR = STORAGE_ROOT / "projects"
CONFIG_DIR = (PROJECT_ROOT / "config").resolve()

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
CHATS_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def is_valid_peldrun_dir(target_path: Optional[Path]) -> bool:
    """Validate if target path represents a valid PELDRUN Core package directory."""
    if not target_path or not target_path.exists() or not target_path.is_dir():
        return False
    return (
        (target_path / "__init__.py").is_file()
        or (target_path / "peldrun" / "__init__.py").is_file()
    )


def resolve_peldrun_root() -> Path:
    """Resolve active engine root prioritizing local embedded package directory."""
    embedded_dir = (BACKEND_DIR / "peldrun").resolve()
    if embedded_dir.is_dir() and (embedded_dir / "__init__.py").is_file():
        return embedded_dir

    adjacent_dir = (PROJECT_ROOT.parent / "peldrun").resolve()
    if adjacent_dir.is_dir() and (adjacent_dir / "__init__.py").is_file():
        return adjacent_dir

    return BACKEND_DIR


peldrun_ROOT = resolve_peldrun_root()
WORKSPACE_ROOT = STORAGE_ROOT

# Register project root and backend strictly in sys.path
for path_entry in [str(PROJECT_ROOT), str(BACKEND_DIR)]:
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)


def get_workspace_root() -> Path:
    """Return central workspace storage directory."""
    return STORAGE_ROOT


def get_storage_root() -> Path:
    """Return primary platform storage directory."""
    return STORAGE_ROOT


def get_engine_status() -> Dict[str, Any]:
    """Provide engine storage metrics and native embedded status."""
    has_cfg = (PROJECT_ROOT / "config" / "config.toml").exists()
    is_embedded = (BACKEND_DIR / "peldrun" / "__init__.py").is_file()

    return {
        "engine_path": str(peldrun_ROOT),
        "is_valid": is_valid_peldrun_dir(peldrun_ROOT),
        "is_embedded": is_embedded,
        "has_config": has_cfg,
        "storage_root": str(STORAGE_ROOT),
        "workspace_exists": STORAGE_ROOT.exists(),
    }


__all__ = [
    "BACKEND_DIR",
    "PROJECT_ROOT",
    "STORAGE_ROOT",
    "WORKSPACE_ROOT",
    "CHATS_DIR",
    "PROJECTS_DIR",
    "CONFIG_DIR",
    "peldrun_ROOT",
    "is_valid_peldrun_dir",
    "resolve_peldrun_root",
    "get_workspace_root",
    "get_storage_root",
    "get_engine_status",
]
