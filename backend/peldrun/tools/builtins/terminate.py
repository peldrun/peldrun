"""
PELDRUN Core Terminate Tool.
Signals successful or failed termination of the agent execution loop.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult


class TerminateArgs(BaseModel):
    """Pydantic schema defining arguments for TerminateTool."""
    status: str = Field(default="success", description="The final status of the task execution (e.g. 'success', 'failure').")
    message: str = Field(default="", description="Summary or final message describing the result.")


# Backward-compatibility aliases
TerminateParameters = TerminateArgs


class TerminateTool(BaseTool):
    """Terminates the current agent execution loop."""

    name: str = "terminate"
    description: str = "Call this tool to terminate the current execution when the task is accomplished or cannot proceed."
    parameters: Dict[str, Any] = TerminateArgs.model_json_schema()

    def __init__(self, workspace_root: Optional[str] = None, **kwargs: Any):
        super().__init__()
        self.workspace_root = workspace_root or os.getcwd()

    async def _arun(self, status: str = "success", message: str = "", **kwargs: Any) -> ToolResult:
        """Asynchronous execution handler required by BaseTool abstract contract."""
        return await self.execute(status=status, message=message, **kwargs)

    async def execute(self, status: str = "success", message: str = "", **kwargs: Any) -> ToolResult:
        """Executes the termination action asynchronously."""
        try:
            msg = message or f"Task execution finished with status: {status}"
            return ToolResult(
                output=msg,
                error="",
                exit_code=0,
                metadata={
                    "status": status,
                    "terminated": True,
                },
            )
        except Exception as exc:
            return ToolResult(
                output="",
                error=f"Termination failure: {str(exc)}",
                exit_code=1,
            )


__all__ = [
    "TerminateArgs",
    "TerminateParameters",
    "TerminateTool",
]