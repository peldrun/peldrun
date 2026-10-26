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

logger = logging.getLogger("omweb.routers.mcp")

router = APIRouter()


class MCPTestRequest(BaseModel):
    server_id: Optional[str] = None
    command: Optional[str] = None
    url: Optional[str] = None
    transport: str = "stdio"


class MCPServerSettingsPayload(BaseModel):
    settings: Optional[Dict[str, Any]] = None
    env: Optional[Dict[str, Any]] = None


async def _fetch_server_tools_safe(manifest: Dict[str, Any], timeout: float = 3.5) -> List[str]:
    """Discover tool names for an active server with a defensive timeout guard."""
    try:
        probe = await asyncio.wait_for(
            mcp_session_pool.probe_connection(manifest),
            timeout=timeout,
        )
        if probe.get("status") == "ok":
            return [t.get("name") for t in probe.get("tools", []) if t.get("name")]
    except Exception as exc:
        logger.debug("Non-blocking discovery skipped for server '%s': %s", manifest.get("id"), exc)
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
    """Toggle MCP server activation status between active and ready."""
    res = extension_registry.toggle_status(server_id)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])

    # If server was deactivated, close any live persistent connection
    if res.get("status") != "active":
        await mcp_session_pool.close_session(server_id)

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

    return {
        "status": "ok",
        "server_id": server_id,
        "settings_schema": settings_schema,
        "env_schema": env_schema,
        "settings": current_settings,
        "env": current_env,
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

    # Invalidate active connection so next invocation picks up updated config
    await mcp_session_pool.close_session(server_id)

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

    await mcp_session_pool.close_session(server_id)
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
