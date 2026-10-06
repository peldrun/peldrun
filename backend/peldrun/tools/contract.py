"""
backend/peldrun/tools/contract.py

PELDRUN Core Tool Runtime and Policy Contracts.
Defines abstract execution protocols, centralized tool execution policies,
and structured tool outcome models (ToolResult).
Maintains zero internal project dependencies to eliminate circular import chains.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict, Field


class ToolResult(BaseModel):
    """
    Structured execution outcome returned by all PELDRUN tools.
    Standardized payload for observation dispatches and state persistence.
    """
    model_config = ConfigDict(extra="allow")

    output: Any = Field(default=None, description="Primary output or response payload produced by tool")
    exit_code: int = Field(default=0, description="Process status indicator (0 = success, non-zero = failure)")
    is_error: bool = Field(default=False, description="Flag signaling execution failure")
    attempt: int = Field(default=1, description="Execution attempt index that produced this result")
    artifacts: List[str] = Field(default_factory=list, description="Relative file paths produced as artifacts")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary execution telemetry")

    @property
    def is_success(self) -> bool:
        """True if execution completed normally without error and exit code 0."""
        return not self.is_error and self.exit_code == 0

    @property
    def error(self) -> str:
        """Retrieve error details if execution failed, or empty string."""
        if self.is_error:
            return str(self.output or self.metadata.get("error", "Tool execution failed"))
        return ""

    def to_observation_dict(self) -> Dict[str, Any]:
        """Convert result into standard observation payload format."""
        return {
            "output": self.output,
            "exit_code": self.exit_code,
            "is_error": self.is_error,
            "attempt": self.attempt,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }


class ToolExecutionPolicy(BaseModel):
    """
    Central execution and retry policy governing tool invocations.
    Differentiates idempotent operations from destructive or side-effecting tasks.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    max_attempts: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Maximum sequential execution attempts allowed before declaring permanent failure.",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description="Timeout ceiling per individual execution attempt.",
    )
    retryable: bool = Field(
        default=False,
        description="Whether this tool permits automatic retries upon transient execution failures.",
    )
    idempotent: bool = Field(
        default=False,
        description="Whether multiple identical invocations produce the same side-effect-free outcome.",
    )
    side_effects: bool = Field(
        default=True,
        description="Whether the tool mutates external state, files, or executes irreversible actions.",
    )
    requires_approval: bool = Field(
        default=False,
        description="Whether operator confirmation is required prior to execution.",
    )
    backoff_factor: float = Field(
        default=1.0,
        ge=0.0,
        description="Base multiplier for exponential backoff calculations between attempts.",
    )

    def compute_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay in seconds for the given attempt index."""
        if attempt <= 1 or self.backoff_factor <= 0:
            return 0.0
        # Exponential backoff formula: backoff_factor * 2^(attempt - 2)
        delay = self.backoff_factor * math.pow(2, attempt - 2)
        return min(delay, 30.0)

    def is_retry_permitted(self, current_attempt: int, error: Optional[Exception] = None) -> bool:
        """
        Evaluate whether another execution attempt is legally permitted.
        Enforces strict guards against retrying non-idempotent or validation errors.
        """
        if current_attempt >= self.max_attempts:
            return False
        if not self.retryable:
            return False
        if self.side_effects and not self.idempotent:
            return False

        # Non-retryable error classes
        if error is not None:
            err_name = type(error).__name__
            if err_name in ("ValidationError", "ValueError", "PermissionError", "FileNotFoundError"):
                return False
            err_msg = str(error).lower()
            if "validation" in err_msg or "outside workspace" in err_msg or "permission denied" in err_msg:
                return False

        return True


@runtime_checkable
class ToolRuntime(Protocol):
    """
    Standard structural interface that any tool or external adapter must satisfy
    to be registered and dispatched inside PELDRUN Core.
    """

    name: str
    description: str

    def to_openai_schema(self) -> Dict[str, Any]:
        """Generate function-calling JSON Schema representation compliant with OpenAI specifications."""
        ...

    async def aexecute(self, **kwargs: Any) -> ToolResult:
        """Asynchronously execute tool logic and return standardized ToolResult."""
        ...

    def get_execution_policy(self) -> ToolExecutionPolicy:
        """Return the authoritative execution and retry policy for this tool."""
        ...


__all__ = [
    "ToolResult",
    "ToolExecutionPolicy",
    "ToolRuntime",
]