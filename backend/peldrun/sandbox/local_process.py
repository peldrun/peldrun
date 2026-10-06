"""
PELDRUN Core Local Process Sandbox Implementation.
Executes commands and manages workspace files within a host-confined local directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from peldrun.sandbox.base import BaseSandbox, SandboxConfig, SandboxResult
from peldrun.security.policy import SecurityViolationError

logger = logging.getLogger("peldrun.sandbox.local_process")


class LocalProcessSandbox(BaseSandbox):
    """
    Standard local sandbox implementation.
    Restricts command executions, script evaluations, and file I/O
    strictly to the designated project workspace root on the local host.
    """

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        security_policy: Optional[Any] = None,
    ) -> None:
        super().__init__(config=config, security_policy=security_policy)
        self._active_processes: set[asyncio.subprocess.Process] = set()

    async def ainitialize(self) -> None:
        """Ensure workspace directory exists and is prepared for sandbox operations."""
        if not self.workspace_root:
            raise ValueError("Cannot initialize sandbox: workspace_root is not configured.")

        root = Path(self.workspace_root).resolve()
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        logger.debug("LocalProcessSandbox initialized at: %s", root)

    async def aexecute(
        self,
        command: str,
        timeout: Optional[float] = None,
        workdir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> SandboxResult:
        """
        Execute command asynchronously bounded inside the workspace directory,
        validating against SecurityPolicy before launch.
        """
        if not self.workspace_root:
            raise PermissionError("Sandbox workspace root is not configured.")

        # Pre-execution security check
        if self.security_policy is not None and hasattr(self.security_policy, "check_command"):
            try:
                self.security_policy.check_command(command)
            except SecurityViolationError as sec_err:
                logger.warning("Security policy blocked command execution: %s", sec_err)
                return SandboxResult(
                    stdout="",
                    stderr=f"Security violation: {str(sec_err)}",
                    exit_code=126,
                    duration_seconds=0.0,
                    is_timeout=False,
                    metadata={"security_blocked": True, "command": command},
                )

        # Determine target working directory
        if workdir:
            exec_dir = self.resolve_safe_path(workdir)
        else:
            exec_dir = Path(self.workspace_root).resolve()

        if not exec_dir.exists():
            await asyncio.to_thread(exec_dir.mkdir, parents=True, exist_ok=True)

        effective_timeout = timeout if timeout is not None else self.config.default_timeout

        # Construct execution environment
        process_env = os.environ.copy()
        if self.config.environment_vars:
            process_env.update(self.config.environment_vars)
        if env:
            process_env.update(env)

        start_time = time.time()
        logger.debug("Sandbox executing: '%s' in %s", command, exec_dir)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(exec_dir),
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active_processes.add(process)

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Process execution timed out after %fs: '%s'", effective_timeout, command)
                try:
                    process.kill()
                    await process.wait()
                except Exception as kill_err:
                    logger.debug("Error while terminating timed out process: %s", kill_err)

                duration = time.time() - start_time
                return SandboxResult(
                    stdout="",
                    stderr=f"Execution timed out after {effective_timeout} seconds.",
                    exit_code=124,
                    duration_seconds=round(duration, 3),
                    is_timeout=True,
                    metadata={"command": command, "workdir": str(exec_dir)},
                )
            finally:
                self._active_processes.discard(process)

            duration = time.time() - start_time
            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

            max_chars = self.config.max_output_chars
            if len(stdout_text) > max_chars:
                stdout_text = (
                    stdout_text[:max_chars]
                    + f"\n... [STDOUT truncated, {len(stdout_text) - max_chars} characters omitted] ..."
                )
            if len(stderr_text) > max_chars:
                stderr_text = (
                    stderr_text[:max_chars]
                    + f"\n... [STDERR truncated, {len(stderr_text) - max_chars} characters omitted] ..."
                )

            exit_code = process.returncode if process.returncode is not None else 0

            return SandboxResult(
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=exit_code,
                duration_seconds=round(duration, 3),
                is_timeout=False,
                metadata={"command": command, "workdir": str(exec_dir)},
            )

        except Exception as ex:
            duration = time.time() - start_time
            logger.exception("Sandbox execution failure for command '%s': %s", command, ex)
            return SandboxResult(
                stdout="",
                stderr=f"Sandbox execution error: {str(ex)}",
                exit_code=1,
                duration_seconds=round(duration, 3),
                is_timeout=False,
                metadata={"error_type": type(ex).__name__},
            )

    async def aread_file(self, relative_path: str, encoding: str = "utf-8") -> str:
        """Read file contents safely from within the sandbox boundary."""
        target_path = self.resolve_safe_path(relative_path)
        if not target_path.is_file():
            raise FileNotFoundError(f"File not found within sandbox: '{relative_path}'")

        return await asyncio.to_thread(target_path.read_text, encoding=encoding, errors="replace")

    async def awrite_file(
        self,
        relative_path: str,
        content: str,
        encoding: str = "utf-8",
    ) -> bool:
        """Write text file safely into the sandbox workspace."""
        target_path = self.resolve_safe_path(relative_path)

        def _sync_write() -> None:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding=encoding)

        await asyncio.to_thread(_sync_write)
        return True

    async def acheck_health(self) -> bool:
        """Verify that workspace exists, is accessible, and writable."""
        if not self.workspace_root:
            return False

        root = Path(self.workspace_root).resolve()
        try:
            if not root.exists():
                await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)

            test_file = root / ".peldrun_health_check"

            def _test_write() -> bool:
                test_file.write_text("health_ok", encoding="utf-8")
                if test_file.exists():
                    test_file.unlink()
                    return True
                return False

            return await asyncio.to_thread(_test_write)
        except Exception as ex:
            logger.debug("Sandbox health check failed: %s", ex)
            return False

    async def acleanup(self) -> None:
        """Terminate any lingering child processes managed by this sandbox."""
        for proc in list(self._active_processes):
            try:
                proc.kill()
                await proc.wait()
            except Exception as ex:
                logger.debug("Error cleaning up sandbox process: %s", ex)
        self._active_processes.clear()
        logger.debug("LocalProcessSandbox cleanup completed.")