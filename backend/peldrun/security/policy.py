"""
PELDRUN Core Security Policy and Governance Engine.
Enforces strict boundaries around filesystem access, terminal execution,
network communications, and dangerous operations.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field


class SecurityViolationError(PermissionError):
    """Raised when an operation breaches runtime security constraints."""
    pass


class ActionRiskLevel(str, Enum):
    """Classification of action risk profiles."""
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    NETWORK_ACCESS = "network_access"
    SYSTEM_EXEC = "system_exec"
    CRITICAL_DESTRUCTIVE = "critical_destructive"


# Critical destructive patterns that are strictly prohibited by default
BLOCKED_COMMAND_PATTERNS: List[re.Pattern] = [
    re.compile(r"\brm\s+-[rRfF\s]*\s+(/|/\*|\*)(\s+|$)", re.IGNORECASE),  # rm -rf / or rm -rf /*
    re.compile(r"\brmdir\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\?", re.IGNORECASE), # Windows rmdir /s /q C:\
    re.compile(r"\bmkfs\b", re.IGNORECASE),                               # Format filesystem
    re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),                   # format C:
    re.compile(r":\(\)\{\s*:\s*\|\s*:\s*&\s*\};\s*:", re.IGNORECASE),    # Fork bomb
    re.compile(r"\bdd\s+if=.*\s+of=/dev/", re.IGNORECASE),               # Overwriting raw block devices
    re.compile(r"\b(shutdown|reboot|poweroff|init\s+0)\b", re.IGNORECASE),# System disruption
]

# Sensitive files that should never be read or written without explicit authorization
SENSITIVE_FILENAME_PATTERNS: List[str] = [
    ".env",
    "id_rsa",
    "id_ed25519",
    ".aws/credentials",
    ".git/config",
]


def validate_workspace_path(target_path: Path | str, workspace_root: Path | str) -> Path:
    """
    Validate that target_path resolves strictly within workspace_root.
    Prevents path traversal attacks (e.g., ../../../etc/passwd).
    """
    workspace_resolved = Path(workspace_root).resolve()
    target_resolved = Path(target_path).resolve()

    # Verify if target starts with workspace root
    try:
        target_resolved.relative_to(workspace_resolved)
    except ValueError as exc:
        raise SecurityViolationError(
            f"Access denied: Path traversal detected: '{target_path}' resolves outside workspace boundary '{workspace_root}'."
        ) from exc

    # Check for sensitive files
    for sensitive in SENSITIVE_FILENAME_PATTERNS:
        if sensitive in target_resolved.name or target_resolved.name == sensitive:
            raise SecurityViolationError(
                f"Access denied to sensitive file pattern: '{target_resolved.name}'."
            )

    return target_resolved


class SecurityPolicy(BaseModel):
    """
    Declarative runtime security governance configuration.
    Defines capabilities, limits, and human-in-the-loop triggers.
    """
    model_config = ConfigDict(extra="forbid")

    workspace_root: Path = Field(
        default_factory=lambda: Path(os.getcwd()).resolve(),
        description="Jailed root directory for all filesystem operations."
    )
    allow_network: bool = Field(
        default=True,
        description="Whether external outbound network calls are authorized."
    )
    allowed_hosts: Set[str] = Field(
        default_factory=set,
        description="Whitelisted hostnames when network access is constrained (empty allows all if allow_network=True)."
    )
    allow_shell_exec: bool = Field(
        default=True,
        description="Whether terminal shell execution is permitted."
    )
    require_human_approval_for_destructive: bool = Field(
        default=True,
        description="Prompt human confirmation before executing high-risk commands or writes."
    )
    max_file_size_bytes: int = Field(
        default=10 * 1024 * 1024,  # 10 MB
        description="Maximum allowed file size for reading/writing."
    )
    execution_timeout_seconds: float = Field(
        default=60.0,
        description="Hard deadline for sandboxed execution invocations."
    )

    def check_path_access(self, path: Path | str) -> Path:
        """Enforce workspace jailing on filesystem paths."""
        return validate_workspace_path(path, self.workspace_root)

    def check_command(self, command: str) -> None:
        """Inspect shell commands for blocked malicious signatures."""
        if not self.allow_shell_exec:
            raise SecurityViolationError("Access denied: Shell execution is disabled by security policy.")

        normalized = command.strip()
        for pattern in BLOCKED_COMMAND_PATTERNS:
            if pattern.search(normalized):
                raise SecurityViolationError(
                    f"Access denied: Command execution blocked due to destructive security signature: '{command}'."
                )

    def classify_action(self, tool_name: str, arguments: Dict[str, Any]) -> ActionRiskLevel:
        """Classify tool action into an ActionRiskLevel tier."""
        name_lower = tool_name.lower()

        if any(term in name_lower for term in ["bash", "shell", "exec", "terminal", "cmd"]):
            cmd = str(arguments.get("command", ""))
            # Check for high-risk write or deletion commands
            if any(term in cmd for term in ["rm ", "del ", "erase ", "drop ", "truncate"]):
                return ActionRiskLevel.CRITICAL_DESTRUCTIVE
            return ActionRiskLevel.SYSTEM_EXEC

        if any(term in name_lower for term in ["write", "save", "delete", "create", "append", "edit"]):
            return ActionRiskLevel.WORKSPACE_WRITE

        if any(term in name_lower for term in ["search", "web", "fetch", "http", "curl", "browser"]):
            return ActionRiskLevel.NETWORK_ACCESS

        return ActionRiskLevel.READ_ONLY

    def is_approval_required(self, risk: ActionRiskLevel) -> bool:
        """Determine if an action requires operator elevation/approval."""
        if self.require_human_approval_for_destructive and risk == ActionRiskLevel.CRITICAL_DESTRUCTIVE:
            return True
        return False