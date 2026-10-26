# backend/omweb/main.py
"""
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
- /api/resource-monitor (PELDRUN Resource Monitor & Hardware Telemetry API)

Lifespan management:
    The application installs a FastAPI lifespan context manager that:
    1. Initializes and shuts down the ResourceMonitorService.
    2. Gracefully closes the shared BrowserSessionManager on shutdown.
    This is critical on Windows, where lingering Chromium processes can
    prevent Uvicorn from exiting and cause the event loop to hang after
    browser_interactive runs.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from omweb.config import STORAGE_ROOT
from integrations.source_control.providers import register_builtin_providers

# Register built-in Git providers globally on startup
register_builtin_providers()
from omweb.routers import (
    chats,
    config_rtr,
    files,
    mcp,
    resource_monitor,
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


# ---------------------------------------------------------------------------
# Lifespan: graceful shutdown of shared subsystems
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context.

    On startup:
        Initializes database migrations, environment discovery snapshot,
        and starts the adaptive sampling scheduler for Resource Monitor.

    On shutdown (ordered so orphaned subprocesses cannot block the event loop):
        1. Terminate active MCP subprocesses (Node / Python children).
        2. Conclude the Resource Monitor session and flush sample buffers.
        3. Gracefully terminate the shared BrowserSessionManager.
    """
    # 1. Start Resource Monitor subsystem
    try:
        from omweb.services.resource_monitor_service import (
            get_resource_monitor_service,
        )

        monitor_service = get_resource_monitor_service()
        await monitor_service.start(app_version="1.0.0")
        logger.info("ResourceMonitorService started cleanly on startup.")
    except Exception as exc:
        logger.error(
            "Failed to start ResourceMonitorService on startup: %s",
            exc,
            exc_info=True,
        )

    yield

    # 1. Terminate active MCP subprocesses BEFORE anything else.
    # On Windows, lingering Node/Python child processes hold file handles
    # on the backend's working directory, preventing uvicorn from releasing
    # the event loop cleanly. Shutting them down first avoids the
    # "orphaned subprocess keeps the app alive" failure mode.
    # A hard timeout protects against a wedged subprocess that would
    # otherwise stall shutdown indefinitely.
    try:
        from omweb.extensions.mcp_pool import mcp_session_pool

        await asyncio.wait_for(
            mcp_session_pool.close_all(),
            timeout=30.0,
        )
        logger.info("McpSessionPool closed cleanly on shutdown.")
    except asyncio.TimeoutError:
        logger.warning(
            "McpSessionPool.close_all() exceeded 30s during shutdown; "
            "any surviving subprocesses will be reaped by the OS."
        )
    except Exception as exc:
        logger.warning(
            "Failed to close McpSessionPool on shutdown: %s", exc
        )

    # 2. Shutdown Resource Monitor subsystem
    try:
        from omweb.services.resource_monitor_service import (
            get_resource_monitor_service,
        )

        await get_resource_monitor_service().shutdown()
        logger.info("ResourceMonitorService shut down cleanly.")
    except Exception as exc:
        logger.warning(
            "Failed to shutdown ResourceMonitorService on shutdown: %s", exc
        )

    # 3. Close BrowserSessionManager
    try:
        from peldrun.tools.builtins.browser_interactive.session import (
            get_session_manager,
        )

        await get_session_manager().close()
        logger.info("BrowserSessionManager closed cleanly on shutdown.")
    except Exception as exc:
        logger.warning(
            "Failed to close BrowserSessionManager on shutdown: %s", exc
        )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PELDRUN Web & Runtime API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
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

# Resource Monitor & Hardware Telemetry router.
#
# NOTE: The router defines its internal prefix as `/api/resource-monitor`
# (see `routers/resource_monitor.py`). Do NOT pass a `prefix=` argument here.
app.include_router(resource_monitor.router)

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
