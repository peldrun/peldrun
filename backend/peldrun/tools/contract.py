# Path: backend/peldrun/tools/contract.py
"""
PELDRUN Core Tool Runtime and Policy Contracts.
Defines abstract execution protocols, centralized tool execution policies,
structured tool outcome models (ToolResult), and the opt-in output-streaming
context (ToolExecutionContext) that tools may consume to publish intermediate
stdout/stderr/progress while still running.

Maintains zero internal project dependencies to eliminate circular import chains.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional, Protocol, runtime_checkable
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


# ---------------------------------------------------------------------------
# Tool Output Streaming Contracts (opt-in)
# ---------------------------------------------------------------------------
#
# These protocols form the public boundary of the Tool Output Streaming
# feature. They are intentionally structural (Protocol, not ABC) so that
# any compatible implementation can be passed to a tool without forcing
# inheritance. Tools that do not wish to stream output simply ignore the
# ``context`` argument — the contract is 100% opt-in.


class ToolOutputChunk(Protocol):
    """
    Wire-level shape of a single streamed output fragment.

    This is the payload that the tool emits into the execution queue and
    that the agent loop forwards to the frontend as ``TOOL_OUTPUT`` /
    ``TOOL_PROGRESS`` events. It is deliberately a pure data shape with
    no behavior — tools do not need to construct it directly; the
    ``ToolExecutionContext`` implementation (see execution_manager.py)
    builds it from ``emit_output`` / ``emit_progress`` calls.

    Attributes
    ----------
    tool_call_id
        Correlates the chunk with the specific tool invocation that
        produced it. Matches ``ToolCall.id`` when the call originated
        from an LLM function call.
    tool_name
        Canonical tool identifier (``"shell"``, ``"mcp__fs__read"``...).
    stream
        Which logical channel the chunk belongs to. ``"stdout"`` and
        ``"stderr"`` are the raw process streams; ``"output"`` is a
        generic channel for tools that do not distinguish; ``"progress"``
        is reserved for structured progress updates (see
        ``emit_progress``).
    chunk
        The raw text fragment. For progress chunks this is typically
        the human-readable ``message``.
    sequence
        1-based monotonic counter, scoped to this tool call. Used by
        the frontend reducer to drop duplicates after SSE reconnects.
    attempt
        1-based retry attempt index. Used by the frontend reducer to
        separate output from a failed attempt from the output of a
        subsequent retry.
    """

    tool_call_id: str
    tool_name: str
    stream: Literal["stdout", "stderr", "output", "progress"]
    chunk: str
    sequence: int
    attempt: int


@runtime_checkable
class ToolExecutionContext(Protocol):
    """
    Execution-scoped context passed to tools that opt into output streaming.

    Lifecycle
    ---------
    One context is created per ``(tool_call_id, attempt)`` pair by the
    agent loop. The agent drains the context's backing queue in a
    background task and translates each item into an SSE event. The
    context MUST NOT be reused across attempts — ``attempt`` is bound
    at construction.

    Failure semantics
    -----------------
    - ``emit_output`` / ``emit_progress`` MUST NOT raise. If the
      underlying queue is full or the run has been cancelled, the
      implementation should drop the chunk silently (best-effort
      streaming) rather than fail the tool.
    - Callers MUST check ``PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING`` before
      producing large volumes of chunks. When streaming is disabled,
      the agent passes a no-op implementation whose methods return
      immediately, so tools that opt in unconditionally still run
      correctly with zero overhead.
    """

    async def emit_output(
        self,
        chunk: str,
        *,
        stream: Literal["stdout", "stderr", "output"] = "stdout",
    ) -> None:
        """
        Publish one raw output chunk.

        Parameters
        ----------
        chunk
            Text fragment to append. Empty strings are accepted (the
            consumer may filter them, but the contract does not
            require it).
        stream
            Channel discriminator. ``"stdout"`` and ``"stderr"`` are
            the raw process streams; ``"output"`` is a generic
            channel for tools whose output has no process semantics.
        """
        ...

    async def emit_progress(
        self,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
        phase: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """
        Publish a structured progress update.

        Progress events are orthogonal to output chunks: they carry a
        machine-readable percentage (``current``/``total``), an optional
        phase label (``"downloading"``, ``"extracting"``...), and an
        optional human-readable message. The frontend renders them in
        a dedicated progress bar, not in the text area that shows
        ``emit_output`` chunks.
        """
        ...


__all__ = [
    "ToolResult",
    "ToolExecutionPolicy",
    "ToolRuntime",
    "ToolOutputChunk",
    "ToolExecutionContext",
]
