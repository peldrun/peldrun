"""
backend/omweb/agents/handover.py

Subtask delegation tool for sovereign multi-agent handovers.

Decoupled under PR 2 (OpenManus Removal):
- Completely eliminates legacy `app.*` imports and dependencies.
- Conforms strictly to native PELDRUN tool contracts.
- Explicitly reports subtask delegation status without legacy runtime invocations.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 1. Resilient BaseTool import abstraction
try:
    from peldrun.tools.base import BaseTool
except ImportError:
    # Standalone resilient tool base fallback
    class BaseTool:  # type: ignore[no-redef]
        name: str = ""
        description: str = ""
        parameters: Dict[str, Any] = {}

        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("Tool execution is not implemented.")


class DelegateSubtaskTool(BaseTool):
    """Tool allowing an agent to delegate an isolated subtask to another agent."""

    name: str = "delegate_subtask"
    description: str = (
        "Delegates an isolated subtask to another Sovereign Agent persona "
        "(e.g., 'code_architect', 'data_scientist', 'deep_researcher') and returns their findings."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "target_agent_id": {
                "type": "string",
                "description": "ID of the sovereign agent to delegate to",
            },
            "subtask_prompt": {
                "type": "string",
                "description": "Clear, detailed prompt for the delegated agent to execute",
            },
        },
        "required": ["target_agent_id", "subtask_prompt"],
    }

    async def execute(self, target_agent_id: str, subtask_prompt: str) -> str:
        """Execute the subtask delegation."""
        return await delegate_subtask_to_agent(target_agent_id, subtask_prompt)


async def delegate_subtask_to_agent(target_agent_id: str, subtask_prompt: str) -> str:
    """
    Delegate execution of a subtask to a specified agent persona.

    Legacy OpenManus execution path has been removed. Returns an explicit notice
    without performing unauthorized or uninstrumented external execution.
    """
    from omweb.agents.registry import agent_registry

    try:
        manifest = agent_registry.get_agent(target_agent_id)
    except Exception:
        manifest = None

    if not manifest:
        return f"Handover Error: Target agent '{target_agent_id}' does not exist in registry."

    agent_name = manifest.get("name", target_agent_id)
    logger.info(
        f"[HANDOVER] Subtask delegation requested for '{agent_name}' (ID: {target_agent_id}). "
        f"Legacy OpenManus runtime is removed."
    )

    return (
        f"Subtask delegation is not available in this version for '{agent_name}'. "
        f"No delegated execution was performed."
    )
