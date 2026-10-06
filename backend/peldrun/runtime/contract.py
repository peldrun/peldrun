from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class WorkspaceContext(BaseModel):
    """Defines isolated workspace path and parameters without mutating process cwd."""
    workspace_id: str
    root_path: str
    chat_id: Optional[str] = None
    project_id: Optional[str] = None
    read_only: bool = False


class AgentSpec(BaseModel):
    """Specification of agent configuration derived from manifests."""
    id: str
    name: str = ""
    system_prompt: str = ""
    tools: List[str] = Field(default_factory=list)
    max_steps: int = 30
    temperature: float = 0.7


class RunRequest(BaseModel):
    """Standardized invocation request connecting Web or external API to Core."""
    run_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    job_id: str
    prompt: str
    agent_spec: AgentSpec
    workspace: WorkspaceContext
    llm_config: Dict[str, Any] = Field(default_factory=dict)
    active_mcp_servers: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)