# backend/omweb/memory/storage/migrations.py
"""
PELDRUN Memory Platform - Storage Schema and Idempotent Migrations.

Single source of truth for the SQL schema that backs the memory
subsystem, together with the migration runner that brings an empty
or older database up to ``CURRENT_VERSION``.

Design invariants
-----------------

1. Additive-only migrations.
   Every change is expressed as ``CREATE ... IF NOT EXISTS`` or
   ``ALTER TABLE ... ADD COLUMN``. Destructive operations (DROP,
   TRUNCATE, column renames) are FORBIDDEN here. When a genuinely
   destructive change becomes necessary, it lives in a NEW
   migration with an explicit backup step -- never as an edit to
   an existing one. Historical databases must be able to walk
   forward without losing data.

2. Version tracking is monotonic.
   ``memory_schema_version`` records each applied version as an
   INSERT-only row. Reading the highest row gives the current
   version. There is no rollback path -- downgrades require a
   fresh restore from backup (documented in P1.7's operator
   guide).

3. External-content FTS5.
   ``memories_fts`` uses ``content='memories'`` so the FTS index
   stores only the inverted index, not a duplicate copy of the
   content. This roughly halves the on-disk footprint for a
   memory-heavy deployment. Every INSERT / UPDATE / DELETE on
   ``memories`` is mirrored into FTS via triggers.

4. The UPDATE trigger has a ``WHEN`` guard.
   The plan's reference spec fires the FTS rebuild on EVERY update
   to ``memories``, including the extremely frequent bulk
   ``mark_used`` calls (one per retrieval, updating N rows) and
   status changes. That design would:
     * rewrite every FTS entry every time an entry is retrieved,
     * grow the WAL by megabytes per hour on an active deployment,
     * create a real risk of FTS drift if a trigger fails midway.
   The ``WHEN new.content != old.content OR new.normalized_content
   != old.normalized_content`` guard skips the rebuild when the
   indexed columns are unchanged, so FTS work is proportional to
   ACTUAL content changes -- not to retrieval frequency.

5. Unicode-aware tokenization.
   ``tokenize='unicode61 remove_diacritics 2'`` gives us:
     * correct token boundaries for Arabic, Russian, and English
       without an external ICU dependency,
     * diacritic folding (Arabic harakat, Cyrillic accents),
     * case folding (``unicode61`` lowercases internally).
   The companion ``normalizer.py`` applies additional,
   language-specific folding BEFORE insert, so FTS sees the
   already-normalized form alongside the raw content.

6. Idempotent application.
   ``apply_schema(conn)`` is safe to call on:
     * an empty database (creates everything, records v1),
     * a database already at ``CURRENT_VERSION`` (no-op),
     * a database at an older version (applies the pending
       migrations in order).
   The migration body uses ``executescript`` -- the fast path --
   and version recording uses an explicit INSERT. A crash between
   the two leaves the schema created but the version
   unrecorded; the next call re-applies the (idempotent) DDL and
   records the version correctly. No data is lost, no drift
   occurs.

7. FTS5 availability is a HARD dependency.
   Python ships with FTS5 since 3.11, but custom builds and some
   Linux distributions compile it out. ``apply_schema`` detects
   ``OperationalError('no such module: fts5')`` and raises
   ``MemoryUnavailableError`` with a specific, actionable
   message. Silently proceeding without FTS would make
   ``search`` return nothing forever with no diagnostic signal.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List, Tuple

from peldrun.memory.domain.errors import MemoryUnavailableError


logger = logging.getLogger("omweb.memory.storage.migrations")


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


CURRENT_VERSION: int = 1
"""Highest schema version this module knows how to produce.

Bump only when adding a new migration. Every bump must be paired
with an entry in ``_MIGRATIONS`` below.
"""


# ---------------------------------------------------------------------------
# DDL -- version table (always created first, before version check)
# ---------------------------------------------------------------------------


_VERSION_TABLE_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at REAL    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# DDL -- v1 (the baseline schema)
# ---------------------------------------------------------------------------
#
# Every statement below is idempotent (``IF NOT EXISTS``). This is
# what makes re-running a partially-applied migration safe.

_V1_MEMORIES_TABLE_DDL: str = """
CREATE TABLE IF NOT EXISTS memories (
    memory_pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id          TEXT    NOT NULL UNIQUE,
    memory_type        TEXT    NOT NULL,
    scope_type         TEXT    NOT NULL,
    scope_id           TEXT    NOT NULL,
    content            TEXT    NOT NULL,
    normalized_content TEXT    NOT NULL,
    tags_json          TEXT    NOT NULL DEFAULT '[]',
    importance         INTEGER NOT NULL DEFAULT 50,
    confidence         REAL    NOT NULL DEFAULT 1.0,
    status             TEXT    NOT NULL DEFAULT 'active',
    is_pinned          INTEGER NOT NULL DEFAULT 0,
    source_type        TEXT    NOT NULL,
    source_chat_id     TEXT,
    source_run_id      TEXT,
    source_turn_id     TEXT,
    imported_from      TEXT,
    content_hash       TEXT    NOT NULL,
    created_at         REAL    NOT NULL,
    updated_at         REAL    NOT NULL,
    last_used_at       REAL,
    expires_at         REAL,
    UNIQUE(scope_type, scope_id, content_hash)
);
"""

_V1_MEMORIES_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope_type, scope_id, status, importance DESC);

CREATE INDEX IF NOT EXISTS idx_memories_used
    ON memories(last_used_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_type
    ON memories(memory_type, status);

CREATE INDEX IF NOT EXISTS idx_memories_pinned
    ON memories(is_pinned, status)
    WHERE is_pinned = 1;
"""

_V1_FTS_TABLE_DDL: str = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    normalized_content,
    content='memories',
    content_rowid='memory_pk',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_V1_TRIGGERS_DDL: str = """
CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories
BEGIN
    INSERT INTO memories_fts(rowid, content, normalized_content)
    VALUES (new.memory_pk, new.content, new.normalized_content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories
BEGIN
    INSERT INTO memories_fts(
        memories_fts, rowid, content, normalized_content
    )
    VALUES (
        'delete',
        old.memory_pk,
        old.content,
        old.normalized_content
    );
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE ON memories
WHEN new.content != old.content
  OR new.normalized_content != old.normalized_content
BEGIN
    INSERT INTO memories_fts(
        memories_fts, rowid, content, normalized_content
    )
    VALUES (
        'delete',
        old.memory_pk,
        old.content,
        old.normalized_content
    );
    INSERT INTO memories_fts(rowid, content, normalized_content)
    VALUES (new.memory_pk, new.content, new.normalized_content);
END;
"""


# ---------------------------------------------------------------------------
# Migration v1 -- full function body
# ---------------------------------------------------------------------------


def _apply_v1(conn: sqlite3.Connection) -> None:
    """Apply the baseline schema (v1).

    Every DDL statement in this function uses ``CREATE ... IF NOT
    EXISTS``. Re-running on a partially-created schema is a no-op
    for the tables that already exist and completes the missing
    ones. This is what makes the whole migration crash-safe without
    true transactions (see the module docstring's note on
    ``executescript``).
    """
    conn.executescript(_V1_MEMORIES_TABLE_DDL)
    conn.executescript(_V1_MEMORIES_INDEXES_DDL)
    conn.executescript(_V1_FTS_TABLE_DDL)
    conn.executescript(_V1_TRIGGERS_DDL)


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------
#
# (target_version, apply_function). Ordered ascending. Adding a new
# migration means:
#
#   1. Add ``_apply_v<N>`` above.
#   2. Append ``(N, _apply_v<N>)`` here.
#   3. Bump ``CURRENT_VERSION`` to N.
#   4. Update ``tests/memory/test_repository.py`` to exercise the
#      new migration if it changes any user-visible behavior.

_MIGRATIONS: List[Tuple[int, Any]] = [
    (1, _apply_v1),
]


# ---------------------------------------------------------------------------
# Public API -- apply
# ---------------------------------------------------------------------------


def _get_recorded_version(conn: sqlite3.Connection) -> int:
    """Return the highest recorded schema version, or 0 if none."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM memory_schema_version"
        ).fetchone()
    except sqlite3.OperationalError:
        # The version table does not exist yet -- fresh database.
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    """Record ``version`` as applied. Idempotent via INSERT OR IGNORE."""
    conn.execute(
        "INSERT OR IGNORE INTO memory_schema_version "
        "(version, applied_at) VALUES (?, ?)",
        (version, time.time()),
    )


def apply_schema(conn: sqlite3.Connection) -> int:
    """Ensure ``conn``'s database is at ``CURRENT_VERSION``.

    Idempotent: safe to call on every startup, on an empty database,
    on a partially-migrated database, and on a database already at
    ``CURRENT_VERSION``.

    Parameters
    ----------
    conn
        An OPEN SQLite connection. The caller (``SqliteMemoryRepository``)
        owns transaction scope, pragmas, and thread-local setup.

    Returns
    -------
    int
        The schema version AFTER this call. Equal to
        ``CURRENT_VERSION`` on success.

    Raises
    ------
    MemoryUnavailableError
        When FTS5 is not compiled into the running SQLite library
        (``OperationalError('no such module: fts5')``). This is a
        deployment-level problem the operator must fix before the
        memory subsystem can function.
    sqlite3.Error
        For any other SQL-level failure (corrupt file, permission
        denied, disk full). The caller (repository) wraps these in
        ``MemoryUnavailableError`` with a retry hint.
    """
    # 1. Ensure the version-tracking table exists. This is a single
    #    statement executed directly (not via executescript) so it
    #    is safe even on a locked database.
    try:
        conn.execute(_VERSION_TABLE_DDL)
    except sqlite3.OperationalError as exc:
        # Some custom SQLite builds reject even ``CREATE TABLE IF
        # NOT EXISTS`` due to an immutable-db lock. Surface this
        # with enough context to be diagnosable.
        raise MemoryUnavailableError(
            f"Failed to create memory_schema_version table: {exc}. "
            f"The database may be open in read-only mode or "
            f"locked by another process.",
            context={"sqlite_error": str(exc)},
            cause=exc,
        ) from exc

    # 2. Read the current version.
    current_version = _get_recorded_version(conn)

    # 3. Apply each pending migration in order.
    for target_version, apply_fn in _MIGRATIONS:
        if current_version >= target_version:
            continue

        logger.info(
            "Applying memory schema migration v%d (current=%d).",
            target_version,
            current_version,
        )

        try:
            apply_fn(conn)
        except sqlite3.OperationalError as exc:
            err_str = str(exc).lower()
            if "no such module" in err_str and "fts5" in err_str:
                # FTS5 missing from the SQLite build. This is not
                # retryable at runtime -- the operator must install
                # an FTS5-enabled Python/SQLite. Flag it clearly.
                raise MemoryUnavailableError(
                    "The running SQLite library does not include the "
                    "FTS5 extension. The memory subsystem requires "
                    "FTS5 for full-text search. Install an "
                    "FTS5-enabled Python (standard CPython >= 3.11 "
                    "bundles it) or rebuild SQLite with "
                    "-DSQLITE_ENABLE_FTS5.",
                    context={
                        "required_extension": "fts5",
                        "sqlite_version": _sqlite_version(conn),
                    },
                    cause=exc,
                ) from exc
            raise MemoryUnavailableError(
                f"Failed to apply memory schema migration "
                f"v{target_version}: {exc}",
                context={
                    "migration_version": target_version,
                    "sqlite_error": str(exc),
                },
                cause=exc,
            ) from exc

        # Record the version -- INSERT OR IGNORE so a crash between
        # apply_fn and this line is recovered by the next run.
        _record_version(conn, target_version)
        current_version = target_version

        logger.info(
            "Memory schema migration v%d applied successfully.",
            target_version,
        )

    return current_version


# ---------------------------------------------------------------------------
# Public API -- FTS rebuild (recovery)
# ---------------------------------------------------------------------------


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index from the authoritative ``memories`` table.

    Called when:
      * ``schema_status(conn)["needs_rebuild"]`` is True
        (memories / fts row counts diverge -- usually after a
        hard crash during a write),
      * the operator manually requests a rebuild via P1.7's
        maintenance endpoint.

    FTS5 supports this via its special ``'rebuild'`` command on the
    external content table. The rebuild reads every row of
    ``memories`` and regenerates the inverted index -- O(N) with N
    = number of entries. For typical deployments (thousands of
    entries), the rebuild takes under a second.

    Safe to call concurrently? NO. The rebuild holds a write lock
    on ``memories_fts`` for its duration; other writers block.
    Callers must ensure no other memory write is in flight (the
    service's write pipeline takes a lock, so this is naturally
    serialized in production).
    """
    try:
        conn.execute(
            "INSERT INTO memories_fts(memories_fts) VALUES('rebuild')"
        )
    except sqlite3.OperationalError as exc:
        raise MemoryUnavailableError(
            f"Failed to rebuild FTS5 index: {exc}",
            context={"operation": "fts_rebuild"},
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Public API -- diagnostics
# ---------------------------------------------------------------------------


def schema_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Return a diagnostic snapshot of the memory schema.

    Used by:
      * the repository's ``__init__`` (informational log line),
      * P1.7's health endpoint,
      * the ``test_repository`` test that asserts the schema is
        fully applied after ``apply_schema``.

    Returns a dict with the following keys:

      * ``current_version`` -- schema version currently recorded,
      * ``target_version``  -- ``CURRENT_VERSION`` (what this
                                module knows how to produce),
      * ``up_to_date``      -- ``current_version >= target_version``,
      * ``memories_count``  -- row count of ``memories``,
      * ``fts_count``       -- row count of ``memories_fts``,
      * ``needs_rebuild``   -- True when the two counts diverge
                                (only meaningful when ``up_to_date``),
      * ``fts5_available``  -- True when FTS5 is compiled into the
                                running SQLite (checked via a
                                ``PRAGMA compile_options`` probe).

    All integer values are non-negative. All booleans are strict
    ``bool`` (not truthy ints) so callers can serialize the dict
    to JSON without special-casing.
    """
    current_version = _get_recorded_version(conn)
    up_to_date = current_version >= CURRENT_VERSION

    memories_count = _safe_count(conn, "memories")
    fts_count = _safe_count(conn, "memories_fts")
    needs_rebuild = (
        up_to_date
        and memories_count > 0
        and memories_count != fts_count
    )

    return {
        "current_version": current_version,
        "target_version": CURRENT_VERSION,
        "up_to_date": up_to_date,
        "memories_count": memories_count,
        "fts_count": fts_count,
        "needs_rebuild": needs_rebuild,
        "fts5_available": _fts5_available(conn),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_count(conn: sqlite3.Connection, table_name: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` with a safe fallback."""
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _sqlite_version(conn: sqlite3.Connection) -> str:
    """Return the SQLite library version as a string."""
    try:
        row = conn.execute("SELECT sqlite_version()").fetchone()
        return str(row[0]) if row else "unknown"
    except sqlite3.Error:
        return "unknown"


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True when FTS5 is compiled into the running SQLite.

    Uses ``PRAGMA compile_options`` when available (SQLite >=
    3.6.18); falls back to attempting a no-op FTS5 CREATE in a
    savepoint when the pragma is not exposed. The fallback path
    leaves no trace because the savepoint is rolled back.
    """
    try:
        rows = conn.execute("PRAGMA compile_options").fetchall()
        for row in rows:
            value = str(row[0]).upper()
            if "ENABLE_FTS5" in value:
                return True
    except sqlite3.Error:
        pass

    # Fallback: try a probe. Uses SAVEPOINT so failure leaves no
    # side effects, even on databases where DDL is otherwise
    # restricted.
    try:
        conn.execute("SAVEPOINT fts5_probe")
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)"
            )
            conn.execute("DROP TABLE temp.fts5_probe")
            return True
        finally:
            conn.execute("RELEASE fts5_probe")
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CURRENT_VERSION",
    "apply_schema",
    "rebuild_fts",
    "schema_status",
]
