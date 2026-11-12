# backend/omweb/memory/__init__.py
"""
PELDRUN Memory Platform - Package Singleton.

Owns the process-wide ``MemoryService`` instance (P1.7 task 1.7.4).

FastAPI's lifespan hook is the intended caller of
``initialize_memory_service`` (on startup) and
``shutdown_memory_service`` (on teardown):

    @asynccontextmanager
    async def lifespan(app):
        await initialize_memory_service()
        try:
            yield
        finally:
            await shutdown_memory_service()

Handlers and tools then call ``get_memory_service()`` from anywhere
in the process.

Design invariants
-----------------

1. **Singleton with an async lock.**
   Two concurrent ``initialize_memory_service`` calls produce
   exactly one service. Repeated initialization calls (e.g. a
   misconfigured server that calls the hook twice) are silent
   no-ops on the second call.

2. **Fail-fast getter.**
   ``get_memory_service()`` raises ``RuntimeError`` before the
   first initialize. This is the ONLY way to guarantee the
   dispatcher / tools cannot silently operate with an
   uninitialized memory subsystem -- a missing ``await`` on
   init would otherwise result in "memory works but always
   returns empty", the worst kind of bug to diagnose.

3. **Graceful shutdown.**
   ``shutdown_memory_service`` closes the provider and clears
   the singleton. A subsequent ``initialize_memory_service``
   starts fresh (useful in tests and in reload scenarios). The
   shutdown path never raises -- the FastAPI lifespan hook runs
   it from a ``finally`` block that must not throw.

4. **Test override hook.**
   ``set_memory_service_for_tests(service)`` installs an
   externally-constructed service without going through
   ``initialize_memory_service`` (which always uses the
   production storage-root paths). Used by integration tests
   that need a temp DB / temp settings file. Marked "for tests"
   so a linter or reviewer flags misuse in production code.

5. **No import of ``MemoryService`` at module scope.**
   The class import is deferred inside the initialization
   function so that importing ``omweb.memory`` for the getter
   (or for ``__all__`` introspection) does not force the
   ``omweb.memory.manager.memory_service`` import tree
   (settings manager -> peldrun.memory.providers -> sqlite_fts
   -> ...). This keeps startup fast in deployments that never
   touch the memory subsystem.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from omweb.memory.manager.memory_service import MemoryService


logger = logging.getLogger("omweb.memory")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_memory_service: Optional["MemoryService"] = None
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def initialize_memory_service(
    *,
    db_path: Optional[Path] = None,
    settings_path: Optional[Path] = None,
) -> None:
    """Initialize the process-wide ``MemoryService``.

    Idempotent. Safe to call on every process startup. A second
    call after the first completes is a silent no-op.

    Parameters
    ----------
    db_path
        Override for the SQLite database file. ``None`` (the
        production default) uses ``<storage_root>/memory.db``.
        Tests pass a ``TemporaryDirectory`` path.
    settings_path
        Override for the settings JSON file. ``None`` uses
        ``<storage_root>/memory_settings.json``.

    Raises
    ------
    Nothing on expected paths. Provider initialization failures
    inside ``MemoryService.ainitialize`` degrade to a null
    provider (see the lifecycle manager's docstring) -- the
    service stays initialized and every ``retrieve`` returns an
    empty context, so this function returns normally.
    """
    global _memory_service
    async with _lock:
        if _memory_service is not None:
            logger.debug(
                "initialize_memory_service: already initialized; no-op."
            )
            return

        from omweb.memory.manager.memory_service import MemoryService

        service = MemoryService(
            db_path=db_path,
            settings_path=settings_path,
        )
        await service.ainitialize()
        _memory_service = service
        logger.info(
            "Memory service initialized (mode=%s, provider=%s).",
            service.settings.mode.value,
            service.settings.provider_id,
        )


def get_memory_service() -> "MemoryService":
    """Return the process-wide ``MemoryService``.

    Raises
    ------
    RuntimeError
        When called before ``initialize_memory_service()``. This is
        the load-bearing contract: a missing initialization is
        visible immediately, not masked as "memory always empty".
    """
    if _memory_service is None:
        raise RuntimeError(
            "MemoryService is not initialized. Call "
            "initialize_memory_service() in FastAPI lifespan before "
            "serving requests."
        )
    return _memory_service


async def shutdown_memory_service() -> None:
    """Close and clear the process-wide ``MemoryService``.

    Safe to call before initialize (no-op). Safe to call twice
    in a row (second call is a no-op). Never raises -- the
    FastAPI lifespan hook runs this from a ``finally`` block.
    """
    global _memory_service
    async with _lock:
        if _memory_service is None:
            return
        service = _memory_service
        _memory_service = None
        try:
            await service.aclose()
            logger.info("Memory service shut down cleanly.")
        except Exception as exc:
            logger.warning(
                "Memory service shutdown raised (ignoring): %s", exc
            )


def set_memory_service_for_tests(
    service: Optional["MemoryService"],
) -> None:
    """[TEST-ONLY] Install or clear the singleton for tests.

    Production code MUST NOT call this. It exists so integration
    tests that need a temp DB / temp settings can construct their
    own ``MemoryService`` and inject it without touching the
    production storage root.

    The name is deliberate -- ``_for_tests`` -- so a reviewer or
    linter flags any use outside ``tests/memory/``.

    Calling with ``None`` clears the singleton (used in test
    teardown).
    """
    global _memory_service
    _memory_service = service


__all__ = [
    "initialize_memory_service",
    "get_memory_service",
    "shutdown_memory_service",
    "set_memory_service_for_tests",
]
