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

  3. ``mcp`` protocol server tools — dynamic discovery and remote
     invocation via mcp_session_pool and persistent MCPClient sessions.

  4. ``get_custom_tool_instance`` fallback — user-uploaded custom
     tools and bundle-installed tools are resolved through the Web
     ToolRegistry exactly as before.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

try:
    from omweb.tools.registry import ToolRegistry, tool_registry
except ImportError:
    from backend.omweb.tools.registry import ToolRegistry, tool_registry

from omweb.extensions.mcp_pool import mcp_session_pool
from omweb.extensions.registry import extension_registry

from .schemas import CANONICAL_TOOL_DESCRIPTIONS, CANONICAL_TOOL_SCHEMAS
from .tools_adapters._contract import AdapterRegistry, AdapterSpec
from .web_tool_adapter import WebToolAdapter

logger = logging.getLogger("omweb.adapters.resolve_tool_runtime")


def _parse_mcp_tool_id(tool_id: str) -> Optional[Tuple[str, str]]:
    """
    Parse an MCP tool identifier into (server_id, remote_tool_name).
    Supports both standard delimiter formats:
      - 'mcp::{server_id}::{tool_name}'
      - 'mcp__{server_id}__{tool_name}'
    """
    raw = str(tool_id or "").strip()
    if raw.startswith("mcp::"):
        parts = raw.split("::", 2)
        if len(parts) == 3:
            return parts[1].strip(), parts[2].strip()
    elif raw.startswith("mcp__"):
        parts = raw.split("__", 2)
        if len(parts) == 3:
            return parts[1].strip(), parts[2].strip()
    return None


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
    env_override: Optional[Dict[str, str]] = None,
) -> WebToolAdapter:
    """
    Resolve a platform tool ID to an executable ``WebToolAdapter``.

    Resolution order:
      1. ``ask_human`` / ``human_input`` special case.
      2. MCP protocol server tool resolution (dynamic remote call).
      3. AdapterRegistry lookup (built-in tool adapters).
      4. ``get_custom_tool_instance`` fallback (custom + bundle tools).
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

    # ── 1.5. MCP protocol server tool resolution ───────────────────
    mcp_target = _parse_mcp_tool_id(name)
    if mcp_target is None and meta.get("server_id") and (meta.get("remote_tool_name") or meta.get("remote_name")):
        mcp_target = (str(meta["server_id"]), str(meta.get("remote_tool_name") or meta.get("remote_name")))

    if executor is None and mcp_target is not None:
        mcp_server_id, mcp_remote_name = mcp_target
        manifest = extension_registry.get_effective_server(mcp_server_id)

        # Fallback parameters schema from MCP metadata if available
        if not params or not params.get("properties"):
            raw_input_schema = meta.get("inputSchema") or meta.get("parameters")
            if isinstance(raw_input_schema, dict) and raw_input_schema.get("properties"):
                params = copy.deepcopy(raw_input_schema)

        async def _mcp_tool_executor(**kwargs: Any) -> Any:
            # Re-fetch the effective server at execution time instead of
            # trusting the manifest captured at resolution time. This
            # closes three failure modes that the previous implementation
            # could not detect:
            #   1. Server was deleted between registration and invocation.
            #   2. Server was toggled from "active" to "ready" mid-run.
            #   3. Server configuration (command / url / env) was edited
            #      after the pool had already captured the old manifest.
            fresh_manifest = extension_registry.get_effective_server(mcp_server_id)
            if not fresh_manifest:
                raise RuntimeError(
                    f"MCP server '{mcp_server_id}' is no longer registered."
                )
            if fresh_manifest.get("status") != "active":
                raise RuntimeError(
                    f"MCP server '{mcp_server_id}' is not active."
                )

            # ── Lazy MCP auth resolution ──────────────────────────
            # This is the ONLY place where per-chat credentials are
            # resolved for MCP. Running it here — instead of eagerly
            # in peldrun_engine before the model is even called —
            # makes "hi" work without opening the auth modal, while
            # still prompting correctly the moment the agent
            # actually tries to use an MCP tool.
            #
            # We deliberately do NOT rely on the caller's
            # ``env_override`` being non-None for auth-required
            # servers: the pre-flight gate was removed because it
            # fired on every message regardless of whether MCP was
            # needed.
            effective_env_override = env_override

            if effective_env_override is None and job_id and chat_id:
                from omweb.extensions.chat_mcp_auth import (
                    get_chat_mcp_auth_store,
                )
                from omweb.extensions.mcp_auth_state import (
                    manifest_needs_auth,
                    mark_auth_required,
                )

                store = get_chat_mcp_auth_store()
                mode = store.get_mode(chat_id, mcp_server_id)

                if mode == "custom":
                    env = store.get_env(chat_id, mcp_server_id) or {}
                    oauth = store.get_oauth_tokens(chat_id, mcp_server_id)
                    if isinstance(oauth, dict) and oauth.get("access_token"):
                        env = dict(env)
                        env["_oauth_access_token"] = str(oauth["access_token"])
                        env["_oauth_token_type"] = str(
                            oauth.get("token_type") or "Bearer"
                        )
                    effective_env_override = env or None
                elif mode == "ephemeral":
                    env = store.get_ephemeral(chat_id, mcp_server_id) or {}
                    oauth = store.get_ephemeral_oauth(chat_id, mcp_server_id)
                    if isinstance(oauth, dict) and oauth.get("access_token"):
                        env = dict(env)
                        env["_oauth_access_token"] = str(oauth["access_token"])
                        env["_oauth_token_type"] = str(
                            oauth.get("token_type") or "Bearer"
                        )
                    effective_env_override = env or None
                elif mode == "use_global":
                    # Fall through to global settings via mcp_pool.
                    effective_env_override = None
                elif mode is None:
                    # No decision recorded. If the server genuinely
                    # requires credentials, signal the engine, emit
                    # the SSE event, and raise so this tool call
                    # fails cleanly.
                    if manifest_needs_auth(fresh_manifest):
                        mark_auth_required(
                            job_id,
                            chat_id=chat_id,
                            server_id=mcp_server_id,
                            manifest=fresh_manifest,
                        )

                        try:
                            from omweb.job_manager import job_manager
                            from omweb.sse_events import (
                                SSEEvent,
                                SSEEventType,
                                dispatch_event,
                            )

                            await dispatch_event(
                                job_id,
                                SSEEvent(
                                    type=SSEEventType.MCP_AUTH_REQUIRED,
                                    step=0,
                                    data={
                                        "server_id": mcp_server_id,
                                        "server_name": (
                                            fresh_manifest.get("name")
                                            or mcp_server_id
                                        ),
                                        "auth_method": str(
                                            fresh_manifest.get("auth_strategy")
                                            or "env"
                                        ),
                                        "env_schema": fresh_manifest.get(
                                            "env_schema"
                                        ),
                                        "oauth_supported": bool(
                                            fresh_manifest.get("url")
                                        ),
                                        "chat_id": chat_id,
                                        "seq": 0,
                                    },
                                ),
                            )
                            job_manager.fail_job(
                                job_id,
                                f"MCP_AUTH_REQUIRED: MCP server "
                                f"'{mcp_server_id}' requires authentication "
                                f"for this chat.",
                            )
                        except Exception as emit_err:  # noqa: BLE001
                            logger.warning(
                                "Failed to emit mcp_auth_required event: %s",
                                emit_err,
                            )

                        # Best-effort cancel so the agent loop does
                        # not keep taking additional steps after
                        # auth failure. Not fatal if it throws — the
                        # engine's post-run auth check handles the
                        # outcome either way.
                        try:
                            from peldrun.runtime.executor import (
                                core_executor as _core_executor,
                            )

                            _core_executor.cancel(job_id)
                        except Exception as cancel_err:  # noqa: BLE001
                            logger.debug(
                                "Could not cancel run after auth failure: %s",
                                cancel_err,
                            )

                        raise RuntimeError(
                            f"MCP_AUTH_REQUIRED: The '{mcp_server_id}' MCP "
                            f"server requires credentials for this chat. "
                            f"Authenticate and retry."
                        )

            client = await mcp_session_pool.get_or_create_client(
                server_id=mcp_server_id,
                manifest=fresh_manifest,
                workspace_root=str(ws_path),
                env_override=effective_env_override,
            )
            return await client.call_tool(
                tool_name=mcp_remote_name,
                arguments=kwargs,
            )

        executor = _mcp_tool_executor

    # ── 2. AdapterRegistry lookup ──────────────────────────────────
    if executor is None:
        spec = AdapterRegistry.lookup(name)
        if spec is not None:
            # M7.6: Include per-tool settings in the context so that
            # adapters declaring ``requires_context={"settings"}`` can
            # receive them via ``spec.matches_context()``. Adapters that
            # do not declare it are unaffected (the filter drops it).
            try:
                tool_settings = active_registry.get_tool_settings(name) if active_registry is not None else {}
            except Exception as exc:
                logger.warning(
                    "Failed to load settings for tool %r: %s", name, exc
                )
                tool_settings = {}

            context: Dict[str, Any] = {
                "search_config": search_config,
                "run_id": run_id,
                "job_id": job_id,
                "session_id": session_id,
                "chat_id": chat_id,
                "project_id": project_id,
                "settings": tool_settings,
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
        custom_inst = active_registry.get_custom_tool_instance(
            name, workspace_root=ws_path
        )
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

        # ── Server-ID expansion (delegation path) ─────────────────
        # A delegated child may be declared with bare server IDs
        # (e.g. ``["filesystem"]``) rather than concrete tool IDs.
        # This happens because the parent's manifest carries
        # ``mcp_servers`` and the coordinator forwards them straight
        # through. Expand each server ID into the full list of tool
        # IDs it exposes, using the per-server cache populated by the
        # parent's run phase.
        #
        # Cold-cache delegates skip silently (as before) because this
        # resolver is synchronous by design. This is best-effort by
        # contract: the parent run almost always warms the cache
        # before delegating, so the common path expands cleanly.
        if not tid.startswith(("mcp__", "mcp::")):
            server_manifest = extension_registry.get_effective_server(tid)
            if server_manifest and server_manifest.get("status") == "active":
                cached_tools = mcp_session_pool.get_cached_tools(tid)
                if cached_tools:
                    for tool_info in cached_tools:
                        remote_name = tool_info.get("name")
                        if not remote_name:
                            continue
                        expanded_id = f"mcp__{tid}__{remote_name}"
                        if expanded_id in seen_ids:
                            continue
                        seen_ids.add(expanded_id)
                        try:
                            adapted = resolve_tool_runtime(
                                tool_id=expanded_id,
                                tool_meta={
                                    "id": expanded_id,
                                    "server_id": tid,
                                    "remote_tool_name": remote_name,
                                    "parameters": tool_info.get("parameters", {}),
                                },
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
                                "MCP tool '%s' expansion failed during "
                                "delegation: %s",
                                expanded_id,
                                exc,
                            )
                else:
                    logger.debug(
                        "Skipping server-ID '%s' during delegation: "
                        "tool cache cold.",
                        tid,
                    )
                # The server ID is consumed regardless of cache state.
                # Do not attempt to resolve it as a regular tool (it
                # would silently fail — the id is not in the store).
                continue

        # Check if the identifier represents an external MCP tool
        mcp_target = _parse_mcp_tool_id(tid)
        if mcp_target is not None:
            mcp_srv_id, mcp_rem_name = mcp_target
            server_manifest = extension_registry.get_effective_server(mcp_srv_id)
            if not server_manifest or server_manifest.get("status") != "active":
                logger.debug(
                    "Skipping MCP tool %r because server %r is inactive or missing.",
                    tid,
                    mcp_srv_id,
                )
                continue
            tool_meta = {
                "id": tid,
                "server_id": mcp_srv_id,
                "remote_tool_name": mcp_rem_name,
            }
        else:
            tool_meta = {"id": tid}
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
