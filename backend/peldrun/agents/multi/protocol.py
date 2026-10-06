"""
PELDRUN Core Multi-Agent Protocol.
Defines contracts for inter-agent delegation, structured sub-task requests,
and child execution results.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from peldrun.artifacts.manifest import ArtifactRef


class DelegatedTask(BaseModel):
    """Payload representing a task delegated from a parent agent to a specialized child agent."""
    model_config = ConfigDict(extra="allow")

    task_id: UUID = Field(default_factory=uuid4, description="Unique identifier of the delegated task")
    parent_run_id: UUID = Field(..., description="Run identifier of the initiating parent agent")
    target_agent_id: str = Field(..., description="Identifier of the target specialist agent (e.g., 'coder', 'researcher')")
    instruction: str = Field(..., description="Specific objective or task instructions for the child agent")
    context_data: Dict[str, Any] = Field(default_factory=dict, description="Contextual parameters or shared variables")
    created_at: float = Field(default_factory=time.time, description="Creation timestamp")


class DelegationResult(BaseModel):
    """Structured response returned upon completion of a delegated sub-task."""
    model_config = ConfigDict(extra="allow")

    task_id: UUID = Field(..., description="Matching identifier of the executed task")
    child_run_id: UUID = Field(..., description="Run identifier generated for child agent execution")
    success: bool = Field(..., description="Whether the sub-task completed successfully")
    output: str = Field(default="", description="Textual result or deliverable summary")
    error: Optional[str] = Field(default=None, description="Error message if sub-task failed")
    artifacts: List[ArtifactRef] = Field(default_factory=list, description="Deliverables produced during child execution")
    duration_seconds: float = Field(default=0.0, description="Elapsed execution time")