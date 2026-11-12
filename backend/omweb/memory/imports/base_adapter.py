# backend/omweb/memory/imports/base_adapter.py
"""
PELDRUN Memory Platform - Import Adapter Base Class + Result Types.

Defines the ABSTRACT BASE for every concrete import adapter
(JsonAdapter, MarkdownAdapter, SqliteAdapter) TOGETHER WITH the
two concrete result models that adapters return:

    * ``BaseImportAdapter``  -- the ABC every adapter subclasses.
    * ``ImportPreview``      -- concrete implementation of the
                                 ``ImportPreview`` Protocol
                                 declared in
                                 ``peldrun.memory.domain.contracts``.
    * ``ImportApplyResult``  -- concrete implementation of the
                                 ``ImportApplyResult`` Protocol.

Why the result types live HERE, not in ``import_manager``
---------------------------------------------------------

The engineering plan's reference spec suggests the concrete
result classes belong in ``import_manager.py``. That placement
creates a NAMING tension with the file sequence the plan ALSO
mandates: ``base_adapter.py`` (file 3 of 11) must be importable
BEFORE ``import_manager.py`` (file 8), because every adapter
subclass in between imports from the base class. If the result
types lived only in ``import_manager``, we would need:

    * deferred imports inside every adapter method
      (``from .import_manager import ImportPreview`` at call
      time), OR
    * a second, competing definition of the same shapes inside
      ``base_adapter.py`` that the manager had to reconcile with.

Neither is acceptable. The cleaner fix -- the one implemented
here -- is to move the concrete result classes NEXT TO the ABC
that produces them. This makes ``base_adapter.py`` a
self-contained unit: importing it gives you the ABC AND the
result types, with zero deferred imports. ``import_manager.py``
imports them from here (which is what it needs anyway, since it
calls ``adapter.preview()`` and ``adapter.apply()``).

The Protocol definitions in ``peldrun.memory.domain.contracts``
remain the CONTRACTUAL description of the shapes -- this module
is the concrete IMPLEMENTATION of those Protocols. The dependency
flow is:

    peldrun.memory.domain.contracts (Protocols -- no deps)
             ^
             |  (structural: the concrete classes satisfy
             |   the Protocol's isinstance check)
             |
    omweb.memory.imports.base_adapter (ABC + concrete classes)
             ^
             |
    omweb.memory.imports.adapters.* (concrete subclasses)

Responsibility split -- the KEY design choice
---------------------------------------------

Every concrete adapter (Json, Markdown, Sqlite) has the SAME
post-parsing work to do:

    1. Turn its source into a list of ``MemoryEntry`` candidates
       (this is the ONLY part that differs).
    2. Analyze each candidate against the existing store to
       classify it as "would create" vs "would skip" (this is
       IDENTICAL across adapters).
    3. When ``apply`` is called, re-analyze each candidate
       against the (possibly changed) store, dispatch through
       ``decide_write_action``, and write (this is also
       IDENTICAL).

By centralizing steps 2 and 3 in ``BaseImportAdapter``, each
concrete adapter only has to implement step 1 -- its parsing
logic. This means:

    * Adding a new adapter (e.g. an Obsidian-vault importer)
      requires ~80 lines of parsing code, not ~300 lines of
      parsing + write orchestration.
    * All adapters share the same write policy because the
      write path goes through the same
      ``decide_write_action`` call. There is no way for the
      JSON adapter to behave differently from the Markdown
      adapter at the write boundary.
    * A bug fix in the write path fixes it for every adapter
      simultaneously.

The trade-off is that an adapter CANNOT customize the write
policy (e.g. "for this format, skip all SUPERSEDE decisions").
In practice, this is correct: the write policy is a global
concern (defined by ``peldrun.memory.services.policies``), not
a per-adapter concern. An adapter that genuinely needed a
different policy would be evidence that the policy itself needs
generalization, not that the base class should expose per-adapter
hooks.

Dedupe analysis -- how it works
-------------------------------

For every candidate, ``_build_preview`` runs a store query to
find entries in the target scope that MIGHT be duplicates,
then delegates classification to ``decide_write_action``:

    existing = await port.search(candidate.content, [scope_ref], limit=5)
    decision = decide_write_action(candidate, existing)

The search query is the candidate's CONTENT. This is the same
approach ``MemorySaveTool`` uses at save time, so preview and
save agree on which entries are considered similar. High-
relevance search results are the likely duplicates; low-relevance
results are likely unrelated and ``decide_write_action`` ignores
them after the hash/Jaccard checks.

The search limit of 5 comes from ``MemorySettings.default_limit``
(matching the retrieval pipeline). A larger limit would catch
more near-duplicates at the cost of more work per candidate; the
default is well-tuned for the common case.

Transactional vs best-effort apply
----------------------------------

The plan (task 1.6.10) says ``apply`` should run "in one
transaction". SQLite's ``MemoryPort`` does not expose per-port
transaction semantics -- each ``create``/``update``/``delete``
is its own implicit transaction, and rolling back across them
would require either a port extension (out of scope for P1) or
a full compensation protocol (error-prone).

The pragmatic design used here is therefore BEST-EFFORT:

    * Each candidate is written independently.
    * A failure on candidate N does NOT abort candidates N+1..
      -- the import continues, logging the error.
    * The final ``ImportApplyResult`` reports:
        * ``created`` -- entries that succeeded (including
                          SUPERSEDE and REACTIVATE paths),
        * ``skipped`` -- entries that matched an existing ACTIVE
                          entry (SKIP decision),
        * ``errors``  -- per-candidate error strings.

This is the same "partial success" semantics that any bulk
import tool needs. Users importing 100 entries from a backup
expect the 95 good ones to land even if 5 are corrupt -- not
for the whole batch to be rejected because of one bad line.

If a future requirement demands true transactional semantics,
the correct path is to add a ``transaction()`` context manager
to ``MemoryPort`` and wrap ``_apply_candidates`` in it. That
port-level change is deliberately not made in P1.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryPort,
    MemoryScopeRef,
    MemorySettings,
    MemoryUnavailableError,
    MemoryValidationError,
)
from peldrun.memory.domain.contracts import MemoryImportAdapter
from peldrun.memory.services import (
    WriteAction,
    decide_write_action,
)


logger = logging.getLogger("omweb.memory.imports.base_adapter")


# ---------------------------------------------------------------------------
# Concrete result models
# ---------------------------------------------------------------------------


class ImportPreview(BaseModel):
    """Concrete preview returned by ``BaseImportAdapter.preview``.

    Satisfies the ``ImportPreview`` Protocol from
    ``peldrun.memory.domain.contracts``. The Protocol requires
    the four public attributes below; this class implements them
    as Pydantic fields so that the result is:
      * validated at construction time,
      * trivially serializable (``model_dump(mode="json")``),
      * automatically rendered by FastAPI as JSON.

    Attributes
    ----------
    candidates
        The parsed entries, unchanged from what the adapter's
        parser produced. Preserved here so the caller can inspect
        what WOULD be imported without having to re-parse.
    to_create
        How many candidates will result in a new ACTIVE entry
        on ``apply``. This count includes CREATE, SUPERSEDE,
        and REACTIVATE decisions -- from the user's perspective,
        all three end with the candidate's content active in the
        target scope.
    to_skip
        How many candidates already exist as ACTIVE entries
        (SKIP decisions). These will be silently no-ops during
        ``apply``.
    warnings
        Non-fatal issues encountered during parsing OR during
        the dedupe analysis. Each warning names the candidate
        index it applies to. Empty list when everything is
        clean.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    candidates: List[MemoryEntry] = Field(
        ...,
        description=(
            "The parsed entries, unchanged from the adapter's "
            "parse step. The caller can inspect these to render "
            "a preview dialog before deciding to apply."
        ),
    )
    to_create: int = Field(
        ...,
        ge=0,
        description=(
            "Count of candidates that will result in a new active "
            "entry when applied (CREATE + SUPERSEDE + REACTIVATE)."
        ),
    )
    to_skip: int = Field(
        ...,
        ge=0,
        description=(
            "Count of candidates that match an existing ACTIVE "
            "entry and will be no-ops on apply (SKIP decisions)."
        ),
    )
    warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal issues from parsing or dedupe analysis. "
            "Each warning names the candidate index it refers to."
        ),
    )


class ImportApplyResult(BaseModel):
    """Concrete result returned by ``BaseImportAdapter.apply``.

    Satisfies the ``ImportApplyResult`` Protocol from
    ``peldrun.memory.domain.contracts``. Attributes mirror the
    protocol's shape exactly; the Protocol's docstrings describe
    what each count means in the successful case.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    created: int = Field(
        ...,
        ge=0,
        description=(
            "Count of candidates that resulted in a new ACTIVE "
            "entry (CREATE + SUPERSEDE + REACTIVATE)."
        ),
    )
    skipped: int = Field(
        ...,
        ge=0,
        description=(
            "Count of candidates that matched an existing ACTIVE "
            "entry and were therefore left alone (SKIP)."
        ),
    )
    errors: List[str] = Field(
        default_factory=list,
        description=(
            "Per-candidate error strings. Empty on full success. "
            "When non-empty, the ``created`` count reflects the "
            "entries that DID succeed -- the import is best-effort."
        ),
    )


# ---------------------------------------------------------------------------
# BaseImportAdapter
# ---------------------------------------------------------------------------


class BaseImportAdapter(MemoryImportAdapter, ABC):
    """Abstract base for every memory import adapter.

    Concrete subclasses MUST implement:

      * ``can_handle(source)`` -- synchronous format detection.
      * ``_parse_to_candidates(source)`` -- async parsing that
        returns ``(candidates, warnings)``.

    They MUST NOT override ``preview`` or ``apply`` -- the base
    class owns those. Overriding them would bypass the shared
    write policy and reintroduce the exact divergence this base
    class exists to prevent. (A subclass that genuinely needs a
    different preview/apply flow is evidence that the flow needs
    to be parameterized at the base-class level, not duplicated
    in the subclass.)

    Construction
    ------------
    Adapters are constructed ONCE per ``ImportManager`` lifetime
    and reused across every import call. The port and settings
    references are held for the manager's lifetime -- they do not
    change between calls.

    The port MUST already be open (``await port.open()`` called
    and returned successfully). The base class does not manage
    the port lifecycle -- that is the ``MemoryService`` singleton's
    job (see P1.7's lifespan wiring).
    """

    def __init__(
        self,
        port: MemoryPort,
        settings: MemorySettings,
    ) -> None:
        """Configure the adapter WITHOUT touching storage.

        Parameters
        ----------
        port
            A ``MemoryPort`` instance -- the memory backend the
            adapter will query for dedupe analysis and write to
            during apply. MUST already be open.
        settings
            The current ``MemorySettings`` snapshot. Read for
            ``default_limit`` (dedupe analysis window) and
            ``mode`` (write-path gating, if the caller already
            checked mode; the adapter does NOT re-check mode --
            that responsibility lives at the ``ImportManager``
            level, which has the user session context to do it
            correctly).

        Raises
        ------
        TypeError
            When ``port`` is None or ``settings`` is not a
            ``MemorySettings``. Structural argument validation --
            caller bugs surface immediately.
        """
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, got "
                f"{type(settings).__name__}"
            )
        self._port: MemoryPort = port
        self._settings: MemorySettings = settings

    #: Bounded fetch size for dedupe analysis. Larger than the
    #: retrieval pipeline's ``default_limit`` (5) because imports
    #: are ONE-SHOT operations: spending an extra ~100ms to check
    #: against 500 existing entries instead of 5 is a clear win
    #: against the cost of creating 100 duplicates.
    #:
    #: The ceiling is not arbitrary:
    #:   * 500 covers most real-world project scopes completely,
    #:   * for very large scopes it degrades gracefully to "check
    #:     against top 500 by importance/recency" (still far better
    #:     than the save-tool's top-5-only behavior),
    #:   * the memory cost of 500 ``MemoryEntry`` objects (~1KB
    #:     each) is ~500KB per import, well within budget.
    #:
    #: P2 will add an indexed lookup path once the memory subsystem
    #: has a proper dedupe index -- until then, this is the
    #: pragmatic Phase 1 compromise.
    _DEDUPE_SCAN_LIMIT: int = 500

    # ------------------------------------------------------------------
    # Dedupe lookup helper
    # ------------------------------------------------------------------

    async def _fetch_existing_for_dedupe(
        self, scope_ref: MemoryScopeRef
    ) -> List[MemoryEntry]:
        """Fetch a bounded set of existing entries for dedupe analysis.

        Uses ``query=""`` (recency/importance listing) rather than
        the candidate's own content. Rationale:

          * The default search does FTS ``MATCH`` with the query's
            tokens AND-connected. A query for ``"we no longer use
            python 3.10; switched to 3.12"`` will NOT match an
            existing entry ``"we use python 3.10"`` -- the old
            entry lacks the query's "no", "longer", "switched",
            "to", "12" tokens. This is precisely the supersede
            pattern we need to detect, and FTS strict matching
            silently hides the entry we should be superseding.

          * ``query=""`` falls back to the repository's
            recency/importance listing, which returns a bounded
            but representative slice of the scope. Combined with
            ``decide_write_action``'s Jaccard-based near-duplicate
            check, this reliably finds the entries that a re-save
            or supersede should act upon.

          * This matches ``MemorySaveTool``'s identical choice,
            so import behavior is consistent with explicit-save
            behavior -- a user who saves an entry then imports the
            same package sees the same dedupe outcome either way.

        The scan is bounded to ``_DEDUPE_SCAN_LIMIT`` entries (500).
        For scopes smaller than the limit, all entries are checked.
        For larger scopes, the top-500 by importance/recency are
        checked -- far better than the save-tool's top-5, and the
        right trade-off for a one-shot import operation.
        """
        scope_ref_str = str(scope_ref)
        # ``status="all"`` is LOAD-BEARING and must not be removed.
        #
        # The repository's default for ``query=""`` (recency
        # listing) filters to ``status='active'`` -- an ARCHIVED or
        # SUPERSEDED entry is invisible to the default scan. That
        # would defeat ``decide_write_action``'s REACTIVATE and
        # SUPERSEDE-against-archived branches entirely:
        #
        #   * "user deletes a memory, later imports a package that
        #     re-adds it" → REACTIVATE could never fire → import
        #     would CREATE (or crash on the UNIQUE constraint if
        #     the archived row shares the same content_hash).
        #
        #   * "user imports a package whose entry was superseded
        #     on this instance by an earlier save" → no duplicate
        #     detected → import would CREATE a second active entry.
        #
        # Passing ``status="all"`` returns every lifecycle state.
        # ``decide_write_action`` then classifies correctly:
        # ACTIVE duplicate → SKIP; ARCHIVED/SUPERSEDED duplicate
        # → REACTIVATE.
        #
        # ``status="all"`` is an implementation-specific filter
        # accepted by the SQLite provider (see
        # ``SqliteMemoryRepository._build_status_filter``). It is
        # intentionally not part of the ``MemoryPort`` protocol's
        # minimum contract -- adapters that use a different
        # provider must ensure the same status-inclusive behavior
        # via whatever mechanism that provider exposes.
        return await self._port.search(
            query="",
            scope_refs=[scope_ref_str],
            limit=self._DEDUPE_SCAN_LIMIT,
            status="all",
        )

    # ------------------------------------------------------------------
    # Abstract contract
    # ------------------------------------------------------------------

    @abstractmethod
    def can_handle(self, source: Any) -> bool:
        """Return True if this adapter can parse ``source``.

        MUST be:
          * SYNCHRONOUS -- called during adapter selection,
          * CHEAP -- no I/O, no parsing, only shape detection,
          * HONEST -- returning True commits the adapter to a
            working ``_parse_to_candidates`` for this source; a
            malformed source returns False; the manager moves on
            to the next adapter.

        See the ``MemoryImportAdapter`` Protocol in
        ``peldrun.memory.domain.contracts`` for the full contract.
        """
        ...

    @abstractmethod
    async def _parse_to_candidates(
        self, source: Any, scope_ref: MemoryScopeRef
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Parse ``source`` into ``(candidates, warnings)``.

        This is the ONLY method concrete adapters need to
        implement.

        ``scope_ref``
            The TARGET scope for entries that need one assigned.
            Adapters that preserve an existing scope (e.g.
            ``JsonAdapter`` uses the scope embedded in each
            package line) accept and IGNORE this parameter.
            Adapters that parse format that carries NO inherent
            scope (e.g. ``MarkdownAdapter``) MUST use this as
            the ``scope_type``/``scope_id`` for every constructed
            ``MemoryEntry``. Using a uniform required signature
            keeps ``preview``/``apply`` simple (they pass it
            exactly once, without per-adapter branching).

        It MUST:

          * be pure with respect to ``source`` (never mutate it),
          * return a list of ``MemoryEntry`` objects with all
            required fields populated (``MemoryEntry.create``
            is the recommended construction path),
          * collect any per-line or per-entry warnings into the
            ``warnings`` list (indexed by position: "line 3:
            ..."), rather than raising for recoverable issues,
          * raise ``MemoryValidationError`` ONLY for FATAL issues
            (malformed manifest, wrong format version, zero
            parseable entries). The base class propagates fatal
            errors to the caller; recoverable issues become
            warnings.

        Implementations SHOULD use ``package_validator``'s
        helpers (``validate_package``, ``validate_manifest``)
        when the source format matches the official package --
        they already handle the "fatal vs warning" boundary
        correctly.
        """
        ...

    # ------------------------------------------------------------------
    # Concrete protocol methods (do NOT override)
    # ------------------------------------------------------------------

    async def preview(
        self, source: Any, scope_ref: MemoryScopeRef
    ) -> ImportPreview:
        """Parse ``source`` and classify each candidate against the store.

        Read-only. The base-class implementation guarantees:

          * ``source`` is never mutated,
          * NO writes reach the port,
          * the returned ``ImportPreview`` carries the full
            candidate list plus accurate ``to_create`` and
            ``to_skip`` counts,
          * parsing warnings AND dedupe analysis warnings are
            merged into a single ``warnings`` list.

        Parameters
        ----------
        source
            The format-specific source (dict, Path, ...). Passed
            verbatim to ``_parse_to_candidates``.
        scope_ref
            The canonical scope reference the import will write
            into. Used both for the dedupe analysis (searches are
            filtered to this scope) and for the apply step (which
            the caller invokes separately).

        Returns
        -------
        ImportPreview
            See the ``ImportPreview`` class docstring.

        Raises
        ------
        MemoryValidationError
            From ``_parse_to_candidates`` -- the source is
            malformed, wrong version, or has no parseable entries.
            The caller (``ImportManager``) wraps this into an
            HTTP 400 response.
        MemoryUnavailableError
            From the port during dedupe analysis. The caller may
            choose to retry; the import has not been started.
        """
        candidates, parse_warnings = await self._parse_to_candidates(
            source, scope_ref
        )
        return await self._build_preview(
            candidates, scope_ref, parse_warnings
        )

    async def apply(
        self, source: Any, scope_ref: MemoryScopeRef
    ) -> ImportApplyResult:
        """Parse ``source`` and write every candidate to the store.

        The base-class implementation:

          * re-parses ``source`` (defensive -- the source should
            not change between preview and apply, but re-parsing
            is cheap and catches caller bugs),
          * dispatches each candidate through
            ``decide_write_action`` against a FRESH store query
            (the store may have changed between preview and
            apply -- for example, another tab added a duplicate),
          * writes the resulting decision via the port,
          * collects per-candidate errors WITHOUT aborting the
            batch (best-effort semantics -- see the module
            docstring).

        Parsing warnings from ``_parse_to_candidates`` are LOGGED
        at DEBUG level. They are NOT propagated into
        ``ImportApplyResult`` because the Protocol shape does not
        have a ``warnings`` field. Callers who want them should
        call ``preview`` first (the interactive flow does exactly
        that; a batch-import script that skips preview accepts
        the loss).

        Parameters
        ----------
        source
            Same as ``preview``.
        scope_ref
            Same as ``preview``.

        Returns
        -------
        ImportApplyResult
            See the ``ImportApplyResult`` class docstring.

        Raises
        ------
        MemoryValidationError
            From ``_parse_to_candidates`` -- the import did not
            start; NO partial writes occurred.
        """
        candidates, parse_warnings = await self._parse_to_candidates(
            source, scope_ref
        )

        if parse_warnings:
            # Log only -- the Protocol result has no warnings slot.
            # See the method docstring for the rationale.
            logger.debug(
                "Import apply proceeding despite %d parse warnings "
                "(%s). Call preview() first to see them.",
                len(parse_warnings),
                parse_warnings[:3],
            )

        return await self._apply_candidates(candidates, scope_ref)

    # ------------------------------------------------------------------
    # Shared helpers (protected -- overridable if genuinely needed)
    # ------------------------------------------------------------------

    async def _build_preview(
        self,
        candidates: List[MemoryEntry],
        scope_ref: MemoryScopeRef,
        parse_warnings: List[str],
    ) -> ImportPreview:
        """Classify each candidate against the current store.

        For every candidate:

          1. Query the store for entries in ``scope_ref`` that
             match the candidate's content (top
             ``settings.default_limit`` results, same query the
             ``memory_save`` tool uses),
          2. Ask ``decide_write_action`` what would happen,
          3. Increment ``to_create`` or ``to_skip`` accordingly.

        A per-candidate dedupe error (e.g. transient port
        unavailable) is treated as "would create" (optimistic --
        the store will report a real error at apply time if the
        issue persists) and recorded as a warning.
        """
        warnings: List[str] = list(parse_warnings)
        to_create = 0
        to_skip = 0

        # Fetch the existing-entry set ONCE for the whole batch
        # (rather than re-querying per candidate). Two benefits:
        #
        #   1. Cost: O(1) query instead of O(N) queries -- critical
        #      when importing 100 entries.
        #   2. Correctness: subsequent candidates in the SAME batch
        #      can be deduped against each other by appending each
        #      would-be-created candidate to the list (below), so
        #      "3 identical entries in one package" correctly
        #      previews as "1 create, 2 skip" instead of "3 create".
        try:
            existing_pool: List[MemoryEntry] = (
                await self._fetch_existing_for_dedupe(scope_ref)
            )
        except Exception as exc:
            # If the fetch fails entirely, fall back to per-candidate
            # checks (worse performance, same semantics) and warn.
            existing_pool = []
            warnings.append(
                f"batch dedupe fetch failed "
                f"({type(exc).__name__}: {exc}); falling back to "
                f"per-candidate checks"
            )

        for idx, candidate in enumerate(candidates):
            try:
                decision = decide_write_action(candidate, existing_pool)
                if decision.action == WriteAction.SKIP:
                    to_skip += 1
                else:
                    # CREATE / SUPERSEDE / REACTIVATE all result in
                    # a NEW active entry from the user's perspective
                    # -- they belong in the "to_create" count.
                    to_create += 1
                    # Add the candidate to the pool so subsequent
                    # candidates that duplicate THIS one within the
                    # same batch are correctly classified as SKIP.
                    # This is a preview-only append -- no writes
                    # actually occur, so the "would-be created" state
                    # is exactly what a real apply would produce.
                    existing_pool.append(candidate)
            except Exception as exc:
                # Optimistic default: assume the entry would be
                # created, warn the caller. If the port is
                # genuinely down, apply will surface a real error.
                to_create += 1
                warnings.append(
                    f"candidate {idx + 1}: dedupe check failed "
                    f"({type(exc).__name__}: {exc}); assuming "
                    f"the entry is new"
                )

        logger.debug(
            "Import preview: %d candidates -> %d create, "
            "%d skip, %d warnings.",
            len(candidates),
            to_create,
            to_skip,
            len(warnings),
        )

        return ImportPreview(
            candidates=candidates,
            to_create=to_create,
            to_skip=to_skip,
            warnings=warnings,
        )

    async def _apply_candidates(
        self,
        candidates: List[MemoryEntry],
        scope_ref: MemoryScopeRef,
    ) -> ImportApplyResult:
        """Write every candidate to the store, best-effort.

        For each candidate:

          1. Re-query the store (the state may have changed since
             preview),
          2. Call ``decide_write_action`` to get a fresh decision,
          3. Execute the decision (CREATE / SKIP / SUPERSEDE /
             REACTIVATE),
          4. On error: log and continue with the next candidate.

        Errors are collected in ``ImportApplyResult.errors`` with
        a ``candidate N (content=...): <exception>`` format so a
        user who investigates can identify which entries failed.
        """
        created = 0
        skipped = 0
        errors: List[str] = []

        scope_ref_str = str(scope_ref)

        # Fetch existing entries ONCE for the batch. Same rationale
        # as ``_build_preview``: O(1) queries vs O(N), and each
        # successful create is appended to the pool so subsequent
        # candidates are correctly deduped against their siblings.
        try:
            existing_pool: List[MemoryEntry] = (
                await self._fetch_existing_for_dedupe(scope_ref)
            )
        except Exception as exc:
            existing_pool = []
            errors.append(
                f"batch dedupe fetch failed "
                f"({type(exc).__name__}: {exc}); dedupe checks "
                f"for this batch will be skipped"
            )

        for idx, candidate in enumerate(candidates):
            try:
                decision = decide_write_action(candidate, existing_pool)

                if decision.action == WriteAction.SKIP:
                    skipped += 1
                    logger.debug(
                        "Import skipped candidate %d (duplicate of %s).",
                        idx + 1,
                        (
                            decision.target_entry.memory_id
                            if decision.target_entry
                            else "<unknown>"
                        ),
                    )
                    continue

                if decision.action == WriteAction.CREATE:
                    created_entry = await self._port.create(candidate)
                    created += 1
                    # Append so subsequent candidates within the
                    # batch are correctly deduped against this new
                    # entry (matches the preview's simulation).
                    existing_pool.append(created_entry)
                    continue

                if decision.action == WriteAction.SUPERSEDE:
                    # Archive the old entry BEFORE creating the
                    # new one. Ordering rationale: if create fails,
                    # the old entry is already archived (both
                    # states visible). If archive fails, we do
                    # NOT create -- the old entry stays active,
                    # and the caller can retry.
                    if decision.target_entry is not None:
                        await self._port.delete(
                            decision.target_entry.memory_id,
                            scope_ref_str,
                        )
                        # Remove the archived target from the pool
                        # so subsequent candidates don't dedupe
                        # against an entry that is no longer active.
                        try:
                            existing_pool.remove(decision.target_entry)
                        except ValueError:
                            pass
                    created_entry = await self._port.create(candidate)
                    created += 1
                    existing_pool.append(created_entry)
                    continue

                if decision.action == WriteAction.REACTIVATE:
                    if decision.target_entry is None:
                        # Should never happen -- REACTIVATE always
                        # carries a target. Defensive fallback to
                        # a plain CREATE so the entry is not lost.
                        await self._port.create(candidate)
                        created += 1
                        continue
                    # Revive the target with the candidate's content,
                    # preserving its memory_id and created_at.
                    revived = decision.target_entry.model_copy(
                        update={
                            "content": candidate.content,
                            "normalized_content": candidate.normalized_content,
                            "content_hash": candidate.content_hash,
                            "importance": candidate.importance,
                            "tags": candidate.tags,
                            "status": MemoryEntry.model_fields[
                                "status"
                            ].default,
                            "provenance": candidate.provenance,
                        }
                    )
                    updated_entry = await self._port.update(revived)
                    created += 1
                    # Update the pool in place: replace the old
                    # (archived/superseded) target with the newly-
                    # revived entry.
                    try:
                        idx_in_pool = existing_pool.index(decision.target_entry)
                        existing_pool[idx_in_pool] = updated_entry
                    except (ValueError, AttributeError):
                        existing_pool.append(updated_entry)
                    continue

                # Unreachable -- WriteAction has exactly 4 members.
                # Defensive: treat as a create so the entry is not
                # silently dropped on a future enum extension.
                logger.warning(
                    "Import: unknown WriteAction %r for candidate %d; "
                    "falling back to CREATE.",
                    decision.action,
                    idx + 1,
                )
                await self._port.create(candidate)
                created += 1

            except MemoryValidationError as exc:
                # Validation error at write time (should be rare --
                # the candidate was already validated during
                # _parse_to_candidates, so a fresh validation error
                # implies a UNIQUE constraint race with a concurrent
                # writer).
                errors.append(
                    f"candidate {idx + 1} "
                    f"(content={candidate.content[:50]!r}): "
                    f"validation error: {exc.message}"
                )
            except MemoryUnavailableError as exc:
                # Backend genuinely unavailable -- log and continue
                # (the next candidate might succeed once the
                # transient issue clears, or the caller will see a
                # batch of errors and retry the whole import).
                errors.append(
                    f"candidate {idx + 1} "
                    f"(content={candidate.content[:50]!r}): "
                    f"backend unavailable: {exc.message}"
                )
            except Exception as exc:  # noqa: BLE001
                # Any other failure -- include the exception type
                # so the caller can classify it (e.g. OSError).
                errors.append(
                    f"candidate {idx + 1} "
                    f"(content={candidate.content[:50]!r}): "
                    f"{type(exc).__name__}: {exc}"
                )

        logger.info(
            "Import apply: %d created, %d skipped, %d errors "
            "(scope=%s, candidates=%d).",
            created,
            skipped,
            len(errors),
            scope_ref_str,
            len(candidates),
        )

        return ImportApplyResult(
            created=created,
            skipped=skipped,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BaseImportAdapter",
    "ImportPreview",
    "ImportApplyResult",
]
