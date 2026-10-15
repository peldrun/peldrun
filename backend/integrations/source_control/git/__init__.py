from __future__ import annotations

from .branch import GitBranchManager
from .commit import GitCommitManager
from .conflict import (
    ConflictMarkerSection,
    ConflictReport,
    FileConflictDetail,
    GitConflictDetector,
)
from .diff import GitDiffEngine
from .executable import (
    GitExecutable,
    GitExecutionError,
    GitNotFoundError,
    GitResult,
    GitTimeoutError,
)
from .path_policy import (
    is_safe_relative_path,
    secure_resolve_path,
    validate_branch_name,
)
from .push import GitPushManager, PushResult
from .status import FileStatusEntry, GitStatusInspector, WorkingTreeStatus
from .workspace import GitWorkspaceManager, WorkspaceError

__all__ = [
    # Executable Runner & Outcomes
    "GitExecutable",
    "GitResult",
    "GitExecutionError",
    "GitNotFoundError",
    "GitTimeoutError",
    # Path Security & Branch Validation Policies
    "validate_branch_name",
    "secure_resolve_path",
    "is_safe_relative_path",
    # Workspace & Worktree Management
    "WorkspaceError",
    "GitWorkspaceManager",
    # Status & Working Tree Inspection
    "FileStatusEntry",
    "WorkingTreeStatus",
    "GitStatusInspector",
    # Diff & Patch Analysis Engine
    "GitDiffEngine",
    # Branch Lifecycle Management
    "GitBranchManager",
    # Commit & Staging Management
    "GitCommitManager",
    # Remote Push & Policy Enforcement
    "PushResult",
    "GitPushManager",
    # Merge/Rebase Conflict Diagnostics & Abort
    "ConflictMarkerSection",
    "FileConflictDetail",
    "ConflictReport",
    "GitConflictDetector",
]
