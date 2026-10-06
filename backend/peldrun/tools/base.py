"""
backend/peldrun/tools/base.py

PELDRUN Core Base Tool Architecture.
Defines abstract tool interfaces, schema reflections, and execution adapters.
Re-exports ToolResult and ToolExecutionPolicy from peldrun.tools.contract.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError

from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

logger = logging.getLogger("peldrun.tools.base")


class BaseTool(ABC):
    """
    Abstract base class for all native tools executable within PELDRUN agents.
    Provides schema reflection, parameter validation, workspace scoping, and execution policy.
    Satisfies the ToolRuntime Protocol.
    """

    name: str = ""
    description: str = ""
    args_schema: Optional[Type[BaseModel]] = None
    execution_policy: ToolExecutionPolicy = ToolExecutionPolicy()

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        execution_policy: Optional[ToolExecutionPolicy] = None,
    ) -> None:
        self.workspace_root = workspace_root
        if execution_policy is not None:
            self.execution_policy = execution_policy
        elif not hasattr(self, "execution_policy") or self.execution_policy is None:
            self.execution_policy = ToolExecutionPolicy()

    def get_execution_policy(self) -> ToolExecutionPolicy:
        """Return the active execution policy governing this tool."""
        return self.execution_policy

    def set_execution_policy(self, policy: ToolExecutionPolicy) -> None:
        """Configure or override execution policy parameters for this tool instance."""
        self.execution_policy = policy

    def set_workspace(self, workspace_root: str) -> None:
        """Configure or update the bounded workspace root for this tool instance."""
        self.workspace_root = workspace_root

    def set_workspace_root(self, workspace_root: str) -> None:
        """Alias for set_workspace ensuring naming uniformity across runtime modules."""
        self.set_workspace(workspace_root)

    def to_openai_schema(self) -> Dict[str, Any]:
        """
        Generate strict OpenAI function-calling JSON schema representation.
        Reflects properties, descriptions, and required keys from args_schema.
        """
        parameters: Dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }

        if self.args_schema is not None:
            raw_schema = self.args_schema.model_json_schema()
            properties = raw_schema.get("properties", {})
            required = raw_schema.get("required", [])

            parameters["properties"] = properties
            parameters["required"] = required
            if "definitions" in raw_schema:
                parameters["definitions"] = raw_schema["definitions"]
            if "$defs" in raw_schema:
                parameters["$defs"] = raw_schema["$defs"]

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": parameters,
            },
        }

    def _validate_arguments(self, **kwargs: Any) -> Dict[str, Any]:
        """Validate raw keyword arguments against args_schema if provided."""
        if self.args_schema is None:
            return kwargs

        try:
            validated = self.args_schema(**kwargs)
            return validated.model_dump()
        except ValidationError as ex:
            raise ValueError(f"Invalid arguments for tool '{self.name}': {ex}") from ex

    @abstractmethod
    async def _arun(self, **kwargs: Any) -> ToolResult:
        """Internal asynchronous tool execution logic to be implemented by subclasses."""
        ...

    def _run(self, **kwargs: Any) -> ToolResult:
        """Resilient synchronous fallback bridging to _arun safely."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: asyncio.run(self._arun(**kwargs)))
                return future.result()
        else:
            return asyncio.run(self._arun(**kwargs))

    async def aexecute(self, **kwargs: Any) -> ToolResult:
        """
        Public asynchronous entrypoint executing a single attempt bounded by timeout.
        Exceptions are captured and returned as structured ToolResult records.
        """
        timeout_val = self.execution_policy.timeout_seconds
        try:
            validated_kwargs = self._validate_arguments(**kwargs)
            exec_coro = self._arun(**validated_kwargs)
            if timeout_val and timeout_val > 0:
                result = await asyncio.wait_for(exec_coro, timeout=timeout_val)
            else:
                result = await exec_coro

            if isinstance(result, ToolResult):
                return result
            elif isinstance(result, dict):
                return ToolResult(**result)
            else:
                return ToolResult(output=result, exit_code=0, is_error=False)

        except asyncio.TimeoutError:
            msg = f"Tool '{self.name}' execution timed out after {timeout_val} seconds."
            logger.warning(msg)
            return ToolResult(
                output=msg,
                exit_code=124,
                is_error=True,
                metadata={"error_type": "TimeoutError", "timeout_seconds": timeout_val},
            )
        except ValueError as val_err:
            logger.warning("Validation error in tool '%s': %s", self.name, val_err)
            return ToolResult(
                output=str(val_err),
                exit_code=1,
                is_error=True,
                metadata={"error_type": "ValidationError"},
            )
        except Exception as ex:
            logger.exception("Unexpected execution error in tool '%s': %s", self.name, ex)
            return ToolResult(
                output=f"Error executing tool '{self.name}': {str(ex)}",
                exit_code=1,
                is_error=True,
                metadata={"error_type": type(ex).__name__},
            )

    def execute(self, **kwargs: Any) -> ToolResult:
        """Public synchronous entrypoint for tool invocation."""
        try:
            validated_kwargs = self._validate_arguments(**kwargs)
            return self._run(**validated_kwargs)
        except Exception as ex:
            logger.exception("Synchronous execution failed in tool '%s': %s", self.name, ex)
            return ToolResult(
                output=f"Error executing tool '{self.name}': {str(ex)}",
                exit_code=1,
                is_error=True,
                metadata={"error_type": type(ex).__name__},
            )


__all__ = [
    "ToolResult",
    "BaseTool",
]