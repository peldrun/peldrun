"""
PELDRUN Core Task Delegation Tool.
Allows autonomous primary agents to delegate complex sub-tasks
to registered specialized sub-agents.
"""

from __future__ import annotations

from typing import Any, Dict
from uuid import UUID
from pydantic import BaseModel, Field

from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask
from peldrun.tools.base import BaseTool, ToolResult


class DelegateParameters(BaseModel):
    target_agent: str = Field(
        ...,
        description="Identifier of the specialist agent to execute the sub-task (e.g., 'coder', 'researcher', 'analyst').",
    )
    instruction: str = Field(
        ...,
        description="Detailed prompt, code requirements, or research objective for the specialist.",
    )


class DelegateTool(BaseTool):
    """Tool enabling multi-agent delegation of sub-tasks."""

    name: str = "delegate_task"
    description: str = "Delegate a specialized sub-task to an expert sub-agent (e.g. coder, researcher) and receive the completed result."
    parameters: Dict[str, Any] = DelegateParameters.model_json_schema()

    def __init__(self, coordinator: MultiAgentCoordinator, parent_run_id: UUID):
        super().__init__()
        self.coordinator = coordinator
        self.parent_run_id = parent_run_id

    async def execute(self, target_agent: str, instruction: str, **kwargs: Any) -> ToolResult:
        task = DelegatedTask(
            parent_run_id=self.parent_run_id,
            target_agent_id=target_agent,
            instruction=instruction,
        )

        res = await self.coordinator.delegate(task)

        if res.success:
            return ToolResult(
                output=res.output,
                error="",
                exit_code=0,
                metadata={
                    "child_run_id": str(res.child_run_id),
                    "target_agent": target_agent,
                    "duration_seconds": res.duration_seconds,
                },
            )
        else:
            return ToolResult(
                output="",
                error=res.error or "Delegated task execution failed.",
                exit_code=1,
                metadata={
                    "child_run_id": str(res.child_run_id),
                    "target_agent": target_agent,
                },
            )