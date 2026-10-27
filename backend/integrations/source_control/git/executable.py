# backend/integrations/source_control/git/executable.py
"""
Safe Git subprocess wrapper for the Source Control Integration
subsystem.

This module is the ONLY place in the subsystem permitted to spawn a
Git process. Every Git operation flows through `GitExecutable.run` or
`GitExecutable.run_sync`, which enforce the following invariants:

    - Commands are executed with `asyncio.create_subprocess_exec`
      using an argument array. `shell=True` is never used.
    - The Git binary path is validated to exclude control characters
      and shell-injection sequences. Windows path separators (`\\`)
      are explicitly permitted because they are not interpreted by
      `create_subprocess_exec`.
    - The environment is sanitized: dangerous variables
      (`GIT_TRACE`, `GIT_ASKPASS`, `GIT_CURL_VERBOSE`,
      `GIT_CONFIG_PARAMETERS`, and the SSH equivalents) are removed,
      and safe defaults (`LANG=C`, `GIT_TERMINAL_PROMPT=0`,
      `GIT_CONFIG_NOSYSTEM=1`, `GIT_PAGER=cat`) are injected.
    - Standard output and standard error are capped at
      `MAX_OUTPUT_BYTES` per stream. Outputs that exceed the cap are
      truncated with an explicit marker.
    - Standard output and standard error are redacted with
      `security.redaction.redact_bytes` before being returned.
    - Every command has a timeout. On timeout, the child process is
      killed and `GitTimeoutError` is raised.

Design rules:
    - This module MUST NOT import from any Git domain module
      (`workspace`, `push`, etc.) to avoid circular imports.
    - This module MUST NOT accept caller-supplied environments. The
      environment is entirely controlled here.
    - Errors are raised as `SourceControlError` subclasses so that
      callers can handle them uniformly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple, Union

from ..errors import (
    CommandFailedError,
    CommandTimeoutError,
    ProviderError,
    ValidationError,
)
from ..security.redaction import redact_bytes

__all__ = [
    "GitExecutable",
    "GitResult",
    "GitCommandResult",
    "GitExecutionError",
    "GitTimeoutError",
    "GitNotAvailableError",
    "get_default_git_executable",
    "set_default_git_executable",
    "reset_default_git_executable",
    "MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "NETWORK_TIMEOUT_SECONDS",
    "LONG_OPERATION_TIMEOUT_SECONDS",
    "NETWORK_COMMANDS",
    "LONG_OPERATION_COMMANDS",
    "MINIMUM_GIT_VERSION",
]


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


#: Maximum bytes retained from each of stdout and stderr. Outputs that
#: exceed this limit are truncated with a marker appended.
MAX_OUTPUT_BYTES: int = 5 * 1024 * 1024  # 5 MiB

#: Default subprocess timeout for LOCAL Git commands
#: (status, commit, log, diff, add, reset, branch, checkout, ...).
#: These operations touch only the local repository and should
#: complete in under a minute even on very large trees.
DEFAULT_TIMEOUT_SECONDS: int = 60

#: Timeout for NETWORK-bound Git commands (clone, fetch, push,
#: pull, ls-remote). These commands depend on the remote server
#: and the network path between the client and the server, so a
#: 60-second ceiling routinely kills legitimate operations:
#:
#:   - ``git clone`` of a 1+ GB repository: 3-10 minutes
#:   - ``git fetch`` over a slow corporate VPN: 1-5 minutes
#:   - ``git push`` of a large branch to a distant server: 1-3 minutes
#:
#: 300 seconds covers the vast majority of real-world cases while
#: still bounding a genuinely wedged command. Callers that need a
#: tighter deadline must pass ``timeout_seconds`` explicitly.
NETWORK_TIMEOUT_SECONDS: int = 300

#: Timeout for computationally HEAVY LOCAL Git commands that
#: intentionally scan or rewrite large parts of the object graph:
#:
#:   - ``filter-branch``: rewrites every commit in the history
#:   - ``gc`` / ``repack``: pack objects across the whole repo
#:   - ``fsck``: verify integrity of every object
#:   - ``reflog expire``: walk the whole reflog
#:   - ``prune`` / ``bundle``: scan the object store
#:
#: These are rarely called but can legitimately take several
#: minutes on multi-GB repositories.
LONG_OPERATION_TIMEOUT_SECONDS: int = 600

#: Git subcommands classified as NETWORK-bound. Used by
#: ``GitExecutable._timeout_for_args`` to pick the appropriate
#: default timeout when the caller does not pass one explicitly.
NETWORK_COMMANDS: frozenset[str] = frozenset(
    {"clone", "fetch", "push", "pull", "ls-remote"}
)

#: Git subcommands classified as computationally HEAVY local
#: operations. Used by ``GitExecutable._timeout_for_args``.
LONG_OPERATION_COMMANDS: frozenset[str] = frozenset(
    {"filter-branch", "gc", "repack", "prune", "reflog", "fsck", "bundle"}
)

#: Minimum supported Git version. Older versions do not support
#: `worktree` (2.5) with all the flags we rely on, and they lack
#: `--force-with-lease` semantics we require.
MINIMUM_GIT_VERSION: Tuple[int, int, int] = (2, 20, 0)

#: Shell metacharacters that MUST NOT appear in the Git binary path.
#:
#: IMPORTANT: The backslash character `\` is deliberately EXCLUDED
#: from this set because:
#:
#:     1. It is the Windows path separator, so every Windows Git
#:        installation path (e.g. `C:\Program Files\Git\cmd\git.exe`)
#:        would be rejected.
#:     2. `asyncio.create_subprocess_exec` does NOT interpret the
#:        binary path through a shell, so backslashes carry no
#:        injection risk.
#:
#: The forbidden characters are limited to shell operators, control
#: characters, and quotes — all of which could break logging or mask
#: an attempt to smuggle a shell command through an environment
#: variable.
#: Shell metacharacters that MUST NOT appear in the Git binary
#: path.
#:
#: IMPORTANT: The characters `(`, `)`, `{`, and `}` are deliberately
#: EXCLUDED because:
#:
#:     1. They appear in the standard Windows Git installation path
#:        (`C:\Program Files (x86)\Git\cmd\git.exe`). Rejecting them
#:        would make PELDRUN unusable on 64-bit Windows systems.
#:     2. `asyncio.create_subprocess_exec` does NOT interpret these
#:        characters through a shell, so they carry no injection
#:        risk.
#:
#: The backslash `\\` is also excluded for the same reason (Windows
#: path separator, not a shell escape). See
#: `test_shell_metacharacters_do_not_include_backslash` in the test
#: suite for the regression guard.
_SHELL_METACHARACTERS = re.compile(r"[;&|`$<>\n\r\t\"'*?\[\]]")

#: Environment variables that are removed before spawning Git.
_UNSAFE_ENV_VARS: Tuple[str, ...] = (
    "GIT_TRACE",
    "GIT_TRACE_PACKET",
    "GIT_TRACE_PERFORMANCE",
    "GIT_TRACE_SETUP",
    "GIT_TRACE_SHALLOW",
    "GIT_TRACE_CURL",
    "GIT_TRACE_CURL_NO_DATA",
    "GIT_CURL_VERBOSE",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
    "GIT_HTTP_LOW_SPEED_LIMIT",
    "GIT_HTTP_LOW_SPEED_TIME",
)

#: Environment variables that a caller MAY provide via `extra_env`.
#:
#: The design rule "the environment is entirely controlled here"
#: forbids arbitrary caller-supplied environments, because that
#: would open the door to `LD_PRELOAD`, `GIT_SSH_COMMAND`,
#: `GIT_EXTERNAL_DIFF`, and similar command-execution vectors. The
#: credential flow in `push.py` nevertheless requires exactly two
#: variables to be set: `GIT_ASKPASS` (and its SSH twin
#: `SSH_ASKPASS`), which point at a temporary script that echoes
#: the access token.
#:
#: Restricting `extra_env` to this allowlist preserves the security
#: invariant while enabling the credential flow. Any other key is a
#: programming error and raises `ValidationError` before the
#: subprocess is spawned. Values are still validated for NUL bytes
#: and non-string types.
_EXTRA_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "GIT_ASKPASS",
        "SSH_ASKPASS",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GitNotAvailableError(ProviderError):
    """
    Raised when the Git binary cannot be located or is too old.

    This error is not retryable: the user must install or upgrade
    Git.
    """

    default_retryable = False
    default_http_status = 500


class GitExecutionError(CommandFailedError):
    """
    Raised when a Git command exits with a non-zero status.

    The exception carries the return code and the (redacted,
    truncated) standard error. Callers that need to inspect the
    output SHOULD prefer the returned `GitResult` over the exception.
    """

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        returncode: int,
        stderr: str = "",
        stdout: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(
            message,
            details={
                "command": list(command),
                "returncode": returncode,
            },
            cause=cause,
        )
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class GitTimeoutError(CommandTimeoutError):
    """
    Raised when a Git command exceeds its allotted time.

    The child process is killed before this error is raised.
    """

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        timeout_seconds: int,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(
            message,
            details={
                "command": list(command),
                "timeout_seconds": timeout_seconds,
            },
            cause=cause,
        )
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    """
    Immutable result of a Git command.

    Fields:
        command:     The exact argument array passed to Git, with the
                     Git binary replaced by its basename for logging.
        returncode:  Process exit status.
        stdout:      Redacted, truncated standard output (UTF-8).
        stderr:      Redacted, truncated standard error (UTF-8).
        duration_ms: Wall-clock duration of the command in
                     milliseconds.
        started_at:  Timezone-aware start timestamp.
        truncated:   True if either stream was truncated.
        redacted:    True if either stream was modified by redaction.
    """

    command: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    started_at: datetime = field(default_factory=_utc_now)
    truncated: bool = False
    redacted: bool = False

    @property
    def succeeded(self) -> bool:
        """Return True if the command exited with status 0."""
        return self.returncode == 0

    def stdout_lines(self) -> List[str]:
        """Return stdout split into non-empty lines."""
        return [line for line in self.stdout.splitlines() if line]

    def stderr_lines(self) -> List[str]:
        """Return stderr split into non-empty lines."""
        return [line for line in self.stderr.splitlines() if line]

    def to_dict(self) -> Mapping[str, object]:
        """Serialize the result for logs and audit events."""
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout_len": len(self.stdout),
            "stderr_len": len(self.stderr),
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "redacted": self.redacted,
            "started_at": self.started_at.isoformat(),
        }


# Backward-compatibility alias for callers expecting GitCommandResult.
GitCommandResult = GitResult


# ---------------------------------------------------------------------------
# Git executable
# ---------------------------------------------------------------------------


class GitExecutable:
    """
    Thread-safe wrapper around the Git command-line tool.

    The same instance may be shared across coroutines. Concurrent
    invocations of `run` spawn independent child processes; the
    wrapper itself holds no per-command state.
    """

    def __init__(
        self,
        *,
        git_binary: Optional[str] = None,
        default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        network_timeout_seconds: int = NETWORK_TIMEOUT_SECONDS,
        long_operation_timeout_seconds: int = LONG_OPERATION_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        validate_version: bool = True,
    ) -> None:
        self._git_binary = self._resolve_binary(git_binary)
        self._default_timeout = max(1, int(default_timeout_seconds))
        # Audit report item 5.2: a single 60s ceiling broke legitimate
        # network operations (clone/fetch/push) and heavy local
        # operations (gc/repack/filter-branch). Three separate
        # defaults are now applied based on the subcommand, with
        # caller-supplied timeouts still taking precedence.
        self._network_timeout = max(1, int(network_timeout_seconds))
        self._long_operation_timeout = max(
            1, int(long_operation_timeout_seconds)
        )
        self._max_output_bytes = max(1024, int(max_output_bytes))
        self._version_lock = threading.RLock()
        self._cached_version: Optional[Tuple[int, int, int]] = None

        if validate_version:
            self._check_version()

    # --- Properties -----------------------------------------------------

    @property
    def git_binary(self) -> str:
        """Return the absolute path to the Git executable."""
        return self._git_binary

    @property
    def default_timeout_seconds(self) -> int:
        """Return the default timeout applied to local Git commands."""
        return self._default_timeout

    @property
    def network_timeout_seconds(self) -> int:
        """Return the timeout applied to network-bound Git commands."""
        return self._network_timeout

    @property
    def long_operation_timeout_seconds(self) -> int:
        """Return the timeout applied to heavy local Git commands."""
        return self._long_operation_timeout

    @property
    def max_output_bytes(self) -> int:
        """Return the maximum bytes retained per output stream."""
        return self._max_output_bytes

    def is_available(self) -> bool:
        """Return True if the Git binary is present and executable."""
        return bool(self._git_binary) and os.access(
            self._git_binary, os.X_OK
        )

    def version(self) -> Tuple[int, int, int]:
        """
        Return the Git version as a `(major, minor, patch)` tuple.

        The result is cached after the first successful call.
        """
        with self._version_lock:
            if self._cached_version is not None:
                return self._cached_version
        self._check_version()
        with self._version_lock:
            assert self._cached_version is not None
            return self._cached_version

    # --- Execution ------------------------------------------------------

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Union[str, Path]] = None,
        timeout_seconds: Optional[int] = None,
        check_returncode: bool = True,
        stdin_bytes: Optional[bytes] = None,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> GitResult:
        """
        Run a Git command asynchronously.

        Args:
            args: Arguments passed to Git, WITHOUT the binary itself.
                For example, `["status", "--porcelain"]`.
            cwd: Working directory for the command. When `None`, the
                process inherits the current directory; callers
                SHOULD always provide an explicit workspace path.
            timeout_seconds: Override the default timeout.
            check_returncode: When True (default), a non-zero exit
                status raises `GitExecutionError`.
            stdin_bytes: Optional bytes written to the child's stdin
                before it is closed. Useful for `git apply` and
                similar commands. Must not contain secrets.
            extra_env: Optional allowlisted environment overrides.
                Only keys in `_EXTRA_ENV_ALLOWLIST` are accepted;
                any other key raises `ValidationError`. Used by the
                credential flow in `push.py` to provide
                `GIT_ASKPASS`/`SSH_ASKPASS`.

        Raises:
            ValidationError: If arguments are malformed, or if
                `extra_env` contains a non-allowlisted key.
            GitTimeoutError: If the command exceeds the timeout.
            GitExecutionError: If the command fails and
                `check_returncode` is True.
        """
        self._validate_args(args)
        timeout = self._coerce_timeout(timeout_seconds, args)
        cwd_path = self._validate_cwd(cwd)
        effective_args = self._inject_crlf_configs(args)
        command = (self._git_binary, *effective_args)

        env = self._build_environment(extra_env=extra_env)

        started = time.monotonic()
        started_at = _utc_now()

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd_path) if cwd_path else None,
                env=env,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_bytes is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise GitNotAvailableError(
                f"Git binary not found at '{self._git_binary}'.",
                details={"git_binary": self._git_binary},
                cause=exc,
            ) from exc
        except OSError as exc:
            raise GitExecutionError(
                "Failed to start the Git process.",
                command=command,
                returncode=-1,
                cause=exc,
            ) from exc

        stdout_bytes, stderr_bytes, timed_out = await self._communicate(
            proc, timeout=timeout, stdin_bytes=stdin_bytes
        )

        duration_ms = int((time.monotonic() - started) * 1000)

        if timed_out:
            raise GitTimeoutError(
                f"Git command timed out after {timeout}s.",
                command=command,
                timeout_seconds=timeout,
            )

        stdout_safe, stdout_truncated, stdout_redacted = self._sanitize_stream(
            stdout_bytes
        )
        stderr_safe, stderr_truncated, stderr_redacted = self._sanitize_stream(
            stderr_bytes
        )

        result = GitResult(
            command=self._log_safe_command(command),
            returncode=proc.returncode or 0,
            stdout=stdout_safe,
            stderr=stderr_safe,
            duration_ms=duration_ms,
            started_at=started_at,
            truncated=stdout_truncated or stderr_truncated,
            redacted=stdout_redacted or stderr_redacted,
        )

        if check_returncode and not result.succeeded:
            raise GitExecutionError(
                f"Git command failed with exit status {result.returncode}.",
                command=command,
                returncode=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )

        return result

    async def arun(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Union[str, Path]] = None,
        timeout_seconds: Optional[Union[int, float]] = None,
        timeout: Optional[Union[int, float]] = None,
        check_returncode: bool = True,
        check: Optional[bool] = None,
        stdin_bytes: Optional[bytes] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> GitResult:
        """
        Non-blocking execution of git commands for async agent tools and facade methods.

        Provides compatibility with callers expecting an `arun` interface or
        shorthand parameter names (`timeout`, `check`, `env`).
        """
        effective_timeout = timeout_seconds if timeout_seconds is not None else timeout
        effective_check = check if check is not None else check_returncode
        effective_env = extra_env if extra_env is not None else env

        timeout_int: Optional[int] = (
            int(effective_timeout) if effective_timeout is not None else None
        )

        return await self.run(
            args,
            cwd=cwd,
            timeout_seconds=timeout_int,
            check_returncode=effective_check,
            stdin_bytes=stdin_bytes,
            extra_env=effective_env,
        )

    def run_sync(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Union[str, Path]] = None,
        timeout_seconds: Optional[int] = None,
        check_returncode: bool = True,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> GitResult:
        """
        Run a Git command synchronously.

        Intended for startup paths (version checks) and for tests.
        Production code SHOULD prefer the async variant to avoid
        blocking the event loop.

        `extra_env` follows the same contract as in `run()`.
        """
        self._validate_args(args)
        timeout = self._coerce_timeout(timeout_seconds, args)
        cwd_path = self._validate_cwd(cwd)
        effective_args = self._inject_crlf_configs(args)
        command = (self._git_binary, *effective_args)
        env = self._build_environment(extra_env=extra_env)

        started = time.monotonic()
        started_at = _utc_now()

        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd_path) if cwd_path else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(
                f"Git command timed out after {timeout}s.",
                command=command,
                timeout_seconds=timeout,
                cause=exc,
            ) from exc
        except FileNotFoundError as exc:
            raise GitNotAvailableError(
                f"Git binary not found at '{self._git_binary}'.",
                details={"git_binary": self._git_binary},
                cause=exc,
            ) from exc
        except OSError as exc:
            raise GitExecutionError(
                "Failed to start the Git process.",
                command=command,
                returncode=-1,
                cause=exc,
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)

        stdout_safe, stdout_truncated, stdout_redacted = self._sanitize_stream(
            completed.stdout or b""
        )
        stderr_safe, stderr_truncated, stderr_redacted = self._sanitize_stream(
            completed.stderr or b""
        )

        result = GitResult(
            command=self._log_safe_command(command),
            returncode=completed.returncode,
            stdout=stdout_safe,
            stderr=stderr_safe,
            duration_ms=duration_ms,
            started_at=started_at,
            truncated=stdout_truncated or stderr_truncated,
            redacted=stdout_redacted or stderr_redacted,
        )

        if check_returncode and not result.succeeded:
            raise GitExecutionError(
                f"Git command failed with exit status {result.returncode}.",
                command=command,
                returncode=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )

        return result

    # --- Internals: subprocess I/O -------------------------------------

    async def _communicate(
        self,
        proc: "asyncio.subprocess.Process",
        *,
        timeout: int,
        stdin_bytes: Optional[bytes],
    ) -> Tuple[bytes, bytes, bool]:
        """
        Drive the child process to completion within `timeout` seconds.

        Reads stdout and stderr concurrently with a hard byte cap per
        stream. When `stdin_bytes` is provided, writes it to the
        child's stdin and closes the pipe.

        Returns `(stdout, stderr, timed_out)`. On timeout, the child
        is killed and `timed_out` is True; the buffers read so far
        are returned.
        """
        stdout_task = asyncio.create_task(
            self._read_stream(proc.stdout, self._max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            self._read_stream(proc.stderr, self._max_output_bytes)
        )

        stdin_task: Optional[asyncio.Task] = None
        if stdin_bytes is not None and proc.stdin is not None:
            stdin_task = asyncio.create_task(
                self._write_stream(proc.stdin, stdin_bytes)
            )

        wait_task = asyncio.create_task(proc.wait())

        try:
            await asyncio.wait_for(wait_task, timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(
                    "Git process did not exit after SIGKILL; "
                    "abandoning handle."
                )

        stdout_bytes = await stdout_task
        stderr_bytes = await stderr_task
        if stdin_task is not None:
            try:
                await stdin_task
            except Exception:  # noqa: BLE001
                # Broken pipe is expected if the child exited before
                # reading all stdin.
                pass

        return stdout_bytes, stderr_bytes, timed_out

    @staticmethod
    async def _read_stream(
        stream: Optional[asyncio.StreamReader],
        max_bytes: int,
    ) -> bytes:
        """
        Read a stream fully, capping retained bytes at `max_bytes`.

        Reading continues past the cap so that the child process does
        not block on a full pipe, but the surplus bytes are discarded.
        The caller is expected to mark the result as truncated when
        the returned buffer is at capacity and the stream had more
        data.
        """
        if stream is None:
            return b""

        buffer = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            if len(buffer) < max_bytes:
                remaining = max_bytes - len(buffer)
                buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
            else:
                truncated = True
        # Signal truncation to the caller by appending the marker.
        if truncated:
            buffer.extend(b"\n[... output truncated ...]\n")
        return bytes(buffer)

    @staticmethod
    async def _write_stream(
        stream: asyncio.StreamWriter, payload: bytes
    ) -> None:
        """Write `payload` to `stream` and close it."""
        try:
            stream.write(payload)
            await stream.drain()
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # --- Internals: sanitization ---------------------------------------

    def _sanitize_stream(
        self, raw: bytes
    ) -> Tuple[str, bool, bool]:
        """
        Redact and decode a raw output stream.

        Returns `(text, truncated, redacted)`.
        """
        if not raw:
            return "", False, False

        truncated = b"[... output truncated ...]" in raw

        redacted_bytes = redact_bytes(raw)
        redacted = redacted_bytes != raw

        text = redacted_bytes.decode("utf-8", errors="replace")
        return text, truncated, redacted

    # --- Internals: validation -----------------------------------------

    @staticmethod
    def _inject_crlf_configs(args: Sequence[str]) -> Tuple[str, ...]:
        """
        Inject standard CRLF configuration overrides to ensure deterministic
        line-ending behavior across Windows and POSIX platforms.
        """
        if args and args[0] in ("--version", "version", "--help", "help"):
            return tuple(args)

        joined = " ".join(args)
        prefix: list[str] = []
        if "core.autocrlf" not in joined:
            prefix.extend(["-c", "core.autocrlf=false"])
        if "core.safecrlf" not in joined:
            prefix.extend(["-c", "core.safecrlf=false"])

        return (*prefix, *args)

    @staticmethod
    def _validate_args(args: Sequence[str]) -> None:
        """
        Validate the argument array before spawning Git.

        Rejects empty sequences and any non-string or empty entries.
        This is the primary defense against argument injection.
        """
        if not args:
            raise ValidationError(
                "Git argument array must not be empty."
            )
        for index, arg in enumerate(args):
            if not isinstance(arg, str):
                raise ValidationError(
                    "Git arguments must all be strings.",
                    details={
                        "index": index,
                        "type": type(arg).__name__,
                    },
                )
            if "\x00" in arg:
                raise ValidationError(
                    "Git arguments must not contain NUL bytes.",
                    details={"index": index},
                )

    def _coerce_timeout(
        self,
        value: Optional[int],
        args: Optional[Sequence[str]] = None,
    ) -> int:
        """
        Resolve the timeout for a Git command.

        Precedence:
            1. ``value`` when supplied (explicit caller override).
            2. Command-aware default resolved from ``args`` (see
               ``_timeout_for_args``).
            3. ``self._default_timeout`` as a last resort (used only
               when ``args`` is None, which happens for internal
               callers that already know the timeout).
        """
        if value is not None:
            if not isinstance(value, int) or value <= 0:
                raise ValidationError(
                    "timeout_seconds must be a positive integer."
                )
            return value
        if args is not None:
            return self._timeout_for_args(args)
        return self._default_timeout

    def _timeout_for_args(self, args: Sequence[str]) -> int:
        """
        Return the appropriate default timeout based on the Git
        subcommand (audit report item 5.2).

        Classification:
            - NETWORK_COMMANDS (clone, fetch, push, pull, ls-remote)
              → ``NETWORK_TIMEOUT_SECONDS`` (300s by default)
            - LONG_OPERATION_COMMANDS (gc, repack, filter-branch, ...)
              → ``LONG_OPERATION_TIMEOUT_SECONDS`` (600s by default)
            - everything else (status, commit, log, diff, ...)
              → ``DEFAULT_TIMEOUT_SECONDS`` (60s by default)

        The caller can always override the resolved timeout by
        passing ``timeout_seconds`` explicitly.
        """
        if not args:
            return self._default_timeout

        # Skip past any leading ``-c key=value`` pairs that either
        # the caller or ``_inject_crlf_configs`` may have prepended.
        # Without this, a call like ``["-c", "foo=bar", "clone", ...]``
        # would misclassify ``-c`` as the subcommand.
        index = 0
        while index < len(args) and args[index] == "-c":
            index += 2  # skip "-c" and its value
        if index >= len(args):
            return self._default_timeout

        command = args[index].lower().lstrip("-")
        if command in NETWORK_COMMANDS:
            return self._network_timeout
        if command in LONG_OPERATION_COMMANDS:
            return self._long_operation_timeout
        return self._default_timeout

    @staticmethod
    def _validate_cwd(cwd: Optional[Union[str, Path]]) -> Optional[Path]:
        """
        Validate the working directory.

        The path is expanded and resolved; it must exist and be a
        directory. `None` is allowed and means "inherit".
        """
        if cwd is None:
            return None
        path = Path(cwd).expanduser()
        if not path.exists():
            raise ValidationError(
                f"Working directory does not exist: {path}.",
                details={"cwd": str(path)},
            )
        if not path.is_dir():
            raise ValidationError(
                f"Working directory is not a directory: {path}.",
                details={"cwd": str(path)},
            )
        return path.resolve()

    # --- Internals: environment ----------------------------------------

    def _build_environment(
        self,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, str]:
        """
        Build the sanitized environment for the child process.

        Dangerous variables are removed, and a small set of safe
        defaults is injected. The `PATH` of the parent is preserved so
        that Git can find its own helper programs.

        `extra_env`, when provided, is applied AFTER sanitization.
        This is what allows the credential flow in `push.py` to
        re-introduce `GIT_ASKPASS`/`SSH_ASKPASS` (both of which are
        on the unsafe list and would otherwise be stripped from the
        inherited environment). Every key in `extra_env` is checked
        against `_EXTRA_ENV_ALLOWLIST`; anything else raises
        `ValidationError` before the subprocess is spawned.
        """
        env: dict[str, str] = dict(os.environ)
        for name in _UNSAFE_ENV_VARS:
            env.pop(name, None)

        # Safe defaults (applied BEFORE extra_env so that the caller
        # cannot accidentally disable them).
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_PAGER"] = "cat"
        env["GIT_EDITOR"] = ":"
        env["GIT_MERGE_AUTOEDIT"] = "no"
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"

        if extra_env:
            for key, value in extra_env.items():
                if key not in _EXTRA_ENV_ALLOWLIST:
                    raise ValidationError(
                        f"Environment variable '{key}' is not allowed "
                        f"as an override.",
                        details={
                            "key": key,
                            "allowed": sorted(_EXTRA_ENV_ALLOWLIST),
                        },
                    )
                if not isinstance(value, str):
                    raise ValidationError(
                        f"Environment variable '{key}' must be a "
                        f"string.",
                        details={"key": key},
                    )
                if "\x00" in key or "\x00" in value:
                    raise ValidationError(
                        f"Environment variable '{key}' contains a NUL "
                        f"byte.",
                        details={"key": key},
                    )
                env[key] = value

        return env

    # --- Internals: binary discovery and version ----------------------

    @staticmethod
    def _resolve_binary(git_binary: Optional[str]) -> str:
        """
        Locate the Git binary and validate its path.

        When `git_binary` is provided, it is validated and used as-is
        (resolved to an absolute path when possible). Otherwise,
        `shutil.which("git")` is used to search `PATH`.

        Validation rules:
            - The path MUST be a non-empty string.
            - The path MUST NOT contain shell metacharacters that
              could break logging or mask an injection attempt. The
              Windows path separator (`\\`) is explicitly permitted;
              see `_SHELL_METACHARACTERS` for the rationale.

        Raises:
            ValidationError: If the supplied path is empty or contains
                forbidden characters.
            GitNotAvailableError: If no Git binary can be found.
        """
        if git_binary is None:
            located = shutil.which("git")
            if not located:
                raise GitNotAvailableError(
                    "Git binary could not be located on PATH."
                )
            return os.path.abspath(located)

        if not isinstance(git_binary, str) or not git_binary.strip():
            raise ValidationError(
                "git_binary must be a non-empty string.",
                details={"type": type(git_binary).__name__},
            )

        if _SHELL_METACHARACTERS.search(git_binary):
            raise ValidationError(
                "git_binary path contains disallowed characters.",
                details={"git_binary": git_binary},
            )

        resolved = Path(git_binary).expanduser()
        if not resolved.is_absolute():
            located = shutil.which(git_binary)
            if located:
                resolved = Path(located)
            else:
                resolved = resolved.absolute()
        return str(resolved)

    def _check_version(self) -> None:
        """
        Verify that the Git binary is present and new enough.

        The check is performed once and cached. Subsequent calls to
        `run` and `run_sync` do not repeat it.
        """
        try:
            result = self.run_sync(
                ["--version"],
                check_returncode=True,
            )
        except GitExecutionError as exc:
            raise GitNotAvailableError(
                "Git binary failed to report its version.",
                details={"git_binary": self._git_binary},
                cause=exc,
            ) from exc

        version = self._parse_version(result.stdout)
        if version is None:
            raise GitNotAvailableError(
                "Could not parse the Git version string.",
                details={"stdout": result.stdout.strip()},
            )

        if version < MINIMUM_GIT_VERSION:
            minimum = ".".join(str(p) for p in MINIMUM_GIT_VERSION)
            current = ".".join(str(p) for p in version)
            raise GitNotAvailableError(
                f"Git {minimum} or newer is required (found {current}).",
                details={"required": minimum, "found": current},
            )

        with self._version_lock:
            self._cached_version = version

    @staticmethod
    def _parse_version(output: str) -> Optional[Tuple[int, int, int]]:
        """
        Parse the version tuple from `git --version` output.

        Accepts the standard form `git version X.Y.Z` and the
        Windows-specific form `git version X.Y.Z.windows.N`.
        """
        if not output:
            return None
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
        if not match:
            return None
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3) or 0)
        return major, minor, patch

    # --- Internals: logging --------------------------------------------

    def _log_safe_command(self, command: Sequence[str]) -> Tuple[str, ...]:
        """
        Return a version of the command safe to log.

        The Git binary path is replaced by its basename. Arguments are
        kept as-is because they are already validated; any argument
        that may contain a secret is expected to be supplied via a
        credential helper rather than on the command line.
        """
        if not command:
            return ()
        return (os.path.basename(command[0]), *command[1:])

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"<GitExecutable binary={self._git_binary!r} "
            f"timeout={self._default_timeout}s "
            f"max_output={self._max_output_bytes}B>"
        )


# ---------------------------------------------------------------------------
# Default executable singleton
# ---------------------------------------------------------------------------


_DEFAULT_EXECUTABLE: Optional[GitExecutable] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_git_executable() -> GitExecutable:
    """
    Return the process-wide default `GitExecutable`, creating it on
    first use.
    """
    global _DEFAULT_EXECUTABLE
    with _DEFAULT_LOCK:
        if _DEFAULT_EXECUTABLE is None:
            _DEFAULT_EXECUTABLE = GitExecutable()
        return _DEFAULT_EXECUTABLE


def set_default_git_executable(executable: GitExecutable) -> None:
    """
    Replace the process-wide default `GitExecutable`.

    Intended for application startup and for tests.
    """
    if not isinstance(executable, GitExecutable):
        raise TypeError(
            "set_default_git_executable expects a GitExecutable instance."
        )
    global _DEFAULT_EXECUTABLE
    with _DEFAULT_LOCK:
        _DEFAULT_EXECUTABLE = executable


def reset_default_git_executable(
    *,
    git_binary: Optional[str] = None,
    validate_version: bool = True,
) -> GitExecutable:
    """
    Replace the default executable with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_EXECUTABLE
    with _DEFAULT_LOCK:
        _DEFAULT_EXECUTABLE = GitExecutable(
            git_binary=git_binary,
            validate_version=validate_version,
        )
        return _DEFAULT_EXECUTABLE
