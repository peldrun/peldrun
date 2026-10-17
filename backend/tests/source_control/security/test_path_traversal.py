# backend/tests/source_control/security/test_path_traversal.py
"""
Security regression tests for path traversal defenses.

These tests attempt every path escape an attacker might try against
the workspace policy, and verify that the policy rejects the attempt
before any filesystem operation occurs.

The threat model:
    A repository, a compromised tool, or a malicious agent submits a
    path that resolves to a location outside the workspace root.
    Once outside, the attacker can read, overwrite, or delete files
    the user did not intend to expose.

The defense layers covered by this module:

    1. `WorkspacePathPolicy.resolve_within_root`: the single
       choke point through which every workspace path passes. It
       rejects `..` traversal, absolute paths outside the root,
       bidi characters, control characters, over-length paths, and
       symlinks that escape the root.
    2. `WorkspacePathPolicy.is_within_root`: the boolean form of
       the same check. It MUST return False for every escape.
    3. `WorkspacePathPolicy.to_relative_path`: converts an
       absolute path inside the root to a relative POSIX path. It
       MUST raise for paths outside the root.
    4. `sanitize_component`: builds a filesystem-safe directory
       name. It strips bidi and control characters, replaces
       illegal characters with `_`, and converts `.` and `..` to
       safe placeholders.
    5. `sanitize_repository_name`: composes components into a
       directory name, adding a hash suffix when the result would
       exceed the configured maximum length.
    6. Branch name validation: rejects `.lock` suffixes and
       over-length names, mirroring `git check-ref-format`.
    7. `secure_resolve_path`: uses the process-wide default policy
       so that the same rules apply to code paths that do not hold
       a policy instance.
"""

from __future__ import annotations

import os

import pytest

# Resilient imports: relative form when pytest is run with
# `rootdir=backend`, absolute form when the repository root is on
# sys.path.
try:
    from ....integrations.source_control.errors import (
        PathTraversalError,
        ValidationError,
    )
    from ....integrations.source_control.git.path_policy import (
        MAX_PATH_LENGTH,
        WorkspacePathPolicy,
        get_default_path_policy,
        is_valid_branch_name,
        sanitize_component,
        sanitize_repository_name,
        secure_resolve_path,
        set_default_path_policy,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.errors import (
        PathTraversalError,
        ValidationError,
    )
    from backend.integrations.source_control.git.path_policy import (
        MAX_PATH_LENGTH,
        WorkspacePathPolicy,
        get_default_path_policy,
        is_valid_branch_name,
        sanitize_component,
        sanitize_repository_name,
        secure_resolve_path,
        set_default_path_policy,
    )


@pytest.fixture
def policy(tmp_path):
    """
    Return a `WorkspacePathPolicy` rooted at a per-test temporary
    directory. The directory is automatically cleaned up by pytest.
    """
    return WorkspacePathPolicy(root=tmp_path)


# ---------------------------------------------------------------------
# 1. Parent traversal and absolute paths outside the root
# ---------------------------------------------------------------------


def test_parent_traversal_rejected(policy: WorkspacePathPolicy) -> None:
    """
    ATTACK: Submit `../outside` to try to escape the workspace root.

    EXPECTED: `PathTraversalError` is raised. No filesystem
    operation is attempted.
    """
    with pytest.raises(PathTraversalError):
        policy.resolve_within_root("../outside")


def test_deep_parent_traversal_rejected(
    policy: WorkspacePathPolicy,
) -> None:
    """
    ATTACK: Submit a chain of `..` segments to walk far above the
    root.

    EXPECTED: `PathTraversalError` is raised.
    """
    with pytest.raises(PathTraversalError):
        policy.resolve_within_root("a/b/c/../../../../../outside")


def test_absolute_path_outside_root_rejected(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    ATTACK: Submit an absolute path that is outside the workspace
    root.

    EXPECTED: `PathTraversalError` is raised.
    """
    # A sibling of tmp_path, definitively outside it.
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)

    with pytest.raises(PathTraversalError):
        policy.resolve_within_root(str(outside))


def test_relative_inside_root_accepted(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    Sanity check: a normal relative path inside the root is
    accepted. This guards against an overzealous validation that
    would break legitimate workspace operations.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")

    result = policy.resolve_within_root("src/main.py")
    assert result == (tmp_path / "src" / "main.py").resolve()


def test_absolute_path_inside_root_accepted(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    Sanity check: an absolute path that is already inside the root
    is accepted. This is required by the tools that build absolute
    paths from the workspace directory.
    """
    (tmp_path / "file.txt").write_text("hi")
    absolute = str((tmp_path / "file.txt").resolve())

    result = policy.resolve_within_root(absolute)
    assert result == (tmp_path / "file.txt").resolve()


# ---------------------------------------------------------------------
# 2. Forbidden characters
# ---------------------------------------------------------------------


def test_bidi_characters_rejected(policy: WorkspacePathPolicy) -> None:
    """
    ATTACK: Embed a bidi override character (`\\u202e`) in a path to
    spoof the display of a filename.

    EXPECTED: `PathTraversalError` is raised. The path is rejected
    before any display or resolution.
    """
    for bidi in ("\u202a", "\u202e", "\u2066", "\u2069", "\u200e"):
        with pytest.raises(PathTraversalError):
            policy.resolve_within_root(f"file{bidi}.txt")


@pytest.mark.parametrize(
    "control_char",
    ["\x00", "\x01", "\x1f", "\x7f"],
)
def test_control_characters_rejected(
    policy: WorkspacePathPolicy, control_char: str
) -> None:
    """
    ATTACK: Embed a control character in a path. Control characters
    can confuse log parsers, truncate argv on POSIX, and hide the
    true extent of a filename in a UI.

    EXPECTED: `PathTraversalError` is raised.
    """
    with pytest.raises(PathTraversalError):
        policy.resolve_within_root(f"file{control_char}name.txt")


# ---------------------------------------------------------------------
# 3. Length and symlink boundaries
# ---------------------------------------------------------------------


def test_path_exceeding_max_length_rejected(tmp_path) -> None:
    """
    ATTACK: Submit a path longer than `max_path_length`.

    EXPECTED: `PathTraversalError` is raised. The check runs on
    `str(resolved)` so the total length (root + relative) is what
    matters.
    """
    short_policy = WorkspacePathPolicy(
        root=tmp_path, max_path_length=50
    )
    long_name = "a" * 100  # far exceeds the 50-char limit

    with pytest.raises(PathTraversalError):
        short_policy.resolve_within_root(long_name)


def test_symlink_escape_rejected(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    ATTACK: Create a symlink inside the workspace that points
    outside, then read a file through it.

    EXPECTED: `PathTraversalError` is raised. `resolve_within_root`
    follows the symlink and detects that the resolved target is
    outside the root.
    """
    outside = tmp_path.parent / "outside_symlink_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("top-secret\n")

    escape_link = tmp_path / "escape"
    try:
        escape_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not supported on this platform.")

    with pytest.raises(PathTraversalError):
        policy.resolve_within_root("escape/secret.txt")


def test_symlink_rejected_when_follow_disabled(tmp_path) -> None:
    """
    ATTACK: Same as above, but the policy is configured to NEVER
    follow symlinks.

    EXPECTED: `PathTraversalError` is raised. The check compares
    the lexical and resolved paths and rejects any difference.
    """
    outside = tmp_path.parent / "outside_symlink_target2"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("top-secret\n")

    escape_link = tmp_path / "escape2"
    try:
        escape_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not supported on this platform.")

    strict_policy = WorkspacePathPolicy(
        root=tmp_path, follow_symlinks=False
    )
    with pytest.raises(PathTraversalError):
        strict_policy.resolve_within_root("escape2/secret.txt")


# ---------------------------------------------------------------------
# 4. is_within_root predicate
# ---------------------------------------------------------------------


def test_is_within_root_returns_false_for_escape(
    policy: WorkspacePathPolicy,
) -> None:
    """
    INVARIANT: `is_within_root` returns False for every path that
    would escape the root. It never raises; it converts a
    `PathTraversalError` into False.
    """
    assert policy.is_within_root("../outside") is False
    assert policy.is_within_root("/etc/passwd") is False
    assert policy.is_within_root("file\u202e.txt") is False


def test_is_within_root_returns_true_for_inside(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    INVARIANT: `is_within_root` returns True for a legitimate path
    inside the root.
    """
    (tmp_path / "file.txt").write_text("hi")
    assert policy.is_within_root("file.txt") is True


# ---------------------------------------------------------------------
# 5. to_relative_path
# ---------------------------------------------------------------------


def test_to_relative_path_inside(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    Sanity check: a file inside the root converts to a
    POSIX-relative path.
    """
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "file.txt").write_text("hi")

    absolute = str(tmp_path / "subdir" / "file.txt")
    relative = policy.to_relative_path(absolute)
    assert str(relative) == "subdir/file.txt"


def test_to_relative_path_outside_raises(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    INVARIANT: `to_relative_path` raises `PathTraversalError` for a
    path outside the root. It MUST NOT silently return a bogus
    relative path.
    """
    outside = tmp_path.parent / "outside_rel"
    outside.mkdir(exist_ok=True)

    with pytest.raises(PathTraversalError):
        policy.to_relative_path(str(outside))


# ---------------------------------------------------------------------
# 6. sanitize_component
# ---------------------------------------------------------------------


def test_sanitize_component_replaces_illegal_chars() -> None:
    """
    INVARIANT: illegal characters are replaced with `_`. The output
    is always filesystem-safe.
    """
    assert sanitize_component("a<b>c") == "a_b_c"
    assert sanitize_component("a;b") == "a_b"
    assert sanitize_component("a|b") == "a_b"
    assert sanitize_component("a?b") == "a_b"
    assert sanitize_component("a*b") == "a_b"


def test_sanitize_component_handles_dot() -> None:
    """
    INVARIANT: `.` and `..` are converted to safe placeholders so
    that a malicious name cannot escape its parent directory when
    used as a component of a path.
    """
    assert sanitize_component(".") == "_"
    assert sanitize_component("..") == "__"


def test_sanitize_component_strips_bidi() -> None:
    """
    INVARIANT: bidi and control characters are removed outright
    (not replaced), so they cannot survive into a filesystem name.
    """
    result = sanitize_component("file\u202e.txt")
    assert "\u202e" not in result
    # The rest of the name is preserved.
    assert "file" in result
    assert "txt" in result


# ---------------------------------------------------------------------
# 7. sanitize_repository_name
# ---------------------------------------------------------------------


def test_sanitize_repository_name_combined() -> None:
    """
    INVARIANT: when a namespace is provided, the result is
    `namespace--name`.
    """
    assert sanitize_repository_name(
        namespace="peldrun", name="repo"
    ) == "peldrun--repo"
    assert sanitize_repository_name(
        namespace=None, name="repo"
    ) == "repo"


def test_sanitize_repository_name_truncation() -> None:
    """
    INVARIANT: when the composed name exceeds `max_length`, a hash
    suffix is appended so the result remains unique.
    """
    long_name = "a" * 200
    result = sanitize_repository_name(
        namespace=None, name=long_name, max_length=60
    )
    assert len(result) <= 60
    assert result  # not empty


# ---------------------------------------------------------------------
# 8. Branch name edge cases
# ---------------------------------------------------------------------


def test_branch_name_rejects_lock_suffix() -> None:
    """
    ATTACK: `feature.lock` looks like a lock file. Git rejects it.

    EXPECTED: `is_valid_branch_name` returns False.
    """
    assert is_valid_branch_name("feature.lock") is False


def test_branch_name_rejects_over_length() -> None:
    """
    ATTACK: A branch name longer than `MAX_BRANCH_NAME_LENGTH`.

    EXPECTED: `is_valid_branch_name` returns False.
    """
    assert is_valid_branch_name("a" * 201) is False


# ---------------------------------------------------------------------
# 9. must_exist semantics
# ---------------------------------------------------------------------


def test_must_exist_raises_when_missing(
    policy: WorkspacePathPolicy,
) -> None:
    """
    INVARIANT: `resolve_within_root(..., must_exist=True)` raises
    `ValidationError` when the target does not exist.
    """
    with pytest.raises(ValidationError):
        policy.resolve_within_root(
            "does_not_exist.txt", must_exist=True
        )


def test_require_existence_policy(tmp_path) -> None:
    """
    INVARIANT: a policy constructed with `require_existence=True`
    applies the same check on every call.
    """
    strict = WorkspacePathPolicy(
        root=tmp_path, require_existence=True
    )
    with pytest.raises(ValidationError):
        strict.resolve_within_root("does_not_exist.txt")


# ---------------------------------------------------------------------
# 10. secure_resolve_path (default policy)
# ---------------------------------------------------------------------


def test_secure_resolve_path_uses_default_policy(tmp_path) -> None:
    """
    INVARIANT: `secure_resolve_path` uses the process-wide default
    policy so that all callers share the same rules. This test
    installs a test policy, exercises the helper, and restores the
    previous default.
    """
    original = get_default_path_policy()
    try:
        set_default_path_policy(WorkspacePathPolicy(root=tmp_path))
        (tmp_path / "file.txt").write_text("hi")

        result = secure_resolve_path("file.txt", must_exist=True)
        assert result == (tmp_path / "file.txt").resolve()

        # And it MUST reject escapes the same way.
        with pytest.raises(PathTraversalError):
            secure_resolve_path("../outside")
    finally:
        set_default_path_policy(original)


# ---------------------------------------------------------------------
# 11. Path separator normalization
# ---------------------------------------------------------------------


def test_backslashes_normalized(
    policy: WorkspacePathPolicy, tmp_path
) -> None:
    """
    INVARIANT: backslashes in the input are normalized to forward
    slashes before resolution. This makes Windows-style paths
    behave identically on all platforms and prevents a backslash
    from being treated as an escape.

    Note: this normalization is documented in the policy. On a
    Unix system, a file literally named `a\\b` would be affected;
    this is an accepted trade-off for cross-platform
    predictability.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("hi")

    result = policy.resolve_within_root(r"src\main.py")
    assert result == (tmp_path / "src" / "main.py").resolve()
