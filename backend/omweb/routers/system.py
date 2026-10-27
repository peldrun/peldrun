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
