"""
backend/omweb/adapters/core_adapter_parts/resolve_tool_runtime.py

Binds platform tool IDs to concrete runtime executors.
Directly maps human interaction tools to PELDRUN Core HumanInputTool,
enforces canonical search-result schema contracts for web_search, and
provides workspace-scoped tool resolution for both root and delegated runs.
"""

from __future__ import annotations

import copy
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

from .executors import RealToolExecutionFactory
from .schemas import CANONICAL_TOOL_DESCRIPTIONS, CANONICAL_TOOL_SCHEMAS
from .web_tool_adapter import WebToolAdapter

logger = logging.getLogger("omweb.adapters.resolve_tool_runtime")


def _resolve_canonical_parameters(name: str, tool_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve and sanitize the JSON Schema parameters for a tool.
    For `web_search`, always enforce the canonical schema contract (no hard-coded
    default of 5 on `max_results`, and bounds 1..20) even if `tool_meta` carries
    stale persisted schema properties.
    """
    if name == "web_search" and "web_search" in CANONICAL_TOOL_SCHEMAS:
        return copy.deepcopy(CANONICAL_TOOL_SCHEMAS["web_search"])

    params = tool_meta.get("parameters")
    if isinstance(params, dict) and isinstance(params.get("properties"), dict) and params["properties"]:
        resolved = copy.deepcopy(params)
        if name == "web_search" and "max_results" in resolved["properties"]:
            max_res_prop = resolved["properties"]["max_results"]
            if isinstance(max_res_prop, dict):
                max_res_prop.pop("default", None)
                max_res_prop["minimum"] = 1
                max_res_prop["maximum"] = 20
        return resolved

    canonical = CANONICAL_TOOL_SCHEMAS.get(name)
    if canonical is not None:
        return copy.deepcopy(canonical)

    return {"type": "object", "properties": {}, "required": []}


def _invoke_factory_method(
    factory_fn: Callable[..., Any],
    workspace_root: Path,
    **extra_kwargs: Any,
) -> Callable[..., Any]:
    """
    Invoke a RealToolExecutionFactory builder safely, forwarding optional keyword
    arguments only when supported by the factory method signature.
    """
    try:
        sig = inspect.signature(factory_fn)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw:
            filtered_kwargs = {k: v for k, v in extra_kwargs.items() if v is not None}
            return factory_fn(workspace_root, **filtered_kwargs)

        filtered_kwargs = {
            k: v for k, v in extra_kwargs.items() if k in params and v is not None
        }
        return factory_fn(workspace_root, **filtered_kwargs)
    except (TypeError, ValueError):
        return factory_fn(workspace_root)


def resolve_tool_runtime(
    tool_id: str,
    tool_meta: Optional[Dict[str, Any]],
    workspace_root: Union[Path, str],
    registry: Optional[ToolRegistry] = None,
    emitter: Optional[Any] = None,
    *,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    session_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
    search_config: Optional[Dict[str, Any]] = None,
) -> WebToolAdapter:
    """
    Resolves platform tool ID to an executable WebToolAdapter with validated schemas.
    Prefers native PELDRUN Core tools when available and binds every tool instance
    strictly to the provided `workspace_root`.
    """
    meta = dict(tool_meta or {})
    active_registry = registry or tool_registry
    ws_path = workspace_root if isinstance(workspace_root, Path) else Path(str(workspace_root))

    name = str(meta.get("id") or tool_id).strip()
    desc = str(meta.get("description") or CANONICAL_TOOL_DESCRIPTIONS.get(name, "")).strip()
    params = _resolve_canonical_parameters(name, meta)

    executor: Optional[Callable[..., Any]] = None

    if name in ("bash", "shell_exec", "isolated_shell"):
        executor = RealToolExecutionFactory.create_bash_executor(ws_path)
    elif name == "python_execute":
        executor = RealToolExecutionFactory.create_python_executor(ws_path)
    elif name == "file_saver":
        saver_builder = getattr(
            RealToolExecutionFactory,
            "create_file_saver_executor",
            RealToolExecutionFactory.create_str_replace_editor_executor,
        )
        executor = saver_builder(ws_path)
    elif name == "file_reader":
        reader_builder = getattr(
            RealToolExecutionFactory,
            "create_file_reader_executor",
            RealToolExecutionFactory.create_str_replace_editor_executor,
        )
        executor = reader_builder(ws_path)
    elif name == "str_replace_editor":
        executor = RealToolExecutionFactory.create_str_replace_editor_executor(ws_path)
    elif name == "web_search":
        executor = _invoke_factory_method(
            RealToolExecutionFactory.create_web_search_executor,
            ws_path,
            search_config=search_config,
            run_id=run_id,
            job_id=job_id,
            session_id=session_id,
            chat_id=chat_id,
            project_id=project_id,
        )
    elif name in ("browser_use", "chrome_browser", "browser"):
        browser_builder = getattr(
            RealToolExecutionFactory,
            "create_browser_executor",
            getattr(RealToolExecutionFactory, "create_browser_use_executor", None),
        )
        if callable(browser_builder):
            executor = browser_builder(ws_path)
    elif name in ("ask_human", "human_input"):
        try:
            from peldrun.tools.builtins.human_input import HumanInputTool

            core_tool = HumanInputTool(workspace_root=str(ws_path), emitter=emitter)
            executor = getattr(core_tool, "aexecute", getattr(core_tool, "_arun", core_tool.execute))
        except Exception:
            human_builder = getattr(
                RealToolExecutionFactory,
                "create_human_input_executor",
                getattr(RealToolExecutionFactory, "create_ask_human_executor", None),
            )
            if callable(human_builder):
                try:
                    executor = human_builder(ws_path)
                except TypeError:
                    executor = human_builder()

    if executor is None and active_registry is not None:
        custom_inst = active_registry.get_custom_tool_instance(name)
        if custom_inst and hasattr(custom_inst, "execute"):
            executor = custom_inst.execute

    return WebToolAdapter(
        name=name,
        description=desc,
        parameters=params,
        executor=executor,
        workspace_root=str(ws_path),
    )


def resolve_authorized_tools(
    authorized_tool_ids: Iterable[str],
    workspace_root: Union[Path, str],
    registry: Optional[ToolRegistry] = None,
    emitter: Optional[Any] = None,
    *,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    session_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
    search_config: Optional[Dict[str, Any]] = None,
) -> List[WebToolAdapter]:
    """
    Build a workspace-isolated list of executable WebToolAdapter instances
    strictly from an explicit allowlist of `authorized_tool_ids`.

    Used by both primary runs and delegated child runs (Phase 7 — SR-701..SR-704)
    so child agents receive only their permitted tools bound to their own workspace.
    """
    active_registry = registry or tool_registry
    ws_path = workspace_root if isinstance(workspace_root, Path) else Path(str(workspace_root))
    resolved_tools: List[WebToolAdapter] = []
    seen_ids: set[str] = set()

    for raw_id in authorized_tool_ids or ():
        tid = str(raw_id or "").strip()
        if not tid or tid in seen_ids or tid in ("terminate", "delegate_task", "delegate"):
            continue
        seen_ids.add(tid)

        tool_meta: Dict[str, Any] = {"id": tid}
        if active_registry is not None:
            for lookup_name in ("get_tool", "get_tool_meta", "get"):
                lookup_fn = getattr(active_registry, lookup_name, None)
                if callable(lookup_fn):
                    try:
                        found = lookup_fn(tid)
                        if isinstance(found, dict):
                            if found.get("enabled") is False:
                                logger.debug("Skipping globally disabled tool %r during resolution.", tid)
                                tool_meta = {"_skip": True}
                            else:
                                tool_meta = dict(found)
                            break
                    except Exception:
                        continue

        if tool_meta.get("_skip"):
            continue

        try:
            adapted = resolve_tool_runtime(
                tool_id=tid,
                tool_meta=tool_meta,
                workspace_root=ws_path,
                registry=active_registry,
                emitter=emitter,
                run_id=run_id,
                job_id=job_id,
                session_id=session_id,
                chat_id=chat_id,
                project_id=project_id,
                search_config=search_config,
            )
            resolved_tools.append(adapted)
        except Exception as exc:
            logger.warning("Failed to resolve authorized tool %r for workspace %s: %s", tid, ws_path, exc)

    return resolved_tools


__all__ = ["resolve_authorized_tools", "resolve_tool_runtime"]
