# backend/peldrun/storage_paths.py
"""
Centralized storage path resolution for PELDRUN core and web layers.

All persistent files (SQLite databases, metadata caches, budgets)
live under a single storage root. This module is the single source
of truth for that root.

Consumers (audit items 2.2 + 2.3):
    - peldrun.runtime.pricing_store
    - peldrun.runtime.usage_store
    - peldrun.runtime.store
    - peldrun.artifacts.store
    - omweb.config (STORAGE_ROOT)
    - omweb.agent_bridge_parts.dispatcher
    - omweb.resource_monitor.config

Design rules:
    - The root is anchored via __file__, NOT the current working
      directory. Launching the app from any subdirectory produces
      the same paths.
    - ``PELDRUN_STORAGE_ROOT`` overrides the anchor for deployments
      that keep storage on a separate volume (Docker volumes,
      network shares, encrypted containers).
    - The module has zero dependencies on omweb or on any other
      PELDRUN subsystem, so it can be imported from the earliest
      possible bootstrap point.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("peldrun.storage_paths")

#: Environment variable that overrides the storage root.
ENV_STORAGE_ROOT = "PELDRUN_STORAGE_ROOT"

#: Optional override for the repository root (rarely needed).
ENV_REPO_ROOT = "PELDRUN_REPO_ROOT"


def get_repo_root() -> Path:
    """Locate the PELDRUN repository root, independent of CWD.

    Detection strategy (in order):
        1. ``PELDRUN_REPO_ROOT`` environment variable.
        2. ``__file__``-based detection:
           - Embedded install: ``backend/peldrun/storage_paths.py``
             → repo root is two levels up from ``peldrun/``.
           - Standalone install: ``peldrun/storage_paths.py``
             → repo root is one level up from ``peldrun/``.
        3. Defensive fallback: grandparent of ``__file__``.

    The function never consults the current working directory, so
    callers can launch the application from any subdirectory and
    get the same result.
    """
    env = os.environ.get(ENV_REPO_ROOT, "").strip()
    if env:
        return Path(env).expanduser().resolve()

    here = Path(__file__).resolve()

    # Embedded install: backend/peldrun/storage_paths.py
    # here.parent.parent.parent is the repo root.
    grandparent = here.parent.parent.parent
    if (grandparent / "backend" / "peldrun" / "__init__.py").exists():
        return grandparent

    # Standalone install: peldrun/storage_paths.py
    # here.parent.parent is the repo root.
    parent = here.parent.parent
    if (parent / "peldrun" / "__init__.py").exists():
        return parent

    # Fallback: use grandparent (better than CWD-relative)
    logger.warning(
        "Could not confidently detect PELDRUN repo root; using %s. "
        "Set PELDRUN_REPO_ROOT to override.",
        grandparent,
    )
    return grandparent


def get_storage_root() -> Path:
    """Return the absolute storage root, creating it if missing.

    Priority:
        1. ``PELDRUN_STORAGE_ROOT`` environment variable.
        2. ``<repo_root>/storage``.
    """
    env = os.environ.get(ENV_STORAGE_ROOT, "").strip()
    if env:
        root = Path(env).expanduser().resolve()
    else:
        root = (get_repo_root() / "storage").resolve()

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create storage root %s: %s", root, exc)
    return root


def get_storage_file(relative_name: str) -> Path:
    """Return an absolute path to ``relative_name`` inside the storage root.

    Args:
        relative_name: File name relative to the storage root,
            e.g. ``"peldrun_runtime.db"``. Leading slashes are
            stripped so the result is always inside the root.

    Returns:
        Absolute path under ``get_storage_root()``. The parent
        directory is guaranteed to exist.
    """
    root = get_storage_root()
    clean = relative_name.lstrip("/\\")
    path = root / clean
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


__all__ = [
    "ENV_STORAGE_ROOT",
    "ENV_REPO_ROOT",
    "get_repo_root",
    "get_storage_root",
    "get_storage_file",
]
