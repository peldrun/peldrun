# backend/omweb/memory/imports/adapters/sqlite_adapter.py
"""
PELDRUN Memory Platform - SQLite Database Import Adapter.

Imports memory entries from an external SQLite database file. Two
DISTINCT use cases, handled by the SAME adapter via schema
detection:

    **1. PELDRUN backup restore** -- a ``.db`` produced by a
        previous PELDRUN instance. The ``memories`` table matches
        the P1.2 canonical schema exactly (``memory_id``,
        ``scope_type``, ``scope_id``, ``source_type``, ...).
        Entries preserve their ORIGINAL scopes, and
        ``source_type=explicit_user`` entries are allowed into
        user scope (same security rule as the package format:
        task 1.6.14 -- an entry that the user originally typed
        themselves retains its claim to user scope).

    **2. External database import** -- an arbitrary SQLite file
        produced by another tool (ChatGPT export, Notion, Obsidian
        SQLite plugin, a custom notes app). The adapter
        auto-detects the table and columns: ``content``/``text``/
        ``body`` for the entry body, optional
        ``tags``/``category``/``importance``/``created_at``. All
        entries are imported as ``source_type=IMPORT``, into the
        caller-supplied ``scope_ref`` (the database has no
        inherent scope). **User scope is rejected** for the whole
        import -- the same safety policy as ``MarkdownAdapter``.

Why one adapter for two cases
-----------------------------

The alternative -- a ``PeldrunBackupAdapter`` for case 1 and a
``GenericSqliteAdapter`` for case 2 -- would produce two adapters
that both open SQLite, both traverse a schema, both convert rows
to ``MemoryEntry`` objects. That is ~400 lines of shared code
duplicated across two files, and the "which adapter handles this
``.db``?" question becomes a per-source judgement call for the
caller.

One adapter with schema DETECTION answers the question
automatically: the presence of the PELDRUN canonical columns is
the discriminator. The user never has to know which mode applies
-- they hand over a ``.db`` and the adapter does the right thing.

Schema detection -- the discriminator
-------------------------------------

Mode A (PELDRUN backup) is selected when the database contains a
``memories`` table whose columns include at least:

    memory_type, scope_type, scope_id, content, source_type,
    importance, tags_json (or tags), created_at

That is a strict subset of the P1.2 schema -- an OLDER PELDRUN
database (missing one of the newer columns) still qualifies as
"probably a PELDRUN backup", and the adapter fills any missing
field with a safe default.

Mode B (external) is selected when NO table matches that shape.
The adapter then walks every table, looking for one with a
"content-like" column. The first match wins; the walk is
deterministic (tables are sorted alphabetically) so identical
input produces identical output.

Neither mode matches -> ``MemoryValidationError`` naming the
tables it inspected. This is the correct response: a database
without recognizable content is not importable, and the user
needs to know which tables the adapter looked at.

Read-only guarantee
-------------------

The external database is opened with ``mode=ro`` (SQLite URI
flag). The adapter NEVER writes to the source file -- not even
under an exception path. This matters because users typically
import from a file they cannot afford to corrupt (a backup,
the only copy of their notes).

The mode is enforced at the ``sqlite3.connect`` level, so even
a bug in this adapter cannot accidentally modify the file.

Row limit
---------

``_MAX_ROWS = 10_000``. The limit exists for two reasons:

    * The preview UI renders every candidate. A 1M-row database
      would overwhelm the browser.
    * Parse time is O(N). 10k rows takes ~2s on a modern machine;
      1M rows would take 3+ minutes and lock up the request.

When the row count exceeds the limit, the adapter imports the
FIRST 10k (in insertion order) and records a warning explaining
how many were skipped. The user can run the import again with a
``LIMIT`` if they need the remainder, or filter the source
database before importing.

Bytes source -- temporary file
------------------------------

``sqlite3`` has no in-memory-from-bytes API. A ``bytes`` source
is written to a NamedTemporaryFile, opened read-only, and the
temp file is deleted on scope exit. This is the only way to
import a SQLite database that arrived as HTTP upload bytes
without writing to a caller-visible path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemoryScopeRef,
    MemorySourceType,
    MemoryType,
    MemoryValidationError,
)
from peldrun.memory.services.policies import classify_memory_type

from omweb.memory.imports.base_adapter import BaseImportAdapter


logger = logging.getLogger("omweb.memory.imports.adapters.sqlite")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_SQLITE_MAGIC: bytes = b"SQLite format 3\x00"
"""The 16-byte magic header at the start of every SQLite file.

Used by ``can_handle`` to detect ``bytes`` sources without
opening the database."""


_ACCEPTED_SUFFIXES: frozenset = frozenset(
    {".db", ".sqlite", ".sqlite3", ".db3"}
)


_MAX_DB_SIZE: int = 100 * 1024 * 1024
"""100 MB ceiling on the source database file."""


_MAX_ROWS: int = 10_000
"""Hard limit on imported rows per import operation.

See module docstring for the two-fold rationale (UI + parse time).
"""


_CANONICAL_MEMORIES_TABLE: str = "memories"
"""The table name used by the P1.2 schema. When this table
exists with the expected columns, the adapter uses PELDRUN
backup mode."""


_CANONICAL_REQUIRED_COLUMNS: frozenset = frozenset(
    {"content", "memory_type", "scope_type", "scope_id", "source_type"}
)
"""Minimum column set that qualifies a ``memories`` table as
PELDRUN canonical."""


_CONTENT_COLUMN_CANDIDATES: Tuple[str, ...] = (
    "content", "text", "body", "note", "value", "message", "data"
)
"""Column names checked (in order) for the entry body in
external mode."""


_TAGS_COLUMN_CANDIDATES: Tuple[str, ...] = (
    "tags_json", "tags", "labels", "categories"
)
"""Column names checked (in order) for tags."""


_TYPE_COLUMN_CANDIDATES: Tuple[str, ...] = (
    "memory_type", "type", "category", "kind"
)
"""Column names checked (in order) for the memory type."""


_IMPORTANCE_COLUMN_CANDIDATES: Tuple[str, ...] = (
    "importance", "weight", "score", "priority"
)
"""Column names checked (in order) for importance (0-100)."""


_CREATED_COLUMN_CANDIDATES: Tuple[str, ...] = (
    "created_at", "created", "timestamp", "ts", "date"
)
"""Column names checked (in order) for the creation timestamp.

Values may be epoch (float/int) or ISO 8601 strings; both are
parsed. Anything else is warned and dropped to ``None``."""


_SLUG_INVALID_RE: re.Pattern = re.compile(r"[^\w\-]+")


# ---------------------------------------------------------------------------
# SqliteAdapter
# ---------------------------------------------------------------------------


class SqliteAdapter(BaseImportAdapter):
    """Import adapter for external SQLite database files.

    See the module docstring for the two-mode design and the
    read-only guarantee.
    """

    adapter_id: str = "sqlite"
    description: str = (
        "SQLite database file (.db, .sqlite, .sqlite3): either a "
        "PELDRUN backup (canonical ``memories`` table) or an "
        "external database with a content-like column. Read-only."
    )

    # ------------------------------------------------------------------
    # Format detection
    # ------------------------------------------------------------------

    def can_handle(self, source: Any) -> bool:
        """Return True for a SQLite database source.

        Accepts:
          * ``Path`` ending in a SQLite-typical extension,
          * ``bytes`` starting with the SQLite magic header,
          * ``dict`` with ``"path"`` / ``"db_path"`` key pointing
            at a string or Path.
        """
        if isinstance(source, dict):
            p = source.get("path") or source.get("db_path")
            return isinstance(p, (str, Path))

        if isinstance(source, Path):
            return (
                source.is_file()
                and source.suffix.lower() in _ACCEPTED_SUFFIXES
            )

        if isinstance(source, bytes):
            return source[:16] == _SQLITE_MAGIC

        return False

    # ------------------------------------------------------------------
    # Parse dispatch
    # ------------------------------------------------------------------

    async def _parse_to_candidates(
        self, source: Any, scope_ref: MemoryScopeRef
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Parse ``source`` into ``(candidates, warnings)``.

        ``scope_ref`` semantics depend on the DETECTED mode:

          * Mode A (PELDRUN backup): the source has scope columns;
            ``scope_ref`` is UNUSED -- each entry keeps its own
            original scope.
          * Mode B (external): the source has no scope information;
            ``scope_ref`` becomes the scope for every entry.

        User scope:
          * Mode A -- allowed when the entry's ``source_type`` is
            ``explicit_user`` (the plan's task 1.6.14 rule).
            Rejected per-entry otherwise.
          * Mode B -- rejected for the WHOLE import at parse start
            (an external DB has no ``source_type`` information;
            every entry would be ``IMPORT``, which cannot legally
            target user scope).
        """
        # ----- Dispatch by source shape ---------------------------
        if isinstance(source, dict):
            db_path = source.get("path") or source.get("db_path")
            if not isinstance(db_path, (str, Path)):
                raise MemoryValidationError(
                    "SQLite dict source requires 'path' or 'db_path' "
                    f"to be a string or Path, got "
                    f"{type(db_path).__name__}",
                    field_name="path",
                )
            return await self._parse_db_path(
                Path(db_path), scope_ref, source=source
            )

        if isinstance(source, Path):
            return await self._parse_db_path(source, scope_ref)

        if isinstance(source, bytes):
            return await self._parse_db_bytes(source, scope_ref)

        raise MemoryValidationError(
            f"SqliteAdapter cannot handle source of type "
            f"{type(source).__name__}; expected dict, Path, or bytes.",
            field_name="source",
        )

    # ------------------------------------------------------------------
    # Path source
    # ------------------------------------------------------------------

    async def _parse_db_path(
        self,
        db_path: Path,
        scope_ref: MemoryScopeRef,
        *,
        source: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Open a SQLite file on disk and parse its rows."""
        if not db_path.is_file():
            raise MemoryValidationError(
                f"SQLite database file not found: {db_path}",
                field_name="source",
                context={"path": str(db_path)},
            )
        try:
            size = db_path.stat().st_size
        except OSError as exc:
            raise MemoryValidationError(
                f"Cannot stat SQLite file {db_path}: {exc}",
                field_name="source",
                cause=exc,
            ) from exc
        if size > _MAX_DB_SIZE:
            raise MemoryValidationError(
                f"SQLite file is too large ({size} bytes > "
                f"{_MAX_DB_SIZE}). Reduce the source or split the "
                f"import.",
                field_name="source",
                context={"path": str(db_path), "size": size},
            )

        # Optional caller-supplied overrides via dict source.
        explicit_table: Optional[str] = None
        explicit_columns: Optional[Dict[str, str]] = None
        if source is not None:
            _t = source.get("table")
            if isinstance(_t, str) and _t.strip():
                explicit_table = _t.strip()
            _c = source.get("columns")
            if isinstance(_c, dict):
                explicit_columns = {
                    str(k): str(v)
                    for k, v in _c.items()
                    if isinstance(v, str) and v.strip()
                }

        raw_rows, detected_mode, warnings = await asyncio.to_thread(
            self._read_rows_sync,
            db_path,
            explicit_table,
            explicit_columns,
        )

        return self._build_candidates(
            raw_rows=raw_rows,
            mode=detected_mode,
            scope_ref=scope_ref,
            source_label=str(db_path),
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Bytes source
    # ------------------------------------------------------------------

    async def _parse_db_bytes(
        self,
        data: bytes,
        scope_ref: MemoryScopeRef,
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Import from raw SQLite file bytes.

        ``sqlite3`` has no "open from memory" API. We write the
        bytes to a NamedTemporaryFile, open it read-only, and
        delete the temp on exit. The file is created with
        ``delete=False`` and explicitly removed in a ``finally``
        so that Windows (where the OS holds a lock on open files)
        works correctly.
        """
        if not data.startswith(_SQLITE_MAGIC):
            raise MemoryValidationError(
                "Source bytes are not a SQLite database (magic "
                "header mismatch).",
                field_name="source",
            )
        if len(data) > _MAX_DB_SIZE:
            raise MemoryValidationError(
                f"SQLite bytes too large ({len(data)} bytes > "
                f"{_MAX_DB_SIZE}). Reduce the source.",
                field_name="source",
                context={"size": len(data)},
            )

        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".sqlite", prefix="peldrun_import_"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(data)
            raw_rows, detected_mode, warnings = await asyncio.to_thread(
                self._read_rows_sync,
                tmp_path,
                None,
                None,
            )
            return self._build_candidates(
                raw_rows=raw_rows,
                mode=detected_mode,
                scope_ref=scope_ref,
                source_label="<bytes>",
                warnings=warnings,
            )
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError as exc:
                logger.debug(
                    "Failed to remove temp SQLite file %s: %s",
                    tmp_path,
                    exc,
                )

    # ------------------------------------------------------------------
    # Sync SQLite reader (runs in a worker thread)
    # ------------------------------------------------------------------

    def _read_rows_sync(
        self,
        db_path: Path,
        explicit_table: Optional[str],
        explicit_columns: Optional[Dict[str, str]],
    ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
        """Open the DB read-only and return normalized rows.

        Returns ``(rows, mode, warnings)`` where ``mode`` is
        ``"canonical"`` or ``"external"``. The rows are dicts
        already mapped to a uniform shape:

            {
              "content": str,
              "memory_type_hint": str | None,
              "scope_type": str | None,
              "scope_id": str | None,
              "source_type": str | None,
              "tags_raw": Any,
              "importance_raw": Any,
              "created_at_raw": Any,
            }

        Turning these into ``MemoryEntry`` objects happens in
        ``_build_candidates`` on the caller's thread -- pure CPU,
        no thread hop needed.
        """
        warnings: List[str] = []

        # --------------------------------------------------------
        # Open read-only. The URI form is the ONLY way to
        # guarantee SQLite will not create a `-journal` or
        # `-wal` file next to the source. Without it, a bug in
        # this adapter could leave artifacts next to the
        # user's backup.
        # --------------------------------------------------------
        uri = f"file:{db_path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            raise MemoryValidationError(
                f"Cannot open SQLite database {db_path}: {exc}",
                field_name="source",
                cause=exc,
            ) from exc

        try:
            conn.row_factory = sqlite3.Row

            # ----- Schema inspection --------------------------
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
            ]
            if not tables:
                raise MemoryValidationError(
                    f"SQLite database {db_path} contains no user "
                    f"tables.",
                    field_name="source",
                    context={"path": str(db_path)},
                )

            # ----- Mode A: canonical PELDRUN memories table ---
            if explicit_table is None and (
                _CANONICAL_MEMORIES_TABLE in tables
            ):
                cols = self._table_columns(conn, _CANONICAL_MEMORIES_TABLE)
                if _CANONICAL_REQUIRED_COLUMNS.issubset(cols):
                    canonical_rows, canonical_warnings = (
                        self._read_canonical_rows(conn)
                    )
                    warnings.extend(canonical_warnings)
                    return canonical_rows, "canonical", warnings
                # Else -- fall through to Mode B with the rest.

            # ----- Mode B: external (content-like column) -----
            target_table = explicit_table or self._detect_external_table(
                conn, tables, warnings
            )
            if target_table is None:
                raise MemoryValidationError(
                    f"SQLite database {db_path} has no table with a "
                    f"content-like column (looked for: "
                    f"{list(_CONTENT_COLUMN_CANDIDATES)}). Tables "
                    f"inspected: {tables}",
                    field_name="source",
                    context={
                        "path": str(db_path),
                        "tables": tables[:20],
                    },
                )

            # `_read_external_rows` mutates the `warnings` list in
            # place (via its internal `_pick` closure). The returned
            # warnings list is therefore the SAME object, and we
            # must NOT extend with it -- that would double every
            # warning. Take only the rows.
            external_rows, _ = self._read_external_rows(
                conn, target_table, explicit_columns, warnings
            )
            return external_rows, "external", warnings
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # Mode A: canonical memories table
    # ------------------------------------------------------------------

    def _read_canonical_rows(
        self, conn: sqlite3.Connection
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Read rows from a canonical PELDRUN ``memories`` table."""
        warnings: List[str] = []
        rows: List[Dict[str, Any]] = []

        cursor = conn.execute(
            "SELECT * FROM memories "
            "ORDER BY created_at ASC "
            f"LIMIT {_MAX_ROWS + 1}"
        )
        raw_rows = cursor.fetchall()
        if len(raw_rows) > _MAX_ROWS:
            warnings.append(
                f"source contains more than {_MAX_ROWS} rows; "
                f"imported the first {_MAX_ROWS} in creation-order"
            )
            raw_rows = raw_rows[:_MAX_ROWS]

        for row in raw_rows:
            rows.append(
                {
                    "content": row["content"],
                    "memory_type_hint": row["memory_type"],
                    "scope_type": row["scope_type"],
                    "scope_id": row["scope_id"],
                    "source_type": row["source_type"],
                    "tags_raw": row["tags_json"]
                    if "tags_json" in row.keys()
                    else row["tags"]
                    if "tags" in row.keys()
                    else None,
                    "importance_raw": row["importance"]
                    if "importance" in row.keys()
                    else None,
                    "created_at_raw": row["created_at"]
                    if "created_at" in row.keys()
                    else None,
                }
            )

        return rows, warnings

    # ------------------------------------------------------------------
    # Mode B: external table
    # ------------------------------------------------------------------

    def _detect_external_table(
        self,
        conn: sqlite3.Connection,
        tables: List[str],
        warnings: List[str],
    ) -> Optional[str]:
        """Return the first table that has a content-like column."""
        for table_name in tables:
            cols = self._table_columns(conn, table_name)
            for candidate in _CONTENT_COLUMN_CANDIDATES:
                if candidate in cols:
                    logger.info(
                        "SqliteAdapter: external table %r selected "
                        "(content column=%r).",
                        table_name,
                        candidate,
                    )
                    return table_name
        return None

    def _read_external_rows(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        explicit_columns: Optional[Dict[str, str]],
        warnings: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Read rows from a non-canonical table, auto-detecting columns."""
        cols = self._table_columns(conn, table_name)

        # Resolve each field's column: explicit override first,
        # then first candidate present in the table.
        def _pick(
            candidates: Tuple[str, ...], override_key: str
        ) -> Optional[str]:
            if explicit_columns and override_key in explicit_columns:
                v = explicit_columns[override_key]
                if v in cols:
                    return v
                warnings.append(
                    f"explicit column {v!r} for {override_key} not "
                    f"in table {table_name!r}; falling back to "
                    f"auto-detection"
                )
            for c in candidates:
                if c in cols:
                    return c
            return None

        content_col = _pick(_CONTENT_COLUMN_CANDIDATES, "content")
        if content_col is None:
            raise MemoryValidationError(
                f"Table {table_name!r} has no content-like column "
                f"(looked for {list(_CONTENT_COLUMN_CANDIDATES)})",
                field_name="content",
                context={"table": table_name, "columns": list(cols)[:20]},
            )
        tags_col = _pick(_TAGS_COLUMN_CANDIDATES, "tags")
        type_col = _pick(_TYPE_COLUMN_CANDIDATES, "memory_type")
        importance_col = _pick(_IMPORTANCE_COLUMN_CANDIDATES, "importance")
        created_col = _pick(_CREATED_COLUMN_CANDIDATES, "created_at")

        # Order by the timestamp column when present so the
        # imported order matches the source's chronological order.
        order_clause = (
            f"ORDER BY {created_col} ASC" if created_col else ""
        )
        sql = (
            f"SELECT * FROM {table_name} {order_clause} "
            f"LIMIT {_MAX_ROWS + 1}"
        )
        raw_rows = conn.execute(sql).fetchall()

        if len(raw_rows) > _MAX_ROWS:
            warnings.append(
                f"source table {table_name!r} has more than "
                f"{_MAX_ROWS} rows; imported the first {_MAX_ROWS}"
            )
            raw_rows = raw_rows[:_MAX_ROWS]

        rows: List[Dict[str, Any]] = []
        for row in raw_rows:
            rows.append(
                {
                    "content": row[content_col],
                    "memory_type_hint": (
                        row[type_col] if type_col else None
                    ),
                    "scope_type": None,  # external DBs lack scope
                    "scope_id": None,
                    "source_type": None,  # treated as IMPORT later
                    "tags_raw": row[tags_col] if tags_col else None,
                    "importance_raw": (
                        row[importance_col] if importance_col else None
                    ),
                    "created_at_raw": (
                        row[created_col] if created_col else None
                    ),
                }
            )

        return rows, warnings

    # ------------------------------------------------------------------
    # Schema helper
    # ------------------------------------------------------------------

    @staticmethod
    def _table_columns(
        conn: sqlite3.Connection, table_name: str
    ) -> set:
        """Return the set of column names for ``table_name``.

        Uses ``PRAGMA table_info`` with the table name QUOTED
        via double-quotes -- protects against a malicious table
        name containing SQL metacharacters. The name comes from
        ``sqlite_master`` (already untrusted), so this is a
        defense-in-depth measure, not a reaction to a specific
        current threat.
        """
        safe_name = table_name.replace('"', '""')
        cursor = conn.execute(f'PRAGMA table_info("{safe_name}")')
        return {row[1] for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # Candidate construction (pure CPU, called on caller's thread)
    # ------------------------------------------------------------------

    def _build_candidates(
        self,
        *,
        raw_rows: List[Dict[str, Any]],
        mode: str,
        scope_ref: MemoryScopeRef,
        source_label: str,
        warnings: List[str],
    ) -> Tuple[List[MemoryEntry], List[str]]:
        """Convert normalized rows into ``MemoryEntry`` objects.

        ``mode`` controls scope behavior:

          * ``"canonical"`` -- each entry gets its OWN
            ``scope_type``/``scope_id`` from the row (the
            PELDRUN backup preserves these). ``scope_ref`` is
            only used as a fallback when the row's scope is
            missing/invalid.
          * ``"external"`` -- every entry uses ``scope_ref``'s
            scope. External DBs carry no scope information.

        User-scope handling:
          * ``"canonical"`` -- per-entry check: allowed only when
            the row's ``source_type`` is ``explicit_user``. Other
            source types are rejected with a warning (matching
            ``package_validator``'s policy).
          * ``"external"`` -- the WHOLE import is rejected at
            parse-start if ``scope_ref`` is user scope.
        """
        # External mode + user scope -> reject whole import before
        # doing any per-row work.
        if mode == "external" and scope_ref.scope_type == MemoryScope.USER:
            raise MemoryValidationError(
                "SqliteAdapter cannot import an EXTERNAL database "
                "into user scope. External databases have no "
                "source_type information, so every entry would be "
                "imported as source_type=import -- and imports may "
                "not target user scope (task 1.6.14). To restore a "
                "PELDRUN backup into user scope, import the backup "
                "file (which carries source_type=explicit_user on "
                "user-scope entries).",
                field_name="scope_ref",
                context={"requested_scope": str(scope_ref)},
            )

        candidates: List[MemoryEntry] = []

        for idx, row in enumerate(raw_rows):
            try:
                entry = self._row_to_entry(
                    row=row,
                    mode=mode,
                    scope_ref=scope_ref,
                    source_label=source_label,
                    row_index=idx,
                    warnings=warnings,
                )
                if entry is not None:
                    candidates.append(entry)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"row {idx + 1}: failed to build entry "
                    f"({type(exc).__name__}: {exc})"
                )

        logger.info(
            "SqliteAdapter built %d/%d candidates (mode=%s, "
            "scope=%s, warnings=%d).",
            len(candidates),
            len(raw_rows),
            mode,
            scope_ref,
            len(warnings),
        )
        return candidates, warnings

    def _row_to_entry(
        self,
        *,
        row: Dict[str, Any],
        mode: str,
        scope_ref: MemoryScopeRef,
        source_label: str,
        row_index: int,
        warnings: List[str],
    ) -> Optional[MemoryEntry]:
        """Convert one normalized row to a ``MemoryEntry``."""
        # ----- Content -------------------------------------------
        content = row.get("content")
        if content is None:
            return None  # silently skip NULL content
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            return None

        # ----- Scope (mode-dependent) ----------------------------
        if mode == "canonical":
            row_scope_type = row.get("scope_type")
            row_scope_id = row.get("scope_id")
            row_source_type = row.get("source_type")

            try:
                resolved_scope_type = MemoryScope(row_scope_type)
            except (ValueError, TypeError):
                warnings.append(
                    f"row {row_index + 1}: invalid scope_type "
                    f"{row_scope_type!r}; using fallback "
                    f"{scope_ref.scope_type.value}"
                )
                resolved_scope_type = scope_ref.scope_type
                row_scope_id = scope_ref.scope_id

            resolved_scope_id = (
                row_scope_id.strip()
                if isinstance(row_scope_id, str) and row_scope_id.strip()
                else scope_ref.scope_id
            )

            try:
                resolved_source_type = MemorySourceType(row_source_type)
            except (ValueError, TypeError):
                warnings.append(
                    f"row {row_index + 1}: invalid source_type "
                    f"{row_source_type!r}; treating as 'import'"
                )
                resolved_source_type = MemorySourceType.IMPORT

            # Per-entry user-scope check (mirrors package_validator).
            if (
                resolved_scope_type == MemoryScope.USER
                and resolved_source_type != MemorySourceType.EXPLICIT_USER
            ):
                warnings.append(
                    f"row {row_index + 1}: REJECTED (security) -- "
                    f"user-scope entry with source_type="
                    f"{resolved_source_type.value!r}. Only "
                    f"'explicit_user' entries may target user scope."
                )
                return None
        else:  # external mode
            resolved_scope_type = scope_ref.scope_type
            resolved_scope_id = scope_ref.scope_id
            resolved_source_type = MemorySourceType.IMPORT

        # ----- Memory type ---------------------------------------
        hint = row.get("memory_type_hint")
        memory_type: Optional[MemoryType] = None
        if isinstance(hint, str) and hint.strip():
            try:
                memory_type = MemoryType(hint.strip().lower())
            except (ValueError, TypeError):
                pass
        if memory_type is None:
            memory_type = (
                classify_memory_type(content) or MemoryType.SEMANTIC
            )

        # ----- Tags ----------------------------------------------
        tags = self._parse_tags(row.get("tags_raw"), row_index, warnings)

        # ----- Importance ----------------------------------------
        importance = self._parse_importance(
            row.get("importance_raw"), row_index, warnings
        )

        # ----- Created at ----------------------------------------
        created_at = self._parse_timestamp(
            row.get("created_at_raw"), row_index, warnings
        )

        # ----- Provenance ----------------------------------------
        provenance = MemoryProvenance(
            source_type=resolved_source_type,
            imported_from=f"sqlite:{source_label}",
        )

        # ----- Build --------------------------------------------
        try:
            entry = MemoryEntry.create(
                content=content,
                memory_type=memory_type,
                scope_type=resolved_scope_type,
                scope_id=resolved_scope_id,
                provenance=provenance,
                importance=importance,
                tags=tags,
            )
            if created_at is not None:
                entry = entry.model_copy(
                    update={"created_at": created_at}
                )
            return entry
        except (ValueError, TypeError) as exc:
            warnings.append(
                f"row {row_index + 1}: construction failed: {exc}"
            )
            return None

    # ------------------------------------------------------------------
    # Field parsers (defensive against varied source schemas)
    # ------------------------------------------------------------------

    def _parse_tags(
        self,
        raw: Any,
        row_index: int,
        warnings: List[str],
    ) -> List[str]:
        """Parse tags from a JSON array, comma string, or list."""
        if raw is None:
            return []

        # JSON array stored as TEXT
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(t) for t in parsed if t is not None]
                except json.JSONDecodeError:
                    warnings.append(
                        f"row {row_index + 1}: unparseable JSON tags, "
                        f"falling back to comma-split"
                    )
            # Comma-separated fallback
            return [t.strip() for t in stripped.split(",") if t.strip()]

        if isinstance(raw, (list, tuple)):
            return [str(t) for t in raw if t is not None]

        warnings.append(
            f"row {row_index + 1}: unexpected tags type "
            f"{type(raw).__name__}, dropping"
        )
        return []

    def _parse_importance(
        self,
        raw: Any,
        row_index: int,
        warnings: List[str],
    ) -> int:
        """Parse importance from an int / float / numeric string."""
        if raw is None:
            return 50
        try:
            n = int(float(raw))
        except (ValueError, TypeError):
            warnings.append(
                f"row {row_index + 1}: unparseable importance "
                f"{raw!r}, using default 50"
            )
            return 50
        return max(0, min(100, n))

    def _parse_timestamp(
        self,
        raw: Any,
        row_index: int,
        warnings: List[str],
    ) -> Optional[datetime]:
        """Parse a timestamp from epoch (float/int) or ISO string."""
        if raw is None:
            return None

        # Numeric epoch
        if isinstance(raw, (int, float)):
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (OSError, ValueError, OverflowError):
                warnings.append(
                    f"row {row_index + 1}: out-of-range epoch "
                    f"timestamp {raw!r}, dropping"
                )
                return None

        # ISO string
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            try:
                dt = datetime.fromisoformat(
                    stripped.replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                warnings.append(
                    f"row {row_index + 1}: unparseable ISO timestamp "
                    f"{raw!r}, dropping"
                )
                return None

        warnings.append(
            f"row {row_index + 1}: unexpected timestamp type "
            f"{type(raw).__name__}, dropping"
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["SqliteAdapter"]
