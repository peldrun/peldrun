# backend/peldrun/agents/multi/protocol.py
"""
PELDRUN Core Multi-Agent Protocol.
Defines contracts for inter-agent delegation, structured sub-task requests,
and child execution results with full lineage tracking and budget governance under Phase M3.
"""

from __future__ import annotations

from enum import Enum
import time
from typing import Any, Dict, List, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator

from peldrun.artifacts.manifest import ArtifactRef
from peldrun.runtime.contract import DelegationBudget


class DelegationStatus(str, Enum):
    """Canonical lifecycle state machine for delegated sub-tasks."""
    PENDING = "pending"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        """Return True if delegation has reached an unresumable conclusion."""
        return self in (
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLED,
            DelegationStatus.TIMED_OUT,
        )


class DelegatedTask(BaseModel):
    """Payload representing a task delegated from a parent agent to a specialized child agent."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    task_id: UUID = Field(default_factory=uuid4, description="Unique identifier of the delegated task")
    parent_run_id: Union[UUID, str] = Field(..., description="Run identifier of the initiating parent agent")
    root_run_id: Optional[Union[UUID, str]] = Field(default=None, description="Root run identifier of tree")
    target_agent_id: str = Field(..., description="Identifier of the target specialist agent (e.g., 'coder', 'researcher')")
    instruction: str = Field(..., description="Specific objective or task instructions for the child agent")
    context_data: Dict[str, Any] = Field(default_factory=dict, description="Contextual parameters or shared variables")
    workspace_root: Optional[str] = Field(default=None, description="Workspace root directory for isolated sub-task")
    max_steps: Optional[int] = Field(default=None, description="Legacy maximum step iterations override")
    delegation_depth: int = Field(default=1, ge=0, le=5, description="Delegation tree depth of the child")
    budget: DelegationBudget = Field(default_factory=DelegationBudget, description="Resource and execution budget constraints")
    approval_request_id: Optional[str] = Field(default=None, description="Associated human approval ID if gate was required")
    created_at: float = Field(default_factory=time.time, description="Creation UTC epoch timestamp")

    @field_validator("parent_run_id", "root_run_id", mode="before")
    @classmethod
    def _coerce_run_ids(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return UUID(v.strip())
            except ValueError:
                return v.strip()
        return v


class DelegationResult(BaseModel):
    """Structured response returned upon completion of a delegated sub-task."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    task_id: UUID = Field(..., description="Matching identifier of the executed task")
    child_run_id: Union[UUID, str] = Field(..., description="Run identifier generated for child agent execution")
    success: bool = Field(..., description="Whether the sub-task completed successfully")
    status: DelegationStatus = Field(default=DelegationStatus.COMPLETED, description="Fine-grained execution status")
    output: str = Field(default="", description="Textual result or deliverable summary")
    error: Optional[str] = Field(default=None, description="Error message if sub-task failed")
    error_code: Optional[str] = Field(default=None, description="Categorized machine-readable error code")
    artifacts: List[ArtifactRef] = Field(default_factory=list, description="Deliverables produced during child execution")
    deliverables: List[str] = Field(default_factory=list, description="List of canonical deliverable relative paths")
    tokens_used: int = Field(default=0, ge=0, description="Total tokens consumed during sub-task execution")
    cost_nano_usd: int = Field(default=0, ge=0, description="Total cost incurred by sub-task in nano-USD")
    duration_seconds: float = Field(default=0.0, description="Elapsed execution time in seconds")
    parent_run_id: Optional[Union[UUID, str]] = Field(default=None, description="Parent run ID reference")
    root_run_id: Optional[Union[UUID, str]] = Field(default=None, description="Root run ID reference")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Auxiliary execution metadata")
