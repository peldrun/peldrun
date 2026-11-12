# backend/peldrun/memory/providers/sqlite_fts.py
"""
PELDRUN Memory Platform - SQLite-FTS Memory Provider.

Concrete ``MemoryProvider`` implementation backed by the SQLite-FTS
repository from ``omweb.memory.storage``.

Architectural role
------------------

This provider is the BRIDGE between the provider abstraction
(``peldrun.memory.providers.base``) and the concrete storage engine
(``omweb.memory.storage.repository``). It sits exactly on the
architectural boundary that the plan mandates:

    peldrun.memory.providers
        |  (imports MemoryProvider, ABC -- NO omweb)
        |
        v
    [ Lazy import inside open() ]
        |
        v
    omweb.memory.storage.repository
        (SQLite, FTS5, normalizer -- NO peldrun.memory.providers)

Why the lazy import
-------------------

``peldrun/memory/providers/__init__.py`` imports every provider
module eagerly so the factory can register them by class. If this
module imported ``omweb.memory.storage.repository`` at the top,
then importing ``peldrun.memory.providers`` from ANYWHERE would
pull in the entire ``omweb.memory.storage`` stack -- including
migrations, normalizer, and their transitive dependency on
``peldrun.memory.domain``.

That would be tolerable for ``omweb`` (it already depends on
``peldrun``), but it would break:

  * **unit tests** that exercise ``peldrun.memory.providers`` in
    isolation (no omweb import needed),
  * **headless CLI tools** that use the domain directly without
    starting the web layer,
  * **the mypy independence check** that asserts
    ``peldrun.memory.providers.base`` imports no omweb module.

Deferring the import to ``open()`` solves all three. The class
remains importable and inspectable without the storage layer;
only when ``open()`` is called does the dependency materialize.

Lifecycle contract
------------------

The provider is instantiated with a database path but does NOT
open it. Resources materialize on ``open()`` and release on
``close()``. Between them, ``is_open()`` returns True and every
CRUD method delegates to the repository.

Failure semantics follow ``BaseMemoryProvider``:

  * ``__init__`` never raises (unless the arguments are
    structurally invalid -- e.g. ``db_path`` is not a ``Path``).
  * ``open`` raises ``MemoryUnavailableError`` for any backend
    failure (permission denied, FTS5 missing, disk full, corrupt
    schema).
  * CRUD methods raise ``MemoryUnavailableError`` when the
    provider is not open, and delegate to the repository for
    domain errors (``MemoryValidationError``, ``MemoryNotFoundError``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryUnavailableError,
)
from peldrun.memory.providers.base import BaseMemoryProvider


logger = logging.getLogger("peldrun.memory.providers.sqlite_fts")


# ---------------------------------------------------------------------------
# SqliteFtsProvider
# ---------------------------------------------------------------------------


class SqliteFtsProvider(BaseMemoryProvider):
    """SQLite + FTS5 memory provider.

    Public attributes
    -----------------
    provider_id : str
        Constant ``"sqlite_fts"`` -- the canonical identifier this
        provider is registered under in the factory. Used by
        diagnostics and by the settings manager (P1.7) to display
        which backend is active.
    db_path : Path
        The SQLite database file this provider opens. Exposed for
        logging and diagnostics; the actual connection is owned by
        the internal repository instance.
    """

    #: Canonical identifier matching ``MemorySettings.provider_id``
    #: and the factory registry key. Class attribute so it is
    #: accessible without an instance (diagnostic tooling can
    #: enumerate providers by walking ``__subclasses__``).
    provider_id: str = "sqlite_fts"

    def __init__(self, *, db_path: Path) -> None:
        """Configure the provider WITHOUT opening any resources.

        Parameters
        ----------
        db_path
            Path to the SQLite database file. Parent directories are
            created on ``open()``. For testing, pass a path under a
            ``tempfile.TemporaryDirectory()``; this provider does
            NOT support ``":memory:"`` (the repository relies on WAL
            for cross-thread visibility, which in-memory databases
            do not provide).

        Raises
        ------
        TypeError
            When ``db_path`` is not a ``Path`` (structural argument
            validation -- this is a programmer error, not a runtime
            backend failure).
        """
        if not isinstance(db_path, Path):
            raise TypeError(
                f"db_path must be pathlib.Path, got "
                f"{type(db_path).__name__}"
            )
        self.db_path: Path = db_path
        # Repository is created lazily in open() -- see module
        # docstring for why. The leading underscore signals "do not
        # access from outside this class"; the factory and callers
        # only interact via the protocol methods.
        self._repository: Optional[Any] = None
        self._is_open_flag: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the SQLite database and warm up the schema.

        Idempotent: a second call is a silent no-op. Failure to
        open raises ``MemoryUnavailableError`` with the underlying
        cause attached, so ``MemoryService`` can log it and either
        retry or degrade to disabled mode.

        The warm-up step (``_warm_up``) forces the schema
        application and migrations to run at open time rather than
        on the first user request. This makes startup slower by a
        few milliseconds and makes runtime faster and more
        predictable -- a healthy trade-off for a subsystem whose
        failures should be visible at startup, not on turn 5 of a
        user's conversation.

        Raises
        ------
        MemoryUnavailableError
            When the database cannot be opened, migrations fail, or
            the running SQLite library lacks FTS5 (in which case
            the underlying migration module raises with a specific
            message -- this method propagates it unchanged).
        """
        if self._is_open_flag:
            logger.debug(
                "SqliteFtsProvider.open() called on already-open "
                "instance (%s); no-op.",
                self.db_path,
            )
            return

        try:
            # Lazy import: the ONLY omweb import in this module.
            # Keeping it inside open() means importing
            # ``peldrun.memory.providers`` never touches omweb.
            from omweb.memory.storage.repository import (
                SqliteMemoryRepository,
            )
        except ImportError as exc:
            raise MemoryUnavailableError(
                "Cannot import SqliteMemoryRepository. The omweb "
                "storage layer is not installed or not on the "
                "current PYTHONPATH. This provider requires the "
                "full PELDRUN deployment.",
                context={
                    "provider_id": self.provider_id,
                    "db_path": str(self.db_path),
                },
                cause=exc,
            ) from exc

        try:
            self._repository = SqliteMemoryRepository(self.db_path)
            # Warm up: force the first connection, migrations, and
            # FTS drift check to run NOW rather than on the first
            # user request. Runs on a worker thread so the event
            # loop stays responsive during the (brief) I/O.
            await asyncio.to_thread(self._repository._get_conn)
        except MemoryUnavailableError:
            # Already wrapped (e.g. FTS5 missing) -- propagate with
            # the context that only this layer knows (db_path).
            self._repository = None
            raise
        except Exception as exc:
            self._repository = None
            raise MemoryUnavailableError(
                f"Failed to open SQLite memory backend at "
                f"{self.db_path}: {exc}",
                context={
                    "provider_id": self.provider_id,
                    "db_path": str(self.db_path),
                },
                cause=exc,
            ) from exc

        self._is_open_flag = True
        logger.info(
            "SqliteFtsProvider opened (db_path=%s).", self.db_path
        )

    async def close(self) -> None:
        """Release the SQLite connection(s) and clear internal state.

        Idempotent and tolerant of never having been opened -- the
        ``MemoryService`` lifespan handler runs ``close()`` in a
        ``finally`` block, so it may be called after a failed
        ``open()``.

        Any exception from ``repository.close_all()`` is logged and
        swallowed: a shutdown handler must not raise, because that
        would prevent subsequent shutdown steps (FTS cleanup,
        provider unregister, ...) from running.
        """
        if not self._is_open_flag and self._repository is None:
            return

        repo = self._repository
        # Clear flags FIRST so a concurrent close()/open() race
        # sees a consistent "not open" state during cleanup.
        self._is_open_flag = False
        self._repository = None

        if repo is None:
            return

        try:
            await asyncio.to_thread(repo.close_all)
            logger.info(
                "SqliteFtsProvider closed (db_path=%s).", self.db_path
            )
        except Exception as exc:
            # Best-effort cleanup. Log but do not raise -- see the
            # docstring rationale.
            logger.warning(
                "Error while closing SqliteFtsProvider (%s): %s",
                self.db_path,
                exc,
            )

    async def is_open(self) -> bool:
        """Return True between a successful ``open()`` and ``close()``."""
        return self._is_open_flag

    # ------------------------------------------------------------------
    # CRUD -- delegate to repository, guarded by _require_repository
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Delegate to ``SqliteMemoryRepository.search``.

        See ``MemoryPort.search`` in the domain contracts for the
        full semantics.
        """
        repo = self._require_repository()
        return await repo.search(
            query=query,
            scope_refs=scope_refs,
            limit=limit,
            **filters,
        )

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """Delegate to ``SqliteMemoryRepository.create``.

        Raises ``MemoryValidationError`` for duplicate content and
        ``MemoryUnavailableError`` for backend failures.
        """
        repo = self._require_repository()
        return await repo.create(entry)

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Delegate to ``SqliteMemoryRepository.update``.

        Raises ``MemoryNotFoundError`` for missing / out-of-scope
        entries.
        """
        repo = self._require_repository()
        return await repo.update(entry)

    async def delete(self, memory_id: str, scope_ref: str) -> bool:
        """Delegate to ``SqliteMemoryRepository.delete`` (soft delete).

        Returns True when an entry was archived, False when no
        entry matched.
        """
        repo = self._require_repository()
        return await repo.delete(memory_id, scope_ref)

    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """Delegate to ``SqliteMemoryRepository.get``.

        Returns None for missing or out-of-scope entries.
        """
        repo = self._require_repository()
        return await repo.get(memory_id, scope_ref)

    async def mark_used(self, memory_ids: List[str]) -> None:
        """Delegate to ``SqliteMemoryRepository.mark_used`` (bulk).

        Silent no-op when ``memory_ids`` is empty. A backend failure
        is logged and swallowed by the repository -- this method
        propagates the successful-return semantic.
        """
        repo = self._require_repository()
        await repo.mark_used(memory_ids)

    # ------------------------------------------------------------------
    # Extended (non-protocol) API -- UI + administrative operations
    # ------------------------------------------------------------------

    async def list_by_scope(
        self,
        scope_ref: str,
        limit: int = 50,
        offset: int = 0,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Delegate to ``SqliteMemoryRepository.list_by_scope``.

        Used by the P1.8 management API (``GET /api/memory``).
        Unlike ``search``, this does NOT apply a text filter and
        includes every status by default.
        """
        repo = self._require_repository()
        return await repo.list_by_scope(
            scope_ref=scope_ref,
            limit=limit,
            offset=offset,
            **filters,
        )

    async def purge(self, memory_id: str, scope_ref: str) -> bool:
        """Delegate to ``SqliteMemoryRepository.purge`` (hard delete)."""
        repo = self._require_repository()
        return await repo.purge(memory_id, scope_ref)

    async def list_project_summaries(self) -> List[Dict[str, Any]]:
        """Delegate to ``SqliteMemoryRepository.list_project_summaries``."""
        repo = self._require_repository()
        return await repo.list_project_summaries()

    async def get_project_stats(self, scope_id: str) -> Dict[str, Any]:
        """Delegate to ``SqliteMemoryRepository.get_project_stats``."""
        repo = self._require_repository()
        return await repo.get_project_stats(scope_id)

    async def bulk_archive(self, scope_id: str) -> int:
        """Delegate to ``SqliteMemoryRepository.bulk_archive``."""
        repo = self._require_repository()
        return await repo.bulk_archive(scope_id)

    async def bulk_purge(self, scope_id: str) -> int:
        """Delegate to ``SqliteMemoryRepository.bulk_purge``.

        DESTRUCTIVE -- the API layer MUST require explicit
        confirmation before invoking.
        """
        repo = self._require_repository()
        return await repo.bulk_purge(scope_id)

    # ------------------------------------------------------------------
    # Extended (non-protocol) API -- used by the ranker (P1.4)
    # ------------------------------------------------------------------

    async def search_ranked(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Delegate to ``SqliteMemoryRepository.search_ranked``.

        Exposes BM25-derived relevance alongside each entry. This
        method is NOT part of the ``MemoryProvider`` protocol --
        it is an extension the provider offers specifically for
        the ranker (P1.4), which combines relevance with stored
        metadata (importance, recency, scope precedence, pinned
        status) to produce the final ordering.

        Callers that only need the entries (the dispatcher, the
        UI, tests) use ``search`` and receive the same entries
        without the extra tuple overhead. Callers that need to
        FILTER by relevance (``min_relevance_score`` in
        ``MemorySettings``) or re-rank use this method.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            ``(entry, relevance)`` pairs, ``relevance`` in
            ``[0.0, 1.0]``. Empty list when nothing matches.
        """
        repo = self._require_repository()
        return await repo.search_ranked(
            query=query,
            scope_refs=scope_refs,
            limit=limit,
            **filters,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_repository(self) -> Any:
        """Return the repository, or raise if the provider is not open.

        Called at the top of every CRUD method. A caller that
        reaches a CRUD method before ``open()`` has a lifecycle bug;
        raising ``MemoryUnavailableError`` with a clear message
        surfaces it immediately, rather than failing with
        ``AttributeError: 'NoneType' has no attribute 'search'``
        much later.

        The method is synchronous -- it does not need to be async
        because it only inspects an in-memory flag.
        """
        if self._repository is None:
            raise MemoryUnavailableError(
                "SqliteFtsProvider is not open. Call open() before "
                "performing any operation.",
                context={
                    "provider_id": self.provider_id,
                    "db_path": str(self.db_path),
                },
            )
        return self._repository

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Developer-readable representation for logs and debugging."""
        state = "open" if self._is_open_flag else "closed"
        return (
            f"<SqliteFtsProvider db_path={str(self.db_path)!r} "
            f"state={state}>"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["SqliteFtsProvider"]
