"""
PELDRUN Core Tool Runtime Contract.
Defines the standard abstract execution protocol for all tools and tool adapters
operating within the PELDRUN Core execution runtime.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

from peldrun.tools.base import ToolResult


@runtime_checkable
class ToolRuntime(Protocol):
    """
    Standard structural interface that any tool or external adapter must satisfy
    to be registered and dispatched inside PELDRUN Core.
    Completely decouples the core execution pipeline from Web storage, manifests,
    and platform administration concerns.
    """

    name: str
    description: str

    def to_openai_schema(self) -> Dict[str, Any]:
        """
        Generate and return the function-calling JSON Schema representation
        compliant with standard OpenAI-compatible LLM specifications.
        """
        ...

    async def aexecute(self, **kwargs: Any) -> ToolResult:
        """
        Asynchronously execute the tool logic against the supplied keyword arguments
        and return a standardized ToolResult instance.
        """
        ...


__all__ = ["ToolRuntime"]