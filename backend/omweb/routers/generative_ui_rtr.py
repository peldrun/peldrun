"""
backend/omweb/routers/generative_ui_rtr.py

Standalone router for Generative UI rendering settings.

Endpoints (mounted at /api/config by omweb.main):
    GET  /api/config/generative-ui  — read persisted settings
    POST /api/config/generative-ui  — overwrite and persist new settings

Persistence is delegated to ``omweb.agent_bridge_parts.generative_ui_store``,
which owns the atomic file I/O and schema normalization. This router is a
thin HTTP shell over that module.

Callers
-------
- The frontend ``GenerativeUIBackendSync`` component hydrates from GET on
  mount and pushes a debounced POST after every user toggle.
- The dispatcher reads the on-disk file directly (not via HTTP) so chat and
  agent dispatch have zero HTTP dependency on this router.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

from omweb.agent_bridge_parts.generative_ui_store import (
    load_generative_ui_settings,
    save_generative_ui_settings,
)
from omweb.agent_bridge_parts.generative_ui_telemetry_store import (
    load_telemetry,
    reset_telemetry,
    save_telemetry,
)

logger = logging.getLogger("omweb.routers.generative_ui_rtr")

router = APIRouter()


@router.get("/generative-ui")
async def get_generative_ui_settings() -> Dict[str, Any]:
    """
    Return the current persisted Generative UI settings.

    Always succeeds. A missing or corrupt file yields the canonical defaults,
    so the frontend never needs to special-case a fresh install.
    """
    settings = load_generative_ui_settings()
    return {"ok": True, "settings": settings}


@router.post("/generative-ui")
async def save_generative_ui_settings_endpoint(
    payload: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """
    Persist new Generative UI settings.

    The payload is normalized before writing, so callers can send a partial
    dict (e.g. ``{"enabled": false}``) and still get a complete, valid record
    on disk. The response echoes the canonical shape.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    try:
        saved = save_generative_ui_settings(payload)
    except Exception as exc:
        logger.exception("Failed to persist GenUI settings")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist Generative UI settings: {exc}",
        )

    return {"ok": True, "settings": saved}


@router.get("/generative-ui-telemetry")
async def get_generative_ui_telemetry() -> Dict[str, Any]:
    """Return the persisted rendering telemetry counters."""
    return {"ok": True, "telemetry": load_telemetry()}


@router.post("/generative-ui-telemetry")
async def save_generative_ui_telemetry(
    payload: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """Overwrite the persisted rendering telemetry counters."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    try:
        saved = save_telemetry(payload)
    except Exception as exc:
        logger.exception("Failed to persist GenUI telemetry")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist telemetry: {exc}",
        )
    return {"ok": True, "telemetry": saved}


@router.delete("/generative-ui-telemetry")
async def reset_generative_ui_telemetry() -> Dict[str, Any]:
    """Reset the persisted rendering telemetry counters to zero."""
    try:
        cleared = reset_telemetry()
    except Exception as exc:
        logger.exception("Failed to reset GenUI telemetry")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset telemetry: {exc}",
        )
    return {"ok": True, "telemetry": cleared}


__all__ = ["router"]
