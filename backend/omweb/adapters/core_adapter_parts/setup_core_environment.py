"""
setup_core_environment: top-level entry point that builds the LLM client
and the fully-bound list of tool adapters for a given conversation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from peldrun.llm.client import AsyncLLMClient

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

from .resolve_tool_runtime import resolve_tool_runtime
from .web_tool_adapter import WebToolAdapter


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
            in the same order as `requested_tool_ids` (unknown ids are
            silently skipped).
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