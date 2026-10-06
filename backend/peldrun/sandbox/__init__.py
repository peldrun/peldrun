"""
PELDRUN Core Sandbox Subsystem.
Exports base sandbox interfaces, execution result contracts, process and Docker sandbox implementations,
and a centralized factory function.
"""

from __future__ import annotations

from typing import Any, Optional

from peldrun.sandbox.base import BaseSandbox, SandboxConfig, SandboxResult
from peldrun.sandbox.docker_sandbox import DockerSandbox, DockerSandboxConfig
from peldrun.sandbox.local_process import LocalProcessSandbox


def get_sandbox(
    sandbox_type: str = "local",
    config: Optional[SandboxConfig] = None,
    workspace_root: Optional[str] = None,
    **kwargs: Any,
) -> BaseSandbox:
    """
    Factory function to instantiate execution sandboxes by type identifier.
    Supports 'local' (host-process) and 'docker' (containerized).
    """
    key = sandbox_type.lower().strip()

    if key in ("local", "process", "host"):
        cfg = config or SandboxConfig(**kwargs)
        if workspace_root:
            cfg.workspace_root = workspace_root
        return LocalProcessSandbox(config=cfg)

    elif key in ("docker", "container"):
        cmd_runner = kwargs.pop("cmd_runner", None)
        cfg = (
            config
            if isinstance(config, DockerSandboxConfig)
            else DockerSandboxConfig(**kwargs)
        )
        if workspace_root:
            cfg.workspace_root = workspace_root
        return DockerSandbox(config=cfg, cmd_runner=cmd_runner)

    else:
        raise ValueError(f"Unsupported sandbox type: '{sandbox_type}'. Expected 'local' or 'docker'.")


__all__ = [
    # Base contracts
    "BaseSandbox",
    "SandboxConfig",
    "SandboxResult",
    # Implementations
    "LocalProcessSandbox",
    "DockerSandbox",
    "DockerSandboxConfig",
    # Factory
    "get_sandbox",
]