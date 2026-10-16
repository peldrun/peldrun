# backend/integrations/source_control/git/conflict.py
"""
Merge conflict detection and resolution for the Source Control
Integration subsystem.

This module is the ONLY place in the subsystem where in-progress
merge, rebase, cherry-pick, and revert operations are inspected or
resolved. It provides:

    - `detect_operation_state` : identify which Git operation is in
      progress, if any.
    - `list_conflicts`         : enumerate conflicted files.
    - `read_stage_content`     : read a specific stage (base, ours,
      theirs) of a conflicted file.
    - `resolve_using_ours`     : bulk-resolve conflicts by choosing
      the local side.
    - `resolve_using_theirs`   : bulk-resolve conflicts by choosing
      the incoming side.
    - `mark_resolved`          : mark manually edited files as
      resolved.
    - `abort_operation`        : abort the in-progress operation.

Design rules:
    - The manager never starts a merge, rebase, cherry-pick, or
      revert. Those operations are driven by the user or by an agent
      through other means. The manager's sole responsibility is to
      inspect and resolve.
    - `--ours` and `--theirs` are used ONLY when an operation is in
      progress. Outside that context, Git's behavior for those flags
      is surprising; the manager refuses rather than surprises the
      caller.
    - The manager never touches the working tree except through
      `git checkout --ours/--theirs` and `git add`.
    - Every path is validated through the workspace path policy
      before any Git invocation.
    - The manager is stateless with respect to per-request data and
      is safe to share across coroutines.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..errors import (
    CommandFailedError,
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
    "GitConflictManager",
    "ConflictEntry",
    "ConflictStage",
    "OperationState",
    "ResolutionSide",
    "get_default_conflict_manager",
    "set_default_conflict_manager",
    "reset_default_conflict_manager",
    "MAX_CONFLICT_FILES",
    "MAX_STAGE_CONTENT_BYTES",
]


logger = logging.getLogger(__name__)


#: Maximum number of conflicted files returned by `list_conflicts`.
#: A repository with more conflicts than this is almost certainly the
#: result of a script gone wrong; the caller should not have to
#: allocate unbounded memory for it.
MAX_CONFLICT_FILES: int = 5_000

#: Maximum bytes read for a single stage of a conflicted file.
MAX_STAGE_CONTENT_BYTES: int = 1 * 1024 * 1024  # 1 MiB

#: Default timeout for abort operations.
_ABORT_TIMEOUT_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OperationState(str, Enum):
    """
    State of an in-progress operation.

    Git records the operation in files under `.git`. The manager
    inspects those files directly rather than invoking Git.
    """

    NONE = "none"
    MERGING = "merging"
    REBASING = "rebasing"
    CHERRY_PICKING = "cherry_picking"
    REVERTING = "reverting"


class ConflictStage(int, Enum):
    """
    The three stages Git records for a conflicted file.

    Values match Git's internal index stages.
    """

    BASE = 1
    OURS = 2
    THEIRS = 3


class ResolutionSide(str, Enum):
    """Which side of a conflict to keep."""

    OURS = "ours"
    THEIRS = "theirs"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictEntry:
    """
    A single conflicted file.

    `has_base`, `has_ours`, and `has_theirs` indicate which of the
    three stages are present. A delete/modify conflict, for example,
    has `has_base=True`, `has_ours=True`, `has_theirs=False` (or the
    reverse). The caller can use these flags to decide which
    resolution is appropriate.
    """

    path: str
    has_base: bool
    has_ours: bool
    has_theirs: bool
    base_sha: Optional[str] = None
    ours_sha: Optional[str] = None
    theirs_sha: Optional[str] = None
    mode: Optional[str] = None

    @property
    def is_delete_modify(self) -> bool:
        """
        Return True when exactly one of `ours` or `theirs` is
        missing, which is the signature of a delete/modify conflict.
        """
        return (self.has_ours != self.has_theirs) and not (
            self.has_ours and self.has_theirs
        )

    @property
    def is_add_add(self) -> bool:
        """
        Return True when both sides added a file and no base exists.
        """
        return self.has_ours and self.has_theirs and not self.has_base

    def to_dict(self) -> Dict[str, object]:
        """Serialize for the UI and audit log (no secrets)."""
        return {
            "path": self.path,
            "has_base": self.has_base,
            "has_ours": self.has_ours,
            "has_theirs": self.has_theirs,
            "is_delete_modify": self.is_delete_modify,
            "is_add_add": self.is_add_add,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitConflictManager:
    """
    Inspect and resolve in-progress merge conflicts.

    The manager holds only immutable references to the Git executable
    and the path policy. It is safe to share across coroutines.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()

    # --- Detection ------------------------------------------------------

    async def detect_operation_state(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> OperationState:
        """
        Return the in-progress operation state, or `NONE`.

        The check inspects the `.git` directory directly. If the
        directory is not a Git repository, `NONE` is returned.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        return self._detect_state_from_git_dir(target)

    async def is_conflicted(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Return True if the workspace has any unresolved conflicts.

        Cheaper than `list_conflicts`: uses `git ls-files -u` and
        short-circuits on the first line.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        result = await self._git.run(
            ["ls-files", "-u", "-z"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip(b"\x00").strip())

    async def list_conflicts(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> Sequence[ConflictEntry]:
        """
        Return every conflicted file in the workspace.

        The result is sorted by path, so callers can rely on a stable
        order across invocations.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        result = await self._git.run(
            ["ls-files", "-u", "-z"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 or not result.stdout:
            return ()
        return _parse_ls_files_u(result.stdout)

    # --- Reading stage content -----------------------------------------

    async def read_stage_content(
        self,
        workspace_id: str,
        *,
        path: str,
        stage: ConflictStage,
        workspace_path: Optional[Union[str, Path]] = None,
        max_bytes: int = MAX_STAGE_CONTENT_BYTES,
    ) -> str:
        """
        Read one stage of a conflicted file.

        Stages are selected via `ConflictStage`. The content is
        returned as UTF-8 text with replacement for invalid
        sequences. Content larger than `max_bytes` is truncated with
        an explicit marker.

        Raises:
            ValidationError: If inputs are malformed.
            NotFoundError: If the stage does not exist.
        """
        if not path:
            raise ValidationError("path must not be empty.")
        if max_bytes <= 0:
            raise ValidationError("max_bytes must be positive.")

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        # Validate the path against the workspace root. The path is
        # used as a Git pathspec, so it must be relative; we resolve
        # it against the root to catch traversal attempts.
        resolved_path = self._policy.resolve_within_root(
            target / path, must_exist=False
        )
        relative = resolved_path.relative_to(target).as_posix()

        # `git show :<stage>:<path>` reads the index entry.
        result = await self._git.run(
            ["show", f":{int(stage)}:{relative}"],
            cwd=target,
            check_returncode=False,
            timeout_seconds=30,
        )
        if result.returncode != 0:
            raise NotFoundError(
                f"Stage {int(stage)} is not present for '{path}'.",
                details={"path": path, "stage": int(stage)},
            )

        text = result.stdout
        if len(text.encode("utf-8", errors="replace")) <= max_bytes:
            return text
        encoded = text.encode("utf-8", errors="replace")
        decoded = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return decoded + "\n[... content truncated ...]\n"

    # --- Resolution -----------------------------------------------------

    async def resolve_using_ours(
        self,
        workspace_id: str,
        *,
        paths: Sequence[str],
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> int:
        """
        Resolve conflicts by keeping the local (`ours`) version.

        For each path, the manager runs `git checkout --ours -- <path>`
        followed by `git add -- <path>`. The number of files
        successfully resolved is returned.

        Raises:
            ValidationError: If no paths are provided.
            ConflictError: If no operation is in progress.
        """
        return await self._bulk_resolve(
            workspace_id=workspace_id,
            paths=paths,
            side=ResolutionSide.OURS,
            workspace_path=workspace_path,
        )

    async def resolve_using_theirs(
        self,
        workspace_id: str,
        *,
        paths: Sequence[str],
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> int:
        """
        Resolve conflicts by keeping the incoming (`theirs`) version.

        See `resolve_using_ours` for semantics.
        """
        return await self._bulk_resolve(
            workspace_id=workspace_id,
            paths=paths,
            side=ResolutionSide.THEIRS,
            workspace_path=workspace_path,
        )

    async def resolve_file_side(
        self,
        workspace_id: str,
        *,
        path: str,
        side: ResolutionSide,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Resolve a single file by choosing one side.

        This is a thin wrapper around `resolve_using_ours` and
        `resolve_using_theirs` for callers that operate on one file
        at a time.
        """
        await self._bulk_resolve(
            workspace_id=workspace_id,
            paths=[path],
            side=side,
            workspace_path=workspace_path,
        )

    async def mark_resolved(
        self,
        workspace_id: str,
        *,
        paths: Sequence[str],
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Mark files as resolved by staging them.

        Use this after manually editing conflicted files. The manager
        runs `git add` for each path after validating it.

        Raises:
            ValidationError: If `paths` is empty.
        """
        if not paths:
            raise ValidationError("paths must not be empty.")

        target = self._resolve_workspace_path(workspace_path, workspace_id)
        validated = self._validate_paths(target, paths)

        await self._git.run(
            ["add", "--", *validated],
            cwd=target,
            timeout_seconds=60,
        )

    # --- Abort ----------------------------------------------------------

    async def abort_operation(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
        timeout_seconds: int = _ABORT_TIMEOUT_SECONDS,
    ) -> OperationState:
        """
        Abort the in-progress operation.

        Detects the operation state and runs the corresponding abort
        command. Returns the state that was aborted.

        Raises:
            ValidationError: If no operation is in progress.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        state = self._detect_state_from_git_dir(target)

        if state == OperationState.NONE:
            raise ValidationError(
                "No in-progress operation to abort.",
                details={"workspace_id": workspace_id},
            )

        command = _abort_command_for(state)
        result = await self._git.run(
            command,
            cwd=target,
            check_returncode=False,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise CommandFailedError(
                f"Failed to abort {state.value} operation.",
                details={
                    "state": state.value,
                    "stderr": result.stderr[:500],
                },
            )
        return state

    # --- Internal: bulk resolution -------------------------------------

    async def _bulk_resolve(
        self,
        *,
        workspace_id: str,
        paths: Sequence[str],
        side: ResolutionSide,
        workspace_path: Optional[Union[str, Path]],
    ) -> int:
        """
        Resolve a set of paths by choosing one side.

        Runs a single `git checkout --ours/--theirs -- p1 p2 ...`
        followed by a single `git add -- p1 p2 ...`. Using one
        invocation per command minimizes overhead and keeps the
        working tree consistent throughout.
        """
        if not paths:
            raise ValidationError("paths must not be empty.")

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        # Refuse to use `--ours/--theirs` when no operation is in
        # progress. Outside that context, Git's behavior is not what
        # callers expect.
        state = self._detect_state_from_git_dir(target)
        if state == OperationState.NONE:
            raise ConflictError(
                "Cannot use --ours/--theirs: no operation is in "
                "progress.",
                details={"workspace_id": workspace_id},
            )

        validated = self._validate_paths(target, paths)
        flag = f"--{side.value}"

        # First, take the chosen side.
        checkout_result = await self._git.run(
            ["checkout", flag, "--", *validated],
            cwd=target,
            check_returncode=False,
            timeout_seconds=60,
        )
        if checkout_result.returncode != 0:
            raise CommandFailedError(
                f"Failed to apply {side.value} side to conflicts.",
                details={
                    "paths": list(validated),
                    "stderr": checkout_result.stderr[:500],
                },
            )

        # Then stage the result to mark the file resolved.
        add_result = await self._git.run(
            ["add", "--", *validated],
            cwd=target,
            check_returncode=False,
            timeout_seconds=60,
        )
        if add_result.returncode != 0:
            raise CommandFailedError(
                "Failed to stage resolved files.",
                details={
                    "paths": list(validated),
                    "stderr": add_result.stderr[:500],
                },
            )

        return len(validated)

    # --- Internal: paths and workspace --------------------------------

    def _resolve_workspace_path(
        self,
        workspace_path: Optional[Union[str, Path]],
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

    def _validate_paths(
        self, target: Path, paths: Sequence[str]
    ) -> List[str]:
        """
        Validate a sequence of workspace-relative paths.

        Returns the normalized POSIX representation of each path.
        Raises `PathTraversalError` for any path that escapes the
        workspace root.
        """
        validated: List[str] = []
        for raw in paths:
            if not raw or not isinstance(raw, str):
                raise ValidationError("path entries must be non-empty strings.")
            resolved = self._policy.resolve_within_root(
                target / raw, must_exist=False
            )
            relative = resolved.relative_to(target)
            validated.append(relative.as_posix())
        return validated

    # --- Internal: state detection -------------------------------------

    @staticmethod
    def _detect_state_from_git_dir(target: Path) -> OperationState:
        """
        Read the `.git` directory to determine the current operation.

        The order of checks matters: some operations create more than
        one marker file, and a rebase takes precedence over a merge
        because rebase uses an underlying merge internally.
        """
        git_dir = target / ".git"

        # Worktrees store their metadata under `.git/worktrees/...`.
        # If `.git` is a file, follow the pointer.
        if git_dir.is_file():
            try:
                content = git_dir.read_text(encoding="utf-8").strip()
            except OSError:
                return OperationState.NONE
            if content.startswith("gitdir:"):
                pointer = content[len("gitdir:"):].strip()
                candidate = Path(pointer)
                if not candidate.is_absolute():
                    candidate = (target / candidate).resolve()
                git_dir = candidate

        if not git_dir.is_dir():
            return OperationState.NONE

        # Rebase takes precedence: both the interactive and the
        # standard rebase create their own directories.
        if (git_dir / "rebase-merge").is_dir():
            return OperationState.REBASING
        if (git_dir / "rebase-apply").is_dir():
            return OperationState.REBASING

        if (git_dir / "CHERRY_PICK_HEAD").exists():
            return OperationState.CHERRY_PICKING

        if (git_dir / "REVERT_HEAD").exists():
            return OperationState.REVERTING

        if (git_dir / "MERGE_HEAD").exists():
            return OperationState.MERGING

        return OperationState.NONE


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


#: `git ls-files -u -z` emits records of the form:
#:     `<mode> <sha> <stage>\t<path>\0`
_LS_FILES_U = re.compile(
    r"^(?P<mode>[0-9a-f]+)\s+(?P<sha>[0-9a-f]+)\s+(?P<stage>[0-9]+)\t(?P<path>.+)$"
)


def _parse_ls_files_u(raw: bytes) -> Tuple[ConflictEntry, ...]:
    """
    Parse the output of `git ls-files -u -z`.

    Groups the three stage records per path into a single
    `ConflictEntry`. Paths are sorted so that the returned order is
    stable.
    """
    if not raw:
        return ()

    text = raw.decode("utf-8", errors="replace")
    records = text.split("\x00")

    by_path: Dict[str, Dict[str, object]] = {}
    order: List[str] = []

    for record in records:
        record = record.strip("\n")
        if not record:
            continue
        match = _LS_FILES_U.match(record)
        if not match:
            continue

        mode = match.group("mode")
        sha = match.group("sha")
        try:
            stage = int(match.group("stage"))
        except ValueError:
            continue
        path = unicodedata.normalize(
            "NFC", match.group("path")
        ).replace("\\", "/")

        entry = by_path.get(path)
        if entry is None:
            entry = {
                "mode": mode,
                "has_base": False,
                "has_ours": False,
                "has_theirs": False,
                "base_sha": None,
                "ours_sha": None,
                "theirs_sha": None,
            }
            by_path[path] = entry
            order.append(path)

        if stage == int(ConflictStage.BASE):
            entry["has_base"] = True
            entry["base_sha"] = sha
        elif stage == int(ConflictStage.OURS):
            entry["has_ours"] = True
            entry["ours_sha"] = sha
        elif stage == int(ConflictStage.THEIRS):
            entry["has_theirs"] = True
            entry["theirs_sha"] = sha

        if len(order) >= MAX_CONFLICT_FILES:
            break

    order.sort()
    result: List[ConflictEntry] = []
    for path in order:
        entry = by_path[path]
        result.append(
            ConflictEntry(
                path=path,
                has_base=bool(entry["has_base"]),
                has_ours=bool(entry["has_ours"]),
                has_theirs=bool(entry["has_theirs"]),
                base_sha=entry["base_sha"],
                ours_sha=entry["ours_sha"],
                theirs_sha=entry["theirs_sha"],
                mode=entry["mode"],
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Abort commands
# ---------------------------------------------------------------------------


def _abort_command_for(state: OperationState) -> List[str]:
    """
    Return the Git command that aborts the given operation.

    Raises `ValidationError` for `NONE`; callers are expected to
    check the state first.
    """
    if state == OperationState.MERGING:
        return ["merge", "--abort"]
    if state == OperationState.REBASING:
        return ["rebase", "--abort"]
    if state == OperationState.CHERRY_PICKING:
        return ["cherry-pick", "--abort"]
    if state == OperationState.REVERTING:
        return ["revert", "--abort"]
    raise ValidationError(
        "No in-progress operation to abort.",
        details={"state": state.value},
    )


# ---------------------------------------------------------------------------
# Default manager singleton
# ---------------------------------------------------------------------------


_DEFAULT_MANAGER: Optional[GitConflictManager] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_conflict_manager() -> GitConflictManager:
    """
    Return the process-wide default `GitConflictManager`, creating
    it on first use.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitConflictManager()
        return _DEFAULT_MANAGER


def set_default_conflict_manager(manager: GitConflictManager) -> None:
    """
    Replace the process-wide default conflict manager.

    Intended for application startup and for tests.
    """
    if not isinstance(manager, GitConflictManager):
        raise TypeError(
            "set_default_conflict_manager expects a GitConflictManager."
        )
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager


def reset_default_conflict_manager() -> GitConflictManager:
    """
    Replace the default manager with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = GitConflictManager()
        return _DEFAULT_MANAGER
