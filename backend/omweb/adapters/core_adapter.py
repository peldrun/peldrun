"""
PELDRUN Web OmWeb Tool Adapter.
Wraps an existing Web OmWeb tool executor into a peldrun-core BaseTool
preserving parameter schemas and contract execution.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional
from peldrun.tools.base import BaseTool, ToolResult


class WebToolAdapter(BaseTool):
    """Wraps an existing Web OmWeb tool executor into a peldrun-core BaseTool."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        executor: Callable[..., Any],
        workspace_root: Optional[str] = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.name = name
        self.description = description
        # Ensure schema structure conforms to OpenAI Function specification
        if isinstance(parameters, dict) and "type" in parameters:
            self.parameters = parameters
        elif isinstance(parameters, dict) and "properties" in parameters:
            self.parameters = {
                "type": "object",
                "properties": parameters.get("properties", {}),
                "required": parameters.get("required", []),
            }
        else:
            self.parameters = {
                "type": "object",
                "properties": {},
                "required": [],
            }
        self._executor = executor

    async def _arun(self, **kwargs: Any) -> ToolResult:
        """Internal asynchronous execution logic satisfying BaseTool."""
        try:
            res = await self._executor(**kwargs) if callable(self._executor) else None
            if isinstance(res, ToolResult):
                return res
            if isinstance(res, dict):
                return ToolResult(
                    output=str(res.get("output", "")),
                    exit_code=res.get("exit_code", 0),
                    is_error=bool(res.get("is_error", res.get("exit_code", 0) != 0)),
                    metadata=res.get("metadata", {}),
                )
            return ToolResult(output=str(res or ""), exit_code=0, is_error=False)
        except Exception as exc:
            return ToolResult(
                output=f"WebToolAdapter '{self.name}' failure: {str(exc)}",
                exit_code=1,
                is_error=True,
            )

    def _run(self, **kwargs: Any) -> ToolResult:
        """Fallback for synchronous execution."""
        raise NotImplementedError(f"WebToolAdapter '{self.name}' is asynchronous only.")


__all__ = ["WebToolAdapter"]