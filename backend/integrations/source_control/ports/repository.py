# backend/integrations/source_control/ports/repository.py
"""
Local repository and Git engine ports for the Source Control
Integration subsystem.

Unlike `ports.provider`, which defines the contract with remote
providers (GitHub, GitLab, Bitbucket), this module defines the
contracts implemented by the LOCAL Git engine:

    - `WorkspacePort`     : isolated workspace lifecycle on disk.
    - `DiffPort`          : diff computation against a base commit.
    - `PushPort`          : pushing a local branch to a remote.
    - `LocalBranchPort`   : local branch creation and inspection.
    - `CommitPort`        : creating local commits.

These ports are implemented by the `git/` package and consumed by the
application layer (`operations/service.py`). They MUST NOT accept or
return raw credentials; authentication for push is provided by the
facade through a secure credential helper.

Design rules:
    - All methods that perform I/O MUST be `async`.
    - No method may spawn a shell. Implementations MUST use
      `asyncio.create_subprocess_exec` with argument arrays.
    - No method may accept arbitrary local paths from the model.
      Paths are resolved by `git.path_policy` before use.
    - Methods MUST raise `SourceControlError` subclasses on failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.enums import FileChangeType, ProviderId
from ..domain.models import (
    BranchRef,
    CommitRef,
    DiffSummary,
    RepositoryRef,
    WorkspaceRef,
)

__all__ = [
    # Re-exports (for convenience)
    "RepositoryRef",
    "BranchRef",
    "WorkspaceRef",
    "DiffSummary",
    "CommitRef",
    # Value objects
    "LocalGitStatus",
    "PushResult",
    "WorkspaceCreateOptions",
    "CommitOptions",
    # Ports
    "WorkspacePort",
    "DiffPort",
    "PushPort",
    "LocalBranchPort",
    "CommitPort",
]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalGitStatus:
    """
    Snapshot of the local Git status of a workspace.

    A workspace is `dirty` when there are uncommitted changes. It is
    `conflicted` when the index contains unmerged paths. `ahead` and
    `behind` reflect divergence from the upstream branch, when one is
    configured.
    """

    is_clean: bool = True
    is_conflicted: bool = False
    ahead: int = 0
    behind: int = 0
    current_branch: Optional[str] = None
    head_sha: Optional[str] = None
    upstream_branch: Optional[str] = None
    changed_paths: Sequence[str] = field(default_factory=tuple)

    def is_dirty(self) -> bool:
        """Return True if the workspace has uncommitted changes."""
        return not self.is_clean

    def has_divergence(self) -> bool:
        """Return True if the branch is ahead or behind its upstream."""
        return self.ahead > 0 or self.behind > 0


@dataclass(frozen=True)
class PushResult:
    """
    Outcome of a push attempt.

    `accepted` reflects the remote's decision. `rejected_reason`
    carries a normalized code such as `"non_fast_forward"`,
    `"protected_branch"`, or `"hook_rejected"` when the push is
    refused. `remote_sha` is the SHA the remote now points to, which
    may differ from `local_sha` if a hook rewrote the commit.
    """

    accepted: bool
    local_branch: str
    local_sha: str
    remote_branch: Optional[str] = None
    remote_sha: Optional[str] = None
    rejected_reason: Optional[str] = None
    message: Optional[str] = None
    pushed_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True)
class WorkspaceCreateOptions:
    """
    Optional parameters for workspace creation.

    All fields have safe defaults. The facade and the agent tools
    SHOULD prefer these defaults unless the user explicitly requests
    otherwise.
    """

    #: When True, create a Git worktree instead of a full clone.
    use_worktree: bool = True
    #: Fetch depth for shallow clones. `None` means full history.
    depth: Optional[int] = None
    #: When True, do not create a new branch; check out `source_branch`.
    reuse_existing_branch: bool = False
    #: When True, fail if the workspace already exists.
    fail_if_exists: bool = True
    #: Free-form metadata to attach to the workspace (never secrets).
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitOptions:
    """
    Optional parameters for creating a local commit.
    """

    #: Author name for the commit. Defaults to the workspace's Git config.
    author_name: Optional[str] = None
    #: Author email for the commit. Defaults to the workspace's Git config.
    author_email: Optional[str] = None
    #: When True, create an empty commit even if nothing changed.
    allow_empty: bool = False
    #: When True, stage all changes before committing.
    stage_all: bool = True


# ---------------------------------------------------------------------------
# Workspace port
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkspacePort(Protocol):
    """
    Isolated workspace lifecycle.

    A workspace is a PELDRUN-owned directory under the configured
    workspace root. It is bound to exactly one repository and one base
    commit. It is either a full clone or a Git worktree.

    Implementations MUST:

        - Validate every path against the configured root.
        - Refuse to create a workspace outside the root.
        - Preserve user changes on failure (never `git clean -fdx`).
        - Reject symlink escapes.
    """

    async def create_workspace(
        self,
        repository_id: Optional[str] = None,
        *,
        repo: Optional[RepositoryRef] = None,
        provider: Optional[ProviderId] = None,
        base_sha: Optional[str] = None,
        branch_name: Optional[str] = None,
        source_branch: Optional[str] = None,
        token: Optional[str] = None,
        options: Optional[WorkspaceCreateOptions] = None,
    ) -> WorkspaceRef:
        """
        Create a new workspace.

        Exactly one of `repository_id` or `repo` MUST be provided.
        Exactly one of `branch_name` or `source_branch` MAY be
        provided; when both are `None`, the repository's default
        branch is used.

        `branch_name` and `source_branch` are treated as synonyms for
        backward compatibility with existing callers and the test
        suite.

        Raises:
            ValidationError: If the inputs are inconsistent.
            WorkspaceNotFoundError: If the repository cannot be
                resolved.
            PathTraversalError: If the resolved path escapes the root.
        """
        ...

    async def get_workspace(
        self, workspace_id: str
    ) -> Optional[WorkspaceRef]:
        """Return the workspace or `None` if unknown."""
        ...

    async def refresh_workspace_status(
        self, workspace_id: str
    ) -> WorkspaceRef:
        """
        Recompute the status of a workspace and return the updated
        reference.

        Raises:
            WorkspaceNotFoundError: If the ID is unknown.
        """
        ...

    async def delete_workspace(self, workspace_id: str) -> None:
        """
        Remove a PELDRUN-owned workspace from disk.

        This is a destructive operation and MUST only be called for
        workspaces owned by PELDRUN. It MUST NOT delete user clones
        outside the workspace root.
        """
        ...

    async def list_workspaces(
        self,
    ) -> Sequence[WorkspaceRef]:
        """Return all known workspaces, ordered by creation time."""
        ...


# ---------------------------------------------------------------------------
# Diff port
# ---------------------------------------------------------------------------


@runtime_checkable
class DiffPort(Protocol):
    """
    Diff computation against a base commit.

    Implementations MUST produce a deterministic `DiffSummary` whose
    `sha256()` is stable across runs, platforms, and orderings.
    """

    async def compute_diff(
        self,
        workspace_id: str,
        *,
        base_sha: Optional[str] = None,
        include_patch: bool = True,
        max_file_size_bytes: int = 1_000_000,
    ) -> DiffSummary:
        """
        Compute the diff of the workspace against `base_sha`.

        When `base_sha` is `None`, the workspace's recorded base is
        used. When `include_patch` is `False`, `FileDiff.patch` is
        `None` for all files, which is faster for large diffs.

        Raises:
            WorkspaceNotFoundError: If the workspace is unknown.
        """
        ...

    async def read_workspace_file(
        self,
        workspace_id: str,
        path: str,
        *,
        max_bytes: int = 1_000_000,
    ) -> str:
        """
        Read a file inside the workspace.

        The path MUST be resolved inside the workspace root. Symlinks
        that escape the root MUST be rejected.
        """
        ...


# ---------------------------------------------------------------------------
# Push port
# ---------------------------------------------------------------------------


@runtime_checkable
class PushPort(Protocol):
    """
    Push a local branch to a remote.

    Implementations MUST:

        - Never persist credentials in `.git/config` or remote URLs.
        - Use `GIT_ASKPASS` or an ephemeral credential helper.
        - Support cancellation and timeouts.
        - Reconcile ambiguous outcomes by re-querying the remote.
    """

    async def push_branch(
        self,
        workspace_id: str,
        *,
        local_branch: str,
        remote: str = "origin",
        remote_branch: Optional[str] = None,
        force_with_lease: bool = False,
        set_upstream: bool = True,
        token: Optional[str] = None,
        timeout_seconds: int = 120,
    ) -> PushResult:
        """
        Push `local_branch` to `remote_branch` (defaults to the same
        name).

        When `force_with_lease` is True, the push uses
        `--force-with-lease` and will be rejected by the remote if
        someone else has pushed in the meantime.

        Raises:
            ProtectedBranchError: If the target branch is protected.
            ConflictError: If the remote rejected the push due to a
                non-fast-forward update.
        """
        ...

    async def delete_remote_branch(
        self,
        workspace_id: str,
        *,
        remote: str,
        remote_branch: str,
        token: Optional[str] = None,
        timeout_seconds: int = 60,
    ) -> None:
        """
        Delete a remote branch.

        This operation is disabled by default in PELDRUN policy and
        MUST only be called after explicit approval.
        """
        ...


# ---------------------------------------------------------------------------
# Local branch port
# ---------------------------------------------------------------------------


@runtime_checkable
class LocalBranchPort(Protocol):
    """
    Local branch creation and inspection.
    """

    async def create_local_branch(
        self,
        workspace_id: str,
        *,
        branch_name: str,
        from_sha: Optional[str] = None,
    ) -> BranchRef:
        """
        Create a local branch and check it out.

        When `from_sha` is `None`, the workspace's current `HEAD` is
        used.

        Raises:
            ConflictError: If the branch already exists.
            ValidationError: If `branch_name` is invalid.
        """
        ...

    async def list_local_branches(
        self, workspace_id: str
    ) -> Sequence[BranchRef]:
        """Return all local branches in the workspace."""
        ...

    async def get_current_branch(self, workspace_id: str) -> str:
        """Return the name of the currently checked-out branch."""
        ...

    async def get_head_sha(self, workspace_id: str) -> str:
        """Return the SHA of the current `HEAD`."""
        ...


# ---------------------------------------------------------------------------
# Commit port
# ---------------------------------------------------------------------------


@runtime_checkable
class CommitPort(Protocol):
    """
    Local commit creation.
    """

    async def create_commit(
        self,
        workspace_id: str,
        *,
        message: str,
        options: Optional[CommitOptions] = None,
    ) -> CommitRef:
        """
        Stage and commit local changes.

        When `options.stage_all` is True (default), all tracked and
        untracked changes are staged before committing. This is the
        behavior needed by the review workflow.

        Raises:
            WorkspaceDirtyError: If there is nothing to commit and
                `allow_empty` is False.
            ValidationError: If `message` is empty.
        """
        ...

    async def amend_commit(
        self,
        workspace_id: str,
        *,
        message: Optional[str] = None,
    ) -> CommitRef:
        """
        Amend the most recent commit.

        This operation MUST NOT be used on commits that have already
        been pushed unless explicitly approved.
        """
        ...
