# backend/omweb/adapters/core_adapter_parts/resolve_tool_runtime.py
"""
Binds platform tool IDs to concrete runtime executors.

Resolution strategy (in order):

  1. AdapterRegistry lookup — every built-in tool adapter (bash,
     python_execute, file_saver, file_reader, str_replace_editor,
     web_search, ask_human, browser_use) registers an AdapterSpec via
     its module in ``tools_adapters/``. The registry provides the
     factory callable and the set of context kwargs it accepts.

  2. ``ask_human`` special case — handled explicitly BEFORE registry
     lookup because it requires the ``emitter`` keyword to route
     durable HumanInputRequests to the Web layer. The registry has no
     visibility into the emitter, so this path cannot be expressed as
     an AdapterSpec.

  3. ``get_custom_tool_instance`` fallback — user-uploaded custom
     tools and bundle-installed tools are resolved through the Web
     ToolRegistry exactly as before.

Adding a new built-in tool adapter requires creating exactly one file
in ``tools_adapters/`` and no changes here. Adding a new *custom* tool
requires no changes here either (registry fallback handles it).
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

from .schemas import CANONICAL_TOOL_DESCRIPTIONS, CANONICAL_TOOL_SCHEMAS
from .tools_adapters._contract import AdapterRegistry, AdapterSpec
from .web_tool_adapter import WebToolAdapter

logger = logging.getLogger("omweb.adapters.resolve_tool_runtime")


# ---------------------------------------------------------------------------
# Canonical parameter resolution (unchanged)
# ---------------------------------------------------------------------------

def _resolve_canonical_parameters(
    name: str, tool_meta: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Resolve and sanitize the JSON Schema parameters for a tool.

    For ``web_search``, always enforce the canonical schema contract
    (no hard-coded default of 5 on ``max_results``, and bounds 1..20)
    even if ``tool_meta`` carries stale persisted schema properties.
    """
    if name == "web_search" and "web_search" in CANONICAL_TOOL_SCHEMAS:
        return copy.deepcopy(CANONICAL_TOOL_SCHEMAS["web_search"])

    params = tool_meta.get("parameters")
    if (
        isinstance(params, dict)
        and isinstance(params.get("properties"), dict)
        and params["properties"]
    ):
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


# ---------------------------------------------------------------------------
# Adapter invocation helper
# ---------------------------------------------------------------------------

def _invoke_adapter(
    spec: AdapterSpec,
    workspace_root: Path,
    context: Dict[str, Any],
) -> Callable[..., Any]:
    """
    Invoke an AdapterSpec's factory, forwarding only the context kwargs
    the adapter declared in ``requires_context``.

    This replaces the previous inspect.signature-based helper with a
    direct contract-driven filter, avoiding per-call introspection.
    """
    filtered = spec.matches_context(context)
    return spec.factory(workspace_root, **filtered)


# ---------------------------------------------------------------------------
# ask_human special case
# ---------------------------------------------------------------------------

def _resolve_ask_human(
    workspace_root: Path,
    emitter: Optional[Any],
) -> Optional[Callable[..., Any]]:
    """
    Resolve ``ask_human`` / ``human_input`` explicitly.

    Priority:
      1. HumanInputTool with emitter binding (interactive sessions).
      2. Factory from AdapterRegistry (non-interactive fallback).
    """
    try:
        from peldrun.tools.builtins.human_input import HumanInputTool

        core_tool = HumanInputTool(
            workspace_root=str(workspace_root), emitter=emitter
        )
        return getattr(
            core_tool,
            "aexecute",
            getattr(core_tool, "_arun", core_tool.execute),
        )
    except Exception as exc:
        logger.debug(
            "HumanInputTool direct binding failed (%s); falling back to "
            "AdapterRegistry.",
            exc,
        )

    spec = AdapterRegistry.lookup("ask_human")
    if spec is not None:
        try:
            return _invoke_adapter(spec, workspace_root, {})
        except Exception as exc:
            logger.warning(
                "AdapterRegistry fallback for ask_human failed: %s", exc
            )
    return None


# ---------------------------------------------------------------------------
# Primary resolution entry point
# ---------------------------------------------------------------------------

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
    Resolve a platform tool ID to an executable ``WebToolAdapter``.

    Resolution order:
      1. ``ask_human`` / ``human_input`` special case.
      2. AdapterRegistry lookup (built-in tool adapters).
      3. ``get_custom_tool_instance`` fallback (custom + bundle tools).
    """
    meta = dict(tool_meta or {})
    active_registry = registry or tool_registry
    ws_path = (
        workspace_root
        if isinstance(workspace_root, Path)
        else Path(str(workspace_root))
    )

    name = str(meta.get("id") or tool_id).strip()
    desc = str(
        meta.get("description")
        or CANONICAL_TOOL_DESCRIPTIONS.get(name, "")
    ).strip()
    params = _resolve_canonical_parameters(name, meta)

    executor: Optional[Callable[..., Any]] = None

    # ── 1. ask_human special case ──────────────────────────────────
    if name in ("ask_human", "human_input"):
        executor = _resolve_ask_human(ws_path, emitter)

    # ── 2. AdapterRegistry lookup ──────────────────────────────────
    if executor is None:
        spec = AdapterRegistry.lookup(name)
        if spec is not None:
            context: Dict[str, Any] = {
                "search_config": search_config,
                "run_id": run_id,
                "job_id": job_id,
                "session_id": session_id,
                "chat_id": chat_id,
                "project_id": project_id,
            }
            try:
                executor = _invoke_adapter(spec, ws_path, context)
            except Exception as exc:
                logger.warning(
                    "Adapter invocation failed for tool %r: %s", name, exc
                )
                executor = None

    # ── 3. Custom / bundle fallback ────────────────────────────────
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
    Build a workspace-isolated list of executable ``WebToolAdapter``
    instances strictly from an explicit allowlist of
    ``authorized_tool_ids``.

    Used by both primary runs and delegated child runs
    (Phase 7 — SR-701..SR-704) so child agents receive only their
    permitted tools bound to their own workspace.
    """
    active_registry = registry or tool_registry
    ws_path = (
        workspace_root
        if isinstance(workspace_root, Path)
        else Path(str(workspace_root))
    )
    resolved_tools: List[WebToolAdapter] = []
    seen_ids: set[str] = set()

    for raw_id in authorized_tool_ids or ():
        tid = str(raw_id or "").strip()
        if (
            not tid
            or tid in seen_ids
            or tid in ("terminate", "delegate_task", "delegate")
        ):
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
                                logger.debug(
                                    "Skipping globally disabled tool %r "
                                    "during resolution.",
                                    tid,
                                )
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
            logger.warning(
                "Failed to resolve authorized tool %r for workspace %s: %s",
                tid,
                ws_path,
                exc,
            )

    return resolved_tools


__all__ = ["resolve_authorized_tools", "resolve_tool_runtime"]
