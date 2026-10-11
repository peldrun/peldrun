# backend/peldrun/tools/builtins/delegate_tool.py
"""
PELDRUN Core Task Delegation Tool.
Enables autonomous primary agents to delegate complex sub-tasks
to registered specialized sub-agents with context injection, workspace scoping,
and strict resource budget propagation under Phase M3.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union
from uuid import UUID
from pydantic import BaseModel, Field

from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask
from peldrun.runtime.contract import DelegationBudget
from peldrun.tools.base import BaseTool, ToolResult


class DelegateParameters(BaseModel):
    """Parameters schema for the delegate_task tool."""
    target_agent: str = Field(
        ...,
        description="Identifier of the specialist agent to execute the sub-task (e.g., 'coder', 'researcher', 'analyst').",
    )
    instruction: str = Field(
        ...,
        description="Detailed objective, prompt, code requirements, or research directive for the specialist.",
    )
    context_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional contextual parameters or shared data to inject into child agent execution.",
    )


class DelegateTool(BaseTool):
    """Tool enabling multi-agent delegation of sub-tasks."""

    name: str = "delegate_task"
    description: str = (
        "Delegate a specialized sub-task to an expert sub-agent "
        "(e.g., 'coder', 'researcher', 'analyst') and receive verified findings."
    )
    parameters: Dict[str, Any] = DelegateParameters.model_json_schema()

    def __init__(
        self,
        coordinator: MultiAgentCoordinator,
        parent_run_id: Union[UUID, str],
        root_run_id: Optional[Union[UUID, str]] = None,
        delegation_depth: int = 0,
        budget: Optional[DelegationBudget] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.parent_run_id = parent_run_id
        self.root_run_id = root_run_id or parent_run_id
        self.delegation_depth = delegation_depth
        self.budget = budget or DelegationBudget()
        self.workspace_root = workspace_root

    async def execute(
        self,
        target_agent: str,
        instruction: str,
        context_data: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute task delegation to the target specialist agent with lineage and budget bounds."""
        ctx = context_data if context_data is not None else kwargs.get("context", {})
        task = DelegatedTask(
            parent_run_id=self.parent_run_id,
            root_run_id=self.root_run_id,
            target_agent_id=target_agent,
            instruction=instruction,
            context_data=ctx if isinstance(ctx, dict) else {},
            workspace_root=self.workspace_root,
            delegation_depth=self.delegation_depth + 1,
            budget=self.budget,
        )

        res = await self.coordinator.delegate(task)

        if res.success:
            return ToolResult(
                output=res.output,
                error="",
                exit_code=0,
                metadata={
                    "child_run_id": str(res.child_run_id),
                    "parent_run_id": str(res.parent_run_id),
                    "root_run_id": str(res.root_run_id),
                    "target_agent": target_agent,
                    "status": res.status.value if hasattr(res.status, "value") else str(res.status),
                    "duration_seconds": res.duration_seconds,
                    "tokens_used": res.tokens_used,
                    "cost_nano_usd": res.cost_nano_usd,
                    "deliverables": res.deliverables,
                    "artifacts_count": len(res.artifacts),
                },
            )
        else:
            return ToolResult(
                output="",
                error=res.error or "Delegated subtask execution failed.",
                exit_code=1,
                metadata={
                    "child_run_id": str(res.child_run_id),
                    "parent_run_id": str(res.parent_run_id),
                    "root_run_id": str(res.root_run_id),
                    "target_agent": target_agent,
                    "status": res.status.value if hasattr(res.status, "value") else str(res.status),
                    "error_code": res.error_code,
                    "duration_seconds": res.duration_seconds,
                },
            )
