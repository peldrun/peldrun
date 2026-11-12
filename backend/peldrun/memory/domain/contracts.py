# backend/peldrun/memory/domain/contracts.py
"""
PELDRUN Memory Platform - Domain Contracts (Protocols).

Defines the THREE structural protocols that decouple the memory
domain from its concrete storage, provider, and import backends:

    * ``MemoryPort``           -- what ``ContextManager`` and the
                                  dispatcher (P1.5) see.
    * ``MemoryProvider``       -- what ``MemoryService`` (P1.7)
                                  sees and selects via
                                  ``MemoryProviderFactory`` (P1.3).
    * ``MemoryImportAdapter``  -- what ``ImportManager`` (P1.6)
                                  sees.

Plus ``NullMemoryPort`` -- the fail-safe implementation used when
memory is disabled or when a test needs a no-I/O port.

Why protocols, not abstract base classes
----------------------------------------

Python's ``typing.Protocol`` gives us STRUCTURAL typing: any class
that implements the required method signatures satisfies the
protocol, WITHOUT needing to inherit from it. This has three
concrete benefits for the memory subsystem:

  1. **Zero coupling.** ``SqliteMemoryRepository`` (P1.2) never
     imports ``MemoryPort``; it merely happens to implement the
     same shape. If a future contributor refactors the repository
     to implement ``MemoryPort`` explicitly, ``isinstance`` checks
     keep working -- but if they accidentally remove a required
     method, mypy catches it at their call site, not at import
     time across the whole tree.

  2. **Trivial test doubles.** A test that needs a fake repository
     can define ``class FakePort: async def search(...): ...``
     in ten lines. No inheritance ceremony, no abstract methods
     to stub for the methods the test does not exercise.

  3. **``runtime_checkable`` for defensive isinstance checks.**
     Critical entry points (the ``MemoryService`` facade, the
     ``dispatcher`` before each retrieval) can assert
     ``isinstance(backend, MemoryPort)`` as a sanity check without
     paying a real per-call cost. ``runtime_checkable`` only
     verifies METHOD NAMES exist (not signatures), which is the
     right level of strictness at runtime -- signature drift is
     caught by mypy, not by the isinstance check.

Why THREE protocols, not one
----------------------------

The plan explicitly separates the concerns:

  * ``MemoryPort`` is HIGH-LEVEL. It is what
    ``ContextManager``, the ``dispatcher``, and the tools
    (``memory_save``/``memory_search``) see. Its methods are
    per-operation CRUD + search. It has NO lifecycle methods
    (open/close) because those belong to the ``MemoryService``
    singleton's lifespan, not to per-turn code.

  * ``MemoryProvider`` is LOW-LEVEL. It is what the
    ``MemoryService`` holds internally and what
    ``MemoryProviderFactory`` selects based on
    ``MemorySettings.provider_id``. It includes lifecycle
    (``open``/``close``) and is the layer that a future
    pgvector/LanceDB backend would implement.

  * ``MemoryImportAdapter`` is ORTHOGONAL. It is only used by
    the import subsystem (P1.6). Bundling it into either of
    the other protocols would force every implementer to stub
    unrelated methods.

Keeping the three separate means a test double for retrieval
never has to answer "what does ``can_handle`` do?" and a future
provider never has to answer "what does ``preview`` do?".

``NullMemoryPort`` -- the fast-path implementation
---------------------------------------------------

``NullMemoryPort`` is the canonical implementation of
``MemoryPort`` for two scenarios:

  * **``mode=disabled`` fast path.** When ``MemoryService.retrieve``
    sees ``mode=DISABLED``, it returns ``MemoryContext.empty()``
    WITHOUT touching the backend. If a caller somehow reaches a
    port method (e.g. an old code path that bypasses the mode
    check), the ``NullMemoryPort`` refuses writes with
    ``MemoryUnavailableError`` and returns safe empties on reads
    -- exactly the same observable behavior a real disabled
    backend would produce.

  * **Test scaffolding.** Any test that exercises a code path
    which does not NEED memory functionality can instantiate
    ``NullMemoryPort`` instead of a real SQLite repository. No
    disk I/O, no schema migrations, no cleanup.

The behavioral contract of ``NullMemoryPort`` is documented on
each method below, but the summary is:

    reads  -> empty / None / False (never raise)
    writes -> raise ``MemoryUnavailableError`` (never silently succeed)

The asymmetry is deliberate: a silent write success is a bug the
caller will discover LATER (when the data turns out not to be
there), whereas a raised write error is a bug the caller
discovers IMMEDIATELY. Fail-fast for writes is always correct
when memory is unavailable.

Design invariants
-----------------

1. **No I/O, no framework, no concrete imports.**
   This module imports only ``typing`` + ``asyncio`` (for
   ``NullMemoryPort``) + sibling domain modules
   (``MemoryEntry``, ``MemoryContext``). It never imports
   ``sqlite3``, ``peldrun.memory.providers.*``, ``omweb.*``, or
   any repository class.

2. **Async for I/O, sync for in-memory checks.**
   Every method that touches storage (``search``, ``create``,
   ``update``, ``delete``, ``get``, ``mark_used``, ``open``,
   ``close``, ``preview``, ``apply``) is ``async``. Every
   method that only inspects an in-memory value
   (``can_handle``) is sync. This mirrors ``peldrun.runtime.
   ports``'s established convention and prevents the "async
   method that secretly blocks" anti-pattern.

3. **Sensible return types, never ``Optional[bool]``.**
   ``delete`` returns ``bool`` (True = deleted, False = not
   present). ``get`` returns ``Optional[MemoryEntry]``. ``search``
   returns ``List[MemoryEntry]``, never ``None``. Callers never
   need to distinguish "empty list" from "not found" -- the empty
   list IS the not-found state.

4. **Protocols carry docstrings, not implementations.**
   The protocols declare signatures with ``...`` bodies. The
   docstring on each method describes the contract every
   implementation must honor. ``NullMemoryPort`` is the
   reference implementation -- if a new protocol method is added,
   ``NullMemoryPort`` must be updated in the same commit, or the
   ``isinstance`` check on it will silently start failing (its
   ``runtime_checkable`` requirement is that the method exists).
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Protocol, runtime_checkable

from peldrun.memory.domain.context import MemoryContext
from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.domain.errors import MemoryUnavailableError
from peldrun.memory.domain.scope import MemoryScopeRef


# ---------------------------------------------------------------------------
# Protocol 1: MemoryPort -- the high-level interface
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryPort(Protocol):
    """High-level CRUD + search interface used by ``MemoryService``,
    the ``dispatcher`` (P1.5), and the memory tools.

    Implementations MUST:
      * be safe to call concurrently from multiple coroutines
        (the runtime may issue overlapping retrievals for
        different chats),
      * NEVER raise for missing data -- return empty/None/False,
      * raise ``MemoryUnavailableError`` (from
        ``peldrun.memory.domain.errors``) when the backend is
        genuinely unreachable (disk full, connection lost,
        provider crashed).

    The protocol deliberately has no lifecycle methods (open /
    close). Those belong to ``MemoryService``'s ``lifespan`` and
    are invisible to per-operation callers.
    """

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Return up to ``limit`` entries matching ``query`` in ``scope_refs``.

        Parameters
        ----------
        query
            The user's retrieval query. May be empty (callers
            sometimes want "recent" entries without a text
            match -- implementations MAY treat an empty query
            as a recency-only scan, or MAY return an empty
            list; the protocol does not mandate one behavior
            because backends differ).
        scope_refs
            Canonical ``"<scope_type>:<scope_id>"`` strings, in
            ``MemoryScope.precedence`` order (see
            ``resolve_scope_refs``). The implementation MUST
            return only entries whose
            ``(scope_type, scope_id)`` matches one of these
            refs -- no cross-scope leakage, ever.
        limit
            Maximum number of entries to return. Implementations
            SHOULD honor this exactly; returning fewer is
            allowed when fewer matches exist.
        **filters
            Implementation-specific filters (e.g.
            ``status="active"``, ``memory_type="preference"``).
            The protocol is intentionally open on this dict so
            backends can expose their native filters without a
            protocol change. Standard filters every
            implementation SHOULD support:

              * ``status``  -- ``MemoryStatus`` value or
                                ``None`` (default: active only)
              * ``memory_type`` -- ``MemoryType`` value
              * ``tags``    -- ``List[str]`` (must contain ALL)

        Returns
        -------
        List[MemoryEntry]
            Ranked by the implementation's relevance scoring.
            Empty list when nothing matches. NEVER ``None``.
        """
        ...

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a new entry.

        The returned entry MAY differ from the input in two ways:

          * ``content_hash`` may be recomputed (if the
            implementation normalizes content differently),
          * ``memory_id`` may be reassigned (rare, but allowed
            for backends that want UUIDv7 or similar).

        Raises
        ------
        MemoryValidationError
            When the entry violates a constraint the domain
            model could not detect (e.g. duplicate content
            within the same scope).
        MemoryUnavailableError
            When the backend cannot accept the write (disk
            full, DB locked).
        """
        ...

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist an update to an existing entry.

        The entry MUST already exist (matching by
        ``entry.memory_id``). Implementations MAY verify that
        the entry's scope still matches the caller's -- the
        protocol does not mandate it, but the ``MemoryService``
        facade (P1.7) DOES verify it before calling
        ``update``, so double-checking is defensive.

        Raises
        ------
        MemoryNotFoundError
            When no entry with this ``memory_id`` exists in the
            caller's scope.
        MemoryValidationError
            When the updated entry violates a constraint.
        MemoryUnavailableError
            When the backend cannot accept the write.
        """
        ...

    async def delete(
        self, memory_id: str, scope_ref: str
    ) -> bool:
        """Delete (or soft-delete) an entry.

        Returns True when an entry was present and is now
        deleted (or soft-deleted), False when no entry matched
        ``(memory_id, scope_ref)``. Returning False for "not
        present" rather than raising keeps bulk delete
        idempotent-friendly.

        ``scope_ref`` is the CALLER's effective scope. The
        implementation MUST verify that the entry's own scope
        matches before deleting; a caller passing a
        different scope must NOT be able to delete another
        user's entry.

        Raises
        ------
        MemoryUnavailableError
            When the backend cannot accept the delete.
        """
        ...

    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """Retrieve a single entry by id, restricted to ``scope_ref``.

        Returns ``None`` when either:

          * no entry with ``memory_id`` exists, OR
          * the entry exists but its scope does NOT match
            ``scope_ref``.

        These two cases MUST produce the SAME observable result
        (``None``) to prevent information disclosure -- see
        ``MemoryNotFoundError``'s docstring in ``errors.py`` for
        the security rationale.

        Raises
        ------
        MemoryUnavailableError
            Only when the backend is unreachable. A missing
            entry is never an error at this layer.
        """
        ...

    async def mark_used(self, memory_ids: List[str]) -> None:
        """Update ``last_used_at`` for the given entries, in bulk.

        Called AFTER a retrieval that injected these entries
        into the prompt, so the ranker's recency heuristic
        knows they were recently seen. Bulk semantics are
        important: a naive per-entry update would cost N round-
        trips for a single turn.

        Implementations MUST be idempotent: marking the same
        ids twice produces the same end state (with the second
        ``last_used_at`` being newer, which is correct).

        Silent no-op when ``memory_ids`` is empty.

        Raises
        ------
        MemoryUnavailableError
            When the backend cannot accept the update. A
            failure here is NOT fatal for the caller -- the
            retrieval already succeeded, and a stale
            ``last_used_at`` only affects future ranking.
            ``MemoryService`` logs and continues on failure.
        """
        ...


# ---------------------------------------------------------------------------
# Protocol 2: MemoryProvider -- the low-level backend interface
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryProvider(Protocol):
    """Low-level storage/retrieval backend.

    A ``MemoryProvider`` is selected by ``MemoryProviderFactory``
    (P1.3) based on ``MemorySettings.provider_id`` and is held by
    the ``MemoryService`` (P1.7). It wraps a concrete repository
    (SQLite + FTS in P1.2) plus its lifecycle (open/close).

    The distinction from ``MemoryPort``:

      * ``MemoryProvider`` has ``open()`` / ``close()`` -- these
        are called ONCE per ``MemoryService`` lifespan, not per
        operation.
      * ``MemoryProvider`` is a registry-friendly interface:
        ``MemoryProviderFactory`` returns instances of this
        protocol for a given ``provider_id``.
      * A ``MemoryProvider`` MAY be wrapped as a ``MemoryPort``
        (the ``MemoryService`` typically does exactly this --
        see P1.7). The two protocols are orthogonal: neither
        implies the other, so a provider could be used directly
        by a future consumer that wants lifecycle control.

    Behavioral contract: identical scope-isolation, empty-on-
    missing, and error-raising guarantees as ``MemoryPort``.
    """

    async def open(self) -> None:
        """Initialize the backend (open the DB, apply migrations, warm caches).

        Called exactly once per ``MemoryService`` lifespan, before
        any other provider method. Implementations MUST be
        idempotent: calling ``open`` on an already-open provider
        is a silent no-op (required for reload scenarios where
        ``MemoryService.reload_settings`` may reconstruct the
        provider without restarting the process).

        Raises
        ------
        MemoryUnavailableError
            When the backend cannot be opened (corrupt DB,
            disk full, missing schema and migrations cannot
            apply). This is FATAL for the memory subsystem --
            the ``lifespan`` handler logs it and disables
            memory for the deployment.
        """
        ...

    async def close(self) -> None:
        """Release backend resources (close the DB, drop caches).

        Called exactly once per ``MemoryService`` lifespan, on
        shutdown. MUST be idempotent.

        Silent no-op when the provider was never opened (the
        ``lifespan`` handler may call ``close`` in a ``finally``
        even when ``open`` failed).
        """
        ...

    async def is_open(self) -> bool:
        """Return True when the provider is ready to serve requests.

        Synchronous inspection of an in-memory flag; async only
        to match the lifecycle methods' calling convention. A
        caller that needs a fast-path "is memory available?"
        check uses this rather than trying a real operation
        and catching the resulting error.
        """
        ...

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """See ``MemoryPort.search`` -- identical contract."""
        ...

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """See ``MemoryPort.create`` -- identical contract."""
        ...

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """See ``MemoryPort.update`` -- identical contract."""
        ...

    async def delete(
        self, memory_id: str, scope_ref: str
    ) -> bool:
        """See ``MemoryPort.delete`` -- identical contract."""
        ...

    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """See ``MemoryPort.get`` -- identical contract."""
        ...

    async def mark_used(self, memory_ids: List[str]) -> None:
        """See ``MemoryPort.mark_used`` -- identical contract."""
        ...


# ---------------------------------------------------------------------------
# Return-shape Protocols for MemoryImportAdapter
# ---------------------------------------------------------------------------
#
# The concrete classes for these two return shapes live in P1.6
# (``omweb.memory.imports.import_manager``). Defining them here as
# minimal Protocols serves three purposes:
#
#   1. The ``MemoryImportAdapter`` protocol can reference its return
#      types without importing from ``omweb`` -- preserving the
#      strict layer separation the plan mandates.
#   2. mypy can resolve the forward references in the adapter's
#      method signatures without a name-defined error.
#   3. The protocol shape documents the interface contract that any
#      future import subsystem must honor, making P1.6's design
#      predictable from the domain layer alone.


@runtime_checkable
class ImportPreview(Protocol):
    """Opaque preview shape returned by ``MemoryImportAdapter.preview``.

    The concrete class is defined in P1.6
    (``omweb.memory.imports.import_manager``). This domain-level
    protocol only documents the attributes the API layer (P1.8) and
    the UI rely on when rendering the import confirmation dialog.
    """

    candidates: List[MemoryEntry]
    """The parsed entries that WOULD be created if ``apply`` runs."""

    to_create: int
    """How many of ``candidates`` are new (not already present)."""

    to_skip: int
    """How many of ``candidates`` are duplicates of existing entries."""

    warnings: List[str]
    """Non-fatal issues (e.g. entries skipped for unauthorized scope)."""


@runtime_checkable
class ImportApplyResult(Protocol):
    """Opaque result shape returned by ``MemoryImportAdapter.apply``.

    The concrete class is defined in P1.6. This protocol only
    documents the attributes the caller needs after a confirmed
    import completes.
    """

    created: int
    """How many entries were actually persisted."""

    skipped: int
    """How many entries were skipped as duplicates."""

    errors: List[str]
    """Any per-entry failures (empty on full success)."""


# ---------------------------------------------------------------------------
# Protocol 3: MemoryImportAdapter -- the import subsystem interface
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryImportAdapter(Protocol):
    """One-shot adapter that can convert an external source into memory entries.

    Selected by ``ImportManager`` (P1.6) based on
    ``can_handle(source)``. The lifecycle is fixed:

      1. ``can_handle(source)`` -- a cheap, SYNCHRONOUS check.
         The manager iterates the registered adapters in
         priority order and picks the first that returns True.

      2. ``preview(source, scope_ref)`` -- async. Parses the
         source and returns a preview of the entries it WOULD
         create. NO writes happen here. The manager returns
         this to the UI as a confirmation dialog.

      3. ``apply(source, scope_ref)`` -- async. Performs the
         actual writes, in a single transaction. Called ONLY
         after the user confirms the preview. A partial failure
         MUST roll back every write from this call.

    Adapters are STATELESS: the same instance may handle many
    sources sequentially. Any per-source state (parsed entries,
    dedupe analysis) lives in the return value of ``preview``,
    not on the adapter.
    """

    def can_handle(self, source: Any) -> bool:
        """Return True if this adapter understands ``source``.

        MUST be:
          * SYNCHRONOUS -- called during adapter selection,
          * CHEAP -- no I/O, no parsing, only type/extension
            sniffing,
          * HONEST -- returning True commits the adapter to
            providing a real ``preview`` and ``apply``; a
            well-behaved adapter never returns True for a
            source it cannot fully process.

        ``source`` shape depends on the adapter:

          * ``JsonAdapter``     -- ``dict`` (parsed manifest),
          * ``MarkdownAdapter`` -- ``Path`` or ``str`` (path to
                                   a ``.md`` file),
          * ``SqliteAdapter``   -- ``Path`` (path to a SQLite
                                   file).

        Raising from this method is FORBIDDEN -- the manager
        does NOT catch exceptions here, because a well-behaved
        adapter must never fail this check. A malformed source
        returns False; the manager moves on.
        """
        ...

    async def preview(
        self,
        source: Any,
        scope_ref: MemoryScopeRef,
    ) -> ImportPreview:
        """Parse ``source`` and return the entries it would create.

        MUST NOT write to storage. MUST NOT mutate ``source``.

        The returned ``ImportPreview`` carries:

          * the parsed ``MemoryEntry`` candidates,
          * counts of how many would be created vs skipped as
            duplicates (based on the current storage state),
          * any warnings the user should see (e.g. "12 entries
            reference a scope your session is not authorized
            for and will be skipped").

        Raises
        ------
        MemoryValidationError
            When ``source`` is malformed (missing required
            fields, wrong schema version).
        ScopeViolationError
            When the source contains entries targeting a scope
            the caller is not authorized to write (e.g. an
            imported package trying to write ``user``-scope
            entries without an explicit ``explicit_user``
            source marker).
        MemoryUnavailableError
            When the backend cannot be queried for the dedupe
            analysis.
        """
        ...

    async def apply(
        self,
        source: Any,
        scope_ref: MemoryScopeRef,
    ) -> ImportApplyResult:
        """Persist the entries from ``source`` to storage.

        Called ONLY after the user has confirmed the preview
        returned by ``preview``. The semantics are
        transactional: either every entry is written, or none
        is. A failure mid-way MUST roll back the whole batch.

        Raises
        ------
        MemoryValidationError
            When re-parsing ``source`` (defensive) reveals a
            change from the preview state that invalidates the
            confirmation.
        ScopeViolationError
            When the caller's effective scope changed between
            preview and apply (e.g. session expired). The
            manager re-validates scope at apply time.
        MemoryUnavailableError
            When the backend fails mid-transaction (rollback
            attempt runs but the caller must treat the import
            as failed).
        """
        ...


# ---------------------------------------------------------------------------
# Forward-declared return types for MemoryImportAdapter
# ---------------------------------------------------------------------------
#
# The concrete shapes of ``ImportPreview`` and ``ImportApplyResult``
# are defined in P1.6 (``omweb.memory.imports.import_manager``).
# Declaring them as forward-reference strings above (in the
# protocol method signatures) keeps the domain layer free of
# implementation coupling. The actual classes are produced by the
# import subsystem and consumed by the API layer -- neither side
# needs to live in the domain package.
#
# If a future contributor needs the shapes inside the domain
# package (e.g. to type-check a test double), they can move the
# definitions here. Until then, keeping them out of the domain
# avoids a "the domain knows about imports" coupling that has no
# current justification.


# ---------------------------------------------------------------------------
# NullMemoryPort -- the fail-safe implementation
# ---------------------------------------------------------------------------


class NullMemoryPort:
    """A ``MemoryPort`` that stores nothing and retrieves nothing.

    Used when:
      * ``mode=DISABLED`` and a caller bypasses the mode check
        (defense in depth against disabled-mode bugs),
      * a test needs to exercise a code path that does not care
        about memory functionality,
      * a subsystem is wired without a real backend (e.g. a
        future stateless-mode deployment).

    Behavioral contract (mirrors the async semantics):

      * ``search``       -> ``[]``  (never raises)
      * ``create``       -> raises ``MemoryUnavailableError``
      * ``update``       -> raises ``MemoryUnavailableError``
      * ``delete``       -> ``False``  (nothing to delete)
      * ``get``          -> ``None``
      * ``mark_used``    -> silent no-op

    The read/write asymmetry is the SAME as the real subsystem's
    ``mode=DISABLED`` behavior:

      * Reads return safe empties so a caller that forgot to
        check ``mode`` first does not crash -- it gets an empty
        context and the run proceeds normally without memory.
      * Writes raise so a caller that tries to persist memories
        while disabled discovers the problem IMMEDIATELY, not
        after the next restart when the memories are silently
        missing.

    ``NullMemoryPort`` satisfies ``isinstance(instance, MemoryPort)``
    because it implements every method with the correct name.
    ``runtime_checkable`` only checks names, so signature
    compatibility is mypy's job -- which is correct, since
    runtime signature introspection would cost more than it
    saves here.
    """

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Return an empty list (no memories are available)."""
        return []

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """Reject every write with a clear, non-retryable error."""
        raise MemoryUnavailableError(
            "Memory is disabled (NullMemoryPort). Cannot create entries.",
            context={"memory_id": entry.memory_id},
        )

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Reject every update with a clear, non-retryable error."""
        raise MemoryUnavailableError(
            "Memory is disabled (NullMemoryPort). Cannot update entries.",
            context={"memory_id": entry.memory_id},
        )

    async def delete(
        self, memory_id: str, scope_ref: str
    ) -> bool:
        """Return False (nothing existed to delete)."""
        return False

    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """Return None (no entry can exist)."""
        return None

    async def mark_used(self, memory_ids: List[str]) -> None:
        """Silent no-op (nothing to mark)."""
        return None


# ---------------------------------------------------------------------------
# NullMemoryProvider -- provider-level fail-safe
# ---------------------------------------------------------------------------


class NullMemoryProvider:
    """A ``MemoryProvider`` that behaves identically to ``NullMemoryPort``.

    Exists because the ``MemoryProviderFactory`` (P1.3) may be
    asked for a provider when no real backend is available
    (misconfiguration, initialization failure). Returning a
    ``NullMemoryProvider`` keeps the ``MemoryService`` alive
    (degraded, but functional) instead of crashing on startup.

    Lifecycle is trivial: ``open`` and ``close`` are no-ops;
    ``is_open`` reports False (there is no real backend to be
    open).

    Behavioral contract matches ``NullMemoryPort`` for every
    CRUD method -- the two classes are functionally identical
    apart from the lifecycle methods. They are kept separate
    because they implement DIFFERENT protocols (a
    ``NullMemoryProvider`` is NOT a ``MemoryPort`` structurally,
    and vice versa, even though their shared methods match).
    """

    async def open(self) -> None:
        """No-op: nothing to open."""
        return None

    async def close(self) -> None:
        """No-op: nothing to close."""
        return None

    async def is_open(self) -> bool:
        """Return False: no real backend is ever 'open'."""
        return False

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Return an empty list."""
        return []

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """Reject with ``MemoryUnavailableError``."""
        raise MemoryUnavailableError(
            "No memory provider is configured. Cannot create entries.",
            context={"memory_id": entry.memory_id},
        )

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Reject with ``MemoryUnavailableError``."""
        raise MemoryUnavailableError(
            "No memory provider is configured. Cannot update entries.",
            context={"memory_id": entry.memory_id},
        )

    async def delete(
        self, memory_id: str, scope_ref: str
    ) -> bool:
        """Return False."""
        return False

    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """Return None."""
        return None

    async def mark_used(self, memory_ids: List[str]) -> None:
        """Silent no-op."""
        return None


# ---------------------------------------------------------------------------
# Convenience constructor for tests
# ---------------------------------------------------------------------------


def empty_memory_context(
    query: str,
    scope_refs: Optional[List[str]] = None,
    token_budget: int = 1024,
) -> MemoryContext:
    """Return an empty ``MemoryContext`` for the given query.

    Thin wrapper around ``MemoryContext.empty`` that also
    supplies a safe default ``token_budget``. Provided here so
    tests and the dispatcher's fallback path (P1.5) have a
    single, canonical way to construct "no memory this turn"
    without importing ``MemoryContext`` directly.

    Kept in ``contracts.py`` (rather than in ``context.py``)
    because it is part of the CONTRACT with callers of
    ``MemoryPort`` -- the standard "empty" return value that
    ``mode=DISABLED`` produces. Putting it here keeps the
    domain package's public surface intuitive: "the contracts
    module defines both the interface AND the canonical empty
    response".
    """
    return MemoryContext.empty(
        query=query,
        scope_refs=list(scope_refs or []),
        token_budget=token_budget,
        router_decision="no_need",
        ranker_version="v1",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    # Protocols
    "MemoryPort",
    "MemoryProvider",
    "MemoryImportAdapter",
    # Import return shapes (Protocols for P1.6 concrete classes)
    "ImportPreview",
    "ImportApplyResult",
    # Fail-safe implementations
    "NullMemoryPort",
    "NullMemoryProvider",
    # Convenience constructor
    "empty_memory_context",
]
