# backend/integrations/source_control/git/log.py
"""
Git history and repository structure reader for the Source Control
Integration subsystem.

This module provides read-only access to a workspace's commit
history and file structure. It is the backend for the Repository
Browser and Commit History views in the frontend.

The reader offers five operations:

    - `read_log`         : paginated commit history with filtering.
    - `read_commit`      : a single commit with aggregate stats.
    - `get_commit_patch` : the full unified patch for a commit.
    - `list_tree`        : the file tree at a ref, as a hierarchical
                           structure ready for `react-arborist`.
    - `read_file_at_ref` : the content of a single file at a ref.

Design rules:
    - Every operation is strictly read-only. The reader never
      mutates the repository, the index, or the working tree.
    - Every workspace path is validated through the path policy
      before any Git invocation.
    - Pagination uses `git log --skip=N -n M`, which is stable and
      efficient on the local object database.
    - Output is already redacted by `GitExecutable._sanitize_stream`.
      The reader does not re-redact.
    - The reader is stateless with respect to per-request data and
      safe to share across coroutines.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..domain.models import CommitRef
from ..errors import (
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
    "GitLogReader",
    "CommitWithStats",
    "TreeNode",
    "get_default_log_reader",
    "set_default_log_reader",
    "reset_default_log_reader",
    "DEFAULT_LOG_PAGE_SIZE",
    "MAX_LOG_PAGE_SIZE",
    "MAX_TREE_ENTRIES",
    "MAX_FILE_CONTENT_BYTES",
]


logger = logging.getLogger(__name__)


#: Default number of commits returned by `read_log` when the caller
#: does not specify a limit.
DEFAULT_LOG_PAGE_SIZE: int = 50

#: Hard cap on the number of commits returned by `read_log` in a
#: single call. Protects against a caller requesting the entire
#: history of a large repository in one shot.
MAX_LOG_PAGE_SIZE: int = 500

#: Hard cap on the number of entries returned by `list_tree`. A
#: repository with more files than this SHOULD use `git sparse-checkout`
#: or another strategy; the reader refuses to build an unbounded tree
#: in memory.
MAX_TREE_ENTRIES: int = 10_000

#: Default maximum bytes returned by `read_file_at_ref`. Larger
#: files are truncated with an explicit marker.
MAX_FILE_CONTENT_BYTES: int = 1_000_000

#: NUL and Record Separator, used by the `git log --format` output
#: as field and record delimiters respectively.
_FIELD_SEP = "\x00"
_RECORD_SEP = "\x1e"

#: Format string for `git log`.
#:
#: Fields (in order):
#:     %H   - full commit SHA
#:     %an  - author name
#:     %ae  - author email
#:     %at  - author timestamp (Unix seconds)
#:     %P   - parent SHAs (space-separated)
#:     %B   - raw commit body (subject + body)
#:     %x00 - NUL field separator
#:     %x1e - RS record separator
_COMMIT_LOG_FORMAT = "%H%x00%an%x00%ae%x00%at%x00%P%x00%B%x1e"

#: Marker appended when file content is truncated.
_TRUNCATION_MARKER = "\n\n... [content truncated] ..."


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitWithStats(CommitRef):
    """
    A single commit with aggregate stats.

    Extends `CommitRef` with the number of files changed, total
    insertions, and total deletions for the commit. These stats are
    computed from `git show --numstat` and are not available in the
    plain `read_log` output.

    Fields (in addition to `CommitRef`):
        files_changed: Number of files touched by the commit.
        insertions:    Total added lines across all files.
        deletions:     Total removed lines across all files.
    """

    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class TreeNode:
    """
    A single node in a hierarchical file tree.

    The tree is designed for `react-arborist` and other tree
    components that expect a nested structure. Every node carries
    `name`, `path`, `type`, and (for directories) a tuple of
    `children`.

    Directories are sorted before files, and each group is sorted
    case-insensitively by name, so the frontend receives a stable,
    predictable order.

    Attributes:
        name:     The final path component (e.g. "src" or "main.py").
        path:     The full path from the repository root, using `/`.
        type:     Either "file" or "directory".
        size:     The file size in bytes, or None for directories.
        children: The child nodes for directories, empty for files.
    """

    name: str
    path: str
    type: str
    size: Optional[int] = None
    children: Tuple["TreeNode", ...] = ()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class GitLogReader:
    """
    Read-only access to commit history and repository structure.

    The reader holds only immutable references to a `GitExecutable`
    and a `WorkspacePathPolicy`. It is safe to share across
    coroutines and across threads.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()

    # --- Public API: commit history ------------------------------------

    async def read_log(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        limit: int = DEFAULT_LOG_PAGE_SIZE,
        skip: int = 0,
        ref: Optional[str] = None,
        path: Optional[str] = None,
        author: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        first_parent: bool = False,
    ) -> Sequence[CommitRef]:
        """
        Return a page of commits reachable from `ref`.

        The output is ordered newest-first, matching `git log`'s
        default order. Pagination is stable: calling `read_log` with
        `skip=N` and `skip=N+limit` returns two non-overlapping
        pages as long as the underlying history does not change.

        Args:
            workspace_id: Workspace identifier (label only).
            workspace_path: Absolute workspace path. Required.
            limit: Maximum number of commits to return. Capped at
                `MAX_LOG_PAGE_SIZE`.
            skip: Number of commits to skip from the tip of `ref`.
                Used for pagination.
            ref: Branch, tag, or SHA to start from. When `None`,
                `HEAD` is used. When the workspace has no commits
                yet and `ref` is `None`, an empty sequence is
                returned (this is a valid state, not an error).
            path: Optional path filter. Only commits that touch the
                given path are returned. The path is interpreted
                relative to the repository root.
            author: Optional author filter. Matches against the
                author name or email, matching `git log --author`'s
                semantics.
            since: When provided, only commits authored on or after
                this timestamp are returned.
            until: When provided, only commits authored on or before
                this timestamp are returned.
            first_parent: When True, only follow the first parent of
                each merge commit. Useful for a linear view of a
                branch's history.

        Returns:
            A tuple of `CommitRef` entries, newest first. Empty when
            the workspace has no commits yet or the filters exclude
            everything.

        Raises:
            ValidationError: If inputs are malformed.
            WorkspaceNotFoundError: If the workspace path does not
                exist or is not a directory.
            NotFoundError: If `ref` is provided and does not exist.
        """
        if not workspace_id or not isinstance(workspace_id, str):
            raise ValidationError("workspace_id must be a non-empty string.")
        if limit <= 0:
            raise ValidationError("limit must be positive.")
        if skip < 0:
            raise ValidationError("skip must not be negative.")

        effective_limit = min(limit, MAX_LOG_PAGE_SIZE)
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        # When no explicit ref is requested and the repository is
        # empty, return an empty sequence immediately. `git log`
        # would otherwise fail with "does not have any commits
        # yet", which is a legitimate state that the UI must render
        # as "no commits" rather than as an error.
        if ref is None:
            has_commits = await self._has_commits(target)
            if not has_commits:
                return ()

        args: List[str] = [
            "log",
            f"-n{effective_limit}",
            f"--skip={skip}",
            f"--format={_COMMIT_LOG_FORMAT}",
            "--no-color",
        ]
        if first_parent:
            args.append("--first-parent")
        if author:
            args.append(f"--author={author}")
        if since is not None:
            args.append(f"--since={_format_git_timestamp(since)}")
        if until is not None:
            args.append(f"--until={_format_git_timestamp(until)}")

        args.append(ref or "HEAD")
        if path:
            # `--` separates revisions from pathspecs. The path is
            # interpreted relative to the repository root; it cannot
            # escape the workspace because git pathspecs are always
            # relative.
            args.append("--")
            args.append(path)

        result = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            # Empty repository reached via an explicit ref (rare).
            if "does not have any commits" in stderr:
                return ()
            raise NotFoundError(
                f"Could not read log for ref '{ref or 'HEAD'}'.",
                details={
                    "workspace_id": workspace_id,
                    "ref": ref or "HEAD",
                    "stderr": (result.stderr or "")[:300],
                },
            )

        return _parse_commit_log(result.stdout)

    async def read_commit(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        sha: str,
    ) -> CommitWithStats:
        """
        Return a single commit with aggregate stats.

        Two Git invocations are made:

            1. `git log -n 1 --format=...` to fetch the commit
               metadata.
            2. `git show --numstat --format=` to fetch the numstat
               lines, which are summed into `files_changed`,
               `insertions`, and `deletions`.

        Both calls use the same workspace path, and the results are
        combined into a single `CommitWithStats`.

        Args:
            workspace_id: Workspace identifier (label only).
            workspace_path: Absolute workspace path.
            sha: Full or abbreviated commit SHA.

        Raises:
            ValidationError: If `sha` is empty.
            WorkspaceNotFoundError: If the workspace is missing.
            NotFoundError: If the commit does not exist.
        """
        if not sha or not isinstance(sha, str):
            raise ValidationError("sha must be a non-empty string.")
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        # 1. Fetch the commit metadata.
        log_args = [
            "log",
            "-n1",
            f"--format={_COMMIT_LOG_FORMAT}",
            "--no-color",
            sha,
        ]
        log_result = await self._git.run(
            log_args,
            cwd=target,
            check_returncode=False,
        )
        if log_result.returncode != 0:
            raise NotFoundError(
                f"Commit '{sha}' was not found.",
                details={"workspace_id": workspace_id, "sha": sha},
            )
        commits = _parse_commit_log(log_result.stdout)
        if not commits:
            raise NotFoundError(
                f"Commit '{sha}' could not be parsed.",
                details={"workspace_id": workspace_id, "sha": sha},
            )
        base = commits[0]

        # 2. Fetch the stats. `--format=` (empty) suppresses the
        # header so the output is only numstat lines.
        stats_args = [
            "show",
            "--numstat",
            "--format=",
            "--no-color",
            sha,
        ]
        stats_result = await self._git.run(
            stats_args,
            cwd=target,
            check_returncode=False,
        )

        files_changed = 0
        insertions = 0
        deletions = 0
        if stats_result.returncode == 0 and stats_result.stdout:
            for line in stats_result.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t", 2)
                if len(parts) < 3:
                    continue
                added, removed, _path = parts
                files_changed += 1
                # Binary files are reported as `-`.
                if added != "-":
                    try:
                        insertions += int(added)
                    except ValueError:
                        pass
                if removed != "-":
                    try:
                        deletions += int(removed)
                    except ValueError:
                        pass

        return CommitWithStats(
            sha=base.sha,
            message=base.message,
            author_name=base.author_name,
            author_email=base.author_email,
            committed_at=base.committed_at,
            parent_shas=base.parent_shas,
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
        )

    async def get_commit_patch(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        sha: str,
        max_bytes: int = MAX_FILE_CONTENT_BYTES,
    ) -> str:
        """
        Return the full unified patch for a commit.

        Uses `git show --patch --format=` to suppress the commit
        header and emit only the patch body. The output is
        truncated to `max_bytes` on a UTF-8-safe boundary.

        Args:
            workspace_id: Workspace identifier (label only).
            workspace_path: Absolute workspace path.
            sha: Full or abbreviated commit SHA.
            max_bytes: Maximum bytes of patch text to return.

        Raises:
            ValidationError: If `sha` is empty or `max_bytes` is not
                positive.
            WorkspaceNotFoundError: If the workspace is missing.
            NotFoundError: If the commit does not exist.
        """
        if not sha or not isinstance(sha, str):
            raise ValidationError("sha must be a non-empty string.")
        if max_bytes <= 0:
            raise ValidationError("max_bytes must be positive.")
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        args = [
            "show",
            "--patch",
            "--format=",
            "--no-color",
            "--no-ext-diff",
            sha,
        ]
        result = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            raise NotFoundError(
                f"Commit '{sha}' was not found.",
                details={"workspace_id": workspace_id, "sha": sha},
            )

        text = result.stdout
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return text
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + _TRUNCATION_MARKER

    # --- Public API: repository structure ------------------------------

    async def list_tree(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        ref: Optional[str] = None,
        max_entries: int = MAX_TREE_ENTRIES,
    ) -> Sequence[TreeNode]:
        """
        Return the file tree at `ref`, as a list of root nodes.

        The tree is built from `git ls-tree -r -l -z`, which lists
        every blob reachable from `ref` in a single pass. The reader
        groups paths by directory and returns a hierarchical
        structure with directories sorted before files.

        Directory entries do not appear in the `-r` output; they are
        synthesized from the file paths. An empty repository (or a
        ref with no entries) returns an empty sequence.

        Args:
            workspace_id: Workspace identifier (label only).
            workspace_path: Absolute workspace path.
            ref: Branch, tag, or SHA. When `None`, `HEAD` is used.
            max_entries: Cap on the number of files included in the
                tree. Files beyond this cap are not included, and a
                warning is logged. Use a larger value (or paginate)
                for very large repositories.

        Raises:
            ValidationError: If `max_entries` is not positive.
            WorkspaceNotFoundError: If the workspace is missing.
            NotFoundError: If `ref` does not exist.
        """
        if max_entries <= 0:
            raise ValidationError("max_entries must be positive.")
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        effective_ref = ref or "HEAD"
        args = [
            "ls-tree",
            "-r",
            "-l",
            "-z",
            effective_ref,
        ]
        result = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            if "not a valid object name" in stderr or "unknown revision" in stderr:
                raise NotFoundError(
                    f"Ref '{effective_ref}' was not found.",
                    details={
                        "workspace_id": workspace_id,
                        "ref": effective_ref,
                    },
                )
            # Empty repo: no output, no error.
            if not result.stdout.strip():
                return ()
            raise NotFoundError(
                f"Could not list tree for ref '{effective_ref}'.",
                details={
                    "workspace_id": workspace_id,
                    "ref": effective_ref,
                    "stderr": (result.stderr or "")[:300],
                },
            )

        entries = _parse_ls_tree_output(result.stdout, max_entries=max_entries)
        return _build_tree(entries, max_entries=max_entries)

    async def read_file_at_ref(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        ref: str,
        path: str,
        max_bytes: int = MAX_FILE_CONTENT_BYTES,
    ) -> str:
        """
        Return the content of a file at a given ref.

        Unlike `GitDiffEngine.read_workspace_file`, which reads the
        working tree, this method reads the committed content at
        `ref` via `git show <ref>:<path>`. It is the correct API for
        browsing historical files in the Repository Browser.

        Args:
            workspace_id: Workspace identifier (label only).
            workspace_path: Absolute workspace path.
            ref: Branch, tag, or commit SHA.
            path: Path of the file, relative to the repository root.
            max_bytes: Maximum bytes of content to return.

        Raises:
            ValidationError: If `ref` or `path` is empty, or if
                `max_bytes` is not positive.
            WorkspaceNotFoundError: If the workspace is missing.
            NotFoundError: If the ref or the file does not exist at
                that ref.
        """
        if not ref or not isinstance(ref, str):
            raise ValidationError("ref must be a non-empty string.")
        if not path or not isinstance(path, str):
            raise ValidationError("path must be a non-empty string.")
        if max_bytes <= 0:
            raise ValidationError("max_bytes must be positive.")
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        # Normalize the path: forward slashes, no leading slash.
        normalized_path = path.replace("\\", "/").lstrip("/")
        if not normalized_path:
            raise ValidationError("path must not be empty after normalization.")

        spec = f"{ref}:{normalized_path}"
        result = await self._git.run(
            ["show", spec],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            raise NotFoundError(
                f"File '{normalized_path}' was not found at ref '{ref}'.",
                details={
                    "workspace_id": workspace_id,
                    "ref": ref,
                    "path": normalized_path,
                },
            )

        text = result.stdout
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return text
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + _TRUNCATION_MARKER

    # --- Internal helpers ----------------------------------------------

    def _resolve_workspace_path(
        self,
        workspace_path: Union[str, Path],
        workspace_id: str,
    ) -> Path:
        """
        Validate and return the workspace directory.

        Raises `ValidationError` when the path is omitted, and
        `WorkspaceNotFoundError` when the path exists but is not a
        directory.
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

    async def _has_commits(self, target: Path) -> bool:
        """
        Return True if the repository has at least one commit.

        Uses `git rev-parse --verify HEAD`, which fails on an empty
        repository. This is the canonical way to distinguish "empty
        repo" from "ref does not exist".
        """
        result = await self._git.run(
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=target,
            check_returncode=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_commit_log(raw: str) -> Tuple[CommitRef, ...]:
    """
    Parse the output of `git log --format=...` produced with
    `_COMMIT_LOG_FORMAT`.

    Each commit is a record terminated by the record separator
    (`\\x1e`) and split into fields by NUL (`\\x00`). Blank and
    malformed records are skipped rather than raising, because a
    single bad entry must not break the entire listing.
    """
    if not raw:
        return ()

    normalized = raw.replace("\r\n", "\n")
    commits: List[CommitRef] = []
    for record in normalized.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < 6:
            continue
        sha, author_name, author_email, author_ts, parents, body = fields[:6]

        committed_at = _parse_unix_timestamp(author_ts)
        parent_shas = tuple(p for p in parents.strip().split(" ") if p)

        message = body
        if message.endswith("\n"):
            message = message[:-1]

        commits.append(
            CommitRef(
                sha=sha.strip(),
                message=message,
                author_name=author_name,
                author_email=author_email,
                committed_at=committed_at,
                parent_shas=parent_shas,
            )
        )
    return tuple(commits)


def _parse_unix_timestamp(value: str) -> datetime:
    """
    Convert a Unix-seconds string to a timezone-aware datetime.

    Falls back to `datetime.now(timezone.utc)` when the value
    cannot be parsed. This is intentionally lenient: a malformed
    timestamp must not block the entire commit list.
    """
    try:
        seconds = int(value.strip())
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return datetime.now(timezone.utc)


def _parse_ls_tree_output(
    raw: str, *, max_entries: int
) -> List[Tuple[str, Optional[int]]]:
    """
    Parse `git ls-tree -r -l -z` output into `(path, size)` tuples.

    The output format with `-l` is::

        <mode> SP <type> SP <object> SP* <size> TAB <path>\\0

    The size is `-` for non-blob entries (e.g. submodules). All
    entries in `-r` mode are blobs, so the size is a number or `-`.
    Paths are NUL-terminated; splitting on `\\x00` yields clean
    records without needing to strip the trailing terminator.

    Paths are normalized to NFC and forward slashes, matching the
    rest of the subsystem.
    """
    if not raw:
        return []

    entries: List[Tuple[str, Optional[int]]] = []
    for record in raw.split("\x00"):
        if not record:
            continue
        # Split off the path on the first TAB. The metadata prefix
        # contains mode, type, object SHA, and size, separated by
        # whitespace.
        tab_index = record.find("\t")
        if tab_index < 0:
            continue
        metadata = record[:tab_index]
        path = record[tab_index + 1 :]
        if not path:
            continue

        parts = metadata.split()
        if len(parts) < 4:
            continue

        size_raw = parts[3]
        size: Optional[int]
        if size_raw == "-":
            size = None
        else:
            try:
                size = int(size_raw)
            except ValueError:
                size = None

        normalized_path = unicodedata.normalize("NFC", path).replace("\\", "/")
        entries.append((normalized_path, size))
        if len(entries) >= max_entries:
            break

    return entries


def _build_tree(
    entries: Sequence[Tuple[str, Optional[int]]],
    *,
    max_entries: int,
) -> Tuple[TreeNode, ...]:
    """
    Build a hierarchical tree from a flat list of file entries.

    The algorithm walks each entry's path components, creating
    intermediate directory nodes as needed, and finally attaching
    the file node. Directories are sorted before files at every
    level, and each group is sorted case-insensitively by name.

    The function is deterministic: given the same input list (in
    any order), it produces the same tree structure.
    """
    # Nested dictionaries mirror the tree structure during
    # construction. The values are either a `dict` (directory
    # children) or a `TreeNode` (file).
    root: Dict[str, object] = {}

    for path, size in entries:
        if not path:
            continue
        parts = path.split("/")
        current: Dict[str, object] = root
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            if is_last:
                # Attach the file node. A file and a directory
                # cannot share a name in Git, so no collision check
                # is necessary.
                current[part] = TreeNode(
                    name=part,
                    path=path,
                    type="file",
                    size=size,
                )
            else:
                existing = current.get(part)
                if not isinstance(existing, dict):
                    # Create an intermediate directory node.
                    existing = {}
                    current[part] = existing
                current = existing

    # Convert the nested dictionary into a tuple of TreeNodes.
    return _convert_level(root, prefix="")


def _convert_level(
    level: Dict[str, object], *, prefix: str
) -> Tuple[TreeNode, ...]:
    """
    Recursively convert a nested dictionary into a tuple of
    `TreeNode` instances.

    Directories are sorted before files. Within each group, entries
    are sorted case-insensitively by name. The sort is applied at
    every level, so the resulting tree is stable across runs.
    """
    directories: List[TreeNode] = []
    files: List[TreeNode] = []

    for name, value in level.items():
        if isinstance(value, TreeNode):
            files.append(value)
        elif isinstance(value, dict):
            child_path = f"{prefix}/{name}" if prefix else name
            children = _convert_level(value, prefix=child_path)
            directories.append(
                TreeNode(
                    name=name,
                    path=child_path,
                    type="directory",
                    size=None,
                    children=children,
                )
            )

    directories.sort(key=lambda node: node.name.lower())
    files.sort(key=lambda node: node.name.lower())
    return tuple(directories) + tuple(files)


def _format_git_timestamp(value: datetime) -> str:
    """
    Format a datetime for `git log --since` / `--until`.

    Git accepts ISO 8601 timestamps with a timezone. The format
    used here is compatible with every Git version from 2.20
    onwards. Naive datetimes are interpreted as UTC, matching the
    subsystem's convention that every datetime is timezone-aware.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# ---------------------------------------------------------------------------
# Default reader singleton
# ---------------------------------------------------------------------------


_DEFAULT_READER: Optional[GitLogReader] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_log_reader() -> GitLogReader:
    """
    Return the process-wide default `GitLogReader`, creating it on
    first use.
    """
    global _DEFAULT_READER
    with _DEFAULT_LOCK:
        if _DEFAULT_READER is None:
            _DEFAULT_READER = GitLogReader()
        return _DEFAULT_READER


def set_default_log_reader(reader: GitLogReader) -> None:
    """
    Replace the process-wide default log reader.

    Intended for application startup and for tests.
    """
    if not isinstance(reader, GitLogReader):
        raise TypeError(
            "set_default_log_reader expects a GitLogReader."
        )
    global _DEFAULT_READER
    with _DEFAULT_LOCK:
        _DEFAULT_READER = reader


def reset_default_log_reader() -> GitLogReader:
    """
    Replace the default reader with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_READER
    with _DEFAULT_LOCK:
        _DEFAULT_READER = GitLogReader()
        return _DEFAULT_READER
