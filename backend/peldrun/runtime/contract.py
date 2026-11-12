# backend/peldrun/runtime/contract.py
"""
Canonical Runtime Invocations and Lifecycle Contract.
Defines standardized execution requests, handles, results, and human interaction specifications.
Adheres strictly to the PELDRUN Runtime V1 Durable Execution specification.
Extended under Phase M3 with typed delegation budgets, lineage tracing, and governance metadata.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

# [P1.5] Runtime import for the ``RunRequest.memory_context`` field's
# type annotation. ``MemoryContext`` is a lightweight domain value
# object (Pydantic + stdlib only -- no I/O, no storage, no framework
# coupling), so importing it eagerly here has no measurable cost and
# no circular-import risk: ``peldrun.memory.domain.context`` does NOT
# import anything from ``peldrun.runtime``.
#
# The eager import is DELIBERATE -- the engineering plan's reference
# spec suggested a TYPE_CHECKING-guarded import with a quoted forward
# reference, but that pattern is fragile under
# ``from __future__ import annotations``: Pydantic v2 would need
# ``RunRequest.model_rebuild()`` to be called explicitly after
# ``MemoryContext`` became importable, and no call site in the
# current tree guarantees that ordering. An eager import makes the
# annotation resolvable at class-creation time without any
# external ``model_rebuild()`` coordination.
from peldrun.memory.domain.context import MemoryContext


class RunStatus(str, Enum):
    """
    Canonical lifecycle execution states for PELDRUN Core runtime operations.
    Enforces unified status vocabulary across Web, Core Runner, and Frontend.
    """
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    RETRYING = "retrying"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Return True if the status represents an unresumable end state."""
        return self in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        """Return True if the runtime is actively executing processing cycles."""
        return self in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.RETRYING)

    @property
    def is_waiting(self) -> bool:
        """Return True if execution is paused or suspended waiting for human input."""
        return self in (RunStatus.WAITING_FOR_INPUT, RunStatus.PAUSED)


class HumanInputStatus(str, Enum):
    """Lifecycle statuses for individual interactive input requests."""
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class HumanInputRequest(BaseModel):
    """
    Durable Human-in-the-loop interaction contract.
    Decoupled from ephemeral in-memory coroutine states to support process restarts.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Globally unique identifier for this specific interaction point",
    )
    run_id: str = Field(..., description="Target execution run identifier")
    step_id: Optional[str] = Field(default=None, description="Step index or identifier where wait occurred")
    tool_call_id: Optional[str] = Field(default=None, description="Associated tool call identifier if tool-invoked")
    question: str = Field(..., description="Prompt or query presented to the human operator")
    input_type: str = Field(default="text", description="Type expected: text, confirmation, single_select, multi_select")
    options: List[str] = Field(default_factory=list, description="Available selection choices if restricted")
    status: HumanInputStatus = Field(default=HumanInputStatus.PENDING, description="Current resolution state")
    created_at: float = Field(default_factory=time.time, description="Creation UTC epoch timestamp")
    expires_at: Optional[float] = Field(default=None, description="Optional deadline epoch timestamp")
    answer: Optional[Any] = Field(default=None, description="Recorded response supplied by operator")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Auxiliary routing and context metadata")


class WorkspaceContext(BaseModel):
    """Defines isolated workspace path and parameters without mutating process cwd."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    workspace_id: str
    root_path: str
    chat_id: Optional[str] = None
    project_id: Optional[str] = None
    read_only: bool = False


class AgentSpec(BaseModel):
    """Specification of agent configuration derived from manifests."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str = ""
    system_prompt: str = ""
    tools: List[str] = Field(default_factory=list)
    max_steps: int = 30
    temperature: float = 0.7


class DelegationBudget(BaseModel):
    """
    Typed budget and resource execution ceilings enforced across child delegation runs.
    Defends against infinite delegation cascades and runaway LLM invocation costs.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    max_steps: int = Field(default=10, ge=1, le=50, description="Step count ceiling for child run")
    max_total_tokens: Optional[int] = Field(default=12_000, ge=1, description="Cumulative token limit across all child steps")
    max_cost_nano_usd: Optional[int] = Field(default=None, ge=1, description="Maximum cost ceiling in nano-USD (1e-9 USD)")
    timeout_seconds: float = Field(default=300.0, gt=0, le=1800.0, description="Hard wall-clock timeout in seconds")
    max_child_runs: int = Field(default=3, ge=0, le=10, description="Maximum number of child runs spawned by parent")
    max_parallel_children: int = Field(default=2, ge=1, le=4, description="Maximum concurrent active child executions")

    @field_validator("max_steps")
    @classmethod
    def _validate_max_steps(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_steps must be at least 1")
        return v


class RunRequest(BaseModel):
    """
    Standardized invocation request connecting Web or external API to Core.
    Equipped with complete lineage tracking and delegation governance parameters.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: Union[uuid.UUID, str] = Field(
        default_factory=uuid.uuid4,
        description="Globally unique canonical identifier for the execution run",
    )
    job_id: str = Field(..., description="Associated Web layer job tracking token")
    prompt: str = Field(..., description="Overarching task directive")
    agent_spec: AgentSpec = Field(..., description="Configured agent blueprint and capabilities")
    workspace: WorkspaceContext = Field(..., description="Filesystem sandbox boundary context")
    llm_config: Dict[str, Any] = Field(default_factory=dict, description="Provider credentials and model hyperparams")
    active_mcp_servers: List[Dict[str, Any]] = Field(default_factory=list, description="External MCP endpoints")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary tracing and telemetry headers")
    context_sidecar: Optional[Any] = Field(default=None, description="Context sidecar for multi-turn history assembly")
    project_rules: Optional[str] = Field(default=None, description="Explicit project execution rules")
    # [P1.5] Retrieved memory context for the current turn.
    #
    # Produced by ``dispatcher.run_instrumented`` /
    # ``dispatcher.run_direct_chat`` calling
    # ``MemoryService.retrieve(...)`` BEFORE constructing the
    # ``RunRequest``. Consumed by ``CoreRuntimeExecutor.execute``
    # (``executor.py``), which forwards it to
    # ``ContextManager.build_messages(memory_context=...)`` so the
    # ``[RETRIEVED MEMORY]`` block lands in the assembled system
    # prompt.
    #
    # ``None`` is the DEFAULT and the fast path: it means memory is
    # DISABLED, the NO_NEED route fired, or retrieval degraded to
    # empty. A ``None`` value produces a prompt bit-for-bit
    # identical to the pre-P1.5 no-memory case.
    #
    # ``exclude=True`` -- ``MemoryContext`` is TRANSIENT. It lives
    # only inside the live Python object. It MUST NOT be serialized
    # into ``ExecutionState``, checkpoints, sidecars, or any
    # ``model_dump()`` caller. The memory block is rebuilt fresh on
    # every turn from a fresh retrieval; there is no
    # "resume-from-old-context" path. See
    # ``peldrun.memory.domain.context``'s module docstring for the
    # full transience contract.
    memory_context: Optional[MemoryContext] = Field(
        default=None,
        exclude=True,
        description=(
            "Retrieved memory context for this turn. None means "
            "memory is disabled, the NO_NEED route fired, or "
            "retrieval degraded to empty. Forwarded by the executor "
            "to ContextManager.build_messages(); NEVER persisted."
        ),
    )
    step_timeout_seconds: Optional[float] = Field(default=120.0, description="Per-step execution deadline")
    total_timeout_seconds: Optional[float] = Field(default=None, description="Global execution deadline ceiling")

    # Multi-Agent Lineage and Governance
    parent_run_id: Optional[str] = Field(default=None, description="Run ID of the delegating parent agent if sub-run")
    root_run_id: Optional[str] = Field(default=None, description="Top-level root run ID of the execution tree")
    delegator_agent_id: Optional[str] = Field(default=None, description="Identifier of the delegating agent blueprint")
    delegation_depth: int = Field(default=0, ge=0, le=5, description="Nesting depth in delegation tree (0 = root)")
    delegation_allowed: bool = Field(default=False, description="Whether this agent is authorized to invoke delegation tools")
    budget: DelegationBudget = Field(default_factory=DelegationBudget, description="Typed resource and execution budget")
    approval_request_id: Optional[str] = Field(default=None, description="Durable human approval token if gate was invoked")

    @field_validator("run_id", mode="before")
    @classmethod
    def _coerce_run_id(cls, v: Any) -> Union[uuid.UUID, str]:
        if isinstance(v, uuid.UUID):
            return v
        if isinstance(v, str):
            try:
                return uuid.UUID(v.strip())
            except ValueError:
                return v.strip()
        return uuid.uuid4()


class RunHandle(BaseModel):
    """Lightweight reference handle returned immediately after queuing or starting a run."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: str
    job_id: str
    status: RunStatus = RunStatus.QUEUED
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """Final canonical execution outcome summary delivered upon run conclusion."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: str
    job_id: str
    status: RunStatus
    output: Optional[str] = None
    deliverables: List[str] = Field(default_factory=list)
    total_steps: int = 0
    error: Optional[str] = None
    error_code: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None

    # Lineage reflection
    parent_run_id: Optional[str] = Field(default=None, description="Parent run ID if this was a delegated sub-run")
    root_run_id: Optional[str] = Field(default=None, description="Root run ID of the execution tree")
    delegation_depth: int = Field(default=0, ge=0, description="Nesting level where this execution occurred")

class BudgetExceededError(RuntimeError):
    """Raised when an agent execution exceeds its cumulative token or cost budget."""

    def __init__(
        self,
        message: str,
        current_usage: int,
        limit: int,
        metric: str = "tokens",
    ) -> None:
        super().__init__(message)
        self.current_usage = current_usage
        self.limit = limit
        self.metric = metric
