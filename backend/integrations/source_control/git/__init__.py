# backend/integrations/source_control/git/__init__.py
"""
Local Git engine package for the Source Control Integration subsystem.

This package exposes the concrete managers that implement the local
Git ports (`WorkspacePort`, `DiffPort`, `PushPort`, `LocalBranchPort`,
`CommitPort`) plus the low-level subprocess wrapper and the path
policy. It is the only package permitted to spawn a Git process.

Design rules:
    - The package never imports from `providers/` or `operations/`.
      It depends only on `domain`, `errors`, `ports`, and `security`.
    - Concrete managers are constructed lazily; importing the package
      does not spawn any subprocess.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Low-level subprocess wrapper
# ---------------------------------------------------------------------------

from .executable import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_OUTPUT_BYTES,
    MINIMUM_GIT_VERSION,
    GitExecutable,
    GitExecutionError,
    GitNotAvailableError,
    GitResult,
    GitTimeoutError,
    get_default_git_executable,
    reset_default_git_executable,
    set_default_git_executable,
)

# ---------------------------------------------------------------------------
# Path policy and branch validation
# ---------------------------------------------------------------------------

from .path_policy import (
    MAX_BRANCH_NAME_LENGTH,
    MAX_COMPONENT_LENGTH,
    MAX_PATH_LENGTH,
    PROTECTED_LOCAL_BRANCH_NAMES,
    WorkspacePathPolicy,
    get_default_path_policy,
    get_workspace_root,
    is_protected_local_branch,
    is_valid_branch_name,
    make_workspace_id,
    reset_default_path_policy,
    sanitize_component,
    sanitize_repository_name,
    secure_resolve_path,
    set_default_path_policy,
    validate_branch_name,
)

# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------

from .workspace import (
    DEFAULT_MAX_WORKSPACES,
    GitWorkspaceManager,
    RepositoryResolver,
    get_default_workspace_manager,
    reset_default_workspace_manager,
    set_default_workspace_manager,
)

# ---------------------------------------------------------------------------
# Branch, commit, and diff managers
# ---------------------------------------------------------------------------

from .branch import (
    GitBranchManager,
    get_default_branch_manager,
    reset_default_branch_manager,
    set_default_branch_manager,
)

from .commit import (
    DEFAULT_AUTHOR_EMAIL,
    DEFAULT_AUTHOR_NAME,
    MAX_COMMIT_MESSAGE_LENGTH,
    MAX_LISTED_COMMITS,
    GitCommitManager,
    get_default_commit_manager,
    reset_default_commit_manager,
    set_default_commit_manager,
)

from .diff import (
    DEFAULT_MAX_FILES,
    DEFAULT_UNIFIED_CONTEXT,
    MAX_FILE_SIZE,
    MAX_PATCH_SIZE,
    GitDiffEngine,
    get_default_diff_engine,
    reset_default_diff_engine,
    set_default_diff_engine,
)

# ---------------------------------------------------------------------------
# Push manager
# ---------------------------------------------------------------------------

from .push import (
    DEFAULT_PUSH_TIMEOUT_SECONDS,
    DELETE_BRANCH_TIMEOUT_SECONDS,
    GitPushManager,
    PushOutcome,
    get_default_push_manager,
    reset_default_push_manager,
    set_default_push_manager,
)

# ---------------------------------------------------------------------------
# Status reader
# ---------------------------------------------------------------------------

from .status import (
    MAX_FILE_LIST,
    GitStatusReader,
    PorcelainEntry,
    get_default_status_reader,
    reset_default_status_reader,
    set_default_status_reader,
)

# ---------------------------------------------------------------------------
# Conflict inspection and resolution
# ---------------------------------------------------------------------------

from .conflict import (
    MAX_CONFLICT_FILES,
    MAX_STAGE_CONTENT_BYTES,
    ConflictEntry,
    ConflictStage,
    GitConflictManager,
    OperationState,
    ResolutionSide,
    get_default_conflict_manager,
    reset_default_conflict_manager,
    set_default_conflict_manager,
)

# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
#
# The package previously re-exported a different naming scheme used by
# an older design. The aliases below keep those import paths working:
#
#     - `GitNotFoundError`  -> `GitNotAvailableError`
#     - `is_safe_relative_path` -> helper derived from `resolve_within_root`
#     - `FileStatusEntry`   -> `PorcelainEntry`
#     - `WorkingTreeStatus` -> `LocalGitStatus` (from ports.repository)
#     - `GitStatusInspector` -> `GitStatusReader`
#     - `GitConflictDetector` -> `GitConflictManager`
#     - `FileConflictDetail`  -> `ConflictEntry`
#     - `WorkspaceError`    -> generic wrapper around `ValidationError`
#     - `ConflictMarkerSection` / `ConflictReport` -> small compatibility
#       containers built on top of `ConflictEntry`

from typing import Any, Mapping, Sequence
from pathlib import PurePosixPath

# `LocalGitStatus` lives in `ports.repository`; re-export it here so
# callers do not need to reach across packages for a common type.
from ..ports.repository import LocalGitStatus  # noqa: E402

#: Alias for `GitNotAvailableError`.
GitNotFoundError = GitNotAvailableError

#: Alias for `PorcelainEntry`.
FileStatusEntry = PorcelainEntry

#: Alias for `LocalGitStatus`.
WorkingTreeStatus = LocalGitStatus

#: Alias for `GitStatusReader`.
GitStatusInspector = GitStatusReader

#: Alias for `GitConflictManager`.
GitConflictDetector = GitConflictManager

#: Alias for `ConflictEntry`.
FileConflictDetail = ConflictEntry


def is_safe_relative_path(path: str) -> bool:
    """
    Return True if `path` is a safe, workspace-relative path.

    A path is considered safe when it is non-empty, does not start
    with a path separator, does not contain a `..` segment, and does
    not contain a NUL byte. The check is purely lexical; it does not
    consult the filesystem.

    This function is a convenience predicate for callers that need a
    boolean answer. For full path validation against the workspace
    root, use `secure_resolve_path` or
    `WorkspacePathPolicy.resolve_within_root`.
    """
    if not path or not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if "\x00" in normalized:
        return False
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        return False
    # Reject Windows drive prefixes such as `C:` at the start.
    if len(normalized) >= 2 and normalized[1] == ":":
        return False
    return True


class WorkspaceError(Exception):
    """
    Backward-compatibility exception wrapper.

    The canonical errors for workspace operations are defined in
    `errors.py` as `SourceControlError` subclasses (for example
    `ValidationError` and `WorkspaceNotFoundError`). This class exists
    only so that older callers that catch a `WorkspaceError` continue
    to compile; it is NOT raised by the current implementation.
    """


class ConflictMarkerSection(str):
    """
    Backward-compatibility container for a single conflict section.

    The current `GitConflictManager` exposes the raw stages of a
    conflicted file through `ConflictEntry` rather than the parsed
    marker sections. This class is a `str`-typed placeholder so that
    older callers importing it do not fail at import time; it is NOT
    produced by the current implementation.
    """


class ConflictReport:
    """
    Backward-compatibility report for a conflict inspection.

    The current `GitConflictManager` returns a sequence of
    `ConflictEntry` objects directly. This class exists only so that
    older callers importing `ConflictReport` do not fail at import
    time; it is NOT produced by the current implementation.
    """

    def __init__(
        self,
        entries: Sequence[ConflictEntry] = (),
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.entries = tuple(entries)
        self.metadata = dict(metadata or {})

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"<ConflictReport entries={len(self.entries)}>"
