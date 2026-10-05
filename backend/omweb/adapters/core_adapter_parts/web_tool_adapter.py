"""
WebToolAdapter: bridges a platform tool manifest to a runtime executor.

The adapter is responsible for:
  * Normalizing the tool's parameter schema (falling back to canonical).
  * Switching into the workspace directory before invoking the executor.
  * Handling both sync and async executors transparently.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from .schemas import CANONICAL_TOOL_SCHEMAS


class WebToolAdapter:
    """Adapts platform tool manifests and binds them to genuine runtime execution logic."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Optional[Dict[str, Any]] = None,
        executor: Optional[Callable[..., Any]] = None,
        workspace_root: Optional[Union[str, Path]] = None,
        **kwargs: Any
    ) -> None:
        """
        Initialize the adapter.

        Args:
            name: Tool identifier (e.g. "bash", "web_search").
            description: Human-readable description for the LLM.
            parameters: JSON-schema-shaped parameter definition.  If missing
                or empty, the canonical schema for `name` is used.
            executor: The actual callable (sync or async) to invoke.
            workspace_root: Directory used as CWD during execution.
            **kwargs: Extra keyword arguments stored for future use.
        """
        self.name = name
        self.description = description

        resolved_params = parameters
        if not resolved_params or not resolved_params.get("properties"):
            canonical = CANONICAL_TOOL_SCHEMAS.get(name)
            if canonical:
                resolved_params = canonical
            else:
                resolved_params = {"type": "object", "properties": {}, "required": []}

        self.parameters = resolved_params
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else Path.cwd()
        self._executor = executor
        self.extra_kwargs = kwargs

    def to_param(self) -> Dict[str, Any]:
        """
        Return the tool in OpenAI / function-calling JSON form.
        """
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
                    "required": list(req) if isinstance(req, list) else []
                }
            }
        }

    def to_openai_schema(self) -> Dict[str, Any]:
        """Alias for :meth:`to_param` (kept for API compatibility)."""
        return self.to_param()

    async def execute(self, **kwargs: Any) -> Any:
        """
        Execute the bound executor inside the workspace directory.

        Handles the following edge cases:
          * Temporarily changes CWD to `workspace_root`, restores it after.
          * Injects `workspace_root` into the executor if its signature
            declares that parameter and the caller did not provide it.
          * Supports both sync and async executors transparently.
          * If no executor is bound, returns a stub string.
        """
        if self._executor is None:
            return f"[Tool '{self.name}' invoked with parameters: {kwargs}]"

        original_cwd = os.getcwd()
        try:
            if self.workspace_root and self.workspace_root.exists():
                os.chdir(str(self.workspace_root))

            call_kwargs = dict(kwargs)
            sig = inspect.signature(self._executor)
            if "workspace_root" in sig.parameters and "workspace_root" not in call_kwargs:
                call_kwargs["workspace_root"] = self.workspace_root

            if inspect.iscoroutinefunction(self._executor):
                return await self._executor(**call_kwargs)

            result = self._executor(**call_kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            try:
                os.chdir(original_cwd)
            except Exception:
                pass