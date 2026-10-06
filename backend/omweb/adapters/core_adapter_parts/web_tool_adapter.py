"""
WebToolAdapter: bridges a platform tool manifest to a runtime executor.

The adapter is responsible for:
  * Implementing the Core ToolRuntime Protocol contract.
  * Normalizing the tool parameter schema.
  * Binding executors directly to workspace paths without mutating global process CWD.
  * Handling both sync and async executors transparently.
  * Standardizing raw execution outcomes into strict Core ToolResult instances.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

try:
    from peldrun.tools.base import ToolResult
    from peldrun.tools.contract import ToolRuntime
except ImportError:
    from typing import Protocol, runtime_checkable
    from pydantic import BaseModel, Field

    class ToolResult(BaseModel):  # type: ignore[no-redef]
        output: Any = Field(default=None)
        exit_code: int = Field(default=0)
        is_error: bool = Field(default=False)
        artifacts: List[str] = Field(default_factory=list)
        metadata: Dict[str, Any] = Field(default_factory=dict)

    @runtime_checkable
    class ToolRuntime(Protocol):  # type: ignore[no-redef]
        name: str
        description: str

        def to_openai_schema(self) -> Dict[str, Any]:
            ...

        async def aexecute(self, **kwargs: Any) -> ToolResult:
            ...

from .schemas import CANONICAL_TOOL_SCHEMAS

logger = logging.getLogger("omweb.adapters.web_tool_adapter")


class WebToolAdapter:
    """Adapts platform tool manifests and binds them to genuine runtime execution logic."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Optional[Dict[str, Any]] = None,
        executor: Optional[Callable[..., Any]] = None,
        workspace_root: Optional[Union[str, Path]] = None,
        **kwargs: Any,
    ) -> None:
        self.name: str = name
        self.description: str = description

        resolved_params = parameters
        if not resolved_params or not resolved_params.get("properties"):
            canonical = CANONICAL_TOOL_SCHEMAS.get(name)
            if canonical:
                resolved_params = canonical
            else:
                resolved_params = {"type": "object", "properties": {}, "required": []}

        self.parameters: Dict[str, Any] = resolved_params
        self.workspace_root: Path = Path(workspace_root).resolve() if workspace_root else Path.cwd()
        self._executor: Optional[Callable[..., Any]] = executor
        self.extra_kwargs: Dict[str, Any] = kwargs

    def set_workspace_root(self, workspace_root: Union[str, Path]) -> None:
        """Configure or update the active workspace root path."""
        self.workspace_root = Path(workspace_root).resolve()

    def set_workspace(self, workspace_root: Union[str, Path]) -> None:
        """Alias for set_workspace_root ensuring backward compatibility."""
        self.set_workspace_root(workspace_root)

    def to_param(self) -> Dict[str, Any]:
        """Return the tool in standard OpenAI function-calling JSON format."""
        props = self.parameters.get("properties", {}) if isinstance(self.parameters, dict) else {}
        req = self.parameters.get("required", []) if isinstance(self.parameters, dict) else []

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props if isinstance(props, dict) else {},
                    "required": list(req) if isinstance(req, list) else [],
                },
            },
        }

    def to_openai_schema(self) -> Dict[str, Any]:
        """Alias for to_param satisfying the Core ToolRuntime Protocol contract."""
        return self.to_param()

    def _standardize_result(self, raw_result: Any) -> ToolResult:
        """Converts arbitrary executor outcome into a canonical Core ToolResult."""
        if isinstance(raw_result, ToolResult):
            return raw_result

        if isinstance(raw_result, dict):
            if "output" in raw_result:
                return ToolResult(
                    output=raw_result.get("output"),
                    exit_code=int(raw_result.get("exit_code", 0)),
                    is_error=bool(raw_result.get("is_error", False)),
                    artifacts=list(raw_result.get("artifacts", [])),
                    metadata=dict(raw_result.get("metadata", {})),
                )
            if "error" in raw_result and raw_result["error"]:
                return ToolResult(
                    output=str(raw_result["error"]),
                    exit_code=1,
                    is_error=True,
                    metadata=raw_result,
                )
            return ToolResult(
                output=raw_result,
                exit_code=0,
                is_error=False,
                metadata={"source": "dictionary_payload"},
            )

        if isinstance(raw_result, str):
            return ToolResult(
                output=raw_result,
                exit_code=0,
                is_error=False,
            )

        if raw_result is None:
            return ToolResult(
                output=f"Tool '{self.name}' executed successfully with no output.",
                exit_code=0,
                is_error=False,
            )

        return ToolResult(
            output=raw_result,
            exit_code=0,
            is_error=False,
        )

    async def aexecute(self, **kwargs: Any) -> ToolResult:
        """Primary asynchronous entrypoint fulfilling the Core ToolRuntime protocol."""
        try:
            raw_result = await self.execute(**kwargs)
            return self._standardize_result(raw_result)
        except Exception as exc:
            logger.exception("Execution failed in WebToolAdapter for tool '%s': %s", self.name, exc)
            return ToolResult(
                output=f"Tool '{self.name}' execution failed: {str(exc)}",
                exit_code=1,
                is_error=True,
                metadata={"error": str(exc), "tool_name": self.name},
            )

    async def execute(self, **kwargs: Any) -> Any:
        """Execute the bound executor inside the isolated workspace context.

        Concurrency Safety: Eliminates process-wide os.chdir() to ensure completely
        isolated concurrent asynchronous executions.
        """
        if self._executor is None:
            return f"[Tool '{self.name}' invoked with parameters: {kwargs}]"

        call_kwargs = dict(kwargs)
        try:
            sig = inspect.signature(self._executor)
            if "workspace_root" in sig.parameters and "workspace_root" not in call_kwargs:
                call_kwargs["workspace_root"] = self.workspace_root
            if "cwd" in sig.parameters and "cwd" not in call_kwargs:
                call_kwargs["cwd"] = self.workspace_root
        except (ValueError, TypeError):
            pass

        if inspect.iscoroutinefunction(self._executor):
            return await self._executor(**call_kwargs)

        result = self._executor(**call_kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


__all__ = ["WebToolAdapter"]