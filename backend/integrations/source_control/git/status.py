# backend/integrations/source_control/git/status.py
"""
Git status reader for the Source Control Integration subsystem.

This module converts raw `git status` output into the
provider-neutral `LocalGitStatus` value object defined in
`ports.repository`. It is the ONLY place in the subsystem where the
porcelain output format is parsed.

Supported operations:

    - `read_status`      : full status snapshot.
    - `read_conflicts`   : list of paths with merge conflicts.
    - `read_untracked`   : list of untracked paths.
    - `read_modified`    : list of tracked paths with modifications.
    - `is_clean`         : fast check without building the full
                           snapshot.

Design rules:
    - All file paths returned by this module are normalized to
      forward slashes and NFC-encoded, matching Git's internal
      representation.
    - The reader never raises for a dirty workspace; dirty is a
      valid state. It raises only when the workspace cannot be read
      (unknown path, missing repository).
    - The reader is stateless: instances may be shared across
      coroutines without synchronization.
    - The reader MUST NOT modify the repository.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from ..ports.repository import LocalGitStatus
from ..domain.enums import FileChangeType
from ..errors import ValidationError, WorkspaceNotFoundError
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
    "GitStatusReader",
    "PorcelainEntry",
    "get_default_status_reader",
    "set_default_status_reader",
    "reset_default_status_reader",
    "MAX_FILE_LIST",
]


logger = logging.getLogger(__name__)


#: Maximum number of entries parsed from a single status call.
#: Prevents unbounded memory use on very large repositories.
MAX_FILE_LIST: int = 10_000


#: Regex for the branch header emitted by `git status --branch`.
#:
#: Matches lines of the form:
#:     ## main...origin/main [ahead 1, behind 2]
#:     ## HEAD (no branch)
#:     ## No commits yet on main
_BRANCH_HEADER = re.compile(
    r"^##\s+(?:(?P<branch>[^\s.]+)"
    r"(?:\.\.\.(?P<upstream>[^\s\[]+))?"
    r"(?:\s+\[(?P<tracking>[^\]]+)\])?|(?P<detached>HEAD \(no branch\))|"
    r"No commits yet on (?P<newbranch>\S+))"
)

#: Regex for the upstream tracking segment: `ahead 1, behind 2`.
_TRACKING = re.compile(
    r"ahead\s+(?P<ahead>\d+)"
    r"|behind\s+(?P<behind>\d+)"
)


# ---------------------------------------------------------------------------
# Porcelain entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PorcelainEntry:
    """
    A single entry from `git status --porcelain`.

    Fields:
        x:          Index status character (` `, M, A, D, R, C, U, ?).
        y:          Worktree status character.
        path:       Primary path (normalized to `/` separators).
        orig_path:  Original path for renames/copies, when present.
    """

    x: str
    y: str
    path: str
    orig_path: Optional[str] = None

    @property
    def is_untracked(self) -> bool:
        """Return True if the entry represents an untracked file."""
        return self.x == "?" and self.y == "?"

    @property
    def is_ignored(self) -> bool:
        """Return True if the entry represents an ignored file."""
        return self.x == "!" and self.y == "!"

    @property
    def is_unmerged(self) -> bool:
        """Return True if the entry represents an unresolved merge."""
        return self.x == "U" or self.y == "U"

    @property
    def is_renamed_or_copied(self) -> bool:
        """Return True for rename and copy entries."""
        return "R" in (self.x, self.y) or "C" in (self.x, self.y)

    def change_type(self) -> FileChangeType:
        """
        Return the normalized `FileChangeType` for this entry.

        The classification prefers the index status (`x`) when it is
        meaningful, then falls back to the worktree status (`y`).
        """
        return _classify_change(self.x, self.y)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class GitStatusReader:
    """
    Stateless reader for `git status` output.

    Instances hold only immutable references to a `GitExecutable` and
    a `WorkspacePathPolicy`. They may be shared across coroutines.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()

    # --- Public API -----------------------------------------------------

    async def read_status(
        self,
        workspace_path: Union[str, Path],
        *,
        include_untracked: bool = True,
    ) -> LocalGitStatus:
        """
        Return the full status snapshot for `workspace_path`.

        The workspace path is validated through the path policy; a
        path outside the workspace root is rejected with
        `PathTraversalError`.

        Args:
            workspace_path: Absolute path to the repository.
            include_untracked: When False, untracked files are
                excluded from the returned `changed_paths`. The
                `is_clean` flag is unaffected.

        Raises:
            WorkspaceNotFoundError: If the directory does not exist.
            GitExecutionError: If `git status` fails unexpectedly.
        """
        target = self._resolve_repo(workspace_path)

        # `-z` uses NUL separators; safe for filenames containing
        # newlines. `--branch` appends the tracking header.
        result = await self._git.run(
            ["status", "--porcelain=v1", "-z", "--branch"],
            cwd=target,
            check_returncode=False,
        )

        if result.returncode != 0 and not result.stdout:
            # A non-zero exit with no output means the directory is
            # not a git repository. Treat it as empty / clean rather
            # than an error to keep callers simple.
            return LocalGitStatus(
                is_clean=True,
                is_conflicted=False,
                current_branch=None,
                head_sha=None,
            )

        entries, header = _parse_porcelain_z(result.stdout)

        changed_paths: List[str] = []
        for entry in entries:
            if entry.is_ignored:
                continue
            if entry.is_untracked and not include_untracked:
                continue
            changed_paths.append(entry.path)
            if len(changed_paths) >= MAX_FILE_LIST:
                break

        conflicted = await self.read_conflicts(target)
        head_sha = await self._read_head(target)

        is_clean = not changed_paths and not conflicted

        return LocalGitStatus(
            is_clean=is_clean,
            is_conflicted=bool(conflicted),
            ahead=header.ahead,
            behind=header.behind,
            current_branch=header.branch,
            head_sha=head_sha,
            upstream_branch=header.upstream,
            changed_paths=tuple(changed_paths),
        )

    async def is_clean(
        self, workspace_path: Union[str, Path]
    ) -> bool:
        """
        Return True if the workspace has no uncommitted changes and
        no unresolved conflicts.

        This is cheaper than `read_status` because it short-circuits
        on the first change.
        """
        target = self._resolve_repo(workspace_path)
        result = await self._git.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            # Not a repository: treat as clean for compatibility.
            return True
        # Any non-empty output means dirty.
        return not result.stdout.strip(b"\x00").strip()

    async def read_conflicts(
        self, workspace_path: Union[str, Path]
    ) -> Sequence[str]:
        """
        Return the list of paths with unresolved merge conflicts.

        Uses `git ls-files -u`, which reports unmerged index entries.
        """
        target = self._resolve_repo(workspace_path)
        result = await self._git.run(
            ["ls-files", "-u", "-z"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 or not result.stdout:
            return ()
        paths: List[str] = []
        # Each unmerged file produces up to three entries (base,
        # ours, theirs) with the same path. Deduplicate.
        seen: set[str] = set()
        for path in _split_z(result.stdout):
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
                if len(paths) >= MAX_FILE_LIST:
                    break
        return tuple(paths)

    async def read_untracked(
        self, workspace_path: Union[str, Path]
    ) -> Sequence[str]:
        """Return the list of untracked paths."""
        target = self._resolve_repo(workspace_path)
        result = await self._git.run(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 or not result.stdout:
            return ()
        return _bounded_split_z(result.stdout)

    async def read_modified(
        self, workspace_path: Union[str, Path]
    ) -> Sequence[str]:
        """
        Return the list of tracked paths with local modifications.

        Untracked and ignored files are NOT included.
        """
        target = self._resolve_repo(workspace_path)
        result = await self._git.run(
            ["diff", "--name-only", "-z"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 or not result.stdout:
            return ()
        # Also include staged changes.
        staged = await self._git.run(
            ["diff", "--cached", "--name-only", "-z"],
            cwd=target,
            check_returncode=False,
        )
        combined: List[str] = []
        seen: set[str] = set()
        for raw in (
            _split_z(result.stdout),
            _split_z(staged.stdout),
        ):
            for path in raw:
                if path and path not in seen:
                    seen.add(path)
                    combined.append(path)
                    if len(combined) >= MAX_FILE_LIST:
                        return tuple(combined)
        return tuple(combined)

    async def read_porcelain_entries(
        self, workspace_path: Union[str, Path]
    ) -> Sequence[PorcelainEntry]:
        """
        Return the parsed porcelain entries without building a full
        `LocalGitStatus`.

        Useful for callers that only need the entry list.
        """
        target = self._resolve_repo(workspace_path)
        result = await self._git.run(
            ["status", "--porcelain=v1", "-z"],
            cwd=target,
            check_returncode=False,
        )
        entries, _ = _parse_porcelain_z(result.stdout)
        return tuple(entries[:MAX_FILE_LIST])

    # --- Internal -------------------------------------------------------

    def _resolve_repo(
        self, workspace_path: Union[str, Path]
    ) -> Path:
        """
        Validate and normalize a workspace path.

        Uses the path policy to reject paths outside the workspace
        root and to normalize separators.

        Raises:
            ValidationError: If the input is not a valid path.
            WorkspaceNotFoundError: If the path does not exist.
        """
        if not workspace_path:
            raise ValidationError("workspace_path must not be empty.")

        resolved = self._policy.resolve_within_root(
            workspace_path, must_exist=True
        )
        if not resolved.is_dir():
            raise WorkspaceNotFoundError(
                f"Workspace path is not a directory: {resolved}.",
                details={"path": str(resolved)},
            )
        return resolved

    async def _read_head(
        self, workspace_path: Path
    ) -> Optional[str]:
        """Return the current HEAD SHA, or `None` on failure."""
        try:
            result = await self._git.run(
                ["rev-parse", "HEAD"],
                cwd=workspace_path,
                check_returncode=False,
            )
        except GitExecutionError:
            return None
        sha = result.stdout.strip()
        return sha or None


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BranchHeader:
    """Parsed output of the `git status --branch` header."""

    branch: Optional[str] = None
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0


def _parse_branch_header(line: str) -> _BranchHeader:
    """
    Parse the `##` header emitted by `git status --branch`.

    Returns a `_BranchHeader` with the parsed fields. Unrecognized
    formats yield an empty header rather than raising.
    """
    match = _BRANCH_HEADER.match(line)
    if not match:
        return _BranchHeader()

    if match.group("detached"):
        return _BranchHeader(branch=None)
    if match.group("newbranch"):
        return _BranchHeader(branch=match.group("newbranch"))

    branch = match.group("branch")
    upstream = match.group("upstream")
    tracking = match.group("tracking") or ""

    ahead = 0
    behind = 0
    for part in tracking.split(","):
        part = part.strip()
        m = _TRACKING.search(part)
        if m:
            if m.group("ahead"):
                ahead = int(m.group("ahead"))
            elif m.group("behind"):
                behind = int(m.group("behind"))

    return _BranchHeader(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
    )


# ---------------------------------------------------------------------------
# Porcelain parsing
# ---------------------------------------------------------------------------


def _parse_porcelain_z(
    raw: bytes,
) -> Tuple[List[PorcelainEntry], _BranchHeader]:
    """
    Parse NUL-delimited porcelain output.

    Returns `(entries, header)`. The header carries the branch name,
    upstream tracking branch, and ahead/behind counts when the
    `--branch` flag was used.

    Handles the rename form `R  orig -> new` (in v1, renames are
    encoded as `R  new\\0orig`, i.e. two NUL-separated fields).
    """
    if not raw:
        return [], _BranchHeader()

    text = raw.decode("utf-8", errors="replace")
    fields = text.split("\x00")
    # The trailing NUL produces a final empty field; drop it.
    if fields and fields[-1] == "":
        fields.pop()

    entries: List[PorcelainEntry] = []
    header = _BranchHeader()
    header_parsed = False
    index = 0

    while index < len(fields):
        field = fields[index]
        index += 1

        if not field:
            continue

        # The first field may be the `##` header when `--branch` was
        # used. It appears only once, at the beginning.
        if not header_parsed and field.startswith("##"):
            header = _parse_branch_header(field)
            header_parsed = True
            continue

        if len(field) < 3:
            # Malformed entry; skip.
            continue

        x = field[0]
        y = field[1]
        # The third character is a space in v1.
        path_part = field[3:] if field[2] == " " else field[2:]

        # Renames/copies in v1 are encoded as two NUL-separated
        # fields: `<XY> new_path\0orig_path`. Detect and merge.
        orig_path: Optional[str] = None
        if (x in ("R", "C") or y in ("R", "C")) and index < len(fields):
            candidate = fields[index]
            if candidate and not candidate.startswith(
                ("##", " ", "M", "A", "D", "R", "C", "U", "?", "!")
            ):
                orig_path = _normalize_path(candidate)
                index += 1

        entries.append(
            PorcelainEntry(
                x=x,
                y=y,
                path=_normalize_path(path_part),
                orig_path=orig_path,
            )
        )

        if len(entries) >= MAX_FILE_LIST:
            break

    return entries, header


def _split_z(raw: bytes) -> List[str]:
    """
    Split a NUL-delimited buffer into normalized path strings.

    Empty fields and the trailing NUL are dropped.
    """
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    return [
        _normalize_path(part)
        for part in text.split("\x00")
        if part
    ]


def _bounded_split_z(raw: bytes) -> List[str]:
    """`_split_z` with the `MAX_FILE_LIST` cap applied."""
    result = _split_z(raw)
    if len(result) > MAX_FILE_LIST:
        return result[:MAX_FILE_LIST]
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """
    Normalize a Git path for internal use.

    Git uses forward slashes internally. On Windows, `git status`
    may emit backslashes in rare configurations. This function
    converts them and applies NFC normalization.
    """
    if not path:
        return path
    normalized = unicodedata.normalize("NFC", path)
    return normalized.replace("\\", "/")


def _classify_change(x: str, y: str) -> FileChangeType:
    """
    Map the porcelain X/Y status pair to a `FileChangeType`.

    The classification considers the index status first and falls
    back to the worktree status. Untracked, ignored, and unmerged
    entries map to their own categories.
    """
    if x == "?" and y == "?":
        return FileChangeType.UNKNOWN
    if x == "!" and y == "!":
        return FileChangeType.UNKNOWN
    if x == "U" or y == "U":
        return FileChangeType.UNMERGED

    # Prefer the index status when it is meaningful.
    primary = x if x != " " else y

    mapping = {
        "M": FileChangeType.MODIFIED,
        "A": FileChangeType.ADDED,
        "D": FileChangeType.DELETED,
        "R": FileChangeType.RENAMED,
        "C": FileChangeType.COPIED,
        "T": FileChangeType.TYPE_CHANGED,
    }
    return mapping.get(primary, FileChangeType.UNKNOWN)


# ---------------------------------------------------------------------------
# Default reader singleton
# ---------------------------------------------------------------------------


_DEFAULT_READER: Optional[GitStatusReader] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_status_reader() -> GitStatusReader:
    """
    Return the process-wide default `GitStatusReader`, creating it on
    first use.
    """
    global _DEFAULT_READER
    with _DEFAULT_LOCK:
        if _DEFAULT_READER is None:
            _DEFAULT_READER = GitStatusReader()
        return _DEFAULT_READER


def set_default_status_reader(reader: GitStatusReader) -> None:
    """
    Replace the process-wide default status reader.

    Intended for application startup and for tests.
    """
    if not isinstance(reader, GitStatusReader):
        raise TypeError(
            "set_default_status_reader expects a GitStatusReader."
        )
    global _DEFAULT_READER
    with _DEFAULT_LOCK:
        _DEFAULT_READER = reader


def reset_default_status_reader() -> GitStatusReader:
    """
    Replace the default reader with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_READER
    with _DEFAULT_LOCK:
        _DEFAULT_READER = GitStatusReader()
        return _DEFAULT_READER
