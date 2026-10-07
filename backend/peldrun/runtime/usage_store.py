"""
backend/peldrun/runtime/usage_store.py

PELDRUN Core Canonical Usage Accounting & Token Ledger Store.

Provides persistent, thread-safe, and immutable storage for LLM invocation records.
Implements the canonical accounting fact pattern where:
- Every LLM invocation produces exactly one durable ledger entry.
- Idempotency is strictly enforced via unique invocation_id.
- Dynamic provider ingestion allows any new local, cloud, or custom endpoint without code changes.
- Monetary values are tracked in integer Nano-USD (1 USD = 1,000,000,000 Nano-USD).
- High-performance indexed aggregations for time-series, summaries, and breakdowns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.runtime.usage_store")

# 1 USD = 1,000,000,000 Nano-USD
NANO_USD_PER_USD = 1_000_000_000


class LLMInvocationRecord(BaseModel):
    """Canonical domain model representing a single completed LLM accounting fact."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    invocation_id: str = Field(..., description="Unique idempotent identifier for this invocation")
    run_id: Optional[str] = None
    job_id: Optional[str] = None
    project_id: Optional[str] = Field(default="default_project")
    chat_id: Optional[str] = None
    turn_id: Optional[str] = None
    step_id: Optional[str] = None

    mode: str = Field(default="agent", description="'agent' or 'chat'")
    provider: str = Field(..., description="Normalized provider ID, e.g. lmstudio, ollama, deepseek, custom-vllm")
    model_requested: str = Field(..., description="Model ID requested by the caller")
    model_returned: Optional[str] = Field(default=None, description="Actual model returned in response headers")

    request_id: Optional[str] = None
    response_id: Optional[str] = None

    attempt: int = Field(default=1, description="Execution attempt counter (1-indexed)")
    status: str = Field(default="completed", description="'completed', 'failed', or 'cancelled'")

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None

    usage_source: str = Field(default="provider", description="'provider', 'estimated', or 'unknown'")
    estimated: bool = Field(default=False)
    tokenizer_id: Optional[str] = None
    tokenizer_version: Optional[str] = None
    estimation_method: Optional[str] = None

    started_at: float = Field(default_factory=time.time)
    first_token_at: Optional[float] = None
    completed_at: Optional[float] = Field(default_factory=time.time)

    latency_ms: Optional[float] = None
    ttft_ms: Optional[float] = None

    finish_reason: Optional[str] = None
    failure_reason: Optional[str] = None

    pricing_version_id: Optional[str] = None
    cost_nano_usd: int = Field(default=0, description="Cost in Nano-USD (integer)")

    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


def is_local_endpoint(provider: str, base_url: Optional[str] = None) -> bool:
    """
    Dynamically determine if an AI endpoint is local (zero billing cost basis).
    Inspects provider name and target network URL without rigid enum restrictions.
    """
    norm_p = (provider or "").strip().lower()
    if norm_p in ("lmstudio", "ollama", "local", "local_gpu"):
        return True

    if base_url:
        norm_url = base_url.strip().lower()
        local_markers = (
            "127.0.0.1",
            "localhost",
            "0.0.0.0",
            ":1234",
            ":11434",
            ":8000",
        )
        if any(marker in norm_url for marker in local_markers):
            return True

    return False


class UsageStore(Protocol):
    """Protocol defining durable token ledger storage contracts."""

    async def record_invocation(self, record: LLMInvocationRecord) -> bool: ...
    async def get_invocation(self, invocation_id: str) -> Optional[Dict[str, Any]]: ...
    async def get_chat_usage_summary(self, chat_id: str) -> Dict[str, Any]: ...
    async def list_chat_invocations(self, chat_id: str) -> List[Dict[str, Any]]: ...
    async def get_global_usage_summary(self) -> Dict[str, Any]: ...
    async def query_summary(self, **filters: Any) -> Dict[str, Any]: ...
    async def query_timeseries(self, granularity: str = "day", **filters: Any) -> List[Dict[str, Any]]: ...
    async def query_breakdown(self, group_by: str = "model", **filters: Any) -> List[Dict[str, Any]]: ...
    async def query_invocations(self, limit: int = 50, cursor: Optional[float] = None, **filters: Any) -> Tuple[List[Dict[str, Any]], Optional[float], bool]: ...


class SqliteUsageStore:
    """Thread-safe SQLite implementation of UsageStore managing the token_ledger fact table."""

    _instance: Optional[SqliteUsageStore] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            storage_dir = Path("storage").resolve()
            storage_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = storage_dir / "peldrun_runtime.db"
        else:
            self.db_path = Path(db_path).resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection configured with WAL mode."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize token_ledger table and optimal composite query indexes."""
        conn = self._get_connection()
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS token_ledger (
                    invocation_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    job_id TEXT,
                    project_id TEXT,
                    chat_id TEXT,
                    turn_id TEXT,
                    step_id TEXT,

                    mode TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_requested TEXT NOT NULL,
                    model_returned TEXT,

                    request_id TEXT,
                    response_id TEXT,

                    attempt INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,

                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    reasoning_output_tokens INTEGER,

                    usage_source TEXT NOT NULL,
                    estimated INTEGER NOT NULL DEFAULT 0,
                    tokenizer_id TEXT,
                    tokenizer_version TEXT,
                    estimation_method TEXT,

                    started_at REAL NOT NULL,
                    first_token_at REAL,
                    completed_at REAL,

                    latency_ms REAL,
                    ttft_ms REAL,

                    finish_reason TEXT,
                    failure_reason TEXT,

                    pricing_version_id TEXT,
                    cost_nano_usd INTEGER NOT NULL DEFAULT 0,

                    metadata_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_token_ledger_created 
                    ON token_ledger(created_at);
                CREATE INDEX IF NOT EXISTS idx_token_ledger_chat_created 
                    ON token_ledger(chat_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_token_ledger_project_created 
                    ON token_ledger(project_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_token_ledger_provider_model_created 
                    ON token_ledger(provider, model_requested, created_at);
                CREATE INDEX IF NOT EXISTS idx_token_ledger_mode_created 
                    ON token_ledger(mode, created_at);
                CREATE INDEX IF NOT EXISTS idx_token_ledger_run 
                    ON token_ledger(run_id);
                CREATE INDEX IF NOT EXISTS idx_token_ledger_turn 
                    ON token_ledger(turn_id);
            """)

    # ---------------------------------------------------------------- Recording
    async def record_invocation(self, record: LLMInvocationRecord) -> bool:
        """Persist an LLM invocation fact idempotently in SQLite."""
        return await asyncio.to_thread(self._sync_record_invocation, record)

    def _sync_record_invocation(self, record: LLMInvocationRecord) -> bool:
        conn = self._get_connection()
        meta_str = json.dumps(record.metadata, ensure_ascii=False)
        with conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO token_ledger (
                    invocation_id, run_id, job_id, project_id, chat_id, turn_id, step_id,
                    mode, provider, model_requested, model_returned,
                    request_id, response_id, attempt, status,
                    input_tokens, output_tokens, total_tokens,
                    cached_input_tokens, reasoning_output_tokens,
                    usage_source, estimated, tokenizer_id, tokenizer_version, estimation_method,
                    started_at, first_token_at, completed_at, latency_ms, ttft_ms,
                    finish_reason, failure_reason, pricing_version_id, cost_nano_usd,
                    metadata_json, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    record.invocation_id,
                    record.run_id,
                    record.job_id,
                    record.project_id or "default_project",
                    record.chat_id,
                    record.turn_id,
                    record.step_id,
                    record.mode,
                    record.provider.strip().lower(),
                    record.model_requested.strip(),
                    record.model_returned.strip() if record.model_returned else None,
                    record.request_id,
                    record.response_id,
                    record.attempt,
                    record.status,
                    record.input_tokens,
                    record.output_tokens,
                    record.total_tokens,
                    record.cached_input_tokens,
                    record.reasoning_output_tokens,
                    record.usage_source,
                    1 if record.estimated else 0,
                    record.tokenizer_id,
                    record.tokenizer_version,
                    record.estimation_method,
                    record.started_at,
                    record.first_token_at,
                    record.completed_at,
                    record.latency_ms,
                    record.ttft_ms,
                    record.finish_reason,
                    record.failure_reason,
                    record.pricing_version_id,
                    int(record.cost_nano_usd),
                    meta_str,
                    record.created_at,
                ),
            )
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- Retrieval
    async def get_invocation(self, invocation_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_get_invocation, invocation_id)

    def _sync_get_invocation(self, invocation_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM token_ledger WHERE invocation_id = ?",
            (invocation_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["metadata"] = json.loads(res.pop("metadata_json", "{}") or "{}")
        res["cost_usd"] = res["cost_nano_usd"] / NANO_USD_PER_USD
        return res

    async def list_chat_invocations(self, chat_id: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._sync_list_chat_invocations, chat_id)

    def _sync_list_chat_invocations(self, chat_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM token_ledger 
            WHERE chat_id = ? 
            ORDER BY created_at ASC
            """,
            (chat_id,),
        )
        invocations = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
            item["cost_usd"] = item["cost_nano_usd"] / NANO_USD_PER_USD
            invocations.append(item)
        return invocations

    async def get_chat_usage_summary(self, chat_id: str) -> Dict[str, Any]:
        """Aggregate all historical token metrics and costs for a given chat session."""
        return await asyncio.to_thread(self._sync_get_chat_usage_summary, chat_id)

    def _sync_get_chat_usage_summary(self, chat_id: str) -> Dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT 
                COUNT(*) as call_count,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cached_input_tokens), 0) as total_cached,
                COALESCE(SUM(reasoning_output_tokens), 0) as total_reasoning,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano,
                COALESCE(SUM(CASE WHEN estimated = 1 THEN total_tokens ELSE 0 END), 0) as estimated_tokens
            FROM token_ledger
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = cursor.fetchone()
        if not row or row["call_count"] == 0:
            return {
                "chat_id": chat_id,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost_nano_usd": 0,
                "total_cost_usd": 0.0,
                "llm_call_count": 0,
                "estimated_tokens": 0,
            }

        cost_nano = int(row["total_cost_nano"])
        return {
            "chat_id": chat_id,
            "input_tokens": int(row["total_input"]),
            "output_tokens": int(row["total_output"]),
            "total_tokens": int(row["total_tokens"]),
            "cached_input_tokens": int(row["total_cached"]),
            "reasoning_output_tokens": int(row["total_reasoning"]),
            "cost_nano_usd": cost_nano,
            "total_cost_usd": round(cost_nano / NANO_USD_PER_USD, 6),
            "llm_call_count": int(row["call_count"]),
            "estimated_tokens": int(row["estimated_tokens"]),
        }

    async def get_global_usage_summary(self) -> Dict[str, Any]:
        """Aggregate system-wide consumption across all chats, jobs, and models."""
        return await asyncio.to_thread(self._sync_get_global_usage_summary)

    def _sync_get_global_usage_summary(self) -> Dict[str, Any]:
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT 
                COUNT(*) as call_count,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cached_input_tokens), 0) as total_cached,
                COALESCE(SUM(reasoning_output_tokens), 0) as total_reasoning,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano,
                COALESCE(SUM(CASE WHEN estimated = 1 THEN total_tokens ELSE 0 END), 0) as estimated_tokens
            FROM token_ledger
            """
        )
        row = cursor.fetchone()
        cost_nano = int(row["total_cost_nano"] or 0) if row else 0
        return {
            "input_tokens": int(row["total_input"] or 0) if row else 0,
            "output_tokens": int(row["total_output"] or 0) if row else 0,
            "total_tokens": int(row["total_tokens"] or 0) if row else 0,
            "cached_input_tokens": int(row["total_cached"] or 0) if row else 0,
            "reasoning_output_tokens": int(row["total_reasoning"] or 0) if row else 0,
            "cost_nano_usd": cost_nano,
            "total_cost_usd": round(cost_nano / NANO_USD_PER_USD, 6),
            "llm_call_count": int(row["call_count"] or 0) if row else 0,
            "estimated_tokens": int(row["estimated_tokens"] or 0) if row else 0,
        }

    # ---------------------------------------------------------------- Analytics Queries (P1-03D)
    def _build_filter_clause(self, filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
        """Construct WHERE clause predicates and parameter bindings securely."""
        conditions = ["1=1"]
        params: List[Any] = []

        if filters.get("from_time") is not None:
            conditions.append("created_at >= ?")
            params.append(float(filters["from_time"]))

        if filters.get("to_time") is not None:
            conditions.append("created_at <= ?")
            params.append(float(filters["to_time"]))

        if filters.get("chat_id"):
            conditions.append("chat_id = ?")
            params.append(str(filters["chat_id"]))

        if filters.get("project_id"):
            conditions.append("project_id = ?")
            params.append(str(filters["project_id"]))

        if filters.get("provider"):
            conditions.append("provider = ?")
            params.append(str(filters["provider"]).strip().lower())

        if filters.get("model"):
            conditions.append("(model_requested = ? OR model_returned = ?)")
            params.extend([str(filters["model"]), str(filters["model"])])

        if filters.get("mode"):
            conditions.append("mode = ?")
            params.append(str(filters["mode"]).strip().lower())

        if filters.get("status"):
            conditions.append("status = ?")
            params.append(str(filters["status"]).strip().lower())

        if filters.get("estimated_only") is True:
            conditions.append("estimated = 1")
        elif filters.get("exact_only") is True:
            conditions.append("estimated = 0")

        return " AND ".join(conditions), params

    async def query_summary(self, **filters: Any) -> Dict[str, Any]:
        """Execute filtered global KPI summary aggregation."""
        return await asyncio.to_thread(self._sync_query_summary, filters)

    def _sync_query_summary(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        conn = self._get_connection()
        where_clause, params = self._build_filter_clause(filters)

        query = f"""
            SELECT 
                COUNT(*) as call_count,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cached_input_tokens), 0) as total_cached,
                COALESCE(SUM(reasoning_output_tokens), 0) as total_reasoning,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano,
                COALESCE(SUM(CASE WHEN estimated = 1 THEN total_tokens ELSE 0 END), 0) as estimated_tokens,
                COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms,
                COALESCE(AVG(ttft_ms), 0.0) as avg_ttft_ms,
                COALESCE(SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END), 0) as retry_count
            FROM token_ledger
            WHERE {where_clause}
        """
        cursor = conn.execute(query, params)
        row = cursor.fetchone()

        call_cnt = int(row["call_count"] or 0)
        cost_nano = int(row["total_cost_nano"] or 0)
        tot_tok = int(row["total_tokens"] or 0)
        est_tok = int(row["estimated_tokens"] or 0)
        est_pct = round((est_tok / tot_tok * 100.0), 2) if tot_tok > 0 else 0.0

        return {
            "llm_call_count": call_cnt,
            "input_tokens": int(row["total_input"] or 0),
            "output_tokens": int(row["total_output"] or 0),
            "total_tokens": tot_tok,
            "cached_input_tokens": int(row["total_cached"] or 0),
            "reasoning_output_tokens": int(row["total_reasoning"] or 0),
            "estimated_tokens": est_tok,
            "estimated_percentage": est_pct,
            "cost_nano_usd": cost_nano,
            "total_cost_usd": round(cost_nano / NANO_USD_PER_USD, 6),
            "avg_latency_ms": round(float(row["avg_latency_ms"] or 0.0), 2),
            "avg_ttft_ms": round(float(row["avg_ttft_ms"] or 0.0), 2),
            "retry_count": int(row["retry_count"] or 0),
        }

    async def query_timeseries(self, granularity: str = "day", **filters: Any) -> List[Dict[str, Any]]:
        """Aggregate token consumption buckets grouped by day, week, or month."""
        return await asyncio.to_thread(self._sync_query_timeseries, granularity, filters)

    def _sync_query_timeseries(self, granularity: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        where_clause, params = self._build_filter_clause(filters)

        fmt = "%Y-%m-%d"
        if granularity == "week":
            fmt = "%Y-W%W"
        elif granularity == "month":
            fmt = "%Y-%m"

        query = f"""
            SELECT 
                strftime('{fmt}', datetime(created_at, 'unixepoch')) as bucket,
                COUNT(*) as call_count,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano
            FROM token_ledger
            WHERE {where_clause}
            GROUP BY bucket
            ORDER BY bucket ASC
        """
        cursor = conn.execute(query, params)
        buckets = []
        for r in cursor.fetchall():
            cost_nano = int(r["total_cost_nano"] or 0)
            buckets.append({
                "bucket": r["bucket"] or "unknown",
                "llm_call_count": int(r["call_count"] or 0),
                "input_tokens": int(r["total_input"] or 0),
                "output_tokens": int(r["total_output"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "cost_nano_usd": cost_nano,
                "total_cost_usd": round(cost_nano / NANO_USD_PER_USD, 6),
            })
        return buckets

    async def query_breakdown(self, group_by: str = "model", **filters: Any) -> List[Dict[str, Any]]:
        """Aggregate token consumption partitioned by dimension (model, provider, mode, project)."""
        return await asyncio.to_thread(self._sync_query_breakdown, group_by, filters)

    def _sync_query_breakdown(self, group_by: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        where_clause, params = self._build_filter_clause(filters)

        col_map = {
            "model": "model_requested",
            "provider": "provider",
            "mode": "mode",
            "project": "project_id",
        }
        col = col_map.get(group_by.lower(), "model_requested")

        query = f"""
            SELECT 
                {col} as key_name,
                COUNT(*) as call_count,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_nano_usd), 0) as total_cost_nano,
                COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms
            FROM token_ledger
            WHERE {where_clause}
            GROUP BY key_name
            ORDER BY total_tokens DESC
        """
        cursor = conn.execute(query, params)
        items = []
        for r in cursor.fetchall():
            cost_nano = int(r["total_cost_nano"] or 0)
            items.append({
                "dimension": group_by,
                "key": r["key_name"] or "unknown",
                "llm_call_count": int(r["call_count"] or 0),
                "input_tokens": int(r["total_input"] or 0),
                "output_tokens": int(r["total_output"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "cost_nano_usd": cost_nano,
                "total_cost_usd": round(cost_nano / NANO_USD_PER_USD, 6),
                "avg_latency_ms": round(float(r["avg_latency_ms"] or 0.0), 2),
            })
        return items

    async def query_invocations(
        self,
        limit: int = 50,
        cursor: Optional[float] = None,
        **filters: Any,
    ) -> Tuple[List[Dict[str, Any]], Optional[float], bool]:
        """Perform cursor-based pagination query on historical token_ledger facts."""
        return await asyncio.to_thread(self._sync_query_invocations, limit, cursor, filters)

    def _sync_query_invocations(
        self,
        limit: int,
        cursor: Optional[float],
        filters: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[float], bool]:
        conn = self._get_connection()
        where_clause, params = self._build_filter_clause(filters)

        safe_limit = max(1, min(limit, 200))
        if cursor is not None:
            where_clause += " AND created_at < ?"
            params.append(float(cursor))

        query = f"""
            SELECT * FROM token_ledger
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(safe_limit + 1)

        db_cursor = conn.execute(query, params)
        rows = db_cursor.fetchall()

        has_more = len(rows) > safe_limit
        selected_rows = rows[:safe_limit]

        items = []
        next_cursor = None
        for r in selected_rows:
            rec = dict(r)
            rec["metadata"] = json.loads(rec.pop("metadata_json", "{}") or "{}")
            rec["cost_usd"] = rec["cost_nano_usd"] / NANO_USD_PER_USD
            items.append(rec)

        if items and has_more:
            next_cursor = items[-1]["created_at"]

        return items, next_cursor, has_more


def get_usage_store(db_path: Optional[Union[str, Path]] = None) -> SqliteUsageStore:
    """Singleton provider returning global thread-safe SqliteUsageStore instance."""
    with SqliteUsageStore._lock:
        if SqliteUsageStore._instance is None:
            SqliteUsageStore._instance = SqliteUsageStore(db_path=db_path)
        return SqliteUsageStore._instance


__all__ = [
    "NANO_USD_PER_USD",
    "LLMInvocationRecord",
    "UsageStore",
    "SqliteUsageStore",
    "get_usage_store",
    "is_local_endpoint",
]