# backend/integrations/source_control/git/tag.py
"""
Git tag manager for the Source Control Integration subsystem.

This module handles LOCAL tag operations on the workspace's Git
repository. It is deliberately limited to local operations:

    - Lightweight tags (``git tag <name>``).
    - Annotated tags   (``git tag -a <name> -m <msg>``).
    - Deletion         (``git tag -d <name>``).

What this module does NOT do:

    - It does NOT push tags to the remote. Pushing a tag is a remote
      write and is deliberately NOT part of this manager: it would
      require the same approval gate the branch-push flow uses.
      A "push tag" operation can be added in a later phase, with its
      own approval workflow.
    - It does NOT create GitHub Releases. Releases are a GitHub
      concept that combines a tag with release notes and assets;
      they live in ``providers/github/releases.py`` and require an
      API call, not a Git command.

Design rules:
    - Every path is validated through the workspace path policy.
    - Lightweight and annotated tags are distinguished via
      ``git for-each-ref``; the ``objecttype`` field is ``tag``
      for an annotated tag and ``commit`` for a lightweight one.
    - Deleting a tag is not destructive in the sense of losing
      commits — a tag is a pointer. The operation is therefore
      not gated by a force flag. It IS gated by the policy
      engine at the facade layer.
    - The manager is stateless and safe to share across
      coroutines.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

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
    "GitTagManager",
    "TagRef",
    "get_default_tag_manager",
    "set_default_tag_manager",
    "reset_default_tag_manager",
    "is_valid_tag_name",
    "MAX_TAGS_RETURNED",
    "MAX_TAG_NAME_LENGTH",
]


logger = logging.getLogger(__name__)


#: Maximum number of tags returned by a single `list_tags` call.
#: A repository with more tags than this is unusual; the cap
#: prevents unbounded memory use on pathological inputs.
MAX_TAGS_RETURNED: int = 5_000

#: Maximum length of a tag name. Git itself allows longer names,
#: but we cap at a value that is comfortable for the UI and that
#: remains below every filesystem limit.
MAX_TAG_NAME_LENGTH: int = 200

#: Forbidden characters in a tag name, mirroring Git's
#: `check-ref-format` rules for the `refs/tags/` namespace. The
#: character class below intentionally excludes whitespace, `~`,
#: `^`, `:`, `?`, `*`, `[`, and `\\`.
_FORBIDDEN_TAG_CHARS = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\]\\]")

#: Forbidden sequences in a tag name.
_FORBIDDEN_TAG_SEQUENCES = ("..", "@{")

#: Format used by `git for-each-ref` to enumerate tags in one pass.
#:
#: Fields (in order, NUL-separated):
#:     refname:short   - tag name
#:     objectname      - SHA of the tag object (annotated) or commit (lightweight)
#:     objecttype      - "tag" for annotated, "commit" for lightweight
#:     *objectname     - underlying commit SHA for annotated; empty otherwise
#:     taggername      - tagger name for annotated; empty otherwise
#:     taggeremail     - tagger email for annotated; empty otherwise
#:     taggerdate:iso8601 - tagger date for annotated; empty otherwise
#:     contents:subject - first line of the annotation message
_TAG_LIST_FORMAT = (
    "%(refname:short)%00"
    "%(objectname)%00"
    "%(objecttype)%00"
    "%(*objectname)%00"
    "%(taggername)%00"
    "%(taggeremail)%00"
    "%(taggerdate:iso8601)%00"
    "%(contents:subject)%1e"
)


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagRef:
    """A local Git tag.

    ``sha`` is always the SHA of the underlying COMMIT, never the
    SHA of an annotated tag object. Callers that need the tag
    object's SHA (rare) can derive it from ``is_annotated`` plus
    the repository's object database; the commit SHA is what
    users and the UI actually care about.

    Fields:
        name:         The tag name (short form, no `refs/tags/`).
        sha:          The commit SHA the tag points to.
        is_annotated: True for annotated tags, False for
                      lightweight ones.
        message:      The annotation message (annotated only).
        tagger_name:  The tagger's name (annotated only).
        tagger_email: The tagger's email (annotated only).
        tagged_at:    The tagger's timestamp (annotated only).
    """

    name: str
    sha: str
    is_annotated: bool = False
    message: Optional[str] = None
    tagger_name: Optional[str] = None
    tagger_email: Optional[str] = None
    tagged_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitTagManager:
    """Local tag operations backed by the Git CLI.

    The manager holds only immutable references to the Git
    executable and the path policy. It is safe to share across
    coroutines.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()

    # --- Public API ----------------------------------------------------

    async def list_tags(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        max_tags: int = MAX_TAGS_RETURNED,
    ) -> Sequence[TagRef]:
        """List every local tag in the workspace, sorted by name.

        Annotated and lightweight tags are both returned, in a
        single alphabetical list. A tag whose annotation message
        contains a RECORD SEPARATOR (`\\x1e`) is handled correctly
        because the format uses RS as the record separator and the
        parser splits on it before examining fields.

        Returns an empty tuple for a repository with no tags.
        """
        if max_tags <= 0:
            raise ValidationError("max_tags must be positive.")
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        result = await self._git.run(
            [
                "for-each-ref",
                f"--format={_TAG_LIST_FORMAT}",
                "refs/tags",
            ],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            # An empty repository can still have no tags but a
            # non-zero exit code typically means "not a git
            # repository" or "corrupt objects". Surface that as a
            # clear error rather than an empty list.
            raise CommandFailedError(
                "Could not list tags.",
                details={
                    "returncode": result.returncode,
                    "stderr": (result.stderr or "")[:300],
                },
            )
        return _parse_tag_list(result.stdout, max_tags=max_tags)

    async def get_tag(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        name: str,
    ) -> TagRef:
        """Return a single tag by name.

        Raises:
            ValidationError: If `name` is not a valid tag name.
            NotFoundError: If the tag does not exist.
        """
        if not is_valid_tag_name(name):
            raise ValidationError(
                f"Invalid tag name: {name!r}.",
                details={"tag_name": name},
            )
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        result = await self._git.run(
            [
                "for-each-ref",
                f"--format={_TAG_LIST_FORMAT}",
                f"refs/tags/{name}",
            ],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise NotFoundError(
                f"Tag '{name}' was not found.",
                details={"tag_name": name},
            )
        tags = _parse_tag_list(result.stdout, max_tags=1)
        if not tags:
            raise NotFoundError(
                f"Tag '{name}' was not found.",
                details={"tag_name": name},
            )
        return tags[0]

    async def create_tag(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        name: str,
        message: Optional[str] = None,
        target_sha: Optional[str] = None,
        force: bool = False,
    ) -> TagRef:
        """Create a local tag.

        A tag with a non-empty `message` is created as an ANNOTATED
        tag (`git tag -a`). A tag without a message is created as a
        LIGHTWEIGHT tag (`git tag`). This matches the behavior of
        the GitHub UI's "Create a tag" action when no release
        message is provided.

        Args:
            name: The tag name. Must satisfy `is_valid_tag_name`.
            message: Annotation message. When provided, the tag is
                annotated. Empty/None produces a lightweight tag.
            target_sha: The commit to tag. Defaults to HEAD.
            force: When True, overwrite an existing tag. When False
                (default), an existing tag causes a ConflictError.

        Raises:
            ValidationError: For malformed inputs.
            ConflictError: If the tag exists and `force` is False.
            NotFoundError: If `target_sha` is not a valid commit.
        """
        if not is_valid_tag_name(name):
            raise ValidationError(
                f"Invalid tag name: {name!r}.",
                details={"tag_name": name},
            )
        if target_sha is not None and not target_sha.strip():
            target_sha = None
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        if not force:
            exists = await self._tag_exists(target, name)
            if exists:
                raise ConflictError(
                    f"Tag '{name}' already exists.",
                    details={"tag_name": name},
                )

        has_message = bool(message and message.strip())

        args: List[str] = []
        if has_message:
            # Annotated tag. Supply an identity inline so a
            # deployment without `~/.gitconfig` still works.
            args.extend(
                [
                    "-c",
                    "user.name=PELDRUN",
                    "-c",
                    "user.email=peldrun@localhost",
                    "tag",
                ]
            )
            if force:
                args.append("-f")
            args.append("-a")
            args.append(name)
            args.append("-m")
            args.append(message or "")
        else:
            args.append("tag")
            if force:
                args.append("-f")
            args.append(name)

        if target_sha:
            args.append(target_sha)

        result = await self._git.run(
            args,
            cwd=target,
            check_returncode=False,
            timeout_seconds=60,
        )
        if result.returncode != 0:
            stderr_lower = (result.stderr or "").lower()
            if "not a valid object name" in stderr_lower:
                raise NotFoundError(
                    f"Commit '{target_sha}' was not found.",
                    details={"target_sha": target_sha},
                )
            raise CommandFailedError(
                f"Could not create tag '{name}'.",
                details={"stderr": (result.stderr or "")[:300]},
            )

        # Read back the tag we just created. This also normalizes
        # the SHA to the underlying commit, which is what callers
        # expect regardless of whether the tag is annotated or
        # lightweight.
        return await self.get_tag(
            workspace_id, workspace_path=target, name=name
        )

    async def delete_tag(
        self,
        workspace_id: str,
        *,
        workspace_path: Union[str, Path],
        name: str,
    ) -> None:
        """Delete a local tag.

        The operation is idempotent at the error level: deleting a
        tag that does not exist raises `NotFoundError`, which the
        facade can translate to a 404 for the UI. It is NOT
        silently ignored because the caller should know the tag
        was already gone.

        Raises:
            ValidationError: If `name` is not a valid tag name.
            NotFoundError: If the tag does not exist.
        """
        if not is_valid_tag_name(name):
            raise ValidationError(
                f"Invalid tag name: {name!r}.",
                details={"tag_name": name},
            )
        target = self._resolve_workspace_path(workspace_path, workspace_id)

        result = await self._git.run(
            ["tag", "-d", name],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            stderr_lower = (result.stderr or "").lower()
            if "not found" in stderr_lower or "unknown" in stderr_lower:
                raise NotFoundError(
                    f"Tag '{name}' was not found.",
                    details={"tag_name": name},
                )
            raise CommandFailedError(
                f"Could not delete tag '{name}'.",
                details={"stderr": (result.stderr or "")[:300]},
            )

    # --- Internal helpers ----------------------------------------------

    async def _tag_exists(self, target: Path, name: str) -> bool:
        """Return True if a tag with `name` exists."""
        result = await self._git.run(
            ["show-ref", "--verify", "--quiet", f"refs/tags/{name}"],
            cwd=target,
            check_returncode=False,
        )
        return result.returncode == 0

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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_tag_list(
    raw: str, *, max_tags: int
) -> Tuple[TagRef, ...]:
    """Parse the output of `git for-each-ref --format=...`.

    The format uses NUL as the field separator and RS (`\\x1e`) as
    the record separator. Blank records are skipped.
    """
    if not raw:
        return ()

    tags: List[TagRef] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split("\x00")
        # The last field is empty because the format ends with RS
        # after the last NUL; drop trailing empties but keep the
        # positional mapping.
        if len(fields) < 8:
            continue

        (
            name,
            objectname,
            objecttype,
            deref_objectname,
            taggername,
            taggeremail,
            taggerdate,
            subject,
        ) = fields[:8]

        name = name.strip()
        if not name:
            continue

        is_annotated = objecttype.strip() == "tag"

        # The commit SHA is `*objectname` for an annotated tag and
        # `objectname` for a lightweight one. If `*objectname` is
        # empty for an annotated tag (should not happen in
        # practice), fall back to `objectname`.
        if is_annotated:
            sha = (deref_objectname or objectname).strip()
        else:
            sha = objectname.strip()

        # Normalize tagger email; git emits `<user@example.com>` for
        # the tagger form, so strip the brackets if present.
        clean_email: Optional[str] = None
        email_raw = taggeremail.strip()
        if email_raw:
            clean_email = email_raw.strip("<>") or None

        tags.append(
            TagRef(
                name=unicodedata.normalize("NFC", name),
                sha=sha,
                is_annotated=is_annotated,
                message=subject.strip() or None,
                tagger_name=taggername.strip() or None,
                tagger_email=clean_email,
                tagged_at=_parse_iso(taggerdate.strip()),
            )
        )
        if len(tags) >= max_tags:
            break

    # `for-each-ref` returns entries sorted lexicographically by
    # refname, which is the same order the UI wants. No re-sort.
    return tuple(tags)


def _parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp from `for-each-ref`.

    An empty string (lightweight tags) returns None. Any parse
    failure also returns None: a malformed tagger date must not
    abort the listing.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Tag name validation
# ---------------------------------------------------------------------------


def is_valid_tag_name(name: str) -> bool:
    """Return True if `name` is a valid Git tag name.

    Mirrors the relevant subset of `git check-ref-format` for the
    `refs/tags/` namespace:

        - Non-empty, no leading or trailing whitespace.
        - No leading `-`, `.`, or `/`.
        - No trailing `.`, `/`, or `.lock`.
        - No `..` or `@{` sequences.
        - No control characters, spaces, `~`, `^`, `:`, `?`, `*`,
          `[`, or `\\`.
        - Length <= MAX_TAG_NAME_LENGTH.
    """
    if not name or not isinstance(name, str):
        return False
    normalized = unicodedata.normalize("NFC", name)
    if len(normalized) > MAX_TAG_NAME_LENGTH:
        return False
    if normalized != normalized.strip():
        return False
    if normalized[0] in "-./":
        return False
    if normalized.endswith(("/", ".")):
        return False
    if normalized.endswith(".lock"):
        return False
    if any(seq in normalized for seq in _FORBIDDEN_TAG_SEQUENCES):
        return False
    if _FORBIDDEN_TAG_CHARS.search(normalized):
        return False
    if set(normalized) <= {".", "/"}:
        return False
    return True


# ---------------------------------------------------------------------------
# Default manager singleton
# ---------------------------------------------------------------------------


_DEFAULT_MANAGER: Optional[GitTagManager] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_tag_manager() -> GitTagManager:
    """Return the process-wide default `GitTagManager`."""
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitTagManager()
        return _DEFAULT_MANAGER


def set_default_tag_manager(manager: GitTagManager) -> None:
    """Replace the process-wide default tag manager."""
    if not isinstance(manager, GitTagManager):
        raise TypeError(
            "set_default_tag_manager expects a GitTagManager."
        )
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager


def reset_default_tag_manager() -> GitTagManager:
    """Replace the default manager with a fresh one."""
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = GitTagManager()
        return _DEFAULT_MANAGER
