# backend/omweb/routers/system.py

"""
System introspection router.

Exposes read-only endpoints that let the frontend diagnose the active
backend connection: liveness probe, build identity, and host metadata.
All fields are safe to expose: no secrets, no config values that could
leak operator credentials, no filesystem paths.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Request

router = APIRouter()

# Captured once at import time so every call reports the same start
# timestamp regardless of when it is queried.
_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _detect_version() -> str:
    """Best-effort version detection.

    Tries installed package metadata first, then ``pyproject.toml`` at
    the backend directory and at the project root, then a placeholder.
    """
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("peldrun")
    except Exception:
        pass

    here = Path(__file__).resolve()
    for depth in (2, 3, 4):
        try:
            candidate = here.parents[depth] / "pyproject.toml"
            if not candidate.exists():
                continue
            for line in candidate.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("version") and "=" in s:
                    _, _, v = s.partition("=")
                    cleaned = v.strip().strip('"').strip("'")
                    if cleaned:
                        return cleaned
        except Exception:
            continue
    return "unknown"


@router.get("/health")
async def health() -> Dict[str, Any]:
    """Return liveness + identity information for the active backend.

    Response shape is intentionally minimal and stable so it can be
    probed by monitoring, load balancers, and the frontend diagnostic
    card without version drift.
    """
    return {
        "status": "ok",
        "service": "peldrun-backend",
        "version": _detect_version(),
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/info")
async def info(request: Request) -> Dict[str, Any]:
    """Extended diagnostic payload used by the settings page.

    Includes the Host header as observed by the backend, which lets
    operators confirm that a reverse-proxy rewrite is routing to the
    intended instance (e.g. a staging frontend talking to a staging
    backend, not production).
    """
    return {
        "status": "ok",
        "service": "peldrun-backend",
        "version": _detect_version(),
        "host_header": request.headers.get("host"),
        "client_host": request.client.host if request.client else None,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Version introspection (audit-adjacent feature)
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Resolve the PELDRUN repository root from this file's location."""
    # backend/omweb/routers/system.py → repo root = parents[3]
    return Path(__file__).resolve().parents[3]


def _read_version_file(path: Path) -> Optional[Dict[str, Any]]:
    """Return parsed VERSION.json, or None when missing/unparseable."""
    import json as _json

    try:
        if not path.exists():
            return None
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@router.get("/version")
async def version() -> Dict[str, Any]:
    """Return the app version and every component version.

    Response shape:

        {
            "app": {
                "version": "1.0.0",
                "released_at": "...",
                "released_commit": "...",
            },
            "components": {
                "omweb":    {"version": "0.20.1", "released_commit": "..."},
                "peldrun":  {"version": "0.1.0",  "released_commit": "..."},
                "frontend": {"version": "0.1.0",  "released_commit": "..."},
            },
            "all_in_sync": true,
            "source": "VERSION.json files",
        }

    When a VERSION.json file is missing or unreadable, that component
    is reported with a null version so the caller can surface a
    diagnostic instead of crashing.
    """
    repo = _repo_root()

    app_doc = _read_version_file(repo / "VERSION.json")
    app_version = (app_doc or {}).get("version", "unknown")
    app_released_at = (app_doc or {}).get("released_at")
    app_released_commit = (app_doc or {}).get("released_commit")

    components: Dict[str, Dict[str, Any]] = {}
    component_paths = {
        "omweb": repo / "backend" / "omweb" / "VERSION.json",
        "peldrun": repo / "backend" / "peldrun" / "VERSION.json",
        "frontend": repo / "frontend" / "VERSION.json",
    }
    all_in_sync = True
    for name, path in component_paths.items():
        doc = _read_version_file(path)
        if doc is None:
            components[name] = {"version": None, "released_commit": None}
            all_in_sync = False
            continue
        components[name] = {
            "version": doc.get("version"),
            "released_commit": doc.get("released_commit"),
        }
        expected = ((app_doc or {}).get("components") or {}).get(name, {}).get("version")
        if expected and expected != doc.get("version"):
            all_in_sync = False

    return {
        "app": {
            "version": app_version,
            "released_at": app_released_at,
            "released_commit": app_released_commit,
        },
        "components": components,
        "all_in_sync": all_in_sync,
        "source": "VERSION.json files",
    }
