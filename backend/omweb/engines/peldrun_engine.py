"""
backend/omweb/engines/peldrun_engine.py

PELDRUN Native Embedded Execution Engine.
Executes autonomous workflows by delegating to peldrun-core via RunRequest.
Integrates ContextManager to enforce strict isolation between persistent history
and LLM context.

Design principle: Each agent's manifest is authoritative. The engine does not
impose tool-calling policies, loop detection, watchdogs, or artificial limits.
The agent's own system_prompt, tools list, and max_steps govern its behavior.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from .base import EngineRunContext, ExecutionEngine

# Core Public Boundary Imports
from peldrun.context.builder import ContextManager
from peldrun.context.generative_ui import build_generative_ui_prompt
from peldrun.context.sidecar import build_context_sidecar
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.runtime.contract import AgentSpec, RunRequest, RunStatus, WorkspaceContext
from peldrun.runtime.executor import core_executor
from peldrun.runtime.pricing_store import NANO_USD_PER_USD
from peldrun.tools.builtins.human_input import HumanInputRegistry

logger = logging.getLogger(__name__)


def _collect_specialist_tool_allowlists(
    enabled_tool_ids: Set[str],
) -> Dict[str, List[str]]:
    """Build a mapping of specialist agent IDs to their effective tool IDs.

    Uses ``get_agent_tools_state`` so user-saved tool overrides
    (disabled_tools / extra_tools) are honoured when an agent delegates
    to a specialist. Falls back to the raw manifest ``tools`` field if
    the effective lookup fails for any agent, so a corrupted settings
    file cannot break delegation discovery.
    """
    specialist_map: Dict[str, List[str]] = {}
    try:
        from omweb.agents.registry import agent_registry

        agents_list: List[Dict[str, Any]] = []
        if hasattr(agent_registry, "list_agents") and callable(agent_registry.list_agents):
            agents_list = list(agent_registry.list_agents() or [])

        for ag in agents_list:
            if not isinstance(ag, dict):
                continue
            ag_id = str(ag.get("id") or "").strip().lower()
            if not ag_id:
                continue

            # Prefer the effective tool list so overrides are honoured.
            tools_source: List[str] = []
            try:
                state = agent_registry.get_agent_tools_state(ag_id)
                tools_source = list(state.get("effective_tools") or [])
            except Exception:
                raw_tools = ag.get("tools")
                tools_source = list(raw_tools) if isinstance(raw_tools, list) else []

            # MCP tool identifiers (``mcp__server__tool`` or
            # ``mcp::server::tool``) must always pass through to the
            # coordinator, because they are not registered in the Web
            # tool registry (`enabled_tool_ids`). The coordinator
            # resolves them at delegation time via `resolve_tool_runtime`
            # → MCP session pool. Filtering on `enabled_tool_ids` alone
            # would silently strip every MCP tool from delegated
            # children.
            filtered: List[str] = []
            seen_specialist_tools: Set[str] = set()
            for raw_t in tools_source:
                t = str(raw_t).strip()
                if not t or t in ("terminate", "delegate_task", "delegate"):
                    continue
                if t in enabled_tool_ids or t.startswith(("mcp__", "mcp::")):
                    if t not in seen_specialist_tools:
                        seen_specialist_tools.add(t)
                        filtered.append(t)

            # ── Expand the specialist's declared MCP servers ─────
            # ``mcp_servers`` on a manifest is a list of *server* ids
            # (e.g. ``["filesystem"]``), not tool ids. Materialise each
            # into the concrete tools it exposes so the coordinator can
            # forward them to the child. We read from the per-server
            # tool cache populated during the parent's run phase (which
            # executes BEFORE this function, so the cache is warm).
            # Cold-cache servers are skipped silently — their tools
            # will be resolved lazily on first invocation by the
            # server-ID expansion path in ``resolve_authorized_tools``.
            try:
                from omweb.extensions.mcp_pool import mcp_session_pool

                effective_manifest = agent_registry.get_effective_agent(ag_id)
                declared_servers = list(
                    effective_manifest.get("effective_mcp_servers")
                    or effective_manifest.get("mcp_servers")
                    or []
                )
                for srv_id in declared_servers:
                    sid = str(srv_id).strip()
                    if not sid:
                        continue
                    cached_tools = mcp_session_pool.get_cached_tools(sid)
                    if not cached_tools:
                        continue
                    for tool_info in cached_tools:
                        remote_name = tool_info.get("name")
                        if not remote_name:
                            continue
                        expanded = f"mcp__{sid}__{remote_name}"
                        if expanded not in seen_specialist_tools:
                            seen_specialist_tools.add(expanded)
                            filtered.append(expanded)
            except Exception as expansion_exc:
                logger.debug(
                    "Specialist MCP server expansion for '%s' skipped: %s",
                    ag_id,
                    expansion_exc,
                )

            specialist_map[ag_id] = filtered
    except Exception as exc:
        logger.debug("Specialist tool allowlist discovery notice: %s", exc)

    return specialist_map


# ---------------------------------------------------------------------------
# MCP per-chat authentication helpers
# ---------------------------------------------------------------------------
#
# The engine no longer enforces MCP auth eagerly. It resolves whatever
# per-chat credentials the user has ALREADY recorded so that tool
# discovery works with the correct auth context, and defers enforcement
# to the MCP tool executor in resolve_tool_runtime.py — see
# _mcp_tool_executor. That design makes "hi" work without opening the
# auth modal, while still prompting correctly the moment the agent
# actually tries to use an MCP tool.
#
# The helper below is pure and side-effect free. All SSE emission,
# job-failure signalling, and run cancellation live in:
#   * omweb.extensions.mcp_auth_state   (state registry)
#   * omweb.adapters.core_adapter_parts.resolve_tool_runtime (lazy gate)
# ---------------------------------------------------------------------------


def _resolve_mcp_env_optional(
    *,
    chat_id: str,
    server_id: str,
) -> Optional[Dict[str, str]]:
    """
    Return the per-chat env override for an MCP server, or None.

    Unlike `_ensure_mcp_auth`, this function has NO side effects: it
    does not emit SSE events, does not fail the job, and does not
    raise. It simply reports what credentials (if any) the user has
    already recorded for this chat.

    Used only for tool *discovery* so the agent sees the correct
    tool list. Auth enforcement happens lazily inside the MCP tool
    executor at invocation time.

    Returns:
      * The env dict (with OAuth meta-keys when applicable) if the
        user recorded "custom" or "ephemeral" credentials.
      * None if there is no decision, or the decision was "use_global"
        (the caller should then fall back to settings_manager).
    """
    from omweb.extensions.chat_mcp_auth import (
        MODE_CUSTOM,
        MODE_EPHEMERAL,
        get_chat_mcp_auth_store,
    )

    store = get_chat_mcp_auth_store()
    mode = store.get_mode(chat_id, server_id)

    if mode == MODE_CUSTOM:
        env = store.get_env(chat_id, server_id) or {}
        oauth = store.get_oauth_tokens(chat_id, server_id)
        if isinstance(oauth, dict) and oauth.get("access_token"):
            env = dict(env)
            env["_oauth_access_token"] = str(oauth["access_token"])
            env["_oauth_token_type"] = str(
                oauth.get("token_type") or "Bearer"
            )
        return env or None

    if mode == MODE_EPHEMERAL:
        env = store.get_ephemeral(chat_id, server_id) or {}
        oauth = store.get_ephemeral_oauth(chat_id, server_id)
        if isinstance(oauth, dict) and oauth.get("access_token"):
            env = dict(env)
            env["_oauth_access_token"] = str(oauth["access_token"])
            env["_oauth_token_type"] = str(
                oauth.get("token_type") or "Bearer"
            )
        return env or None

    return None


class PeldrunEngine(ExecutionEngine):
    """Isolated Web execution adapter powered by local PELDRUN Core."""

    engine_id: str = "peldrun"

    async def health(self) -> Dict[str, Any]:
        """Perform health and version diagnostics for the embedded PELDRUN runtime."""
        try:
            import peldrun

            return {
                "engine_id": self.engine_id,
                "available": True,
                "embedded": getattr(peldrun, "__is_embedded__", False),
                "version": getattr(peldrun, "__version__", "unknown"),
                "upstream_commit": getattr(peldrun, "__upstream_commit__", "unknown"),
                "package_path": str(Path(peldrun.__file__).resolve().parent),
            }
        except ImportError as err:
            return {
                "engine_id": self.engine_id,
                "available": False,
                "embedded": False,
                "error": f"Failed to import embedded peldrun package: {err}",
            }

    async def run(self, context: EngineRunContext) -> None:
        """Execute autonomous agent workflow through canonical Core boundaries."""
        from omweb.adapters.core_adapter import resolve_tool_runtime
        from omweb.agent_bridge_parts.state import job_scoped_artifacts
        from omweb.agent_bridge_parts.text_utils import sanitize_final_result_text
        from omweb.chat_storage_engine import chat_storage_engine
        from omweb.extensions.mcp_pool import mcp_session_pool
        from omweb.extensions.registry import extension_registry
        from omweb.job_manager import job_manager
        from omweb.project_manager import project_manager
        from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event
        from omweb.tools.registry import tool_registry as web_tool_registry

        job_id = context.job_id
        run_id_str = str(uuid.uuid4())
        prompt = context.prompt
        agent_id = context.agent_id
        active_llm = context.active_llm
        model_name = context.model_name
        provider_name = context.provider_name
        project_dir = context.project_dir
        chat_id = context.chat_id or f"chat_{job_id}"

        # Use get_effective_agent so user-saved settings from the Store
        # settings modal (max_steps) are merged into the manifest before
        # the runtime sees it. Only keys declared in
        # settings.schema.properties are eligible for override; identity
        # fields (system_prompt, tools, name, status) are always preserved.
        # Falls back to the raw context manifest if the registry is
        # unavailable so a broken registry never blocks execution.
        manifest = context.manifest
        try:
            from omweb.agents.registry import agent_registry as _agent_registry
            manifest = _agent_registry.get_effective_agent(agent_id)
        except Exception as _eff_err:
            logger.debug(
                "Effective agent manifest lookup failed for %r: %s. "
                "Falling back to raw context manifest.",
                agent_id,
                _eff_err,
            )

        project_id = str(manifest.get("project_id") or "default_project")

        print(f"\n[ENGINE PELDRUN] >>> Starting Task Execution for Job: {job_id} (Chat: {chat_id}) <<<")

        job_scoped_artifacts.setdefault(job_id, [])
        if chat_id:
            job_scoped_artifacts.setdefault(chat_id, [])

        ws_path = project_dir.resolve()
        ws_path_str = str(ws_path)

        def _scan_workspace_files() -> Dict[str, Path]:
            scanned: Dict[str, Path] = {}
            if not project_dir.exists():
                return scanned
            for file_path in project_dir.rglob("*"):
                if file_path.is_file():
                    rel_parts = file_path.relative_to(project_dir).parts
                    if any(part.startswith(".") for part in rel_parts):
                        continue
                    canonical_rel = str(file_path.relative_to(project_dir)).replace("\\", "/")
                    scanned[canonical_rel] = file_path
            return scanned

        files_baseline: Dict[str, Path] = _scan_workspace_files()
        known_artifact_paths: Set[str] = set(files_baseline.keys())
        latest_meaningful_thought: str = ""

        max_steps = int(manifest.get("max_steps") or 30)
        base_system_prompt = manifest.get(
            "system_prompt",
            "You are peldrun, an all-around autonomous specialist agent.",
        )

        # Manifest system_prompt is authoritative. The engine only adds
        # operational guidance common to all agents.
        system_prompt = (
            f"{base_system_prompt}\n\n"
            f"[OPERATIONAL DIRECTIVE]\n"
            f"1. Autonomous Loop Contract: You run inside an automated agent loop. "
            f"If you need to ask the user questions or wait for input, invoke the "
            f"'ask_human' tool. Printing questions as plain text without a tool "
            f"call terminates execution.\n"
            f"2. Task Scope Sovereignty: Satisfy the user's objective faithfully. "
            f"Do not invent a need to create files or write code unless commanded.\n"
            f"3. Tool Selection: Use the tools best suited to each step. The list "
            f"of available tools is defined by your manifest."
        )

        project_rules = (
            "1. User Directive Sovereignty: Follow USER TASK instructions strictly.\n"
            "2. Workspace Operations: Current working directory is already the workspace "
            "root. Use relative paths.\n"
            "3. Task Conclusion: Ensure all parts of the user request are satisfied "
            "before calling 'terminate'."
        )

        # === Generative UI Directive Injection ===
        genui_cfg = active_llm.get("generative_ui") if active_llm else None
        if genui_cfg and genui_cfg.get("enabled"):
            genui_directive = build_generative_ui_prompt(genui_cfg)
            if genui_directive:
                system_prompt = system_prompt.rstrip() + "\n\n" + genui_directive

        # 1. Resolve & Refresh Context Sidecar from disk
        session_dir = project_manager.chats_dir / chat_id
        sidecar = chat_storage_engine.get_sidecar_object(chat_id)
        if not sidecar:
            chat_data = project_manager.get_chat(chat_id) or project_manager.get_chat(job_id) or {}
            sidecar = build_context_sidecar(
                session_id=chat_id,
                session_data=chat_data,
                model_id=model_name,
                storage_root=project_manager.storage_dir,
            )

        # 2. Assemble Sanitized Prompt Context via ContextManager
        context_mgr = ContextManager(default_system_prompt=system_prompt)
        # `include_execution=False` intentionally excludes the
        # previous turn's execution telemetry from the initial
        # context. `build_context_sidecar` sets
        # `sidecar.current_execution` to `None` once the latest turn
        # is no longer "running" (see
        # `sidecar.extract_conversation_and_execution`), so the flag
        # is a no-op for the common case. When the process was
        # killed mid-turn and `current_execution` still holds stale
        # data, hiding it prevents the agent from reasoning about
        # tool calls that the restart already invalidated. Do not
        # flip this to True without also gating on
        # `sidecar.current_execution.status == "running"` — otherwise
        # stale execution state would leak into every new turn.
        context_messages, context_usage = context_mgr.build_messages(
            sidecar=sidecar,
            current_user_message=prompt,
            system_prompt=system_prompt,
            project_rules=project_rules,
            include_execution=False,
        )

        effective_context_tokens = context_usage.get("total_tokens", context_usage.get("total", 0))

        logger.info(
            "[CONTEXT HARDENING] Assembled %d context messages for job %s (Tokens: %s)",
            len(context_messages),
            job_id,
            effective_context_tokens,
        )

        # 3. Resolve custom Web tools to adapt into Core tools
        #
        # The effective manifest may carry ``disabled_tools`` and
        # ``extra_tools`` when the agent declares
        # ``settings.tools_editable = true``. Apply them here so both the
        # AgentSpec (tools exposed to the LLM) and the adapter list
        # (tools invokable at runtime) reflect the user's selection.
        #
        # Precedence for a tool ID:
        #   1. If it is in ``disabled_tools`` → excluded.
        #   2. Otherwise kept from the manifest baseline.
        #   3. Any ``extra_tools`` entry is appended if not already
        #      present. Deduplication prevents double-resolution when the
        #      user re-adds a tool already in the manifest.
        manifest_tools = list(manifest.get("tools", []))
        disabled_tools = set(manifest.get("disabled_tools") or [])
        extra_tools = list(manifest.get("extra_tools") or [])

        requested_tools: List[str] = [
            t for t in manifest_tools if t not in disabled_tools
        ]
        for t in extra_tools:
            if t not in requested_tools:
                requested_tools.append(t)

        # 'terminate' is a Core built-in; it must be present in
        # AgentSpec.tools so Core exposes it to the LLM.
        if "terminate" not in requested_tools:
            requested_tools.append("terminate")

        available_web_tools = {
            t["id"]: t
            for t in web_tool_registry.list_tools()
            if t.get("is_enabled", True) and t.get("enabled", True) is not False
        }

        adapted_core_tools: List[Any] = []
        resolved_tool_ids: List[str] = []

        try:
            resolve_sig = inspect.signature(resolve_tool_runtime)
            resolve_params = resolve_sig.parameters
            accepts_context_kw = (
                "run_id" in resolve_params
                or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in resolve_params.values())
            )
        except (TypeError, ValueError):
            accepts_context_kw = False

        for req_tool in list(requested_tools):
            # 'terminate' and 'file_saver' are Core built-ins; 'mcp' is a
            # discovery marker (not an invokable tool). Do not inject custom
            # executors for them, but keep terminate/file_saver in AgentSpec.tools.
            if req_tool in ("terminate", "file_saver", "mcp"):
                continue

            # MCP tool identifiers are resolved dynamically by
            # `resolve_tool_runtime` — they are NOT registered in the
            # Web tool registry. Bypass the `available_web_tools`
            # filter for them so an MCP tool explicitly placed in
            # `extra_tools` actually gets materialised. Without this
            # branch, user-added MCP tool ids would vanish silently
            # from AgentSpec.tools — the LLM would be told the tool
            # does not exist even though the user just added it.
            is_mcp_tool = req_tool.startswith(("mcp__", "mcp::"))
            if not is_mcp_tool and req_tool not in available_web_tools:
                continue

            t_meta = (
                available_web_tools[req_tool]
                if not is_mcp_tool
                else {
                    "id": req_tool,
                    "name": req_tool,
                    "description": f"MCP tool '{req_tool}' resolved at runtime.",
                    "parameters": {"type": "object", "properties": {}},
                }
            )
            try:
                if accepts_context_kw:
                    adapter = resolve_tool_runtime(
                        tool_id=req_tool,
                        tool_meta=t_meta,
                        workspace_root=ws_path,
                        registry=web_tool_registry,
                        run_id=run_id_str,
                        job_id=job_id,
                        session_id=chat_id,
                        chat_id=chat_id,
                        project_id=project_id,
                    )
                else:
                    adapter = resolve_tool_runtime(
                        tool_id=req_tool,
                        tool_meta=t_meta,
                        workspace_root=ws_path,
                        registry=web_tool_registry,
                    )
                adapted_core_tools.append(adapter)
                resolved_tool_ids.append(req_tool)
            except Exception as tool_resolve_err:
                logger.warning(
                    "Failed to resolve tool %r for job %s: %s",
                    req_tool,
                    job_id,
                    tool_resolve_err,
                )

        # ── MCP Protocol Server Tool Discovery & Registration ─────────
        # The manifest may carry per-agent MCP servers either explicitly
        # (effective_mcp_servers / mcp_servers) or via the generic "mcp"
        # tool marker. We probe each active server for its live tool list
        # and register each remote tool under the canonical name:
        #     mcp__{server_id}__{tool_name}
        # Double underscore is used as a separator because most LLM
        # providers reject "::" in function names.
        effective_mcp_servers = list(
            manifest.get("effective_mcp_servers")
            or manifest.get("mcp_servers")
            or []
        )

        target_mcp_servers: List[Dict[str, Any]] = []
        if effective_mcp_servers:
            for s_id in effective_mcp_servers:
                try:
                    srv = extension_registry.get_effective_server(s_id)
                except Exception as srv_lookup_err:
                    logger.debug(
                        "MCP effective-server lookup failed for %r: %s",
                        s_id,
                        srv_lookup_err,
                    )
                    srv = None
                if srv and srv.get("status") == "active":
                    target_mcp_servers.append(srv)
        elif "mcp" in requested_tools:
            # Baseline compatibility: if the agent declares the generic
            # 'mcp' marker, expose every active MCP server.
            try:
                target_mcp_servers = list(
                    extension_registry.get_active_mcp_servers() or []
                )
            except Exception as mcp_list_err:
                logger.debug(
                    "MCP active-server enumeration failed: %s", mcp_list_err
                )
                target_mcp_servers = []

        # ── Lazy MCP credential resolution (NO pre-flight gate) ────
        #
        # We deliberately do NOT fail the run up-front when an MCP
        # server requires credentials that have not been recorded for
        # this chat. Doing so broke the simple case: sending "hi"
        # through an agent that happens to have an authenticated MCP
        # server bound used to fail immediately without the model
        # ever being called.
        #
        # Instead, credentials are resolved here on a best-effort
        # basis for tool DISCOVERY (so the agent knows which MCP
        # tools it could use), and the actual auth check is deferred
        # to the moment the agent tries to invoke an MCP tool — see
        # ``_mcp_tool_executor`` in resolve_tool_runtime.py.
        #
        # A server that requires auth and has no decision simply
        # contributes no MCP tools to this run. If the user's
        # request genuinely needs them, the agent will try to call
        # one, the executor will detect the missing auth, and the
        # modal will open exactly when it's needed.
        env_overrides: Dict[str, Optional[Dict[str, str]]] = {}
        for srv in target_mcp_servers:
            pre_sid = srv.get("id")
            if not pre_sid:
                continue
            env_overrides[pre_sid] = _resolve_mcp_env_optional(
                chat_id=chat_id,
                server_id=pre_sid,
            )

        # ── MCP subsystem kill-switch ──────────────────────────────
        # When the operator has disabled the entire MCP subsystem in
        # Setup → MCP, skip discovery altogether. `target_mcp_servers`
        # remains non-empty (the registry still knows about the
        # servers), but we treat it as empty for this run so no
        # subprocess is spawned and no MCP tool is advertised to the
        # LLM. Existing chat sessions are unaffected because they
        # already have their credentials resolved.
        try:
            from omweb.extensions.mcp_subsystem_settings import (
                is_enabled as _mcp_subsystem_enabled,
            )

            if not _mcp_subsystem_enabled():
                logger.info(
                    "[MCP DISABLED] Subsystem disabled in settings; "
                    "skipping discovery for job %s.",
                    job_id,
                )
                target_mcp_servers = []
        except Exception as kill_switch_err:  # noqa: BLE001
            logger.debug(
                "Could not read MCP kill-switch state for job %s: %s",
                job_id,
                kill_switch_err,
            )

        # ── Inject MCP access hint into system prompt ─────────────
        # Some models (e.g. GigaChat) hallucinate and refuse to use
        # MCP tools if they see a past "Authentication Required" error
        # in the conversation history. This block explicitly informs
        # the model that it has active, working access to these servers.
        #
        # Additionally, if a server declares a ``usage_hint`` field in
        # its manifest, that hint is appended verbatim — it typically
        # describes a multi-step workflow that the raw tool descriptions
        # do not convey (e.g. WordPress meta-tools: discover → get-info
        # → execute). Without this hint, LLMs either guess wrong tool
        # names or skip the discovery step entirely.
        _authenticated_mcp_servers = []
        _usage_hints: list[str] = []
        if target_mcp_servers:
            from omweb.extensions.chat_mcp_auth import get_chat_mcp_auth_store
            from omweb.extensions.mcp_auth_state import manifest_needs_auth

            _auth_store = get_chat_mcp_auth_store()
            for srv in target_mcp_servers:
                s_id = srv.get("id")
                if not s_id:
                    continue
                mode = _auth_store.get_mode(chat_id, s_id)
                # It is "authenticated" if the user made a choice OR
                # if the server doesn't actually need auth.
                if mode in ("custom", "use_global", "ephemeral") or not manifest_needs_auth(srv):
                    _authenticated_mcp_servers.append(srv.get("name") or s_id)
                    # Collect the per-server usage hint (if any).
                    hint = srv.get("usage_hint")
                    if isinstance(hint, str) and hint.strip():
                        _usage_hints.append(
                            f"[{srv.get('name') or s_id}]\n{hint.strip()}"
                        )

        if _authenticated_mcp_servers:
            _access_block = (
                "\n\n[MCP SERVER ACCESS]\n"
                "You have active, authenticated access to the following MCP servers: "
                + ", ".join(_authenticated_mcp_servers)
                + ".\n"
                "Use their tools freely to accomplish the user's task. "
                "Do NOT refuse to use them or claim you lack access."
            )
            if _usage_hints:
                _access_block += (
                    "\n\n[MCP USAGE HINTS]\n"
                    "Follow these workflows when calling tools from the "
                    "servers below. Do NOT skip the discovery step.\n\n"
                    + "\n\n".join(_usage_hints)
                )
            system_prompt = system_prompt.rstrip() + _access_block

        # ── Inject MCP access hint into system prompt ─────────────
        # Some models (e.g. GigaChat) hallucinate and refuse to use
        # MCP tools if they see a past "Authentication Required" error
        # in the conversation history. This block explicitly informs
        # the model that it has active, working access to these servers.
        _authenticated_mcp_servers = []
        if target_mcp_servers:
            from omweb.extensions.chat_mcp_auth import get_chat_mcp_auth_store
            from omweb.extensions.mcp_auth_state import manifest_needs_auth

            _auth_store = get_chat_mcp_auth_store()
            for srv in target_mcp_servers:
                s_id = srv.get("id")
                if not s_id:
                    continue
                mode = _auth_store.get_mode(chat_id, s_id)
                # It is "authenticated" if the user made a choice OR
                # if the server doesn't actually need auth.
                if mode in ("custom", "use_global", "ephemeral") or not manifest_needs_auth(srv):
                    _authenticated_mcp_servers.append(srv.get("name") or s_id)

        if _authenticated_mcp_servers:
            system_prompt = system_prompt.rstrip() + (
                "\n\n[MCP SERVER ACCESS]\n"
                "You have active, authenticated access to the following MCP servers: "
                + ", ".join(_authenticated_mcp_servers)
                + ".\n"
                "Use their tools freely to accomplish the user's task. "
                "Do NOT refuse to use them or claim you lack access."
            )

        # Discover tools from every target server CONCURRENTLY. A
        # serial loop here contributed N × probe-timeout to the total
        # pre-task latency, so 3 wedged servers would delay the run
        # by 45-90 seconds before any LLM call happened. The parallel
        # gather caps the total at the slowest probe + a small
        # bounded overhead.
        #
        # We also switch from `probe_connection()` to `get_tools()`:
        #   (a) results are cached per server (TTL 300s) so repeated
        #       runs collapse from O(N) subprocess spawns to O(1)
        #       cached lookups per warm server;
        #   (b) the pooled persistent client is reused across runs
        #       instead of spawning a disposable subprocess per step.
        #
        # The per-chat env_override resolved in the pre-flight gate
        # above is forwarded to get_tools so both the MCP client's
        # credentials and the tool-cache key are scoped to the chat.
        async def _probe_one(
            srv: Dict[str, Any],
        ) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]]]:
            try:
                srv_id_probe = srv.get("id", "")
                tools = await mcp_session_pool.get_tools(
                    server_id=srv_id_probe,
                    manifest=srv,
                    workspace_root=ws_path_str,
                    env_override=env_overrides.get(srv_id_probe),
                )
                return srv, list(tools or [])
            except Exception as exc:
                logger.debug(
                    "MCP tool discovery failed for server %r: %s",
                    srv.get("id"),
                    exc,
                )
                return srv, None

        if target_mcp_servers:
            probe_results = await asyncio.gather(
                *[_probe_one(srv) for srv in target_mcp_servers],
                return_exceptions=False,
            )
        else:
            probe_results = []

        # Materialise discovered tools serially after the parallel probe
        # completes (adapter construction is synchronous and cheap).
        #
        # IMPORTANT: an adapter MUST be successfully constructed before
        # its id is added to `requested_tools`. Otherwise AgentSpec.tools
        # would advertise to the LLM a tool that cannot actually be
        # invoked, producing an opaque "tool not found" at call time.
        for srv, tools in probe_results:
            srv_id = srv.get("id")
            if not srv_id or not tools:
                continue

            for tool_info in tools:
                remote_name = tool_info.get("name")
                if not remote_name:
                    continue

                # ── Per-tool enable filter ────────────────────────
                # Tools the user has explicitly disabled in the
                # "Manage Tools" UI are NOT injected into AgentSpec.
                # This cuts tool-definition tokens substantially on
                # large servers (GitHub: 50+ tools) while keeping the
                # ones the user actually needs.
                #
                # `user_enabled` is always present because tools come
                # from the merged cache (see mcp_tool_cache.merge_tools).
                # The .get(..., True) fallback is defensive only for
                # legacy caches written before this feature shipped.
                if not bool(tool_info.get("user_enabled", True)):
                    logger.debug(
                        "Skipping disabled MCP tool '%s::%s' for job %s",
                        srv_id,
                        remote_name,
                        job_id,
                    )
                    continue

                mcp_tool_id = f"mcp__{srv_id}__{remote_name}"
                if mcp_tool_id in resolved_tool_ids:
                    continue
                try:
                    mcp_adapter = resolve_tool_runtime(
                        tool_id=mcp_tool_id,
                        tool_meta={
                            "id": mcp_tool_id,
                            "name": mcp_tool_id,
                            "description": (
                                f"[{srv.get('name', srv_id)}] "
                                f"{tool_info.get('description', '')}"
                            ).strip(),
                            "parameters": tool_info.get("parameters", {}),
                            "server_id": srv_id,
                            "remote_tool_name": remote_name,
                        },
                        workspace_root=ws_path,
                        registry=web_tool_registry,
                        # job_id and chat_id are required so the
                        # executor can perform lazy per-chat auth
                        # resolution at tool-call time. No env_override
                        # is passed: the executor always checks the
                        # store first, then falls back to global.
                        job_id=job_id,
                        chat_id=chat_id,
                    )
                except Exception as reg_exc:
                    logger.warning(
                        "MCP adapter construction failed for %r on job %s: %s",
                        mcp_tool_id,
                        job_id,
                        reg_exc,
                    )
                    continue

                adapted_core_tools.append(mcp_adapter)
                resolved_tool_ids.append(mcp_tool_id)
                if mcp_tool_id not in requested_tools:
                    requested_tools.append(mcp_tool_id)

        # The 'mcp' marker is a discovery trigger, not an invokable tool;
        # strip it now that concrete MCP tool IDs have been materialized
        # into ``requested_tools`` (which feeds AgentSpec.tools below).
        if "mcp" in requested_tools:
            requested_tools = [t for t in requested_tools if t != "mcp"]

        specialist_tools_map = _collect_specialist_tool_allowlists(set(available_web_tools.keys()))

        # 4. Build Canonical RunRequest
        workspace_ctx = WorkspaceContext(
            workspace_id=chat_id,
            root_path=ws_path_str,
            chat_id=chat_id,
            project_id=project_id,
            read_only=False,
        )

        agent_spec = AgentSpec(
            id=agent_id,
            name=manifest.get("name") or agent_id,
            system_prompt=system_prompt,
            tools=requested_tools,
            max_steps=max_steps,
            temperature=float(active_llm.get("temperature", 0.2)),
        )

        run_metadata: Dict[str, Any] = {
            "model_name": model_name,
            "chat_id": chat_id,
            "provider": provider_name,
            "session_dir": str(session_dir),
            "project_rules": project_rules,
            "sidecar": sidecar,
            "context_messages": context_messages,
            "context_usage": context_usage,
            "authorized_tools": list(requested_tools),
            "resolved_custom_tools": list(resolved_tool_ids),
            "specialist_tools": specialist_tools_map,
            # [FIX-MULTIMODAL-PROPAGATE] Forward the multimodal attachment
            # references received from dispatcher.py so that
            # executor.py's [FIX-MULTIMODAL] block can resolve them to
            # image_url content parts before the LLM call. Without this
            # field, attachments are silently dropped between
            # peldrun_engine and executor — the model receives only the
            # text prompt and reports that it cannot see any image.
            #
            # This field is consumed exclusively by
            # CoreRuntimeExecutor.execute via
            # ``request.metadata.get("attachments")``. It is NOT read by
            # any other layer, so adding it cannot cause side effects.
            "attachments": context.attachments or [],
        }

        run_request = RunRequest(
            run_id=run_id_str,
            job_id=job_id,
            prompt=prompt,
            agent_spec=agent_spec,
            workspace=workspace_ctx,
            llm_config=active_llm,
            metadata=run_metadata,
            context_sidecar=sidecar,
            project_rules=project_rules,
            step_timeout_seconds=float(active_llm.get("timeout") or 120.0),
        )

        # 5. Setup Async Event Processing Bridge
        event_queue: asyncio.Queue[Optional[PeldrunEvent]] = asyncio.Queue()
        event_seq: int = 0
        current_core_step: int = 1
        is_first_step_start: bool = True
        terminal_dispatched: bool = False

        def _extract_event_step(evt: PeldrunEvent) -> Optional[int]:
            if hasattr(evt, "step") and isinstance(evt.step, int) and evt.step > 0:
                return evt.step
            if hasattr(evt, "payload") and isinstance(evt.payload, dict):
                p_step = (
                    evt.payload.get("step")
                    or evt.payload.get("step_num")
                    or evt.payload.get("current_step")
                )
                if isinstance(p_step, int) and p_step > 0:
                    return p_step
                if isinstance(p_step, str) and p_step.isdigit() and int(p_step) > 0:
                    return int(p_step)
            return None

        async def _sync_and_dispatch_artifacts(step_num: int) -> List[str]:
            nonlocal event_seq
            current_files = _scan_workspace_files()
            discovered_paths: List[str] = []

            for rel_path, file_path in current_files.items():
                if rel_path not in known_artifact_paths:
                    known_artifact_paths.add(rel_path)
                    discovered_paths.append(rel_path)
                    file_name = file_path.name
                    file_size = file_path.stat().st_size

                    if rel_path not in job_scoped_artifacts[job_id]:
                        job_scoped_artifacts[job_id].append(rel_path)

                    if chat_id and rel_path not in job_scoped_artifacts[chat_id]:
                        job_scoped_artifacts[chat_id].append(rel_path)

                    event_seq += 1
                    print(f"[ENGINE PELDRUN] Detected Artifact Created on Disk: {rel_path} ({file_size} bytes)")

                    await dispatch_event(
                        job_id,
                        SSEEvent(
                            type=SSEEventType.ARTIFACT_CREATED,
                            step=step_num,
                            data={
                                "artifact": rel_path,
                                "path": rel_path,
                                "relative_path": rel_path,
                                "name": file_name,
                                "size_bytes": file_size,
                                "chat_id": chat_id,
                                "job_id": job_id,
                                "seq": event_seq,
                                "model": model_name,
                            },
                        ),
                    )

            return discovered_paths

        async def _process_single_core_event(event: PeldrunEvent) -> None:
            nonlocal latest_meaningful_thought, current_core_step, is_first_step_start, event_seq

            event_seq += 1
            explicit_step = _extract_event_step(event)
            if explicit_step is not None:
                current_core_step = explicit_step
                is_first_step_start = False
            elif event.type == EventType.STEP_START:
                if is_first_step_start:
                    current_core_step = 1
                    is_first_step_start = False
                else:
                    current_core_step += 1

            curr_step = current_core_step

            if event.type == EventType.STEP_START:
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.STEP_START,
                        step=curr_step,
                        data={
                            "step": curr_step,
                            "seq": event_seq,
                            "model": model_name,
                            "engine": self.engine_id,
                            "status": "running",
                        },
                    ),
                )

            elif event.type == EventType.THOUGHT or getattr(event.type, "name", "") == "STEP_PROGRESS":
                # [STREAMING-AGENT] Streaming deltas and full-thought events
                # share the same EventType.THOUGHT channel, discriminated by
                # ``is_delta`` in the payload. Deltas must NOT be stripped
                # (whitespace tokens like " " are semantically meaningful
                # for word boundaries), and must be accumulated into
                # ``latest_meaningful_thought`` rather than replacing it —
                # otherwise a fallback final message derived from this
                # variable would end up being the last single token.
                raw_thought = event.payload.get("thought", "")
                is_delta = bool(event.payload.get("is_delta"))
                thought_text = str(raw_thought)

                # For deltas: keep whitespace-only fragments.
                # For non-deltas: skip whitespace-only noise.
                should_emit = is_delta and thought_text != "" or (
                    not is_delta and thought_text.strip()
                )

                if should_emit:
                    if is_delta:
                        latest_meaningful_thought = (
                            (latest_meaningful_thought or "") + thought_text
                        )
                    else:
                        latest_meaningful_thought = thought_text

                    await dispatch_event(
                        job_id,
                        SSEEvent(
                            type=SSEEventType.THOUGHT,
                            step=curr_step,
                            data={
                                "thought": thought_text,
                                "content": thought_text,
                                "is_delta": is_delta,
                                "seq": event_seq,
                                "model": model_name,
                            },
                        ),
                    )

            elif (
                event.type == getattr(EventType, "TOOL_CALL", None)
                or event.type == getattr(EventType, "TOOL_CALLED", None)
            ):
                tool_name = str(event.payload.get("tool_name") or event.payload.get("tool") or "tool")
                arguments = event.payload.get("arguments", {})
                raw_args = (
                    json.dumps(arguments, ensure_ascii=False)
                    if isinstance(arguments, dict)
                    else str(arguments)
                )

                is_human_ask = tool_name in ("ask_human", "human_input")
                tool_call_payload: Dict[str, Any] = {
                    "name": tool_name,
                    "toolName": tool_name,
                    "arguments": raw_args,
                    "content": raw_args,
                    "seq": event_seq,
                    "model": model_name,
                    "requires_input": is_human_ask,
                }
                if is_human_ask and isinstance(arguments, dict):
                    tool_call_payload["prompt"] = (
                        arguments.get("prompt")
                        or arguments.get("query")
                        or arguments.get("question")
                        or ""
                    )
                    tool_call_payload["input_type"] = arguments.get("input_type", "text")
                    tool_call_payload["options"] = arguments.get("options", [])
                    req_id = arguments.get("request_id")
                    if (
                        not req_id
                        and hasattr(HumanInputRegistry, "_pending_requests")
                        and HumanInputRegistry._pending_requests
                    ):
                        req_id = list(HumanInputRegistry._pending_requests.keys())[-1]
                    if req_id:
                        tool_call_payload["request_id"] = req_id

                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.TOOL_CALL,
                        step=curr_step,
                        data=tool_call_payload,
                    ),
                )

            elif event.type == EventType.ASK_HUMAN:
                payload = event.payload if isinstance(event.payload, dict) else {}
                prompt_text = payload.get("prompt") or payload.get("question") or ""
                req_id = payload.get("request_id") or ""
                tool_call_payload = {
                    "name": "ask_human",
                    "toolName": "ask_human",
                    "prompt": prompt_text,
                    "question": prompt_text,
                    "input_type": payload.get("input_type", "text"),
                    "options": payload.get("options", []),
                    "request_id": req_id,
                    "requires_input": True,
                    "seq": event_seq,
                    "model": model_name,
                }
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.TOOL_CALL,
                        step=curr_step,
                        data=tool_call_payload,
                    ),
                )

            elif (
                event.type == EventType.OBSERVATION
                or event.type == getattr(EventType, "TOOL_COMPLETED", None)
            ):
                obs_out = event.payload.get("output", "") or event.payload.get("error", "")

                # Serialize structured tool outputs as JSON, not as
                # Python `repr`. `str({"approval_id": "x"})` produces
                # "{'approval_id': 'x'}" (single quotes, Python repr),
                # which `JSON.parse` on the frontend rejects. In
                # practice peldrun-core often stringifies tool outputs
                # BEFORE this bridge sees them, so a plain string can
                # still be a Python repr. Handle both shapes:
                #
                #   - dict / list          -> json.dumps
                #   - string beginning "{"/"[" -> try ast.literal_eval,
                #                               then json.dumps
                #   - everything else      -> str()
                if isinstance(obs_out, (dict, list)):
                    obs_str = json.dumps(
                        obs_out, ensure_ascii=False, default=str
                    )
                elif isinstance(obs_out, str) and obs_out.lstrip().startswith(
                    ("{", "[")
                ):
                    stripped = obs_out.strip()
                    try:
                        parsed = ast.literal_eval(stripped)
                        if isinstance(parsed, (dict, list)):
                            obs_str = json.dumps(
                                parsed, ensure_ascii=False, default=str
                            )
                        else:
                            obs_str = str(obs_out)
                    except (ValueError, SyntaxError):
                        obs_str = str(obs_out)
                else:
                    obs_str = str(obs_out)

                is_tool_error = bool(
                    event.payload.get("is_error") or event.payload.get("error")
                )

                await _sync_and_dispatch_artifacts(curr_step)

                # Try to recover the tool name from the event payload.
                # peldrun-core may or may not include it depending on
                # the version; this is best-effort. The frontend no
                # longer requires it (see `extractOperationFromSteps`),
                # but carrying it makes the UI able to group steps more
                # precisely.
                observation_tool = str(
                    event.payload.get("tool_name")
                    or event.payload.get("tool")
                    or event.payload.get("toolName")
                    or ""
                )

                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.OBSERVATION,
                        step=curr_step,
                        data={
                            "output": obs_str,
                            "content": obs_str,
                            "tool_name": observation_tool,
                            "is_error": is_tool_error,
                            "error": event.payload.get("error") if is_tool_error else None,
                            "seq": event_seq,
                            "model": model_name,
                        },
                    ),
                )

            elif event.type == EventType.ERROR:
                err_msg = str(event.payload.get("error", "Runtime Error"))
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=curr_step,
                        data={
                            "message": err_msg,
                            "content": err_msg,
                            "seq": event_seq,
                            "model": model_name,
                        },
                    ),
                )

        async def _ordered_event_consumer() -> None:
            while True:
                event = await event_queue.get()
                if event is None:
                    event_queue.task_done()
                    break
                try:
                    await _process_single_core_event(event)
                except Exception as ex:
                    logger.error(
                        f"[ENGINE PELDRUN ERROR] Consumer failed for job {job_id}: {ex}",
                        exc_info=True,
                    )
                finally:
                    event_queue.task_done()

        consumer_task = asyncio.create_task(_ordered_event_consumer())

        async def _on_core_event(evt: PeldrunEvent) -> None:
            await event_queue.put(evt)

        # 6. Delegate Execution to Core Authority
        try:
            run_result = await core_executor.execute(
                request=run_request,
                event_listener=_on_core_event,
                custom_tools=adapted_core_tools,
            )

            await _sync_and_dispatch_artifacts(run_result.total_steps or current_core_step)
            await event_queue.put(None)
            await consumer_task

            # ── Post-run auth check ───────────────────────────────
            # If a lazy MCP auth failure occurred during the run, the
            # MCP tool executor has already:
            #   1. Recorded the failure in mcp_auth_state.
            #   2. Emitted the mcp_auth_required SSE event (modal opens).
            #   3. Called job_manager.fail_job with the correct message.
            #   4. Signalled core_executor.cancel (best-effort).
            #
            # Our only responsibility here is to NOT overwrite that
            # failure with a successful completion. We simply return.
            from omweb.extensions.mcp_auth_state import pop_auth_required

            _auth_flag = pop_auth_required(job_id)
            if _auth_flag:
                logger.info(
                    "[MCP AUTH] Job %s terminated due to lazy auth "
                    "requirement for server '%s' (chat=%s)",
                    job_id,
                    _auth_flag.get("server_id"),
                    _auth_flag.get("chat_id"),
                )
                return

        except asyncio.CancelledError:
            core_executor.cancel(job_id)
            try:
                await event_queue.put(None)
                await asyncio.wait_for(consumer_task, timeout=1.0)
            except Exception:
                consumer_task.cancel()
            print(f"[ENGINE PELDRUN] Task cancelled by operator for job: {job_id}")
            raise

        except Exception as exc:
            try:
                await event_queue.put(None)
                await asyncio.wait_for(consumer_task, timeout=1.0)
            except Exception:
                consumer_task.cancel()

            err_msg = f"Task execution interrupted: {exc}"
            print(f"[ENGINE PELDRUN ERROR] {err_msg}")
            job_manager.fail_job(job_id, err_msg)
            if not terminal_dispatched:
                terminal_dispatched = True
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=current_core_step,
                        data={
                            "message": err_msg,
                            "model": model_name,
                            "seq": event_seq + 1,
                        },
                    ),
                )
            return

        # 7. Extract Telemetry & Usage Metrics
        diag_data = run_result.metadata.get("diagnostics", {})
        inp_tok = int(diag_data.get("total_input_tokens") or 0)
        out_tok = int(diag_data.get("total_output_tokens") or 0)
        tot_tok = int(diag_data.get("total_tokens") or (inp_tok + out_tok))
        cost_nano = int(diag_data.get("total_cost_nano_usd") or 0)
        cost_usd = round(cost_nano / NANO_USD_PER_USD, 6)

        turn_id = f"turn_{int(time.time())}"
        turn_usage_dict = {
            "turn_id": turn_id,
            "prompt_tokens": inp_tok,
            "completion_tokens": out_tok,
            "total_tokens": tot_tok,
            "cached_input_tokens": diag_data.get("total_cached_tokens"),
            "reasoning_output_tokens": diag_data.get("total_reasoning_tokens"),
            "cost_usd": cost_usd,
            "cost_nano_usd": cost_nano,
            "latency_ms": diag_data.get("total_duration_ms"),
            "tokens_per_second": diag_data.get("overall_tokens_per_second"),
            "model": model_name,
            "provider": provider_name,
        }

        # [FIX-5.4] Forward the resolved file references (if any) into the
        # persisted turn record. `context.context_refs` is populated by the
        # dispatcher after mention resolution, and is None when the user did
        # not reference any workspace file for this turn.
        session_usage_summary = project_manager.record_turn_usage(
            chat_id=chat_id,
            turn_id=turn_id,
            turn_usage=turn_usage_dict,
            project_id=workspace_ctx.project_id or "default_project",
            context_refs=context.context_refs,
        )

        all_created = sorted([p for p in known_artifact_paths if p not in files_baseline])
        canonical_deliverables = all_created
        step_count = run_result.total_steps if run_result.total_steps > 0 else current_core_step

        if run_result.status == RunStatus.FAILED:
            err_msg = run_result.error or "Execution failed before reaching completion."
            print(f"[ENGINE PELDRUN] Job {job_id} terminated as FAILED in step {step_count}: {err_msg}")
            job_manager.fail_job(job_id, err_msg)
            if not terminal_dispatched:
                terminal_dispatched = True
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=step_count,
                        data={
                            "message": err_msg,
                            "model": model_name,
                            "engine": self.engine_id,
                            "produced_files": canonical_deliverables,
                            "usage": turn_usage_dict,
                            "usage_summary": session_usage_summary,
                            "seq": event_seq + 1,
                        },
                    ),
                )
            return

        if run_result.status == RunStatus.CANCELLED:
            cancel_msg = run_result.error or "Execution was cancelled by operator."
            print(f"[ENGINE PELDRUN] Job {job_id} terminated as CANCELLED in step {step_count}")
            job_manager.fail_job(job_id, cancel_msg)
            if not terminal_dispatched:
                terminal_dispatched = True
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=step_count,
                        data={
                            "message": cancel_msg,
                            "model": model_name,
                            "engine": self.engine_id,
                            "status": "cancelled",
                            "seq": event_seq + 1,
                        },
                    ),
                )
            return

        # Compose the final message shown to the user.
        #
        # The previous implementation replaced the agent's own output
        # with a generic "Deliverables Created Successfully" block
        # whenever any file appeared in the workspace. For source
        # control this silently dropped the operation IDs, approval
        # IDs, and PR URLs the agent had reported — exactly the data
        # the user needs to complete the approval workflow. The fix
        # preserves the agent's output as the primary message and
        # only falls back to the deliverables block when the agent
        # produced no meaningful output of its own.
        agent_output = sanitize_final_result_text(
            run_result.output, latest_meaningful_thought
        )

        # Ignore generic success strings emitted by the terminate tool so
        # the UI does not display redundant "Task execution finished..." text.
        _GENERIC_STRINGS = {
            "task execution finished with status: success",
            "task execution finished",
            "task completed successfully",
            "task completed.",
            "task execution finished with status: success.",
        }
        _normalized = (agent_output or "").strip().lower()
        agent_output_is_meaningful = bool(
            agent_output
            and agent_output.strip()
            and _normalized not in _GENERIC_STRINGS
        )

        if agent_output_is_meaningful:
            # The agent produced a meaningful message. Keep it as the
            # final result. The deliverables list is still visible to
            # the UI through the ARTIFACT_CREATED SSE events and the
            # produced_files panel, so appending it here would only
            # add noise (especially for cloned repositories).
            result_text = agent_output
        elif canonical_deliverables:
            # The agent produced no meaningful message; fall back to
            # the deliverables block so the user still knows what was
            # created. This matches the historical behavior for
            # agents that only produce files (e.g. code generators)
            # and terminate without a summary.
            file_bullets = "\n".join(
                [f"- `{f}`" for f in canonical_deliverables]
            )
            result_text = (
                f"### Deliverables Created Successfully\n\n"
                f"The requested project files have been built and "
                f"saved in your workspace:\n\n"
                f"{file_bullets}\n\n"
                f"You can preview and interact with the application "
                f"live in the **Preview** panel."
            )
        else:
            # Neither the agent nor the file scanner produced
            # anything. Keep the historical behavior: propagate the
            # (empty or thought-derived) agent output unchanged.
            result_text = agent_output

        print(
            f"[ENGINE PELDRUN] Job {job_id} completed successfully in step {step_count}. "
            f"Tokens: {tot_tok} | Cost: ${cost_usd}"
        )

        job_manager.complete_job(job_id, result_text)

        # 8. Atomically Synchronize Context Sidecar after task conclusion
        try:
            chat_storage_engine.sync_sidecar(chat_id, model_id=model_name)
        except Exception as sidecar_err:
            logger.debug(f"[ENGINE PELDRUN] Sidecar synchronization notice: {sidecar_err}")

        if not terminal_dispatched:
            terminal_dispatched = True
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.FINAL,
                    step=step_count,
                    data={
                        "result": result_text,
                        "model": model_name,
                        "engine": self.engine_id,
                        "produced_files": canonical_deliverables,
                        "usage": turn_usage_dict,
                        "usage_summary": session_usage_summary,
                        "cost_usd": cost_usd,
                        "seq": event_seq + 1,
                    },
                ),
            )
