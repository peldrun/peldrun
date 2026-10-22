"""
Store Router - Endpoints for agents, tool catalog, custom uploads, and lifecycle toggling.
"""

from __future__ import annotations
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

from omweb.agents.registry import agent_registry
from omweb.tools.registry import tool_registry
from omweb.extensions.registry import extension_registry
from omweb.tools.bundles.errors import BundleError

router = APIRouter()


class AgentUpsertRequest(BaseModel):
    name: str = Field(..., min_length=2)
    role: Optional[str] = "Specialist"
    icon: Optional[str] = "Sparkles"
    description: Optional[str] = ""
    system_prompt: str = Field(..., min_length=5)
    tools: List[str] = Field(default_factory=list)
    max_steps: Optional[int] = Field(default=30, ge=1, le=100)


@router.get("/agents")
async def list_all_agents():
    return {"status": "ok", "agents": agent_registry.list_agents()}


@router.get("/agents/{agent_id}")
async def get_agent_detail(agent_id: str):
    agent = agent_registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok", "agent": agent}


@router.post("/agents")
async def create_agent(req: AgentUpsertRequest):
    created = agent_registry.create_custom_agent(req.model_dump())
    return {"status": "ok", "agent": created}


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, req: AgentUpsertRequest):
    updated = agent_registry.update_custom_agent(agent_id, req.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="Agent not found or is immutable built-in")
    return {"status": "ok", "agent": updated}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    success = agent_registry.delete_custom_agent(agent_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot delete built-in agent or agent does not exist")
    return {"status": "ok", "message": f"Agent {agent_id} deleted successfully"}


@router.post("/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: str):
    res = agent_registry.toggle_agent_status(agent_id)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return {"status": "ok", "agent": res}


@router.get("/tools")
async def list_all_tools():
    return {"status": "ok", "tools": tool_registry.list_tools()}


@router.get("/tools/bundles")
async def list_bundles():
    """
    List every installed bundle.

    IMPORTANT: this route MUST be declared BEFORE `/tools/{tool_id}`.
    FastAPI matches routes in registration order, so a generic
    `{tool_id}` pattern would otherwise catch the literal "bundles"
    segment and return 404. Every other literal segment under /tools
    (e.g. "upload", "extensions") must be declared before the catch-all
    `{tool_id}` route for the same reason.

    When no BundleManager is attached to the registry the endpoint
    returns an empty list instead of failing, so the frontend can
    render the "no bundles installed" state without error handling.
    """
    manager = tool_registry.get_bundle_manager()
    if manager is None:
        return {"status": "ok", "bundles": [], "total": 0}

    result = []
    for entry in manager.list_bundles():
        installed = getattr(entry, "installed_versions", None)
        if installed is None:
            installed = [entry.active_version]
        elif not isinstance(installed, list):
            installed = list(installed)

        result.append(
            {
                "id": entry.id,
                "name": getattr(entry, "name", entry.id),
                "description": getattr(entry, "description", ""),
                "safety_level": getattr(entry, "safety_level", "custom"),
                "status": getattr(entry, "status", "active"),
                "active_version": entry.active_version,
                "active_hash": getattr(entry, "active_hash", ""),
                "installed_versions": installed,
                "capabilities": list(getattr(entry, "capabilities", [])),
                "tags": list(getattr(entry, "tags", [])),
            }
        )

    return {"status": "ok", "bundles": result, "total": len(result)}


# ─────────────────────────────────────────────────────────────────
# Bundle management — single-item, delete, activate, health, upload
# ─────────────────────────────────────────────────────────────────
# IMPORTANT: every route below MUST be registered BEFORE the generic
# `/tools/{tool_id}` route. FastAPI matches in registration order, so
# `/tools/bundles/{id}` must be declared before `/tools/{tool_id}/...`
# would otherwise catch "bundles" as a tool id.
#
# `BundleError` carries a structured payload ({error, code, details})
# which is forwarded verbatim into the HTTPException.detail so the
# frontend can surface the exact reason (e.g. BUNDLE_DELETE_REFUSED).

def _bundle_entry_to_dict(entry: Any) -> Dict[str, Any]:
    """
    Serialise a `BundleEntry` (pydantic v2 model) or a duck-typed
    equivalent into a plain dict.

    Pydantic's `model_dump()` is preferred because it recursively
    serialises the `installed_versions` list with no manual mapping.
    The dict fallback keeps the endpoint resilient to future schema
    swaps (e.g. dataclasses) without code changes.
    """
    if isinstance(entry, dict):
        return entry
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    return {
        "id": getattr(entry, "id", ""),
        "name": getattr(entry, "name", ""),
        "description": getattr(entry, "description", ""),
        "active_version": getattr(entry, "active_version", ""),
        "active_hash": getattr(entry, "active_hash", ""),
        "installed_versions": list(getattr(entry, "installed_versions", []) or []),
        "status": getattr(entry, "status", "active"),
        "safety_level": getattr(entry, "safety_level", "custom"),
        "capabilities": list(getattr(entry, "capabilities", []) or []),
        "tags": list(getattr(entry, "tags", []) or []),
        "author": getattr(entry, "author", None),
        "homepage": getattr(entry, "homepage", None),
    }


def _install_result_to_dict(result: Any) -> Dict[str, Any]:
    """Serialise a `BundleInstallResult` (pydantic v2 model) to a dict."""
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return {
        "id": getattr(result, "id", ""),
        "version": getattr(result, "version", ""),
        "hash": getattr(result, "hash", ""),
        "name": getattr(result, "name", ""),
        "description": getattr(result, "description", ""),
        "size_bytes": getattr(result, "size_bytes", 0),
        "file_count": getattr(result, "file_count", 0),
        "capabilities": list(getattr(result, "capabilities", []) or []),
        "tags": list(getattr(result, "tags", []) or []),
        "install_report": getattr(result, "install_report", None),
        "warnings": list(getattr(result, "warnings", []) or []),
    }


class _ActivateVersionRequest(BaseModel):
    """Body payload for activating a specific bundle version."""

    version: str = Field(..., min_length=1)


@router.get("/tools/bundles/{bundle_id}")
async def get_bundle(bundle_id: str):
    """Return a single bundle's registry entry by id."""
    manager = tool_registry.get_bundle_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Bundle subsystem not available",
        )
    entry = manager.get_bundle(bundle_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Bundle '{bundle_id}' not found",
        )
    return {"status": "ok", "bundle": _bundle_entry_to_dict(entry)}


@router.delete("/tools/bundles/{bundle_id}")
async def delete_bundle_endpoint(bundle_id: str, force: bool = False):
    """
    Delete a bundle.

    When the bundle has more than one installed version and `force=false`,
    the manager raises `BUNDLE_DELETE_REFUSED` so a rollback target is
    not lost by accident. The structured error is forwarded so the
    frontend can prompt the user to retry with `?force=true`.
    """
    manager = tool_registry.get_bundle_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Bundle subsystem not available",
        )
    try:
        deleted = manager.delete_bundle(bundle_id, force=force)
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict())
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Bundle '{bundle_id}' not found",
        )
    return {"status": "ok", "bundle_id": bundle_id, "deleted": True}


@router.post("/tools/bundles/{bundle_id}/activate")
async def activate_bundle_version(bundle_id: str, req: _ActivateVersionRequest):
    """Switch the active version pointer to a previously installed version."""
    manager = tool_registry.get_bundle_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Bundle subsystem not available",
        )
    try:
        ok = manager.activate_version(bundle_id, req.version)
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict())
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Bundle '{bundle_id}' or version '{req.version}' not found",
        )
    return {
        "status": "ok",
        "bundle_id": bundle_id,
        "active_version": req.version,
    }


@router.get("/tools/bundles/{bundle_id}/health")
async def get_bundle_health(bundle_id: str):
    """Run the manager's health checks for a bundle's active version."""
    manager = tool_registry.get_bundle_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Bundle subsystem not available",
        )
    try:
        report = manager.check_health(bundle_id)
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict())
    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"Bundle '{bundle_id}' not found",
        )
    data = report.model_dump() if hasattr(report, "model_dump") else report
    return {"status": "ok", "health": data}


@router.post("/tools/upload-bundle")
async def upload_bundle_endpoint(
    file: UploadFile = File(...),
    install_requirements: bool = Form(True),
):
    """
    Upload and install a `.zip` bundle.

    The frontend sends `install_requirements` as a string boolean in the
    multipart form. FastAPI coerces it to a Python bool because the
    field is declared as `Form(True)`.

    Any `BundleError` is translated into a structured JSON detail
    ({error, code, details}) so the upload UI can display the exact
    reason (e.g. "manifest invalid", "zip too large", ...) instead of a
    generic 400.
    """
    manager = tool_registry.get_bundle_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Bundle subsystem not available",
        )

    filename = file.filename or "bundle.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Bundles must be uploaded as .zip archives",
        )

    content = await file.read()

    try:
        result = manager.install_bundle(
            content,
            source_filename=filename,
            install_requirements=install_requirements,
        )
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict())
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Bundle install failed: {exc}",
        )

    return {"status": "ok", "bundle": _install_result_to_dict(result)}


@router.get("/tools/{tool_id}")
async def get_tool_detail(tool_id: str):
    """
    Return the complete manifest for a single tool, including every
    optional field (author, license, examples, settings, ...).

    The list endpoint (`GET /tools`) returns the same payload, but this
    route exists so the frontend can lazily fetch a single manifest when
    the user opens the details modal without re-downloading the entire
    catalog.
    """
    tool = tool_registry.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"status": "ok", "tool": tool}


@router.get("/tools/{tool_id}/settings")
async def get_tool_settings(tool_id: str) -> Dict[str, Any]:
    """
    Return the settings schema and current effective values for a tool.

    Effective values are the schema defaults merged with any values
    previously saved by the user (user values override defaults). If
    the tool does not declare a ``settings_schema`` it is not
    configurable and this endpoint returns 404 so the frontend can
    hide the Settings button on the tool card.
    """
    schema = tool_registry.get_tool_settings_schema(tool_id)
    if schema is None:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_id}' has no configurable settings.",
        )
    values = tool_registry.get_tool_settings(tool_id)
    return {"status": "ok", "schema": schema, "values": values}


@router.put("/tools/{tool_id}/settings")
async def update_tool_settings(
    tool_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Validate and persist user settings for a tool.

    Values are validated against the tool's ``settings_schema`` using
    jsonschema. On validation failure this returns 400 and the on-disk
    settings file is left untouched (the atomic write only happens
    after successful validation inside ToolSettingsManager).
    """
    try:
        merged = tool_registry.save_tool_settings(tool_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "values": merged}


@router.get("/tools/{tool_id}/test-environment")
async def test_tool_environment(tool_id: str) -> Dict[str, Any]:
    """
    Probe a tool's runtime environment and return a structured report.

    Only ``browser_interactive`` has concrete checks today:
      * Playwright Python package is importable.
      * Chromium binary is present on disk.
      * ``screenshot_dir`` is writable and contained in the workspace.

    For any other tool, a benign "no checks available" report is
    returned so the frontend can render the button without special
    casing.

    The endpoint is side-effect free: it never launches a browser and
    never mutates the settings file.
    """
    if tool_id == "browser_interactive":
        try:
            from peldrun.tools.builtins.browser_interactive.environment import (
                probe_browser_environment,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Environment probe unavailable: {exc}",
            )

        # Load the user's current settings so screenshot_dir reflects
        # the saved configuration (not the raw manifest defaults).
        try:
            current_values = tool_registry.get_tool_settings(tool_id)
        except Exception:
            current_values = {}
        screenshot_dir = None
        if isinstance(current_values, dict):
            sd = current_values.get("screenshot_dir")
            if isinstance(sd, str) and sd.strip():
                screenshot_dir = sd.strip()

        report = await probe_browser_environment(
            workspace_root=None,   # probe against backend CWD
            screenshot_dir=screenshot_dir,
        )
        return {"status": "ok", **report}

    # Generic tools: report a benign pass.
    return {
        "status": "ok",
        "tool_id": tool_id,
        "ready": True,
        "checks": [
            {
                "name": "no_environment_checks",
                "label": "Environment checks",
                "passed": True,
                "message": "No environment checks are defined for this tool.",
            }
        ],
    }


@router.get("/tools/{tool_id}/icon.svg")
async def get_tool_icon(tool_id: str):
    """
    Serve the SVG icon for a tool.

    Icons can live in one of several layouts on disk, and the manifest
    field alone is not authoritative (some tools ship an icon without
    declaring it, and some manifests point at a file that has since
    moved). To stay robust across every layout this handler tries a
    list of candidate paths in priority order and serves the first
    existing file.

    Candidate order (first match wins):
      1. builtins/{tool_id}/icon.svg           (per-tool directory)
      2. builtins/{tool["icon"]}               (manifest-declared path)
      3. builtins/{tool_id}.svg                (flat next to manifest)
      4. builtins/icons/{tool_id}.svg          (shared icons, flat)
      5. builtins/icons/{tool_id}/icon.svg     (shared icons, per-tool dir)
      6. builtins/{tool_id}/icons/icon.svg     (nested inside per-tool dir)

    Every candidate is resolved and checked to live inside `builtins/`
    so a malicious manifest cannot escape the directory via "..".
    Duplicates (e.g. when `icon` already equals "icons/bash.svg") are
    visited only once. All failure modes collapse into a 404 so the
    frontend can fall back to a Lucide icon without needing to
    distinguish them.
    """
    tool = tool_registry.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    builtins_dir = tool_registry.builtins_dir.resolve()

    # Build the candidate list in priority order.
    candidates: list[Path] = []

    # 1. Per-tool directory (modern, self-contained layout).
    candidates.append(builtins_dir / tool_id / "icon.svg")

    # 2. Manifest-declared path (explicit developer intent).
    icon_rel = tool.get("icon")
    if isinstance(icon_rel, str) and icon_rel.strip():
        candidates.append(builtins_dir / icon_rel)

    # 3. Flat SVG next to the manifest (bash.svg alongside bash.json).
    candidates.append(builtins_dir / f"{tool_id}.svg")

    # 4. Shared icons directory, flat (icons/bash.svg).
    candidates.append(builtins_dir / "icons" / f"{tool_id}.svg")

    # 5. Per-tool subdirectory inside shared icons (icons/bash/icon.svg).
    candidates.append(builtins_dir / "icons" / tool_id / "icon.svg")

    # 6. Nested icons directory inside per-tool dir
    #    (bash/icons/icon.svg).
    candidates.append(builtins_dir / tool_id / "icons" / "icon.svg")

    # Deduplicate by resolved path so the filesystem is hit only once
    # per unique file (e.g. when `icon == "icons/bash.svg"` candidate 2
    # and candidate 4 collapse to the same file).
    seen: set[Path] = set()

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue

        if resolved in seen:
            continue
        seen.add(resolved)

        # Containment check: reject anything outside builtins/.
        if builtins_dir not in resolved.parents:
            continue

        if resolved.is_file():
            return FileResponse(
                resolved,
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=3600"},
            )

    raise HTTPException(status_code=404, detail="Icon file missing")


@router.post("/tools/{tool_id}/toggle")
async def toggle_tool(tool_id: str):
    res = tool_registry.toggle_tool_status(tool_id)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])
    return {"status": "ok", "tool": res}


@router.post("/tools/upload")
async def upload_tool_file(file: UploadFile = File(...)):
    filename = file.filename or "custom_tool.py"
    if not (filename.endswith(".py") or filename.endswith(".json")):
        raise HTTPException(status_code=400, detail="Only .py script or .json manifest files are supported.")

    content = await file.read()
    res = tool_registry.save_custom_tool_file(filename, content)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return {"status": "ok", "tool": res}


@router.delete("/tools/{tool_id}")
async def delete_custom_tool(tool_id: str):
    success = tool_registry.delete_custom_tool(tool_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot delete built-in tool or file does not exist.")
    return {"status": "ok", "message": f"Tool {tool_id} deleted successfully"}


@router.get("/extensions")
async def list_all_extensions():
    return {"status": "ok", "extensions": extension_registry.list_extensions()}


@router.post("/extensions/upload")
async def upload_extension_json(file: UploadFile = File(...)):
    filename = file.filename or "mcp_extension.json"
    if not filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Extensions must be uploaded as JSON manifest files.")

    content = await file.read()
    try:
        data = json.loads(content.decode("utf-8"))
        saved = extension_registry.save_extension(data)
        return {"status": "ok", "extension": saved}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse extension JSON: {e}")
