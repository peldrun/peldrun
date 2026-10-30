# backend/omweb/routers/mcp.py
"""
Model Context Protocol (MCP) Router.

Provides complete lifecycle management for external MCP servers:
  * Registry listing and status reporting.
  * Live dynamic tool discovery with non-blocking timeouts.
  * Real JSON-RPC 2.0 connectivity handshakes.
  * Dedicated server configuration and environment variables management.
  * Subprocess session pool coordination and cleanup.
  * Standalone MCP zip bundle upload, extraction, and atomic purging.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from omweb.extensions.bundle_manager import McpBundleError, mcp_bundle_manager
from omweb.extensions.mcp_pool import mcp_session_pool
from omweb.extensions.registry import extension_registry
from omweb.extensions.mcp_subsystem_settings import (
    load_settings as _load_mcp_settings,
    save_settings as _save_mcp_settings,
    DEFAULT_SETTINGS as _MCP_DEFAULT_SETTINGS,
)
from omweb.extensions.mcp_tool_cache import (
    list_cached_servers as _list_cached_servers,
    load_cache as _load_tool_cache,
    set_tool_enabled as _set_tool_enabled,
    set_all_tools_enabled as _set_all_tools_enabled,
    invalidate as _invalidate_tool_cache,
)

logger = logging.getLogger("omweb.routers.mcp")

router = APIRouter()


class MCPTestRequest(BaseModel):
    server_id: Optional[str] = None
    command: Optional[str] = None
    url: Optional[str] = None
    transport: str = "stdio"


class MCPSettingsPayload(BaseModel):
    """
    All fields optional so a partial PUT is a safe patch. The server
    will merge the provided keys into the existing settings and
    re-validate the whole document.
    """
    enabled: Optional[bool] = None
    tool_cache_ttl_hours: Optional[int] = None
    startup_timeout_seconds: Optional[int] = None
    call_timeout_seconds: Optional[int] = None
    max_tools_per_server: Optional[int] = None
    auto_cleanup_idle_minutes: Optional[int] = None
    lazy_start_on_demand: Optional[bool] = None
    send_all_tools_by_default: Optional[bool] = None


class ToolTogglePayload(BaseModel):
    """Body for PATCH /api/mcp/servers/{sid}/tools/{name} and bulk PATCH."""
    enabled: bool


class MCPServerSettingsPayload(BaseModel):
    settings: Optional[Dict[str, Any]] = None
    env: Optional[Dict[str, Any]] = None


async def _fetch_server_tools_safe(
    manifest: Dict[str, Any],
    timeout: float = 3.5,
) -> List[str]:
    """
    Discover tool names for an active server via the cached tool-list
    path.

    Historic behaviour used `probe_connection` here, which spawned a
    fresh stdio subprocess on every call to GET /api/mcp — one
    subprocess per active server per page load. `get_tools` instead
    reuses the pooled persistent client and, after the first call,
    serves the result from a 300s-TTL cache. The timeout guard is
    preserved so a wedged server cannot stall the Store page.
    """
    server_id = manifest.get("id", "")
    if not server_id:
        return []
    try:
        tools = await asyncio.wait_for(
            mcp_session_pool.get_tools(
                server_id=server_id,
                manifest=manifest,
            ),
            timeout=timeout,
        )
        return [
            str(t.get("name"))
            for t in (tools or [])
            if isinstance(t, dict) and t.get("name")
        ]
    except Exception as exc:
        logger.debug(
            "Tool list fetch skipped for server '%s': %s",
            server_id,
            exc,
        )
        return []


@router.get("")
@router.get("/")
async def get_mcp_status():
    """Retrieve all registered MCP servers and summarize active tools dynamically."""
    mcp_servers = extension_registry.list_mcp_servers()
    active_servers = [s for s in mcp_servers if s.get("status") == "active"]

    discovery_tasks = []
    for server in active_servers:
        effective_manifest = extension_registry.get_effective_server(server["id"]) or server
        discovery_tasks.append(_fetch_server_tools_safe(effective_manifest))

    discovered_lists = await asyncio.gather(*discovery_tasks, return_exceptions=True)

    tools_summary: List[str] = []
    for item in discovered_lists:
        if isinstance(item, list):
            tools_summary.extend(item)

    return {
        "status": "ok",
        "total_servers": len(mcp_servers),
        "active_servers": len(active_servers),
        "servers": mcp_servers,
        "active_tools": sorted(list(set(tools_summary))),
    }


@router.get("/servers")
async def list_mcp_servers():
    """List all registered MCP server configurations."""
    return {
        "status": "ok",
        "servers": extension_registry.list_mcp_servers(),
    }


@router.get("/servers/{server_id}")
async def get_mcp_server_detail(server_id: str):
    """Retrieve full configuration and effective state for a single MCP server."""
    server = extension_registry.get_effective_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' not found.")
    return {"status": "ok", "server": server}


@router.post("/servers/{server_id}/toggle")
async def toggle_mcp_server(server_id: str):
    """
    Toggle MCP server activation status between active and ready.

    Also invalidates the tool cache when the server is DEACTIVATED
    (active -> ready) so that a subsequent reactivation does not
    accidentally serve tools discovered under the previous session's
    credentials or config.
    """
    current = extension_registry.get_extension(server_id)
    was_active = bool(current and current.get("status") == "active")

    res = extension_registry.toggle_status(server_id)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])

    # Always tear down every workspace-bound session for this server.
    await mcp_session_pool.close_server_sessions(server_id)

    # Drop the persistent + in-memory tool cache when deactivating.
    # The next activation will re-discover.
    if was_active and res.get("status") != "active":
        try:
            _invalidate_tool_cache(server_id)
        except Exception as invalidate_err:  # noqa: BLE001
            logger.debug(
                "Tool cache invalidation on toggle-off failed for '%s': %s",
                server_id,
                invalidate_err,
            )

    return {"status": "ok", "server": res}


@router.get("/servers/{server_id}/settings")
async def get_mcp_server_settings(server_id: str):
    """Retrieve schemas and effective configuration values for an MCP server."""
    ext = extension_registry.get_extension(server_id)
    if not ext:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' not found.")

    settings_schema = ext.get("settings_schema")
    env_schema = ext.get("env_schema")

    current_settings = extension_registry.get_server_settings(server_id)
    current_env = extension_registry.get_server_env(server_id)

    # ── OAuth capability (mirrors routers/mcp_auth._is_oauth_capable) ──
    # OAuth is only meaningful for HTTP-transport servers that expose
    # a URL and declare auth_strategy="oauth". The frontend uses this
    # to conditionally render the "Connect with OAuth" section — a
    # stdio server cannot participate in an OAuth flow.
    _transport = str(ext.get("transport") or "").lower()
    _url = ext.get("url")
    _auth_strategy = str(ext.get("auth_strategy") or "").lower()
    _is_http = _transport in ("sse", "http", "streamable-http", "")
    _has_url = isinstance(_url, str) and bool(_url)
    _oauth_capable = _auth_strategy == "oauth" and _is_http and _has_url

    return {
        "status": "ok",
        "server_id": server_id,
        "settings_schema": settings_schema,
        "env_schema": env_schema,
        "settings": current_settings,
        "env": current_env,
        "auth_strategy": _auth_strategy or None,
        "transport": _transport or "stdio",
        "oauth_capable": _oauth_capable,
    }


@router.put("/servers/{server_id}/settings")
async def update_mcp_server_settings(server_id: str, payload: MCPServerSettingsPayload = Body(...)):
    """Validate and atomically persist configuration and environment variables."""
    ext = extension_registry.get_extension(server_id)
    if not ext:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' not found.")

    saved_settings = None
    saved_env = None

    try:
        if payload.settings is not None:
            saved_settings = extension_registry.save_server_settings(server_id, payload.settings)
        if payload.env is not None:
            saved_env = extension_registry.save_server_env(server_id, payload.env)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))

    # Settings and env changes can alter the effective transport:
    #   - `command` may embed paths derived from settings (e.g.
    #     @modelcontextprotocol/server-filesystem allowed_directories)
    #   - env vars are passed verbatim into the subprocess
    # A cached client bound to the old settings will silently keep
    # calling the server with stale arguments. Tear down every
    # workspace-bound session so the next invocation re-launches with
    # the freshly-persisted config.
    await mcp_session_pool.close_server_sessions(server_id)

    return {
        "status": "ok",
        "server_id": server_id,
        "settings": saved_settings or extension_registry.get_server_settings(server_id),
        "env": saved_env or extension_registry.get_server_env(server_id),
    }


@router.get("/servers/{server_id}/tools")
async def get_mcp_server_tools(server_id: str):
    """Perform tool discovery directly against an MCP server and return tool definitions."""
    ext = extension_registry.get_effective_server(server_id)
    if not ext:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' not found.")

    probe_result = await mcp_session_pool.probe_connection(ext)
    if probe_result.get("status") == "ok":
        return {
            "status": "ok",
            "server_id": server_id,
            "tools": probe_result.get("tools", []),
        }

    raise HTTPException(
        status_code=400,
        detail=probe_result.get("message", "Failed to retrieve tools from MCP server."),
    )


@router.post("/test")
async def test_mcp_connectivity(payload: MCPTestRequest = Body(...)):
    """Test connectivity and execute real JSON-RPC handshake against an MCP server."""
    if payload.server_id:
        manifest = extension_registry.get_effective_server(payload.server_id)
        if not manifest:
            return {
                "status": "error",
                "message": f"MCP server '{payload.server_id}' not found in registry.",
            }
    else:
        manifest = {
            "id": "ephemeral_test_probe",
            "command": payload.command,
            "url": payload.url,
            "transport": payload.transport,
        }

    result = await mcp_session_pool.probe_connection(manifest)
    return result


@router.delete("/servers/{server_id}")
async def delete_mcp_server(server_id: str):
    """
    Delete an MCP server registration. If the server is an installed bundle,
    completely purges its isolated bundle folder and runtime sessions.
    """
    ext = extension_registry.get_extension(server_id)
    if not ext:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' not found.")

    if ext.get("is_builtin"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in MCP server.")

    # If it is a bundle server, perform thorough directory and session purge
    if ext.get("is_bundle"):
        await mcp_bundle_manager.delete_bundle(server_id)
        return {
            "status": "ok",
            "message": f"MCP bundle server '{server_id}' and all folder files purged successfully.",
        }

    # Purge every workspace-bound session before removing the manifest
    # so no orphaned subprocess survives the deletion. The bundle
    # branch above delegates this to `mcp_bundle_manager.delete_bundle`,
    # which already calls close_server_sessions internally.
    await mcp_session_pool.close_server_sessions(server_id)
    success = extension_registry.delete_extension(server_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete custom server.")

    return {"status": "ok", "message": f"MCP server '{server_id}' deleted successfully."}


# ─────────────────────────────────────────────────────────────────
# Standalone MCP Bundles (.zip) Endpoints
# ─────────────────────────────────────────────────────────────────

@router.get("/bundles")
async def list_mcp_bundles():
    """List all installed MCP bundles on disk."""
    return {
        "status": "ok",
        "bundles": mcp_bundle_manager.list_installed_bundles(),
    }


@router.post("/bundles/upload")
async def upload_mcp_bundle(file: UploadFile = File(...)):
    """
    Upload and install a standalone MCP bundle (.zip archive).
    Extracts the bundle into an isolated storage directory named after the bundle ID.
    """
    filename = file.filename or "mcp_bundle.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="MCP bundles must be uploaded as .zip archives.",
        )

    content = await file.read()
    try:
        result = mcp_bundle_manager.install_bundle(content, source_filename=filename)
        return {"status": "ok", "bundle": result}
    except McpBundleError as err:
        raise HTTPException(status_code=400, detail=err.to_dict())
    except Exception as exc:
        logger.exception("Failed to install MCP bundle: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to install MCP bundle: {exc}")


@router.delete("/bundles/{bundle_id}")
async def delete_mcp_bundle_endpoint(bundle_id: str):
    """
    Purge an installed MCP bundle folder, configuration, and sessions.
    """
    try:
        deleted = await mcp_bundle_manager.delete_bundle(bundle_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"MCP bundle '{bundle_id}' not found.")
        return {"status": "ok", "bundle_id": bundle_id, "deleted": True}
    except Exception as exc:
        logger.exception("Failed to delete MCP bundle '%s': %s", bundle_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────
# MCP Subsystem Settings
# ─────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_mcp_settings():
    """
    Return the effective MCP subsystem settings.

    These are process-wide knobs (kill switch, timeouts, cache TTL,
    etc.) that affect ALL MCP servers and tools. Per-server settings
    live on each extension manifest; per-chat auth decisions live in
    mcp_auth.json. See omweb.extensions.mcp_subsystem_settings for
    the canonical list of fields and their defaults.
    """
    return {
        "status": "ok",
        "settings": _load_mcp_settings(),
        "defaults": dict(_MCP_DEFAULT_SETTINGS),
    }


@router.put("/settings")
async def update_mcp_settings(payload: MCPSettingsPayload = Body(...)):
    """
    Patch and persist MCP subsystem settings.

    Only the fields present in the request body are updated; omitted
    fields keep their current value. All values are validated and
    clamped to safe ranges by the settings module.

    When `enabled` transitions from True to False, all active MCP
    sessions are closed immediately so no subprocess survives the
    kill switch. When it transitions False -> True, no action is
    needed; the next call will lazily re-initialize.
    """
    current = _load_mcp_settings()

    # Merge only the explicitly provided fields.
    patch = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
    merged = {**current, **patch}

    was_enabled = bool(current.get("enabled", True))
    will_be_enabled = bool(merged.get("enabled", True))

    saved = _save_mcp_settings(merged)

    # Apply the kill-switch side effect synchronously.
    if was_enabled and not will_be_enabled:
        try:
            await mcp_session_pool.close_all()
            logger.info(
                "MCP subsystem disabled; closed %d active session(s).",
                0,  # close_all logs the exact count itself
            )
        except Exception as close_err:  # noqa: BLE001
            logger.warning(
                "Failed to close MCP sessions during kill-switch: %s",
                close_err,
            )

    return {
        "status": "ok",
        "settings": saved,
    }


# ─────────────────────────────────────────────────────────────────
# MCP Tool Cache & Per-Tool Toggles
# ─────────────────────────────────────────────────────────────────

@router.get("/servers/{server_id}/tools/cached")
async def get_cached_tools_endpoint(server_id: str):
    """
    Return the persisted tool cache for a server, including each
    tool's `user_enabled` flag, without spawning a subprocess.

    This is the primary endpoint used by the "Manage Tools" UI.
    It does NOT trigger discovery — if the cache is empty, use
    POST /tools/refresh to populate it.
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    cache = _load_tool_cache(server_id)
    if not cache:
        return {
            "status": "ok",
            "server_id": server_id,
            "cached": False,
            "fetched_at": None,
            "tools": [],
            "total": 0,
            "enabled": 0,
        }

    tools = cache.get("tools", [])
    return {
        "status": "ok",
        "server_id": server_id,
        "cached": True,
        "fetched_at": cache.get("fetched_at"),
        "server_config_hash": cache.get("server_config_hash"),
        "tools": tools,
        "total": len(tools),
        "enabled": sum(1 for t in tools if isinstance(t, dict) and t.get("user_enabled")),
    }


@router.patch("/servers/{server_id}/tools/{tool_name}")
async def toggle_tool_endpoint(
    server_id: str,
    tool_name: str,
    payload: ToolTogglePayload = Body(...),
):
    """
    Enable or disable a single MCP tool.

    The decision is persisted in the tool cache and takes effect on
    the NEXT run: disabled tools are not injected into the LLM's
    AgentSpec, reducing tool-definition tokens substantially for
    large servers (GitHub: 50+ tools).
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    ok = _set_tool_enabled(server_id, tool_name, payload.enabled)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Tool '{tool_name}' not found in cache for server "
                f"'{server_id}'. Run POST /tools/refresh first."
            ),
        )

    return {
        "status": "ok",
        "server_id": server_id,
        "tool_name": tool_name,
        "enabled": payload.enabled,
    }


@router.patch("/servers/{server_id}/tools")
async def bulk_toggle_tools_endpoint(
    server_id: str,
    payload: ToolTogglePayload = Body(...),
):
    """
    Enable or disable ALL tools of a server in one call.

    Used by the "Enable All" / "Disable All" buttons in the Manage
    Tools UI.
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    count = _set_all_tools_enabled(server_id, payload.enabled)
    if count == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No cached tools found for server '{server_id}'. "
                f"Run POST /tools/refresh first."
            ),
        )

    return {
        "status": "ok",
        "server_id": server_id,
        "updated": count,
        "enabled": payload.enabled,
    }


@router.post("/servers/{server_id}/tools/refresh")
async def refresh_tools_endpoint(server_id: str):
    """
    Force a live re-discovery of tools for a server.

    Bypasses both the persistent and in-memory caches. The result
    is merged with the existing cache: tools still present keep their
    `user_enabled` decisions, newly discovered tools use the current
    `send_all_tools_by_default` value, and tools that no longer exist
    on the server are dropped.
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    manifest = extension_registry.get_effective_server(server_id)
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' has no effective manifest.",
        )

    # ── Credential pre-check ──────────────────────────────────────
    # When an MCP server declares that it needs auth but the user
    # has NOT saved global credentials, attempting to spawn it
    # produces a 30-120s timeout because most MCP servers hang
    # waiting for their token on stdin. Fail fast with an
    # actionable 412 instead.
    #
    # OAuth servers (Notion, Linear, etc.) have auth_strategy="oauth"
    # and NO env_schema. Their credential is a global OAuth token
    # bundle managed via mcp_settings_manager.save_oauth (populated
    # by POST /api/mcp/servers/{id}/oauth/start).
    try:
        from omweb.extensions.mcp_auth_state import manifest_needs_auth
        from omweb.extensions.settings_manager import mcp_settings_manager

        auth_strategy = str(
            manifest.get("auth_strategy") or ""
        ).lower().strip()

        if auth_strategy == "oauth":
            bundle = mcp_settings_manager.get_oauth(server_id)
            if not (bundle and bundle.get("access_token")):
                raise HTTPException(
                    status_code=412,
                    detail=(
                        f"Server '{server_id}' requires OAuth authorization. "
                        f"Open Store → Extensions → "
                        f"{manifest.get('name', server_id)} → Settings and "
                        f"click 'Connect with OAuth' to authorize before "
                        f"refreshing tools."
                    ),
                )
        elif manifest_needs_auth(manifest):
            global_env = mcp_settings_manager.get_env(
                server_id, manifest.get("env_schema")
            )
            meaningful = {
                k: v for k, v in (global_env or {}).items()
                if v and str(v).strip() and "•" not in str(v)
            }
            if not meaningful:
                raise HTTPException(
                    status_code=412,
                    detail=(
                        f"Server '{server_id}' requires credentials but no "
                        f"global env is configured. Set them first via "
                        f"Store → Extensions → {manifest.get('name', server_id)} "
                        f"→ Settings. Without a token, most MCP servers hang "
                        f"silently waiting for input, which manifests as a "
                        f"timeout."
                    ),
                )
    except HTTPException:
        raise
    except Exception as cred_check_err:  # noqa: BLE001
        logger.debug(
            "Credential pre-check skipped for '%s': %s",
            server_id,
            cred_check_err,
        )

    try:
        # force_refresh=True skips both caches; the merged list is
        # written back to the persistent cache by mcp_pool.get_tools.
        tools = await mcp_session_pool.get_tools(
            server_id=server_id,
            manifest=manifest,
            force_refresh=True,
        )
    except TimeoutError as timeout_exc:
        logger.warning(
            "Tool refresh timed out for MCP server '%s': %s",
            server_id,
            timeout_exc,
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"Tool discovery for '{server_id}' timed out. Common causes: "
                f"(1) the server is a stdio process that hangs when its "
                f"required env/token is missing — verify credentials above; "
                f"(2) `npx -y` is downloading the package for the first time "
                f"— pre-warm it by running the command manually; "
                f"(3) increase 'Startup Timeout' in Setup → MCP."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Tool refresh failed for MCP server '%s': %s", server_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail=f"Tool discovery failed: {exc}",
        )

    # Re-read the cache to get the post-merge view (with user_enabled
    # decisions and timestamps).
    cache = _load_tool_cache(server_id) or {}
    final_tools = cache.get("tools", tools)

    return {
        "status": "ok",
        "server_id": server_id,
        "fetched_at": cache.get("fetched_at"),
        "total": len(final_tools),
        "enabled": sum(
            1 for t in final_tools
            if isinstance(t, dict) and t.get("user_enabled")
        ),
        "tools": final_tools,
    }


# ─────────────────────────────────────────────────────────────────
# MCP Resources & Prompts (MCP 2025-11-25 §5, §6)
# ─────────────────────────────────────────────────────────────────
#
# Three read-only endpoints that surface the non-tool primitives
# exposed by an MCP server. Historically only tools were discoverable
# in the Store UI, so a "documentation server" (3 resources, 0 tools)
# appeared as "0 tools — empty" — indistinguishable from a broken
# server.
#
# Cache strategy: identical to ``/tools/cached``. The endpoints
# serve the cache when it is fresh; otherwise they fall back to a
# live discovery that re-populates the cache. This keeps the
# `MCPDetailDrawer` responsive on every visit after the first.
# ─────────────────────────────────────────────────────────────────

@router.get("/servers/{server_id}/resources")
async def get_mcp_server_resources(server_id: str):
    """
    Return the resources exposed by a server.

    Cache-first: the response is served from the persistent cache
    when fresh, and discovered live otherwise. A live discovery
    updates the cache as a side effect, so a second call hits the
    cache.

    Empty list is a valid result: most MCP servers today expose
    tools only and do not declare any resources. The frontend
    treats an empty list as "no resources tab" and does not show
    an error.
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    manifest = extension_registry.get_effective_server(server_id)
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' has no effective manifest.",
        )

    # ── Fast path: cached resources ──────────────────────────────
    try:
        from omweb.extensions.mcp_tool_cache import (
            get_resources as _get_cached_resources,
        )

        cached = _get_cached_resources(server_id)
        if cached:
            return {
                "status": "ok",
                "server_id": server_id,
                "resources": cached,
                "total": len(cached),
                "source": "cache",
            }
    except Exception as cache_err:  # noqa: BLE001
        # A broken cache read must not block live discovery.
        logger.debug(
            "Cached resources read failed for '%s': %s",
            server_id,
            cache_err,
        )

    # ── Slow path: live discovery ────────────────────────────────
    try:
        resources = await mcp_session_pool.get_resources(
            server_id=server_id,
            manifest=manifest,
        )
        return {
            "status": "ok",
            "server_id": server_id,
            "resources": resources,
            "total": len(resources),
            "source": "live",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Failed to list resources for '%s': %s", server_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail=f"Resource discovery failed: {exc}",
        )


@router.get("/servers/{server_id}/prompts")
async def get_mcp_server_prompts(server_id: str):
    """
    Return the prompts exposed by a server.

    Mirrors ``/resources`` in every respect — cache-first, live
    fallback, empty list is a valid response.
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    manifest = extension_registry.get_effective_server(server_id)
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' has no effective manifest.",
        )

    try:
        from omweb.extensions.mcp_tool_cache import (
            get_prompts as _get_cached_prompts,
        )

        cached = _get_cached_prompts(server_id)
        if cached:
            return {
                "status": "ok",
                "server_id": server_id,
                "prompts": cached,
                "total": len(cached),
                "source": "cache",
            }
    except Exception as cache_err:  # noqa: BLE001
        logger.debug(
            "Cached prompts read failed for '%s': %s",
            server_id,
            cache_err,
        )

    try:
        prompts = await mcp_session_pool.get_prompts(
            server_id=server_id,
            manifest=manifest,
        )
        return {
            "status": "ok",
            "server_id": server_id,
            "prompts": prompts,
            "total": len(prompts),
            "source": "live",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Failed to list prompts for '%s': %s", server_id, exc
        )
        raise HTTPException(
            status_code=502,
            detail=f"Prompt discovery failed: {exc}",
        )


class ReadResourcePayload(BaseModel):
    """Body for POST /servers/{sid}/resources/read."""
    uri: str


@router.post("/servers/{server_id}/resources/read")
async def read_mcp_resource(
    server_id: str,
    payload: ReadResourcePayload = Body(...),
):
    """
    Read the contents of a single resource by URI.

    Returns ``{contents: [{uri, mimeType, text|blob}]}`` on
    success. The response shape mirrors MCP's ``resources/read``
    verbatim so clients can rely on the standard schema.

    A 404 is returned when the resource is unavailable (URI is
    valid but returns no contents). This is distinct from 502
    (transport / RPC failure) and lets the UI distinguish "this
    URI is gone" from "the server is unreachable".
    """
    if not extension_registry.get_extension(server_id):
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' not found.",
        )

    manifest = extension_registry.get_effective_server(server_id)
    if not manifest:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server '{server_id}' has no effective manifest.",
        )

    try:
        client = await mcp_session_pool.get_or_create_client(
            server_id=server_id,
            manifest=manifest,
        )
        result = await client.read_resource(payload.uri)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Resource '{payload.uri}' is not available on "
                    f"server '{server_id}'."
                ),
            )
        return {
            "status": "ok",
            "server_id": server_id,
            "uri": payload.uri,
            **result,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Failed to read resource '%s' from '%s': %s",
            payload.uri,
            server_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Resource read failed: {exc}",
        )


# ─────────────────────────────────────────────────────────────────
# MCP Subsystem Maintenance Endpoints
# ─────────────────────────────────────────────────────────────────

@router.post("/maintenance/warm-npm-cache")
async def maintenance_warm_npm_cache():
    """
    Re-download every npx package used by a registered MCP server.

    Safe to run at any time. Runs packages in parallel (bounded at
    two concurrent installs) so a five-server setup completes in
    ~30-60s instead of 5 × 30-60s.

    Use after: ``npm cache clean --force``, network outages, or when
    ``Test Connection`` times out because ``npx -y`` is re-downloading
    a package.
    """
    from omweb.extensions.mcp_maintenance import warm_npm_cache

    try:
        report = await warm_npm_cache()
        return {"status": "ok", **report}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Warm npm cache failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Warm npm cache failed: {exc}",
        )


@router.post("/maintenance/cleanup-sessions")
async def maintenance_cleanup_sessions():
    """
    Close every active MCP session and invalidate all tool caches.

    Use when: Test Connection or Tools Refresh hangs, sessions appear
    stuck, or after editing an MCP manifest that is not being picked up.
    """
    from omweb.extensions.mcp_maintenance import cleanup_orphans

    try:
        report = await cleanup_orphans()
        return {"status": "ok", **report}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Session cleanup failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Session cleanup failed: {exc}",
        )


@router.get("/maintenance/diagnose-restart")
async def maintenance_diagnose_restart():
    """
    Diagnose the restart command without actually restarting.

    Returns the reconstructed argv, whether the executable path is
    valid, and the raw sys.argv/psutil values for debugging. Useful
    when "Restart Backend" fails because of Windows path issues.
    """
    import sys as _sys
    from omweb.extensions.mcp_maintenance import (
        _get_restart_argv,
        _validate_restart_argv,
    )

    argv = _get_restart_argv()
    validation_error = _validate_restart_argv(argv)

    psutil_raw: Optional[list] = None
    try:
        import psutil
        psutil_raw = psutil.Process().cmdline()
    except Exception:
        pass

    return {
        "status": "ok" if validation_error is None else "error",
        "restart_argv": argv,
        "validation_error": validation_error,
        "sys_executable": _sys.executable,
        "sys_argv": list(_sys.argv),
        "psutil_raw_cmdline": psutil_raw,
    }


@router.post("/maintenance/restart")
async def maintenance_restart_backend():
    """
    Restart the backend process in-place.

    Uses ``os.execv`` so the same PID is preserved (PowerShell and any
    supervisor keep waiting on the process). The response is sent
    first, then a 1.5s delay elapses, then the process re-executes
    itself with the original command line.

    Returns **500 with a descriptive message** when the restart argv
    cannot be reconstructed safely. In that case the server is NOT
    stopped — the user must restart manually. This is strictly better
    than stopping without a replacement, which is what caused the
    "server went down and didn't come back" failure mode reported
    after a Windows path-with-spaces bug in ``psutil.cmdline()``.
    """
    from omweb.extensions.mcp_maintenance import schedule_restart

    try:
        argv = schedule_restart(delay_seconds=1.5)
        return {
            "status": "ok",
            "message": "Backend is restarting in 1.5s…",
            "command": " ".join(argv),
            "delay_seconds": 1.5,
        }
    except RuntimeError as exc:
        # Refused to restart because argv was invalid. Server stays up.
        logger.warning("Restart refused (invalid argv): %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Restart refused: {exc}\n\n"
                f"The server is still running. Please restart manually: "
                f"Ctrl+C in the terminal, then run '.\\start-backend' again."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Restart scheduling failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Restart failed: {exc}",
        )
