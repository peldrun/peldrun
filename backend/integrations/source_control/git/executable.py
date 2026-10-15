from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from ..domain.enums import SourceControlErrorCode
from ..errors import SourceControlError
from ..security.redaction import redact_text


class GitExecutionError(SourceControlError):
    """Raised when a Git command execution returns a non-zero exit code."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int,
        cmd: Sequence[str],
        stdout: str = "",
        stderr: str = "",
        details: Optional[dict[str, str]] = None,
    ) -> None:
        merged_details = {
            "returncode": str(returncode),
            "command": redact_text(" ".join(cmd)),
            "stderr": redact_text(stderr.strip()),
        }
        if details:
            merged_details.update(details)

        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            retryable=False,
            details=merged_details,
        )
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitNotFoundError(SourceControlError):
    """Raised when the git executable cannot be discovered in system PATH."""

    def __init__(self, message: str = "Git executable not found in system PATH.") -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.PROVIDER_ERROR,
            retryable=False,
        )


class GitTimeoutError(SourceControlError):
    """Raised when a git subprocess times out."""

    def __init__(self, message: str, timeout_seconds: float) -> None:
        super().__init__(
            message=message,
            code=SourceControlErrorCode.NETWORK_ERROR,
            retryable=True,
            details={"timeout_seconds": str(timeout_seconds)},
        )


@dataclass(frozen=True)
class GitResult:
    """Immutable execution outcome of a safe git subprocess command."""

    returncode: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def success(self) -> bool:
        return self.returncode == 0


class GitExecutable:
    """Safe wrapper for executing system Git subprocess commands with isolation and secret scrubbing."""

    def __init__(
        self,
        git_path: Optional[str] = None,
        default_timeout_seconds: float = 60.0,
    ) -> None:
        self._git_path = git_path or shutil.which("git")
        self.default_timeout_seconds = default_timeout_seconds

    @property
    def is_available(self) -> bool:
        """Checks if a valid git binary is accessible."""
        return self._git_path is not None and os.path.isfile(self._git_path)

    @property
    def binary_path(self) -> str:
        """Returns the absolute path to the git executable or raises GitNotFoundError."""
        if not self._git_path or not os.path.isfile(self._git_path):
            discovered = shutil.which("git")
            if not discovered:
                raise GitNotFoundError()
            self._git_path = discovered
        return self._git_path

    async def get_version(self) -> str:
        """Retrieves and parses the git version string (e.g. '2.43.0')."""
        result = await self.run(["--version"], cwd=None)
        match = re.search(r"git version\s+([\d\.]+)", result.stdout)
        if match:
            return match.group(1)
        return result.stdout.strip()

    def _prepare_env(self, extra_env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """Creates an isolated, non-interactive environment for git execution."""
        env = dict(os.environ)

        # Force non-interactive behavior to prevent hanging background processes
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["LANG"] = "C"
        env["LC_ALL"] = "C"

        # Neutralize unsafe trace variables
        for var in ("GIT_TRACE", "GIT_TRANSFER_TRACE", "GIT_CURL_VERBOSE"):
            env.pop(var, None)

        if extra_env:
            env.update(extra_env)

        return env

    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        check: bool = True,
        extra_env: Optional[Mapping[str, str]] = None,
        stdin_bytes: Optional[bytes] = None,
    ) -> GitResult:
        """Executes a git command with timeout safeguards and sanitized outputs.

        Raises:
            GitNotFoundError: If git binary is missing.
            GitTimeoutError: If execution exceeds timeout duration.
            GitExecutionError: If command fails and check=True.
        """
        executable = self.binary_path
        effective_timeout = timeout if timeout is not None else self.default_timeout_seconds
        cmd = [executable, *args]

        env = self._prepare_env(extra_env)
        start_time = time.perf_counter()

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError as exc:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            clean_cmd = redact_text(" ".join(cmd))
            raise GitTimeoutError(
                f"Git command timed out after {effective_timeout}s: '{clean_cmd}'",
                timeout_seconds=effective_timeout,
            ) from exc
        except FileNotFoundError as exc:
            raise GitNotFoundError(f"Git executable not found at '{executable}'.") from exc

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        returncode = proc.returncode if proc.returncode is not None else -1

        # Sanitize text outputs to prevent token/secret leakage
        stdout_text = redact_text(stdout_bytes.decode("utf-8", errors="replace"))
        stderr_text = redact_text(stderr_bytes.decode("utf-8", errors="replace"))

        result = GitResult(
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_ms=duration_ms,
        )

        if check and not result.success:
            raise GitExecutionError(
                f"Git command failed with exit code {returncode}: {stderr_text.strip() or stdout_text.strip()}",
                returncode=returncode,
                cmd=cmd,
                stdout=stdout_text,
                stderr=stderr_text,
            )

        return result
