"""
PELDRUN Core Base Tool Architecture.
Defines abstract tool interfaces, structured invocation results, automatic JSON Schema generation,
and a resilient synchronous-to-asynchronous execution bridge.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("peldrun.tools.base")


class ToolResult(BaseModel):
    """Structured execution outcome returned by all PELDRUN tools."""
    model_config = ConfigDict(extra="allow")

    output: Any = Field(default=None, description="Primary output or response payload produced by tool")
    exit_code: int = Field(default=0, description="Process status indicator (0 = success, non-zero = failure)")
    is_error: bool = Field(default=False, description="Flag signaling execution failure")
    artifacts: List[str] = Field(default_factory=list, description="Relative file paths produced as artifacts")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary execution telemetry")

    @property
    def is_success(self) -> bool:
        """True if execution completed normally without error and exit code 0."""
        return not self.is_error and self.exit_code == 0

    @property
    def error(self) -> str:
        """Retrieve error details if execution failed, or empty string."""
        if self.is_error:
            return str(self.output or self.metadata.get("error", "Tool execution failed"))
        return ""

    def to_observation_dict(self) -> Dict[str, Any]:
        """Convert result into standard observation payload format."""
        return {
            "output": self.output,
            "exit_code": self.exit_code,
            "is_error": self.is_error,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }


class BaseTool(ABC):
    """
    Abstract base class for all native tools executable within PELDRUN agents.
    Provides schema reflection, parameter validation, and workspace scoping.
    Satisfies the ToolRuntime Protocol.
    """

    name: str = ""
    description: str = ""
    args_schema: Optional[Type[BaseModel]] = None

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self.workspace_root = workspace_root

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
        """
        Resilient synchronous fallback bridging to _arun without raising NotImplementedError.
        Handles event loop resolution across synchronous threads and async contexts.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Run in a separate dedicated thread to prevent blocking or event loop conflicts
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: asyncio.run(self._arun(**kwargs)))
                return future.result()
        else:
            return asyncio.run(self._arun(**kwargs))

    async def aexecute(self, **kwargs: Any) -> ToolResult:
        """
        Public asynchronous entrypoint for tool invocation.
        Performs validation and handles unhandled exceptions safely.
        """
        try:
            validated_kwargs = self._validate_arguments(**kwargs)
            return await self._arun(**validated_kwargs)
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
        """
        Public synchronous entrypoint for tool invocation.
        Useful in non-async contexts or legacy script pipelines.
        """
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