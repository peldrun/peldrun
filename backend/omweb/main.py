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
    project_knowledge,
    projects,
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
from omweb.memory.api import memory_agentic

# Phase 4: periodic auto-batch scheduler.
#
# Imported at module level (not lazily inside lifespan) because
# ``BatchScheduler`` itself has no runtime dependency on
# ``MemoryService`` -- it only holds a TYPE_CHECKING import, so
# the module loads without touching the memory service tree.
# Importing at the top lets the lifespan hook reference the
# class once, in block 0e below, without an inline import.
from omweb.memory.jobs.batch_scheduler import BatchScheduler

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

    # 0c. Start the Memory Extraction Worker (Phase 1-C).
    #
    # Runs AFTER `initialize_memory_service()` because the worker
    # depends on the extraction pipeline components the service
    # constructed during its own init (outbox, scanner, router,
    # orchestrator). When extraction is disabled in settings -- or
    # the memory provider is not sqlite_fts -- the service never
    # builds those components, so `start_extraction_worker()`
    # returns False and this block is a no-op.
    #
    # Ordering within the block is load-bearing:
    #
    #   1. `recover_extractions()` FIRST. It re-enqueues sessions
    #      that completed during a previous crash but were never
    #      extracted. Doing this BEFORE the worker starts guarantees
    #      that recovered work is queued before the worker begins
    #      its claim loop, so FIFO ordering is preserved across
    #      restarts.
    #
    #   2. `start_extraction_worker()` SECOND. The worker refuses
    #      to start when no real LLM transport was supplied via
    #      `transport_provider` -- this is the Phase 1-C graceful
    #      degradation: jobs may be enqueued and inspected (Batch
    #      UI, tests, diagnostics) but no LLM call is ever
    #      attempted with the default stub transport.
    #
    # Failure handling: any exception here is logged at WARNING
    # and NEVER blocks startup. The memory subsystem degrades to
    # "extraction unavailable" without affecting any other service
    # -- the P1 regression requirement (T1) is that the app must
    # boot cleanly even when extraction is broken.
    try:
        from omweb.memory import get_memory_service

        _mem_svc = get_memory_service()
        if _mem_svc is not None and _mem_svc.extraction_enabled:
            _recovered_jobs = await _mem_svc.recover_extractions()
            if _recovered_jobs:
                logger.info(
                    "Memory extraction recovery: re-enqueued %d "
                    "job(s) for completed sessions with pending "
                    "turns.",
                    _recovered_jobs,
                )
            _worker_started = await _mem_svc.start_extraction_worker()
            if _worker_started:
                logger.info(
                    "MemoryExtractionWorker started; extraction "
                    "will process queued jobs in the background "
                    "using the configured LLM provider."
                )
            else:
                logger.warning(
                    "MemoryExtractionWorker NOT started. This is "
                    "expected ONLY when transport_provider was "
                    "explicitly set to return None (opt-out). In "
                    "all other cases, check the log above for a "
                    "gateway initialization error. Jobs may still "
                    "be enqueued; they will remain pending until "
                    "the worker starts."
                )
        else:
            logger.debug(
                "Memory extraction pipeline disabled; worker "
                "startup skipped."
            )
    except Exception as exc:
        logger.warning(
            "Memory extraction worker startup failed "
            "(non-fatal; extraction unavailable): %s",
            exc,
            exc_info=True,
        )

    # 0d. Start the Embedding Worker (Phase 4).
    #
    # Runs AFTER `start_extraction_worker()` for two reasons:
    #
    #   1. **Schema warm-up.** The extraction pipeline opens the
    #      ``SqliteMemoryRepository`` first, which triggers the
    #      lazy ``apply_schema`` on its first connection. By the
    #      time the embedding pipeline opens its own
    #      ``SqliteMemoryRepository`` instance, the schema
    #      (memories + FTS + memory_embeddings + memory_vectors
    #      tables) is guaranteed to exist. This ordering is not
    #      strictly required -- both repositories apply the
    #      schema lazily and idempotently -- but it produces a
    #      cleaner startup log and eliminates one class of
    #      first-request cold-start weirdness.
    #
    #   2. **Independent failure domains.** If extraction startup
    #      fails, the embedding worker can still start (and vice
    #      versa). Each is wrapped in its own try/except so one
    #      broken subsystem cannot disable the other.
    #
    # The embedding worker is CREATED during
    # ``MemoryService.ainitialize()`` (via ``_initialize_embedding``)
    # ONLY when:
    #
    #   * ``settings.embedding.enabled == True``,
    #   * ``settings.embedding.model_key`` is a non-empty string,
    #   * memory mode permits writes,
    #   * the memory provider is ``sqlite_fts``.
    #
    # When those preconditions are not met, ``embedding_enabled``
    # is ``False`` and this block is a cheap no-op that logs at
    # DEBUG. The typical deployment (embedding not yet enabled)
    # pays zero cost here.
    #
    # Failure handling: any exception is logged at WARNING and
    # NEVER blocks startup. The memory subsystem degrades to
    # "embedding unavailable" without affecting retrieval,
    # extraction, or any other service -- jobs enqueued by
    # ``POST /api/memory/reindex`` will simply remain in
    # ``pending`` status until the operator fixes the
    # configuration and restarts.
    try:
        from omweb.memory import get_memory_service

        _emb_svc = get_memory_service()
        if _emb_svc is not None and _emb_svc.embedding_enabled:
            _emb_started = await _emb_svc.start_embedding_worker()
            if _emb_started:
                logger.info(
                    "EmbeddingWorker started; queued embedding "
                    "jobs (if any) will be processed in the "
                    "background using the configured embedding "
                    "model."
                )
            else:
                logger.warning(
                    "EmbeddingWorker NOT started despite the "
                    "pipeline being initialized. Check the "
                    "preceding log lines for the specific "
                    "failure. Embedding jobs will remain in "
                    "'pending' status until the worker starts."
                )
        else:
            logger.debug(
                "Memory embedding pipeline disabled; worker "
                "startup skipped."
            )
    except Exception as exc:
        logger.warning(
            "Memory embedding worker startup failed "
            "(non-fatal; embedding unavailable): %s",
            exc,
            exc_info=True,
        )

    # 0e. Start the Batch Scheduler (Phase 4).
    #
    # Runs AFTER the extraction worker (0c) and the embedding
    # worker (0d) because it depends on components BOTH of those
    # blocks may have created:
    #
    #   * ``batch_service`` (from the extraction pipeline, block
    #     0c) -- the scheduler calls
    #     ``batch_service.create_batch`` and
    #     ``batch_service.list_batches``. If extraction is
    #     disabled, ``batch_service is None`` and the scheduler
    #     reads that state via ``_read_schedule_hours``,
    #     returning None and idling. That is the CORRECT
    #     degraded behavior -- no crash, no spurious work.
    #
    #   * ``settings.ai.batch_auto_schedule_hours`` -- read from
    #     the current settings on every tick, so a settings
    #     change via PATCH /api/memory/settings takes effect
    #     within 60 seconds WITHOUT a restart. The scheduler
    #     does not cache the value.
    #
    # Scheduling behavior summary:
    #
    #   * ``batch_enabled=False`` -> scheduler idles (60s polls).
    #   * ``batch_auto_schedule_hours=None`` -> scheduler idles
    #     until the operator sets a value.
    #   * ``batch_auto_schedule_hours=N`` -> waits N hours
    #     (interruptible in 60s slices), then creates a batch
    #     IF no other batch is RUNNING or PAUSED.
    #
    # Failure handling: any exception here is logged at WARNING
    # and NEVER blocks startup. ``_batch_scheduler`` is
    # explicitly set to None BEFORE the try so the shutdown
    # block below can always check ``is not None`` without a
    # ``NameError`` on partial-init failure.
    _batch_scheduler: Optional[BatchScheduler] = None
    try:
        _sched_svc = get_memory_service()
        if _sched_svc is not None:
            _batch_scheduler = BatchScheduler(_sched_svc)
            await _batch_scheduler.start()
            logger.info(
                "BatchScheduler started (poll_interval=60s); "
                "auto-batch will run when "
                "`batch_auto_schedule_hours` is set in the AI "
                "settings. This is a no-op until then."
            )
        else:
            logger.debug(
                "BatchScheduler: memory service unavailable; "
                "scheduler not started."
            )
    except Exception as exc:
        logger.warning(
            "Batch scheduler startup failed (non-fatal; "
            "auto-batch unavailable until restart): %s",
            exc,
            exc_info=True,
        )
        _batch_scheduler = None

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

    # 3a. Stop the Embedding Worker (Phase 4) BEFORE the extraction
    # worker below.
    #
    # Same rationale as block 3b: the worker's transition methods
    # (``complete`` / ``fail`` / ``retry``) all require its outbox
    # SQLite connection to remain open. Stopping the worker first
    # drains any in-flight job cleanly (up to 30s), then the outbox
    # can be closed by ``shutdown_memory_service()`` below without
    # race conditions.
    #
    # Embedding stops before extraction only for symmetry with the
    # startup ordering (extraction -> embedding). The two pipelines
    # are independent -- they use separate SQLite tables
    # (``memory_jobs`` vs ``memory_embedding_jobs``) and separate
    # repository instances -- so either shutdown order would be
    # correct. The symmetry makes the lifespan log easier to reason
    # about at a glance.
    #
    # Both ``stop_embedding_worker`` and this wrapper are
    # idempotent and never raise -- a shutdown failure here must
    # not prevent the memory service (and the provider it holds)
    # from closing. ``MemoryService.aclose()`` will run its own
    # cleanup pass (``_close_embedding``) afterwards as defense in
    # depth; the second pass sees the worker already stopped and
    # is a no-op.
    try:
        from omweb.memory import get_memory_service

        _emb_svc = get_memory_service()
        if _emb_svc is not None:
            await _emb_svc.stop_embedding_worker(timeout_seconds=30)
    except Exception as exc:
        logger.warning(
            "Memory embedding worker stop failed on shutdown "
            "(non-fatal): %s",
            exc,
        )

    # 3-prior. [PHASE 4] Stop the Auto-Batch Scheduler BEFORE the
    # extraction worker (block 3b) and before the memory service
    # shutdown (block 4).
    #
    # Ordering rationale:
    #
    #   * The scheduler's ``_run_loop`` calls
    #     ``batch_service.create_batch`` and
    #     ``batch_service.list_batches``, both of which touch the
    #     extraction outbox. Stopping the scheduler FIRST
    #     guarantees no in-flight tick races the extraction
    #     worker's shutdown (block 3b, which calls
    #     ``stop_extraction_worker``) or the outbox close (block
    #     4, inside ``shutdown_memory_service``).
    #
    #   * ``BatchScheduler.stop()`` has a 10s timeout -- generous
    #     for a scheduler whose only blocking operation is a
    #     DB write that completes in single-digit milliseconds.
    #     The 10s cap exists to bound the worst case where
    #     ``create_batch`` hits a wedged SQLite transaction; the
    #     scheduler's own ``stop()`` cancels the run task after
    #     the timeout, so shutdown cannot hang indefinitely.
    #
    #   * ``_batch_scheduler`` was initialized to ``None`` at the
    #     top of block 0e, so ``is not None`` is safe even when
    #     startup failed partway through. The outer try/except
    #     catches any exception from ``stop()`` -- a broken
    #     scheduler shutdown must not prevent the extraction
    #     worker or the memory service from shutting down.
    if _batch_scheduler is not None:
        try:
            await _batch_scheduler.stop(timeout_seconds=10)
            logger.info("BatchScheduler stopped cleanly.")
        except Exception as exc:
            logger.warning(
                "Batch scheduler stop failed (non-fatal): %s",
                exc,
            )
        _batch_scheduler = None

    # 3b. [PHASE 1-C] Stop the Memory Extraction Worker BEFORE the
    # memory service shutdown below.
    #
    # Ordering criticality:
    #
    #   1. `stop_extraction_worker()` waits for the current job to
    #      finish (up to 30s). During that wait the worker calls
    #      ``outbox.complete()`` or ``outbox.fail()`` -- BOTH
    #      require the outbox SQLite connection to still be open.
    #
    #   2. `shutdown_memory_service()` (block 4 below) closes the
    #      outbox. If we reversed the order, a running worker would
    #      hit "outbox is not open" on every heartbeat and, worse,
    #      any in-flight complete() would raise -- the job would
    #      stay in `running` until its lease expired, and the
    #      cursor would never advance for that batch of turns.
    #
    # Both the worker stop and this wrapper are idempotent and
    # never raise -- a shutdown failure here must not prevent the
    # memory service (and the provider it holds) from closing.
    try:
        from omweb.memory import get_memory_service

        _mem_svc = get_memory_service()
        if _mem_svc is not None:
            await _mem_svc.stop_extraction_worker(timeout_seconds=30)
    except Exception as exc:
        logger.warning(
            "Memory extraction worker stop failed on shutdown "
            "(non-fatal): %s",
            exc,
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
# Phase 6 agentic search. Registered BEFORE ``memory_crud`` so the
# two-segment ``/search/agentic`` route is matched without ever
# reaching the CRUD router's ``/{memory_id}`` catch-all. Separate
# module and separate endpoint because an agentic run costs several
# LLM calls -- it must not share a URL with the UI's cheap preview
# search (see ``memory_agentic.py``'s module docstring).
app.include_router(
    memory_agentic.router,
    prefix="/api/memory",
    tags=["memory", "agentic"],
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

# Projects API router.
#
# NOTE: The router defines its internal prefix as `/api/projects`
# (see `routers/projects.py`). Do NOT pass a `prefix=` argument here,
# or the path would be double-prefixed. This matches the source_control
# and resource_monitor router convention.
app.include_router(projects.router)

# Project Knowledge Base router.
#
# NOTE: Like ``projects.router`` above, this router declares its own
# prefix internally (``/api/projects/{project_id}/knowledge``). Do NOT
# pass a ``prefix=`` argument here, or the path would be double-
# prefixed. Registration order does not matter between these two
# routers because their path patterns do not overlap: FastAPI matches
# ``/api/projects/{project_id}/knowledge/files`` before it would ever
# try to interpret ``/knowledge`` as a project_id under the projects
# router, since ``projects.router`` has no catch-all ``/{project_id}``
# route with a trailing ``/knowledge`` subpath.
app.include_router(project_knowledge.router)

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
