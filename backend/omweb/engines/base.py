"""
backend/omweb/engines/base.py

Core Execution Engine Contracts and Runtime Context Interfaces.

Defines the universal execution protocol and immutable runtime payload
shared across all concrete agent execution engines in PELDRUN Web.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


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

    # [FIX-3.3] Structured file references selected by the user via the `@`
    # typeahead menu. Each entry is a dict with the shape:
    #   {"relPath": str, "fileName": str | None, "origin": str | None}
    #
    # The dispatcher is responsible for resolving and injecting their contents
    # as trusted context before the engine builds LLM messages. Engines that
    # don't consume this field simply ignore it.
    #
    # Defaults to None so that all existing engine instantiations remain
    # backward compatible without any call-site changes.
    context_refs: Optional[List[Dict[str, Any]]] = None


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
