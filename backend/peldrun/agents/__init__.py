"""
PELDRUN Core Agents Subsystem.
Exports all autonomous agent implementations, configuration specifications,
plan structures, and the centralized agent factory.
"""

from typing import Any

from .base import AgentConfig, BaseAgent
from .coding_agent import CodingAgent, CodingAgentConfig
from .planning_agent import Plan, PlanStep, PlanningAgent, PlanningAgentConfig
from .react_agent import ReActAgent, ReActAgentConfig
from .tool_call_agent import ToolCallAgent


def get_agent(agent_type: str, **kwargs: Any) -> BaseAgent:
    """
    Centralized agent factory routing initialization requests by identifier.

    Args:
        agent_type: Identifier string ('react', 'planning', 'coding', 'tool_call').
        **kwargs: Configuration and runtime dependencies forwarded to constructor.

    Returns:
        BaseAgent: Instantiated concrete agent instance.

    Raises:
        ValueError: If agent_type does not match a supported variant.
    """
    variant = str(agent_type).strip().lower()
    if variant in ("react", "react_agent"):
        return ReActAgent(**kwargs)
    elif variant in ("planning", "planning_agent", "plan"):
        return PlanningAgent(**kwargs)
    elif variant in ("coding", "coding_agent", "code"):
        return CodingAgent(**kwargs)
    elif variant in ("tool_call", "tool_call_agent", "toolcall"):
        return ToolCallAgent(**kwargs)
    else:
        raise ValueError(
            f"Unsupported agent type: '{agent_type}'. "
            f"Supported variants are: 'react', 'planning', 'coding', 'tool_call'."
        )


__all__ = [
    "BaseAgent",
    "AgentConfig",
    "ReActAgent",
    "ReActAgentConfig",
    "PlanningAgent",
    "PlanningAgentConfig",
    "Plan",
    "PlanStep",
    "CodingAgent",
    "CodingAgentConfig",
    "ToolCallAgent",
    "get_agent",
]