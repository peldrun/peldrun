#backend\peldrun\tools\builtins\shell_exec.py
"""
PELDRUN Core Shell Execution Tool.
Executes shell commands strictly bounded within the project workspace,
enforcing directory containment policies and satisfying BaseTool abstract contracts.

Hardened under P0 Item 4 (Security):
- [FIX-ALLOWLIST-BYPASS] When `settings.allowed_commands` is active, the
  command is dispatched via `create_subprocess_exec` (no shell parsing),
  and every token is checked for shell metacharacters (`;`, `|`, `&`,
  `>`, `<`, `$`, backtick) before spawn. The historical implementation
  checked only the first token and then handed the raw string to a
  shell, allowing `python script.py; curl evil.com` to bypass the
  allowlist. See the fix comment near the allowlist block below.
- [FIX-PROCTREE] The child is spawned in a new session / process group
  so that a timeout terminates the ENTIRE process tree (shell ->
  node -> npm) via killpg on Unix or `taskkill /F /T` on Windows,
  instead of only the direct child.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionContext


# ---------------------------------------------------------------------------
# Shell safety: metacharacter rejection + process-tree termination
# ---------------------------------------------------------------------------
#
# [FIX-ALLOWLIST-BYPASS]
# The historical implementation checked only the first token of the
# command against `allowed_commands` but still dispatched the raw string
# to `create_subprocess_shell`. A command like
#     `python script.py; curl http://evil.com`
# passed the allowlist check (first token `python` was allowed) and then
# `/bin/sh` (or `cmd.exe`) executed BOTH halves because `;` is a shell
# operator. Same bypass existed for `&&`, `||`, `|`, `>`, `<`, `$(...)`,
# and backticks.
#
# Two-layer fix:
#   1. When an allowlist is active, dispatch via
#      `create_subprocess_exec(*tokens)` -- no shell parsing, so `;` in
#      a token is just a literal character in argv.
#   2. Additionally reject any token containing a shell metacharacter so
#      that argv-injection through `python -c "import os; os.system(...)"`
#      is caught before the child even spawns.
#
# [FIX-PROCTREE]
# When a command times out we must kill the ENTIRE process tree, not
# just the direct child. `shell` -> `node` -> `npm` is a common pattern
# in JS builds; killing only the shell leaves `node` and `npm` running
# for minutes after the run is marked cancelled. The child is therefore
# spawned in a NEW session/process-group so a single killpg (Unix) or
# `taskkill /T` (Windows) tears down the whole tree.

_SHELL_METACHARACTERS: Set[str] = {
    ";", "|", "&", ">", "<", "$", "`", "\n",
}


def _token_has_metacharacter(token: str) -> Optional[str]:
    """
    Return the first shell metacharacter found in ``token``, or ``None``.

    Used to reject compound commands BEFORE they reach the child. This
    is the primary defense against the allowlist-bypass bug: even if
    the first token matches an allowed command, a trailing
    ``; rm -rf /`` must be blocked here.
    """
    for ch in token:
        if ch in _SHELL_METACHARACTERS:
            return ch
    return None


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
) -> None:
    """
    Kill ``proc`` AND every process it spawned.

    Platform-specific:
        * Unix     -- ``os.killpg`` against the process group created
                      by ``start_new_session=True``.
        * Windows  -- ``taskkill /F /T`` which walks the process tree.

    Best-effort: never raises. A failed kill is preferable to a
    timeout handler that itself crashes.
    """
    if proc.returncode is not None:
        return  # Already exited.

    if sys.platform == "win32":
        try:
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(taskkill.wait(), timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:
        pass


def _build_spawn_kwargs() -> Dict[str, Any]:
    """
    Return platform-specific kwargs that place the child in a NEW
    process group / session so ``_terminate_process_tree`` can kill
    the whole tree instead of just the direct child.
    """
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP is a Windows-only constant exposed
        # by the subprocess module.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class ShellExecArgs(BaseModel):
    """Pydantic schema defining arguments for ShellExecTool."""
    command: str = Field(..., description="The command-line instruction to execute.")
    # `default=None` (was: 60.0) is critical (audit report item 5.1).
    # The schema is serialized and shown to the LLM as part of the
    # tool spec. When it advertised `default: 60.0`, the LLM would
    # often send ``{"command": "...", "timeout": 60}`` explicitly --
    # silently overriding the operator-configured
    # ``settings.timeout_seconds``. ``None`` communicates "use the
    # configured default", and ``_arun`` already falls back to
    # ``self._settings_timeout`` when timeout is None.
    timeout: Optional[float] = Field(
        default=None,
        description=(
            "Optional execution timeout in seconds. Omit to use the "
            "operator-configured timeout from the bash manifest "
            "(recommended for long-running commands such as "
            "'npm install' or 'cargo build'). Set explicitly only "
            "when you need a tighter deadline than the configured "
            "default."
        ),
    )
    working_subdir: Optional[str] = Field(default=None, description="Optional relative directory to execute within.")


# Backward-compatibility alias
ShellExecParameters = ShellExecArgs


class ShellExecTool(BaseTool):
    """
    Executes shell commands safely bounded within the project workspace root.
    Implements abstract _arun method from BaseTool.
    """

    name: str = "shell_exec"
    description: str = "Executes command-line instructions safely bounded within the project workspace."
    args_schema = ShellExecArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        default_timeout: float = 60.0,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the shell tool with optional user-supplied settings (M7.9).

        Args:
            workspace_root: Root directory for shell execution.
            settings: User-configured values from ``settings_schema``
                (``timeout_seconds``, ``max_output_chars``,
                ``working_directory``, ``inherit_env``,
                ``allowed_commands``). Missing keys fall back to the
                schema defaults, then to ``default_timeout``.
            default_timeout: Ultimate fallback when neither settings nor
                the per-call ``timeout`` argument specify a value.
                Preserved so callers using the factory ``timeout``
                parameter keep working.
        """
        super().__init__(workspace_root=workspace_root)
        self.workspace_root = workspace_root or os.getcwd()

        s: Dict[str, Any] = settings or {}

        def _as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _as_float(value: Any, default: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        self._settings_timeout: float = _as_float(
            s.get("timeout_seconds"), default_timeout
        )
        self._settings_max_output: int = _as_int(
            s.get("max_output_chars"), 20000
        )
        self._settings_working_dir: Optional[str] = (
            s.get("working_directory") or "workspace_root"
        )
        self._settings_inherit_env: bool = bool(s.get("inherit_env", False))
        raw_allow = s.get("allowed_commands")
        self._settings_allowed_commands: list[str] = (
            [str(x) for x in raw_allow if isinstance(x, str)]
            if isinstance(raw_allow, list)
            else []
        )

    async def _arun(
        self,
        command: str,
        timeout: Optional[float] = None,
        working_subdir: Optional[str] = None,
        context: Optional[ToolExecutionContext] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Internal asynchronous shell execution satisfying BaseTool contract.

        Settings precedence (M7.9):
          * ``timeout``: per-call arg  >  settings.timeout_seconds  >
            factory default (via ``default_timeout``).
          * Working directory: per-call ``working_subdir``  >
            settings.working_directory  > workspace root.
          * Environment: if settings.inherit_env is False (default),
            a scrubbed, allow-listed env is passed so user secrets
            (API keys, tokens) never leak into subprocesses.
          * ``settings.allowed_commands`` (if non-empty): first token
            of the command MUST be in the list, else rejected. When
            active, dispatch happens via ``create_subprocess_exec``
            (no shell), and every token is checked for shell
            metacharacters to close the historical bypass.
          * ``settings.max_output_chars``: output is truncated after
            execution to prevent runaway log capture.
        """
        root_path = Path(self.workspace_root).resolve()
        target_dir = root_path

        # 1. Resolve working directory (explicit subdir wins, then settings).
        effective_subdir = working_subdir
        if effective_subdir is None and self._settings_working_dir not in (
            None,
            "",
            "workspace_root",
        ):
            effective_subdir = self._settings_working_dir

        if effective_subdir:
            candidate = (root_path / effective_subdir).resolve()
            try:
                candidate.relative_to(root_path)
                target_dir = candidate
            except ValueError:
                return ToolResult(
                    output="Access Denied: Working directory escapes workspace boundary.",
                    exit_code=1,
                    is_error=True,
                )

        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

        # 2. Resolve timeout (per-call > settings > instance default).
        if timeout is not None:
            timeout_val = float(timeout)
        else:
            timeout_val = float(self._settings_timeout)

        # 3. Enforce the allowed-commands allowlist when configured.
        #
        # [FIX-ALLOWLIST-BYPASS] When an allowlist is active, the
        # command MUST be dispatched via `create_subprocess_exec` (no
        # shell parsing) so that `python script.py; curl evil.com`
        # cannot bypass the allowlist check on the first token.
        # Metacharacter rejection is the second layer: even if the
        # caller writes `python -c "import os; os.system(...)"`,
        # the `;` inside the code string is caught before spawn.
        use_allowlist_mode = bool(self._settings_allowed_commands)
        tokens: List[str] = []

        if use_allowlist_mode:
            try:
                tokens = shlex.split(command)
            except ValueError as split_err:
                return ToolResult(
                    output=(
                        f"Shell execution failure: could not parse "
                        f"command: {split_err}"
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "UNPARSEABLE_COMMAND"},
                )

            if not tokens:
                return ToolResult(
                    output="Shell execution failure: empty command.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "EMPTY_COMMAND"},
                )

            # [FIX-ALLOWLIST-BYPASS] Reject any token containing a
            # shell metacharacter. This catches `;`, `&&`, `||`, `|`,
            # `>`, `<`, `$(...)`, and backticks both at the argv
            # level and inside quoted code strings passed to
            # `python -c` / `node -e` / `perl -e`.
            for token in tokens:
                bad_char = _token_has_metacharacter(token)
                if bad_char is not None:
                    return ToolResult(
                        output=(
                            f"Shell execution rejected: token "
                            f"{token!r} contains shell metacharacter "
                            f"{bad_char!r}. Compound commands "
                            f"(pipes, redirection, semicolons, "
                            f"command substitution) are NOT permitted "
                            f"when an allowlist is active."
                        ),
                        exit_code=1,
                        is_error=True,
                        metadata={
                            "error_code": "SHELL_METACHARACTER_REJECTED",
                            "token": token,
                            "metacharacter": bad_char,
                        },
                    )

            # Compare by basename so absolute paths to allowed binaries
            # (e.g. /usr/bin/python) still match "python".
            first = Path(tokens[0]).name or tokens[0]
            if first not in self._settings_allowed_commands:
                return ToolResult(
                    output=(
                        f"Command not permitted by settings allow-list: "
                        f"{tokens[0]!r}. Allowed: "
                        f"{sorted(self._settings_allowed_commands)}"
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "COMMAND_NOT_ALLOWED",
                        "command": tokens[0],
                    },
                )

        # 4. Build a scrubbed environment when inherit_env is disabled.
        #    Only toolchain-critical variables are forwarded; secrets
        #    (API keys, tokens) are dropped by construction.
        proc_env: Optional[Dict[str, str]] = None
        if not self._settings_inherit_env:
            allow_keys = {
                "PATH", "HOME", "USER", "USERNAME", "USERPROFILE",
                "LANG", "LC_ALL", "LC_CTYPE", "TERM",
                "TMP", "TEMP", "TMPDIR",
                "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
                "PYTHONPATH", "PYTHONHOME",
            }
            proc_env = {k: v for k, v in os.environ.items() if k in allow_keys}

        # 5. Spawn the child in a NEW session / process group so that
        #    `_terminate_process_tree` can tear down the entire tree
        #    on timeout. See the module-level constants above for the
        #    rationale.
        try:
            spawn_kwargs = _build_spawn_kwargs()

            if use_allowlist_mode:
                # [FIX-ALLOWLIST-BYPASS] exec mode: no shell parsing.
                # The exact argv validated above is what the child
                # receives -- `;` inside a token is a literal
                # character, not a command separator.
                proc = await asyncio.create_subprocess_exec(
                    *tokens,
                    cwd=str(target_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                    **spawn_kwargs,
                )
            else:
                # No allowlist active: preserve shell mode so the
                # operator's pipes/redirection continue to work as
                # before. The operator explicitly opted out of
                # command restriction.
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(target_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                    **spawn_kwargs,
                )

            if context is not None:
                stdout_chunks = []
                stderr_chunks = []

                async def _read_stream(stream, chunks, stream_name):
                    while True:
                        # read(4096) is non-blocking and cross-platform safe
                        chunk = await stream.read(4096)
                        if not chunk:
                            break
                        text = chunk.decode("utf-8", errors="replace")
                        chunks.append(text)
                        await context.emit_output(text, stream=stream_name)

                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            _read_stream(proc.stdout, stdout_chunks, "stdout"),
                            _read_stream(proc.stderr, stderr_chunks, "stderr"),
                            proc.wait()
                        ),
                        timeout=timeout_val
                    )
                    stdout_str = "".join(stdout_chunks).strip()
                    stderr_str = "".join(stderr_chunks).strip()
                except asyncio.TimeoutError:
                    # [FIX-PROCTREE] Terminate the entire process tree,
                    # not just the direct child. A shell that spawned
                    # `node` (which spawned `npm`) must not leave
                    # grandchildren running after the timeout fires.
                    await _terminate_process_tree(proc)
                    return ToolResult(
                        output=f"Shell command execution timed out after {timeout_val} seconds.",
                        exit_code=1,
                        is_error=True,
                        metadata={"error_code": "EXECUTION_TIMEOUT"},
                    )
            else:
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=timeout_val
                    )
                except asyncio.TimeoutError:
                    # [FIX-PROCTREE] Same rationale as the streaming
                    # branch above.
                    await _terminate_process_tree(proc)
                    return ToolResult(
                        output=f"Shell command execution timed out after {timeout_val} seconds.",
                        exit_code=1,
                        is_error=True,
                        metadata={"error_code": "EXECUTION_TIMEOUT"},
                    )

                stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
                stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()

            is_err = proc.returncode != 0
            combined_output = stdout_str if stdout_str else stderr_str

            # [FIX-EMPTY-OUTPUT-SUCCESS] Emit a descriptive success or
            # failure message when the command produced NO output.
            #
            # Background:
            #   Many Unix commands are intentionally silent on success:
            #   `mkdir`, `rm -f`, `mv`, `cp`, `touch`, `chmod`, `ln`,
            #   `git add`, `git checkout`, and many more. For these
            #   commands `stdout_str` and `stderr_str` are BOTH empty,
            #   so the pre-fix code returned:
            #
            #       ToolResult(output="", exit_code=0, is_error=False)
            #
            #   The OBSERVATION event then carried `output: ""`, which
            #   small / quantized models (GigaChat-2-Max, Gemma, Phi)
            #   frequently misread as "the command failed silently" or
            #   "nothing happened". Observed real-world symptom: the
            #   agent retried `mkdir "my app"` two or three times and
            #   then reported a tool failure, even though the directory
            #   had already been created successfully on the first try.
            #
            #   A short factual message resolves the ambiguity without
            #   inventing information: it states exactly what happened
            #   (the process ran to completion and exited with code N)
            #   and nothing more. The agent can then proceed confidently
            #   to the next step.
            #
            # The failure side is covered symmetrically: a command that
            # exits non-zero WITHOUT producing output (e.g. the `false`
            # builtin, or `grep` finding no match) also gets a clear
            # message. Without it, the LLM sees an empty output plus an
            # error flag and sometimes concludes the tool itself broke
            # instead of "the command ran and returned a non-zero exit
            # status" — a critical distinction for retry logic.
            if not combined_output:
                if is_err:
                    combined_output = (
                        f"Command exited with code {proc.returncode} "
                        f"and produced no output."
                    )
                else:
                    combined_output = (
                        f"Command executed successfully with no "
                        f"output (exit code {proc.returncode})."
                    )

            # 6. Truncate to settings.max_output_chars (safety net).
            if (
                self._settings_max_output > 0
                and len(combined_output) > self._settings_max_output
            ):
                combined_output = (
                    combined_output[: self._settings_max_output]
                    + f"\n... [output truncated to {self._settings_max_output} characters]"
                )

            return ToolResult(
                output=combined_output,
                exit_code=proc.returncode if proc.returncode is not None else (1 if is_err else 0),
                is_error=is_err,
                metadata={
                    "command": command,
                    "cwd": str(target_dir),
                    "timeout_seconds": timeout_val,
                },
            )

        except Exception as exc:
            return ToolResult(
                output=f"Shell execution failure: {str(exc)}",
                exit_code=1,
                is_error=True,
            )

    async def execute(self, command: str, timeout: Optional[float] = None, context: Optional[ToolExecutionContext] = None, **kwargs: Any) -> ToolResult:
        """Public alias maintaining backward compatibility.

        The previous signature declared ``timeout=60.0``, which meant
        any caller of ``execute()`` without an explicit timeout
        silently bypassed ``settings.timeout_seconds``. Passing
        ``None`` (the new default) lets ``_arun`` fall back to the
        configured value, matching the behavior of the ``aexecute``
        path used by the adapter.
        """
        return await self._arun(command=command, timeout=timeout, **kwargs)


__all__ = [
    "ShellExecArgs",
    "ShellExecParameters",
    "ShellExecTool",
]
