"""
backend/omweb/routers/store_rtr.py

Store Router - Endpoints for agents, tool catalog, custom uploads, and
lifecycle toggling.

Design note on agent settings
-----------------------------
The merge, persistence, and precedence rules for user-saved agent
settings live entirely in ``omweb.agents.registry.AgentRegistry``
(``get_effective_agent`` / ``get_agent_settings`` / ``save_agent_settings``).
This keeps the runtime (``PeldrunEngine.run`` → ``get_effective_agent``)
and the UI (``GET /agents/{id}/settings``) reading from the same source
of truth — the merge cannot drift between the two paths.

The endpoints in this file are thin wrappers. The only piece of
logic that remains local is schema extraction (``_settings_schema_of``),
kept here because the registry exposes effective *values* but not the
*schema* itself. If the registry ever gains a public ``get_agent_schema``
helper, that function should be deleted and the callsites updated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# jsonschema is optional: minimal deployments may not ship it. The
# settings endpoints degrade gracefully (skip validation) rather than
# crash at import time when it is absent.
try:
    import jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive fallback
    _JSONSCHEMA_AVAILABLE = False

from omweb.agents.registry import AgentNotFoundError, agent_registry
from omweb.extensions.bundle_manager import McpBundleError, mcp_bundle_manager
from omweb.extensions.registry import extension_registry
from omweb.tools.bundles.errors import BundleError
from omweb.tools.registry import tool_registry

router = APIRouter()


class AgentUpsertRequest(BaseModel):
    name: str = Field(..., min_length=2)
    role: Optional[str] = "Specialist"
    icon: Optional[str] = "Sparkles"
    # ``core_color`` is the accent color used by the agent card and
    # details modal. It must be declared here, otherwise FastAPI
    # silently drops it from the incoming payload (Pydantic ignores
    # undeclared fields), and the color the user picked in the
    # builder modal never reaches ``create_custom_agent`` — leaving
    # it absent from the persisted manifest.
    core_color: Optional[str] = None
    description: Optional[str] = ""
    system_prompt: str = Field(..., min_length=5)
    tools: List[str] = Field(default_factory=list)
    mcp_servers: List[str] = Field(default_factory=list)
    max_steps: Optional[int] = Field(default=30, ge=1, le=100)


# ─────────────────────────────────────────────────────────────────
# Agents — CRUD & lifecycle
# ─────────────────────────────────────────────────────────────────

@router.get("/agents")
async def list_all_agents():
    return {"status": "ok", "agents": agent_registry.list_agents()}


@router.get("/agents/{agent_id}")
async def get_agent_detail(agent_id: str):
    """Return the complete manifest for a single agent.

    Uses ``get_agent`` (raw manifest) so the details modal always shows
    the developer-authored baseline. User-saved overrides live on
    ``/agents/{id}/settings`` and are merged only at runtime via
    ``get_effective_agent``.
    """
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )
    return {"status": "ok", "agent": agent}


@router.post("/agents")
async def create_agent(req: AgentUpsertRequest):
    created = agent_registry.create_custom_agent(req.model_dump())
    return {"status": "ok", "agent": created}


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, req: AgentUpsertRequest):
    updated = agent_registry.update_custom_agent(agent_id, req.model_dump())
    if not updated:
        raise HTTPException(
            status_code=404,
            detail="Agent not found or is immutable built-in",
        )
    return {"status": "ok", "agent": updated}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    success = agent_registry.delete_custom_agent(agent_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete built-in agent or agent does not exist",
        )
    return {"status": "ok", "message": f"Agent {agent_id} deleted successfully"}


@router.post("/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: str):
    res = agent_registry.toggle_agent_status(agent_id)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return {"status": "ok", "agent": res}


# ─────────────────────────────────────────────────────────────────
# Agent tools — per-agent tool selection & activation
# ─────────────────────────────────────────────────────────────────
#
# Users can disable manifest-declared tools and/or add tools from the
# Store. Both channels are gated on ``settings.tools_editable`` in the
# manifest, so locked agents cannot be altered even by direct API
# calls. Changes are persisted in ``agent_settings.json`` (isolated
# from activation state) and honoured at runtime through
# ``get_effective_agent`` → ``PeldrunEngine.run``.


class _AgentToolsPayload(BaseModel):
    """Request body for PUT /agents/{id}/tools."""

    disabled_tools: List[str] = Field(default_factory=list)
    extra_tools: List[str] = Field(default_factory=list)


@router.get("/agents/{agent_id}/tools")
async def get_agent_tools_endpoint(agent_id: str) -> Dict[str, Any]:
    """Return the current tool selection for an agent.

    Combines:
      - the manifest baseline (``manifest_tools``),
      - the user's exclusions (``disabled_tools``),
      - the user's additions (``extra_tools``),
      - the computed ``effective_tools`` list the runtime will use,
      - the full Store catalog (``available_tools``) so the UI can
        render every tool with ``in_manifest`` metadata.
    """
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    settings_block = agent.get("settings") or {}
    if not settings_block.get("tools_editable", False):
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' does not allow tool editing.",
        )

    state = agent_registry.get_agent_tools_state(agent_id)
    manifest_set = set(state["manifest_tools"])

    available_tools: List[Dict[str, Any]] = []
    for t in tool_registry.list_tools():
        if not t.get("is_enabled", True):
            continue
        tid = t["id"]
        available_tools.append({
            "id": tid,
            "name": t.get("name", tid),
            "description": t.get("description", ""),
            "category": t.get("category", ""),
            "is_builtin": bool(t.get("is_builtin", False)),
            "safety_level": t.get("safety_level"),
            "in_manifest": tid in manifest_set,
        })

    return {
        "status": "ok",
        **state,
        "available_tools": available_tools,
    }


@router.put("/agents/{agent_id}/tools")
async def update_agent_tools_endpoint(
    agent_id: str, req: _AgentToolsPayload
) -> Dict[str, Any]:
    """Persist tool overrides for an agent.

    Validation:
      - the agent must exist and declare ``tools_editable: true``;
      - every entry in ``disabled_tools`` must be a real manifest tool
        (otherwise the user could silently block a tool that is not
        even authorized);
      - every entry in ``extra_tools`` must exist and be enabled in the
        Store (otherwise the runtime would silently drop it later).
    """
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    settings_block = agent.get("settings") or {}
    if not settings_block.get("tools_editable", False):
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' does not allow tool editing.",
        )

    manifest_set = set(agent.get("tools") or [])
    for t in req.disabled_tools:
        if t not in manifest_set:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot disable '{t}' — not in manifest tools.",
            )

    available_ids = {
        t["id"]
        for t in tool_registry.list_tools()
        if t.get("is_enabled", True)
    }
    for t in req.extra_tools:
        if t not in available_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot add '{t}' — not an available store tool.",
            )

    try:
        state = agent_registry.save_agent_tools_state(
            agent_id, req.disabled_tools, req.extra_tools
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "ok", **state}


# ─────────────────────────────────────────────────────────────────
# Agent MCP — per-agent MCP server selection & activation
# ─────────────────────────────────────────────────────────────────

class _AgentMcpPayload(BaseModel):
    """Request body for PUT /agents/{id}/mcp."""
    disabled_mcp_servers: List[str] = Field(default_factory=list)
    extra_mcp_servers: List[str] = Field(default_factory=list)


@router.get("/agents/{agent_id}/mcp")
async def get_agent_mcp_endpoint(agent_id: str) -> Dict[str, Any]:
    """Return the current MCP server selection for an agent."""
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    manifest_mcp = list(agent.get("mcp_servers") or [])

    if hasattr(agent_registry, "get_agent_mcp_state") and callable(agent_registry.get_agent_mcp_state):
        mcp_state = agent_registry.get_agent_mcp_state(agent_id)
    else:
        # Fallback to persistent agent settings container
        settings = agent_registry.get_agent_settings(agent_id) if hasattr(agent_registry, "get_agent_settings") else {}
        saved = settings.get("_mcp_state", {}) if isinstance(settings, dict) else {}
        disabled = list(saved.get("disabled_mcp_servers", []))
        extra = list(saved.get("extra_mcp_servers", []))
        effective = [s for s in manifest_mcp if s not in disabled] + [s for s in extra if s not in disabled]
        mcp_state = {
            "manifest_mcp_servers": manifest_mcp,
            "disabled_mcp_servers": disabled,
            "extra_mcp_servers": extra,
            "effective_mcp_servers": list(dict.fromkeys(effective)),
        }

    manifest_set = set(mcp_state.get("manifest_mcp_servers", []))
    available_servers: List[Dict[str, Any]] = []
    for s in extension_registry.list_mcp_servers():
        sid = s.get("id")
        if not sid:
            continue
        available_servers.append({
            "id": sid,
            "name": s.get("name", sid),
            "description": s.get("description", ""),
            "transport": s.get("transport", "stdio"),
            "status": s.get("status", "ready"),
            "is_builtin": bool(s.get("is_builtin", False)),
            "is_bundle": bool(s.get("is_bundle", False)),
            "in_manifest": sid in manifest_set,
        })

    return {
        "status": "ok",
        **mcp_state,
        "available_mcp_servers": available_servers,
    }


@router.put("/agents/{agent_id}/mcp")
async def update_agent_mcp_endpoint(
    agent_id: str, req: _AgentMcpPayload
) -> Dict[str, Any]:
    """Persist MCP server overrides for an agent."""
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    manifest_mcp = list(agent.get("mcp_servers") or [])
    available_ids = {s.get("id") for s in extension_registry.list_mcp_servers() if s.get("id")}

    for s in req.disabled_mcp_servers:
        if s not in manifest_mcp:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot disable '{s}' — not in manifest MCP servers.",
            )

    for s in req.extra_mcp_servers:
        if s not in available_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot add '{s}' — not a registered store MCP server.",
            )

    if hasattr(agent_registry, "save_agent_mcp_state") and callable(agent_registry.save_agent_mcp_state):
        state = agent_registry.save_agent_mcp_state(
            agent_id, req.disabled_mcp_servers, req.extra_mcp_servers
        )
    else:
        disabled = list(req.disabled_mcp_servers)
        extra = list(req.extra_mcp_servers)
        effective = [s for s in manifest_mcp if s not in disabled] + [s for s in extra if s not in disabled]
        state = {
            "manifest_mcp_servers": manifest_mcp,
            "disabled_mcp_servers": disabled,
            "extra_mcp_servers": extra,
            "effective_mcp_servers": list(dict.fromkeys(effective)),
        }
        try:
            current_settings = agent_registry.get_agent_settings(agent_id)
            current_settings["_mcp_state"] = state
            agent_registry.save_agent_settings(agent_id, current_settings)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to persist MCP state: {exc}")

    return {"status": "ok", **state}


# ─────────────────────────────────────────────────────────────────
# Agent settings — thin wrappers over AgentRegistry
# ─────────────────────────────────────────────────────────────────

def _settings_schema_of(manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return ``settings.schema`` only when it declares properties.

    An empty ``properties: {}`` block (used by zero-config agents) is
    treated the same as no schema at all so the frontend can hide the
    Settings button on the card without special-casing.
    """
    block = manifest.get("settings")
    if not isinstance(block, dict):
        return None
    schema = block.get("schema")
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    return schema


@router.get("/agents/{agent_id}/settings")
async def get_agent_settings_endpoint(agent_id: str) -> Dict[str, Any]:
    """Return the settings schema and current effective values.

    Effective values follow the same precedence chain used at runtime
    (via ``get_agent_settings``), so what the UI shows is exactly what
    the engine will see on the next run.
    """
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    schema = _settings_schema_of(agent)
    if schema is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' has no configurable settings.",
        )

    values = agent_registry.get_agent_settings(agent_id)
    return {"status": "ok", "schema": schema, "values": values}


@router.put("/agents/{agent_id}/settings")
async def update_agent_settings_endpoint(
    agent_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate and persist user-saved settings for an agent.

    Two safety layers protect the store from malformed writes:

      1. ``cleaned`` strips every key not declared in the schema. This
         prevents manifest-owned fields (system_prompt, tools, ...)
         from ever reaching the on-disk settings file, regardless of
         whether the schema declares ``additionalProperties: false``.

      2. jsonschema validation (when available) rejects values that
         violate declared types/bounds. The registry only writes
         after a successful call to ``save_agent_settings``, so a
         failed validation leaves the on-disk file untouched.
    """
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' not found"
        )

    schema = _settings_schema_of(agent)
    if schema is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' has no configurable settings.",
        )

    allowed = set((schema.get("properties") or {}).keys())
    cleaned = {k: v for k, v in payload.items() if k in allowed}

    if _JSONSCHEMA_AVAILABLE:
        try:
            jsonschema.validate(instance=cleaned, schema=schema)
        except jsonschema.ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Validation failed: {exc.message}",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Settings validation error: {exc}",
            )

    try:
        effective = agent_registry.save_agent_settings(agent_id, cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "ok", "values": effective}


# Neutral fallback icon served when an agent has no SVG file on disk.
# Renders a Lucide-style Bot at viewBox 0 0 24 24, using currentColor
# so callers can tint it (e.g., with core_color) without a second
# request. Kept inline (not a file) so the fallback is guaranteed to
# be available even if the icons directory is empty.
_DEFAULT_AGENT_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 8V4H8"/>'
    '<rect width="16" height="12" x="4" y="8" rx="2"/>'
    '<path d="M2 14h2"/>'
    '<path d="M20 14h2"/>'
    '<path d="M15 13v2"/>'
    '<path d="M9 13v2"/>'
    '</svg>'
)


# ─────────────────────────────────────────────────────────────────
# Agent icon — exhaustive filesystem search + always-valid response
# ─────────────────────────────────────────────────────────────────

# Map of common Lucide icon names to their SVG path markup.
#
# Keys are stored in a canonical, normalized form (lowercase, no
# hyphens, no underscores) so that "Sparkles", "sparkles",
# "BarChart3", "bar-chart-3", and "barchart3" all resolve to the
# same entry. The normalization is performed in ``_lucide_svg_for``
# before lookup, so this dict only needs the canonical form.
#
# Values are the raw <path>/<circle>/<rect> elements from the
# corresponding Lucide icon at a 24x24 viewBox. The wrapping <svg>
# element is added by ``_lucide_svg_for`` so every rendered icon
# shares the same stroke/fill/viewBox setup.
#
# Adding a new icon means adding one entry here — no other code
# needs to change. Unknown names fall through to the neutral Bot
# SVG so the frontend never sees an HTTP error.
_LUCIDE_ICON_PATHS: Dict[str, str] = {
    "sparkles": (
        '<path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/>'
        '<path d="M5 3v4"/>'
        '<path d="M19 17v4"/>'
        '<path d="M3 5h4"/>'
        '<path d="M17 19h4"/>'
    ),
    "code2": (
        '<path d="m18 16 4-4-4-4"/>'
        '<path d="m6 8-4 4 4 4"/>'
        '<path d="m14.5 4-5 16"/>'
    ),
    "search": (
        '<circle cx="11" cy="11" r="8"/>'
        '<path d="m21 21-4.3-4.3"/>'
    ),
    "barchart3": (
        '<path d="M3 3v18h18"/>'
        '<path d="M18 17V9"/>'
        '<path d="M13 17V5"/>'
        '<path d="M8 17v-3"/>'
    ),
    "bot": (
        '<path d="M12 8V4H8"/>'
        '<rect width="16" height="12" x="4" y="8" rx="2"/>'
        '<path d="M2 14h2"/>'
        '<path d="M20 14h2"/>'
        '<path d="M15 13v2"/>'
        '<path d="M9 13v2"/>'
    ),
    "user": (
        '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/>'
        '<circle cx="12" cy="7" r="4"/>'
    ),
    "puzzle": (
        '<path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 0 1-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 1 0-3.214 3.214c.446.166.855.497.925.968a.979.979 0 0 1-.276.837l-1.61 1.61a2.404 2.404 0 0 1-1.705.707 2.402 2.402 0 0 1-1.704-.706l-1.568-1.568a1.026 1.026 0 0 0-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 1 1-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 0 0-.289-.877l-1.568-1.568A2.402 2.402 0 0 1 1.998 12c0-.617.236-1.234.706-1.704L4.23 8.77c.24-.24.581-.353.917-.303.515.077.877.528 1.073 1.01a2.5 2.5 0 1 0 3.259-3.259c-.482-.196-.933-.558-1.01-1.073-.05-.336.062-.676.303-.917l1.525-1.525A2.402 2.402 0 0 1 12 1.998c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 1 1 3.237 3.237c-.464.18-.894.527-.967 1.02Z"/>'
    ),
    "filecode2": (
        '<path d="M4 22h14a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v4"/>'
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
        '<path d="m5 12-3 3 3 3"/>'
        '<path d="m9 18 3-3-3-3"/>'
    ),
}


def _lucide_svg_for(icon_name: str) -> Optional[str]:
    """Return a complete SVG document for a Lucide icon name.

    Returns ``None`` when the name is empty, unknown, or has no
    entry in ``_LUCIDE_ICON_PATHS``. Callers MUST fall through to a
    neutral fallback in that case.

    Normalization:
      * Strip whitespace.
      * Strip a trailing ``.svg`` extension.
      * Lowercase.
      * Remove ``-`` and ``_`` separators so ``BarChart3``,
        ``bar-chart-3``, and ``barchart3`` resolve identically.
    """
    if not isinstance(icon_name, str):
        return None
    key = icon_name.strip().lower()
    if key.endswith(".svg"):
        key = key[:-4]
    key = key.replace("-", "").replace("_", "")
    if not key:
        return None
    inner = _LUCIDE_ICON_PATHS.get(key)
    if not inner:
        return None
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{inner}"
        "</svg>"
    )


@router.get("/agents/{agent_id}/icon.svg")
async def get_agent_icon(agent_id: str):
    """
    Serve the SVG icon for an agent.

    Searches every plausible on-disk location (see candidates below)
    and returns the first match. If nothing is found, a neutral Bot
    SVG is returned so the browser never receives a 404 / blank
    response — the frontend always renders *something* useful.

    Candidates are tried in this exact order (first existing file wins):

      ── Custom agents directory (`custom_dir`) ──
        1. custom_dir / {agent_id} / icon.svg
        2. custom_dir / {agent_id}.svg
        3. custom_dir / {icon field}                 (e.g. "peldrun.svg")
        4. custom_dir / icons / {agent_id}.svg
        5. custom_dir / icons / {icon field}

      ── Built-in agents directory (`builtins_dir`) ──
        6. builtins_dir / {agent_id} / icon.svg
        7. builtins_dir / {agent_id} / icons / icon.svg
        8. builtins_dir / {icon field}
        9. builtins_dir / {agent_id}.svg
       10. builtins_dir / icons / {agent_id}.svg     ← the path you named
       11. builtins_dir / icons / {icon field}
       12. builtins_dir / icons / {agent_id} / icon.svg
       13. builtins_dir / icons / {agent_id} / icons / icon.svg

    Every resolved candidate is containment-checked against
    ``builtins_dir`` and ``custom_dir`` so a malicious manifest cannot
    escape via "..".
    """
    try:
        agent = agent_registry.get_agent(agent_id)
    except AgentNotFoundError:
        # Return 404 so the frontend's <img onError> handler fires
        # and renders the Lucide fallback icon instead of a generic
        # bot. The previous behaviour (200 + bot SVG) prevented the
        # onError handler from ever running, so users always saw a
        # bot regardless of the manifest's ``icon`` field.
        raise HTTPException(status_code=404, detail="Agent not found")

    # ── Resolve roots defensively ───────────────────────────────
    try:
        builtins_dir = Path(agent_registry.builtins_dir).resolve()
    except Exception:
        builtins_dir = Path()
    try:
        custom_dir = Path(agent_registry.custom_dir).resolve()
    except Exception:
        custom_dir = Path()

    icon_field = agent.get("icon")
    icon_field_str = icon_field.strip() if isinstance(icon_field, str) else ""

    # ── Build the full candidate list ───────────────────────────
    candidates: list[Path] = []

    # 1-3: custom per-agent + flat + manifest-declared
    candidates.append(custom_dir / agent_id / "icon.svg")
    candidates.append(custom_dir / f"{agent_id}.svg")
    if icon_field_str:
        candidates.append(custom_dir / icon_field_str)

    # 4-5: custom shared icons dir
    candidates.append(custom_dir / "icons" / f"{agent_id}.svg")
    if icon_field_str:
        candidates.append(custom_dir / "icons" / icon_field_str)

    # 6-7: builtin per-agent dir
    candidates.append(builtins_dir / agent_id / "icon.svg")
    candidates.append(builtins_dir / agent_id / "icons" / "icon.svg")

    # 8-9: builtin flat next to manifest + manifest-declared
    if icon_field_str:
        candidates.append(builtins_dir / icon_field_str)
    candidates.append(builtins_dir / f"{agent_id}.svg")

    # 10-11: builtin shared icons (THIS is where builtins/icons/peldrun.svg lives)
    candidates.append(builtins_dir / "icons" / f"{agent_id}.svg")
    if icon_field_str:
        candidates.append(builtins_dir / "icons" / icon_field_str)

    # 12-13: nested inside shared icons
    candidates.append(builtins_dir / "icons" / agent_id / "icon.svg")
    candidates.append(builtins_dir / "icons" / agent_id / "icons" / "icon.svg")

    # ── Try each candidate ───────────────────────────────────────
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)

        # Containment check.
        if (
            builtins_dir not in resolved.parents
            and custom_dir not in resolved.parents
        ):
            continue

        if not resolved.is_file():
            continue

        try:
            return FileResponse(
                resolved,
                media_type="image/svg+xml",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "Content-Type": "image/svg+xml",
                },
            )
        except Exception:
            # Corrupted file or permission error — fall through.
            continue

    # ── Fallback: resolve the ``icon`` field to a Lucide SVG ────
    # Custom agents do not ship SVG files on disk; their ``icon``
    # field names a Lucide icon (e.g. "Sparkles", "Code2"). We
    # generate the corresponding SVG here and return it with a 200
    # so both the <img> probe and the mask render in AgentIcon.tsx
    # receive a valid image with no HTTP error. Returning 404 caused
    # <img onError> to fire but left the mask URL retrying on every
    # render, which produced a request storm for multi-agent views.
    lucide_svg = _lucide_svg_for(icon_field_str)
    if lucide_svg:
        return Response(
            content=lucide_svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=300"},
        )

    # ── Final fallback: neutral Bot SVG ─────────────────────────
    # Reached only when the ``icon`` field is empty or names an
    # unknown Lucide icon. Returning 200 with a neutral bot SVG is
    # safer than 404 because the frontend never sees an HTTP error.
    return Response(
        content=_DEFAULT_AGENT_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


# ─────────────────────────────────────────────────────────────────
# Tools — catalog, bundles, settings, environment, icons
# ─────────────────────────────────────────────────────────────────

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
        raise HTTPException(
            status_code=400,
            detail="Only .py script or .json manifest files are supported.",
        )

    content = await file.read()
    res = tool_registry.save_custom_tool_file(filename, content)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return {"status": "ok", "tool": res}


@router.delete("/tools/{tool_id}")
async def delete_custom_tool(tool_id: str):
    success = tool_registry.delete_custom_tool(tool_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete built-in tool or file does not exist.",
        )
    return {"status": "ok", "message": f"Tool {tool_id} deleted successfully"}


# ─────────────────────────────────────────────────────────────────
# Extensions — catalog, ZIP bundles & manifest upload, deletion
# ─────────────────────────────────────────────────────────────────

@router.get("/extensions")
async def list_all_extensions():
    return {"status": "ok", "extensions": extension_registry.list_extensions()}


@router.post("/extensions/upload")
async def upload_extension_file(file: UploadFile = File(...)):
    """
    Upload an MCP server manifest (.json) or a standalone MCP bundle (.zip).
    ZIP bundles are unpacked into an isolated directory and registered automatically.
    """
    filename = file.filename or "mcp_extension"
    content = await file.read()

    # Case A: Standalone MCP bundle archive (.zip)
    if filename.lower().endswith(".zip"):
        try:
            result = mcp_bundle_manager.install_bundle(content, source_filename=filename)
            return {"status": "ok", "bundle": result, "extension": result.get("server")}
        except McpBundleError as err:
            raise HTTPException(status_code=400, detail=err.to_dict())
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to install MCP bundle: {exc}"
            )

    # Case B: Standard MCP server manifest (.json)
    if filename.lower().endswith(".json"):
        try:
            data = json.loads(content.decode("utf-8"))
            saved = extension_registry.save_extension(data)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to parse extension JSON: {e}",
            )

        # If this upload replaced an existing server manifest with a
        # different transport signature, save_extension() already
        # invalidated the tool cache. Here we go one step further and
        # tear down any live subprocess so the next invocation
        # re-launches with the freshly-written command / url / env.
        saved_id = (saved or {}).get("id") if isinstance(saved, dict) else None
        if saved_id:
            try:
                await mcp_session_pool.close_server_sessions(saved_id)
            except Exception as exc:
                logger.warning(
                    "close_server_sessions('%s') failed after extension "
                    "upload: %s",
                    saved_id,
                    exc,
                )
        return {"status": "ok", "extension": saved}

    raise HTTPException(
        status_code=400,
        detail="Extensions must be uploaded as .json manifest files or .zip bundle archives.",
    )


@router.delete("/extensions/{ext_id}")
async def delete_extension_endpoint(ext_id: str):
    """Delete a custom extension or MCP bundle from the Store catalog."""
    ext = extension_registry.get_extension(ext_id)
    if not ext:
        raise HTTPException(status_code=404, detail=f"Extension '{ext_id}' not found.")

    if ext.get("is_builtin"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in extensions.")

    if ext.get("is_bundle"):
        # mcp_bundle_manager.delete_bundle() calls close_server_sessions
        # internally (Phase 5), so no extra teardown is required here.
        await mcp_bundle_manager.delete_bundle(ext_id)
        return {"status": "ok", "message": f"MCP bundle '{ext_id}' deleted successfully."}

    # Non-bundle extension: tear down every workspace-bound session
    # BEFORE removing the manifest so no orphaned subprocess survives
    # the deletion.
    await mcp_session_pool.close_server_sessions(ext_id)

    success = extension_registry.delete_extension(ext_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete extension.")
    return {"status": "ok", "message": f"Extension '{ext_id}' deleted successfully."}
