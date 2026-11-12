# backend\peldrun\memory\providers\base.py
"""
PELDRUN Memory Platform - Provider Abstract Base Class.

Defines ``BaseMemoryProvider``: the abstract class that every
concrete backend (SQLite-FTS today; pgvector / LanceDB / Redis
tomorrow) must subclass.

Why an abstract CLASS, not another Protocol
-------------------------------------------

The domain layer (``peldrun.memory.domain.contracts``) already
declares ``MemoryProvider`` as a ``Protocol``. That Protocol is what
``MemoryService`` (P1.7) type-checks against. This module provides
a MIRROR abstract class with a single, specific purpose:

    * **Enforce the lifecycle contract at inheritance time.**

A concrete subclass MUST implement ``open``, ``close``, ``is_open``,
plus the six CRUD methods. ``Protocol`` only requires the shapes to
be present -- a concrete class could accidentally omit a method and
``isinstance(instance, MemoryProvider)`` at runtime would still
return True for a partially-implemented class (``runtime_checkable``
checks method NAMES, not abstract-method completeness).

``BaseMemoryProvider`` gives us:

    * an ``ABC`` that cannot be instantiated directly,
    * a compiler-time (well, import-time) guarantee that a
      subclass has overridden every abstract method,
    * a single place to document the lifecycle contract that all
      providers share.

The two coexist deliberately:

    * ``MemoryProvider`` (Protocol) is what CALLERS hold.
    * ``BaseMemoryProvider`` (ABC) is what IMPLEMENTERS inherit.

A future plugin written by a third party can satisfy the
Protocol without subclassing our ABC -- the factory accepts any
object that implements the methods (it uses ``isinstance``
against the Protocol). This keeps the design open for backends
that do not want to depend on our abstract base (e.g. a
short-lived test double).

Lifecycle contract
------------------

Every provider goes through exactly this sequence:

    1. ``__init__(...)`` -- configure, do NOT open resources.
       Constructors must be fast and side-effect-free.

    2. ``await open()`` -- open the backend. Called exactly once
       per MemoryService lifespan, from ``lifespan`` (P1.7).
       MUST be idempotent (a second ``open()`` is a no-op).

    3. Zero or more CRUD calls (``search``/``create``/...).
       ``is_open()`` MUST return True between ``open`` and
       ``close``.

    4. ``await close()`` -- release resources. Called exactly
       once per MemoryService lifespan, on shutdown. MUST be
       idempotent AND MUST tolerate being called without a
       preceding successful ``open`` (the ``lifespan`` handler
       calls ``close`` in a ``finally`` block, even if ``open``
       failed).

This contract is what allows ``MemoryService`` to be crash-safe:
a failed ``open`` never leaves resources half-allocated, because
the caller always runs ``close``.

Error semantics
---------------

Every method that touches storage MUST raise
``MemoryUnavailableError`` when the backend cannot serve the
request (disk full, connection lost, migrations failed). Every
method that receives a caller-supplied identifier MUST raise
``MemoryValidationError`` or ``MemoryNotFoundError`` per the
domain contract. The abstract class documents these obligations
in each method's docstring so a new implementer sees them at
the exact point where they implement the method.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvider,
)


logger = logging.getLogger("omweb.memory.providers.base")


# ---------------------------------------------------------------------------
# BaseMemoryProvider
# ---------------------------------------------------------------------------


class BaseMemoryProvider(ABC, MemoryProvider):
    """Abstract base class for every memory backend.

    Subclasses MUST implement every ``@abstractmethod`` below. The
    inherited ``MemoryProvider`` protocol is satisfied structurally
    by the presence of these methods; the ABC layer adds the
    inheritance-time enforcement that the Protocol cannot.

    Concrete subclass construction rules
    ------------------------------------
    * The ``__init__`` MUST be fast (no I/O). Resource acquisition
      belongs in ``open()``.
    * The ``__init__`` MUST NOT raise for misconfiguration that
      ``open()`` can detect and report with a good message --
      raise ``MemoryUnavailableError`` from ``open()`` instead,
      where the MemoryService can catch and degrade cleanly.
    * The subclass SHOULD store its configuration (path, URL,
      credentials, ...) as instance attributes so diagnostics and
      logs can inspect them without calling into the backend.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def open(self) -> None:
        """Open the backend and make it ready to serve requests.

        Called exactly once per ``MemoryService`` lifespan, before
        any CRUD call. Idempotent: a second ``open()`` on an
        already-open provider is a silent no-op.

        Implementations SHOULD:
          * acquire every resource they will need (SQLite
            connection pool, HTTP client, ...),
          * run migrations / schema checks,
          * validate configuration and raise
            ``MemoryUnavailableError`` with a precise,
            operator-actionable message if validation fails,
          * set an internal ``_is_open`` flag as the LAST step
            (so ``is_open()`` correctly reports False during a
            partial open that then fails).

        Implementations MUST NOT:
          * perform I/O beyond what opening requires (no eager
            preloading of entries),
          * raise for a transient failure that a retry could fix
            (let the caller decide -- see ``MemoryService``'s
            retry policy in P1.7).

        Raises
        ------
        MemoryUnavailableError
            When the backend genuinely cannot be opened. The
            message should tell the operator what to fix.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release backend resources.

        Called exactly once per ``MemoryService`` lifespan, on
        shutdown. MUST be:
          * idempotent (safe to call multiple times),
          * tolerant of a failed ``open()`` (must not raise when
            the provider was never opened),
          * best-effort (a failure to release one resource must
            not prevent releasing the others).

        Implementations SHOULD log resource-release failures at
        WARNING level and continue -- a shutdown handler must not
        raise (that would prevent subsequent shutdown steps from
        running).
        """
        ...

    @abstractmethod
    async def is_open(self) -> bool:
        """Return True when the provider is ready to serve requests.

        Synchronous inspection of an in-memory flag; async only to
        match the lifecycle methods' calling convention. A caller
        that needs a fast-path "is memory available?" check uses
        this rather than trying a real operation and catching the
        resulting error.
        """
        ...

    # ------------------------------------------------------------------
    # CRUD -- same contracts as MemoryPort.search / create / etc.
    # ------------------------------------------------------------------

    @abstractmethod
    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Search entries in the given scopes.

        See ``MemoryPort.search`` in
        ``peldrun.memory.domain.contracts`` for the full contract.
        In short:

          * only entries matching one of ``scope_refs`` are
            returned -- no cross-scope leakage, ever,
          * ``query`` may be empty (backends MAY interpret as a
            recency listing),
          * returns ``[]`` when nothing matches -- NEVER ``None``,
          * raises ``MemoryUnavailableError`` on backend failure.

        Implementations MUST apply their own scope filtering. The
        caller does NOT pre-filter, and a bug in one provider's
        filter cannot be caught by a different provider's filter.
        """
        ...

    @abstractmethod
    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a new entry.

        Raises
        ------
        MemoryValidationError
            When the entry violates a constraint the domain model
            could not detect (e.g. duplicate content within the
            same scope).
        MemoryUnavailableError
            When the backend cannot accept the write.
        """
        ...

    @abstractmethod
    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist an update to an existing entry.

        Raises
        ------
        MemoryNotFoundError
            When no entry with this ``memory_id`` exists in the
            caller's effective scope.
        MemoryValidationError
            When the updated entry violates a constraint.
        MemoryUnavailableError
            When the backend cannot accept the write.
        """
        ...

    @abstractmethod
    async def delete(self, memory_id: str, scope_ref: str) -> bool:
        """Soft-delete (or hard-delete) an entry.

        Returns True when an entry was present and is now
        deleted/archived, False when no entry matched
        ``(memory_id, scope_ref)``.

        The semantics of "delete" (soft vs hard) are
        provider-specific. SQLite-FTS soft-deletes
        (``status=archived``) and offers a separate ``purge``
        method for hard deletion. A future backend (e.g. Redis
        with TTL) may delete hard by default -- the contract only
        requires that subsequent ``search`` calls do not return
        the entry.

        Raises
        ------
        MemoryUnavailableError
            When the backend cannot accept the delete.
        """
        ...

    @abstractmethod
    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """Retrieve a single entry by id, restricted to ``scope_ref``.

        Returns ``None`` when either no entry with ``memory_id``
        exists OR the entry exists but its scope does not match
        ``scope_ref``. The two cases MUST be indistinguishable
        (information-disclosure prevention, see
        ``MemoryNotFoundError``'s docstring).

        Raises
        ------
        MemoryUnavailableError
            Only when the backend is unreachable. A missing entry
            is never an error at this layer.
        """
        ...

    @abstractmethod
    async def mark_used(self, memory_ids: List[str]) -> None:
        """Update ``last_used_at`` for the given entries, in bulk.

        Silent no-op when ``memory_ids`` is empty.

        Implementations SHOULD batch (a single UPDATE ... WHERE
        memory_id IN (...)) rather than iterate -- retrieval
        happens on every turn and N round-trips would be a
        measurable cost.

        Failures SHOULD be logged and swallowed. A failure to
        update ``last_used_at`` only affects future ranking; it
        must not abort a run whose retrieval already succeeded.
        """
        ...


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["BaseMemoryProvider"]
