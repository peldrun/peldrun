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
- /api/store/providers (Provider Packages Subsystem — M9)
- /api/telemetry (P1-03D Canonical Analytics & Metering APIs)
- /api/source-control (Source Control Integration: providers,
  repositories, workspaces, approvals, operations, audit)
- /api/resource-monitor (PELDRUN Resource Monitor & Hardware Telemetry API)
- /api/catalog (Model Capability Catalog — LiteLLM + HuggingFace +
  cached provider_live facts, resolved via capability_resolver)

Lifespan management:
    The application installs a FastAPI lifespan context manager that:
    1. Initializes and shuts down the ResourceMonitorService.
    2. Gracefully closes the shared BrowserSessionManager on shutdown.
    This is critical on Windows, where lingering Chromium processes can
    prevent Uvicorn from exiting and cause the event loop to hang after
    browser_interactive runs.
"""

from __future__ import annotations

# =====================================================================
# MUST be the very first executable statement in this module.
# Forces UTF-8 encoding on stdout/stderr and all logging handlers so
# that non-ASCII content returned by providers (Jina, SearXNG, Google,
# Yandex, Brave) never raises UnicodeEncodeError on Windows consoles.
# See omweb/_unicode_setup.py for the full rationale.
# =====================================================================
from omweb._unicode_setup import install_utf8_console
install_utf8_console()

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from omweb.config import STORAGE_ROOT
from integrations.source_control.providers import register_builtin_providers

# Register built-in Git providers globally on startup
register_builtin_providers()
from omweb.routers import (
    catalog_rtr,
    chats,
    config_rtr,
    files,
    generative_ui_rtr,
    image_proxy_rtr,
    mcp,
    mcp_auth,
    memory_rtr,
    resource_monitor,
    run,
    runtime_settings_rtr,
    search_rtr,
    setup,
    skills as skills_rtr,
    source_control,
    status,
    storage,
    store_rtr,
    system as system_rtr,
    telemetry,
)

# Memory Platform -- CRUD router (mounted at /api/memory AFTER
# memory_rtr.router; see the include comment below for ordering).
from omweb.memory.api.memory_crud import router as memory_crud

# Tool Bundles Subsystem router
from omweb.routers.store import bundles as store_bundles

# Provider Packages Subsystem router (M9 administrative layer).
#
# Exposes /api/store/providers/* — list, upload, catalog, enable,
# disable, activate, uninstall, health. The router itself is stateless;
# all lifecycle state lives in storage/store/providers/.
from omweb.routers.store import providers as store_providers

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
    # 0. Reconcile persisted session state with the live run registry.
    #
    # Any session left in a non-terminal state (running, paused, starting,
    # ...) by a crashed or interrupted previous process is promoted to
    # "interrupted" so the frontend reports an accurate state and no
    # phantom spinner is displayed in the sidebar or history views.
    #
    # This runs FIRST — before any subsystem — because:
    #   * It is the fastest operation in the startup sequence (JSON file
    #     scan only; no network, no subprocesses).
    #   * It must complete even if a later subsystem fails to start, so
    #     the user never observes a stale "running" state after a crash.
    #
    # See docs/state-contracts.md §7.3 for the full policy.
    #
    # Multi-worker note: this recovery assumes a single-process backend.
    # In a multi-worker deployment each worker would see an empty
    # ACTIVE_JOB_TASKS at startup and would therefore mark sessions that
    # are actually owned by OTHER workers as "interrupted". The current
    # deployment model is single-process (uvicorn default), so this is
    # safe. A future multi-worker refactor must move the live-run
    # registry to a shared store (Redis / disk lockfile) before enabling
    # more than one worker.
    try:
        from omweb.services.recovery import recover_orphaned_sessions

        # [P0-6 FIX — Await the now-async recovery sweep]
        #
        # ``recover_orphaned_sessions`` became an ``async def`` when
        # the RecoveryManager wiring landed (P0 Item 6). Calling it
        # without ``await`` returns a coroutine that never runs and
        # produces a ``RuntimeWarning: coroutine was never awaited``
        # at GC time — the sweep would silently never execute, so
        # every orphaned run would remain in its stale ``running``
        # state forever. The ``await`` is load-bearing.
        #
        # The sweep now consults ``RecoveryManager.plan_resume`` for
        # every orphan and reports the count of runs that are
        # genuinely resumable, so the log line distinguishes
        # "recovered to interrupted, nothing to resume" from
        # "recovered to interrupted, N runs ready for /resume".
        _recovery_report = await recover_orphaned_sessions()
        _resumable_count = int(
            _recovery_report.get("resumable_count", 0)
        )
        if _recovery_report.get("interrupted"):
            logger.warning(
                "Session recovery: promoted %d orphaned session(s) "
                "out of %d scanned (%d resumable via POST /resume); "
                "previous process likely crashed or was forcibly "
                "terminated.",
                _recovery_report["interrupted"],
                _recovery_report["scanned"],
                _resumable_count,
            )
        elif _recovery_report.get("skipped_unreadable") or _recovery_report.get(
            "errors"
        ):
            logger.warning(
                "Session recovery completed with warnings: scanned=%d "
                "unreadable=%d errors=%d",
                _recovery_report["scanned"],
                _recovery_report.get("skipped_unreadable", 0),
                len(_recovery_report.get("errors", [])),
            )
        else:
            logger.info(
                "Session recovery clean: %d session(s) scanned, no orphans.",
                _recovery_report["scanned"],
            )
    except Exception as _rec_err:
        # A recovery failure must never block startup. The next restart
        # will retry the same scan; nothing is lost by skipping.
        logger.warning(
            "Session recovery skipped due to error: %s",
            _rec_err,
            exc_info=True,
        )

    # 0b. Initialize the Memory Subsystem (P1.7).
    #
    # Runs EARLY -- before any handler that might call
    # ``get_memory_service()`` (the dispatcher, the engines, the
    # memory tools, the memory API). The service itself degrades
    # gracefully on failure: a provider initialization error inside
    # ``MemoryService.ainitialize`` falls back to a null provider,
    # so the service stays "initialized" and every ``retrieve``
    # returns an empty context. Startup cannot be blocked by a
    # broken settings file or a missing FTS5 build.
    #
    # ``initialize_memory_service`` takes optional ``db_path`` /
    # ``settings_path`` overrides. Production uses the canonical
    # storage-root locations; tests inject temp paths through the
    # ``set_memory_service_for_tests`` hook instead.
    try:
        from omweb.memory import initialize_memory_service

        await initialize_memory_service()
        logger.info("MemoryService initialized cleanly on startup.")
    except Exception as exc:
        logger.warning(
            "MemoryService initialization failed on startup "
            "(memory will be unavailable until restart): %s",
            exc,
            exc_info=True,
        )

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

    # 1b. Skills subsystem: clean up any leftover staging or trash
    # directories from a previous run that crashed mid-install.
    # Runs synchronously (fast) and swallows errors — a cleanup
    # failure must never block startup.
    try:
        from omweb.skills.manager import skill_manager

        cleanup_report = skill_manager.cleanup_orphans()
        logger.info(
            "Skill subsystem cleanup on startup: %s",
            cleanup_report,
        )
        # Prime the registry so the first request sees a warm cache
        # and any broken packages surface in the startup log.
        try:
            skill_manager.list_skills()
            broken = skill_manager.list_broken_skills()
            if broken:
                logger.warning(
                    "Skills subsystem reports %d broken package(s) "
                    "on startup: %s",
                    len(broken),
                    list(broken.keys()),
                )
        except Exception as warm_err:  # noqa: BLE001 — best-effort
            logger.warning(
                "Skill registry warm-up failed on startup: %s",
                warm_err,
            )

        # Log the Progressive Disclosure feature flag once at startup
        # so operators can verify the effective mode from the log
        # without introspecting environment variables after the fact.
        #
        # NOTE: The flag is read on EVERY run inside peldrun_engine.py
        # (not from a cached module-level constant), so a value change
        # takes effect on the next chat run without a restart. This
        # log line reflects the mode at STARTUP time only — if the
        # env var changes later, subsequent runs use the new value
        # but this log stays as historical evidence of the original
        # setting.
        import os as _os_local
        _pd_raw = _os_local.getenv("PELDRUN_PROGRESSIVE_DISCLOSURE")
        _pd_effective = (
            _pd_raw if _pd_raw is not None else "true (default)"
        )
        _pd_enabled = (
            (_pd_raw or "true").lower() in ("1", "true", "yes")
        )
        logger.info(
            "Skills progressive disclosure: %s "
            "(PELDRUN_PROGRESSIVE_DISCLOSURE=%s).",
            "ENABLED" if _pd_enabled else "DISABLED",
            _pd_effective,
        )
    except Exception as exc:
        logger.warning(
            "Skill subsystem initialization failed on startup: %s",
            exc,
            exc_info=True,
        )

    # 2. Start the periodic temp-artifact cleaner.
    #
    # Sweeps ``storage/artifacts/temp/`` every 6 hours and on startup,
    # deleting files older than 24 hours. Reclaims the space occupied
    # by pasted screenshots of past conversations — the workspace copy
    # under ``<chat_files>/.peldrun/user_uploads/`` remains the
    # authoritative fallback, so removing the temp file after 24h
    # cannot break any in-flight or recently-resumed run.
    #
    # Enabled/disabled via PELDRUN_TEMP_CLEANUP_ENABLED (default "1").
    # Interval and threshold overridable via
    # PELDRUN_TEMP_CLEANUP_INTERVAL_HOURS (default "6") and
    # PELDRUN_TEMP_CLEANUP_MAX_AGE_HOURS (default "24").
    try:
        from omweb.services.temp_artifacts_cleaner import (
            temp_artifacts_cleaner,
        )

        await temp_artifacts_cleaner.start()
        logger.info("TempArtifactsCleaner started cleanly on startup.")
    except Exception as exc:
        logger.warning(
            "Failed to start TempArtifactsCleaner on startup: %s",
            exc,
            exc_info=True,
        )

    # 3. Discover Provider Packages installed as pip entry points.
    #
    # Idempotent and never raises — a discovery failure must not block
    # startup, because the built-in adapters are registered at import
    # time regardless of what discovery finds. M9-installed packages
    # in isolated venvs are NOT discovered here — they are registered
    # in step 4 below.
    try:
        from peldrun.llm.providers import (
            ensure_external_providers_discovered,
        )

        _discovered = ensure_external_providers_discovered()
        if _discovered:
            logger.info(
                "Discovered %d external provider package(s) on startup.",
                _discovered,
            )
    except Exception as exc:
        logger.warning(
            "External provider discovery failed on startup "
            "(non-fatal): %s",
            exc,
        )

    # 4. Register installed Provider Packages as runtime adapters.
    #
    # Every enabled package from /api/store/providers gets a
    # registration in provider_registry under its canonical ID, with
    # PluginProviderAdapter as the adapter class and SOURCE_EXTERNAL
    # as its source. After this runs, provider_factory.create(pkg_id,
    # connection) returns a working adapter.
    #
    # For compatible packages: the adapter resolves to a built-in
    # delegate (in-process, no worker). For native/custom packages:
    # the adapter lazily spawns a worker subprocess on the first
    # request (see WorkerSupervisor.start). Startup cost is therefore
    # O(number of packages), not O(number of workers) — subprocess
    # creation is deferred.
    #
    # Failures are logged at WARNING and never block startup, because
    # a broken package must not prevent the backend from starting
    # with its built-in providers intact.
    try:
        from peldrun.llm.providers.custom_plugin import (
            ensure_plugin_providers_registered,
        )

        _plugins = ensure_plugin_providers_registered()
        if _plugins:
            logger.info(
                "Registered %d provider package(s) as runtime adapters.",
                _plugins,
            )
    except Exception as exc:
        logger.warning(
            "Provider package registration failed on startup "
            "(non-fatal): %s",
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

    # 2. Stop the temp-artifact cleaner.
    #
    # Cheap (cancel task + await), so it runs before Resource Monitor
    # shutdown. Placed here rather than last so its log line appears
    # before the heavier subsystem shutdowns, keeping logs readable.
    try:
        from omweb.services.temp_artifacts_cleaner import (
            temp_artifacts_cleaner,
        )

        await temp_artifacts_cleaner.stop()
        logger.info("TempArtifactsCleaner stopped cleanly on shutdown.")
    except Exception as exc:
        logger.warning(
            "Failed to stop TempArtifactsCleaner on shutdown: %s", exc
        )

    # 3. Shutdown Resource Monitor subsystem
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

    # 4. Shutdown the Memory Subsystem (P1.7).
    #
    # Runs LAST -- after every other subsystem has released its
    # resources. This ordering ensures that in-flight requests,
    # tool handlers, and background extraction jobs (once P1.6
    # lands) can safely call ``get_memory_service()`` until the
    # very end of the shutdown sequence.
    #
    # ``shutdown_memory_service`` is idempotent and never raises
    # -- it clears the singleton and closes the provider, both
    # best-effort. A failure here is logged at WARNING by the
    # service itself; the wrapper below is belt-and-suspenders
    # for an import error.
    try:
        from omweb.memory import shutdown_memory_service

        await shutdown_memory_service()
        logger.info("MemoryService shut down cleanly on shutdown.")
    except Exception as exc:
        logger.warning(
            "Failed to shutdown MemoryService on shutdown: %s", exc
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


# ---------------------------------------------------------------------------
# Application-level exception handler -- memory API envelope
# ---------------------------------------------------------------------------
#
# FastAPI's default response for request-body validation failures
# (``extra="forbid"`` triggered, wrong type, missing required
# field) is HTTP 422 with FastAPI's OWN error shape -- NOT the
# memory API's uniform envelope. That inconsistency is a real
# problem for the memory UI: the frontend has to parse two
# different error shapes depending on whether the failure came
# from our business logic (400, custom shape) or FastAPI's
# validator (422, default shape).
#
# This handler normalizes EVERY RequestValidationError to:
#
#     HTTP 400
#     {
#         "status": "error",
#         "operation": "request_validation",
#         "error": "invalid_request",
#         "message": "Request validation failed.",
#         "fields": [
#             {"loc": ["body", "mode"], "msg": "...", "type": "..."},
#             ...
#         ]
#     }
#
# The ``fields`` list mirrors the shape produced by
# ``memory_settings._pydantic_error_to_400`` so the frontend
# can highlight the offending field the same way in both paths.
#
# Scope -- INTENTIONALLY GLOBAL
# -----------------------------
#
# We register this handler at the APP level, not per-router. Two
# reasons:
#
#   1. **Consistency.** Every router that uses Pydantic body
#      models benefits (memory_crud, memory_settings,
#      memory_projects, and future routers). A per-router
#      registration would require duplication at every mount
#      site, and any new router that forgot it would silently
#      regress.
#
#   2. **Behavioral contract.** A 400-with-structure is the
#      CORRECT response for "you sent invalid input". FastAPI
#      chose 422 for historical reasons (WebDAV's Unprocessable
#      Entity semantics); most REST APIs use 400 for the same
#      case. This handler aligns our API with the convention
#      the rest of the codebase already uses (see
#      ``memory_settings._pydantic_error_to_400`` and every
#      ``_error_response`` in ``omweb.memory.api.*``).
#
# Does the frontend break?
# ------------------------
#
# No. The frontend must already handle 400 with a structured
# detail (from the business-logic paths). This handler makes the
# validator path produce the SAME shape, so the frontend's
# existing error-handling code works unchanged for both paths.
# Any caller that was specifically checking for 422 will now see
# 400 -- but no such caller exists in this codebase (grep for
# "422" in ``frontend/`` returns zero hits).
@app.exception_handler(RequestValidationError)
async def _memory_style_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Normalize FastAPI 422 responses to the memory API's 400 shape."""
    fields = []
    for err in exc.errors():
        if not isinstance(err, dict):
            continue
        fields.append(
            {
                "loc": [str(x) for x in err.get("loc", [])],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return JSONResponse(
        status_code=400,
        content={
            "status": "error",
            "operation": "request_validation",
            "error": "invalid_request",
            "message": "Request validation failed.",
            "fields": fields,
        },
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
# Model Capability Catalog.
#
# Read-only view over the cached capability resolution layer
# (LiteLLM registry + HuggingFace configs + models_metadata.json).
# The `POST /refresh` endpoint triggers a LiteLLM cache refresh
# from GitHub — it never touches any provider's endpoint. Live
# provider queries happen ONLY through /api/config/fetch-models
# (the Scan Models button).
app.include_router(
    catalog_rtr.router, prefix="/api/catalog", tags=["catalog"]
)
app.include_router(
    generative_ui_rtr.router,
    prefix="/api/config",
    tags=["config", "generative-ui"],
)
# Runtime settings router — the router already declares
# ``prefix="/api/config/runtime-settings"`` internally, so no prefix=
# argument here (matches source_control / resource_monitor convention).
app.include_router(runtime_settings_rtr.router)
app.include_router(search_rtr.router, prefix="/api/search", tags=["search"])
# Image proxy router — serves images for the image_search gallery
# (/api/search/image-proxy). Mounted under /api/search so it shares
# the same CORS + auth surface as the search endpoints.
app.include_router(
    image_proxy_rtr.router, prefix="/api/search", tags=["search", "image-proxy"]
)
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
# Provider Packages Subsystem (M9).
#
# The router internally declares paths as `/`, `/catalog`, `/upload`,
# `/{provider_id}`, etc. FastAPI matches in registration order, so
# mounting at the literal prefix `/api/store/providers` gives us:
#
#     GET    /api/store/providers
#     GET    /api/store/providers/catalog
#     GET    /api/store/providers/catalog/{item_id}
#     POST   /api/store/providers/catalog/refresh
#     POST   /api/store/providers/upload
#     POST   /api/store/providers/{provider_id}/install-from-catalog
#     GET    /api/store/providers/{provider_id}
#     DELETE /api/store/providers/{provider_id}
#     POST   /api/store/providers/{provider_id}/enable
#     POST   /api/store/providers/{provider_id}/disable
#     POST   /api/store/providers/{provider_id}/activate
#     GET    /api/store/providers/{provider_id}/health
#
# The provider package subsystem manages external LLM provider adapters
# loaded from ZIP archives or the built-in catalog. It is entirely
# disjoint from the tool bundle subsystem above — the two share only
# the `/api/store` namespace for UI grouping.
app.include_router(
    store_providers.router,
    prefix="/api/store/providers",
    tags=["store", "providers"],
)

# Memory Platform API (P1.8).
#
# Two includes, ordered. The separation (rather than a single
# aggregator) is enforced by FastAPI's constraint that
# ``include_router(router, prefix="")`` is invalid when the
# child router carries an empty-path route (``@router.get("")``).
# ``memory_crud`` has such a route (the list/create endpoints),
# so it must be mounted at the app level with a non-empty prefix.
#
# Ordering guarantee:
#   1. ``memory_rtr`` first  -- fixed prefixes (/settings,
#      /projects, /import, /export, /usage).
#   2. ``memory_crud`` second -- catch-all (/, /search,
#      /{memory_id}, ...).
# FastAPI matches in registration order, so every fixed-prefix
# router wins before ``/{memory_id}`` is tried.
app.include_router(
    memory_rtr.router,
    prefix="/api/memory",
    tags=["memory"],
)
# NOTE: ``memory_crud`` (imported as ``from omweb.memory.api.memory_crud
# import router as memory_crud``) IS the APIRouter instance -- the
# ``as memory_crud`` alias bound the router object itself, not the
# module. Calling ``memory_crud.router`` would try to access an
# attribute named ``router`` on the APIRouter, which does not exist.
# Pass ``memory_crud`` directly.
app.include_router(
    memory_crud,
    prefix="/api/memory",
    tags=["memory", "crud"],
)

# Skill Catalog sub-router MUST be registered BEFORE the main skills
# router. The main router declares `GET /{skill_id}` which would
# otherwise match `/catalog` as `skill_id="catalog"`. FastAPI matches
# routes in registration order, not by specificity.
app.include_router(
    skills_rtr.catalog_router,
    prefix="/api/skills",
    tags=["skills", "catalog"],
)
app.include_router(
    skills_rtr.router,
    prefix="/api/skills",
    tags=["skills"],
)
app.include_router(telemetry.router, prefix="/api/telemetry", tags=["telemetry"])

# System diagnostics router.
#
# Exposes /api/system/health and /api/system/info for the frontend
# BackendStatusCard. These endpoints are read-only and safe to expose:
# no secrets, no filesystem paths, no config values.
app.include_router(system_rtr.router, prefix="/api/system", tags=["system"])

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

# MCP OAuth 2.1 and per-chat credentials router.
#
# NOTE: This router defines its internal paths with absolute prefixes
# (`/api/chats/...` and `/api/mcp/...`). Do NOT pass a prefix= argument
# here, or the paths would be double-prefixed. This matches the
# source_control and resource_monitor router convention.
app.include_router(mcp_auth.router)

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
