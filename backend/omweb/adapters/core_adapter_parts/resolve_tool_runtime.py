"""
backend/omweb/adapters/core_adapter_parts/resolve_tool_runtime.py

Binds platform tool IDs to concrete runtime executors.
Directly maps human interaction tools to PELDRUN Core HumanInputTool.
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
    registry: ToolRegistry,
    emitter: Optional[Any] = None,
) -> WebToolAdapter:
    """
    Resolves platform tool ID to an executable WebToolAdapter with validated schemas.
    Prefers native PELDRUN Core tools when available.
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
        try:
            from peldrun.tools.builtins.human_input import HumanInputTool
            core_tool = HumanInputTool(workspace_root=str(workspace_root), emitter=emitter)
            executor = getattr(core_tool, "aexecute", getattr(core_tool, "_arun", core_tool.execute))
        except Exception:
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
        workspace_root=workspace_root,
    )