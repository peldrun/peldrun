"""
backend/peldrun/tools/registry.py

PELDRUN Core Pure Runtime Tool Registry.
Provides an in-memory execution runtime registry for agent tool dispatch
and execution policy resolution.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional, Union

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.collection import ToolCollection
from peldrun.tools.contract import ToolExecutionPolicy, ToolRuntime

logger = logging.getLogger("peldrun.tools.registry")


class ToolRegistry:
    """
    In-memory Runtime Registry maintaining active ToolRuntime instances.
    Delegates execution and policy discovery directly to ToolCollection.
    """

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        tools: Optional[List[Union[ToolRuntime, BaseTool]]] = None,
    ) -> None:
        self.workspace_root = workspace_root
        self._collection = ToolCollection()
        if tools:
            for tool in tools:
                self.register(tool)

    def set_workspace_root(self, workspace_root: str) -> None:
        """Propagate active workspace root to all registered tools."""
        self.workspace_root = workspace_root
        for tool in self._collection.list_tools():
            if hasattr(tool, "set_workspace") and callable(tool.set_workspace):
                tool.set_workspace(workspace_root)
            elif hasattr(tool, "set_workspace_root") and callable(tool.set_workspace_root):
                tool.set_workspace_root(workspace_root)
            elif hasattr(tool, "workspace_root"):
                setattr(tool, "workspace_root", workspace_root)

    def register(self, tool: Union[ToolRuntime, BaseTool]) -> None:
        """Register a tool instance and apply workspace boundaries."""
        if self.workspace_root:
            if hasattr(tool, "workspace_root") and getattr(tool, "workspace_root") is None:
                if hasattr(tool, "set_workspace") and callable(tool.set_workspace):
                    tool.set_workspace(self.workspace_root)
                elif hasattr(tool, "set_workspace_root") and callable(tool.set_workspace_root):
                    tool.set_workspace_root(self.workspace_root)
                else:
                    setattr(tool, "workspace_root", self.workspace_root)

        self._collection.add_tool(tool)
        logger.debug("Registered tool '%s' into Core ToolRegistry", tool.name)

    def unregister(self, name: str) -> Optional[Union[ToolRuntime, BaseTool]]:
        """Remove a tool from the runtime registry by name."""
        return self._collection._tools.pop(name, None)

    def get(self, name: str) -> Optional[Union[ToolRuntime, BaseTool]]:
        """Retrieve a registered tool instance by name."""
        return self._collection.get_tool(name)

    def get_tool_policy(self, name: str) -> ToolExecutionPolicy:
        """Retrieve execution and retry policy for the designated tool."""
        return self._collection.get_tool_policy(name)

    def list_tools(self) -> List[Union[ToolRuntime, BaseTool]]:
        """Return all tools registered in the runtime session."""
        return self._collection.list_tools()

    def get_openai_schemas(self) -> List[Dict[str, Any]]:
        """Export all registered tool definitions as OpenAI-compatible function schemas."""
        return self._collection.to_openai_schemas()

    def to_openai_schemas(self) -> List[Dict[str, Any]]:
        """Alias for get_openai_schemas providing API uniformity."""
        return self.get_openai_schemas()

    async def aexecute(self, name: str, arguments: Any = None, **kwargs: Any) -> ToolResult:
        """Asynchronously dispatch tool execution by name."""
        if arguments is not None and not kwargs:
            return await self._collection.execute(name, arguments)
        elif kwargs:
            merged: Dict[str, Any] = dict(arguments) if isinstance(arguments, dict) else {}
            merged.update(kwargs)
            return await self._collection.execute(name, merged)
        else:
            return await self._collection.execute(name, {})

    def __contains__(self, name: str) -> bool:
        """Check if tool name is registered."""
        return name in self._collection

    def __len__(self) -> int:
        """Return the number of tools registered."""
        return len(self._collection)

    def __iter__(self) -> Iterator[Union[ToolRuntime, BaseTool]]:
        """Iterate over all registered tools."""
        return iter(self._collection.list_tools())


__all__ = ["ToolRegistry"]