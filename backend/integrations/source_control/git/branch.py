# backend/integrations/source_control/git/branch.py
"""
Local branch management for the Source Control Integration subsystem.

This module implements `LocalBranchPort` from `ports.repository`. It
is the ONLY place in the subsystem where local branches are created,
listed, checked out, renamed, or deleted.

The manager deliberately limits itself to LOCAL branches. Remote
branch operations (fetch, push, delete remote) belong to
`workspace.py` and `push.py` respectively. Keeping the concerns
separate simplifies reasoning about the state of the repository.

Design rules:
    - Branch names are validated before any Git invocation. Invalid
      names raise `ValidationError` with a clear message rather than
      a cryptic Git error.
    - Protected branches (main, master, develop, release, stage,
      prod) cannot be deleted or renamed unless the caller explicitly
      passes `force=True`. This is a safety net, not a security
      boundary; the policy engine remains the authority.
    - Destructive operations require `force=True`. The default is
      always the safe choice.
    - The manager is stateless except for immutable references to the
      Git executable and the path policy. Instances may be shared
      across coroutines.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from ...ports.repository import LocalBranchPort
from ..domain.models import BranchRef
from ..errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
    WorkspaceNotFoundError,
)
from .executable import (
    GitExecutable,
    GitExecutionError,
    get_default_git_executable,
)
from .path_policy import (
    WorkspacePathPolicy,
    get_default_path_policy,
)

__all__ = [
    "GitBranchManager",
    "get_default_branch_manager",
    "set_default_branch_manager",
    "reset_default_branch_manager",
    "is_valid_branch_name",
    "is_protected_local_branch",
    "PROTECTED_LOCAL_BRANCH_NAMES",
    "MAX_BRANCH_NAME_LENGTH",
]


logger = logging.getLogger(__name__)


#: Maximum length of a branch name accepted by the manager. Git
#: itself allows longer names, but we cap at a value that is
#: comfortably below every filesystem and UI limit.
MAX_BRANCH_NAME_LENGTH: int = 200


#: Branch names that are treated as protected by default. These
#: cannot be deleted or renamed without `force=True`.
PROTECTED_LOCAL_BRANCH_NAMES: frozenset[str] = frozenset(
    {
        "main",
        "master",
        "trunk",
        "develop",
        "development",
        "dev",
        "release",
        "stage",
        "staging",
        "prod",
        "production",
    }
)


#: Characters and sequences that Git forbids in a ref name, mirrored
#: here so that callers receive a clear error before Git runs.
_FORBIDDEN_SEQUENCES = (
    "..",
    "@{",
    "\\",
)

_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\]]")

#: Format string used to enumerate branches in a single call.
#:
#: Fields:
#:     %(refname:short) -> branch name without refs/heads/ prefix
#:     %(objectname)    -> full commit SHA
#:     %(HEAD)          -> "*" if the branch is the current one,
#:                         otherwise " "
_BRANCH_LIST_FORMAT = "%(refname:short)%00%(objectname)%00%(HEAD)"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitBranchManager(LocalBranchPort):
    """
    Local branch manager backed by the Git command-line tool.

    The manager is stateless with respect to per-request data. It is
    safe to share across coroutines.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
        default_branch: Optional[str] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()
        self._default_branch = (
            default_branch.strip()
            if isinstance(default_branch, str) and default_branch.strip()
            else None
        )

    # --- Public API (LocalBranchPort) ----------------------------------

    async def create_local_branch(
        self,
        workspace_id: str,
        *,
        branch_name: str,
        from_sha: Optional[str] = None,
        workspace_path: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> BranchRef:
        """
        Create a local branch and check it out.

        Args:
            workspace_id: Workspace identifier (label).
            branch_name: Name of the branch to create.
            from_sha: Starting point. When `None`, the current HEAD
                is used.
            workspace_path: Absolute workspace path.
            force: When True, reset an existing branch to the new
                starting point. When False (default), an existing
                branch raises `ConflictError`.

        Raises:
            ValidationError: If `branch_name` is invalid.
            ConflictError: If the branch already exists and `force`
                is False.
            WorkspaceNotFoundError: If the workspace path is
                missing.
        """
        self._validate_branch_name(branch_name)
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        start_point = from_sha or "HEAD"

        if not force:
            exists = await self.branch_exists(
                workspace_id,
                branch_name,
                workspace_path=target,
            )
            if exists:
                raise ConflictError(
                    f"Branch '{branch_name}' already exists.",
                    details={"branch_name": branch_name},
                )

        # `-B` resets an existing branch to the start point;
        # `-b` fails when the branch already exists.
        flag = "-B" if force else "-b"

        await self._git.run(
            ["checkout", flag, branch_name, start_point],
            cwd=target,
        )

        head_sha = await self._read_head_sha(target)
        return BranchRef(
            name=branch_name,
            head_sha=head_sha,
            is_default=self._is_default_branch(branch_name),
            is_protected=is_protected_local_branch(branch_name),
        )

    async def list_local_branches(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> Sequence[BranchRef]:
        """
        Return every local branch in the workspace.

        The current branch is marked with `is_default=True` when its
        name matches the manager's default branch, and with
        `is_protected=True` when its name is in the protected set.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        raw = await self._git.run(
            ["branch", f"--format={_BRANCH_LIST_FORMAT}"],
            cwd=target,
            check_returncode=False,
        )
        if raw.returncode != 0 and not raw.stdout:
            return ()
        return _parse_branch_list(raw.stdout)

    async def get_current_branch(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> str:
        """
        Return the name of the currently checked-out branch.

        Raises:
            ConflictError: If the workspace is in detached HEAD
                state. The caller MUST handle this case explicitly;
                silently returning an empty string would hide a
                genuine problem.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        branch = await self._read_current_branch(target)
        if branch is None:
            raise ConflictError(
                "Workspace is in detached HEAD state.",
                details={"workspace_id": workspace_id},
            )
        return branch

    async def get_head_sha(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> str:
        """Return the SHA of the current HEAD."""
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        sha = await self._read_head_sha(target)
        if not sha:
            raise NotFoundError(
                "Workspace has no commits yet.",
                details={"workspace_id": workspace_id},
            )
        return sha

    # --- Extended operations -------------------------------------------

    async def checkout_branch(
        self,
        workspace_id: str,
        *,
        branch_name: str,
        workspace_path: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> BranchRef:
        """
        Check out an existing local branch.

        When `force` is False (default), the operation is refused if
        the workspace has uncommitted changes, so that user work is
        not silently discarded.

        Raises:
            ValidationError: If `branch_name` is invalid.
            ConflictError: If the workspace is dirty and `force` is
                False.
            NotFoundError: If the branch does not exist.
        """
        self._validate_branch_name(branch_name)
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        if not force:
            dirty = await self._is_dirty(target)
            if dirty:
                raise ConflictError(
                    "Cannot switch branches with uncommitted changes.",
                    details={"branch_name": branch_name},
                )

        args = ["checkout", branch_name]
        if force:
            args = ["checkout", "-f", branch_name]

        result = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "Checkout failed."
            raise NotFoundError(
                f"Could not check out '{branch_name}': {message}",
                details={"branch_name": branch_name},
            )

        head_sha = await self._read_head_sha(target)
        return BranchRef(
            name=branch_name,
            head_sha=head_sha,
            is_default=self._is_default_branch(branch_name),
            is_protected=is_protected_local_branch(branch_name),
        )

    async def delete_local_branch(
        self,
        workspace_id: str,
        *,
        branch_name: str,
        workspace_path: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> None:
        """
        Delete a local branch.

        When `force` is False (default), the branch is protected or
        unmerged changes cause a refusal. Deleting the currently
        checked-out branch is always rejected.

        Raises:
            ValidationError: If `branch_name` is invalid.
            ConflictError: If the branch is protected, unmerged, or
                currently checked out.
            NotFoundError: If the branch does not exist.
        """
        self._validate_branch_name(branch_name)
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        current = await self._read_current_branch(target)
        if current is not None and current == branch_name:
            raise ConflictError(
                "Cannot delete the currently checked-out branch.",
                details={"branch_name": branch_name},
            )

        if not force and is_protected_local_branch(branch_name):
            raise ConflictError(
                f"Branch '{branch_name}' is protected; pass force=True "
                "to delete it.",
                details={"branch_name": branch_name},
            )

        if not await self.branch_exists(
            workspace_id, branch_name, workspace_path=target
        ):
            raise NotFoundError(
                f"Branch '{branch_name}' does not exist.",
                details={"branch_name": branch_name},
            )

        flag = "-D" if force else "-d"
        result = await self._git.run(
            ["branch", flag, branch_name],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "Branch deletion failed."
            raise ConflictError(
                f"Could not delete '{branch_name}': {message}",
                details={"branch_name": branch_name},
            )

    async def rename_local_branch(
        self,
        workspace_id: str,
        *,
        old_name: str,
        new_name: str,
        workspace_path: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> BranchRef:
        """
        Rename a local branch.

        Renaming a protected branch without `force=True` is
        rejected, mirroring the behavior of `delete_local_branch`.
        """
        self._validate_branch_name(old_name)
        self._validate_branch_name(new_name)
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        if not force and is_protected_local_branch(old_name):
            raise ConflictError(
                f"Branch '{old_name}' is protected; pass force=True to "
                "rename it.",
                details={"old_name": old_name},
            )

        result = await self._git.run(
            ["branch", "-m", old_name, new_name],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "Branch rename failed."
            raise ConflictError(
                f"Could not rename '{old_name}' to '{new_name}': "
                f"{message}",
                details={"old_name": old_name, "new_name": new_name},
            )

        head_sha = await self._read_head_sha(target)
        return BranchRef(
            name=new_name,
            head_sha=head_sha,
            is_default=self._is_default_branch(new_name),
            is_protected=is_protected_local_branch(new_name),
        )

    async def branch_exists(
        self,
        workspace_id: str,
        branch_name: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Return True if a local branch with the given name exists.
        """
        if not branch_name:
            return False
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        # `git show-ref --verify --quiet` exits 0 when the ref exists
        # and 1 when it does not. We bypass the exception and read
        # the return code directly.
        result = await self._git.run(
            [
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch_name}",
            ],
            cwd=target,
            check_returncode=False,
        )
        return result.returncode == 0

    async def get_branch_ref(
        self,
        workspace_id: str,
        branch_name: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> BranchRef:
        """
        Return a `BranchRef` for a single branch.

        Raises:
            NotFoundError: If the branch does not exist.
        """
        self._validate_branch_name(branch_name)
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        result = await self._git.run(
            ["rev-parse", "--verify", f"refs/heads/{branch_name}"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            raise NotFoundError(
                f"Branch '{branch_name}' does not exist.",
                details={"branch_name": branch_name},
            )
        sha = result.stdout.strip() or None
        return BranchRef(
            name=branch_name,
            head_sha=sha,
            is_default=self._is_default_branch(branch_name),
            is_protected=is_protected_local_branch(branch_name),
        )

    async def is_detached_head(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """Return True if the workspace is in detached HEAD state."""
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        return (await self._read_current_branch(target)) is None

    # --- Internal helpers ----------------------------------------------

    def _resolve_workspace_path(
        self,
        workspace_path: Optional[Union[str, Path]],
        workspace_id: str,
    ) -> Path:
        """
        Validate and return the workspace path.

        When `workspace_path` is omitted, a `ValidationError` is
        raised. This keeps the manager stateless with respect to
        workspace ownership: callers MUST supply the path.
        """
        if workspace_path is None:
            raise ValidationError(
                "workspace_path is required for this operation.",
                details={"workspace_id": workspace_id},
            )
        resolved = self._policy.resolve_within_root(
            workspace_path, must_exist=True
        )
        if not resolved.is_dir():
            raise WorkspaceNotFoundError(
                f"Workspace path is not a directory: {resolved}.",
                details={"path": str(resolved)},
            )
        return resolved

    async def _read_current_branch(
        self, target: Path
    ) -> Optional[str]:
        """
        Return the current branch name, or `None` in detached HEAD.
        """
        try:
            result = await self._git.run(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            return None
        name = result.stdout.strip()
        if not name or name == "HEAD":
            return None
        return name

    async def _read_head_sha(self, target: Path) -> Optional[str]:
        """Return the current HEAD SHA, or `None` when unavailable."""
        try:
            result = await self._git.run(
                ["rev-parse", "HEAD"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            return None
        sha = result.stdout.strip()
        return sha or None

    async def _is_dirty(self, target: Path) -> bool:
        """
        Return True when the workspace has uncommitted changes.

        Implemented via `git status --porcelain` to avoid coupling to
        the status reader module. The check is intentionally simple:
        any non-empty output means dirty.
        """
        try:
            result = await self._git.run(
                ["status", "--porcelain"],
                cwd=target,
                check_returncode=False,
            )
        except GitExecutionError:
            # If git itself failed, be conservative and report dirty
            # so that the caller does not silently lose changes.
            return True
        return bool(result.stdout.strip())

    def _is_default_branch(self, branch_name: str) -> bool:
        """
        Return True when `branch_name` matches the configured default
        branch, or when no explicit default is configured and the
        name matches the common defaults.
        """
        if self._default_branch is not None:
            return branch_name == self._default_branch
        return branch_name in ("main", "master")

    # --- Validation ----------------------------------------------------

    @staticmethod
    def _validate_branch_name(name: str) -> None:
        """
        Validate a branch name using the same rules as
        `git check-ref-format`.

        Raises `ValidationError` with a precise message when the name
        is invalid. The check is deliberately conservative: any name
        that could confuse Git or the filesystem is rejected.
        """
        if not is_valid_branch_name(name):
            raise ValidationError(
                f"Invalid branch name: {name!r}.",
                details={"branch_name": name},
            )


# ---------------------------------------------------------------------------
# Branch name validation
# ---------------------------------------------------------------------------


def is_valid_branch_name(name: str) -> bool:
    """
    Return True if `name` is a valid Git branch name.

    Rules (subset of `git check-ref-format`):
        - Non-empty, no leading or trailing whitespace.
        - No leading `-`, `.`, or `/`.
        - No trailing `.`, `/`, or `.lock`.
        - No `..`, `@{`, or `\\`.
        - No control characters, spaces, or `~^:?*[`.
        - Length <= `MAX_BRANCH_NAME_LENGTH`.
    """
    if not name or not isinstance(name, str):
        return False
    # Unicode NFC normalization for stability.
    normalized = unicodedata.normalize("NFC", name)
    if len(normalized) > MAX_BRANCH_NAME_LENGTH:
        return False
    if normalized != normalized.strip():
        return False
    if normalized[0] in "-./":
        return False
    if normalized.endswith(("/", ".")):
        return False
    if normalized.endswith(".lock"):
        return False
    for seq in _FORBIDDEN_SEQUENCES:
        if seq in normalized:
            return False
    if _FORBIDDEN_CHARS.search(normalized):
        return False
    # A branch name must not consist solely of dots or slashes.
    if set(normalized) <= {".", "/"}:
        return False
    return True


def is_protected_local_branch(name: str) -> bool:
    """
    Return True if `name` is a protected local branch.

    Comparison is case-insensitive, and compound names such as
    `release/1.2` are considered protected when their prefix matches
    a protected name.
    """
    if not name:
        return False
    lowered = name.strip().lower()
    if lowered in PROTECTED_LOCAL_BRANCH_NAMES:
        return True
    # `release/*`, `hotfix/*`, `production/*` and similar prefixes.
    prefix = lowered.split("/", 1)[0]
    return prefix in PROTECTED_LOCAL_BRANCH_NAMES


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_branch_list(raw: str) -> Tuple[BranchRef, ...]:
    """
    Parse the NUL-delimited output of `git branch --format=...`.

    Each entry is a triple `name\\0sha\\0head`. Entries without a
    valid triple are skipped. The current branch is returned first so
    that callers can easily present it at the top of a list.
    """
    if not raw:
        return ()

    text = raw
    # Output may be line-separated (one branch per line) or a single
    # buffer. Normalize by splitting on newlines first, then each
    # line on NUL.
    lines = text.splitlines()
    entries: List[BranchRef] = []
    current: Optional[BranchRef] = None

    for line in lines:
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) < 3:
            continue
        name, sha, marker = parts[0], parts[1], parts[2]
        normalized_name = unicodedata.normalize("NFC", name)
        head_sha = sha or None
        is_current = marker.strip() == "*"
        ref = BranchRef(
            name=normalized_name,
            head_sha=head_sha,
            is_default=is_current,
            is_protected=is_protected_local_branch(normalized_name),
        )
        if is_current:
            current = ref
        else:
            entries.append(ref)

    result: List[BranchRef] = []
    if current is not None:
        result.append(current)
    result.extend(entries)
    return tuple(result)


# ---------------------------------------------------------------------------
# Default manager singleton
# ---------------------------------------------------------------------------


_DEFAULT_MANAGER: Optional[GitBranchManager] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_branch_manager() -> GitBranchManager:
    """
    Return the process-wide default `GitBranchManager`, creating it
    on first use.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitBranchManager()
        return _DEFAULT_MANAGER


def set_default_branch_manager(manager: GitBranchManager) -> None:
    """
    Replace the process-wide default branch manager.

    Intended for application startup and for tests.
    """
    if not isinstance(manager, GitBranchManager):
        raise TypeError(
            "set_default_branch_manager expects a GitBranchManager."
        )
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager


def reset_default_branch_manager() -> GitBranchManager:
    """
    Replace the default manager with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = GitBranchManager()
        return _DEFAULT_MANAGER
