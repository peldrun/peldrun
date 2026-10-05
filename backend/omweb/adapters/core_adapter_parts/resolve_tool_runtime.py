"""
resolve_tool_runtime: bind a platform tool id to a real runtime executor.

Given a tool id / metadata, this module decides which concrete executor
factory to invoke, then wraps the result inside a :class:`WebToolAdapter`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

from .schemas import CANONICAL_TOOL_SCHEMAS
from .executors import RealToolExecutionFactory
from .web_tool_adapter import WebToolAdapter


def resolve_tool_runtime(
    tool_id: str,
    tool_meta: Dict[str, Any],
    workspace_root: Path,
    registry: ToolRegistry
) -> WebToolAdapter:
    """
    Resolves any platform tool into a genuine, executable WebToolAdapter with validated schemas.

    The resolution order is:
      1. Known built-in tool names mapped to a concrete factory.
      2. A custom tool registered in `registry`.
      3. If neither matches, the adapter is created with no executor
         (returns a stub string when invoked).

    Args:
        tool_id: Platform tool identifier.
        tool_meta: Platform metadata dict for the tool.
        workspace_root: Resolved workspace path.
        registry: The platform tool registry (for custom tool lookup).

    Returns:
        WebToolAdapter: Ready-to-execute adapter instance.
    """
    name = tool_meta.get("id") or tool_id
    desc = tool_meta.get("description", "")
    params = tool_meta.get("parameters")

    if not params or not params.get("properties"):
        params = CANONICAL_TOOL_SCHEMAS.get(name, {"type": "object", "properties": {}, "required": []})

    executor: Optional[Callable[..., Any]] = None

    if name in ("bash", "shell_exec", "isolated_shell"):
        executor = RealToolExecutionFactory.create_bash_executor(workspace_root)
    elif name == "python_execute":
        executor = RealToolExecutionFactory.create_python_executor(workspace_root)
    elif name in ("str_replace_editor", "file_saver"):
        executor = RealToolExecutionFactory.create_str_replace_editor_executor(workspace_root)
    elif name == "web_search":
        executor = RealToolExecutionFactory.create_web_search_executor(workspace_root)
    elif name in ("browser_use", "chrome_browser", "browser"):
        executor = RealToolExecutionFactory.create_browser_executor(workspace_root)
    elif name in ("ask_human", "human_input"):
        executor = RealToolExecutionFactory.create_human_input_executor()

    if executor is None:
        custom_inst = registry.get_custom_tool_instance(name)
        if custom_inst and hasattr(custom_inst, "execute"):
            executor = custom_inst.execute

    return WebToolAdapter(
        name=name,
        description=desc,
        parameters=params,
        executor=executor,
        workspace_root=workspace_root
    )