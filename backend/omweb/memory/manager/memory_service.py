# backend/omweb/memory/manager/memory_service.py
"""
PELDRUN Memory Platform - Memory Service Facade.

The single entry point every consumer of the memory subsystem
talks to (P1.7 task 1.7.3). Wraps the lifecycle manager and the
P1.4 retrieval pipeline in a façade with:

  * a clean public API (``retrieve``, ``reload_settings``),
  * mode enforcement (DISABLED -> always empty result),
  * read-only properties that expose the underlying provider and
    settings to trusted consumers (memory_save/memory_search tools),
  * a ``_record_usage`` stub the P1.9 monitoring subsystem will
    replace with the real ``MemoryUsageTracker``.

Design invariants
-----------------

1. **``retrieve`` always returns a ``MemoryContext``.**
   Never ``None``. Never raises for expected failure modes
   (disabled mode, empty query, no scopes, provider unavailable).
   Downstream consumers treat "empty context" and "no memories"
   identically -- the difference is only in the diagnostic
   fields (``router_decision``, ``dropped_for_budget``).

   The consequence for the dispatcher is a smaller try/except:
   the only exception ``retrieve`` can raise is a programmer
   error (``RuntimeError`` from accessing an uninitialized
   service), and that path should NEVER occur in production
   because FastAPI's lifespan initializes the service before
   serving requests.

2. **Write operations are deferred to P1.6.**
   The ``memory_save`` tool writes DIRECTLY to ``provider`` +
   ``settings`` -- it does not go through ``MemoryService``.
   This is deliberate: ``WritePipeline`` (the SKIP / SUPERSEDE /
   REACTIVATE decision layer, P1.7 task 1.7.3 / P1.6) has not
   landed yet, so a ``save()`` method here would be either
   incomplete or a duplicate of the eventual pipeline. Tools
   use the already-validated ``decide_write_action`` + provider
   primitives, which P1.6 will wrap without changing the tool's
   external contract.

3. **``_record_usage`` is a stub today, a real tracker tomorrow.**
   P1.9 introduces ``MemoryUsageTracker`` and a persistent
   ``memory_usage_ledger``. Until then, ``_record_usage`` logs
   the usage at INFO level so operators can see per-run token
   spend in the server log. P1.9 will replace the body without
   changing the method signature or any call site.

4. **Singleton enforcement lives in ``omweb.memory.__init__``.**
   This file exposes the CLASS. Process-wide singleton access
   (``get_memory_service()``, ``initialize_memory_service()``,
   ``shutdown_memory_service()``) lives in the package
   ``__init__.py`` so that tests can construct their own
   ``MemoryService`` instances with isolated settings + DB
   paths without touching the global.

5. **No FastAPI imports.**
   The service is a plain async class. Any lifespan wiring lives
   in ``omweb/main.py`` (deferred to the P1.7 lifespan task).
   This keeps the service unit-testable in isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from peldrun.memory.domain import (
    MemoryContext,
    MemoryMode,
    MemoryProvider,
    MemorySettings,
    default_settings,
)
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.services import (
    build_empty_context,
    build_memory_context,
    classify_route,
)

from omweb.memory.manager.memory_lifecycle import (
    MemoryLifecycleManager,
)
from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)


logger = logging.getLogger("omweb.memory.manager.service")


class MemoryService:
    """Façade for the memory subsystem.

    See the module docstring for the design rationale, the write
    deferral decision, and the ``_record_usage`` stub plan.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        settings_path: Optional[Path] = None,
    ) -> None:
        """Construct an uninitialized ``MemoryService``.

        The service is NOT usable until ``ainitialize()`` completes.
        Properties that need an open provider raise
        ``RuntimeError`` before then -- see the lifecycle manager's
        module docstring for the rationale on fail-fast access.

        Parameters
        ----------
        db_path
            Override for the SQLite database file. ``None`` uses
            ``<storage_root>/memory.db``. Tests pass a temp path.
        settings_path
            Override for the settings JSON file. ``None`` uses
            ``<storage_root>/memory_settings.json``. Tests pass a
            temp path.
        """
        self._settings_manager = MemorySettingsManager(path=settings_path)
        self._lifecycle = MemoryLifecycleManager(
            settings_manager=self._settings_manager,
            db_path=db_path,
        )
        self._initialized = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ainitialize(self) -> None:
        """Initialize the underlying lifecycle manager and provider.

        Idempotent. On DISABLED mode, uses ``NullMemoryProvider`` and
        never touches the DB file (see ``MemoryLifecycleManager``).
        Failures during provider open degrade gracefully to a null
        provider -- the service stays "available" (initialized) but
        every ``retrieve`` returns an empty context.
        """
        if self._initialized:
            return
        await self._lifecycle.ainitialize()
        self._initialized = True
        logger.info(
            "MemoryService initialized (mode=%s, provider=%s).",
            self._lifecycle.settings.mode.value,
            self._lifecycle.settings.provider_id,
        )

    async def aclose(self) -> None:
        """Close the provider and reset state. Idempotent.

        Safe to call before ``ainitialize()`` -- FastAPI's lifespan
        hook runs this from a ``finally`` block that may execute
        after a failed startup.
        """
        if not self._initialized and not self._lifecycle.is_ready:
            return
        await self._lifecycle.aclose()
        self._initialized = False
        logger.info("MemoryService closed.")

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True after ``ainitialize()`` until ``aclose()``.

        Being "available" does NOT guarantee a working provider --
        in degraded states (provider failed to open, mode disabled)
        the service is available but ``retrieve`` returns empty
        contexts. Health checks that need to distinguish these
        cases inspect ``mode`` and ``provider`` directly.
        """
        return self._initialized and self._lifecycle.is_ready

    @property
    def mode(self) -> MemoryMode:
        """Current effective mode. Raises before ``ainitialize()``."""
        return self._lifecycle.settings.mode

    @property
    def settings(self) -> MemorySettings:
        """Full settings snapshot. Raises before ``ainitialize()``."""
        return self._lifecycle.settings

    @property
    def provider(self) -> MemoryProvider:
        """The live provider. Raises before ``ainitialize()``.

        Tools (``memory_save``, ``memory_search``) need both
        ``provider`` and ``settings``. In DISABLED mode ``provider``
        is a ``NullMemoryProvider`` -- every write raises
        ``MemoryUnavailableError`` and every read returns empty,
        which is exactly the semantics DISABLED mode promises.
        """
        return self._lifecycle.provider

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        query: str,
        chat_id: Optional[str],
        project_id: Optional[str],
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> MemoryContext:
        """Retrieve memory context for a turn.

        This is THE retrieval entry point. Every consumer of the
        memory subsystem -- ``dispatcher.run_instrumented``,
        ``dispatcher.run_direct_chat``, the ``memory_search`` tool,
        future API endpoints -- goes through this method.

        Parameters
        ----------
        query
            The user's prompt (or the tool's search query). May be
            empty; the pipeline falls back to a recency listing in
            that case.
        chat_id
            Current chat id, if any. Used to build chat-scope refs.
        project_id
            Current project id, if any. Used for project-scope refs
            and to gate the PRECISE router path.
        user_id
            Optional explicit user id. Defaults to ``"user:default"``
            in single-user deployments.
        run_id
            Optional run id, forwarded to ``_record_usage`` for
            P1.9 attribution. When ``None`` (early bootstrap calls,
            tests), the usage log entry uses ``"<unknown>"``.

        Returns
        -------
        MemoryContext
            Always returns a context, never ``None``. The result
            ``is_empty`` is True in every failure / no-match /
            disabled case.

        Raises
        ------
        RuntimeError
            When the service has not been initialized. This is a
            programmer error (the lifespan hook must call
            ``ainitialize`` first); the internal fast paths below
            do NOT swallow it, so a missing initialization is
            immediately visible.
        """
        if not self._initialized:
            raise RuntimeError(
                "MemoryService is not initialized. Call "
                "initialize_memory_service() in FastAPI lifespan "
                "before serving requests."
            )

        # ------------------------------------------------------------------
        # Fast path 1: DISABLED mode
        # ------------------------------------------------------------------
        #
        # Return an empty context without touching the provider. The
        # lifecycle manager installed a NullMemoryProvider for
        # DISABLED mode, so even if we fell through to
        # build_memory_context, it would return [] -- but the fast
        # path avoids the pipeline overhead and produces a cleaner
        # diagnostic (router_decision="no_need" is honest about why
        # nothing was retrieved).
        settings = self._lifecycle.settings
        if not settings.mode.can_read:
            logger.debug(
                "MemoryService.retrieve: DISABLED mode, returning "
                "empty context (query=%r).",
                query[:60],
            )
            empty = build_empty_context(
                query=query,
                scope_refs=[],
                settings=settings,
                router_decision="no_need",
            )
            self._record_usage(run_id=run_id, context=empty)
            return empty

        # ------------------------------------------------------------------
        # Resolve scopes
        # ------------------------------------------------------------------
        try:
            scope_refs = resolve_scope_refs(
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as exc:
            # ``resolve_scope_refs`` raises only on invalid inputs
            # (a ``:`` inside an id, a control character, an empty
            # scope id). All are caller bugs. Log and return empty
            # rather than propagating -- a scope-resolution error
            # must not abort a run.
            logger.warning(
                "MemoryService.retrieve: scope resolution failed "
                "(chat_id=%r, project_id=%r): %s",
                chat_id,
                project_id,
                exc,
            )
            empty = build_empty_context(
                query=query,
                scope_refs=[],
                settings=settings,
                router_decision="no_need",
            )
            self._record_usage(run_id=run_id, context=empty)
            return empty

        # ------------------------------------------------------------------
        # Classify route (deterministic -- never raises)
        # ------------------------------------------------------------------
        route = classify_route(
            query,
            has_active_project=bool(project_id),
        )

        # ------------------------------------------------------------------
        # Build context via the P1.4 pipeline
        # ------------------------------------------------------------------
        #
        # ``build_memory_context`` already handles NO_NEED routes,
        # empty scope_refs, zero budgets, and provider exceptions
        # internally -- every failure path returns a well-formed
        # context. The try/except here is defense in depth against
        # a bug in the pipeline itself.
        try:
            context = await build_memory_context(
                query=query,
                scope_refs=scope_refs,
                route=route,
                provider=self._lifecycle.provider,
                settings=settings,
            )
        except Exception as exc:
            logger.warning(
                "MemoryService.retrieve: pipeline failed "
                "(query=%r, scopes=%r): %s. Returning empty context.",
                query[:60],
                scope_refs,
                exc,
            )
            context = build_empty_context(
                query=query,
                scope_refs=scope_refs,
                settings=settings,
                router_decision=route.decision.value,
            )

        self._record_usage(run_id=run_id, context=context)
        return context

    # ------------------------------------------------------------------
    # Settings reload
    # ------------------------------------------------------------------

    async def reload_settings(self) -> MemorySettings:
        """Re-read settings from disk and apply them.

        Delegates to ``MemoryLifecycleManager.reload_settings``,
        which performs the surgical rebuild logic (rebuild the
        provider only when ``provider_id`` or the DISABLED boundary
        changed; otherwise just swap the settings reference).

        Used by ``PATCH /api/memory/settings`` (P1.8) and by the
        ``main.py`` lifespan if a reload-on-SIGHUP hook is added
        later.

        Raises
        ------
        RuntimeError
            When called before ``ainitialize()``.
        """
        new_settings = await self._lifecycle.reload_settings()
        logger.info(
            "MemoryService settings reloaded (mode=%s, provider=%s).",
            new_settings.mode.value,
            new_settings.provider_id,
        )
        return new_settings

    # ------------------------------------------------------------------
    # Extraction queue (P1.6 stub)
    # ------------------------------------------------------------------

    async def enqueue_extraction(
        self,
        *,
        run_id: str,
        chat_id: str,
        project_id: str,
    ) -> None:
        """Enqueue a post-run extraction job. NOT IMPLEMENTED YET.

        Will land with ``WritePipeline`` + ``Outbox`` in P1.6. Until
        then this method logs and returns -- the dispatcher's
        auto-extraction call site (which is also deferred) is
        already prepared for this no-op behavior: it only invokes
        this method when ``auto_extraction_enabled`` is True, and
        that flag defaults to False in ``MemorySettings``.
        """
        logger.debug(
            "enqueue_extraction called (run=%s, chat=%s, project=%s) "
            "-- not implemented yet (P1.6).",
            run_id,
            chat_id,
            project_id,
        )

    # ------------------------------------------------------------------
    # Internal: usage recording stub
    # ------------------------------------------------------------------

    def _record_usage(
        self,
        *,
        run_id: Optional[str],
        context: MemoryContext,
        actual_tokens: Optional[int] = None,
    ) -> None:
        """Record one retrieval's usage. STUB for P1.9.

        P1.9 replaces this method body with a call to
        ``MemoryUsageTracker.record(run_id, context, actual_tokens)``
        which writes to the ``memory_usage_ledger`` table.

        Until then, this logs the usage at INFO so operators can see
        per-run memory token spend in the server log without needing
        a persistent ledger. The signature will NOT change -- P1.9
        keeps the same parameters, so no call site updates are
        required when the tracker lands.
        """
        try:
            # ``mode`` access can raise if the service was closed
            # between the ``retrieve`` call and this method. Read
            # it defensively so a shutdown race during a slow
            # retrieval does not throw from the log path.
            mode_value = self._lifecycle.settings.mode.value
        except RuntimeError:
            mode_value = "<closed>"

        logger.info(
            "Memory retrieval: run_id=%s, entries=%d, used_tokens=%d, "
            "dropped=%d, mode=%s, router=%s",
            run_id or "<unknown>",
            len(context.memories),
            context.used_tokens,
            context.dropped_for_budget,
            mode_value,
            context.router_decision,
        )
        # TODO(P1.9): replace this log with
        #   await MemoryUsageTracker.record(...)
        # The tracker is async; if we make _record_usage async in
        # P1.9, the two call sites above will need ``await``. That
        # is a mechanical change; the signature and semantics stay
        # the same.


__all__ = ["MemoryService"]
