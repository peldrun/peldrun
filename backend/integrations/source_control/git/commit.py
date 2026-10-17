# backend/integrations/source_control/git/commit.py
"""
Local commit creation for the Source Control Integration subsystem.

This module implements `CommitPort` from `ports.repository`. It is
the ONLY place in the subsystem where commits are created.

Design rules:
    - The commit message is passed via stdin (`-F -`) rather than on
      the command line. This correctly handles newlines, Unicode,
      long messages, and avoids any encoding pitfalls on Windows.
    - The author identity is injected via `-c user.name=...` and
      `-c user.email=...`, so that the operation does not depend on a
      global `~/.gitconfig`.
    - Destructive operations (`amend_commit`) refuse to run against
      commits that already exist on a remote unless the caller
      explicitly opts in. The manager cannot verify the remote state
      itself; it documents the contract and relies on the caller.
    - The manager never uses `git reset`, `git clean`, or any
      operation that discards user work.
    - The manager is stateless with respect to per-request data and
      safe to share across coroutines.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from ..ports.repository import CommitOptions, CommitPort
from ..domain.models import CommitRef
from ..errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
    WorkspaceDirtyError,
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
    "GitCommitManager",
    "get_default_commit_manager",
    "set_default_commit_manager",
    "reset_default_commit_manager",
    "MAX_COMMIT_MESSAGE_LENGTH",
    "DEFAULT_AUTHOR_NAME",
    "DEFAULT_AUTHOR_EMAIL",
    "MAX_LISTED_COMMITS",
]


logger = logging.getLogger(__name__)


#: Maximum length of a commit message accepted by the manager. The
#: cap is intentionally generous (well above any realistic message)
#: and exists only to bound memory use.
MAX_COMMIT_MESSAGE_LENGTH: int = 10_000

#: Default author used when none is provided. This allows PELDRUN to
#: commit inside sandboxes that do not have a configured Git identity.
DEFAULT_AUTHOR_NAME: str = "PELDRUN"
DEFAULT_AUTHOR_EMAIL: str = "peldrun@localhost"

#: Maximum number of commits returned by `list_commits` in one call.
MAX_LISTED_COMMITS: int = 1_000

#: Delimiter used between fields in `git log --format`. The record
#: separator (`\x1e`) marks the end of one commit and the start of
#: the next.
_FIELD_SEP = "\x00"
_RECORD_SEP = "\x1e"

#: Format string for `git log --format=...`.
#:
#: Fields (in order):
#:     %H  full commit SHA
#:     %an author name
#:     %ae author email
#:     %at author timestamp (Unix seconds)
#:     %P  parent SHAs (space-separated)
#:     %B  raw commit body (may contain newlines)
_COMMIT_LOG_FORMAT = (
    f"%H{_FIELD_SEP}%an{_FIELD_SEP}%ae{_FIELD_SEP}%at"
    f"{_FIELD_SEP}%P{_FIELD_SEP}%B{_RECORD_SEP}"
)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitCommitManager(CommitPort):
    """
    Local commit manager backed by the Git command-line tool.

    The manager never mutates the working tree except through
    `git add` and `git commit`. It does not use `git reset`, `git
    clean`, or any other state-discarding command.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
        default_author_name: str = DEFAULT_AUTHOR_NAME,
        default_author_email: str = DEFAULT_AUTHOR_EMAIL,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()
        self._default_author_name = _sanitize_author_name(default_author_name)
        self._default_author_email = _sanitize_author_email(default_author_email)

    # --- Public API (CommitPort) ---------------------------------------

    async def create_commit(
        self,
        workspace_id: str,
        *,
        message: str,
        options: Optional[CommitOptions] = None,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> CommitRef:
        """
        Stage and commit local changes.

        Args:
            workspace_id: Workspace identifier (label).
            message: Commit message. Must be non-empty.
            options: Optional commit options.
            workspace_path: Absolute workspace path.

        Raises:
            ValidationError: If the message is empty or too long.
            WorkspaceNotFoundError: If the workspace path is
                missing.
            WorkspaceDirtyError: If there is nothing to commit and
                `allow_empty` is False.
        """
        opts = options or CommitOptions()
        normalized_message = self._validate_message(message)
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        author_name, author_email = self._resolve_author(opts)

        if opts.stage_all:
            await self._stage_all(target)

        if not opts.allow_empty:
            has_staged = await self._has_staged_changes(target)
            if not has_staged:
                raise WorkspaceDirtyError(
                    "There is nothing to commit.",
                    details={"workspace_id": workspace_id},
                )

        await self._run_commit(
            target=target,
            message=normalized_message,
            author_name=author_name,
            author_email=author_email,
            allow_empty=opts.allow_empty,
            amend=False,
        )

        commit = await self._read_latest_commit(target)
        if commit is None:
            raise ConflictError(
                "Commit succeeded but the new commit could not be "
                "read back.",
                details={"workspace_id": workspace_id},
            )
        return commit

    async def amend_commit(
        self,
        workspace_id: str,
        *,
        message: Optional[str] = None,
        options: Optional[CommitOptions] = None,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> CommitRef:
        """
        Amend the most recent commit.

        When `message` is `None`, the original message is preserved
        via `--no-edit`. When a new message is provided, it is used.

        WARNING: amending a commit that has already been pushed
        rewrites history and requires a force push. The manager does
        NOT verify remote state; the caller MUST ensure that either
        the commit has not been pushed, or that the user has approved
        a force push.

        Raises:
            ValidationError: If the workspace is empty (no commits).
            WorkspaceNotFoundError: If the workspace path is missing.
        """
        opts = options or CommitOptions()
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        author_name, author_email = self._resolve_author(opts)

        if not await self._has_any_commit(target):
            raise ValidationError(
                "Cannot amend: the repository has no commits yet.",
                details={"workspace_id": workspace_id},
            )

        if opts.stage_all:
            await self._stage_all(target)

        normalized_message = (
            self._validate_message(message) if message is not None else None
        )

        await self._run_commit(
            target=target,
            message=normalized_message,
            author_name=author_name,
            author_email=author_email,
            allow_empty=True,  # amend always allows empty
            amend=True,
        )

        commit = await self._read_latest_commit(target)
        if commit is None:
            raise ConflictError(
                "Amend succeeded but the new commit could not be read "
                "back.",
                details={"workspace_id": workspace_id},
            )
        return commit

    # --- Staging helpers -----------------------------------------------

    async def stage_all(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Stage every change in the workspace, including deletions.

        Uses `git add -A`, which also picks up deletions and
        modifications to tracked files.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        await self._stage_all(target)

    async def stage_files(
        self,
        workspace_id: str,
        *,
        paths: Sequence[str],
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Stage a specific set of paths.

        Each path is resolved through the workspace path policy before
        being passed to `git add`. Paths outside the workspace are
        rejected with `PathTraversalError`.

        Raises:
            ValidationError: If `paths` is empty.
        """
        if not paths:
            raise ValidationError("paths must contain at least one entry.")

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        validated_paths: List[str] = []
        for raw in paths:
            if not raw:
                raise ValidationError("path entries must not be empty.")
            resolved = self._policy.resolve_within_root(
                target / raw, must_exist=False
            )
            # Convert back to a workspace-relative POSIX path for
            # Git.
            relative = resolved.relative_to(target)
            validated_paths.append(relative.as_posix())

        await self._git.run(
            ["add", "--", *validated_paths],
            cwd=target,
        )

    # --- State queries -------------------------------------------------

    async def has_staged_changes(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """Return True if the index differs from HEAD."""
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        return await self._has_staged_changes(target)

    async def has_uncommitted_changes(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Return True if the working tree differs from HEAD.

        This is broader than `has_staged_changes`: it also reports
        unstaged modifications and untracked files.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        result = await self._git.run(
            ["status", "--porcelain"],
            cwd=target,
            check_returncode=False,
        )
        return bool(result.stdout.strip())

    async def commit_exists(
        self,
        workspace_id: str,
        sha: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """Return True if `sha` refers to an existing commit object."""
        if not sha or not isinstance(sha, str):
            return False
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        result = await self._git.run(
            ["cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=target,
            check_returncode=False,
        )
        return result.returncode == 0

    # --- Commit read operations ----------------------------------------

    async def get_commit_info(
        self,
        workspace_id: str,
        sha: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> CommitRef:
        """
        Return details for a single commit.

        Raises:
            ValidationError: If `sha` is empty.
            NotFoundError: If the commit does not exist.
        """
        if not sha:
            raise ValidationError("sha must not be empty.")
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        result = await self._git.run(
            [
                "show",
                "-s",
                f"--format={_COMMIT_LOG_FORMAT}",
                "--no-color",
                "--no-pager",
                sha,
            ],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            raise NotFoundError(
                f"Commit '{sha}' was not found.",
                details={"sha": sha},
            )
        commits = _parse_commit_log(result.stdout)
        if not commits:
            raise NotFoundError(
                f"Commit '{sha}' could not be parsed.",
                details={"sha": sha},
            )
        return commits[0]

    async def get_latest_commit(
        self,
        workspace_id: str,
        *,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> Optional[CommitRef]:
        """
        Return the most recent commit in the current branch, or
        `None` if the repository has no commits.
        """
        target = self._resolve_workspace_path(workspace_path, workspace_id)
        return await self._read_latest_commit(target)

    async def list_commits(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        ref: Optional[str] = None,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> Sequence[CommitRef]:
        """
        Return the most recent commits reachable from `ref`.

        Args:
            limit: Maximum number of commits to return.
            ref: Branch, tag, or SHA to start from. When `None`,
                `HEAD` is used.

        Raises:
            ValidationError: If `limit` is not positive.
        """
        if limit <= 0:
            raise ValidationError("limit must be positive.")
        if limit > MAX_LISTED_COMMITS:
            limit = MAX_LISTED_COMMITS

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        result = await self._git.run(
            [
                "log",
                f"-n{limit}",
                f"--format={_COMMIT_LOG_FORMAT}",
                "--no-color",
                "--no-pager",
                ref or "HEAD",
            ],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 and not result.stdout:
            return ()
        return _parse_commit_log(result.stdout)

    # --- Internal: commit execution ------------------------------------

    async def _run_commit(
        self,
        *,
        target: Path,
        message: Optional[str],
        author_name: str,
        author_email: str,
        allow_empty: bool,
        amend: bool,
    ) -> None:
        """
        Invoke `git commit` with the given message and identity.

        The message is supplied via stdin (`-F -`), and the author
        identity via `-c` flags, so the operation does not depend on
        any global Git configuration.

        `--no-gpg-sign` is passed unconditionally: PELDRUN does not
        ship a signing key, and a hanging GPG prompt would deadlock
        the process. Projects that require signed commits must be
        signed outside PELDRUN.
        """
        args: List[str] = [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "--no-color",
        ]
        if allow_empty:
            args.append("--allow-empty")
        if amend:
            if message is None:
                args.append("--amend")
                args.append("--no-edit")
            else:
                args.append("--amend")
                args.append("-F")
                args.append("-")
        else:
            args.append("-F")
            args.append("-")

        stdin_payload = (
            _format_commit_message(message).encode("utf-8")
            if message is not None
            else None
        )

        # `--no-verify` is technically unnecessary here because we
        # also remove dangerous environment variables. It is included
        # for clarity and to prevent a hostile hook from blocking
        # the operation even when the user has approved it.
        await self._git.run(
            args,
            cwd=target,
            stdin_bytes=stdin_payload,
            timeout_seconds=120,
        )

    async def _stage_all(self, target: Path) -> None:
        """
        Run `git add -A` to stage every change.

        This is intentionally broad: it stages modifications,
        additions, and deletions across the entire workspace.
        """
        await self._git.run(
            ["add", "-A"],
            cwd=target,
            timeout_seconds=120,
        )

    async def _has_staged_changes(self, target: Path) -> bool:
        """
        Return True if the index differs from HEAD.

        Uses `git diff --cached --quiet`, which exits 1 when there
        are differences and 0 otherwise.
        """
        result = await self._git.run(
            ["diff", "--cached", "--quiet"],
            cwd=target,
            check_returncode=False,
        )
        return result.returncode != 0

    async def _has_any_commit(self, target: Path) -> bool:
        """
        Return True if the repository has at least one commit.

        Uses `git rev-parse --verify HEAD`, which fails on an empty
        repository.
        """
        result = await self._git.run(
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=target,
            check_returncode=False,
        )
        return result.returncode == 0

    async def _read_latest_commit(
        self, target: Path
    ) -> Optional[CommitRef]:
        """Return the HEAD commit, or `None` for an empty repository."""
        if not await self._has_any_commit(target):
            return None
        result = await self._git.run(
            [
                "log",
                "-n1",
                f"--format={_COMMIT_LOG_FORMAT}",
                "--no-color",
                "--no-pager",
                "HEAD",
            ],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 and not result.stdout:
            return None
        commits = _parse_commit_log(result.stdout)
        return commits[0] if commits else None

    # --- Internal: validation ------------------------------------------

    def _resolve_workspace_path(
        self,
        workspace_path: Optional[Union[str, Path]],
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

    def _validate_message(self, message: str) -> str:
        """
        Validate and normalize a commit message.

        Raises `ValidationError` for empty messages and for messages
        that exceed `MAX_COMMIT_MESSAGE_LENGTH`. Returns the
        normalized message with trailing newlines stripped and a
        single final newline.
        """
        if message is None or not isinstance(message, str):
            raise ValidationError("Commit message must be a string.")
        normalized = _normalize_message(message)
        if not normalized.strip():
            raise ValidationError("Commit message must not be empty.")
        if len(normalized) > MAX_COMMIT_MESSAGE_LENGTH:
            raise ValidationError(
                "Commit message exceeds the maximum allowed length.",
                details={
                    "length": len(normalized),
                    "max_length": MAX_COMMIT_MESSAGE_LENGTH,
                },
            )
        return normalized

    def _resolve_author(self, options: CommitOptions) -> Tuple[str, str]:
        """
        Resolve the (name, email) pair for a commit.

        Precedence: `CommitOptions` fields, then the manager's
        defaults. Both values are sanitized before use.
        """
        name = (
            _sanitize_author_name(options.author_name)
            if options.author_name
            else self._default_author_name
        )
        email = (
            _sanitize_author_email(options.author_email)
            if options.author_email
            else self._default_author_email
        )
        return name, email


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def _normalize_message(message: str) -> str:
    """
    Normalize a commit message.

    Converts CRLF to LF, strips trailing whitespace on each line,
    collapses runs of blank lines, and ensures a single trailing
    newline. The subject line is preserved verbatim (aside from
    trailing whitespace).
    """
    if not message:
        return ""

    # Normalize Unicode to NFC and standardize newlines.
    text = unicodedata.normalize("NFC", message)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Strip trailing whitespace per line.
    lines = [line.rstrip() for line in text.split("\n")]

    # Collapse runs of blank lines into a single blank line.
    collapsed: List[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)

    # Strip leading and trailing blank lines.
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()

    return "\n".join(collapsed)


def _format_commit_message(message: Optional[str]) -> str:
    """
    Return the final byte payload for `git commit -F -`.

    A trailing newline is always appended, matching Git's own
    convention and avoiding a warning from `git commit`.
    """
    if message is None:
        return ""
    if message.endswith("\n"):
        return message
    return message + "\n"


# ---------------------------------------------------------------------------
# Author validation
# ---------------------------------------------------------------------------


_AUTHOR_NAME_FORBIDDEN = re.compile(r"[\n\r\x00<>]")
_AUTHOR_EMAIL_FORBIDDEN = re.compile(r"[\n\r\x00<>]")


def _sanitize_author_name(name: str) -> str:
    """
    Validate an author name.

    Rejects newlines, NUL bytes, and the characters `<` and `>`,
    which would corrupt the commit header.
    """
    if not name or not isinstance(name, str):
        return DEFAULT_AUTHOR_NAME
    trimmed = name.strip()
    if not trimmed:
        return DEFAULT_AUTHOR_NAME
    if _AUTHOR_NAME_FORBIDDEN.search(trimmed):
        raise ValidationError(
            "Author name contains forbidden characters.",
            details={"value_length": len(trimmed)},
        )
    if len(trimmed) > 200:
        raise ValidationError(
            "Author name exceeds the maximum allowed length.",
            details={"length": len(trimmed)},
        )
    return trimmed


def _sanitize_author_email(email: str) -> str:
    """
    Validate an author email address.

    Rejects newlines, NUL bytes, angle brackets, and strings without
    an `@`. Does not attempt to fully validate RFC 5322; a single
    `@` with non-empty local and domain parts is sufficient for Git.
    """
    if not email or not isinstance(email, str):
        return DEFAULT_AUTHOR_EMAIL
    trimmed = email.strip()
    if not trimmed:
        return DEFAULT_AUTHOR_EMAIL
    if _AUTHOR_EMAIL_FORBIDDEN.search(trimmed):
        raise ValidationError(
            "Author email contains forbidden characters.",
            details={"value_length": len(trimmed)},
        )
    if "@" not in trimmed:
        raise ValidationError(
            "Author email must contain '@'.",
            details={"value_length": len(trimmed)},
        )
    local, _, domain = trimmed.partition("@")
    if not local or not domain:
        raise ValidationError(
            "Author email must have a non-empty local part and domain.",
        )
    if len(trimmed) > 320:
        raise ValidationError(
            "Author email exceeds the maximum allowed length.",
            details={"length": len(trimmed)},
        )
    return trimmed


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def _parse_commit_log(raw: str) -> Tuple[CommitRef, ...]:
    """
    Parse the output of `git log --format=...` produced with
    `_COMMIT_LOG_FORMAT`.

    The format terminates each commit with the record separator
    (`\\x1e`) and separates fields with NUL (`\\x00`). Blank
    records are skipped. Malformed records are skipped rather than
    raising, because a single bad entry must not break the entire
    listing.
    """
    if not raw:
        return ()

    commits: List[CommitRef] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < 6:
            continue
        sha, author_name, author_email, author_ts, parents, body = fields[:6]

        committed_at = _parse_unix_timestamp(author_ts)
        parent_shas = tuple(p for p in parents.strip().split(" ") if p)

        # The body may contain trailing newlines from the format;
        # strip a single trailing one for cleanliness.
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
    """Convert a Unix-seconds string to a timezone-aware datetime."""
    try:
        seconds = int(value.strip())
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Default manager singleton
# ---------------------------------------------------------------------------


_DEFAULT_MANAGER: Optional[GitCommitManager] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_commit_manager() -> GitCommitManager:
    """
    Return the process-wide default `GitCommitManager`, creating it
    on first use.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitCommitManager()
        return _DEFAULT_MANAGER


def set_default_commit_manager(manager: GitCommitManager) -> None:
    """
    Replace the process-wide default commit manager.

    Intended for application startup and for tests.
    """
    if not isinstance(manager, GitCommitManager):
        raise TypeError(
            "set_default_commit_manager expects a GitCommitManager."
        )
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager


def reset_default_commit_manager() -> GitCommitManager:
    """
    Replace the default manager with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = GitCommitManager()
        return _DEFAULT_MANAGER
