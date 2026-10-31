# Path: backend/omweb/routers/runtime_settings_rtr.py
"""
Runtime settings REST API.

Exposes the three-tier runtime configuration store as a simple CRUD
surface. All endpoints operate on ``<storage_root>/runtime_settings.json``
via ``omweb.services.runtime_settings``.

Design notes
------------
* ``GET`` returns both the effective values AND the source tier for
  each field (``file`` / ``env:VAR`` / ``default``). The UI uses this
  to display a badge so users know whether they can actually change a
  value or whether an env var is winning.
* ``PATCH`` accepts a partial body — only the fields supplied are
  updated. Omitting a field leaves it untouched.
* Setting a field to ``null`` explicitly reverts it to env/default.
* The API key field is write-only: it accepts new values, but always
  returns ``••••<last4>`` on read.
* The ``schema`` field in the ``GET`` response contains field metadata
  (label, type, min/max, description) so the frontend can render the
  form generically without hardcoding per-field markup.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from omweb.services import runtime_settings as rt

logger = logging.getLogger("omweb.routers.runtime_settings")

router = APIRouter(prefix="/api/config/runtime-settings", tags=["config", "runtime-settings"])


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class SettingsPatchRequest(BaseModel):
    """
    Partial update body for ``PATCH /api/config/runtime-settings``.

    NOTE: ``llm_providers`` is intentionally absent. LLM provider
    settings are managed by the dedicated Models settings page. Any
    legacy client that still sends ``llm_providers`` will receive a
    ``422 Unprocessable Entity`` — an explicit signal to update, rather
    than the silent dead-code behavior of the previous version.
    """

    model_config = ConfigDict(extra="forbid")

    timeouts: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Partial updates for the timeouts section.",
    )
    cleanup: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Partial updates for the cleanup section.",
    )
    streaming: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Partial updates for the streaming section.",
    )


# Field metadata consumed by the frontend's generic renderer.
_SETTINGS_SCHEMA: Dict[str, Dict[str, Dict[str, Any]]] = {
    "timeouts": {
        "llm_request_timeout_seconds": {
            "label": "LLM Request Timeout",
            "type": "number",
            "min": 1.0,
            "max": 3600.0,
            "step": 10.0,
            "description": (
                "HTTP timeout for each LLM provider request. "
                "Raise for very slow models (70B on CPU, large images). "
                "Enforced by AsyncLLMClient."
            ),
        },
        "step_timeout_seconds": {
            "label": "Step Timeout",
            "type": "number",
            "min": 1.0,
            "max": 1800.0,
            "step": 10.0,
            "description": (
                "Timeout for one agent step (LLM call + tool executions). "
                "Capped by the remaining total timeout. "
                "Enforced by AgentRunner."
            ),
        },
        "total_timeout_seconds": {
            "label": "Total Run Timeout",
            "type": "number",
            "min": 10.0,
            "max": 7200.0,
            "step": 60.0,
            "description": (
                "Wall-clock ceiling for the entire run. "
                "Prevents runaway agent loops from consuming hours. "
                "Enforced by AgentRunner across all steps."
            ),
        },
    },
    "cleanup": {
        "enabled": {
            "label": "Enable Auto Cleanup",
            "type": "boolean",
            "description": (
                "Master switch for periodic sweeping of temp files and "
                "checkpoint cache. Effective immediately on save."
            ),
        },
        "interval_hours": {
            "label": "Interval (hours)",
            "type": "number",
            "min": 0.016,
            "max": 168.0,
            "step": 0.5,
            "description": (
                "How often the sweeper runs. "
                "Effective on the next cycle — no restart required."
            ),
        },
        "max_age_hours": {
            "label": "Max File Age (hours)",
            "type": "number",
            "min": 0.1,
            "max": 720.0,
            "step": 1.0,
            "description": (
                "Files older than this are removed on the next sweep. "
                "Effective immediately."
            ),
        },
    },
    "streaming": {
        "flush_interval_seconds": {
            "label": "Flush Interval (seconds)",
            "type": "number",
            "min": 0.01,
            "max": 1.0,
            "step": 0.01,
            "description": (
                "Minimum delay between SSE flushes during agent streaming. "
                "Lower = smoother; higher = lighter DB load. "
                "Effective on the next think() call."
            ),
        },
        "flush_chars": {
            "label": "Flush Characters",
            "type": "number",
            "min": 5,
            "max": 200,
            "step": 5,
            "description": (
                "Minimum buffered characters before an SSE flush. "
                "Effective on the next think() call."
            ),
        },
    },
}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def get_runtime_settings():
    """
    Return effective values, source tiers, defaults, and schema.

    Response shape::

        {
            "values":   { "cleanup": { "interval_hours": 6.0, ... }, ... },
            "sources":  { "cleanup": { "interval_hours": "file", ... }, ... },
            "defaults": { "cleanup": { "interval_hours": 6.0, ... }, ... },
            "schema":   { "cleanup": { "interval_hours": { ... } }, ... },
            "env_vars": { "cleanup": { "interval_hours": "PELDRUN_...", ... } }
        }
    """
    snapshot = rt.get_effective_snapshot()
    return {
        **snapshot,
        "schema": _SETTINGS_SCHEMA,
        "env_vars": rt.ENV_VARS,
    }


@router.patch("")
async def patch_runtime_settings(payload: SettingsPatchRequest):
    """
    Merge partial updates into the runtime settings file and persist.

    Only the fields present in the request body are updated. A field
    set to ``null`` explicitly reverts to env/default. Sections not
    mentioned are untouched.
    """
    updates = payload.model_dump(exclude_unset=True, exclude_none=False)
    applied: Dict[str, str] = {}

    for section, section_updates in updates.items():
        if not isinstance(section_updates, dict):
            continue
        try:
            rt.update_section(section, section_updates)
            applied[section] = ", ".join(section_updates.keys())
        except Exception as exc:
            logger.warning(
                "Failed to update section %s: %s", section, exc, exc_info=True
            )
            raise HTTPException(
                status_code=400,
                detail=f"Failed to update section '{section}': {exc}",
            )

    logger.info("Runtime settings updated: %s", applied)

    # Return the fresh snapshot for the UI to render immediately.
    return {
        "status": "ok",
        "applied": applied,
        **(await get_runtime_settings()),
    }


@router.delete("")
async def reset_runtime_settings():
    """
    Delete ``runtime_settings.json``, reverting all fields to env/default.

    Idempotent: resetting an already-empty file succeeds.
    """
    rt.reset_document()
    logger.info("Runtime settings reset to defaults.")
    return {
        "status": "ok",
        "message": "All runtime settings reverted to defaults.",
        **(await get_runtime_settings()),
    }


@router.post("/trigger-cleanup")
async def trigger_cleanup_now(
    older_than_hours: Optional[float] = None,
):
    """
    Immediately invoke the temp/checkpoint cleanup routine.

    Uses the effective ``max_age_hours`` when ``older_than_hours`` is
    not supplied. Useful for the UI's "Clean Now" button.
    """
    effective_age = (
        older_than_hours
        if older_than_hours is not None
        else float(rt.resolve("cleanup", "max_age_hours"))
    )
    from omweb.routers.files import cleanup_temp_artifacts

    report = cleanup_temp_artifacts(older_than_hours=effective_age)
    return {"status": "ok", "report": report}


__all__ = ["router"]
