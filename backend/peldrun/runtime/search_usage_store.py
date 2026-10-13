"""
backend/peldrun/runtime/search_usage_store.py

PELDRUN Dedicated Search Telemetry & Quota Accounting Store.
Uses an isolated SQLite database file with guaranteed connection closure
to completely eliminate event loop deadlocks and file lock contention.

FIXED: Separated singleton creation lock from database lock, and made the
DB lock reentrant to prevent the self-deadlock that occurred when
get_search_usage_store() acquired the class lock and then _init_database()
tried to acquire the same lock again.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.runtime.search_usage_store")

NANO_USD_PER_USD = 1_000_000_000


class SearchUsageRecord(BaseModel):
    """Immutable domain model for search operation telemetry."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = Field(default_factory=time.time)
    query: str
    query_hash: str
    provider: str
    is_success: bool = True
    results_count: int = 0
    latency_ms: float = 0.0
    cost_nano_usd: int = 0
    error_message: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None


# ── FIX #1: Two completely independent locks ──────────────────────────────
# _singleton_lock : guards the creation of the singleton instance only.
# _db_lock        : guards SQLite writes/reads. Made reentrant so nested
#                   acquisitions (init -> insert) never deadlock.
_singleton_lock: threading.Lock = threading.Lock()


class SearchUsageStore:
    """Isolated thread-safe SQLite ledger with safe per-query connection management."""

    _instance: Optional["SearchUsageStore"] = None

    # FIX #2: Use RLock (reentrant) instead of Lock.
    # This alone is sufficient to fix the deadlock even if init is nested.
    _db_lock: threading.RLock = threading.RLock()

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent.parent
            storage_dir = project_root / "backend" / "storage"
            if not storage_dir.exists():
                storage_dir = project_root / "storage"
            self.db_path = (storage_dir / "peldrun_search_usage.db").resolve()
        else:
            self.db_path = Path(db_path).resolve()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self) -> None:
        # FIX #3: Use the reentrant DB lock (separate from the singleton lock).
        with self._db_lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5.0)
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS search_usage_ledger (
                            record_id TEXT PRIMARY KEY,
                            timestamp REAL NOT NULL,
                            query TEXT NOT NULL,
                            query_hash TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            is_success INTEGER NOT NULL,
                            results_count INTEGER NOT NULL,
                            latency_ms REAL NOT NULL,
                            cost_nano_usd INTEGER NOT NULL,
                            error_message TEXT,
                            session_id TEXT,
                            run_id TEXT
                        );
                    """)
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
    ) -> Optional[SearchUsageRecord]:
        try:
            clean_q = query.strip()
            q_hash = hashlib.sha256(clean_q.lower().encode("utf-8")).hexdigest()[:16]
            cost_nano = int(round(cost_usd * NANO_USD_PER_USD))

            record = SearchUsageRecord(
                query=clean_q,
                query_hash=q_hash,
                provider=provider,
                is_success=is_success,
                results_count=results_count,
                latency_ms=latency_ms,
                cost_nano_usd=cost_nano,
                error_message=error_message,
                session_id=session_id,
                run_id=run_id,
            )

            with self._db_lock:
                conn = sqlite3.connect(str(self.db_path), timeout=2.0)
                try:
                    conn.execute(
                        """
                        INSERT INTO search_usage_ledger (
                            record_id, timestamp, query, query_hash, provider, is_success,
                            results_count, latency_ms, cost_nano_usd, error_message, session_id, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            record.record_id,
                            record.timestamp,
                            record.query,
                            record.query_hash,
                            record.provider,
                            1 if record.is_success else 0,
                            record.results_count,
                            record.latency_ms,
                            record.cost_nano_usd,
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
            logger.debug(f"Failed to record search telemetry: {exc}")
            return None

    def get_summary_metrics(self, period: str = "all") -> Dict[str, Any]:
        cutoff = self._get_period_cutoff(period)
        query_sql = """
            SELECT
                COUNT(*) as total_queries,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano,
                COALESCE(SUM(CASE WHEN is_success = 1 THEN 1 ELSE 0 END), 0) as successful_queries,
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

            total_q = row["total_queries"]
            succ_q = row["successful_queries"]
            return {
                "period": period,
                "total_queries": total_q,
                "total_cost_usd": round(float(row["total_cost_nano"]) / NANO_USD_PER_USD, 4),
                "successful_queries": succ_q,
                "failed_queries": total_q - succ_q,
                "success_rate_pct": round((succ_q / total_q * 100.0) if total_q > 0 else 0.0, 2),
                "avg_latency_ms": round(row["avg_latency_ms"], 2),
                "total_results": row["total_results"],
            }
        except Exception:
            return self._empty_summary(period)

    def get_provider_breakdown(self, period: str = "all") -> List[Dict[str, Any]]:
        cutoff = self._get_period_cutoff(period)
        query_sql = """
            SELECT
                provider,
                COUNT(*) as queries_count,
                COALESCE(SUM(cost_nano_usd), 0) as cost_nano,
                COALESCE(SUM(CASE WHEN is_success = 1 THEN 1 ELSE 0 END), 0) as successes,
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
                q_count = r["queries_count"]
                items.append({
                    "provider": r["provider"],
                    "queries_count": q_count,
                    "cost_usd": round(float(r["cost_nano"]) / NANO_USD_PER_USD, 4),
                    "success_rate_pct": round((r["successes"] / q_count * 100.0) if q_count > 0 else 0.0, 2),
                    "avg_latency_ms": round(r["avg_latency_ms"], 2),
                })
            return items
        except Exception:
            return []

    def get_timeseries(self, period: str = "week") -> List[Dict[str, Any]]:
        cutoff = self._get_period_cutoff(period)
        query_sql = """
            SELECT
                strftime('%Y-%m-%d', datetime(timestamp, 'unixepoch')) as bucket_date,
                COUNT(*) as queries,
                COALESCE(SUM(cost_nano_usd), 0) as cost_nano,
                COALESCE(SUM(CASE WHEN is_success = 1 THEN 1 ELSE 0 END), 0) as successful
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
                timeseries.append({
                    "date": r["bucket_date"],
                    "queries": r["queries"],
                    "cost_usd": round(float(r["cost_nano"]) / NANO_USD_PER_USD, 4),
                    "successful": r["successful"],
                })
            return timeseries
        except Exception:
            return []

    @staticmethod
    def _get_period_cutoff(period: str) -> float:
        now = time.time()
        p = period.lower()
        if p == "day":
            return now - 86400.0
        elif p == "week":
            return now - (7 * 86400.0)
        elif p == "month":
            return now - (30 * 86400.0)
        return 0.0

    @staticmethod
    def _empty_summary(period: str) -> Dict[str, Any]:
        return {
            "period": period,
            "total_queries": 0,
            "total_cost_usd": 0.0,
            "successful_queries": 0,
            "failed_queries": 0,
            "success_rate_pct": 0.0,
            "avg_latency_ms": 0.0,
            "total_results": 0,
        }


def get_search_usage_store(db_path: Optional[Union[str, Path]] = None) -> SearchUsageStore:
    """
    FIX #4: Use the dedicated _singleton_lock (NOT the class _db_lock).
    The instance constructor now uses _db_lock internally, so there is no
    nested acquisition of the same lock object and no deadlock.
    """
    if SearchUsageStore._instance is None:
        with _singleton_lock:
            if SearchUsageStore._instance is None:
                SearchUsageStore._instance = SearchUsageStore(db_path=db_path)
    return SearchUsageStore._instance
