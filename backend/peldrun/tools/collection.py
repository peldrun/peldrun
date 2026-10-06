"""
PELDRUN Core Tool Collection Architecture.
Aggregates and executes BaseTool instances and ToolRuntime protocol implementations
with flexible invocation resilience and standardized outcome mapping.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolRuntime


class ToolCollection:
    """Manages a collection of ToolRuntime / BaseTool instances for tool-calling agents."""

    def __init__(self, tools: Optional[List[Union[ToolRuntime, BaseTool]]] = None) -> None:
        self._tools: Dict[str, Union[ToolRuntime, BaseTool]] = {}
        if tools:
            for tool in tools:
                self.add_tool(tool)

    def add_tool(self, tool: Union[ToolRuntime, BaseTool]) -> None:
        """Register a tool or adapter instance in the collection."""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[Union[ToolRuntime, BaseTool]]:
        """Retrieve a tool by unique name identifier."""
        return self._tools.get(name)

    def list_tools(self) -> List[Union[ToolRuntime, BaseTool]]:
        """Return all tools registered in the collection."""
        return list(self._tools.values())

    def __contains__(self, name: str) -> bool:
        """Check if tool name is present in collection."""
        return name in self._tools

    def __len__(self) -> int:
        """Return total count of active tools in collection."""
        return len(self._tools)

    def to_openai_schemas(self) -> List[Dict[str, Any]]:
        """Export all registered tools as OpenAI-compatible function definitions."""
        schemas: List[Dict[str, Any]] = []
        for tool in self._tools.values():
            if hasattr(tool, "to_openai_schema") and callable(tool.to_openai_schema):
                schemas.append(tool.to_openai_schema())
            else:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": getattr(tool, "description", ""),
                        "parameters": getattr(tool, "parameters", {}),
                    },
                })
        return schemas

    async def execute(self, name: str, arguments: Any) -> ToolResult:
        """
        Execute a tool by name, handling argument parsing and calling aexecute, _arun, or execute.
        Guarantees that the returned object is always an instance of ToolResult.
        """
        tool = self.get_tool(name)
        if not tool:
            return ToolResult(
                output=f"Tool '{name}' not found in ToolCollection.",
                exit_code=1,
                is_error=True,
            )

        parsed_args: Dict[str, Any]
        if isinstance(arguments, str):
            try:
                parsed_args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                return ToolResult(
                    output=f"Failed to parse arguments as JSON: {exc}",
                    exit_code=1,
                    is_error=True,
                )
        elif isinstance(arguments, dict):
            parsed_args = arguments
        else:
            parsed_args = {}

        try:
            res: Any
            if hasattr(tool, "aexecute") and callable(tool.aexecute):
                res = await tool.aexecute(**parsed_args)
            elif hasattr(tool, "_arun") and callable(tool._arun):
                res = await tool._arun(**parsed_args)
            elif hasattr(tool, "execute") and callable(tool.execute):
                res = tool.execute(**parsed_args)
                if hasattr(res, "__await__"):
                    res = await res
            else:
                return ToolResult(
                    output=f"Tool '{name}' has no executable entrypoint.",
                    exit_code=1,
                    is_error=True,
                )

            # Standardize output into strict ToolResult
            if isinstance(res, ToolResult):
                return res
            elif isinstance(res, dict):
                return ToolResult(**res)
            else:
                return ToolResult(output=res, exit_code=0, is_error=False)

        except Exception as exc:
            return ToolResult(
                output=f"Tool '{name}' execution raised an exception: {exc}",
                exit_code=1,
                is_error=True,
            )


__all__ = ["ToolCollection"]