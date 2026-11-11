"""
backend/peldrun/tools/collection.py

PELDRUN Core Tool Collection Architecture.
Aggregates and dispatches BaseTool instances and ToolRuntime implementations
with unified execution policies and structured ToolResult conversions.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import (
    ToolExecutionContext,
    ToolExecutionPolicy,
    ToolRuntime,
)
from peldrun.tools.execution_manager import ExecutionManager
from peldrun.tools.execution_result import ToolExecutionResult


logger = logging.getLogger("peldrun.tools.collection")


class ToolCollection:
    """Manages a collection of ToolRuntime / BaseTool instances for tool-calling agents."""

    def __init__(
        self,
        tools: Optional[List[Union[ToolRuntime, BaseTool]]] = None,
        execution_manager: Optional[ExecutionManager] = None,
    ) -> None:
        self._tools: Dict[str, Union[ToolRuntime, BaseTool]] = {}
        # [C.2 FIX] Optional dependency injection. A fresh
        # ExecutionManager is cheap to construct (it holds zero mutable
        # state), so the default is to create one per collection. Callers
        # that need a shared manager — e.g., a manager with a bound
        # registry for cross-name resolution — pass it explicitly.
        self._execution_manager: ExecutionManager = (
            execution_manager
            if execution_manager is not None
            else ExecutionManager()
        )
        if tools:
            for tool in tools:
                self.add_tool(tool)

    def add_tool(
        self,
        tool: Union[ToolRuntime, BaseTool],
        *,
        force: bool = False,
    ) -> None:
        """
        Register a tool or adapter instance in the collection.

        [P1-10 FIX — Collision detection]
        The historical implementation wrote
        ``self._tools[tool.name] = tool`` unconditionally, silently
        overwriting any previously-registered tool with the same name.
        That produced a class of "phantom" bugs the user could not
        diagnose: a bundle update, a plugin install, or a double
        ``ToolRegistry.register`` call could replace the tool the
        user selected with a different class — no warning, no log,
        no exception.

        Post-fix behavior (three explicit cases):

          * **Identical object identity** — no-op. Callers that
            re-register the SAME instance are safe (idempotent).
          * **Different class instance with the same name** — raises
            ``ValueError`` with a message naming both classes, so the
            bug surfaces at the call site instead of as a mysterious
            behavior change at runtime.
          * **``force=True``** — overwrite with a WARNING log entry
            so the collision remains visible in the audit trail.
            This is the escape hatch for a deliberate replacement
            (e.g. a migration that swaps in a newer implementation).
        """
        existing = self._tools.get(tool.name)
        if existing is not None and existing is not tool:
            existing_class = type(existing).__name__
            new_class = type(tool).__name__
            message = (
                f"Tool name collision: '{tool.name}' is already "
                f"registered as {existing_class}; refusing to "
                f"overwrite with {new_class}. Pass force=True to "
                f"override deliberately, or register the new tool "
                f"under a distinct name."
            )
            if not force:
                raise ValueError(message)
            logger.warning(message + " (force=True; overwriting)")
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[Union[ToolRuntime, BaseTool]]:
        """Retrieve a tool by unique name identifier."""
        return self._tools.get(name)

    def get_tool_policy(self, name: str) -> ToolExecutionPolicy:
        """
        Retrieve the execution policy for a specified tool.
        Defaults to safe one-shot policy if the tool does not declare a custom policy.
        """
        tool = self.get_tool(name)
        if tool is not None:
            if hasattr(tool, "get_execution_policy") and callable(tool.get_execution_policy):
                return tool.get_execution_policy()
            if hasattr(tool, "execution_policy") and isinstance(tool.execution_policy, ToolExecutionPolicy):
                return tool.execution_policy
        return ToolExecutionPolicy(max_attempts=1, retryable=False, side_effects=True)

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

    async def aexecute_with_result(
        self,
        name: str,
        arguments: Any = None,
        *,
        tool_call_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        run_id: Optional[str] = None,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """
        Execute a tool by name and return a canonical ``ToolExecutionResult``.

        [C.2 FIX — B5 + B6] The historical ``execute()`` method returns
        a raw ``ToolResult``, which lacks the ``status`` / ``outcome``
        fields the termination evaluator (B3) and the runtime executor
        need to decide whether a run is complete. This new method is
        the correct entry point for the agent loop: it delegates to
        ``ExecutionManager``, which enforces the ``ToolExecutionResult``
        contract:

          * exit_code is aligned with status (B4);
          * sync-only tools are offloaded to a worker thread so the
            event loop is never frozen (B6);
          * a per-call timeout produces ``.timeout()`` with the
            canonical exit code 124;
          * terminate metadata invariants are enforced at the boundary.

        The legacy ``execute()`` is preserved verbatim for backward
        compatibility. External tools and tests that rely on the raw
        ``ToolResult`` shape are unaffected.

        Retries
        -------
        This method does NOT retry. Retry semantics live in
        ``ToolCallAgent.act()`` — the agent decides whether a given
        failure is retriable based on the tool's
        ``ToolExecutionPolicy``, emits ``TOOL_RETRY`` events with
        attempt counts and delays, and applies backoff. A manager-level
        retry loop would duplicate that logic and lose the agent's
        visibility into each attempt.

        Parameters
        ----------
        name
            The registered tool name.
        arguments
            Arguments for the tool: dict, JSON-encoded string, or
            anything else (treated as ``{}`` after best-effort parse).
            Matches ``execute()`` semantics exactly.
        tool_call_id
            Correlation id (typically the originating ``ToolCall.id``).
            Forwarded to the result so callers can pair it with the
            assistant message that emitted the call.
        timeout_seconds
            Optional outer timeout. When ``None``, the tool's own
            ``execution_policy.timeout_seconds`` governs (via
            ``ExecutionManager._resolve_timeout``). For a ``BaseTool``,
            its ``aexecute()`` ALSO applies its own inner timeout — the
            outer wrapper is a safety net for adapters that do not.
        run_id
            Optional. Forwarded to ``ExecutionManager`` for log
            enrichment.
        context
            Optional streaming context. When supplied AND the target
            tool declares an explicit ``context`` parameter on its
            ``_arun`` / ``aexecute`` entrypoint, the manager forwards
            the context to the tool so it can publish intermediate
            output via ``await context.emit_output(...)`` (or
            ``context.emit_output_sync(...)`` for sync tools). When
            ``None``, no context is created and the tool runs exactly
            as before -- the feature is fully opt-in.

        Returns
        -------
        ToolExecutionResult
            NEVER raises. Resolution failures, argument-parse failures,
            timeouts, cancellations, and unexpected exceptions all
            produce a well-formed ``ToolExecutionResult``.
        """
        # ------------------------------------------------------------------
        # 1. Resolve the tool by name.
        # ------------------------------------------------------------------
        tool = self.get_tool(name)
        if tool is None:
            available = sorted(self._tools.keys())
            return ToolExecutionResult.failure(
                tool_name=name,
                tool_call_id=tool_call_id,
                error=(
                    f"Tool '{name}' not found in ToolCollection. "
                    f"Available tools: "
                    f"{', '.join(available) if available else '(none)'}."
                ),
                exit_code=1,
                metadata={"available_tools": available},
            )

        # ------------------------------------------------------------------
        # 2. Parse arguments — matches ``execute()`` semantics exactly.
        #    Three shapes are recognized:
        #
        #      * str   — JSON-encoded (returns empty dict for "")
        #      * dict  — used verbatim
        #      * None  — treated as {}
        #      * other — treated as {} (defensive: never crash on
        #                unexpected input, matching execute()).
        # ------------------------------------------------------------------
        parsed_args: Dict[str, Any]
        if isinstance(arguments, str):
            try:
                parsed_args = (
                    json.loads(arguments) if arguments.strip() else {}
                )
            except json.JSONDecodeError as exc:
                return ToolExecutionResult.failure(
                    tool_name=name,
                    tool_call_id=tool_call_id,
                    error=f"Failed to parse arguments as JSON: {exc}",
                    exit_code=1,
                    metadata={"error_type": "JSONDecodeError"},
                )
        elif isinstance(arguments, dict):
            parsed_args = arguments
        elif arguments is None:
            parsed_args = {}
        else:
            # Unknown shape — same degradation as execute(). Do NOT
            # raise: a caller that passed a malformed argument object
            # should see a well-formed failure result, not an exception.
            parsed_args = {}

        # ------------------------------------------------------------------
        # 3. Delegate to ExecutionManager.
        #
        # Passing ``tool_name=name`` explicitly (rather than relying on
        # the manager to read ``tool.name``) guarantees the returned
        # ``ToolExecutionResult.tool_name`` matches what the caller
        # requested — even if a registered adapter stores its name under
        # a different attribute (e.g. a wrapper class that overrides
        # ``__getattr__`` for proxying).
        # ------------------------------------------------------------------
        return await self._execution_manager.execute(
            tool,
            parsed_args,
            tool_name=name,
            tool_call_id=tool_call_id,
            timeout_seconds=timeout_seconds,
            run_id=run_id,
            context=context,
        )

    async def execute(self, name: str, arguments: Any) -> ToolResult:
        """
        Execute a tool single invocation attempt by name, standardizing arguments
        and delegating to the tool runtime instance.

        [LEGACY] This method is preserved unchanged for backward
        compatibility. New callers — in particular the agent loop
        (``ToolCallAgent.act``) after C.3 — MUST use
        ``aexecute_with_result`` instead, which returns the canonical
        ``ToolExecutionResult`` contract.
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
                metadata={"error_type": type(exc).__name__},
            )


__all__ = ["ToolCollection"]
