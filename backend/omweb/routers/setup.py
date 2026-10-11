"""
backend/omweb/routers/setup.py

PELDRUN Web Setup and Engine Initialization Router.

Handles initial environment onboarding and embedded core validation.
Hardened under PR 3: Completely eliminates external git cloning, heuristic discovery,
and external engine linking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from omweb.config import BACKEND_DIR, get_engine_status, is_valid_peldrun_dir
from omweb.engine_resolver import (
    detect_potential_engine_paths,
    resolve_active_engine_path,
)
from pydantic import BaseModel

router = APIRouter()


class LinkEngineRequest(BaseModel):
    engine_path: str


@router.get("/status")
async def setup_status() -> Dict[str, Any]:
    """Return runtime engine installation and workspace status."""
    return get_engine_status()


@router.post("/detect")
async def detect_engines() -> Dict[str, Any]:
    """Probe system for available engine candidates, anchored to embedded PELDRUN Core."""
    return {"candidates": detect_potential_engine_paths()}


@router.post("/link")
async def link_engine(payload: LinkEngineRequest) -> Dict[str, Any]:
    """
    Link external engine path.
    External linking is deprecated and rejected as PELDRUN Core is natively embedded.
    """
    target_path = Path(payload.engine_path).resolve()
    embedded_path = resolve_active_engine_path().resolve()

    if target_path == embedded_path:
        return {
            "status": "linked",
            "engine_path": str(embedded_path),
            "is_embedded": True,
        }

    raise HTTPException(
        status_code=400,
        detail="External engine linking is not supported. PELDRUN Core is embedded natively.",
    )


@router.post("/install-embedded")
async def install_embedded() -> Dict[str, Any]:
    """
    Validate presence of embedded PELDRUN Core runtime natively.
    Eliminates external subprocess git cloning entirely.
    """
    embedded_core = (BACKEND_DIR / "peldrun").resolve()
    if is_valid_peldrun_dir(embedded_core):
        return {"status": "already_installed", "engine_path": str(embedded_core)}

    if is_valid_peldrun_dir(BACKEND_DIR):
        return {"status": "already_installed", "engine_path": str(BACKEND_DIR)}

    raise HTTPException(
        status_code=500,
        detail="Embedded PELDRUN Core package is missing from the backend installation directory.",
    )
