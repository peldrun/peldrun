# backend/omweb/memory/monitoring/memory_usage.py
"""
PELDRUN Memory Platform - Memory Usage Tracker & Ledger.

Persists per-run memory usage metrics to a dedicated SQLite table
(``memory_usage_ledger``) in ``peldrun_runtime.db``. Provides the
data that powers the monitoring API (``GET /api/memory/usage/*``)
and the monitoring dashboard.

Design invariants
-----------------

1. **Dedicated table, isolated schema.**
   The ledger lives in the SAME ``peldrun_runtime.db`` file as
   the ``token_ledger`` (P1.9 accounting), but in its OWN table
   so that the memory subsystem's schema evolution is
   independent of the LLM accounting schema. A future column
   added to ``token_ledger`` never requires touching
   ``memory_usage_ledger`` and vice versa.

2. **Per-RUN, not per-invocation.**
   Plan task 1.9.1 specifies
   ``MemoryUsageTracker.record(run_id, context, actual_tokens)``
   -- ONE row per run captures the whole memory lifecycle: what
   was retrieved, what was injected, what was dropped, and the
   per-scope attribution. LLM invocations WITHIN the run carry
   their own ``memory_tokens_used`` (see P1.9's
   ``usage_store`` patch). The ledger summarizes at the run
   level; the token ledger attributes at the invocation level.
   The two are correlated by ``run_id``.

3. **Append-only.**
   Rows are INSERTed, never UPDATEd. A "re-record" of the same
   ``run_id`` is prevented by the ``UNIQUE(run_id)`` constraint
   -- the second call is a silent no-op. Historical usage is
   immutable, matching the token_ledger's policy.

4. **Denormalized per-scope tokens.**
   ``MemoryUsage`` already carries ``user_tokens``,
   ``project_tokens``, ``chat_tokens``, ``agent_tokens``. The
   ledger denormalizes these directly rather than re-deriving
   them from the ``MemoryContext.memories`` list on every
   query. This makes the "by=scope" breakdown a simple
   ``GROUP BY``, not a per-row rescan of retrieved memories.

5. **Time-bucketed queries.**
   ``query_timeseries`` uses SQLite's ``strftime`` to bucket
   rows by day/week/month, matching the API's ``period``
   parameter. No transformation in Python; the DB does the
   aggregation. Results are simple dicts, ready for JSON.

6. **Failure isolation.**
   Recording a usage row is BEST-EFFORT: a failure in
   ``record`` logs a warning but never propagates to the run.
   Memory is an ENHANCEMENT, and a monitoring write failure
   must not abort the primary operation. The pattern mirrors
   ``memory_save``'s "structured result, never raises" rule.

Threading model
---------------

Thread-local connections, WAL mode, ``asyncio.to_thread`` for
every public method. The exact same pattern as
``peldrun/runtime/usage_store.py``. Consistency across the two
stores matters more than innovation -- a developer switching
between them should not have to learn two threading models.

Schema evolution
----------------

Future columns MUST be added via migration, not by editing the
DDL below. The exact recipe:

  1. Add the column to the DDL (for fresh databases).
  2. Add an ``ALTER TABLE ... ADD COLUMN`` to the migration
     helper (for existing databases).
  3. Bump ``_SCHEMA_VERSION``.

The migration check uses ``PRAGMA table_info`` -- identical to
``peldrun/runtime/usage_store.py``'s approach.
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
    MemoryContext,
    MemoryEntry,
    MemoryScope,
    MemoryUsage,
)


logger = logging.getLogger("omweb.memory.monitoring.usage")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA_VERSION: int = 1


_MEMORY_USAGE_LEDGER_DDL: str = """
CREATE TABLE IF NOT EXISTS memory_usage_ledger (
    -- Identity / attribution
    usage_id            TEXT    PRIMARY KEY,
    run_id              TEXT    NOT NULL UNIQUE,
    chat_id             TEXT,
    project_id          TEXT,

    -- Token buckets
    retrieval_tokens    INTEGER NOT NULL DEFAULT 0,
    context_tokens      INTEGER NOT NULL DEFAULT 0,
    overhead_tokens     INTEGER NOT NULL DEFAULT 0,

    -- Per-scope breakdown (sum == context_tokens)
    user_tokens         INTEGER NOT NULL DEFAULT 0,
    project_tokens      INTEGER NOT NULL DEFAULT 0,
    chat_tokens         INTEGER NOT NULL DEFAULT 0,
    agent_tokens        INTEGER NOT NULL DEFAULT 0,

    -- Budget
    budget_tokens       INTEGER NOT NULL DEFAULT 0,

    -- Entry counts (used + dropped == retrieved)
    entries_retrieved   INTEGER NOT NULL DEFAULT 0,
    entries_used        INTEGER NOT NULL DEFAULT 0,
    entries_dropped     INTEGER NOT NULL DEFAULT 0,

    -- Timestamps
    created_at          REAL    NOT NULL
);
"""


_MEMORY_USAGE_LEDGER_INDEXES_DDL: str = """
CREATE INDEX IF NOT EXISTS idx_memory_usage_created
    ON memory_usage_ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_usage_chat
    ON memory_usage_ledger(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_usage_project
    ON memory_usage_ledger(project_id, created_at);
"""


# ---------------------------------------------------------------------------
# SqliteMemoryUsageStore
# ---------------------------------------------------------------------------


class SqliteMemoryUsageStore:
    """Thread-safe SQLite-backed store for the memory usage ledger.

    One instance per process (see ``get_memory_usage_store``). The
    store is opened lazily on first write; a database with no
    memory activity never pays the connection cost.
    """

    _instance: Optional["SqliteMemoryUsageStore"] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Path) -> None:
        """Configure the store WITHOUT opening a connection.

        Parameters
        ----------
        db_path
            Path to ``peldrun_runtime.db`` (shared with the
            token ledger). Parent directory created if missing.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._schema_applied: bool = False
        self._schema_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the thread's connection, creating it if needed."""
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
            conn.execute("PRAGMA busy_timeout = 30000")

            if not self._schema_applied:
                with self._schema_lock:
                    if not self._schema_applied:
                        self._ensure_schema(conn)
                        self._schema_applied = True

            self._local.conn = conn
        return self._local.conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """Create tables + indexes if missing. Idempotent."""
        conn.executescript(_MEMORY_USAGE_LEDGER_DDL)
        conn.executescript(_MEMORY_USAGE_LEDGER_INDEXES_DDL)

    def close_all(self) -> None:
        """Close this thread's connection. Test helper."""
        if (
            hasattr(self._local, "conn")
            and self._local.conn is not None
        ):
            try:
                self._local.conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(self, usage: MemoryUsage) -> bool:
        """Persist a ``MemoryUsage`` row. Returns True on write.

        Returns False when the row already exists (the UNIQUE
        constraint on ``run_id`` rejected it -- a re-record of
        the same run is a no-op, not an error).

        Never raises for a storage failure: logs and returns
        False. The tracker's own ``record`` method is the
        fail-safe wrapper; this method can raise ``sqlite3.Error``
        and the caller handles it.
        """
        return await asyncio.to_thread(self._save_sync, usage)

    def _save_sync(self, usage: MemoryUsage) -> bool:
        conn = self._get_conn()
        now = time.time()
        usage_id = f"mu_{usage.run_id}"

        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO memory_usage_ledger (
                        usage_id, run_id, chat_id, project_id,
                        retrieval_tokens, context_tokens,
                        overhead_tokens,
                        user_tokens, project_tokens,
                        chat_tokens, agent_tokens,
                        budget_tokens,
                        entries_retrieved, entries_used,
                        entries_dropped,
                        created_at
                    ) VALUES (
                        ?, ?, ?, ?,
                        ?, ?,
                        ?,
                        ?, ?,
                        ?, ?,
                        ?,
                        ?, ?,
                        ?,
                        ?
                    )
                    ON CONFLICT(run_id) DO NOTHING
                    """,
                    (
                        usage_id,
                        usage.run_id,
                        usage.chat_id,
                        usage.project_id,
                        usage.retrieval_tokens,
                        usage.context_tokens,
                        usage.overhead_tokens,
                        usage.user_tokens,
                        usage.project_tokens,
                        usage.chat_tokens,
                        usage.agent_tokens,
                        usage.budget_tokens,
                        usage.entries_retrieved,
                        usage.entries_used,
                        usage.entries_dropped,
                        now,
                    ),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            logger.warning(
                "Failed to write memory usage for run %r: %s",
                usage.run_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def query_summary(
        self,
        *,
        period: str = "all",
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate KPIs across a time period and optional filters."""
        return await asyncio.to_thread(
            self._query_summary_sync, period, chat_id, project_id
        )

    def _query_summary_sync(
        self,
        period: str,
        chat_id: Optional[str],
        project_id: Optional[str],
    ) -> Dict[str, Any]:
        conn = self._get_conn()
        cutoff = _period_to_cutoff(period)

        where_parts = ["created_at >= ?"]
        params: List[Any] = [cutoff]
        if chat_id is not None:
            where_parts.append("chat_id = ?")
            params.append(chat_id)
        if project_id is not None:
            where_parts.append("project_id = ?")
            params.append(project_id)

        where_sql = " AND ".join(where_parts)

        try:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS run_count,
                    COALESCE(SUM(retrieval_tokens), 0) AS retrieval_tokens,
                    COALESCE(SUM(context_tokens), 0) AS context_tokens,
                    COALESCE(SUM(overhead_tokens), 0) AS overhead_tokens,
                    COALESCE(SUM(user_tokens), 0) AS user_tokens,
                    COALESCE(SUM(project_tokens), 0) AS project_tokens,
                    COALESCE(SUM(chat_tokens), 0) AS chat_tokens,
                    COALESCE(SUM(agent_tokens), 0) AS agent_tokens,
                    COALESCE(SUM(budget_tokens), 0) AS budget_tokens,
                    COALESCE(SUM(entries_retrieved), 0) AS entries_retrieved,
                    COALESCE(SUM(entries_used), 0) AS entries_used,
                    COALESCE(SUM(entries_dropped), 0) AS entries_dropped
                FROM memory_usage_ledger
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("memory_usage query_summary failed: %s", exc)
            return _empty_summary(period)

        if row is None:
            return _empty_summary(period)

        total_tokens = (
            int(row["retrieval_tokens"])
            + int(row["context_tokens"])
            + int(row["overhead_tokens"])
        )
        budget_sum = int(row["budget_tokens"])
        utilization = (
            (total_tokens / budget_sum) if budget_sum > 0 else 0.0
        )
        entries_retrieved = int(row["entries_retrieved"])
        entries_dropped = int(row["entries_dropped"])
        drop_rate = (
            (entries_dropped / entries_retrieved)
            if entries_retrieved > 0
            else 0.0
        )

        return {
            "period": period,
            "run_count": int(row["run_count"]),
            "total_tokens": total_tokens,
            "retrieval_tokens": int(row["retrieval_tokens"]),
            "context_tokens": int(row["context_tokens"]),
            "overhead_tokens": int(row["overhead_tokens"]),
            "user_tokens": int(row["user_tokens"]),
            "project_tokens": int(row["project_tokens"]),
            "chat_tokens": int(row["chat_tokens"]),
            "agent_tokens": int(row["agent_tokens"]),
            "budget_tokens": budget_sum,
            "budget_utilization": round(utilization, 4),
            "entries_retrieved": entries_retrieved,
            "entries_used": int(row["entries_used"]),
            "entries_dropped": entries_dropped,
            "drop_rate": round(drop_rate, 4),
        }

    async def query_breakdown(
        self,
        *,
        by: str = "scope",
        period: str = "all",
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return per-dimension breakdown of usage."""
        return await asyncio.to_thread(
            self._query_breakdown_sync,
            by,
            period,
            chat_id,
            project_id,
        )

    def _query_breakdown_sync(
        self,
        by: str,
        period: str,
        chat_id: Optional[str],
        project_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cutoff = _period_to_cutoff(period)

        where_parts = ["created_at >= ?"]
        params: List[Any] = [cutoff]
        if chat_id is not None:
            where_parts.append("chat_id = ?")
            params.append(chat_id)
        if project_id is not None:
            where_parts.append("project_id = ?")
            params.append(project_id)
        where_sql = " AND ".join(where_parts)

        if by == "scope":
            # The four scope buckets are columns, not rows. We
            # unpivot them via a UNION so the caller gets the
            # same shape regardless of `by`.
            sql = f"""
                SELECT 'user' AS key,
                       COALESCE(SUM(user_tokens), 0) AS tokens,
                       COUNT(*) AS run_count
                FROM memory_usage_ledger
                WHERE {where_sql}
                UNION ALL
                SELECT 'project',
                       COALESCE(SUM(project_tokens), 0),
                       COUNT(*)
                FROM memory_usage_ledger
                WHERE {where_sql}
                UNION ALL
                SELECT 'chat',
                       COALESCE(SUM(chat_tokens), 0),
                       COUNT(*)
                FROM memory_usage_ledger
                WHERE {where_sql}
                UNION ALL
                SELECT 'agent',
                       COALESCE(SUM(agent_tokens), 0),
                       COUNT(*)
                FROM memory_usage_ledger
                WHERE {where_sql}
                """
            full_params = params * 4
        elif by == "project":
            sql = f"""
                SELECT COALESCE(project_id, '<none>') AS key,
                       COALESCE(SUM(
                           retrieval_tokens + context_tokens +
                           overhead_tokens
                       ), 0) AS tokens,
                       COUNT(*) AS run_count
                FROM memory_usage_ledger
                WHERE {where_sql}
                GROUP BY project_id
                ORDER BY tokens DESC
                """
            full_params = params
        elif by == "chat":
            sql = f"""
                SELECT COALESCE(chat_id, '<none>') AS key,
                       COALESCE(SUM(
                           retrieval_tokens + context_tokens +
                           overhead_tokens
                       ), 0) AS tokens,
                       COUNT(*) AS run_count
                FROM memory_usage_ledger
                WHERE {where_sql}
                GROUP BY chat_id
                ORDER BY tokens DESC
                """
            full_params = params
        else:
            raise ValueError(
                f"unknown breakdown dimension: {by!r}; "
                f"expected one of: scope, project, chat"
            )

        try:
            rows = conn.execute(sql, full_params).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "memory_usage query_breakdown failed: %s", exc
            )
            return []

        total = sum(int(r["tokens"]) for r in rows)
        result: List[Dict[str, Any]] = []
        for r in rows:
            tokens = int(r["tokens"])
            result.append(
                {
                    "dimension": by,
                    "key": str(r["key"]),
                    "tokens": tokens,
                    "runs": int(r["run_count"]),
                    "pct": (
                        round((tokens / total) * 100.0, 2)
                        if total > 0
                        else 0.0
                    ),
                }
            )
        return result

    async def query_timeseries(
        self,
        *,
        period: str = "month",
        granularity: str = "day",
    ) -> List[Dict[str, Any]]:
        """Return a bucketed time series of token consumption."""
        return await asyncio.to_thread(
            self._query_timeseries_sync, period, granularity
        )

    def _query_timeseries_sync(
        self,
        period: str,
        granularity: str,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cutoff = _period_to_cutoff(period)

        fmt_map = {
            "day": "%Y-%m-%d",
            "week": "%Y-W%W",
            "month": "%Y-%m",
        }
        fmt = fmt_map.get(granularity, "%Y-%m-%d")

        sql = f"""
            SELECT
                strftime('{fmt}', datetime(created_at, 'unixepoch'))
                    AS bucket,
                COUNT(*) AS runs,
                COALESCE(SUM(retrieval_tokens), 0) AS retrieval_tokens,
                COALESCE(SUM(context_tokens), 0) AS context_tokens,
                COALESCE(SUM(overhead_tokens), 0) AS overhead_tokens,
                COALESCE(SUM(entries_used), 0) AS entries_used
            FROM memory_usage_ledger
            WHERE created_at >= ?
            GROUP BY bucket
            ORDER BY bucket ASC
            """

        try:
            rows = conn.execute(sql, (cutoff,)).fetchall()
        except sqlite3.Error as exc:
            logger.warning(
                "memory_usage query_timeseries failed: %s", exc
            )
            return []

        result: List[Dict[str, Any]] = []
        for r in rows:
            total = (
                int(r["retrieval_tokens"])
                + int(r["context_tokens"])
                + int(r["overhead_tokens"])
            )
            result.append(
                {
                    "bucket": r["bucket"] or "unknown",
                    "runs": int(r["runs"]),
                    "total_tokens": total,
                    "retrieval_tokens": int(r["retrieval_tokens"]),
                    "context_tokens": int(r["context_tokens"]),
                    "overhead_tokens": int(r["overhead_tokens"]),
                    "entries_used": int(r["entries_used"]),
                }
            )
        return result


# ---------------------------------------------------------------------------
# Period -> cutoff
# ---------------------------------------------------------------------------


def _period_to_cutoff(period: str) -> float:
    """Map a ``period`` string to a Unix-epoch cutoff."""
    now = time.time()
    normalized = (period or "all").strip().lower()
    if normalized == "day":
        return now - 86400.0
    if normalized == "week":
        return now - (7 * 86400.0)
    if normalized == "month":
        return now - (30 * 86400.0)
    # ``all`` and any unknown value -> no filter
    return 0.0


def _empty_summary(period: str) -> Dict[str, Any]:
    """Return a zero-valued summary dict."""
    return {
        "period": period,
        "run_count": 0,
        "total_tokens": 0,
        "retrieval_tokens": 0,
        "context_tokens": 0,
        "overhead_tokens": 0,
        "user_tokens": 0,
        "project_tokens": 0,
        "chat_tokens": 0,
        "agent_tokens": 0,
        "budget_tokens": 0,
        "budget_utilization": 0.0,
        "entries_retrieved": 0,
        "entries_used": 0,
        "entries_dropped": 0,
        "drop_rate": 0.0,
    }


# ---------------------------------------------------------------------------
# MemoryUsageTracker
# ---------------------------------------------------------------------------


class MemoryUsageTracker:
    """Public interface for recording per-run memory usage.

    Wraps the store with two pieces of convenience:

      1. **Per-scope token computation.** The caller passes a
         ``MemoryContext`` and the tracker walks its entries,
         estimating per-scope token counts. The store's row then
         carries the breakdown directly.

      2. **Fail-safe recording.** A failure to persist NEVER
         propagates. Runs complete normally even when the ledger
         is broken (disk full, schema corruption, ...) -- memory
         is an ENHANCEMENT, not a critical-path resource.

    See ``memory_usage.py``'s module docstring for the design
    rationale behind per-run recording.
    """

    def __init__(
        self,
        store: SqliteMemoryUsageStore,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """True when recording is enabled."""
        return self._enabled

    async def record(
        self,
        *,
        run_id: str,
        context: MemoryContext,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        retrieval_tokens: int = 0,
        overhead_tokens: int = 0,
    ) -> bool:
        """Record one run's memory usage.

        Parameters
        ----------
        run_id
            The run this usage belongs to. Mandatory --
            without it the record cannot be correlated with
            LLM invocation records via ``token_ledger``.

        context
            The ``MemoryContext`` produced by
            ``MemoryService.retrieve`` for this run. The
            tracker reads ``used_tokens``, ``token_budget``,
            ``memories``, ``dropped_for_budget``, and
            derives per-scope token counts from the
            memories' scope types.

        chat_id, project_id
            Optional attribution -- the same ids used at
            retrieval time, copied verbatim so the ledger
            can filter by chat or project without a join.

        retrieval_tokens, overhead_tokens
            Additional numeric attribution:
              * ``retrieval_tokens`` -- cost of running the
                retrieval pipeline itself (typically 0 for
                SQLite; non-zero for tokenizing backends).
              * ``overhead_tokens`` -- cost of formatting the
                ``[RETRIEVED MEMORY]`` block (header, labels,
                separators). See
                ``context_builder.format_memory_context`` for
                the exact format this accounts for.

        Returns
        -------
        bool
            True when the row was persisted, False when the
            write was skipped (disabled, duplicate run_id, or
            storage failure). Never raises.
        """
        if not self._enabled:
            return False

        if not isinstance(context, MemoryContext):
            logger.warning(
                "MemoryUsageTracker.record: context is not a "
                "MemoryContext (got %s); skipping.",
                type(context).__name__,
            )
            return False

        if not isinstance(run_id, str) or not run_id.strip():
            logger.warning(
                "MemoryUsageTracker.record: run_id is empty; skipping."
            )
            return False

        try:
            usage = self._build_usage(
                run_id=run_id.strip(),
                context=context,
                chat_id=chat_id,
                project_id=project_id,
                retrieval_tokens=retrieval_tokens,
                overhead_tokens=overhead_tokens,
            )
        except Exception as exc:
            logger.warning(
                "MemoryUsageTracker: failed to build usage for "
                "run %r: %s",
                run_id,
                exc,
            )
            return False

        try:
            return await self._store.save(usage)
        except Exception as exc:
            logger.warning(
                "MemoryUsageTracker: failed to persist usage for "
                "run %r: %s",
                run_id,
                exc,
            )
            return False

    def _build_usage(
        self,
        *,
        run_id: str,
        context: MemoryContext,
        chat_id: Optional[str],
        project_id: Optional[str],
        retrieval_tokens: int,
        overhead_tokens: int,
    ) -> MemoryUsage:
        """Convert ``MemoryContext`` + extras into a ``MemoryUsage``."""
        # ----- Per-scope token computation ----------------------
        #
        # ``MemoryContext.used_tokens`` is the TOTAL injected
        # size. We distribute it across the four scope buckets
        # by proportionally attributing each entry's estimated
        # token count.
        #
        # Estimation uses ``len(content) // 4`` -- the SAME
        # heuristic as ``context_builder._count_tokens``. Using
        # the same function ensures the bucket sums equal
        # ``context.used_tokens`` (with a small rounding
        # remainder added to the LARGEST bucket).
        per_scope: Dict[MemoryScope, int] = {
            MemoryScope.USER: 0,
            MemoryScope.PROJECT: 0,
            MemoryScope.CHAT: 0,
            MemoryScope.AGENT: 0,
        }
        for entry in context.memories:
            tokens = max(1, len(entry.content.strip()) // 4)
            per_scope[entry.scope_type] += tokens

        # Attribute the residual (from header + labels -- the
        # delta between sum-of-contents and used_tokens) to the
        # largest bucket, so the sums match exactly.
        bucket_sum = sum(per_scope.values())
        residual = context.used_tokens - bucket_sum
        if residual > 0 and bucket_sum > 0:
            largest_scope = max(per_scope, key=per_scope.get)
            per_scope[largest_scope] += residual

        return MemoryUsage(
            run_id=run_id,
            chat_id=chat_id,
            project_id=project_id,
            retrieval_tokens=max(0, int(retrieval_tokens)),
            context_tokens=int(context.used_tokens),
            overhead_tokens=max(0, int(overhead_tokens)),
            user_tokens=per_scope[MemoryScope.USER],
            project_tokens=per_scope[MemoryScope.PROJECT],
            chat_tokens=per_scope[MemoryScope.CHAT],
            agent_tokens=per_scope[MemoryScope.AGENT],
            budget_tokens=int(context.token_budget),
            entries_retrieved=(
                len(context.memories) + context.dropped_for_budget
            ),
            entries_used=len(context.memories),
            entries_dropped=int(context.dropped_for_budget),
        )


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------


def get_memory_usage_store(
    db_path: Optional[Path] = None,
) -> SqliteMemoryUsageStore:
    """Return the process-wide ``SqliteMemoryUsageStore``.

    The first call resolves ``db_path``:

      * explicit arg wins,
      * else default to ``<storage_root>/peldrun_runtime.db`` --
        the SAME file used by the token ledger and the run store.
    """
    with SqliteMemoryUsageStore._lock:
        if SqliteMemoryUsageStore._instance is None:
            if db_path is None:
                from peldrun.storage_paths import get_storage_file
                resolved = get_storage_file("peldrun_runtime.db")
            else:
                resolved = Path(db_path).resolve()
            SqliteMemoryUsageStore._instance = (
                SqliteMemoryUsageStore(resolved)
            )
        return SqliteMemoryUsageStore._instance


def get_memory_usage_tracker(
    enabled: bool = True,
) -> MemoryUsageTracker:
    """Return a ``MemoryUsageTracker`` bound to the singleton store.

    Stateless per call -- safe to invoke from any layer.
    """
    return MemoryUsageTracker(
        store=get_memory_usage_store(),
        enabled=enabled,
    )


def reset_for_tests() -> None:
    """Reset the singleton store. Test-only helper.

    Not exported via ``__all__`` -- production code should never
    need to reset the singleton mid-process. Tests that want an
    isolated store should construct ``SqliteMemoryUsageStore``
    directly against a temp path.
    """
    with SqliteMemoryUsageStore._lock:
        if SqliteMemoryUsageStore._instance is not None:
            try:
                SqliteMemoryUsageStore._instance.close_all()
            except Exception:
                pass
        SqliteMemoryUsageStore._instance = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryUsageTracker",
    "SqliteMemoryUsageStore",
    "get_memory_usage_store",
    "get_memory_usage_tracker",
]
