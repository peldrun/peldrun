# backend/integrations/source_control/tools/run_checks.py
"""
The `source_control.run_checks` tool.

Runs one or more pre-registered check commands inside a workspace.
The commands are configured at application startup; the agent does
NOT supply them. The agent selects checks by name from a list that
the application controls.

This design is a deliberate security decision. Running arbitrary
repository-provided code is inherently risky:

    - Check scripts may exfiltrate environment variables, network
      access, or filesystem contents.
    - The agent cannot be trusted to construct safe commands.
    - The user must be able to see and approve exactly what will
      run.

By restricting the agent to selecting named checks from a
whitelist, we ensure that:

    - The command set is visible and auditable.
    - The user can approve the addition of a check once and then
      rely on it.
    - No amount of prompt injection can turn this tool into a
      generic command executor.

Design rules:
    - The tool MUST NOT accept a command string or argument list
      from the caller.
    - The tool MUST resolve the workspace path through the
      workspace manager.
    - The tool MUST enforce a per-check timeout and kill the child
      process if it exceeds it.
    - The tool MUST bound stdout and stderr, and MUST redact both.
    - The tool MUST use `asyncio.create_subprocess_exec` with an
      argument array. Shell execution is prohibited.
    - The tool MUST NOT pass the process environment to the child
      unchanged. A small, sanitized environment is constructed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.models import WorkspaceRef
from ..errors import (
    ValidationError,
    WorkspaceNotFoundError,
    to_source_control_error,
)
from ..git.path_policy import (
    WorkspacePathPolicy,
    get_default_path_policy,
)
from ..git.workspace import (
    GitWorkspaceManager,
    get_default_workspace_manager,
)
from ..security.redaction import redact_bytes, redact_text
from .base import SourceControlTool, ToolContext, ToolResult

__all__ = [
    "RunChecksTool",
    "CheckDefinition",
    "CheckRegistry",
    "CheckOutcome",
    "get_default_check_registry",
    "set_default_check_registry",
    "reset_default_check_registry",
    "MAX_CHECK_OUTPUT_BYTES",
    "DEFAULT_CHECK_TIMEOUT_SECONDS",
]


logger = logging.getLogger(__name__)


#: Maximum bytes retained from each of stdout and stderr.
MAX_CHECK_OUTPUT_BYTES: int = 200_000

#: Default timeout for a single check, in seconds.
DEFAULT_CHECK_TIMEOUT_SECONDS: int = 300

#: Maximum number of checks that can be executed in a single call.
MAX_CHECKS_PER_CALL: int = 10


# ---------------------------------------------------------------------------
# Definition and registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckDefinition:
    """
    A single, pre-registered check.

    Fields:
        name:                Short, stable identifier used by the
                             agent to select the check.
        command:             Argument array. `command[0]` MUST be an
                             executable name resolvable on PATH or
                             an absolute path. No shell is involved.
        description:         One-sentence English description shown
                             to the model.
        timeout_seconds:     Per-execution timeout.
        allow_failure:       When True, a non-zero exit status does
                             not mark the overall result as failed.
        working_directory:   Optional path relative to the workspace
                             root. Defaults to the workspace root.
        environment:         Optional environment variables added on
                             top of the sanitized base environment.
                             Values MUST NOT contain secrets.
    """

    name: str
    command: Tuple[str, ...]
    description: str = ""
    timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS
    allow_failure: bool = False
    working_directory: Optional[str] = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValidationError("Check name must not be empty.")
        if not self.command:
            raise ValidationError(
                "Check command must have at least one element.",
                details={"name": self.name},
            )
        for arg in self.command:
            if not isinstance(arg, str) or "\x00" in arg:
                raise ValidationError(
                    "Check command arguments must be strings without "
                    "NUL bytes.",
                    details={"name": self.name},
                )
        if self.timeout_seconds <= 0:
            raise ValidationError(
                "Check timeout must be positive.",
                details={"name": self.name},
            )


@dataclass(frozen=True)
class CheckOutcome:
    """Result of running a single check."""

    name: str
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    passed: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "passed": self.passed,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }


class CheckRegistry:
    """
    Thread-safe registry of check definitions.

    The registry is populated by the application at startup. The
    agent never sees the registry directly; it only selects by name
    from the checks the application exposes.
    """

    def __init__(self) -> None:
        self._checks: Dict[str, CheckDefinition] = {}
        self._lock = threading.RLock()

    def register(
        self,
        definition: CheckDefinition,
        *,
        overwrite: bool = False,
    ) -> None:
        if not isinstance(definition, CheckDefinition):
            raise TypeError(
                "CheckRegistry.register expects a CheckDefinition."
            )
        with self._lock:
            if definition.name in self._checks and not overwrite:
                raise ValidationError(
                    f"Check '{definition.name}' is already registered.",
                    details={"name": definition.name},
                )
            self._checks[definition.name] = definition

    def unregister(self, name: str) -> bool:
        if not name:
            return False
        with self._lock:
            return self._checks.pop(name, None) is not None

    def get(self, name: str) -> Optional[CheckDefinition]:
        if not name:
            return None
        with self._lock:
            return self._checks.get(name)

    def list_names(self) -> Sequence[str]:
        with self._lock:
            return tuple(sorted(self._checks.keys()))

    def list_definitions(self) -> Sequence[CheckDefinition]:
        with self._lock:
            items = sorted(self._checks.items(), key=lambda kv: kv[0])
        return tuple(d for _n, d in items)

    def clear(self) -> None:
        with self._lock:
            self._checks.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._checks)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        with self._lock:
            return name in self._checks


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class RunChecksTool(SourceControlTool):
    """
    Run one or more registered checks inside a workspace.

    Inputs:
        workspace_id: Workspace to run the checks in. Required.
        checks:       List of check names to run. Required, non-empty.
        fail_fast:    When True (default), stop after the first
                      failure. When False, run every requested check.

    The tool's result contains one `CheckOutcome` per check that was
    actually executed. `passed` is True only when every executed
    check either passed or had `allow_failure=True`.
    """

    def __init__(
        self,
        *,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        check_registry: Optional[CheckRegistry] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
        audit_logger: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger)
        self._explicit_workspace_manager = workspace_manager
        self._explicit_check_registry = check_registry
        self._path_policy = path_policy or get_default_path_policy()

    # --- Metadata -------------------------------------------------------

    @property
    def name(self) -> str:
        return "source_control.run_checks"

    @property
    def description(self) -> str:
        registry = (
            self._explicit_check_registry or get_default_check_registry()
        )
        available = ", ".join(registry.list_names()) or "none"
        return (
            "Run one or more pre-registered check commands inside a "
            f"workspace. Available checks: {available}."
        )

    @property
    def input_schema(self) -> Mapping[str, Any]:
        registry = (
            self._explicit_check_registry or get_default_check_registry()
        )
        names = list(registry.list_names())
        return {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Identifier of the workspace to run checks in."
                    ),
                },
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": names or None,
                    },
                    "minItems": 1,
                    "maxItems": MAX_CHECKS_PER_CALL,
                    "description": (
                        "Names of the checks to run, in order."
                    ),
                },
                "fail_fast": {
                    "type": "boolean",
                    "description": (
                        "When true (default), stop after the first "
                        "failed check."
                    ),
                    "default": True,
                },
            },
            "required": ["workspace_id", "checks"],
            "additionalProperties": False,
        }

    # --- Execution ------------------------------------------------------

    async def execute(
        self, context: ToolContext, **kwargs: Any
    ) -> ToolResult:
        # --- Parse inputs ----------------------------------------------
        workspace_id = kwargs.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`workspace_id` must be a non-empty string."
                ),
            )
        workspace_id = workspace_id.strip()

        raw_checks = kwargs.get("checks")
        if (
            not isinstance(raw_checks, (list, tuple))
            or not raw_checks
        ):
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    "`checks` must be a non-empty list of check names."
                ),
            )
        if len(raw_checks) > MAX_CHECKS_PER_CALL:
            return self.failure_result(
                error_code="VALIDATION_ERROR",
                error_message=(
                    f"At most {MAX_CHECKS_PER_CALL} checks may be "
                    "requested in a single call."
                ),
            )
        check_names: List[str] = []
        for item in raw_checks:
            if not isinstance(item, str) or not item.strip():
                return self.failure_result(
                    error_code="VALIDATION_ERROR",
                    error_message=(
                        "Every entry in `checks` must be a non-empty "
                        "string."
                    ),
                )
            check_names.append(item.strip())

        fail_fast = bool(kwargs.get("fail_fast", True))

        # --- Resolve workspace -----------------------------------------
        workspace_manager = (
            self._explicit_workspace_manager
            or get_default_workspace_manager()
        )
        workspace = workspace_manager.get_workspace(workspace_id)
        if workspace is None:
            return self.failure_result(
                error_code="WORKSPACE_NOT_FOUND",
                error_message=(
                    f"Workspace '{workspace_id}' is not registered."
                ),
                metadata={"workspace_id": workspace_id},
            )
        if not isinstance(workspace, WorkspaceRef):
            return self.failure_result(
                error_code="PROVIDER_ERROR",
                error_message="Workspace record is malformed.",
            )

        # Resolve and validate the workspace root.
        try:
            workspace_root = self._path_policy.resolve_within_root(
                workspace.workspace_path, must_exist=True
            )
        except Exception as exc:  # noqa: BLE001
            normalized = to_source_control_error(exc)
            return self.failure_result(
                error_code=normalized.code.value,
                error_message=normalized.message,
                metadata={"workspace_id": workspace_id},
            )
        if not workspace_root.is_dir():
            return self.failure_result(
                error_code="WORKSPACE_NOT_FOUND",
                error_message=(
                    "Workspace directory does not exist on disk."
                ),
                metadata={"workspace_id": workspace_id},
            )

        # --- Resolve check definitions ---------------------------------
        registry = (
            self._explicit_check_registry or get_default_check_registry()
        )
        definitions: List[CheckDefinition] = []
        for check_name in check_names:
            definition = registry.get(check_name)
            if definition is None:
                return self.failure_result(
                    error_code="NOT_FOUND",
                    error_message=(
                        f"Check '{check_name}' is not registered."
                    ),
                    metadata={
                        "workspace_id": workspace_id,
                        "available_checks": list(registry.list_names()),
                    },
                )
            definitions.append(definition)

        # --- Execute checks --------------------------------------------
        outcomes: List[CheckOutcome] = []
        for definition in definitions:
            outcome = await self._run_one(
                definition=definition,
                workspace_root=workspace_root,
            )
            outcomes.append(outcome)
            if fail_fast and not outcome.passed:
                # Stop after the first failure (fail-fast contract).
                # If the check allows failure, `passed` is already
                # True and we continue.
                if not definition.allow_failure:
                    break

        overall_passed = all(o.passed for o in outcomes)

        return self.success_result(
            {
                "workspace_id": workspace_id,
                "overall_passed": overall_passed,
                "executed": len(outcomes),
                "requested": len(check_names),
                "results": [o.to_dict() for o in outcomes],
            },
            metadata={
                "workspace_id": workspace_id,
                "fail_fast": fail_fast,
            },
        )

    # --- Execution of one check ----------------------------------------

    async def _run_one(
        self,
        *,
        definition: CheckDefinition,
        workspace_root: Path,
    ) -> CheckOutcome:
        """
        Run a single check and return its outcome.

        Failures at the process level are captured in the outcome;
        this method does not raise.
        """
        cwd = workspace_root
        if definition.working_directory:
            try:
                cwd = self._path_policy.resolve_within_root(
                    workspace_root / definition.working_directory,
                    must_exist=True,
                )
            except Exception as exc:  # noqa: BLE001
                normalized = to_source_control_error(exc)
                return CheckOutcome(
                    name=definition.name,
                    exit_code=-1,
                    duration_ms=0,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    truncated=False,
                    passed=False,
                    error=(
                        "Working directory is not accessible: "
                        + normalized.message
                    ),
                )

        # Sanitized environment.
        env = self._build_environment(definition)

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *definition.command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return CheckOutcome(
                name=definition.name,
                exit_code=-1,
                duration_ms=0,
                stdout="",
                stderr="",
                timed_out=False,
                truncated=False,
                passed=False,
                error=(
                    f"Executable '{definition.command[0]}' was not "
                    "found."
                ),
            )
        except OSError as exc:  # noqa: BLE001
            return CheckOutcome(
                name=definition.name,
                exit_code=-1,
                duration_ms=0,
                stdout="",
                stderr="",
                timed_out=False,
                truncated=False,
                passed=False,
                error=f"Failed to start check: {type(exc).__name__}",
            )

        stdout_b, stderr_b, timed_out = await self._communicate(
            proc, timeout=definition.timeout_seconds
        )

        duration_ms = int((time.monotonic() - started) * 1000)

        stdout_safe, out_trunc = self._sanitize(stdout_b)
        stderr_safe, err_trunc = self._sanitize(stderr_b)

        exit_code = proc.returncode or 0
        passed = (exit_code == 0) or definition.allow_failure
        if timed_out:
            passed = False

        return CheckOutcome(
            name=definition.name,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout_safe,
            stderr=stderr_safe,
            timed_out=timed_out,
            truncated=out_trunc or err_trunc,
            passed=passed,
            error=None,
        )

    async def _communicate(
        self,
        proc: "asyncio.subprocess.Process",
        *,
        timeout: int,
    ) -> Tuple[bytes, bytes, bool]:
        """
        Drive the process to completion with a hard timeout.

        Output is capped at `MAX_CHECK_OUTPUT_BYTES` per stream.
        The child is killed on timeout.
        """
        stdout_task = asyncio.create_task(
            self._read_stream(proc.stdout, MAX_CHECK_OUTPUT_BYTES)
        )
        stderr_task = asyncio.create_task(
            self._read_stream(proc.stderr, MAX_CHECK_OUTPUT_BYTES)
        )
        wait_task = asyncio.create_task(proc.wait())

        timed_out = False
        try:
            await asyncio.wait_for(wait_task, timeout=timeout)
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
                    "Check process did not exit after SIGKILL."
                )

        stdout_b = await stdout_task
        stderr_b = await stderr_task
        return stdout_b, stderr_b, timed_out

    @staticmethod
    async def _read_stream(
        stream: Optional[asyncio.StreamReader],
        max_bytes: int,
    ) -> bytes:
        """
        Read `stream` fully, capping the retained bytes.
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
        if truncated:
            buffer.extend(b"\n[... output truncated ...]\n")
        return bytes(buffer)

    def _sanitize(self, raw: bytes) -> Tuple[str, bool]:
        """Redact and decode a raw output stream."""
        if not raw:
            return "", False
        truncated = b"[... output truncated ...]" in raw
        redacted = redact_bytes(raw)
        return redacted.decode("utf-8", errors="replace"), truncated

    # --- Environment ----------------------------------------------------

    def _build_environment(
        self, definition: CheckDefinition
    ) -> Mapping[str, str]:
        """
        Build the sanitized environment for a check.

        The environment is intentionally minimal:

            - `PATH` is preserved so that the check can find its own
              subprocesses.
            - `HOME` and `LANG` are set to safe defaults.
            - `TERM` is set to `dumb` so that no terminal escape
              sequences are emitted.
            - `CI` is set to `1` so that well-behaved tools switch
              to non-interactive output.
            - Every `PELDRUN_*` and secret-bearing variable from the
              parent environment is dropped.
            - The definition may add its own variables on top; those
              MUST NOT contain secrets.
        """
        env: Dict[str, str] = {}
        # Minimal baseline.
        path = os.environ.get("PATH", "/usr/bin:/bin")
        env["PATH"] = path
        env["HOME"] = os.environ.get("HOME", "/tmp")
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        env["TERM"] = "dumb"
        env["CI"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"

        # Add the definition's variables last, but never allow
        # known-sensitive names to be overridden.
        forbidden = {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TERM",
        }
        for key, value in definition.environment.items():
            if key.upper() in forbidden:
                continue
            env[key] = str(value)
        return env


# ---------------------------------------------------------------------------
# Default registry singleton
# ---------------------------------------------------------------------------


_DEFAULT_REGISTRY: Optional[CheckRegistry] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_check_registry() -> CheckRegistry:
    """Return the process-wide default check registry."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = CheckRegistry()
        return _DEFAULT_REGISTRY


def set_default_check_registry(registry: CheckRegistry) -> None:
    """Replace the process-wide default check registry."""
    if not isinstance(registry, CheckRegistry):
        raise TypeError(
            "set_default_check_registry expects a CheckRegistry."
        )
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = registry


def reset_default_check_registry() -> CheckRegistry:
    """Replace the default registry with a fresh empty one."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = CheckRegistry()
        return _DEFAULT_REGISTRY
