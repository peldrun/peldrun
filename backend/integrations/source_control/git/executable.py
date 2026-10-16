# backend/integrations/source_control/git/executable.py
"""
Safe Git subprocess wrapper for the Source Control Integration
subsystem.

This module is the ONLY place in the subsystem permitted to spawn a
Git process. Every Git operation flows through `GitExecutable.run` or
`GitExecutable.run_sync`, which enforce the following invariants:

    - Commands are executed with `asyncio.create_subprocess_exec`
      using an argument array. `shell=True` is never used.
    - The Git binary path is validated to contain no shell
      metacharacters.
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
    "GitExecutionError",
    "GitTimeoutError",
    "GitNotAvailableError",
    "get_default_git_executable",
    "set_default_git_executable",
    "reset_default_git_executable",
    "MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MINIMUM_GIT_VERSION",
]


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


#: Maximum bytes retained from each of stdout and stderr. Outputs that
#: exceed this limit are truncated with a marker appended.
MAX_OUTPUT_BYTES: int = 5 * 1024 * 1024  # 5 MiB

#: Default subprocess timeout for a Git command.
DEFAULT_TIMEOUT_SECONDS: int = 60

#: Minimum supported Git version. Older versions do not support
#: `worktree` (2.5) with all the flags we rely on, and they lack
#: `--force-with-lease` semantics we require.
MINIMUM_GIT_VERSION: Tuple[int, int, int] = (2, 20, 0)

#: Shell metacharacters that MUST NOT appear in the Git binary path.
_SHELL_METACHARACTERS = re.compile(r"[;&|`$<>\n\r\t\"'\\*?\[\]{}()]")

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
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        validate_version: bool = True,
    ) -> None:
        self._git_binary = self._resolve_binary(git_binary)
        self._default_timeout = max(1, int(default_timeout_seconds))
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
        """Return the default timeout applied to Git commands."""
        return self._default_timeout

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

        Raises:
            ValidationError: If arguments are malformed.
            GitTimeoutError: If the command exceeds the timeout.
            GitExecutionError: If the command fails and
                `check_returncode` is True.
        """
        self._validate_args(args)
        timeout = self._coerce_timeout(timeout_seconds)
        cwd_path = self._validate_cwd(cwd)
        command = (self._git_binary, *args)

        env = self._build_environment()

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

    def run_sync(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Union[str, Path]] = None,
        timeout_seconds: Optional[int] = None,
        check_returncode: bool = True,
    ) -> GitResult:
        """
        Run a Git command synchronously.

        Intended for startup paths (version checks) and for tests.
        Production code SHOULD prefer the async variant to avoid
        blocking the event loop.
        """
        self._validate_args(args)
        timeout = self._coerce_timeout(timeout_seconds)
        cwd_path = self._validate_cwd(cwd)
        command = (self._git_binary, *args)
        env = self._build_environment()

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

    def _coerce_timeout(self, value: Optional[int]) -> int:
        if value is None:
            return self._default_timeout
        if not isinstance(value, int) or value <= 0:
            raise ValidationError(
                "timeout_seconds must be a positive integer."
            )
        return value

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

    def _build_environment(self) -> Mapping[str, str]:
        """
        Build the sanitized environment for the child process.

        Dangerous variables are removed, and a small set of safe
        defaults is injected. The `PATH` of the parent is preserved so
        that Git can find its own helper programs.
        """
        env: dict[str, str] = dict(os.environ)
        for name in _UNSAFE_ENV_VARS:
            env.pop(name, None)

        # Safe defaults.
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_PAGER"] = "cat"
        env["GIT_EDITOR"] = ":"
        env["GIT_MERGE_AUTOEDIT"] = "no"
        # Prevent SSH from opening a prompt for a password.
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        # Never write to a global credential helper.
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"

        return env

    # --- Internals: binary discovery and version ----------------------

    @staticmethod
    def _resolve_binary(git_binary: Optional[str]) -> str:
        """
        Locate the Git binary and validate its path.

        When `git_binary` is provided, it is validated and used as-is.
        Otherwise, `shutil.which("git")` is used to search `PATH`.

        Raises:
            ValidationError: If the supplied path contains shell
                metacharacters.
            GitNotAvailableError: If no Git binary can be found.
        """
        if git_binary is None:
            located = shutil.which("git")
            if not located:
                raise GitNotAvailableError(
                    "Git binary could not be located on PATH."
                )
            git_binary = located

        if _SHELL_METACHARACTERS.search(git_binary):
            raise ValidationError(
                "git_binary path contains disallowed characters.",
                details={"git_binary": git_binary},
            )

        resolved = Path(git_binary).expanduser()
        if not resolved.is_absolute():
            # Resolve to an absolute path if possible.
            located = shutil.which(git_binary)
            if located:
                resolved = Path(located)

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
