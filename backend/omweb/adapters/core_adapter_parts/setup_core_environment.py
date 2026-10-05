"""
setup_core_environment: top-level entry point that builds the LLM client,
resolves WebToolAdapter instances, and prepares Core ToolRegistry environments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from peldrun.llm.client import AsyncLLMClient

try:
    from peldrun.tools.registry import ToolRegistry as CoreToolRegistry
except ImportError:
    CoreToolRegistry = None  # type: ignore[assignment, misc]

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

from .resolve_tool_runtime import resolve_tool_runtime
from .web_tool_adapter import WebToolAdapter


def setup_core_registry(
    registry: ToolRegistry,
    requested_tool_ids: List[str],
    workspace_root: Optional[Union[str, Path]] = None
) -> Any:
    """
    Creates a dedicated session-scoped CoreToolRegistry and registers all resolved
    WebToolAdapter instances requested by the platform catalog.

    Args:
        registry: The platform tool registry (for catalog lookups).
        requested_tool_ids: Tool identifiers requested by the agent manifest.
        workspace_root: Working directory path for execution boundaries.

    Returns:
        CoreToolRegistry: An active in-memory Core ToolRegistry instance.
    """
    ws_path = Path(workspace_root).resolve() if workspace_root else Path.cwd()
    ws_str = str(ws_path)

    if CoreToolRegistry is not None:
        core_reg = CoreToolRegistry(workspace_root=ws_str)
    else:
        from peldrun.tools.collection import ToolCollection
        core_reg = ToolCollection()

    all_tools = {t["id"]: t for t in registry.list_tools() if t.get("is_enabled", True)}

    for tool_id in requested_tool_ids:
        if tool_id in all_tools:
            meta = all_tools[tool_id]
            adapter = resolve_tool_runtime(tool_id, meta, ws_path, registry)
            if hasattr(core_reg, "register"):
                core_reg.register(adapter)
            elif hasattr(core_reg, "add_tool"):
                core_reg.add_tool(adapter)

    return core_reg


def setup_core_environment(
    llm_provider: Any,
    registry: ToolRegistry,
    requested_tool_ids: List[str],
    workspace_root: Optional[Union[str, Path]] = None
) -> Tuple[AsyncLLMClient, List[WebToolAdapter]]:
    """
    Prepare the LLM client and the fully-resolved list of tool adapters
    requested by the platform for this conversation.

    Args:
        llm_provider: The (already configured) async LLM client.
        registry: The platform tool registry.
        requested_tool_ids: Tool identifiers the platform wants to expose.
        workspace_root: Optional workspace path; defaults to CWD.

    Returns:
        Tuple[AsyncLLMClient, List[WebToolAdapter]]:
            The same `llm_provider` (for chaining) and the list of adapters
            in the same order as `requested_tool_ids`.
    """
    ws_path = Path(workspace_root).resolve() if workspace_root else Path.cwd()
    all_tools = {t["id"]: t for t in registry.list_tools()}

    core_tools: List[WebToolAdapter] = []
    for tool_id in requested_tool_ids:
        if tool_id in all_tools:
            meta = all_tools[tool_id]
            adapter = resolve_tool_runtime(tool_id, meta, ws_path, registry)
            core_tools.append(adapter)

    return llm_provider, core_tools


__all__ = [
    "setup_core_environment",
    "setup_core_registry",
]