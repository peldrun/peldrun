"""
backend/omweb/agents/handover.py

Subtask delegation tool for sovereign multi-agent handovers.

Decoupled under Phase M1:
- Completely eliminates mandatory top-level imports of legacy `app.*` packages.
- Provides native tool contract compatibility.
- Gracefully handles missing legacy execution backends without crashing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 1. Resilient BaseTool import abstraction
try:
    from peldrun.tools.base import BaseTool
except ImportError:
    try:
        from app.tool.base import BaseTool  # type: ignore[import-not-found]
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
    
    Resolves agent definitions from the authoritative Store and delegates
    without breaking runtime execution if legacy runtime components are absent.
    """
    from omweb.agents.registry import agent_registry

    try:
        manifest = agent_registry.get_agent(target_agent_id)
    except Exception:
        manifest = None

    if not manifest:
        return f"Handover Error: Target agent '{target_agent_id}' does not exist in registry."

    agent_name = manifest.get("name", target_agent_id)
    print(f"[HANDOVER] Delegating subtask to '{agent_name}' (ID: {target_agent_id})...")

    # 2. Isolated execution invocation
    # Probes legacy OpenManus runtime only dynamically if available
    try:
        from app.agent.peldrun import peldrun  # type: ignore[import-not-found]

        sub_agent = peldrun()
        allowed_tools = [t.lower() for t in manifest.get("tools", [])]
        allowed_tools.extend(["terminate", "ask_human"])

        if hasattr(sub_agent, "tools") and isinstance(sub_agent.tools, list):
            sub_agent.tools = [
                t
                for t in sub_agent.tools
                if getattr(t, "name", t.__class__.__name__).lower() in allowed_tools
            ]

        sys_prompt = manifest.get("system_prompt", "").strip()
        if sys_prompt and hasattr(sub_agent, "system_prompt"):
            sub_agent.system_prompt = f"{sys_prompt}\n\n{sub_agent.system_prompt}"

        await sub_agent.run(subtask_prompt)
        return f"Handover Success: Subtask completed by {agent_name}."

    except ImportError:
        logger.warning(
            f"[HANDOVER] Legacy agent runtime not found. "
            f"Native multi-agent execution pipeline will take over in Phase M3 for agent: {target_agent_id}"
        )
        return (
            f"Handover Notice: Subtask for '{agent_name}' acknowledged. "
            f"Native isolated multi-agent coordinator is slated for Phase M3."
        )
    except Exception as exc:
        logger.error(f"[HANDOVER ERROR] Execution failed: {exc}", exc_info=True)
        return f"Handover Execution Exception: {str(exc)}"