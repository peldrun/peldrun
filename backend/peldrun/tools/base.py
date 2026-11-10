# Path: backend/peldrun/tools/base.py
"""
backend/peldrun/tools/base.py

PELDRUN Core Base Tool Architecture.
Defines abstract tool interfaces, schema reflections, and execution adapters.
Re-exports ToolResult and ToolExecutionPolicy from peldrun.tools.contract.

Tool Output Streaming
---------------------
``aexecute`` and ``execute`` accept an optional ``context`` argument of
type ``ToolExecutionContext``. This is the opt-in hook that lets tools
publish raw stdout/stderr chunks and structured progress events WHILE
they run, rather than only at the end via ``ToolResult``.

Backward compatibility guarantee
================================
- ``context`` defaults to ``None``. Existing tools whose ``_arun`` does
  NOT declare a ``context`` parameter run exactly as before — the base
  class inspects the subclass signature and only forwards ``context``
  when the subclass explicitly opted in.
- ``_arun`` subclasses that DO declare ``context`` receive the live
  context object, which they can use to call
  ``await context.emit_output(...)``.
- ``self._current_context`` is set for the duration of the call, so
  tools that prefer attribute access over parameter threading can read
  it from ``self``.

Why signature inspection instead of **kwargs forwarding
=======================================================
A subclass whose ``_arun`` signature is ``async def _arun(self, **kwargs)``
(like ``MCPToolAdapter``) would otherwise receive ``context`` inside
``**kwargs`` and forward it to the underlying tool as if it were a
domain argument. Signature inspection avoids that class of bug entirely:
the base class only forwards ``context`` when the subclass explicitly
declared it.

If a tool's ``args_schema`` happens to declare a field named ``context``,
that field is treated as a normal argument (as before) and the streaming
context is NOT available to that tool. Tool authors who want streaming
must rename their domain field; the alternative — silently shadowing
user data — would be strictly worse.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError

from peldrun.tools.contract import (
    ToolExecutionContext,
    ToolExecutionPolicy,
    ToolResult,
)

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
        # Holds the streaming context for the DURATION of one aexecute /
        # execute call, then restored in ``finally``. Tools that prefer
        # attribute access over parameter threading can read it here.
        # ``None`` when the caller did not supply a context (streaming
        # disabled, or calling code did not opt in).
        self._current_context: Optional[ToolExecutionContext] = None

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

    def _arun_accepts_context(self) -> bool:
        """
        Return True if ``self._arun`` declares an explicit ``context`` parameter.

        Only methods with a real ``context`` parameter in their signature
        are considered opt-in. A method that accepts ``**kwargs`` is NOT
        considered opt-in, because passing ``context`` inside ``**kwargs``
        would leak it into the tool's domain arguments — that is exactly
        the class of bug this signature inspection avoids.
        """
        try:
            sig = inspect.signature(self._arun)
        except (TypeError, ValueError):
            return False
        if "context" not in sig.parameters:
            return False
        param = sig.parameters["context"]
        # ``**kwargs`` named "context" is a bag, not a parameter. Reject it.
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return False
        return True

    async def _invoke_arun(
        self,
        context: Optional[ToolExecutionContext],
        validated_kwargs: Dict[str, Any],
    ) -> ToolResult:
        """
        Invoke ``_arun`` with the correct forwarding shape.

        - If the subclass declared ``context`` explicitly → forward it.
        - Otherwise → do NOT forward it, preserving legacy behavior.
        """
        if context is not None and self._arun_accepts_context():
            return await self._arun(context=context, **validated_kwargs)
        return await self._arun(**validated_kwargs)

    @abstractmethod
    async def _arun(self, **kwargs: Any) -> ToolResult:
        """Internal asynchronous tool execution logic to be implemented by subclasses."""
        ...

    def _run(
        self,
        context: Optional[ToolExecutionContext] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Resilient synchronous fallback bridging to _arun safely."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    lambda: asyncio.run(
                        self._invoke_arun(context, kwargs)
                    )
                )
                return future.result()
        else:
            return asyncio.run(self._invoke_arun(context, kwargs))

    async def aexecute(
        self,
        context: Optional[ToolExecutionContext] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Public asynchronous entrypoint executing a single attempt bounded by timeout.
        Exceptions are captured and returned as structured ToolResult records.

        ``context`` is the opt-in streaming hook — passing a non-None value
        enables tools that declared a ``context`` parameter in their
        ``_arun`` signature to publish live output while running. Tools
        that did not declare the parameter receive nothing new; they run
        exactly as before.
        """
        timeout_val = self.execution_policy.timeout_seconds
        # Expose the context on ``self`` for the duration of the call so
        # tools that prefer attribute access can use it. Restored to the
        # previous value in ``finally`` so nested tool calls (adapter →
        # inner tool) do not permanently clobber the outer one.
        previous_context = self._current_context
        self._current_context = context
        try:
            validated_kwargs = self._validate_arguments(**kwargs)
            exec_coro = self._invoke_arun(context, validated_kwargs)
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
        finally:
            self._current_context = previous_context

    def execute(
        self,
        context: Optional[ToolExecutionContext] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Public synchronous entrypoint for tool invocation.

        ``context`` has the same semantics as in ``aexecute`` — optional,
        opt-in, and only forwarded to subclasses that declared the
        parameter in their ``_arun`` signature.
        """
        previous_context = self._current_context
        self._current_context = context
        try:
            validated_kwargs = self._validate_arguments(**kwargs)
            return self._run(context=context, **validated_kwargs)
        except Exception as ex:
            logger.exception("Synchronous execution failed in tool '%s': %s", self.name, ex)
            return ToolResult(
                output=f"Error executing tool '{self.name}': {str(ex)}",
                exit_code=1,
                is_error=True,
                metadata={"error_type": type(ex).__name__},
            )
        finally:
            self._current_context = previous_context


__all__ = [
    "ToolResult",
    "BaseTool",
    "ToolExecutionContext",
]
