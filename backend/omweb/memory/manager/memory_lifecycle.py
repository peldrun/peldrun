# backend/omweb/memory/manager/memory_lifecycle.py
"""
PELDRUN Memory Platform - Provider Lifecycle Manager.

Owns the process-wide provider lifecycle for the memory subsystem
(P1.7 task 1.7.2):

  * ``ainitialize()``     -- open the configured provider,
  * ``aclose()``          -- close it (safe to call without init),
  * ``reload_settings()`` -- re-read settings and rebuild the
                             provider only when something relevant
                             changed.

The lifecycle manager is a PURE ORCHESTRATOR. It holds no business
logic -- all decisions about "should this query be remembered?",
"how much budget?", "which scopes?" live in ``MemoryService``
(P1.7 task 1.7.3). This class answers exactly one question: "is
there a live provider behind the current settings?".

Design invariants
-----------------

1. **Disabled mode = no provider.**
   When ``settings.mode == MemoryMode.DISABLED``, the lifecycle
   manager uses ``NullMemoryProvider`` and does NOT touch the
   SQLite file. This honors the acceptance test #11 contract:
   "mode=disabled does not read or write". No DB file is opened,
   so no read is even possible.

2. **Failures are loud but non-fatal.**
   If the provider cannot be opened (SQLite missing FTS5, corrupt
   file, permissions), the lifecycle manager logs at ERROR and
   falls back to ``NullMemoryProvider``. Startup NEVER blocks on
   a broken memory subsystem.

3. **Idempotent lifecycle.**
   * ``ainitialize()`` on a ready manager is a no-op.
   * ``aclose()`` on a never-initialized manager is a no-op.
   * ``aclose()`` twice in a row: second call is a no-op.
   This lets the FastAPI lifespan hook call ``aclose()`` from a
   ``finally`` block without guarding.

4. **Settings reload is surgical.**
   ``reload_settings()`` re-reads the settings file, then decides
   whether the provider must be rebuilt:

     * ``provider_id`` changed              -> rebuild,
     * ``mode`` crossed the DISABLED
       boundary (either direction)          -> rebuild,
     * otherwise                            -> keep provider,
                                               swap settings only.

   The "keep provider" path preserves SQLite's open connection
   and FTS index cache, so a settings change like
   ``min_relevance_score=0.3 -> 0.4`` costs zero I/O.

5. **State exposure is synchronous and lock-free.**
   ``is_ready``, ``provider``, and ``settings`` are sync
   properties. They read immutable references; no lock is
   needed. ``provider`` and ``settings`` raise ``RuntimeError``
   when accessed before ``ainitialize()`` -- a caller that
   forgets to initialize gets a clear, actionable error instead
   of a silent attribute access on ``None``.

6. **Bounded concurrency.**
   ``ainitialize``, ``aclose``, and ``reload_settings`` take the
   manager's ``asyncio.Lock``. A racing pair of
   ``ainitialize`` calls produces exactly one provider instance.
   A ``reload_settings`` concurrent with ``aclose`` cannot leak
   a provider: whichever lock acquirer runs second sees the
   state the first left behind.

DB path resolution
------------------
The default DB location is ``<storage_root>/memory.db``. A caller
can override via the ``db_path`` constructor argument -- tests
always do; production never needs to. The override only affects
the ``sqlite_fts`` provider; other providers (future pgvector,
Redis) will use their own configuration source, which the
provider factory will read from ``MemorySettings``. That
wiring lives inside ``MemoryProviderFactory.create`` (P1.3), not
here.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from peldrun.memory.domain import (
    MemoryMode,
    MemoryProvider,
    MemorySettings,
    NullMemoryProvider,
)
from peldrun.memory.providers import MemoryProviderFactory

from omweb.memory.manager.memory_settings_manager import (
    MemorySettingsManager,
)


logger = logging.getLogger("omweb.memory.manager.lifecycle")


class MemoryLifecycleManager:
    """Owns the provider lifecycle for the memory subsystem.

    See the module docstring for the full rationale and the exact
    failure semantics.
    """

    def __init__(
        self,
        settings_manager: MemorySettingsManager,
        db_path: Optional[Path] = None,
    ) -> None:
        """Construct the lifecycle manager.

        Parameters
        ----------
        settings_manager
            The persistent settings store. The lifecycle manager
            reads from it during ``ainitialize`` and
            ``reload_settings``; it never writes.

        db_path
            Override for the SQLite database file. ``None`` (the
            production default) resolves lazily to
            ``<storage_root>/memory.db`` so tests can override
            without racing an import. Ignored for non-sqlite
            providers.

        Raises
        ------
        TypeError
            When ``settings_manager`` is not a
            ``MemorySettingsManager`` instance or ``db_path`` is
            neither a ``Path`` nor ``None``.
        """
        if not isinstance(settings_manager, MemorySettingsManager):
            raise TypeError(
                f"settings_manager must be MemorySettingsManager, "
                f"got {type(settings_manager).__name__}"
            )
        if db_path is not None and not isinstance(db_path, Path):
            raise TypeError(
                f"db_path must be pathlib.Path or None, "
                f"got {type(db_path).__name__}"
            )
        self._settings_manager = settings_manager
        self._explicit_db_path: Optional[Path] = (
            Path(db_path) if db_path is not None else None
        )

        self._lock = asyncio.Lock()
        self._provider: Optional[MemoryProvider] = None
        self._settings: Optional[MemorySettings] = None
        self._ready: bool = False

    # ------------------------------------------------------------------
    # Read-only state access
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """True after a successful ``ainitialize()`` until ``aclose()``."""
        return self._ready

    @property
    def provider(self) -> MemoryProvider:
        """Return the current provider.

        Raises
        ------
        RuntimeError
            Before ``ainitialize()`` or after ``aclose()``. This is
            deliberate -- every caller in the memory subsystem
            (``MemoryService``, tests, health endpoints) has an
            explicit initialization contract. A ``None`` return
            would push the "did someone forget to init?" check to
            every call site.
        """
        if not self._ready or self._provider is None:
            raise RuntimeError(
                "MemoryLifecycleManager is not initialized. Call "
                "ainitialize() before accessing the provider."
            )
        return self._provider

    @property
    def settings(self) -> MemorySettings:
        """Return the current settings snapshot.

        Raises
        ------
        RuntimeError
            Before ``ainitialize()`` or after ``aclose()``. Same
            rationale as ``provider``.
        """
        if not self._ready or self._settings is None:
            raise RuntimeError(
                "MemoryLifecycleManager is not initialized. Call "
                "ainitialize() before accessing settings."
            )
        return self._settings

    @property
    def db_path(self) -> Path:
        """Return the effective SQLite path (resolved on first access)."""
        if self._explicit_db_path is not None:
            return self._explicit_db_path
        from peldrun.storage_paths import get_storage_root

        return get_storage_root() / "memory.db"

    # ------------------------------------------------------------------
    # Lifecycle: initialize
    # ------------------------------------------------------------------

    async def ainitialize(self) -> None:
        """Open the configured provider. Idempotent.

        In ``MemoryMode.DISABLED``, the provider is set to
        ``NullMemoryProvider`` and the DB file is never touched.
        This is the acceptance-test #11 guarantee: no read or
        write is even POSSIBLE in disabled mode, because there is
        no live backend.

        Failure handling: any provider-open exception is logged
        at ERROR, and the manager falls back to
        ``NullMemoryProvider``. ``is_ready`` becomes ``True`` in
        both the success and failure cases -- the manager is
        "ready" to serve requests, even if the answer to every
        request is "no memories". This is what lets ``MemoryService``
        (which calls into the provider blindly) stay simple.

        Raises
        ------
        Nothing. Provider failures are handled internally, and
        settings loading never raises (see
        ``MemorySettingsManager.load``).
        """
        async with self._lock:
            if self._ready:
                logger.debug(
                    "MemoryLifecycleManager.ainitialize() on an "
                    "already-ready manager: no-op."
                )
                return

            settings = await self._settings_manager.load()
            self._settings = settings

            await self._open_provider_for(settings)
            self._ready = True

            logger.info(
                "MemoryLifecycleManager ready (mode=%s, provider=%s).",
                settings.mode.value,
                settings.provider_id,
            )

    # ------------------------------------------------------------------
    # Lifecycle: close
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the current provider and reset state. Idempotent.

        Safe to call before ``ainitialize()`` (the FastAPI lifespan
        hook runs it from a ``finally`` block that may execute
        after a failed startup). Also safe to call twice in a row
        -- the second call is a no-op.

        Does NOT raise. Provider-close failures are logged at
        WARNING and swallowed: a shutdown path must not throw,
        because that would skip every subsequent cleanup step.
        """
        async with self._lock:
            if not self._ready and self._provider is None:
                return

            provider = self._provider
            self._provider = None
            self._settings = None
            self._ready = False

            if provider is None:
                return

            try:
                await provider.close()
                logger.info(
                    "MemoryLifecycleManager closed the provider."
                )
            except Exception as exc:
                logger.warning(
                    "MemoryLifecycleManager: provider close raised: %s",
                    exc,
                )

    # ------------------------------------------------------------------
    # Lifecycle: reload settings
    # ------------------------------------------------------------------

    async def reload_settings(self) -> MemorySettings:
        """Re-read settings from disk; rebuild provider only if needed.

        Surgical-rebuild rules (documented in the module docstring):

          * ``provider_id`` changed                 -> rebuild,
          * ``mode`` crossed the DISABLED boundary -> rebuild,
          * otherwise                              -> keep provider,
                                                     swap settings only.

        The "keep provider" path is the common case for a settings
        change like raising ``min_relevance_score`` or adjusting
        ``token_budget`` -- both are read by the retrieval pipeline
        on every call, so swapping the ``settings`` reference is
        enough.

        Returns
        -------
        MemorySettings
            The new settings snapshot, fully validated.

        Raises
        ------
        RuntimeError
            When called before ``ainitialize()``. Reloading on an
            uninitialized manager is a programmer error -- the
            initial init must succeed (or degrade) first.
        """
        async with self._lock:
            if not self._ready:
                raise RuntimeError(
                    "MemoryLifecycleManager.reload_settings() requires "
                    "a ready manager. Call ainitialize() first."
                )

            assert self._settings is not None  # guaranteed by _ready
            old_settings = self._settings
            new_settings = await self._settings_manager.load()

            old_disabled = old_settings.mode == MemoryMode.DISABLED
            new_disabled = new_settings.mode == MemoryMode.DISABLED

            provider_id_changed = (
                new_settings.provider_id != old_settings.provider_id
            )
            disabled_boundary_crossed = old_disabled != new_disabled

            if provider_id_changed or disabled_boundary_crossed:
                logger.info(
                    "MemoryLifecycleManager: provider rebuild required "
                    "(provider_id %r->%r, mode %r->%r).",
                    old_settings.provider_id,
                    new_settings.provider_id,
                    old_settings.mode.value,
                    new_settings.mode.value,
                )
                await self._close_provider_safe()
                await self._open_provider_for(new_settings)
            else:
                logger.info(
                    "MemoryLifecycleManager: settings reloaded in "
                    "place (mode=%s, provider=%s).",
                    new_settings.mode.value,
                    new_settings.provider_id,
                )

            self._settings = new_settings
            return new_settings

    # ------------------------------------------------------------------
    # Internal: provider open / close helpers
    # ------------------------------------------------------------------

    async def _open_provider_for(
        self, settings: MemorySettings
    ) -> None:
        """Set ``self._provider`` according to ``settings``.

        On DISABLED mode, install ``NullMemoryProvider`` and skip
        all I/O. Otherwise, build the configured provider via the
        factory and open it. On any failure, fall back to
        ``NullMemoryProvider`` with a loud ERROR log.
        """
        if settings.mode == MemoryMode.DISABLED:
            self._provider = NullMemoryProvider()
            logger.info(
                "MemoryLifecycleManager: DISABLED mode -- provider "
                "set to NullMemoryProvider (no DB touched)."
            )
            return

        kwargs: dict = {}
        if settings.provider_id == "sqlite_fts":
            db_path = self.db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            kwargs["db_path"] = db_path

        try:
            provider = MemoryProviderFactory.create(
                settings.provider_id, **kwargs
            )
            await provider.open()
            self._provider = provider
            logger.info(
                "MemoryLifecycleManager: provider %r opened (db_path=%s).",
                settings.provider_id,
                str(kwargs.get("db_path", "<provider default>")),
            )
        except Exception as exc:
            logger.error(
                "MemoryLifecycleManager: failed to open provider %r "
                "-- falling back to NullMemoryProvider. Cause: %s",
                settings.provider_id,
                exc,
                exc_info=True,
            )
            self._provider = NullMemoryProvider()

    async def _close_provider_safe(self) -> None:
        """Close ``self._provider`` if present, swallowing errors.

        Used by ``reload_settings`` before a rebuild. The caller
        is inside ``self._lock``, so no double-close race with
        ``aclose`` is possible.
        """
        provider = self._provider
        self._provider = None
        if provider is None:
            return
        try:
            await provider.close()
        except Exception as exc:
            logger.warning(
                "MemoryLifecycleManager: provider close during "
                "reload raised: %s",
                exc,
            )


__all__ = ["MemoryLifecycleManager"]
