"""
PELDRUN Web Setup and Engine Initialization Router.

Handles initial environment onboarding, candidate engine discovery,
and embedded core validation without external git cloning dependencies.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from fastapi import APIRouter, HTTPException
from omweb.config import BACKEND_DIR, get_engine_status
from omweb.engine_resolver import (
    PROJECT_ROOT,
    detect_potential_engine_paths,
    get_persisted_engine_path,
    is_valid_peldrun_dir,
    save_engine_path,
)
from pydantic import BaseModel

router = APIRouter()


class LinkEngineRequest(BaseModel):
    engine_path: str


@router.get("/status")
async def setup_status():
    """Return runtime engine installation and workspace status."""
    return get_engine_status()


@router.post("/detect")
async def detect_engines():
    """Probe system for available embedded and legacy engine candidates."""
    return {"candidates": detect_potential_engine_paths()}


@router.post("/link")
async def link_engine(payload: LinkEngineRequest):
    """Link an external engine path while verifying directory validity."""
    target_path = Path(payload.engine_path).resolve()
    if not is_valid_peldrun_dir(target_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid engine directory: missing agent entrypoint or configuration.",
        )
    save_engine_path(target_path)
    return {"status": "linked", "engine_path": str(target_path)}


@router.post("/install-embedded")
async def install_embedded():
    """Validate or link embedded PELDRUN Core runtime natively."""
    # 1. Native embedded package takes precedence
    embedded_core = (BACKEND_DIR / "peldrun").resolve()
    if is_valid_peldrun_dir(embedded_core):
        save_engine_path(embedded_core, engine_type="peldrun")
        return {"status": "already_installed", "engine_path": str(embedded_core)}

    # 2. Legacy fallback path
    embedded_dir = (PROJECT_ROOT / "engine" / "peldrun").resolve()
    if is_valid_peldrun_dir(embedded_dir):
        save_engine_path(embedded_dir)
        return {"status": "already_installed", "engine_path": str(embedded_dir)}

    embedded_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["git", "clone", "https://github.com/FoundationAgents/peldrun.git", str(embedded_dir)],
            check=True,
            capture_output=True,
            text=True,
        )

        example_cfg = embedded_dir / "config" / "config.example.toml"
        target_cfg = embedded_dir / "config" / "config.toml"
        if example_cfg.exists() and not target_cfg.exists():
            shutil.copyfile(example_cfg, target_cfg)

        save_engine_path(embedded_dir)
        return {"status": "installed", "engine_path": str(embedded_dir)}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git clone failed: {e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))