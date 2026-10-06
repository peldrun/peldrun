"""
backend/peldrun/runtime/contract.py

Canonical Runtime Invocations and Lifecycle Contract.
Defines standardized execution requests, handles, results, and human interaction specifications.
Adheres strictly to the PELDRUN Runtime V1 Durable Execution specification.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class RunRequest(BaseModel):
    """Standardized invocation request connecting Web or external API to Core."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    run_id: Union[uuid.UUID, str] = Field(
        default_factory=uuid.uuid4,
        description="Globally unique identifier for the execution run",
    )
    job_id: str = Field(..., description="Associated Web layer job tracking token")
    prompt: str = Field(..., description="Overarching task directive")
    agent_spec: AgentSpec = Field(..., description="Configured agent blueprint and capabilities")
    workspace: WorkspaceContext = Field(..., description="Filesystem sandbox boundary context")
    llm_config: Dict[str, Any] = Field(default_factory=dict, description="Provider credentials and model hyperparams")
    active_mcp_servers: List[Dict[str, Any]] = Field(default_factory=list, description="External MCP endpoints")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary tracing and telemetry headers")
    step_timeout_seconds: Optional[float] = Field(default=120.0, description="Per-step execution deadline")
    total_timeout_seconds: Optional[float] = Field(default=None, description="Global execution deadline ceiling")

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