"""
Core Execution Engine Contracts and Runtime Context Interfaces.

Defines the universal execution protocol and immutable runtime payload
shared across all concrete agent execution engines in PELDRUN Web.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Protocol, runtime_checkable


@dataclass(frozen=True)
class EngineRunContext:
    """Immutable runtime payload delivered to an execution engine for a task."""

    job_id: str
    prompt: str
    agent_id: str
    active_llm: Dict[str, Any]
    model_name: str
    provider_name: str
    project_dir: Path
    chat_id: str
    manifest: Dict[str, Any]


@runtime_checkable
class ExecutionEngine(Protocol):
    """Universal execution contract required for all registered runtime engines."""

    engine_id: str

    async def run(self, context: EngineRunContext) -> None:
        """Execute an autonomous task within the engine's isolated lifecycle.

        Args:
            context: The immutable runtime configuration and parameters.
        """
        ...

    async def health(self) -> Dict[str, Any]:
        """Inspect and return runtime engine health, availability, and diagnostics.

        Returns:
            Dictionary containing health status, version metadata, and engine attributes.
        """
        ...