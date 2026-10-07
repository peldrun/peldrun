"""
backend/omweb/main.py

PELDRUN Web FastAPI Application Entrypoint.

Initializes middleware, static files, and canonical REST routers:
- /api/chats
- /api/run
- /api/status
- /api/config
- /api/files
- /api/mcp
- /api/setup
- /api/storage
- /api/store
- /api/telemetry (P1-03D Canonical Analytics & Metering APIs)
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
    setup,
    status,
    storage,
    store_rtr,
    telemetry,
)

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
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(mcp.router, prefix="/api/mcp", tags=["mcp"])
app.include_router(setup.router, prefix="/api/setup", tags=["setup"])
app.include_router(storage.router, prefix="/api/storage", tags=["storage"])
app.include_router(store_rtr.router, prefix="/api/store", tags=["store"])
app.include_router(telemetry.router, prefix="/api/telemetry", tags=["telemetry"])

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
