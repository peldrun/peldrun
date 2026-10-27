"""
backend/peldrun/runtime/search_usage_store.py

PELDRUN Dedicated Search Telemetry & Quota Accounting Store.
Uses an isolated SQLite database file with guaranteed connection closure
and automatic schema migration to record provider status, physical request
counts, provider-reported usage units, cost basis, and optional privacy
redaction without blocking the asyncio event loop.

Outcome class invariants (mutual exclusion enforced at the write boundary):
  - success : is_success=1, status='success'
  - empty   : is_success=0, status='empty'
  - failure : is_success=0, status=<any other value>

Because these three classes are strictly disjoint in the ledger,
get_summary_metrics can compute:
    failed_queries = total_queries - successful_queries - empty_queries
without ever double-counting a valid empty search as a failure.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Set, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.runtime.search_usage_store")

NANO_USD_PER_USD = 1_000_000_000
REDACTED_QUERY_PLACEHOLDER = "[REDACTED_PRIVACY_MODE]"

# Aliases used to normalize the (status, is_success) pair into the three
# canonical, mutually exclusive outcome classes. Any status not listed here
# is treated as a genuine failure (is_success=0).
_SUCCESS_STATUS_ALIASES = frozenset(
    {"success", "completed", "ok", "succeeded", "finished", "passed"}
)
_EMPTY_STATUS_ALIASES = frozenset(
    {"empty", "no_results", "zero_results", "nothing_found"}
)


class SearchUsageRecord(BaseModel):
    """Immutable domain model for search operation telemetry."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = Field(default_factory=time.time)
    query: str
    query_hash: str
    provider: str
    status: str = "success"
    is_success: bool = True
    request_count: int = 1
    usage_units: Optional[float] = None
    results_count: int = 0
    latency_ms: float = 0.0
    cost_nano_usd: int = 0
    cost_basis: str = "unknown"
    error_message: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None


_singleton_lock: threading.Lock = threading.Lock()


class SearchUsageStore:
    """Isolated thread-safe SQLite ledger with per-operation connection management."""

    _instance: Optional["SearchUsageStore"] = None
    _db_lock: threading.RLock = threading.RLock()

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            # Audit items 2.2 + 2.3: use the centralized storage root so
            # this database always lives at the same absolute path
            # regardless of the CWD the application was launched from.
            # Previously it picked between backend/storage and storage/
            # based on a runtime existence check, which silently routed
            # writes to a new empty database the moment backend/storage
            # was recreated by an unrelated subsystem.
            from peldrun.storage_paths import get_storage_file

            self.db_path = get_storage_file("peldrun_search_usage.db")
        else:
            self.db_path = Path(db_path).resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_database()

    def _init_database(self) -> None:
        with self._db_lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5.0)
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS search_usage_ledger (
                            record_id TEXT PRIMARY KEY,
                            timestamp REAL NOT NULL,
                            query TEXT NOT NULL,
                            query_hash TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'success',
                            is_success INTEGER NOT NULL,
                            request_count INTEGER NOT NULL DEFAULT 1,
                            usage_units REAL,
                            results_count INTEGER NOT NULL,
                            latency_ms REAL NOT NULL,
                            cost_nano_usd INTEGER NOT NULL,
                            cost_basis TEXT NOT NULL DEFAULT 'unknown',
                            error_message TEXT,
                            session_id TEXT,
                            run_id TEXT
                        );
                        """
                    )
                    self._migrate_schema_if_needed(conn)
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_search_ledger_ts "
                        "ON search_usage_ledger(timestamp);"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_search_ledger_prov "
                        "ON search_usage_ledger(provider);"
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                logger.error(f"Failed to initialize peldrun_search_usage schema: {exc}")

    @staticmethod
    def _migrate_schema_if_needed(conn: sqlite3.Connection) -> None:
        """
        Safely add new accounting columns to pre-existing SQLite ledger files
        without dropping existing telemetry records.
        """
        rows = conn.execute("PRAGMA table_info(search_usage_ledger);").fetchall()
        existing_cols: Set[str] = {str(r[1]) for r in rows}

        migrations = [
            ("status", "TEXT NOT NULL DEFAULT 'success'"),
            ("request_count", "INTEGER NOT NULL DEFAULT 1"),
            ("usage_units", "REAL"),
            ("cost_basis", "TEXT NOT NULL DEFAULT 'unknown'"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                conn.execute(
                    f"ALTER TABLE search_usage_ledger ADD COLUMN {col_name} {col_def};"
                )

    # ------------------------------------------------------------------
    # Outcome-class normalization
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_outcome(
        status: Optional[str],
        is_success: bool,
        results_count: int,
        error_message: Optional[str],
    ) -> tuple[str, bool]:
        """
        Normalize (status, is_success) into one of the three mutually exclusive
        outcome classes so downstream aggregations cannot double-count a record:

            - success : ("success", True)
            - empty   : ("empty",   False)
            - failure : (<status>,  False)   # any other status

        An explicit `status` wins over `is_success` and is coerced to a known
        canonical form. When no status is supplied, the outcome is derived from
        is_success + results_count + error_message.
        """
        # Unwrap enum-like statuses (e.g. ProviderStatus.EMPTY → "empty").
        raw_status: Any = status
        if hasattr(raw_status, "value"):
            raw_status = raw_status.value

        if raw_status is None or str(raw_status).strip() == "":
            if is_success:
                return ("success", True)
            if int(results_count) == 0 and not error_message:
                return ("empty", False)
            return ("upstream_error", False)

        st = str(raw_status).strip().lower()
        if st in _SUCCESS_STATUS_ALIASES:
            return ("success", True)
        if st in _EMPTY_STATUS_ALIASES:
            return ("empty", False)
        return (st, False)

    def record_search(
        self,
        query: str,
        provider: str,
        results_count: int,
        latency_ms: float,
        cost_usd: float = 0.0,
        is_success: bool = True,
        error_message: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        *,
        status: Optional[str] = None,
        request_count: int = 1,
        usage_units: Optional[float] = None,
        cost_basis: str = "unknown",
        privacy_mode: bool = False,
        record_id: Optional[str] = None,
    ) -> Optional[SearchUsageRecord]:
        try:
            clean_q = (query or "").strip()
            q_hash = hashlib.sha256(clean_q.lower().encode("utf-8")).hexdigest()[:16]
            stored_query = REDACTED_QUERY_PLACEHOLDER if privacy_mode else clean_q

            safe_cost = max(0.0, float(cost_usd or 0.0))
            cost_nano = int(round(safe_cost * NANO_USD_PER_USD))

            # Enforce the mutually exclusive (status, is_success) invariant at
            # the write boundary so get_summary_metrics arithmetic is always safe.
            effective_status, effective_is_success = self._normalize_outcome(
                status=status,
                is_success=bool(is_success),
                results_count=max(0, int(results_count)),
                error_message=error_message,
            )
            safe_req_count = max(0, int(request_count if request_count is not None else 1))

            record = SearchUsageRecord(
                record_id=record_id or str(uuid.uuid4()),
                query=stored_query,
                query_hash=q_hash,
                provider=provider,
                status=effective_status,
                is_success=effective_is_success,
                request_count=safe_req_count,
                usage_units=float(usage_units) if usage_units is not None else None,
                results_count=max(0, int(results_count)),
                latency_ms=max(0.0, float(latency_ms)),
                cost_nano_usd=cost_nano,
                cost_basis=str(cost_basis or "unknown"),
                error_message=error_message,
                session_id=session_id,
                run_id=run_id,
            )

            with self._db_lock:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO search_usage_ledger (
                            record_id, timestamp, query, query_hash, provider,
                            status, is_success, request_count, usage_units,
                            results_count, latency_ms, cost_nano_usd, cost_basis,
                            error_message, session_id, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            record.record_id,
                            record.timestamp,
                            record.query,
                            record.query_hash,
                            record.provider,
                            record.status,
                            1 if record.is_success else 0,
                            record.request_count,
                            record.usage_units,
                            record.results_count,
                            record.latency_ms,
                            record.cost_nano_usd,
                            record.cost_basis,
                            record.error_message,
                            record.session_id,
                            record.run_id,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            return record
        except Exception as exc:
            logger.warning(f"Failed to record search telemetry: {exc}")
            return None

    def clear_ledger(self) -> int:
        """
        Delete all persisted search usage records from the local SQLite ledger.
        Returns the number of deleted rows.
        """
        try:
            with self._db_lock:
                conn = sqlite3.connect(str(self.db_path), timeout=5.0)
                try:
                    cursor = conn.execute("DELETE FROM search_usage_ledger;")
                    deleted = (
                        cursor.rowcount
                        if cursor.rowcount is not None and cursor.rowcount >= 0
                        else 0
                    )
                    conn.commit()
                    return deleted
                finally:
                    conn.close()
        except Exception as exc:
            logger.error(f"Failed to clear search usage ledger: {exc}")
            return 0

    def get_summary_metrics(self, period: str = "all") -> Dict[str, Any]:
        cutoff = self._get_period_cutoff(period)
        # Outcome classes are guaranteed mutually exclusive by _normalize_outcome,
        # but the SQL below additionally excludes any legacy record where
        # status='empty' AND is_success=1 (pre-hardening corruption) from the
        # successful bucket so that failed_q = total - succ - empty is always valid.
        query_sql = """
            SELECT
                COUNT(*) as total_queries,
                COALESCE(SUM(request_count), COUNT(*)) as total_requests,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano,
                COALESCE(
                    SUM(CASE WHEN is_success = 1 AND status <> 'empty' THEN 1 ELSE 0 END),
                    0
                ) as successful_queries,
                COALESCE(
                    SUM(CASE WHEN status = 'empty' THEN 1 ELSE 0 END),
                    0
                ) as empty_queries,
                COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms,
                COALESCE(SUM(results_count), 0) as total_results
            FROM search_usage_ledger
            WHERE timestamp >= ?;
        """
        try:
            with self._db_lock:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(query_sql, (cutoff,)).fetchone()
                finally:
                    conn.close()

            if not row:
                return self._empty_summary(period)

            total_q = int(row["total_queries"] or 0)
            succ_q = int(row["successful_queries"] or 0)
            empty_q = int(row["empty_queries"] or 0)

            # Successful, empty, and failed are mutually exclusive; empty searches
            # must NOT be counted again as failures.
            failed_q = max(0, total_q - succ_q - empty_q)

            return {
                "period": period,
                "total_queries": total_q,
                "total_requests": int(row["total_requests"] or 0),
                "total_cost_usd": round(
                    float(row["total_cost_nano"]) / NANO_USD_PER_USD, 4
                ),
                "successful_queries": succ_q,
                "empty_queries": empty_q,
                "failed_queries": failed_q,
                "success_rate_pct": round(
                    (succ_q / total_q * 100.0) if total_q > 0 else 0.0, 2
                ),
                "avg_latency_ms": round(float(row["avg_latency_ms"] or 0.0), 2),
                "total_results": int(row["total_results"] or 0),
            }
        except Exception as exc:
            logger.warning(f"Failed to read summary metrics: {exc}")
            return self._empty_summary(period)

    def get_provider_breakdown(self, period: str = "all") -> List[Dict[str, Any]]:
        cutoff = self._get_period_cutoff(period)
        # As with get_summary_metrics, exclude status='empty' records from the
        # success bucket so per-provider success_rate_pct does not conflate a
        # valid empty search with a successful one.
        query_sql = """
            SELECT
                provider,
                COUNT(*) as queries_count,
                COALESCE(SUM(request_count), COUNT(*)) as requests_count,
                COALESCE(SUM(cost_nano_usd), 0) as cost_nano,
                COALESCE(
                    SUM(CASE WHEN is_success = 1 AND status <> 'empty' THEN 1 ELSE 0 END),
                    0
                ) as successes,
                COALESCE(
                    SUM(CASE WHEN status = 'empty' THEN 1 ELSE 0 END),
                    0
                ) as empty_count,
                COALESCE(
                    SUM(CASE WHEN is_success = 0 AND status <> 'empty' THEN 1 ELSE 0 END),
                    0
                ) as failed_count,
                COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms
            FROM search_usage_ledger
            WHERE timestamp >= ?
            GROUP BY provider
            ORDER BY queries_count DESC;
        """
        try:
            items: List[Dict[str, Any]] = []
            with self._db_lock:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(query_sql, (cutoff,)).fetchall()
                finally:
                    conn.close()

            for r in rows:
                q_count = int(r["queries_count"] or 0)
                succ = int(r["successes"] or 0)
                empty_c = int(r["empty_count"] or 0)
                failed_c = int(r["failed_count"] or 0)
                items.append(
                    {
                        "provider": r["provider"],
                        "queries_count": q_count,
                        "requests_count": int(r["requests_count"] or q_count),
                        "cost_usd": round(
                            float(r["cost_nano"]) / NANO_USD_PER_USD, 4
                        ),
                        "successful_count": succ,
                        "empty_count": empty_c,
                        "failed_count": failed_c,
                        "success_rate_pct": round(
                            (succ / q_count * 100.0) if q_count > 0 else 0.0, 2
                        ),
                        "avg_latency_ms": round(float(r["avg_latency_ms"] or 0.0), 2),
                    }
                )
            return items
        except Exception as exc:
            logger.warning(f"Failed to read provider breakdown: {exc}")
            return []

    def get_timeseries(self, period: str = "week") -> List[Dict[str, Any]]:
        cutoff = self._get_period_cutoff(period)
        query_sql = """
            SELECT
                strftime('%Y-%m-%d', datetime(timestamp, 'unixepoch')) as bucket_date,
                COUNT(*) as queries,
                COALESCE(SUM(cost_nano_usd), 0) as cost_nano,
                COALESCE(
                    SUM(CASE WHEN is_success = 1 AND status <> 'empty' THEN 1 ELSE 0 END),
                    0
                ) as successful,
                COALESCE(
                    SUM(CASE WHEN status = 'empty' THEN 1 ELSE 0 END),
                    0
                ) as empty_count,
                COALESCE(
                    SUM(CASE WHEN is_success = 0 AND status <> 'empty' THEN 1 ELSE 0 END),
                    0
                ) as failed_count
            FROM search_usage_ledger
            WHERE timestamp >= ?
            GROUP BY bucket_date
            ORDER BY bucket_date ASC;
        """
        try:
            timeseries: List[Dict[str, Any]] = []
            with self._db_lock:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(query_sql, (cutoff,)).fetchall()
                finally:
                    conn.close()

            for r in rows:
                timeseries.append(
                    {
                        "date": r["bucket_date"],
                        "queries": int(r["queries"] or 0),
                        "cost_usd": round(
                            float(r["cost_nano"]) / NANO_USD_PER_USD, 4
                        ),
                        "successful": int(r["successful"] or 0),
                        "empty": int(r["empty_count"] or 0),
                        "failed": int(r["failed_count"] or 0),
                    }
                )
            return timeseries
        except Exception as exc:
            logger.warning(f"Failed to read search timeseries: {exc}")
            return []

    @staticmethod
    def _get_period_cutoff(period: str) -> float:
        now = time.time()
        p = period.lower()
        if p == "day":
            return now - 86400.0
        if p == "week":
            return now - (7 * 86400.0)
        if p == "month":
            return now - (30 * 86400.0)
        return 0.0

    @staticmethod
    def _empty_summary(period: str) -> Dict[str, Any]:
        return {
            "period": period,
            "total_queries": 0,
            "total_requests": 0,
            "total_cost_usd": 0.0,
            "successful_queries": 0,
            "empty_queries": 0,
            "failed_queries": 0,
            "success_rate_pct": 0.0,
            "avg_latency_ms": 0.0,
            "total_results": 0,
        }


def get_search_usage_store(
    db_path: Optional[Union[str, Path]] = None,
) -> SearchUsageStore:
    if SearchUsageStore._instance is None:
        with _singleton_lock:
            if SearchUsageStore._instance is None:
                SearchUsageStore._instance = SearchUsageStore(db_path=db_path)
    return SearchUsageStore._instance
