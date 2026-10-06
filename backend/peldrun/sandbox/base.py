"""
PELDRUN Core Sandbox Base Architecture.
Defines abstract sandbox interfaces, execution results, path boundary controls,
and environment configuration models.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.sandbox.base")


class SandboxResult(BaseModel):
    """Structured execution outcome returned by sandbox environments."""
    model_config = ConfigDict(extra="allow")

    stdout: str = Field(default="", description="Captured standard output stream")
    stderr: str = Field(default="", description="Captured standard error stream")
    exit_code: int = Field(default=0, description="Process return code (0 = success)")
    duration_seconds: float = Field(default=0.0, description="Elapsed execution time in seconds")
    is_timeout: bool = Field(default=False, description="Flag indicating if execution was halted due to timeout")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Auxiliary sandbox metrics")

    @property
    def is_success(self) -> bool:
        """True if process exited normally with return code 0 and did not timeout."""
        return self.exit_code == 0 and not self.is_timeout

    @property
    def combined_output(self) -> str:
        """Convenience property joining stdout and stderr."""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(f"[STDERR]\n{self.stderr.strip()}")
        return "\n".join(parts) if parts else ""


class SandboxConfig(BaseModel):
    """Configuration schema governing sandbox isolation policies."""
    model_config = ConfigDict(extra="ignore")

    workspace_root: Optional[str] = Field(
        default=None,
        description="Host filesystem path strictly bounding workspace interactions"
    )
    default_timeout: float = Field(
        default=60.0,
        ge=1.0,
        le=1200.0,
        description="Default execution ceiling in seconds"
    )
    max_output_chars: int = Field(
        default=50_000,
        ge=1000,
        description="Maximum characters captured per stdout/stderr stream before truncation"
    )
    environment_vars: Dict[str, str] = Field(
        default_factory=dict,
        description="Restricted environment variables forwarded to sandbox execution"
    )


class BaseSandbox(ABC):
    """
    Abstract base class for execution sandboxes.
    Guarantees consistent command execution, scoped filesystem access, and lifecycle controls.
    """

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        security_policy: Optional[Any] = None,
    ) -> None:
        self.config = config or SandboxConfig()
        self.security_policy = security_policy

    @property
    def workspace_root(self) -> Optional[str]:
        """Return configured workspace root boundary."""
        return self.config.workspace_root

    def set_workspace_root(self, workspace_root: str) -> None:
        """Update workspace root boundary."""
        self.config.workspace_root = workspace_root

    def resolve_safe_path(self, relative_path: str) -> Path:
        """
        Validate and resolve a path against the workspace boundary and security policy.
        Raises PermissionError if path attempts to traverse outside workspace root.
        """
        if not self.workspace_root:
            raise PermissionError("Sandbox workspace root is not configured. File access blocked.")

        root = Path(self.workspace_root).resolve()
        target = (root / relative_path.strip().lstrip("/\\")).resolve()

        if root != target and root not in target.parents:
            raise PermissionError(
                f"Access denied: Path '{relative_path}' escapes sandbox boundary '{root}'."
            )

        if self.security_policy is not None and hasattr(self.security_policy, "check_path_access"):
            self.security_policy.check_path_access(target)

        return target

    @abstractmethod
    async def ainitialize(self) -> None:
        """Prepare and bootstrap sandbox runtime resources."""
        ...

    @abstractmethod
    async def aexecute(
        self,
        command: str,
        timeout: Optional[float] = None,
        workdir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> SandboxResult:
        """Execute command inside the sandbox boundary asynchronously."""
        ...

    @abstractmethod
    async def aread_file(self, relative_path: str, encoding: str = "utf-8") -> str:
        """Read text file contents from within the sandbox."""
        ...

    @abstractmethod
    async def awrite_file(
        self,
        relative_path: str,
        content: str,
        encoding: str = "utf-8",
    ) -> bool:
        """Write text file contents safely inside the sandbox."""
        ...

    @abstractmethod
    async def acheck_health(self) -> bool:
        """Verify responsiveness and readiness of the sandbox runtime."""
        ...

    @abstractmethod
    async def acleanup(self) -> None:
        """Release allocated sandbox processes, containers, or temp files."""
        ...