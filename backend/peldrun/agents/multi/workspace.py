# backend/peldrun/agents/multi/workspace.py
"""
PELDRUN Multi-Agent Workspace Isolation and Security Boundary Enforcer.
Provides deterministic workspace scoping and defense-in-depth against path traversal
vulnerabilities (e.g., '..', absolute paths, and symlink escapes) under Phase M3.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union
from uuid import UUID

logger = logging.getLogger("peldrun.agents.multi.workspace")


class WorkspaceBoundaryError(ValueError):
    """Raised when a file path escapes the designated workspace boundary."""
    pass


def resolve_child_workspace(
    parent_root: Union[str, Path],
    child_run_id: Union[str, UUID],
) -> Path:
    """
    Derive and safely instantiate a fully isolated workspace directory for a child agent.

    Guarantees:
    - Rooted strictly under the parent workspace: `<parent_root>/.peldrun/children/<child_run_id>`.
    - Canonical path resolution to neutralize directory traversal tokens.
    - Automatic creation of the child directory sandbox.
    """
    parent_path = Path(parent_root).expanduser().resolve()
    parent_path.mkdir(parents=True, exist_ok=True)

    child_dir_name = str(child_run_id)
    child_workspace = (parent_path / ".peldrun" / "children" / child_dir_name).resolve()

    try:
        is_contained = child_workspace.is_relative_to(parent_path)
    except AttributeError:
        # Compatibility fallback for Python environments < 3.9
        try:
            child_workspace.relative_to(parent_path)
            is_contained = True
        except ValueError:
            is_contained = False

    if not is_contained:
        err_msg = (
            f"Security violation: Derived child workspace '{child_workspace}' "
            f"escapes parent root boundary '{parent_path}'."
        )
        logger.error(err_msg)
        raise WorkspaceBoundaryError(err_msg)

    child_workspace.mkdir(parents=True, exist_ok=True)
    logger.debug("Isolated child workspace established at: %s", child_workspace)
    return child_workspace


def validate_within_boundary(
    workspace_root: Union[str, Path],
    target_path: Union[str, Path],
) -> Path:
    """
    Verify that target_path resides strictly within the workspace_root directory.

    Neutralizes:
    - Dot-dot escapes (e.g., '../../etc/passwd')
    - External absolute path injections
    - Symlinks resolving outside the root sandbox
    """
    root = Path(workspace_root).expanduser().resolve()
    target = Path(target_path)

    if target.is_absolute():
        resolved_target = target.resolve()
    else:
        resolved_target = (root / target).resolve()

    try:
        is_safe = resolved_target.is_relative_to(root)
    except AttributeError:
        try:
            resolved_target.relative_to(root)
            is_safe = True
        except ValueError:
            is_safe = False

    if not is_safe:
        err_msg = (
            f"Path traversal detected: Target '{target_path}' (resolved: '{resolved_target}') "
            f"is outside workspace sandbox '{root}'."
        )
        logger.warning(err_msg)
        raise WorkspaceBoundaryError(err_msg)

    return resolved_target


def sanitize_deliverable_path(
    workspace_root: Union[str, Path],
    deliverable_path_str: str,
) -> str:
    """
    Validate a deliverable path emitted by an agent and return its clean relative POSIX path.
    Raises WorkspaceBoundaryError if the path breaches the workspace boundary.
    """
    root = Path(workspace_root).expanduser().resolve()
    valid_resolved = validate_within_boundary(root, deliverable_path_str)
    return valid_resolved.relative_to(root).as_posix()
