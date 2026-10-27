# backend/integrations/source_control/git/path_policy.py
"""
Workspace path policy for the Source Control Integration subsystem.

This module defines the rules that every path crossing into the Git
engine MUST satisfy. Its purpose is to prevent path traversal,
symlink escape, and filesystem injection from any source, including
the language model.

The central abstraction is `WorkspacePathPolicy`. All Git operations
resolve target paths through a policy instance before touching the
filesystem. The policy enforces:

    - Every resolved path is inside the configured workspace root.
    - Symlinks are followed; the resolved target MUST still be inside
      the root. Symlinks that escape the root are rejected.
    - Paths containing `..` segments are normalized and then checked
      against the root.
    - Unicode is normalized to NFC to avoid duplicate entries on
      case-insensitive or normalization-sensitive filesystems.
    - Bidirectional control characters (used in filename spoofing)
      are rejected.
    - Component names are sanitized before being used to build
      workspace directory names.
    - Reserved Windows device names (`CON`, `NUL`, ...) are
      rejected.

Additionally, this module provides Git branch name validation:

    - `is_valid_branch_name` : predicate returning a boolean.
    - `validate_branch_name` : raising variant used by Pydantic
      field validators in the schema layer. It raises a plain
      `ValueError` (not `ValidationError`) because Pydantic v2 only
      catches `ValueError` and `AssertionError`.

The branch-name implementation lives here rather than in
`git/branch.py` to keep the rules co-located with the path rules and
to avoid a circular import: `branch.py` imports from this module, so
this module must not import from `branch.py`.

Design rules:
    - The policy is immutable after construction. The root cannot be
      changed; create a new policy instead.
    - The policy MUST NOT perform any I/O beyond `Path.resolve` and
      `os.path` queries. It never creates, deletes, or opens files.
    - The policy is thread-safe.
    - Errors are `PathTraversalError` or `ValidationError`. No
      `OSError` is propagated to the caller.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Sequence, Union

from ..errors import PathTraversalError, ValidationError

__all__ = [
    # Policy class and helpers
    "WorkspacePathPolicy",
    "get_default_path_policy",
    "set_default_path_policy",
    "reset_default_path_policy",
    "get_workspace_root",
    "make_workspace_id",
    "sanitize_component",
    "sanitize_repository_name",
    # Branch name validation
    "is_valid_branch_name",
    "validate_branch_name",
    "PROTECTED_LOCAL_BRANCH_NAMES",
    "MAX_BRANCH_NAME_LENGTH",
    # Secure path resolution convenience
    "secure_resolve_path",
    # Constants
    "MAX_PATH_LENGTH",
    "MAX_COMPONENT_LENGTH",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants: workspace paths
# ---------------------------------------------------------------------------


#: Maximum total length of an absolute path. Chosen conservatively to
#: remain below the Windows MAX_PATH limit (260) with room for
#: suffixes such as `.git` and `.lock`.
MAX_PATH_LENGTH: int = 200

#: Maximum length of a single path component.
MAX_COMPONENT_LENGTH: int = 100

#: Default workspace root when no environment variable is set. The
#: `~` is expanded at policy construction time.
DEFAULT_WORKSPACE_ROOT: str = "~/.peldrun/workspaces"

#: Environment variable consulted by `get_workspace_root`.
ENV_WORKSPACE_ROOT: str = "PELDRUN_WORKSPACE_ROOT"

#: Reserved device names on Windows. These must never be used as
#: directory or file names because the OS interprets them as devices.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

#: Characters allowed in a sanitized component.
_ALLOWED_COMPONENT_CHARS = re.compile(r"[^A-Za-z0-9._-]")

#: Bidi and formatting characters used in filename spoofing attacks.
#: These are rejected outright.
_BIDI_CHARS = re.compile(
    "[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]"
)

#: Control characters (except tab and newline, which are separately
#: rejected by the component sanitizer).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Constants: branch names
# ---------------------------------------------------------------------------


#: Maximum length of a branch name accepted by the subsystem. Git
#: itself allows longer names, but we cap at a value that is
#: comfortably below every filesystem and UI limit.
MAX_BRANCH_NAME_LENGTH: int = 200


#: Branch names that are treated as protected by default. These
#: cannot be deleted or renamed without `force=True`. The set is
#: duplicated from `git/branch.py` intentionally: `path_policy` is
#: the lower-level module and must not import from `branch.py`.
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


#: Character sequences that Git forbids in a ref name. Mirrored here
#: so that callers receive a clear error before Git runs.
_FORBIDDEN_BRANCH_SEQUENCES = (
    "..",
    "@{",
    "\\",
)

#: Individual characters that Git forbids in a ref name.
_FORBIDDEN_BRANCH_CHARS = re.compile(r"[\x00-\x1f\x7f ~^:?*\[\]]")


def _utc_seed() -> str:
    """
    Return a stable entropy source string.

    Kept separate so tests may monkeypatch it if they need
    deterministic workspace IDs. The returned string is combined with
    `secrets.token_hex` in `make_workspace_id`.
    """
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspacePathPolicy:
    """
    Immutable policy that governs workspace paths.

    Fields:
        root:                    Absolute workspace root directory.
        max_path_length:         Maximum total path length.
        max_component_length:    Maximum length of a single component.
        follow_symlinks:         When True (default), symlinks are
                                 resolved and must remain inside the
                                 root. When False, symlinks are
                                 rejected outright.
        require_existence:       When True, `resolve_within_root`
                                 requires the path to exist. Defaults
                                 to False so that new workspace
                                 directories can be planned before
                                 they are created.
    """

    root: Path
    max_path_length: int = MAX_PATH_LENGTH
    max_component_length: int = MAX_COMPONENT_LENGTH
    follow_symlinks: bool = True
    require_existence: bool = False
    # Additional roots that are considered "inside the workspace" for
    # path-traversal purposes. This is how chat-scoped workspaces
    # (created under `storage/chats/<chat_id>/files/`) are allowed to
    # coexist with the primary workspace root (`~/.peldrun/workspaces`)
    # without disabling the containment check entirely. Every entry
    # MUST be an absolute path; relative entries are resolved against
    # the current process working directory during construction.
    extra_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        # `frozen=True` prevents normal assignment; use
        # `object.__setattr__` only to normalize the root.
        if not isinstance(self.root, Path):
            object.__setattr__(self, "root", Path(self.root))

        root = self.root.expanduser()
        try:
            root = root.resolve(strict=False)
        except (OSError, RuntimeError):
            # If the root cannot be resolved (e.g. permission
            # denied), fall back to the expanded path. Resolution
            # will be retried when a path is actually validated.
            root = root.absolute()

        object.__setattr__(self, "root", root)

        if self.max_path_length <= 0:
            raise ValidationError(
                "max_path_length must be positive.",
                details={"max_path_length": self.max_path_length},
            )
        if self.max_component_length <= 0:
            raise ValidationError(
                "max_component_length must be positive.",
                details={"max_component_length": self.max_component_length},
            )

        # Normalize each extra root the same way the primary root was
        # normalized above: expand `~`, resolve to an absolute path,
        # and drop entries that resolve to the primary root (they are
        # redundant). Duplicate entries are also removed to keep the
        # containment check cheap and deterministic.
        if self.extra_roots:
            normalized_extras: list[Path] = []
            seen: set[str] = set()
            primary_key = str(root)
            for entry in self.extra_roots:
                candidate = (
                    entry if isinstance(entry, Path) else Path(entry)
                ).expanduser()
                try:
                    resolved_extra = candidate.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved_extra = candidate.absolute()
                key = str(resolved_extra)
                if key == primary_key or key in seen:
                    continue
                seen.add(key)
                normalized_extras.append(resolved_extra)
            object.__setattr__(
                self, "extra_roots", tuple(normalized_extras)
            )

    # --- Public API -----------------------------------------------------

    def resolve_within_root(
        self,
        candidate: Union[str, Path],
        *,
        must_exist: Optional[bool] = None,
    ) -> Path:
        """
        Resolve `candidate` to an absolute path inside the workspace
        root, or raise `PathTraversalError`.

        The resolution rules are:

            1. The candidate is normalized to NFC Unicode.
            2. Bidirectional control characters are rejected.
            3. The candidate is interpreted relative to the root
               unless it is already absolute and inside the root.
            4. The candidate is resolved with `Path.resolve` so that
               `..` segments and symlinks are followed.
            5. The resolved path MUST be inside the root.
            6. The total path length MUST not exceed
               `max_path_length`.
            7. When `must_exist` (or the policy's `require_existence`)
               is True, the path MUST already exist.

        Raises:
            ValidationError: If the input is not a `str` or `Path`,
                or if a component name is invalid.
            PathTraversalError: If the resolved path escapes the
                root, or contains a forbidden character.
        """
        raw = self._coerce_candidate(candidate)
        normalized = self._normalize_string(raw)

        if _BIDI_CHARS.search(normalized):
            raise PathTraversalError(
                "Path contains bidirectional control characters.",
                details={"candidate": _safe_repr(normalized)},
            )

        if _CONTROL_CHARS.search(normalized):
            raise PathTraversalError(
                "Path contains control characters.",
                details={"candidate": _safe_repr(normalized)},
            )

        candidate_path = Path(normalized)

        # Interpret relative paths against the root. Absolute paths
        # are permitted only when they are already inside the root.
        if not candidate_path.is_absolute():
            combined = self.root / candidate_path
        else:
            combined = candidate_path

        # Resolve `..` and symlinks. `strict=False` allows paths
        # that do not yet exist, which is needed for workspace
        # creation.
        try:
            resolved = combined.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "Path could not be resolved.",
                details={
                    "candidate": _safe_repr(normalized),
                    "reason": type(exc).__name__,
                },
                cause=exc,
            ) from exc

        # Containment check.
        if not self._is_inside_root(resolved):
            raise PathTraversalError(
                "Resolved path escapes the workspace root.",
                details={
                    "candidate": _safe_repr(normalized),
                    "resolved": _safe_repr(str(resolved)),
                    "root": _safe_repr(str(self.root)),
                },
            )

        # Symlink policy: `resolve` already followed symlinks. We
        # only need an additional check when the policy forbids
        # following them at all, which we approximate by comparing
        # the lexical path to the resolved path.
        if not self.follow_symlinks:
            lexical = os.path.normpath(str(combined))
            if os.path.normpath(str(resolved)) != lexical:
                raise PathTraversalError(
                    "Symlink traversal is not permitted by policy.",
                    details={
                        "candidate": _safe_repr(normalized),
                    },
                )

        # Length checks.
        self._check_path_length(resolved)

        # Existence requirement.
        require_exist = (
            self.require_existence if must_exist is None else must_exist
        )
        if require_exist and not resolved.exists():
            raise ValidationError(
                "Path does not exist.",
                details={"resolved": _safe_repr(str(resolved))},
            )

        return resolved

    def is_within_root(self, candidate: Union[str, Path]) -> bool:
        """
        Return True if `candidate` resolves to a path inside the
        workspace root.

        This method never raises for invalid input; it returns False
        instead. Use `resolve_within_root` when a diagnostic error is
        more useful than a boolean.
        """
        try:
            self.resolve_within_root(candidate)
            return True
        except (PathTraversalError, ValidationError):
            return False

    def to_relative_path(
        self, absolute_path: Union[str, Path]
    ) -> PurePosixPath:
        """
        Convert an absolute path inside the allowed roots to a
        POSIX-style relative path suitable for use with Git.

        The relative path is computed against the root that actually
        contains the input: the primary root (`self.root`) takes
        priority; otherwise each entry in `self.extra_roots` is
        tried. Callers that want to know WHICH root was used should
        compare the returned relative path against their expected
        workspace directory.

        Raises:
            PathTraversalError: If the input is not inside any
                allowed root.
        """
        resolved = self.resolve_within_root(absolute_path)
        candidates = (self.root, *self.extra_roots)
        last_error: Optional[Exception] = None
        for candidate in candidates:
            try:
                relative = resolved.relative_to(candidate)
                parts = relative.parts
                if not parts:
                    return PurePosixPath(".")
                return PurePosixPath(*parts)
            except ValueError as exc:
                last_error = exc
                continue
        # Unreachable when resolve_within_root succeeded; kept for
        # defense in depth.
        raise PathTraversalError(
            "Path is not inside any allowed workspace root.",
            details={"path": _safe_repr(str(resolved))},
            cause=last_error,
        )

    # --- Internals ------------------------------------------------------

    def _coerce_candidate(self, candidate: Union[str, Path]) -> str:
        if isinstance(candidate, Path):
            return str(candidate)
        if isinstance(candidate, str):
            return candidate
        raise ValidationError(
            "Path must be a string or a pathlib.Path.",
            details={"type": type(candidate).__name__},
        )

    @staticmethod
    def _normalize_string(value: str) -> str:
        """
        Normalize a path string.

        Applies Unicode NFC normalization and converts backslashes to
        forward slashes on all platforms. Git uses forward slashes
        internally; keeping a single form simplifies downstream
        comparison.
        """
        if not value:
            return value
        normalized = unicodedata.normalize("NFC", value)
        # Replace Windows separators with POSIX ones.
        normalized = normalized.replace("\\", "/")
        return normalized

    def _is_inside_root(self, resolved: Path) -> bool:
        """
        Return True if `resolved` is inside any of the allowed roots.

        The primary root (`self.root`) is checked first. When it does
        not contain the path, each entry in `self.extra_roots` is
        tried in order. A path is accepted only if it lies within one
        of these roots; anything else is treated as an escape attempt
        and is rejected by `resolve_within_root`.
        """
        try:
            resolved.relative_to(self.root)
            return True
        except ValueError:
            pass
        for extra_root in self.extra_roots:
            try:
                resolved.relative_to(extra_root)
                return True
            except ValueError:
                continue
        return False

    def _check_path_length(self, resolved: Path) -> None:
        text = str(resolved)
        if len(text) > self.max_path_length:
            raise PathTraversalError(
                "Path exceeds the maximum allowed length.",
                details={
                    "length": len(text),
                    "max_length": self.max_path_length,
                },
            )

    def _check_component(self, name: str) -> str:
        """
        Validate and return a single component name.

        Raises `ValidationError` when the component is empty, too
        long, or reserved. The returned value is NFC-normalized.
        """
        if not name:
            raise ValidationError("Path component must not be empty.")
        normalized = unicodedata.normalize("NFC", name)
        if len(normalized) > self.max_component_length:
            raise ValidationError(
                "Path component is too long.",
                details={
                    "length": len(normalized),
                    "max_length": self.max_component_length,
                },
            )
        if normalized.upper() in RESERVED_NAMES:
            raise ValidationError(
                "Path component uses a reserved device name.",
                details={"component": normalized},
            )
        return normalized


# ---------------------------------------------------------------------------
# Workspace ID generation
# ---------------------------------------------------------------------------


def make_workspace_id(
    *,
    provider_hint: Optional[str] = None,
    repository_hint: Optional[str] = None,
    entropy: Optional[str] = None,
) -> str:
    """
    Return a fresh, opaque workspace identifier.

    The identifier is designed to be:

        - Unique: 16 random bytes of entropy.
        - Readable: an optional prefix derived from the provider and
          repository hints, to aid debugging.
        - Filesystem-safe: it contains only `[a-z0-9-]`.

    Args:
        provider_hint: Optional provider name (e.g. `"gh"`).
        repository_hint: Optional repository name. It is sanitized
            and truncated to `MAX_COMPONENT_LENGTH`.
        entropy: Optional entropy override for deterministic tests.
            Production callers SHOULD NOT set this.

    Returns:
        A string of the form `<prefix><hex>`, e.g.
        `peldrun-gh-repo-a1b2c3d4e5f60718`.
    """
    parts = ["peldrun"]

    if provider_hint:
        sanitized_provider = sanitize_component(provider_hint)[:16]
        if sanitized_provider:
            parts.append(sanitized_provider)

    if repository_hint:
        sanitized_repo = sanitize_component(repository_hint)[
            : MAX_COMPONENT_LENGTH // 2
        ]
        if sanitized_repo:
            parts.append(sanitized_repo)

    token = entropy if entropy is not None else _utc_seed()
    parts.append(token)

    identifier = "-".join(parts)

    # Hard cap on the final identifier length.
    if len(identifier) > MAX_COMPONENT_LENGTH:
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
        identifier = f"peldrun-{digest}"

    return identifier


# ---------------------------------------------------------------------------
# Component and repository name sanitization
# ---------------------------------------------------------------------------


def sanitize_component(name: str) -> str:
    """
    Return a filesystem-safe version of `name`.

    All characters outside `[A-Za-z0-9._-]` are replaced with `_`.
    Leading dots are preserved (for hidden files) but the special
    names `.` and `..` become `_` and `__` respectively. Empty input
    yields an empty string.
    """
    if not name:
        return ""
    normalized = unicodedata.normalize("NFC", str(name))
    if _BIDI_CHARS.search(normalized) or _CONTROL_CHARS.search(normalized):
        normalized = _BIDI_CHARS.sub("", normalized)
        normalized = _CONTROL_CHARS.sub("", normalized)
    sanitized = _ALLOWED_COMPONENT_CHARS.sub("_", normalized)
    if sanitized == ".":
        return "_"
    if sanitized == "..":
        return "__"
    return sanitized


def sanitize_repository_name(
    *,
    namespace: Optional[str],
    name: str,
    max_length: int = MAX_COMPONENT_LENGTH,
) -> str:
    """
    Build a human-readable, filesystem-safe repository directory name.

    The result is `"<namespace>--<name>"` when a namespace is
    provided, and `"<name>"` otherwise. Both parts are sanitized with
    `sanitize_component`, and the total is truncated to `max_length`
    characters. When truncation occurs, a short hash suffix is
    appended to preserve uniqueness.
    """
    safe_name = sanitize_component(name)
    if not safe_name:
        safe_name = "repository"

    if namespace:
        safe_namespace = sanitize_component(namespace)
        combined = f"{safe_namespace}--{safe_name}" if safe_namespace else safe_name
    else:
        combined = safe_name

    if len(combined) <= max_length:
        return combined

    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:8]
    # Reserve 9 characters for "-" plus the 8-character digest.
    head_budget = max(1, max_length - 9)
    head = combined[:head_budget]
    return f"{head}-{digest}"


# ---------------------------------------------------------------------------
# Branch name validation
# ---------------------------------------------------------------------------
#
# These functions are the single source of truth for branch name
# validity. `git/branch.py` re-exports them and adds manager methods
# on top. `schemas/requests.py` uses `validate_branch_name` inside
# Pydantic field validators.


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
        - The name cannot consist solely of dots or slashes.
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
    for seq in _FORBIDDEN_BRANCH_SEQUENCES:
        if seq in normalized:
            return False
    if _FORBIDDEN_BRANCH_CHARS.search(normalized):
        return False
    # A branch name must not consist solely of dots or slashes.
    if set(normalized) <= {".", "/"}:
        return False
    return True


def validate_branch_name(name: str) -> str:
    """
    Validate a branch name and return its normalized form.

    This function exists for callers that need a value back (for
    example Pydantic field validators). It raises a plain
    `ValueError`, not `ValidationError`, because Pydantic v2 only
    catches `ValueError` and `AssertionError` inside field
    validators. Callers that want a `SourceControlError` subclass
    SHOULD wrap this call.

    Returns:
        The NFC-normalized, stripped branch name.

    Raises:
        ValueError: If the name is empty, not a string, or fails any
            of the rules enforced by `is_valid_branch_name`.
    """
    if not name or not isinstance(name, str):
        raise ValueError("Branch name must be a non-empty string.")
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("Branch name must not be blank.")
    normalized = unicodedata.normalize("NFC", trimmed)
    if not is_valid_branch_name(normalized):
        raise ValueError(
            f"Invalid branch name: {name!r}. "
            "Branch names must not contain whitespace, control "
            "characters, '..', '@{', '\\\\', '~', '^', ':', '?', "
            "'*', or '[', and must not start with '-', '.', or '/'."
        )
    return normalized


def is_protected_local_branch(name: str) -> bool:
    """
    Return True if `name` is a protected local branch.

    Comparison is case-insensitive, and compound names such as
    `release/1.2` are considered protected when their prefix matches
    a protected name. The function is exposed here so that callers
    (for example the schema layer) can validate branch names without
    importing `git/branch.py`.
    """
    if not name:
        return False
    lowered = name.strip().lower()
    if lowered in PROTECTED_LOCAL_BRANCH_NAMES:
        return True
    prefix = lowered.split("/", 1)[0]
    return prefix in PROTECTED_LOCAL_BRANCH_NAMES


# ---------------------------------------------------------------------------
# Secure path resolution convenience
# ---------------------------------------------------------------------------


def secure_resolve_path(
    candidate: Union[str, Path],
    *,
    must_exist: Optional[bool] = None,
) -> Path:
    """
    Resolve `candidate` against the process-wide default policy.

    This is a convenience wrapper for callers that do not hold a
    `WorkspacePathPolicy` instance. It delegates to
    `WorkspacePathPolicy.resolve_within_root` on the default policy.

    Raises:
        PathTraversalError: If the resolved path escapes the root.
        ValidationError: If the input is malformed or the path does
            not exist (when `must_exist` is True).
    """
    policy = get_default_path_policy()
    return policy.resolve_within_root(candidate, must_exist=must_exist)


# ---------------------------------------------------------------------------
# Workspace root resolution
# ---------------------------------------------------------------------------


def get_workspace_root(
    override: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Return the workspace root directory.

    Resolution order (audit items 2.2 + 2.4):
        1. The `override` argument, when provided.
        2. The `PELDRUN_WORKSPACE_ROOT` environment variable.
        3. `peldrun.storage_paths.get_storage_root() / "workspaces"`
           — the centralized storage root, so standalone workspaces
           live next to chat-scoped workspaces and every other piece
           of persistent state.
        4. `<backend>/storage/workspaces` — bootstrap fallback for
           isolated test runs where `peldrun.storage_paths` is not
           importable.
        5. `~/.peldrun/workspaces` — legacy fallback. Retained only
           so pre-existing workspaces remain discoverable.

    The returned path is expanded and absolute but is NOT created;
    callers that need the directory to exist are responsible for
    creating it.
    """
    if override is not None:
        candidate = Path(override)
    elif os.environ.get(ENV_WORKSPACE_ROOT):
        candidate = Path(os.environ[ENV_WORKSPACE_ROOT])
    else:
        candidate = _resolve_default_workspace_root()

    expanded = candidate.expanduser()
    try:
        return expanded.resolve(strict=False)
    except (OSError, RuntimeError):
        return expanded.absolute()


def _resolve_default_workspace_root() -> Path:
    """
    Resolve the default workspace root, anchored to the centralized
    storage root so all persistent state lives under one umbrella.

    Priority (audit items 2.2 + 2.4):
        1. `peldrun.storage_paths.get_storage_root() / "workspaces"`
           — the canonical, project-root-anchored location.
        2. `<backend>/storage/workspaces` — bootstrap fallback for
           isolated test runs.
        3. `~/.peldrun/workspaces` — legacy fallback so existing
           workspaces in the old location remain accessible.

    Before this function existed, every standalone workspace was
    created under `~/.peldrun/workspaces`, which meant:
        - A repository backup did not include them.
        - Moving the project to another machine lost them.
        - Multi-user hosts isolated them per user.

    Anchoring to the storage root fixes all three problems while
    preserving backward compatibility: the legacy path is still
    checked as a last resort, and `_discover_workspace_on_disk()`
    in `workspace.py` scans both locations during recovery.
    """
    try:
        from peldrun.storage_paths import get_storage_root

        return (get_storage_root() / "workspaces").resolve()
    except Exception:  # noqa: BLE001 — bootstrap fallback
        pass

    try:
        backend_root = Path(__file__).resolve().parent.parent.parent.parent
        return (backend_root / "storage" / "workspaces").resolve()
    except Exception:  # noqa: BLE001 — last resort
        return Path("~/.peldrun/workspaces").expanduser().resolve()

# ---------------------------------------------------------------------------
# Default policy singleton
# ---------------------------------------------------------------------------


_DEFAULT_POLICY: Optional[WorkspacePathPolicy] = None
_DEFAULT_LOCK = threading.RLock()


def _resolve_extra_roots() -> tuple[Path, ...]:
    """
    Return the additional roots that the default policy should accept.

    Chat-scoped workspaces live under the application's storage
    directory (`storage/chats/<chat_id>/files/` or
    `storage/projects/<project_id>/chats/<chat_id>/files/`), not under
    the primary workspace root (`~/.peldrun/workspaces`). Without an
    explicit allowance, every Git operation on a chat workspace would
    fail the containment check with `PathTraversalError`.

    Resolution order for the storage root:
        1. `PELDRUN_STORAGE_ROOT` environment variable, when set.
        2. `omweb.config.settings.storage_dir` (defensive import;
           missing or broken configuration is ignored).
        3. `<backend>/storage/` as a last-resort fallback.

    Only paths that exist on disk are returned; non-existent entries
    are dropped so that the containment check does not accidentally
    accept a path that merely shares a lexical prefix.
    """
    candidates: list[Path] = []

    env_override = os.environ.get("PELDRUN_STORAGE_ROOT")
    if env_override:
        candidates.append(Path(env_override).expanduser())

    # Authoritative source: `project_manager.storage_dir` is the
    # directory the engine passes to the agent as `project_dir`.
    # If the policy's extra_roots do not include it,
    # `_force_remove_directory` refuses to clean up a partially
    # materialized workspace (error "Refusing to remove a directory
    # outside the workspace root"), and `_materialize` sees a
    # different directory than the one the agent edited.
    #
    # IMPORTANT: `project_manager` resolves storage as
    # `<project_root>/storage` (three parents up from
    # `backend/omweb/project_manager.py`), while this file's own
    # fallback resolves it as `<backend>/storage` (four parents up
    # from `backend/integrations/source_control/git/path_policy.py`).
    # The two paths differ; querying project_manager directly is the
    # only way to guarantee a match.
    try:
        from omweb.project_manager import project_manager

        candidates.append(Path(project_manager.storage_dir))
    except Exception:  # noqa: BLE001
        # project_manager is optional in isolated test runs; the
        # config-based lookup and the `<backend>/storage` fallback
        # below still cover the default deployment layout.
        pass

    try:
        import omweb.config as _om_cfg  # type: ignore[import-not-found]
        storage_candidate: Optional[Path] = None
        if hasattr(_om_cfg, "settings") and hasattr(
            _om_cfg.settings, "storage_dir"
        ):
            storage_candidate = Path(_om_cfg.settings.storage_dir)
        elif hasattr(_om_cfg, "STORAGE_DIR"):
            storage_candidate = Path(_om_cfg.STORAGE_DIR)
        elif hasattr(_om_cfg, "get_settings"):
            storage_candidate = Path(_om_cfg.get_settings().storage_dir)
        if storage_candidate is not None:
            candidates.append(storage_candidate.expanduser())
    except Exception:  # noqa: BLE001
        # Configuration is optional; the fallback below covers the
        # default deployment layout.
        pass

    # Fallback: `<backend>/storage`.
    try:
        backend_root = Path(__file__).resolve().parent.parent.parent.parent
        candidates.append((backend_root / "storage").expanduser())
    except Exception:  # noqa: BLE001
        pass

    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved_candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved_candidate = candidate.absolute()
        key = str(resolved_candidate)
        if key in seen:
            continue
        seen.add(key)
        if not resolved_candidate.exists():
            continue
        resolved.append(resolved_candidate)
    return tuple(resolved)


def get_default_path_policy() -> WorkspacePathPolicy:
    """
    Return the process-wide default `WorkspacePathPolicy`, creating
    it on first use.

    The primary root is resolved with `get_workspace_root()`. The
    additional roots are resolved with `_resolve_extra_roots()` so
    that chat-scoped workspaces under `storage/` are also allowed.
    Tests can replace the policy entirely with
    `set_default_path_policy`.
    """
    global _DEFAULT_POLICY
    with _DEFAULT_LOCK:
        if _DEFAULT_POLICY is None:
            _DEFAULT_POLICY = WorkspacePathPolicy(
                root=get_workspace_root(),
                extra_roots=_resolve_extra_roots(),
            )
        return _DEFAULT_POLICY


def set_default_path_policy(policy: WorkspacePathPolicy) -> None:
    """
    Replace the process-wide default policy.

    Intended for application startup and for tests.
    """
    if not isinstance(policy, WorkspacePathPolicy):
        raise TypeError(
            "set_default_path_policy expects a WorkspacePathPolicy."
        )
    global _DEFAULT_POLICY
    with _DEFAULT_LOCK:
        _DEFAULT_POLICY = policy


def reset_default_path_policy(
    root: Optional[Union[str, Path]] = None,
    extra_roots: Optional[Sequence[Union[str, Path]]] = None,
) -> WorkspacePathPolicy:
    """
    Replace the default policy with a fresh one.

    Args:
        root: Primary workspace root. When omitted, resolved via
            `get_workspace_root()`.
        extra_roots: Additional allowed roots. When omitted,
            resolved via `_resolve_extra_roots()`. Pass an empty
            sequence to explicitly disable the extra roots (useful
            for tests that want a strictly single-root policy).

    Returns:
        The newly installed policy.
    """
    global _DEFAULT_POLICY
    resolved_root = (
        get_workspace_root(root) if root is not None else get_workspace_root()
    )
    if extra_roots is None:
        resolved_extras = _resolve_extra_roots()
    else:
        resolved_extras = tuple(
            Path(entry) for entry in extra_roots
        )
    with _DEFAULT_LOCK:
        _DEFAULT_POLICY = WorkspacePathPolicy(
            root=resolved_root,
            extra_roots=resolved_extras,
        )
        return _DEFAULT_POLICY


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_repr(value: str, limit: int = 120) -> str:
    """
    Return a truncated, printable representation of `value`.

    Used inside error details to avoid unbounded error messages when
    the caller supplied a very long string.
    """
    if len(value) <= limit:
        return value
    return value[:limit] + "..."
