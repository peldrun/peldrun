# backend/omweb/memory/bootstrap.py
"""
P1.5 temporary memory bootstrap for dispatcher integration.

Provides lazy, singleton access to a ``MemoryPort`` and
``MemorySettings`` so ``dispatcher.run_instrumented`` and
``dispatcher.run_direct_chat`` can perform memory retrieval BEFORE
constructing their respective ``RunRequest`` / ``build_messages``
call.

TEMPORARY STATUS
----------------
This module is a PLACEHOLDER for the memory lifecycle subsystem that
P1.7 will introduce. The public API it exposes
(``get_memory_bootstrap().retrieve(...)``) is deliberately narrow so
that P1.7 can replace the dispatcher's call site with
``get_memory_service().retrieve(...)`` -- same signature, same
return type, same failure semantics -- without touching the
dispatcher.

P1.7 will replace:
  * environment-variable configuration with ``memory_settings.json``
    managed by ``MemorySettingsManager``,
  * the internal SqliteFtsProvider construction with
    ``MemoryProviderFactory.create(...)`` for full provider support,
  * the ad-hoc lifecycle with a proper FastAPI lifespan hook.

Until then, the bootstrap is intentionally minimal: enough to wire
P1.5 end-to-end, no more.

Configuration
-------------
Uses ``default_settings()`` (READ_WRITE, sqlite_fts, 1024-token
budget) unless:

  * ``PELDRUN_MEMORY_ENABLED=0``   -- forces ``MemoryMode.DISABLED``
    (retrieval returns None; provider never opens).
  * ``PELDRUN_MEMORY_DB_PATH=<p>`` -- override the SQLite file path
    (default: ``<storage_root>/memory.db``).

Design invariants
-----------------

1. Lazy initialization.
   The provider opens on the FIRST dispatch that needs memory, not
   at import time. Startup stays fast for deployments that never
   touch the endpoint.

2. Singleton with an async lock.
   Only one provider instance is created process-wide. A retry
   after a transient failure reuses the same instance if the open
   succeeded; if the open failed, the failure is CACHED -- a broken
   provider is not retried on every request, and memory degrades to
   "empty every turn" until the process restarts. This is the
   correct behavior for a subsystem whose failures should be
   visible at startup, not on every dispatch log line.

3. Never raises to the caller.
   ``retrieve`` returns ``None`` on any failure -- mode disabled,
   provider unavailable, scope resolution error, router error,
   pipeline error. The dispatcher treats ``None`` identically to
   ``MemoryContext.empty()``: no injection, no crash, no log spam
   above DEBUG. This matches the plan's fast-path contract.

4. No dependency on ``omweb.memory.manager``.
   P1.7 will build on this module, not replace it in place. The
   manager will read the same settings, use the same provider
   factory, and the dispatcher's call site will simply be updated
   to use the service's public API. There is no scenario in which
   both bootstrap and service are alive simultaneously in
   production.
"""

# [DEPRECATED — P1.7]
# This module is superseded by ``omweb.memory.manager.memory_service``
# and the process-wide singleton in ``omweb.memory.__init__``. It
# remains only as a fallback reference until the P1.7 end-to-end
# verification passes; deletion is scheduled for the P1.7 cleanup.
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from peldrun.memory.domain import (
    MemoryContext,
    MemoryMode,
    MemoryProvider,
    MemorySettings,
    NullMemoryProvider,
    default_settings,
)
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.services import (
    build_memory_context,
    classify_route,
)


logger = logging.getLogger("omweb.memory.bootstrap")


class MemoryBootstrap:
    """Lazy, process-wide memory port + settings holder.

    See the module docstring for the design rationale and the P1.7
    replacement plan.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._provider: Optional[MemoryProvider] = None
        self._settings: Optional[MemorySettings] = None
        self._init_attempted: bool = False
        self._init_failed: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        """Lazily open the provider exactly once. Cache failures."""
        if self._init_attempted:
            return
        async with self._lock:
            if self._init_attempted:
                return
            self._init_attempted = True

            settings = self._load_settings()
            self._settings = settings

            if settings.mode == MemoryMode.DISABLED:
                logger.info(
                    "MemoryBootstrap: DISABLED mode -- provider will "
                    "not be opened."
                )
                self._provider = NullMemoryProvider()
                return

            try:
                self._provider = await self._build_provider(settings)
                logger.info(
                    "MemoryBootstrap: provider %r opened successfully.",
                    settings.provider_id,
                )
            except Exception as exc:
                # Cache the failure. Every subsequent retrieve()
                # short-circuits without retrying, so a permanently
                # broken DB does not spam the log on every request.
                logger.error(
                    "MemoryBootstrap: failed to open provider %r -- "
                    "memory will be unavailable for this process. "
                    "Cause: %s",
                    settings.provider_id,
                    exc,
                    exc_info=True,
                )
                self._provider = NullMemoryProvider()
                self._init_failed = True

    def _load_settings(self) -> MemorySettings:
        """Load settings from env (P1.5). P1.7 replaces with disk load."""
        settings = default_settings()

        raw = os.environ.get("PELDRUN_MEMORY_ENABLED", "1").strip().lower()
        if raw in ("0", "false", "no", "off", "disabled"):
            settings = settings.model_copy(
                update={"mode": MemoryMode.DISABLED}
            )

        return settings

    async def _build_provider(
        self, settings: MemorySettings
    ) -> MemoryProvider:
        """Build and open the configured provider.

        The P1.5 bootstrap only supports ``sqlite_fts``. P1.7's
        ``MemoryProviderFactory.create`` will handle the full
        registry; until then, any other provider_id is a
        misconfiguration we surface clearly.
        """
        if settings.provider_id != "sqlite_fts":
            raise ValueError(
                f"MemoryBootstrap (P1.5) only supports "
                f"provider_id='sqlite_fts', got "
                f"{settings.provider_id!r}. Use MemoryService "
                f"(P1.7) for other providers."
            )

        from peldrun.memory.providers import SqliteFtsProvider
        from peldrun.storage_paths import get_storage_root

        override = os.environ.get("PELDRUN_MEMORY_DB_PATH")
        if override:
            db_path = Path(override)
        else:
            db_path = get_storage_root() / "memory.db"

        db_path.parent.mkdir(parents=True, exist_ok=True)

        provider = SqliteFtsProvider(db_path=db_path)
        await provider.open()
        return provider

    async def aclose(self) -> None:
        """Close the provider. Reserved for P1.7's lifespan hook.

        P1.5 has no shutdown hook -- this is a no-op unless a test
        explicitly invokes it. P1.7 will wire it into FastAPI's
        lifespan and call it on application shutdown.
        """
        async with self._lock:
            if self._provider is not None:
                try:
                    await self._provider.close()
                except Exception as exc:
                    logger.debug(
                        "MemoryBootstrap: provider close raised: %s",
                        exc,
                    )
            self._provider = None
            self._settings = None
            self._init_attempted = False
            self._init_failed = False

    # ------------------------------------------------------------------
    # Public retrieval API
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        query: str,
        chat_id: Optional[str],
        project_id: Optional[str],
        user_id: Optional[str] = None,
    ) -> Optional[MemoryContext]:
        """Retrieve memory context for a turn.

        Returns ``None`` on ANY failure so the dispatcher can treat
        it identically to "memory disabled" -- no try/except is
        needed at the call site.

        Parameters
        ----------
        query
            The user's prompt (or search query). Empty string is
            legal -- the underlying pipeline falls back to a
            recency listing.
        chat_id
            Current chat id, if any. Used to construct chat-scope
            refs via ``resolve_scope_refs``.
        project_id
            Current project id, if any. Used for project-scope
            refs and to gate the PRECISE router path.
        user_id
            Optional explicit user id. Defaults to
            ``"user:default"`` in single-user deployments.

        Returns
        -------
        Optional[MemoryContext]
            The retrieved context on success (possibly
            ``is_empty`` when nothing matched). ``None`` on any
            failure -- disabled mode, provider failure, retrieval
            error, scope resolution error. The dispatcher MUST
            treat ``None`` and empty ``MemoryContext`` identically.
        """
        try:
            await self._ensure_initialized()
        except Exception as exc:  # defensive -- _ensure swallows
            logger.warning(
                "MemoryBootstrap: initialization failed: %s", exc
            )
            return None

        if self._settings is None or self._provider is None:
            return None

        if self._init_failed:
            return None

        if self._settings.mode == MemoryMode.DISABLED:
            return None

        # Resolve scopes.
        try:
            scope_refs = resolve_scope_refs(
                chat_id=chat_id,
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.debug(
                "MemoryBootstrap: scope resolution failed: %s", exc
            )
            return None

        # Classify route (deterministic -- no LLM, never raises).
        try:
            route = classify_route(
                query,
                has_active_project=bool(project_id),
            )
        except Exception as exc:
            logger.debug(
                "MemoryBootstrap: route classification failed: %s", exc
            )
            return None

        # Build context. The pipeline catches its own provider
        # failures, but we wrap the whole call for defense in depth
        # -- a bug in the pipeline must not crash the dispatcher.
        try:
            return await build_memory_context(
                query=query,
                scope_refs=scope_refs,
                route=route,
                provider=self._provider,
                settings=self._settings,
            )
        except Exception as exc:
            logger.warning(
                "MemoryBootstrap: retrieval failed (query=%r, "
                "scopes=%r): %s. Continuing without memory.",
                query[:60],
                scope_refs,
                exc,
            )
            return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: Optional[MemoryBootstrap] = None
_singleton_lock = asyncio.Lock()


async def get_memory_bootstrap() -> MemoryBootstrap:
    """Return the process-wide MemoryBootstrap singleton."""
    global _singleton
    if _singleton is None:
        async with _singleton_lock:
            if _singleton is None:
                _singleton = MemoryBootstrap()
    return _singleton


__all__ = ["MemoryBootstrap", "get_memory_bootstrap"]
