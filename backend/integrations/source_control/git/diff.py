# backend/integrations/source_control/git/diff.py
"""
Git diff engine for the Source Control Integration subsystem.

This module produces provider-neutral `DiffSummary` objects from a
local workspace. The summary is the canonical representation of "what
changed" and is used for:

    - Human review in the UI.
    - Approval binding via `DiffSummary.sha256()`.
    - Test/check gating before push.

The engine does NOT store the diff anywhere. Patches are computed on
demand and discarded after the caller receives them. Durable storage
of the diff hash is the responsibility of the operation journal.

Design rules:
    - The diff hash MUST be deterministic: two identical workspaces
      MUST produce the same `DiffSummary.sha256()` regardless of
      platform, Git version, or file ordering.
    - `read_workspace_file` MUST resolve paths through the workspace
      path policy. No symlink escape, no `..` traversal.
    - Patches are capped at `MAX_PATCH_SIZE`. Larger patches are
      truncated with an explicit marker.
    - File reads are capped at `MAX_FILE_SIZE`. Reads never load more
      than the cap into memory.
    - The engine is stateless and safe to share across coroutines.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..ports.repository import DiffPort
from ..domain.enums import FileChangeType
from ..domain.models import DiffSummary, FileDiff
from ..errors import (
    NotFoundError,
    SourceControlError,
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
from .status import (
    GitStatusReader,
    get_default_status_reader,
)

__all__ = [
    "GitDiffEngine",
    "get_default_diff_engine",
    "set_default_diff_engine",
    "reset_default_diff_engine",
    "MAX_PATCH_SIZE",
    "MAX_FILE_SIZE",
    "DEFAULT_MAX_FILES",
    "DEFAULT_UNIFIED_CONTEXT",
]


logger = logging.getLogger(__name__)


#: Maximum bytes retained per file patch. Patches beyond this size
#: are truncated. The cap protects memory and the review UI.
MAX_PATCH_SIZE: int = 1 * 1024 * 1024  # 1 MiB

#: Maximum bytes read from a single workspace file by
#: `read_workspace_file`.
MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10 MiB

#: Maximum number of files included in a single `DiffSummary`.
DEFAULT_MAX_FILES: int = 5_000

#: Number of context lines included in each patch hunk.
DEFAULT_UNIFIED_CONTEXT: int = 3

#: Marker appended to a truncated patch.
_TRUNCATION_MARKER = "\n[... patch truncated ...]\n"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class GitDiffEngine(DiffPort):
    """
    Stateless diff engine backed by the local Git binary.

    The engine resolves workspace paths through a
    `WorkspacePathPolicy` and executes Git through a `GitExecutable`.
    It holds no per-request state and is safe to share.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
        status_reader: Optional[GitStatusReader] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()
        self._status = status_reader or get_default_status_reader()

    # --- Public API (DiffPort) -----------------------------------------

    async def compute_diff(
        self,
        workspace_id: str,
        *,
        base_sha: Optional[str] = None,
        include_patch: bool = True,
        max_file_size_bytes: int = MAX_PATCH_SIZE,
        max_files: int = DEFAULT_MAX_FILES,
        unified_context: int = DEFAULT_UNIFIED_CONTEXT,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> DiffSummary:
        """
        Compute the diff of the workspace against `base_sha`.

        The method first resolves the workspace path. `workspace_id`
        is used as a label; the actual path must be supplied via
        `workspace_path` when the engine is used standalone. Callers
        that integrate with `GitWorkspaceManager` typically pass
        both.

        Args:
            workspace_id: Opaque workspace identifier (label only).
            base_sha: Base commit for the comparison. When `None`,
                the workspace's current HEAD is used, which produces
                a diff of uncommitted changes against HEAD.
            include_patch: When True, each `FileDiff` carries its
                patch text. When False, only metadata is computed.
            max_file_size_bytes: Maximum bytes retained per patch.
            max_files: Cap the number of files included.
            unified_context: Number of context lines per hunk.
            workspace_path: Absolute workspace path. Required unless
                the engine can resolve it from the workspace ID.

        Raises:
            ValidationError: If inputs are malformed.
            WorkspaceNotFoundError: If the workspace path does not
                exist.
        """
        if not workspace_id:
            raise ValidationError("workspace_id must not be empty.")
        if max_files <= 0:
            raise ValidationError("max_files must be positive.")
        if max_file_size_bytes <= 0:
            raise ValidationError("max_file_size_bytes must be positive.")
        if unified_context < 0:
            raise ValidationError("unified_context must not be negative.")

        target = await self._resolve_workspace_path(
            workspace_id=workspace_id, workspace_path=workspace_path
        )

        head_sha = await self._read_head_sha(target)
        resolved_base = base_sha or head_sha

        # If base equals head, diff the working tree against HEAD.
        # The caller passes `base_sha=None` for that case.
        diff_base = base_sha if base_sha else "HEAD"

        numstat = await self._run_numstat(
            target=target, base=diff_base, max_files=max_files
        )
        name_status = await self._run_name_status(
            target=target, base=diff_base, max_files=max_files
        )

        files = self._merge_numstat_and_name(
            numstat=numstat, name_status=name_status, max_files=max_files
        )

        if include_patch:
            files = await self._attach_patches(
                target=target,
                base=diff_base,
                files=files,
                unified_context=unified_context,
                max_patch_bytes=max_file_size_bytes,
            )

        total_additions = sum(f.additions for f in files)
        total_deletions = sum(f.deletions for f in files)

        summary = DiffSummary(
            files=tuple(files),
            base_sha=resolved_base or "",
            head_sha=head_sha or "",
            total_additions=total_additions,
            total_deletions=total_deletions,
        )
        return summary

    async def read_workspace_file(
        self,
        workspace_id: str,
        path: str,
        *,
        max_bytes: int = MAX_FILE_SIZE,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> str:
        """
        Read a text file inside the workspace.

        The path is resolved through the workspace path policy before
        any I/O. Reads are capped at `max_bytes`; larger files are
        returned truncated with an explicit marker.

        Raises:
            ValidationError: If the input is malformed.
            WorkspaceNotFoundError: If the workspace path does not
                exist.
            NotFoundError: If the file does not exist.
            PathTraversalError: If the path escapes the root.
        """
        if not workspace_id:
            raise ValidationError("workspace_id must not be empty.")
        if not path:
            raise ValidationError("path must not be empty.")
        if max_bytes <= 0:
            raise ValidationError("max_bytes must be positive.")

        target = await self._resolve_workspace_path(
            workspace_id=workspace_id, workspace_path=workspace_path
        )

        # Resolve the file path inside the workspace.
        candidate = target / path
        resolved = self._policy.resolve_within_root(
            candidate, must_exist=True
        )

        if not resolved.is_file():
            raise NotFoundError(
                f"Path is not a file: {path}.",
                details={"path": path},
            )

        return await self._bounded_read(resolved, max_bytes=max_bytes)

    # --- Internal: path resolution --------------------------------------

    async def _resolve_workspace_path(
        self,
        *,
        workspace_id: str,
        workspace_path: Optional[Union[str, Path]],
    ) -> Path:
        """
        Return a validated workspace directory.

        When `workspace_path` is supplied, it is validated through
        the path policy. Otherwise, the method attempts to reconstruct
        a plausible path from the workspace ID alone; if that fails,
        a `WorkspaceNotFoundError` is raised. The intended use is for
        callers to always supply the path.
        """
        if workspace_path is None:
            raise ValidationError(
                "workspace_path is required for this engine instance.",
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

    # --- Internal: git invocation ---------------------------------------

    async def _run_numstat(
        self,
        *,
        target: Path,
        base: str,
        max_files: int,
    ) -> List[Tuple[str, str, str]]:
        """
        Run `git diff --numstat -z` and return raw tuples.

        Returns a list of `(added, removed, path)` tuples where
        `added` and `removed` are decimal strings or `"-"` for binary
        files.
        """
        result = await self._git.run(
            [
                "diff",
                "--numstat",
                "-z",
                "--no-color",
                "--no-ext-diff",
                "--ignore-submodules=dirty",
                base,
            ],
            cwd=target,
            check_returncode=False,
            timeout_seconds=120,
        )
        # The previous implementation silently returned an empty
        # diff whenever git exited non-zero without producing
        # stdout. That masked a class of real failures — the most
        # common being an invalid `base_sha` ("fatal: bad revision")
        # — behind a plausible-looking "no changes" result. The
        # approval flow relies on distinguishing "workspace has no
        # changes" (a legitimate state that blocks approval with a
        # clear message) from "diff computation failed" (an actual
        # error the user must see). A non-zero return code is
        # always a genuine failure: `git diff` without
        # `--exit-code` returns 0 even when there are differences.
        if result.returncode != 0:
            raise SourceControlError(
                f"git diff --numstat failed for base '{base}'.",
                details={
                    "base": base,
                    "returncode": result.returncode,
                    "stderr": (result.stderr or "").strip()[:500],
                },
            )
        return _parse_numstat_z(result.stdout, max_files=max_files)

    async def _run_name_status(
        self,
        *,
        target: Path,
        base: str,
        max_files: int,
    ) -> List[Tuple[str, str, Optional[str]]]:
        """
        Run `git diff --name-status -z` and return raw tuples.

        Returns a list of `(status, path, orig_path)` tuples where
        `status` is one of `A`, `M`, `D`, `R###`, `C###`, `T`.
        """
        result = await self._git.run(
            [
                "diff",
                "--name-status",
                "-z",
                "--no-color",
                "--no-ext-diff",
                "--ignore-submodules=dirty",
                "-M",
                "-C",
                base,
            ],
            cwd=target,
            check_returncode=False,
            timeout_seconds=120,
        )
        # Mirror the failure contract of `_run_numstat`: a non-zero
        # return code from `git diff` is a genuine error (invalid
        # base SHA, corrupt repository, wrong working directory),
        # not an empty result. Surfacing it here lets the caller
        # distinguish "workspace is unchanged" from "diff failed".
        if result.returncode != 0:
            raise SourceControlError(
                f"git diff --name-status failed for base '{base}'.",
                details={
                    "base": base,
                    "returncode": result.returncode,
                    "stderr": (result.stderr or "").strip()[:500],
                },
            )
        return _parse_name_status_z(result.stdout, max_files=max_files)

    async def _read_head_sha(self, target: Path) -> Optional[str]:
        """Return the workspace HEAD SHA, or `None`."""
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

    # --- Internal: merge and patch --------------------------------------

    def _merge_numstat_and_name(
        self,
        *,
        numstat: Sequence[Tuple[str, str, str]],
        name_status: Sequence[Tuple[str, str, Optional[str]]],
        max_files: int,
    ) -> List[FileDiff]:
        """
        Combine numstat and name-status entries into `FileDiff`s.

        numstat provides line counts and binary detection. name-status
        provides the change type and, for renames/copies, the
        original path. Entries present in only one source are still
        included with sane defaults.
        """
        # Index name-status by target path.
        status_by_path: Dict[str, Tuple[str, Optional[str]]] = {}
        for status, path, orig in name_status:
            status_by_path[path] = (status, orig)

        files: List[FileDiff] = []
        seen: set[str] = set()

        for added, removed, path in numstat:
            if len(files) >= max_files:
                break
            status, orig = status_by_path.get(path, ("M", None))
            change_type = _classify_status(status)
            is_binary = added == "-" or removed == "-"
            additions = 0 if is_binary else _safe_int(added)
            deletions = 0 if is_binary else _safe_int(removed)

            files.append(
                FileDiff(
                    path=path,
                    change_type=change_type,
                    additions=additions,
                    deletions=deletions,
                    old_path=orig,
                    patch=None,
                    is_binary=is_binary,
                    is_truncated=False,
                )
            )
            seen.add(path)

        # Include any name-status entry missing from numstat (e.g.
        # pure renames with no line changes).
        for status, path, orig in name_status:
            if path in seen:
                continue
            if len(files) >= max_files:
                break
            change_type = _classify_status(status)
            files.append(
                FileDiff(
                    path=path,
                    change_type=change_type,
                    additions=0,
                    deletions=0,
                    old_path=orig,
                    patch=None,
                    is_binary=False,
                    is_truncated=False,
                )
            )
            seen.add(path)

        # Keep a stable order for reproducibility.
        files.sort(key=lambda f: f.path)
        return files

    async def _attach_patches(
        self,
        *,
        target: Path,
        base: str,
        files: Sequence[FileDiff],
        unified_context: int,
        max_patch_bytes: int,
    ) -> List[FileDiff]:
        """
        Fetch the patch for each file and attach it.

        Binary files are skipped: their patches are meaningless and
        would produce noise. Truncated patches are marked via
        `is_truncated=True`.
        """
        result: List[FileDiff] = []
        for diff in files:
            if diff.is_binary:
                result.append(diff)
                continue

            patch = await self._read_patch(
                target=target,
                base=base,
                path=diff.path,
                context=unified_context,
                max_bytes=max_patch_bytes,
            )
            if patch is None:
                result.append(diff)
                continue

            truncated = patch.endswith(_TRUNCATION_MARKER)
            result.append(
                replace(diff, patch=patch, is_truncated=truncated)
            )
        return result

    async def _read_patch(
        self,
        *,
        target: Path,
        base: str,
        path: str,
        context: int,
        max_bytes: int,
    ) -> Optional[str]:
        """
        Read the patch for a single file.

        Returns `None` when the patch cannot be read, in which case
        the caller keeps the metadata-only `FileDiff`. This is not an
        error: some Git configurations suppress patches for certain
        file types.
        """
        try:
            result = await self._git.run(
                [
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    "--ignore-submodules=dirty",
                    f"--unified={context}",
                    base,
                    "--",
                    path,
                ],
                cwd=target,
                check_returncode=False,
                timeout_seconds=60,
            )
        except GitExecutionError:
            return None

        if result.returncode != 0:
            return None

        text = result.stdout
        if len(text.encode("utf-8", errors="replace")) <= max_bytes:
            return text

        # Truncate at the byte cap while keeping valid UTF-8.
        encoded = text.encode("utf-8", errors="replace")
        truncated_bytes = encoded[:max_bytes]
        decoded = truncated_bytes.decode("utf-8", errors="ignore")
        return decoded + _TRUNCATION_MARKER

    # --- Internal: bounded file read ------------------------------------

    async def _bounded_read(self, path: Path, *, max_bytes: int) -> str:
        """
        Read a file up to `max_bytes`, decoding as UTF-8 with
        replacement. Larger files are truncated with an explicit
        marker.
        """
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise NotFoundError(
                f"Could not stat file: {path.name}.",
                details={"path": str(path)},
                cause=exc,
            ) from exc

        try:
            with path.open("rb") as fh:
                payload = fh.read(max_bytes)
                truncated = size > max_bytes
        except OSError as exc:
            raise NotFoundError(
                f"Could not read file: {path.name}.",
                details={"path": str(path)},
                cause=exc,
            ) from exc

        decoded = payload.decode("utf-8", errors="replace")
        if truncated:
            decoded += "\n[... file truncated ...]\n"
        return decoded


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_numstat_z(
    raw: str, *, max_files: int
) -> List[Tuple[str, str, str]]:
    """
    Parse `git diff --numstat -z` output.

    Format: `<added>\\t<removed>\\t<path>\\0` where `<path>` may
    itself contain tabs. Binary files use `-` for both counts.
    Renames are encoded as `<added>\\t<removed>\\t\\0<old>\\0<new>\\0`.

    The caller passes `GitResult.stdout`, which is already a decoded
    `str` (see `GitExecutable._sanitize_stream`). The previous
    implementation assumed `bytes` and called `.decode()` on it,
    which raised `AttributeError: 'str' object has no attribute
    'decode'` on every invocation. The NUL and tab separators survive
    the UTF-8 decode intact because they are ASCII bytes, so splitting
    on the `str` equivalents is equivalent to the old behavior.
    """
    if not raw:
        return []

    fields = raw.split("\x00")
    if fields and fields[-1] == "":
        fields.pop()

    results: List[Tuple[str, str, str]] = []
    index = 0
    while index < len(fields) and len(results) < max_files:
        field = fields[index]
        index += 1
        if not field:
            continue

        parts = field.split("\t", 2)
        if len(parts) < 3:
            continue

        added, removed, path = parts
        # Rename form: the path portion is empty and the next two
        # fields carry the old and new paths.
        if path == "" and index + 1 < len(fields):
            old = fields[index]
            new = fields[index + 1]
            index += 2
            results.append((added, removed, _normalize(path=new or old)))
            continue

        results.append((added, removed, _normalize(path=path)))

    return results


def _parse_name_status_z(
    raw: str, *, max_files: int
) -> List[Tuple[str, str, Optional[str]]]:
    """
    Parse `git diff --name-status -z` output.

    Format: `<status>\\0<path>\\0` for non-renames, and
    `<status>\\0<old>\\0<new>\\0` for renames/copies.

    The caller passes `GitResult.stdout`, which is already a decoded
    `str`. See `_parse_numstat_z` for the full rationale.
    """
    if not raw:
        return []

    fields = raw.split("\x00")
    if fields and fields[-1] == "":
        fields.pop()

    results: List[Tuple[str, str, Optional[str]]] = []
    index = 0
    while index < len(fields) and len(results) < max_files:
        status = fields[index]
        index += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            if index + 1 >= len(fields):
                break
            old = fields[index]
            new = fields[index + 1]
            index += 2
            results.append((status, _normalize(path=new), _normalize(path=old)))
        else:
            if index >= len(fields):
                break
            path = fields[index]
            index += 1
            results.append((status, _normalize(path=path), None))
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_status(status: str) -> FileChangeType:
    """
    Map a name-status code to a `FileChangeType`.

    Rename and copy codes carry a similarity score suffix (e.g.
    `R100`); only the first character is meaningful for the type.
    """
    if not status:
        return FileChangeType.UNKNOWN
    code = status[0].upper()
    mapping = {
        "A": FileChangeType.ADDED,
        "M": FileChangeType.MODIFIED,
        "D": FileChangeType.DELETED,
        "R": FileChangeType.RENAMED,
        "C": FileChangeType.COPIED,
        "T": FileChangeType.TYPE_CHANGED,
        "U": FileChangeType.UNMERGED,
    }
    return mapping.get(code, FileChangeType.UNKNOWN)


def _safe_int(value: str) -> int:
    """Parse `value` as an integer, returning 0 on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize(path: str) -> str:
    """Normalize a Git path (NFC, forward slashes)."""
    if not path:
        return path
    import unicodedata

    return unicodedata.normalize("NFC", path).replace("\\", "/")


# ---------------------------------------------------------------------------
# Default engine singleton
# ---------------------------------------------------------------------------


_DEFAULT_ENGINE: Optional[GitDiffEngine] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_diff_engine() -> GitDiffEngine:
    """
    Return the process-wide default `GitDiffEngine`, creating it on
    first use.
    """
    global _DEFAULT_ENGINE
    with _DEFAULT_LOCK:
        if _DEFAULT_ENGINE is None:
            _DEFAULT_ENGINE = GitDiffEngine()
        return _DEFAULT_ENGINE


def set_default_diff_engine(engine: GitDiffEngine) -> None:
    """
    Replace the process-wide default diff engine.

    Intended for application startup and for tests.
    """
    if not isinstance(engine, GitDiffEngine):
        raise TypeError(
            "set_default_diff_engine expects a GitDiffEngine."
        )
    global _DEFAULT_ENGINE
    with _DEFAULT_LOCK:
        _DEFAULT_ENGINE = engine


def reset_default_diff_engine() -> GitDiffEngine:
    """
    Replace the default engine with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_ENGINE
    with _DEFAULT_LOCK:
        _DEFAULT_ENGINE = GitDiffEngine()
        return _DEFAULT_ENGINE
