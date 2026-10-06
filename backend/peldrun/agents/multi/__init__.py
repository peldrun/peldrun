"""
PELDRUN Core Multi-Agent Architecture Package.
Exports delegation protocols, coordination engine, and multi-agent interaction tools.
"""

from peldrun.agents.multi.protocol import (
    DelegatedTask,
    DelegationResult,
)
from peldrun.agents.multi.coordinator import MultiAgentCoordinator

__all__ = [
    "DelegatedTask",
    "DelegationResult",
    "MultiAgentCoordinator",
]