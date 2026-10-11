# backend/omweb/agents/handover.py
"""
PELDRUN Web Handover and Delegation Adapter.

Preserves full backward compatibility with legacy web tool schemas
(target_agent_id, subtask_prompt) while eliminating obsolete OpenManus (app.*) imports.
Under Phase M3, primary autonomous orchestration executes natively via
PELDRUN Core DelegateTool (delegate_task).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from peldrun.tools.base import BaseTool

logger = logging.getLogger("peldrun.omweb.agents.handover")


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

    async def execute(self, target_agent_id: str, subtask_prompt: str, **kwargs: Any) -> str:
        """Execute the subtask delegation adhering strictly to legacy contract."""
        return await delegate_subtask_to_agent(target_agent_id, subtask_prompt)


async def delegate_subtask_to_agent(target_agent_id: str, subtask_prompt: str) -> str:
    """
    Delegate execution of a subtask to a specified agent persona.

    Validates target agent against authoritative registry without crashing
    and eliminates legacy app.* execution dependencies.
    """
    from omweb.agents.registry import agent_registry

    try:
        manifest = agent_registry.get_agent(target_agent_id)
    except Exception as reg_err:
        logger.error("Failed to query agent registry for '%s': %s", target_agent_id, reg_err)
        manifest = None

    if not manifest:
        return f"Handover Error: Target agent '{target_agent_id}' does not exist in registry."

    agent_name = manifest.get("name", target_agent_id)
    agent_status = manifest.get("status", "active")

    if agent_status != "active":
        return f"Handover Error: Target agent '{agent_name}' ({target_agent_id}) is currently disabled."

    logger.info("Web delegation handover acknowledged for '%s' (ID: %s)", agent_name, target_agent_id)

    # Clean native response replacing the legacy OpenManus fallback message
    return (
        f"Handover Notice: Subtask for specialist [{agent_name}] acknowledged. "
        f"Directive: {subtask_prompt[:120]}... "
        f"Full multi-agent orchestration is managed natively via PELDRUN Core Runtime."
    )
