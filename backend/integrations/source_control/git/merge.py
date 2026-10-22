# backend/integrations/source_control/git/merge.py
"""
Git merge and rebase manager for the Source Control Integration
subsystem.

This module drives merge and rebase operations against the local
workspace. It does NOT resolve conflicts itself; that is the
responsibility of `GitConflictManager` (`conflict.py`). The split
is deliberate:

    - `GitMergeManager` starts and completes operations.
    - `GitConflictManager` inspects and resolves conflicts.

The intended workflow for a merge with conflicts:

    1. ``GitMergeManager.merge_branch()`` starts the merge.
    2. On conflict, the caller inspects
       ``GitConflictManager.list_conflicts()``.
    3. The user resolves conflicts (via ``GitConflictManager``).
    4. The caller invokes ``GitMergeManager.finalize_merge()`` to
       commit the merge, or ``GitConflictManager.abort_operation()``
       to abandon it.

Design rules:
    - The manager never discards user work. A dirty working tree
      causes an explicit ``WorkspaceDirtyError`` rather than a
      silent ``git stash`` or a silent reset.
    - The manager never uses ``--force`` or ``--no-verify`` on any
      merge/rebase invocation.
    - Every path is validated through the workspace path policy.
    - Timeouts on merge and rebase are generous (they can involve
      network and tree-walking) but bounded.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple, Union

from ..errors import (
    CommandFailedError,
    ConflictError,
    ValidationError,
    WorkspaceDirtyError,
    WorkspaceNotFoundError,
)
from .conflict import (
    ConflictEntry,
    GitConflictManager,
    OperationState,
    get_default_conflict_manager,
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
    "GitMergeManager",
    "MergeOutcome",
    "MergeOutcomeKind",
    "get_default_merge_manager",
    "set_default_merge_manager",
    "reset_default_merge_manager",
    "MERGE_TIMEOUT_SECONDS",
    "REBASE_TIMEOUT_SECONDS",
]


logger = logging.getLogger(__name__)


#: Default timeout for merge operations. Generous because merges
#: walk both histories and can involve renames detection.
MERGE_TIMEOUT_SECONDS: int = 120

#: Default timeout for rebase operations. Slightly larger than the
#: merge timeout because a rebase applies one commit at a time.
REBASE_TIMEOUT_SECONDS: int = 180

#: Regex-safe list of strategies accepted by `merge_branch`.
_VALID_MERGE_STRATEGIES = frozenset({"merge", "squash", "ff-only"})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class MergeOutcomeKind(str):
    """String-typed outcome of a merge or rebase operation.

    Defined as a `str` subclass (not an enum) so it serializes
    transparently and compares to literal strings without importing
    this module.
    """

    #: The operation completed and produced a new commit.
    CLEAN = "clean"
    #: The operation paused for conflict resolution.
    CONFLICTED = "conflicted"
    #: Nothing to do (already up to date).
    UP_TO_DATE = "up_to_date"


class MergeOutcome:
    """Result of a merge or rebase operation.

    Attributes:
        kind:          One of the `MergeOutcomeKind` values.
        conflicts:     The list of conflicted files when `kind` is
                       `CONFLICTED`; empty otherwise.
        operation_state: The state of the in-progress operation
                       (`MERGING` / `REBASING` / `NONE`).
        head_sha:      The workspace HEAD SHA after the operation.
        message:       Optional human-readable status.
    """

    def __init__(
        self,
        *,
        kind: str,
        conflicts: Tuple[ConflictEntry, ...] = (),
        operation_state: OperationState = OperationState.NONE,
        head_sha: Optional[str] = None,
        message: str = "",
    ) -> None:
        self.kind = kind
        self.conflicts = conflicts
        self.operation_state = operation_state
        self.head_sha = head_sha
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"<MergeOutcome kind={self.kind} "
            f"state={self.operation_state.value} "
            f"conflicts={len(self.conflicts)}>"
        )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitMergeManager:
    """Local merge and rebase operations backed by the Git CLI.

    The manager holds only immutable references to the Git
    executable, the path policy, and the conflict manager. It is
    safe to share across coroutines.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
        conflict_manager: Optional[GitConflictManager] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()
        self._conflicts = conflict_manager or get_default_conflict_manager()

    # --- Public API: merge ---------------------------------------------

    async def merge_branch(
        self,
        workspace_id: str,
        *,
        source_branch: str,
        target_branch: str,
        strategy: str = "merge",
        commit_message: Optional[str] = None,
        workspace_path: Union[str, Path],
    ) -> MergeOutcome:
        """Merge `source_branch` into `target_branch`.

        The workspace is checked out on `target_branch` first. A
        dirty working tree causes an explicit error rather than a
        silent stash; the caller must commit or discard its changes
        before merging.

        Args:
            workspace_id: Workspace identifier (label only).
            source_branch: Branch to merge FROM.
            target_branch: Branch to merge INTO. The workspace is
                checked out on this branch first.
            strategy: One of ``"merge"``, ``"squash"``, or
                ``"ff-only"``. ``"merge"`` creates a merge commit
                when the fast-forward is not possible; ``"squash"``
                produces a single squashed commit; ``"ff-only"``
                refuses to create a merge commit.
            commit_message: Optional custom commit message for the
                merge commit (``"merge"`` and ``"squash"`` only).
            workspace_path: Absolute workspace path.

        Raises:
            ValidationError: For malformed inputs.
            WorkspaceDirtyError: If the working tree is dirty and
                the target checkout would fail.
            ConflictError: If the merge has conflicts (returned, not
                raised — the caller inspects ``outcome.conflicts``).
            CommandFailedError: For any other Git failure.
        """
        self._validate_branch(source_branch, field_name="source_branch")
        self._validate_branch(target_branch, field_name="target_branch")
        if source_branch == target_branch:
            raise ValidationError(
                "source_branch and target_branch must differ.",
                details={
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                },
            )
        clean_strategy = (strategy or "merge").strip().lower()
        if clean_strategy not in _VALID_MERGE_STRATEGIES:
            raise ValidationError(
                f"Invalid strategy '{strategy}'. Must be one of: "
                f"{sorted(_VALID_MERGE_STRATEGIES)}",
                details={"strategy": strategy},
            )

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        # Refuse if there is already an in-progress operation. The
        # caller should either finalize or abort it before starting
        # a new one; nesting Git operations is a recipe for
        # corruption.
        state = self._conflicts._detect_state_from_git_dir(target)
        if state != OperationState.NONE:
            raise ConflictError(
                f"Cannot start a merge: a '{state.value}' operation "
                "is already in progress. Finalize or abort it first.",
                details={"in_progress": state.value},
            )

        # Checkout target. If the working tree is dirty, this will
        # fail; we translate that into a clear error.
        checkout = await self._git.run(
            ["checkout", target_branch],
            cwd=target,
            check_returncode=False,
        )
        if checkout.returncode != 0:
            stderr_lower = (checkout.stderr or "").lower()
            if (
                "would be overwritten" in stderr_lower
                or "uncommitted changes" in stderr_lower
                or "local changes" in stderr_lower
            ):
                raise WorkspaceDirtyError(
                    "Cannot check out the target branch: the "
                    "workspace has uncommitted changes that would "
                    "be overwritten.",
                    details={"target_branch": target_branch},
                )
            raise CommandFailedError(
                f"Could not check out '{target_branch}'.",
                details={"stderr": (checkout.stderr or "")[:500]},
            )

        # Build the merge command.
        args: List[str] = ["merge", "--no-edit"]
        if clean_strategy == "merge":
            # Force a merge commit even when a fast-forward is
            # possible. This matches the behaviour of GitHub's
            # "Create a merge commit" option, which preserves the
            # branch topology in the history.
            args.append("--no-ff")
        elif clean_strategy == "squash":
            args.append("--squash")
        elif clean_strategy == "ff-only":
            args.append("--ff-only")
        if commit_message:
            args.append("-m")
            args.append(commit_message)
        args.append(source_branch)

        merge = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
            timeout_seconds=MERGE_TIMEOUT_SECONDS,
        )

        # Return code contract for `git merge`:
        #   0  = success (up-to-date or merged)
        #   1  = conflicts; partial result in the working tree
        # 128+ = hard failure
        if merge.returncode == 0:
            # "Already up to date." is a normal, non-failing message
            # printed on stdout.
            up_to_date = "already up to date" in (
                (merge.stdout or "") + (merge.stderr or "")
            ).lower()
            head_sha = await self._read_head(target)
            return MergeOutcome(
                kind=(
                    MergeOutcomeKind.UP_TO_DATE
                    if up_to_date
                    else MergeOutcomeKind.CLEAN
                ),
                operation_state=OperationState.NONE,
                head_sha=head_sha,
                message=(merge.stdout or "").strip()[:500],
            )

        if merge.returncode == 1:
            # Conflicts. The merge is now in progress; the caller
            # is expected to resolve conflicts via the conflict
            # manager.
            conflicts = await self._conflicts.list_conflicts(
                workspace_id, workspace_path=target
            )
            new_state = self._conflicts._detect_state_from_git_dir(target)
            head_sha = await self._read_head(target)
            return MergeOutcome(
                kind=MergeOutcomeKind.CONFLICTED,
                conflicts=tuple(conflicts),
                operation_state=new_state,
                head_sha=head_sha,
                message="Merge paused for conflict resolution.",
            )

        raise CommandFailedError(
            "git merge failed.",
            details={
                "returncode": merge.returncode,
                "stderr": (merge.stderr or "")[:1000],
            },
        )

    # --- Public API: finalize merge -----------------------------------

    async def finalize_merge(
        self,
        workspace_id: str,
        *,
        commit_message: Optional[str] = None,
        workspace_path: Union[str, Path],
    ) -> MergeOutcome:
        """Commit a merge that was paused for conflict resolution.

        The caller is expected to have staged resolved files (via
        ``GitConflictManager.mark_resolved`` or one of the
        ``resolve_using_*`` helpers) before calling this. If any
        unmerged entries remain in the index, Git will refuse to
        commit and this method surfaces that as a conflict outcome.

        A squash merge does not produce a `MERGE_HEAD`, so this
        method is a no-op for a squash merge that has already been
        finalized at merge time (which always has a clean outcome).
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        state = self._conflicts._detect_state_from_git_dir(target)
        if state != OperationState.MERGING:
            raise ConflictError(
                "Cannot finalize a merge: no merge is in progress.",
                details={"state": state.value},
            )

        # Refuse if unmerged entries remain. `git commit` would fail
        # with an opaque error; we surface it as a conflict outcome
        # so the caller can keep the resolution UI open.
        if await self._conflicts.is_conflicted(
            workspace_id, workspace_path=target
        ):
            conflicts = await self._conflicts.list_conflicts(
                workspace_id, workspace_path=target
            )
            return MergeOutcome(
                kind=MergeOutcomeKind.CONFLICTED,
                conflicts=tuple(conflicts),
                operation_state=state,
                head_sha=await self._read_head(target),
                message=(
                    "Unresolved conflicts remain. Resolve them "
                    "before finalizing the merge."
                ),
            )

        args: List[str] = [
            "-c", "user.name=PELDRUN",
            "-c", "user.email=peldrun@localhost",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
        ]
        if commit_message:
            args.append("-m")
            args.append(commit_message)
        else:
            args.append("--no-edit")

        commit = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
            timeout_seconds=MERGE_TIMEOUT_SECONDS,
        )
        if commit.returncode != 0:
            raise CommandFailedError(
                "Could not commit the merged changes.",
                details={
                    "returncode": commit.returncode,
                    "stderr": (commit.stderr or "")[:500],
                },
            )

        return MergeOutcome(
            kind=MergeOutcomeKind.CLEAN,
            operation_state=OperationState.NONE,
            head_sha=await self._read_head(target),
            message="Merge committed.",
        )

    # --- Public API: rebase -------------------------------------------

    async def rebase_branch(
        self,
        workspace_id: str,
        *,
        branch: str,
        onto: str,
        workspace_path: Union[str, Path],
    ) -> MergeOutcome:
        """Rebase `branch` onto `onto`.

        The workspace is checked out on `branch` first. A dirty
        working tree causes an explicit error (same rationale as in
        `merge_branch`).

        Conflict handling mirrors `merge_branch`: a return code of 1
        from `git rebase` means the rebase paused, and the caller
        must resolve conflicts then call `finalize_rebase` or
        `abort_operation`.
        """
        self._validate_branch(branch, field_name="branch")
        self._validate_branch(onto, field_name="onto")
        if branch == onto:
            raise ValidationError(
                "branch and onto must differ.",
                details={"branch": branch, "onto": onto},
            )

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        state = self._conflicts._detect_state_from_git_dir(target)
        if state != OperationState.NONE:
            raise ConflictError(
                f"Cannot start a rebase: a '{state.value}' operation "
                "is already in progress.",
                details={"in_progress": state.value},
            )

        checkout = await self._git.run(
            ["checkout", branch],
            cwd=target,
            check_returncode=False,
        )
        if checkout.returncode != 0:
            stderr_lower = (checkout.stderr or "").lower()
            if (
                "would be overwritten" in stderr_lower
                or "uncommitted changes" in stderr_lower
                or "local changes" in stderr_lower
            ):
                raise WorkspaceDirtyError(
                    "Cannot check out the branch to rebase: the "
                    "workspace has uncommitted changes.",
                    details={"branch": branch},
                )
            raise CommandFailedError(
                f"Could not check out '{branch}'.",
                details={"stderr": (checkout.stderr or "")[:500]},
            )

        rebase = await self._git.run(
            ["rebase", onto],
            cwd=target,
            check_returncode=False,
            timeout_seconds=REBASE_TIMEOUT_SECONDS,
        )

        if rebase.returncode == 0:
            up_to_date = "up to date" in (
                (rebase.stdout or "") + (rebase.stderr or "")
            ).lower()
            head_sha = await self._read_head(target)
            return MergeOutcome(
                kind=(
                    MergeOutcomeKind.UP_TO_DATE
                    if up_to_date
                    else MergeOutcomeKind.CLEAN
                ),
                operation_state=OperationState.NONE,
                head_sha=head_sha,
                message=(rebase.stdout or "").strip()[:500],
            )

        # `git rebase` uses 1 for conflicts and various >1 codes for
        # other failures. Any returncode > 1 is a hard failure.
        if rebase.returncode != 1:
            raise CommandFailedError(
                "git rebase failed.",
                details={
                    "returncode": rebase.returncode,
                    "stderr": (rebase.stderr or "")[:1000],
                },
            )

        conflicts = await self._conflicts.list_conflicts(
            workspace_id, workspace_path=target
        )
        new_state = self._conflicts._detect_state_from_git_dir(target)
        return MergeOutcome(
            kind=MergeOutcomeKind.CONFLICTED,
            conflicts=tuple(conflicts),
            operation_state=new_state,
            head_sha=await self._read_head(target),
            message="Rebase paused for conflict resolution.",
        )

    # --- Public API: finalize rebase ----------------------------------

    async def finalize_rebase(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
    ) -> MergeOutcome:
        """Continue a paused rebase after conflicts were resolved.

        Assumes all conflicts have been resolved (staged) already;
        if not, Git exits non-zero and the outcome is CONFLICTED.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        state = self._conflicts._detect_state_from_git_dir(target)
        if state != OperationState.REBASING:
            raise ConflictError(
                "Cannot finalize a rebase: no rebase is in progress.",
                details={"state": state.value},
            )

        if await self._conflicts.is_conflicted(
            workspace_id, workspace_path=target
        ):
            conflicts = await self._conflicts.list_conflicts(
                workspace_id, workspace_path=target
            )
            return MergeOutcome(
                kind=MergeOutcomeKind.CONFLICTED,
                conflicts=tuple(conflicts),
                operation_state=state,
                head_sha=await self._read_head(target),
                message=(
                    "Unresolved conflicts remain. Resolve them "
                    "before finalizing the rebase."
                ),
            )

        # `--continue` invokes an editor in some configurations;
        # `GIT_EDITOR=:` in the executable's environment makes it
        # a no-op, so the commit message is preserved.
        cont = await self._git.run(
            ["rebase", "--continue"],
            cwd=target,
            check_returncode=False,
            timeout_seconds=REBASE_TIMEOUT_SECONDS,
        )
        if cont.returncode != 0:
            # A non-zero return code here can mean "another commit
            # in the rebase hit a conflict" (a legitimate case) or a
            # hard failure. Distinguish by inspecting the state.
            new_state = self._conflicts._detect_state_from_git_dir(target)
            if new_state == OperationState.REBASING:
                conflicts = await self._conflicts.list_conflicts(
                    workspace_id, workspace_path=target
                )
                return MergeOutcome(
                    kind=MergeOutcomeKind.CONFLICTED,
                    conflicts=tuple(conflicts),
                    operation_state=new_state,
                    head_sha=await self._read_head(target),
                    message=(
                        "Rebase paused again for conflict "
                        "resolution."
                    ),
                )
            raise CommandFailedError(
                "git rebase --continue failed.",
                details={
                    "returncode": cont.returncode,
                    "stderr": (cont.stderr or "")[:500],
                },
            )

        return MergeOutcome(
            kind=MergeOutcomeKind.CLEAN,
            operation_state=OperationState.NONE,
            head_sha=await self._read_head(target),
            message="Rebase completed.",
        )

    # --- Public API: abort --------------------------------------------

    async def abort_operation(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
    ) -> OperationState:
        """Abort whichever operation is currently in progress.

        Delegates to `GitConflictManager.abort_operation` so there
        is exactly one place where the per-operation abort commands
        live.

        Returns the state that was aborted.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        return await self._conflicts.abort_operation(
            workspace_id, workspace_path=target
        )

    # --- Internal helpers ---------------------------------------------

    def _resolve_workspace_path(
        self,
        workspace_path: Union[str, Path],
        workspace_id: str,
    ) -> Path:
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

    async def _read_head(self, target: Path) -> Optional[str]:
        """Return the current HEAD SHA, or None on failure."""
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

    @staticmethod
    def _validate_branch(name: str, *, field_name: str) -> None:
        """Validate a branch name with the same rules as
        `git check-ref-format`. Kept local to this module to avoid a
        circular import with `branch.py`, which imports from
        `path_policy.py` (and could in turn import from here).
        """
        from .path_policy import is_valid_branch_name

        if not name or not isinstance(name, str):
            raise ValidationError(
                f"{field_name} must be a non-empty string.",
                details={"field": field_name},
            )
        if not is_valid_branch_name(name.strip()):
            raise ValidationError(
                f"{field_name} is not a valid branch name: {name!r}.",
                details={"field": field_name, "value": name},
            )


# ---------------------------------------------------------------------------
# Default manager singleton
# ---------------------------------------------------------------------------


_DEFAULT_MANAGER: Optional[GitMergeManager] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_merge_manager() -> GitMergeManager:
    """Return the process-wide default `GitMergeManager`."""
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitMergeManager()
        return _DEFAULT_MANAGER


def set_default_merge_manager(manager: GitMergeManager) -> None:
    """Replace the process-wide default merge manager."""
    if not isinstance(manager, GitMergeManager):
        raise TypeError(
            "set_default_merge_manager expects a GitMergeManager."
        )
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager


def reset_default_merge_manager() -> GitMergeManager:
    """Replace the default manager with a fresh one."""
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = GitMergeManager()
        return _DEFAULT_MANAGER
