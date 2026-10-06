"""
PELDRUN Web Central System and Storage Configuration.

Manages persistent storage paths, workspace anchoring, and execution engine roots.
Natively prioritizes embedded PELDRUN Core while maintaining backward-compatible
interfaces for existing storage and router consumers.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Dict

from omweb.engine_resolver import (
    BACKEND_DIR,
    PROJECT_ROOT,
    get_persisted_engine_path,
    is_valid_peldrun_dir,
)

STORAGE_ROOT = (PROJECT_ROOT / "storage").resolve()
CHATS_DIR = STORAGE_ROOT / "chats"
PROJECTS_DIR = STORAGE_ROOT / "projects"
CONFIG_DIR = (PROJECT_ROOT / "config").resolve()

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
CHATS_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def resolve_peldrun_root() -> Path:
    """Resolve active engine root prioritizing local embedded package directory."""
    embedded_dir = (BACKEND_DIR / "peldrun").resolve()
    if embedded_dir.is_dir() and (embedded_dir / "__init__.py").is_file():
        return embedded_dir

    persisted = get_persisted_engine_path()
    if persisted and is_valid_peldrun_dir(persisted):
        return persisted

    adjacent_dir = (PROJECT_ROOT.parent / "peldrun").resolve()
    if is_valid_peldrun_dir(adjacent_dir):
        return adjacent_dir

    return PROJECT_ROOT


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
    "resolve_peldrun_root",
    "get_workspace_root",
    "get_storage_root",
    "get_engine_status",
]