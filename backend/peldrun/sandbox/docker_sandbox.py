"""
PELDRUN Core Docker Container Sandbox Implementation.
Executes commands and manages workspace files within an isolated Docker container runtime.
Supports live Docker CLI execution and injectable command runners for hermetic testing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from pydantic import Field

from peldrun.sandbox.base import BaseSandbox, SandboxConfig, SandboxResult

logger = logging.getLogger("peldrun.sandbox.docker_sandbox")


class DockerSandboxConfig(SandboxConfig):
    """Extended configuration schema for Docker-based sandbox environments."""
    image: str = Field(
        default="python:3.11-slim",
        description="Docker image tag used for container instantiation",
    )
    container_prefix: str = Field(
        default="peldrun-sandbox",
        description="Prefix for uniquely generated container names",
    )
    network_disabled: bool = Field(
        default=False,
        description="Whether to isolate container from network access (--network none)",
    )
    memory_limit: str = Field(
        default="2g",
        description="Memory ceiling enforced on container (--memory)",
    )
    cpu_quota: float = Field(
        default=1.0,
        description="Maximum CPU cores allocated to container (--cpus)",
    )
    auto_remove: bool = Field(
        default=True,
        description="Automatically prune container upon cleanup",
    )


class DockerSandbox(BaseSandbox):
    """
    Isolated container sandbox utilizing Docker CLI.
    Mounts host workspace into /workspace and executes commands non-blockingly via docker exec.
    """

    def __init__(
        self,
        config: Optional[DockerSandboxConfig] = None,
        cmd_runner: Optional[Callable[[List[str], Optional[float]], Awaitable[Tuple[int, str, str]]]] = None,
    ) -> None:
        super().__init__(config=config or DockerSandboxConfig())
        self.docker_config: DockerSandboxConfig = self.config  # type: ignore[assignment]
        self._cmd_runner = cmd_runner
        self._container_id: Optional[str] = None
        self._container_name: str = f"{self.docker_config.container_prefix}-{uuid.uuid4().hex[:8]}"
        self._initialized: bool = False
        self._lock = asyncio.Lock()

    @property
    def container_name(self) -> str:
        """Return active container name."""
        return self._container_name

    async def _run_command(self, cmd_args: List[str], timeout: Optional[float] = 30.0) -> Tuple[int, str, str]:
        """Helper to run a subprocess command safely or delegate to custom cmd_runner."""
        if self._cmd_runner is not None:
            return await self._cmd_runner(cmd_args, timeout)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as fnf_err:
            return 127, "", f"Docker executable not found: {fnf_err}"
        except Exception as ex:
            return 1, "", str(ex)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode if proc.returncode is not None else 0
            return (
                exit_code,
                stdout_bytes.decode("utf-8", errors="replace").strip(),
                stderr_bytes.decode("utf-8", errors="replace").strip(),
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return 124, "", "Subprocess timed out."

    async def ainitialize(self) -> None:
        """Verify Docker availability and bootstrap the background container."""
        async with self._lock:
            if self._initialized:
                return

            if not self.workspace_root:
                raise ValueError("Cannot initialize DockerSandbox: workspace_root is not configured.")

            abs_root = Path(self.workspace_root).resolve()
            await asyncio.to_thread(abs_root.mkdir, parents=True, exist_ok=True)

            # 1. Verify Docker daemon reachability
            code, _, err = await self._run_command(["docker", "info"], timeout=10.0)
            if code != 0:
                raise RuntimeError(f"Docker daemon is not running or unreachable: {err}")

            # 2. Build docker run arguments
            cmd = [
                "docker", "run", "-d",
                "--name", self._container_name,
                "-v", f"{abs_root}:/workspace",
                "-w", "/workspace",
                "--memory", self.docker_config.memory_limit,
                f"--cpus={self.docker_config.cpu_quota}",
            ]

            if self.docker_config.network_disabled:
                cmd.extend(["--network", "none"])

            cmd.extend([self.docker_config.image, "tail", "-f", "/dev/null"])

            code, out, err = await self._run_command(cmd, timeout=60.0)
            if code != 0:
                raise RuntimeError(f"Failed to start Docker sandbox container '{self._container_name}': {err}")

            self._container_id = out.strip()
            self._initialized = True
            logger.info("DockerSandbox container initialized: %s (%s)", self._container_name, self._container_id[:12])

    async def aexecute(
        self,
        command: str,
        timeout: Optional[float] = None,
        workdir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> SandboxResult:
        """Execute command inside the Docker container via docker exec."""
        if not self._initialized:
            await self.ainitialize()

        effective_timeout = timeout if timeout is not None else self.config.default_timeout
        start_time = time.time()

        # Build execution directory
        exec_workdir = "/workspace"
        if workdir:
            clean_sub = workdir.strip().lstrip("/\\")
            exec_workdir = f"/workspace/{clean_sub}"

        exec_cmd = ["docker", "exec", "-w", exec_workdir]

        # Inject environment variables
        combined_env: Dict[str, str] = {}
        if self.config.environment_vars:
            combined_env.update(self.config.environment_vars)
        if env:
            combined_env.update(env)

        for k, v in combined_env.items():
            exec_cmd.extend(["-e", f"{k}={v}"])

        exec_cmd.extend([self._container_name, "/bin/sh", "-c", command])

        logger.debug("Executing in container %s: '%s'", self._container_name, command)
        code, stdout_text, stderr_text = await self._run_command(exec_cmd, timeout=effective_timeout)
        duration = time.time() - start_time
        is_timeout = code == 124

        # Truncate output streams if they exceed safety thresholds
        max_chars = self.config.max_output_chars
        if len(stdout_text) > max_chars:
            stdout_text = stdout_text[:max_chars] + f"\n... [STDOUT truncated, {len(stdout_text) - max_chars} chars omitted] ..."
        if len(stderr_text) > max_chars:
            stderr_text = stderr_text[:max_chars] + f"\n... [STDERR truncated, {len(stderr_text) - max_chars} chars omitted] ..."

        return SandboxResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=code,
            duration_seconds=round(duration, 3),
            is_timeout=is_timeout,
            metadata={"container": self._container_name, "command": command, "workdir": exec_workdir},
        )

    async def aread_file(self, relative_path: str, encoding: str = "utf-8") -> str:
        """Read file directly from the bind-mounted workspace on the host."""
        target_path = self.resolve_safe_path(relative_path)
        if not target_path.is_file():
            raise FileNotFoundError(f"File not found within sandbox: '{relative_path}'")
        return await asyncio.to_thread(target_path.read_text, encoding=encoding, errors="replace")

    async def awrite_file(self, relative_path: str, content: str, encoding: str = "utf-8") -> bool:
        """Write file directly into the bind-mounted workspace on the host."""
        target_path = self.resolve_safe_path(relative_path)

        def _sync_write() -> None:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding=encoding)

        await asyncio.to_thread(_sync_write)
        return True

    async def acheck_health(self) -> bool:
        """Check if Docker container is currently running."""
        if not self._initialized or not self._container_name:
            return False

        code, out, _ = await self._run_command(
            ["docker", "inspect", "-f", "{{.State.Running}}", self._container_name],
            timeout=5.0,
        )
        return code == 0 and out.strip().lower() == "true"

    async def acleanup(self) -> None:
        """Stop and remove the isolated Docker container."""
        async with self._lock:
            if not self._initialized:
                return

            logger.info("Cleaning up DockerSandbox container: %s", self._container_name)
            await self._run_command(["docker", "rm", "-f", self._container_name], timeout=15.0)
            self._container_id = None
            self._initialized = False