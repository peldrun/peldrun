from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from ..errors import ValidationError

# Allowed branch name regex pattern compliant with git check-ref-format
_BRANCH_NAME_REGEX = re.compile(
    r"^(?!/|.*//|.*@\{|.*\\|\.lock$)"
    r"[^\0-37\177 ~^:?*\[]+$"
)


def validate_branch_name(branch_name: str) -> str:
    """Validates git branch name to prevent command injection and malformed references."""
    if not branch_name or not isinstance(branch_name, str):
        raise ValidationError("Branch name cannot be empty.")

    clean_name = branch_name.strip()
    if not _BRANCH_NAME_REGEX.match(clean_name) or clean_name.startswith("-") or clean_name.endswith("."):
        raise ValidationError(
            f"Invalid branch name '{branch_name}': violates git naming rules."
        )
    return clean_name


def secure_resolve_path(workspace_root: str | Path, target_path: str | Path) -> Path:
    """Resolves a target path against a workspace root and enforces strict boundary containment.

    Raises:
        ValidationError: If path traversal or boundary escape is detected.
    """
    root = Path(workspace_root).resolve()

    # Resolve target path safely
    try:
        target = Path(target_path)
        if not target.is_absolute():
            resolved = (root / target).resolve()
        else:
            resolved = target.resolve()
    except Exception as exc:
        raise ValidationError(f"Invalid file path specification: '{target_path}'") from exc

    # Ensure resolved path is strictly inside workspace root (Path Traversal Protection)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            f"Security violation: path traversal attempt detected for path '{target_path}'."
        ) from exc

    return resolved


def is_safe_relative_path(workspace_root: str | Path, target_path: str | Path) -> bool:
    """Checks whether a target path resides safely within the workspace root without raising exceptions."""
    try:
        secure_resolve_path(workspace_root, target_path)
        return True
    except ValidationError:
        return False
