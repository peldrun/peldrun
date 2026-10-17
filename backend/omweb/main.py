"""
backend/omweb/main.py

PELDRUN Web FastAPI Application Entrypoint.

Initializes middleware, static files, and canonical REST routers:
- /api/chats
- /api/run
- /api/status
- /api/config
- /api/search (Dedicated Search Orchestration, Providers & Telemetry API)
- /api/files
- /api/mcp
- /api/setup
- /api/storage
- /api/store
- /api/store/tools/bundles (Tool Bundles Subsystem)
- /api/telemetry (P1-03D Canonical Analytics & Metering APIs)
- /api/source-control (Source Control Integration: providers,
  repositories, workspaces, approvals, operations, audit)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from omweb.config import STORAGE_ROOT
from omweb.routers import (
    chats,
    config_rtr,
    files,
    mcp,
    run,
    search_rtr,
    setup,
    source_control,
    status,
    storage,
    store_rtr,
    telemetry,
)

# Tool Bundles Subsystem router
from omweb.routers.store import bundles as store_bundles

# Attach the bundle manager to the tool registry so installed bundles
# are discoverable by the agent runtime.
from omweb.routers.store.bundles import get_bundle_manager
from omweb.tools.registry import tool_registry as _tool_registry

_tool_registry.set_bundle_manager(get_bundle_manager())

logger = logging.getLogger("omweb.main")

app = FastAPI(
    title="PELDRUN Web & Runtime API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Mount Canonical Routers
app.include_router(chats.router, prefix="/api/chats", tags=["chats"])
app.include_router(run.router, prefix="/api/run", tags=["run"])
app.include_router(status.router, prefix="/api/status", tags=["status"])
app.include_router(config_rtr.router, prefix="/api/config", tags=["config"])
app.include_router(search_rtr.router, prefix="/api/search", tags=["search"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(mcp.router, prefix="/api/mcp", tags=["mcp"])
app.include_router(setup.router, prefix="/api/setup", tags=["setup"])
app.include_router(storage.router, prefix="/api/storage", tags=["storage"])
app.include_router(store_rtr.router, prefix="/api/store", tags=["store"])
app.include_router(
    store_bundles.router,
    prefix="/api/store/tools",
    tags=["store", "bundles"],
)
app.include_router(telemetry.router, prefix="/api/telemetry", tags=["telemetry"])

# Source Control Integration router.
#
# NOTE: The router itself defines its internal prefix as
# `/api/source-control` (see `routers/source_control.py`). Do NOT pass
# a `prefix=` argument here, or the path would be double-prefixed.
app.include_router(source_control.router)

# Mount Storage Filesystem for Deliverables and Downloads
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/storage", StaticFiles(directory=str(STORAGE_ROOT)), name="storage")


@app.get("/api/health")
async def health_check():
    """Universal health ping endpoint."""
    return {"status": "ok", "app": "peldrun-core", "version": "1.0.0"}


@app.get("/")
async def root():
    return {"message": "peldrun Web Backend is running"}
