"""
PELDRUN Core MCP Tool Adapter.
Adapts an MCP server endpoint tool to a native BaseTool usable by agents,
implementing the abstract _arun method.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.mcp_client import MCPClient


class MCPToolAdapter(BaseTool):
    """Adapts an MCP server endpoint tool to a native BaseTool usable by agents."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        mcp_client: MCPClient,
        remote_tool_name: Optional[str] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.name = name
        self.description = description
        self.parameters = parameters
        self.mcp_client = mcp_client
        self.remote_tool_name = remote_tool_name or name

    async def _arun(self, **kwargs: Any) -> ToolResult:
        """Internal asynchronous tool execution logic satisfying BaseTool."""
        try:
            raw_result = await self.mcp_client.call_tool(
                tool_name=self.remote_tool_name,
                arguments=kwargs,
            )

            if isinstance(raw_result, ToolResult):
                return raw_result

            output_text = ""
            if isinstance(raw_result, dict):
                content = raw_result.get("content", [])
                if isinstance(content, list):
                    texts = [
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    output_text = "\n".join(texts) if texts else json.dumps(raw_result)
                else:
                    output_text = json.dumps(raw_result)
            else:
                output_text = str(raw_result)

            return ToolResult(
                output=output_text,
                exit_code=0,
                is_error=False,
                metadata={"source": "mcp", "remote_name": self.remote_tool_name},
            )

        except Exception as exc:
            return ToolResult(
                output=f"MCP tool '{self.name}' execution failed: {str(exc)}",
                exit_code=1,
                is_error=True,
            )

    def _run(self, **kwargs: Any) -> ToolResult:
        """Fallback for synchronous execution."""
        raise NotImplementedError(f"MCPToolAdapter '{self.name}' is asynchronous only.")


__all__ = ["MCPToolAdapter"]