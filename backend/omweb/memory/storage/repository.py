# backend/omweb/memory/storage/repository.py
"""
PELDRUN Memory Platform - SQLite Memory Repository.

The concrete storage engine backing the memory subsystem. Implements
the ``MemoryPort`` protocol (structurally) over SQLite with FTS5.

Design invariants
-----------------

1. Never blocks the event loop.
   Every public method is ``async`` and delegates to a synchronous
   ``_*_sync`` implementation via ``asyncio.to_thread``. SQLite calls
   are fast (sub-millisecond for indexed queries) but not instant,
   and running them inline would starve the loop under concurrent
   load. This matches the runtime's existing convention (see
   ``peldrun.runtime.store``).

2. Thread-local connections.
   Each worker thread gets its own ``sqlite3.Connection`` via
   ``threading.local``. SQLite connections are NOT thread-safe, so
   sharing one across the thread pool would corrupt state. WAL mode
   makes concurrent readers safe; writes serialize naturally at the
   SQLite level (one writer at a time, brief block).

3. Scope isolation, enforced twice.
   Every read/write that targets an entry verifies the caller's
   ``scope_ref`` MATCHES the entry's stored scope, both by:
     * SQL WHERE clause (fast, index-backed), and
     * explicit ``_parse_scope_ref`` validation (catches malformed
       input before it reaches SQL).

   Defense in depth. A bug in one layer cannot leak another project's
   memories because the other layer still blocks it.

4. Dedupe is atomic via UNIQUE constraint.
   ``UNIQUE(scope_type, scope_id, content_hash)`` is enforced by
   SQLite itself. Attempting a duplicate insert raises
   ``IntegrityError``, which we translate to ``MemoryValidationError``.
   No TOCTOU race: the check and insert are one statement.

5. FTS5 is a JOIN, not a table.
   ``memories_fts`` is external-content: it stores only the inverted
   index, not the row data. The canonical search query starts from
   ``memories`` and joins ``memories_fts`` on ``rowid = memory_pk``.
   This is the shape that ``bm25(memories_fts)`` requires. Direct
   ``SELECT memory_id FROM memories_fts`` fails (no such column) --
   a subtlety we caught in the migrations smoke test.

6. Normalization happens at the boundary.
   ``create`` and ``update`` call ``normalize_for_storage`` on the
   incoming content BEFORE computing the hash. ``search`` calls
   ``normalize_query`` BEFORE the MATCH. Both use the same
   normalizer, so "محمد" stored and "مُحَمَّد" queried normalize to
   the same string -- which is the entire reason search works at
   all in Arabic.

7. Expired entries are excluded in SQL, not in Python.
   ``expires_at`` filtering happens in the WHERE clause so a query
   for 5 results doesn't have to over-fetch (because Python filters
   expired entries out) and under-return. The cost is negligible
   and the correctness guarantee is stronger.

8. Corruption never crashes the caller.
   ``_row_to_entry`` returns ``None`` for rows that cannot be
   reconstructed (bad enum value, malformed JSON in ``tags_json``).
   Callers filter out ``None`` and log a warning. A single corrupt
   row does not take down retrieval -- the alternative (raise and
   fail the whole query) is worse for every user.

9. Schema application is idempotent and once-per-instance.
   ``_get_conn`` calls ``apply_schema`` on first connection. The
   ``_schema_applied`` flag + lock ensures only one thread pays the
   cost. Subsequent threads get a pre-migrated database.

10. FTS drift is auto-recovered.
    On first connection, ``schema_status`` reports whether the
    memories and FTS row counts diverge (a sign of a hard crash
    during a write). If so, ``rebuild_fts`` runs automatically. This
    is acceptance test #8 in the plan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryNotFoundError,
    MemoryProvenance,
    MemoryScope,
    MemoryScopeRef,
    MemorySourceType,
    MemoryStatus,
    MemoryType,
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory.storage.migrations import (
    apply_schema,
    rebuild_fts,
    schema_status,
)
from omweb.memory.storage.normalizer import (
    normalize_for_storage,
    normalize_query,
)


logger = logging.getLogger("omweb.memory.storage.repository")


# ---------------------------------------------------------------------------
# SqliteMemoryRepository
# ---------------------------------------------------------------------------


class SqliteMemoryRepository:
    """Thread-safe SQLite-backed storage for memory entries.

    Satisfies ``MemoryPort`` structurally. Instantiated by
    ``SqliteFtsProvider`` (P1.3) and consumed by ``MemoryService``
    (P1.7). Callers never import this class directly -- they go
    through the provider.

    Lifecycle
    ---------
    * ``__init__`` -- records the DB path; does NOT open a connection
      (startup must be fast).
    * First operation on any thread -- lazy ``_get_conn`` creates the
      thread-local connection, applies the schema (once per
      instance), and returns. Subsequent operations reuse it.
    * ``close_all`` -- closes every connection this instance has
      opened. Called by tests and by the provider's ``close`` (P1.3)
      on shutdown.
    """

    def __init__(self, db_path: Path) -> None:
        """Initialize the repository WITHOUT opening a connection.

        Parameters
        ----------
        db_path
            Path to the SQLite database file. Parent directories are
            created if missing. For in-memory testing, pass a path
            under a ``tempfile.TemporaryDirectory()`` -- this class
            does not support ``:memory:`` (it relies on WAL for
            cross-thread visibility).
        """
        self.db_path: Path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._schema_applied: bool = False
        self._schema_lock = threading.Lock()

        # Track every connection so ``close_all`` can release file
        # handles on Windows. SQLite locks the file while a connection
        # is open; without this, tests cannot delete their temp files.
        self._connections: List[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the current thread's connection, creating it if needed.

        On first call per thread, opens the connection, configures
        pragmas, and ensures the schema is applied (once per
        instance, guarded by ``_schema_lock``). On every call, also
        checks whether an FTS rebuild is needed -- this runs once per
        thread and only when ``schema_status`` reports drift.
        """
        if (
            not hasattr(self._local, "conn")
            or self._local.conn is None
        ):
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")

            # Apply schema once per instance (not once per thread).
            if not self._schema_applied:
                with self._schema_lock:
                    if not self._schema_applied:
                        apply_schema(conn)
                        self._schema_applied = True

                        status = schema_status(conn)
                        if status["needs_rebuild"]:
                            logger.warning(
                                "Memory FTS index drift detected "
                                "(memories=%d, fts=%d). Rebuilding.",
                                status["memories_count"],
                                status["fts_count"],
                            )
                            try:
                                rebuild_fts(conn)
                            except MemoryUnavailableError as exc:
                                logger.error(
                                    "FTS rebuild failed on startup: %s",
                                    exc,
                                )

            with self._connections_lock:
                self._connections.append(conn)
            self._local.conn = conn

        return self._local.conn

    def close_all(self) -> None:
        """Close every connection this instance has opened.

        Required on Windows to release the SQLite file handle (a
        locked file cannot be deleted). Called by:
          * ``MemoryProvider.close`` (P1.3) on shutdown,
          * test teardown after each repository test.

        Safe to call multiple times. Any subsequent operation
        recreates the thread-local connection on demand.
        """
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error as exc:
                    logger.debug("Error closing memory connection: %s", exc)
            self._connections.clear()

        # Clear this thread's reference so the next call rebuilds it.
        if hasattr(self._local, "conn"):
            self._local.conn = None

    # ------------------------------------------------------------------
    # Helpers -- scope and datetime
    # ------------------------------------------------------------------

    def _parse_scope_ref(self, ref: str) -> Tuple[str, str]:
        """Return ``(scope_type_value, scope_id)`` from a canonical ref.

        Delegates to ``MemoryScopeRef.from_string`` so the parsing
        rules are identical to every other consumer -- there is no
        second "how do we split a scope_ref" implementation to drift.

        Raises
        ------
        MemoryValidationError
            When ``ref`` is not a well-formed ``"<type>:<id>"`` pair.
        """
        try:
            parsed = MemoryScopeRef.from_string(ref)
        except ValueError as exc:
            raise MemoryValidationError(
                f"Invalid scope reference: {exc}",
                field_name="scope_ref",
                context={"scope_ref": str(ref)},
                cause=exc,
            ) from exc
        return parsed.scope_type.value, parsed.scope_id

    @staticmethod
    def _dt_to_epoch(dt: Optional[datetime]) -> Optional[float]:
        """Convert an aware/naive ``datetime`` to POSIX epoch seconds.

        Naive datetimes are assumed UTC (matching ``MemoryEntry``'s
        field validator behavior). ``None`` passes through.
        """
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _epoch_to_dt(epoch: Optional[float]) -> Optional[datetime]:
        """Convert POSIX epoch seconds to an aware UTC ``datetime``."""
        if epoch is None:
            return None
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)

    # ------------------------------------------------------------------
    # Helpers -- SQL filter builders
    # ------------------------------------------------------------------

    def _build_scope_filter(
        self, scope_refs: List[str]
    ) -> Tuple[str, List[str]]:
        """Build the ``(scope_type, scope_id) OR (...)`` filter clause.

        Returns a clause and its bound parameters. When
        ``scope_refs`` is empty, returns a clause that matches
        nothing (``1=0``) -- an empty scope set is a legitimate
        "search nothing" request, not an error.
        """
        if not scope_refs:
            return "1=0", []

        clauses: List[str] = []
        params: List[str] = []
        for ref in scope_refs:
            scope_type, scope_id = self._parse_scope_ref(ref)
            clauses.append("(scope_type = ? AND scope_id = ?)")
            params.extend([scope_type, scope_id])
        return "(" + " OR ".join(clauses) + ")", params

    def _build_status_filter(
        self, status: Optional[Any]
    ) -> Tuple[str, List[str]]:
        """Build the status filter clause.

        ``status=None`` or ``"active"`` selects only retrievable
        entries (the default for search). ``status="all"`` disables
        the filter (used by the UI's "show archived" toggle).
        Any other value is passed through as a literal status.
        """
        if status is None or status == "active":
            return "status = ?", [MemoryStatus.ACTIVE.value]
        if status == "all":
            return "1=1", []
        # Accept both enum members and their string values.
        status_value = (
            status.value if hasattr(status, "value") else str(status)
        )
        return "status = ?", [status_value]

    def _build_expiry_filter(self) -> str:
        """Return a clause that excludes expired entries.

        ``expires_at`` is nullable; ``NULL`` means "no expiration".
        An entry is expired iff ``expires_at IS NOT NULL AND
        expires_at <= now``. We invert that as the WHERE clause.
        """
        return "(expires_at IS NULL OR expires_at > strftime('%s','now'))"

    def _build_optional_filters(
        self, filters: Dict[str, Any]
    ) -> Tuple[List[str], List[Any]]:
        """Build type / pinned / tags filters from ``**filters``.

        Returns a list of ``"AND ..."`` clauses (or empty strings
        for unused filters, which are simply dropped) and their
        bound parameters.
        """
        clauses: List[str] = []
        params: List[Any] = []

        memory_type = filters.get("memory_type")
        if memory_type:
            type_value = (
                memory_type.value
                if hasattr(memory_type, "value")
                else str(memory_type)
            )
            clauses.append("memory_type = ?")
            params.append(type_value)

        is_pinned = filters.get("is_pinned")
        if is_pinned is not None:
            clauses.append("is_pinned = ?")
            params.append(1 if is_pinned else 0)

        tags = filters.get("tags")
        if tags and isinstance(tags, (list, tuple)):
            for tag in tags:
                # JSON is text; the tag is stored as a quoted string
                # inside the array. LIKE with surrounding quotes
                # matches only full-tag hits (not "dark" matching
                # "darkish" inside a stored tag "darkish").
                clauses.append("tags_json LIKE ?")
                params.append(f'%"{str(tag).strip().lower()}"%')

        return clauses, params

    # ------------------------------------------------------------------
    # Helpers -- row <-> entry conversion
    # ------------------------------------------------------------------

    def _entry_to_params(self, entry: MemoryEntry) -> Tuple[Any, ...]:
        """Convert a ``MemoryEntry`` to the INSERT parameter tuple.

        Field order MUST match the ``INSERT`` statement in
        ``_create_sync``. Both places carry a comment listing the
        order so a future edit to one obviously needs the other.
        """
        return (
            entry.memory_id,
            entry.memory_type.value,
            entry.scope_type.value,
            entry.scope_id,
            entry.content,
            entry.normalized_content,
            json.dumps(entry.tags, ensure_ascii=False),
            entry.importance,
            entry.confidence,
            entry.status.value,
            1 if entry.is_pinned else 0,
            entry.provenance.source_type.value,
            entry.provenance.source_chat_id,
            entry.provenance.source_run_id,
            entry.provenance.source_turn_id,
            entry.provenance.imported_from,
            entry.content_hash,
            self._dt_to_epoch(entry.created_at),
            self._dt_to_epoch(entry.updated_at),
            self._dt_to_epoch(entry.last_used_at),
            self._dt_to_epoch(entry.expires_at),
        )

    def _row_to_entry(self, row: sqlite3.Row) -> Optional[MemoryEntry]:
        """Convert a ``sqlite3.Row`` to a ``MemoryEntry`` or ``None``.

        Returns ``None`` (with a logged warning) when the row's
        stored values cannot be reconstructed -- a corrupt enum
        value, malformed ``tags_json``, or a missing required field.
        Callers filter ``None`` and continue serving the rest of the
        result set. One corrupt row does not break retrieval.
        """
        try:
            tags_raw = row["tags_json"]
            tags = json.loads(tags_raw) if tags_raw else []
            if not isinstance(tags, list):
                tags = []

            return MemoryEntry(
                memory_id=row["memory_id"],
                memory_type=MemoryType(row["memory_type"]),
                scope_type=MemoryScope(row["scope_type"]),
                scope_id=row["scope_id"],
                content=row["content"],
                normalized_content=row["normalized_content"],
                tags=tags,
                importance=int(row["importance"]),
                confidence=float(row["confidence"]),
                status=MemoryStatus(row["status"]),
                is_pinned=bool(row["is_pinned"]),
                provenance=MemoryProvenance(
                    source_type=MemorySourceType(row["source_type"]),
                    source_chat_id=row["source_chat_id"],
                    source_run_id=row["source_run_id"],
                    source_turn_id=row["source_turn_id"],
                    imported_from=row["imported_from"],
                ),
                created_at=self._epoch_to_dt(row["created_at"]),
                updated_at=self._epoch_to_dt(row["updated_at"]),
                last_used_at=self._epoch_to_dt(row["last_used_at"]),
                expires_at=self._epoch_to_dt(row["expires_at"]),
                content_hash=row["content_hash"],
            )
        except Exception as exc:
            logger.warning(
                "Skipping corrupt memory row (memory_id=%r): %s",
                dict(row).get("memory_id", "<unknown>"),
                exc,
            )
            return None

    def _rows_to_entries(
        self, rows: List[sqlite3.Row]
    ) -> List[MemoryEntry]:
        """Convert rows, dropping any that cannot be reconstructed."""
        entries = [self._row_to_entry(r) for r in rows]
        return [e for e in entries if e is not None]

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a new entry. Raises on duplicate content in the same scope."""
        return await asyncio.to_thread(self._create_sync, entry)

    def _create_sync(self, entry: MemoryEntry) -> MemoryEntry:
        conn = self._get_conn()

        # Re-normalize: the caller may have built the entry via
        # ``MemoryEntry.create()`` which uses a naive normalization.
        # The canonical normalizer lives here so every stored row has
        # the same hash a search would compute.
        normalized = normalize_for_storage(entry.content)
        entry = entry.with_normalized_content(normalized)

        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO memories (
                        memory_id, memory_type, scope_type, scope_id,
                        content, normalized_content, tags_json,
                        importance, confidence, status, is_pinned,
                        source_type, source_chat_id, source_run_id,
                        source_turn_id, imported_from,
                        content_hash, created_at, updated_at,
                        last_used_at, expires_at
                    ) VALUES (
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?
                    )
                    """,
                    self._entry_to_params(entry),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint" in str(exc):
                raise MemoryValidationError(
                    (
                        f"Duplicate memory: content already exists "
                        f"in scope "
                        f"{entry.scope_type.value}:{entry.scope_id}"
                    ),
                    field_name="content",
                    context={
                        "content_hash": entry.content_hash,
                        "scope_type": entry.scope_type.value,
                        "scope_id": entry.scope_id,
                    },
                    cause=exc,
                ) from exc
            raise MemoryUnavailableError(
                f"Database constraint violation during create: {exc}",
                context={"memory_id": entry.memory_id},
                cause=exc,
            ) from exc
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during create: {exc}",
                context={"memory_id": entry.memory_id},
                cause=exc,
            ) from exc

        return entry

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    async def get(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        """Fetch one entry restricted to ``scope_ref``. ``None`` if missing."""
        return await asyncio.to_thread(self._get_sync, memory_id, scope_ref)

    def _get_sync(
        self, memory_id: str, scope_ref: str
    ) -> Optional[MemoryEntry]:
        conn = self._get_conn()
        scope_type, scope_id = self._parse_scope_ref(scope_ref)

        try:
            row = conn.execute(
                """
                SELECT * FROM memories
                WHERE memory_id = ?
                  AND scope_type = ?
                  AND scope_id = ?
                """,
                (memory_id, scope_type, scope_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during get: {exc}",
                context={"memory_id": memory_id},
                cause=exc,
            ) from exc

        if row is None:
            return None
        return self._row_to_entry(row)

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Full-text search restricted to ``scope_refs``.

        When ``query`` normalizes to an empty string (only whitespace
        or punctuation), falls back to a recency-ordered listing.
        Empty ``scope_refs`` returns ``[]``.
        """
        return await asyncio.to_thread(
            self._search_sync, query, scope_refs, limit, filters
        )

    async def search_ranked(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Full-text search, returning each entry with its relevance.

        Companion to ``search`` that exposes the BM25-derived
        relevance that ``search`` discards. Used exclusively by
        the ranker (P1.4) -- not part of the ``MemoryPort``
        protocol, since the protocol's callers do not need
        numeric relevance.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            ``(entry, relevance)`` pairs in BM25-descending order.
            ``relevance`` is a normalized float in ``[0.0, 1.0]``
            (1.0 = best match in this result set, 0.0 = worst).
            Empty list when nothing matches.
        """
        return await asyncio.to_thread(
            self._search_ranked_sync, query, scope_refs, limit, filters
        )

    def _search_sync(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
        filters: Dict[str, Any],
    ) -> List[MemoryEntry]:
        """Search entries in scopes, dropping relevance scores.

        Thin wrapper over ``_search_ranked_sync`` that strips the
        BM25-derived scores. Satisfies the ``MemoryPort.search``
        protocol contract (which returns bare ``MemoryEntry``
        objects). Callers that need the scores use
        ``search_ranked`` / ``_search_ranked_sync`` directly.
        """
        ranked = self._search_ranked_sync(
            query, scope_refs, limit, filters
        )
        return [entry for entry, _score in ranked]

    def _search_ranked_sync(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
        filters: Dict[str, Any],
    ) -> List[Tuple[MemoryEntry, float]]:
        """Search entries, returning each paired with its relevance.

        Relevance is a normalized float in ``[0.0, 1.0]`` where
        1.0 is the best match for this specific query and 0.0 is
        the weakest match in the returned set. Normalization is
        min-max over the RESULT SET, not absolute -- SQLite's
        BM25 values are negative and unbounded, so absolute
        normalization would be misleading. For ranking (the
        ranker's purpose) and for threshold filtering
        (``min_relevance_score``) relative normalization is the
        correct choice: a result set where the best match scores
        1.0 and the worst scores 0.0 filters identically to an
        absolute threshold.

        Empty-query fallback: when the normalized query is empty
        (whitespace, punctuation only), returns the recency
        listing with linearly decaying position-based scores
        (1.0, 1.0 - 1/n, 1.0 - 2/n, ...). This matches the
        caller's intent: "show me recent entries" has no
        relevance signal, so a positional proxy is honest.

        Ordering: identical to ``_search_sync`` -- BM25 ascending
        (most negative = best), then importance DESC, then
        last_used_at DESC.
        """
        if limit <= 0:
            return []

        scope_clause, scope_params = self._build_scope_filter(scope_refs)
        if not scope_params:
            return []

        normalized_query = normalize_query(query)

        # ----- Empty-query fallback: recency listing + position scores -----
        if not normalized_query:
            recent = self._list_recent_sync(
                scope_clause, scope_params, limit, filters
            )
            n = len(recent)
            if n == 0:
                return []
            # Position-based score: index 0 -> 1.0, index n-1 -> 1/n.
            # The 1/n floor (rather than 0.0) keeps the worst entry
            # above the typical ``min_relevance_score=0.3`` cutoff
            # when the result set is small, preserving the "show me
            # recents" intent.
            return [
                (entry, 1.0 - (i / n))
                for i, entry in enumerate(recent)
            ]

        # ----- Normal FTS query -----
        status_clause, status_params = self._build_status_filter(
            filters.get("status")
        )
        optional_clauses, optional_params = self._build_optional_filters(
            filters
        )
        optional_sql = (
            " AND " + " AND ".join(optional_clauses)
            if optional_clauses
            else ""
        )

        sql = f"""
            SELECT m.*, bm25(memories_fts) AS rank
            FROM memories m
            JOIN memories_fts f ON f.rowid = m.memory_pk
            WHERE memories_fts MATCH ?
              AND {scope_clause}
              AND {status_clause}
              AND {self._build_expiry_filter()}
              {optional_sql}
            ORDER BY rank ASC, m.importance DESC, m.last_used_at DESC
            LIMIT ?
        """

        params: List[Any] = (
            [normalized_query]
            + scope_params
            + status_params
            + optional_params
            + [limit]
        )

        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # FTS5 MATCH can fail for inputs that survive our
            # normalizer but still confuse the tokenizer
            # (pathological inputs like extremely long unbroken
            # tokens). Degrade to "no results" rather than
            # failing the run.
            logger.warning(
                "FTS search failed for normalized query %r: %s",
                normalized_query[:100],
                exc,
            )
            return []
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during search: {exc}",
                context={"query": normalized_query[:100]},
                cause=exc,
            ) from exc

        if not rows:
            return []

        # ----- Normalize BM25 to a magnitude-relative [0, 1] scale -----
        #
        # SQLite's bm25() returns NEGATIVE floats; more negative =
        # better match. abs(bm25) is therefore the "magnitude" of
        # relevance: larger magnitude = stronger match.
        #
        # We normalize by the MAXIMUM magnitude in the result set.
        # The best match always scores 1.0; a weaker match scores
        # its magnitude divided by the best's magnitude -- a
        # stable, absolute-ish ratio that does NOT unconditionally
        # floor the worst entry at 0.0.
        #
        # Why not min-max: min-max always maps the WORST entry to
        # 0.0, regardless of how good it actually is. With the
        # default ``min_relevance_score=0.3``, that would drop the
        # bottom (roughly) 30% of EVERY result set -- including
        # legitimate matches in small, all-good result sets. Real-
        # world example: query "python" returns 3 matches, min-max
        # scores them [1.0, 0.0, 0.0], and 2 valid matches get
        # silently dropped.
        #
        # Magnitude ratio preserves relative relevance without
        # this floor-at-zero artifact:
        #
        #   raw scores [-5, -3, -3]
        #     min-max gives   [1.0, 0.0, 0.0]   (drops 2 of 3)
        #     magnitude gives [1.0, 0.6, 0.6]   (keeps all 3)
        #
        #   raw scores [-10, -1]
        #     min-max gives   [1.0, 0.0]        (drops 1 of 2)
        #     magnitude gives [1.0, 0.1]        (drops 1 of 2 - weak)
        magnitudes = [abs(float(r["rank"])) for r in rows]
        max_mag = max(magnitudes) if magnitudes else 0.0

        result: List[Tuple[MemoryEntry, float]] = []
        for row, mag in zip(rows, magnitudes):
            entry = self._row_to_entry(row)
            if entry is None:
                continue
            if max_mag > 0.0:
                normalized = mag / max_mag
            else:
                # Defensive: BM25 should never be 0 for an FTS
                # MATCH hit, but if the tokenizer produces no
                # usable tokens the score can degenerate. Treat
                # all such rows as equally (fully) relevant.
                normalized = 1.0
            # Clamp defensively; floating-point arithmetic on
            # extremes can produce 1.0000000001 or slightly
            # negative values.
            normalized = max(0.0, min(1.0, normalized))
            result.append((entry, normalized))
        return result

    def _list_recent_sync(
        self,
        scope_clause: str,
        scope_params: List[str],
        limit: int,
        filters: Dict[str, Any],
    ) -> List[MemoryEntry]:
        """Recency-ordered fallback for empty-text queries."""
        status_clause, status_params = self._build_status_filter(
            filters.get("status")
        )
        optional_clauses, optional_params = self._build_optional_filters(
            filters
        )
        optional_sql = (
            " AND " + " AND ".join(optional_clauses)
            if optional_clauses
            else ""
        )

        sql = f"""
            SELECT * FROM memories
            WHERE {scope_clause}
              AND {status_clause}
              AND {self._build_expiry_filter()}
              {optional_sql}
            ORDER BY
                importance DESC,
                COALESCE(last_used_at, 0) DESC,
                created_at DESC
            LIMIT ?
        """

        params: List[Any] = (
            scope_params + status_params + optional_params + [limit]
        )

        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during recency listing: {exc}",
                cause=exc,
            ) from exc

        return self._rows_to_entries(rows)

    # ------------------------------------------------------------------
    # list_by_scope (UI page)
    # ------------------------------------------------------------------

    async def list_by_scope(
        self,
        scope_ref: str,
        limit: int = 50,
        offset: int = 0,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Return entries in a single scope, newest/most important first.

        Used by the management UI (P1.8). Unlike ``search``, this
        does not apply a text filter and, by default, includes every
        status (the UI's list view wants to show archived entries
        too). Pass ``status="active"`` to restrict.
        """
        return await asyncio.to_thread(
            self._list_by_scope_sync, scope_ref, limit, offset, filters
        )

    def _list_by_scope_sync(
        self,
        scope_ref: str,
        limit: int,
        offset: int,
        filters: Dict[str, Any],
    ) -> List[MemoryEntry]:
        if limit <= 0:
            return []

        scope_type, scope_id = self._parse_scope_ref(scope_ref)

        # Default for the management UI is "all statuses"; the caller
        # can override by passing ``status="active"`` explicitly.
        status_filter = filters.get("status", "all")
        status_clause, status_params = self._build_status_filter(
            status_filter
        )

        optional_clauses, optional_params = self._build_optional_filters(
            filters
        )
        optional_sql = (
            " AND " + " AND ".join(optional_clauses)
            if optional_clauses
            else ""
        )

        sql = f"""
            SELECT * FROM memories
            WHERE scope_type = ?
              AND scope_id = ?
              AND {status_clause}
              {optional_sql}
            ORDER BY
                is_pinned DESC,
                importance DESC,
                updated_at DESC
            LIMIT ? OFFSET ?
        """

        params: List[Any] = (
            [scope_type, scope_id]
            + status_params
            + optional_params
            + [limit, offset]
        )

        conn = self._get_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during list_by_scope: {exc}",
                context={"scope_ref": scope_ref},
                cause=exc,
            ) from exc

        return self._rows_to_entries(rows)

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist an update to an existing entry.

        Raises ``MemoryNotFoundError`` when the entry is missing OR
        belongs to a different scope. The two cases are deliberately
        indistinguishable at this layer (information disclosure
        prevention -- see ``MemoryNotFoundError`` docstring).
        """
        return await asyncio.to_thread(self._update_sync, entry)

    def _update_sync(self, entry: MemoryEntry) -> MemoryEntry:
        conn = self._get_conn()

        # Re-normalize (content may have changed) and recompute hash.
        normalized = normalize_for_storage(entry.content)
        entry = entry.with_normalized_content(normalized)
        entry = entry.with_updated_timestamp()

        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE memories SET
                        memory_type        = ?,
                        content            = ?,
                        normalized_content = ?,
                        tags_json          = ?,
                        importance         = ?,
                        confidence         = ?,
                        status             = ?,
                        is_pinned          = ?,
                        content_hash       = ?,
                        updated_at         = ?,
                        expires_at         = ?
                    WHERE memory_id = ?
                      AND scope_type = ?
                      AND scope_id = ?
                    """,
                    (
                        entry.memory_type.value,
                        entry.content,
                        entry.normalized_content,
                        json.dumps(entry.tags, ensure_ascii=False),
                        entry.importance,
                        entry.confidence,
                        entry.status.value,
                        1 if entry.is_pinned else 0,
                        entry.content_hash,
                        self._dt_to_epoch(entry.updated_at),
                        self._dt_to_epoch(entry.expires_at),
                        entry.memory_id,
                        entry.scope_type.value,
                        entry.scope_id,
                    ),
                )

                if cursor.rowcount == 0:
                    raise MemoryNotFoundError(
                        entry.memory_id,
                        message=(
                            f"Memory not found or outside caller's "
                            f"scope: {entry.memory_id}"
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint" in str(exc):
                raise MemoryValidationError(
                    (
                        f"Update would create a duplicate in scope "
                        f"{entry.scope_type.value}:{entry.scope_id}"
                    ),
                    field_name="content",
                    context={"content_hash": entry.content_hash},
                    cause=exc,
                ) from exc
            raise MemoryUnavailableError(
                f"Database constraint violation during update: {exc}",
                context={"memory_id": entry.memory_id},
                cause=exc,
            ) from exc
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during update: {exc}",
                context={"memory_id": entry.memory_id},
                cause=exc,
            ) from exc

        return entry

    # ------------------------------------------------------------------
    # delete (soft) + purge (hard)
    # ------------------------------------------------------------------

    async def delete(self, memory_id: str, scope_ref: str) -> bool:
        """Soft-delete: set ``status=archived``.

        Returns ``True`` when an entry was present and is now
        archived, ``False`` when no entry matched or it was already
        archived. Idempotent on already-archived entries (returns
        ``False`` -- nothing changed).
        """
        return await asyncio.to_thread(
            self._delete_sync, memory_id, scope_ref
        )

    def _delete_sync(self, memory_id: str, scope_ref: str) -> bool:
        conn = self._get_conn()
        scope_type, scope_id = self._parse_scope_ref(scope_ref)
        now = time.time()

        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET status = ?, updated_at = ?
                    WHERE memory_id = ?
                      AND scope_type = ?
                      AND scope_id = ?
                      AND status != ?
                    """,
                    (
                        MemoryStatus.ARCHIVED.value,
                        now,
                        memory_id,
                        scope_type,
                        scope_id,
                        MemoryStatus.ARCHIVED.value,
                    ),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during delete: {exc}",
                context={"memory_id": memory_id},
                cause=exc,
            ) from exc

    async def purge(self, memory_id: str, scope_ref: str) -> bool:
        """Hard delete: remove the row (and its FTS entry via trigger).

        Separate from ``delete`` so the intent is explicit and the
        API layer (P1.8) can choose. The FTS delete trigger fires
        automatically, so the search index is cleaned up with no
        extra work from this method.
        """
        return await asyncio.to_thread(
            self._purge_sync, memory_id, scope_ref
        )

    def _purge_sync(self, memory_id: str, scope_ref: str) -> bool:
        conn = self._get_conn()
        scope_type, scope_id = self._parse_scope_ref(scope_ref)

        try:
            with conn:
                cursor = conn.execute(
                    """
                    DELETE FROM memories
                    WHERE memory_id = ?
                      AND scope_type = ?
                      AND scope_id = ?
                    """,
                    (memory_id, scope_type, scope_id),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during purge: {exc}",
                context={"memory_id": memory_id},
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # mark_used
    # ------------------------------------------------------------------

    async def mark_used(self, memory_ids: List[str]) -> None:
        """Bulk-update ``last_used_at`` for the given entries.

        Silent no-op for an empty list. Idempotent: running twice
        produces the same end state (with the second
        ``last_used_at`` being newer, which is correct).

        Bulk semantics matter: a per-entry update would cost N
        round-trips per retrieval, and retrieval happens on every
        turn. The single UPDATE ... WHERE memory_id IN (...) keeps
        the operation O(1) in round-trips.
        """
        if not memory_ids:
            return
        await asyncio.to_thread(self._mark_used_sync, memory_ids)

    def _mark_used_sync(self, memory_ids: List[str]) -> None:
        conn = self._get_conn()
        now = time.time()
        placeholders = ",".join("?" for _ in memory_ids)

        try:
            with conn:
                conn.execute(
                    f"""
                    UPDATE memories
                    SET last_used_at = ?
                    WHERE memory_id IN ({placeholders})
                    """,
                    [now] + list(memory_ids),
                )
        except sqlite3.Error as exc:
            logger.warning(
                "Failed to update last_used_at for %d entries: %s",
                len(memory_ids),
                exc,
            )

    # ------------------------------------------------------------------
    # Project aggregation (P1.8 memory_projects.py)
    # ------------------------------------------------------------------

    async def list_project_summaries(self) -> List[Dict[str, Any]]:
        """Return one summary row per project scope that has memories.

        Used by ``GET /api/memory/projects`` (P1.8). Aggregates via
        a single SQL GROUP BY -- O(distinct project count) rows,
        not O(total entries).
        """
        return await asyncio.to_thread(self._list_project_summaries_sync)

    def _list_project_summaries_sync(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        sql = """
        SELECT scope_id,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END) AS archived,
               SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded,
               MAX(updated_at) AS last_updated
        FROM memories
        WHERE scope_type = 'project'
        GROUP BY scope_id
        ORDER BY total DESC, scope_id ASC
        """
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during project summary: {exc}",
                cause=exc,
            ) from exc

        return [
            {
                "project_id": str(r["scope_id"]),
                "total": int(r["total"]),
                "active": int(r["active"]),
                "archived": int(r["archived"]),
                "superseded": int(r["superseded"]),
                "last_updated": (
                    float(r["last_updated"])
                    if r["last_updated"] is not None
                    else None
                ),
            }
            for r in rows
        ]

    async def get_project_stats(self, scope_id: str) -> Dict[str, Any]:
        """Return detailed stats for one project scope.

        Aggregates counts, importance range, type distribution, and
        top tags in SQL + one small Python pass over the tags JSON
        (tags are stored as JSON arrays and cannot be aggregated in
        vanilla SQLite without a JSON1 extension load).
        """
        return await asyncio.to_thread(
            self._get_project_stats_sync, scope_id
        )

    def _get_project_stats_sync(self, scope_id: str) -> Dict[str, Any]:
        conn = self._get_conn()

        try:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END) AS archived,
                    SUM(CASE WHEN status='superseded' THEN 1 ELSE 0 END) AS superseded,
                    MIN(importance) AS min_imp,
                    MAX(importance) AS max_imp,
                    AVG(importance) AS avg_imp,
                    MIN(created_at) AS oldest,
                    MAX(created_at) AS newest,
                    MAX(updated_at) AS last_updated
                FROM memories
                WHERE scope_type='project' AND scope_id=?
                """,
                (scope_id,),
            ).fetchone()

            type_rows = conn.execute(
                """
                SELECT memory_type, COUNT(*) AS cnt FROM memories
                WHERE scope_type='project' AND scope_id=?
                  AND status='active'
                GROUP BY memory_type ORDER BY cnt DESC
                """,
                (scope_id,),
            ).fetchall()

            tag_rows = conn.execute(
                """
                SELECT tags_json FROM memories
                WHERE scope_type='project' AND scope_id=?
                  AND status='active'
                """,
                (scope_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during project stats: {exc}",
                cause=exc,
            ) from exc

        tag_counts: Dict[str, int] = {}
        for r in tag_rows:
            try:
                tags = json.loads(r["tags_json"] or "[]")
                if isinstance(tags, list):
                    for tag in tags:
                        if isinstance(tag, str):
                            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except Exception:
                pass
        top_tags = sorted(
            ({"tag": k, "count": v} for k, v in tag_counts.items()),
            key=lambda x: (-x["count"], x["tag"]),
        )[:20]

        return {
            "project_id": scope_id,
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "archived": int(row["archived"] or 0),
            "superseded": int(row["superseded"] or 0),
            "importance": {
                "min": int(row["min_imp"]) if row["min_imp"] is not None else None,
                "max": int(row["max_imp"]) if row["max_imp"] is not None else None,
                "avg": (
                    round(float(row["avg_imp"]), 2)
                    if row["avg_imp"] is not None
                    else None
                ),
            },
            "created_range": {
                "oldest": row["oldest"],
                "newest": row["newest"],
            },
            "last_updated": row["last_updated"],
            "types": [
                {"memory_type": str(t["memory_type"]), "count": int(t["cnt"])}
                for t in type_rows
            ],
            "top_tags": top_tags,
        }

    async def bulk_archive(self, scope_id: str) -> int:
        """Archive all ACTIVE memories in one project. Returns count."""
        return await asyncio.to_thread(self._bulk_archive_sync, scope_id)

    def _bulk_archive_sync(self, scope_id: str) -> int:
        conn = self._get_conn()
        now = time.time()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET status = 'archived', updated_at = ?
                    WHERE scope_type='project' AND scope_id=?
                      AND status='active'
                    """,
                    (now, scope_id),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during bulk archive: {exc}",
                cause=exc,
            ) from exc

    async def bulk_purge(self, scope_id: str) -> int:
        """Hard-delete all memories in one project. Returns count.

        DESTRUCTIVE -- irreversible. The API layer is responsible
        for requiring explicit confirmation before invoking this.
        The FTS delete trigger cleans up the index automatically.
        """
        return await asyncio.to_thread(self._bulk_purge_sync, scope_id)

    def _bulk_purge_sync(self, scope_id: str) -> int:
        conn = self._get_conn()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    DELETE FROM memories
                    WHERE scope_type='project' AND scope_id=?
                    """,
                    (scope_id,),
                )
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise MemoryUnavailableError(
                f"Database error during bulk purge: {exc}",
                cause=exc,
            ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["SqliteMemoryRepository"]
