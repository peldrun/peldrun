"""
backend/peldrun/runtime/pricing_store.py

PELDRUN Core Dynamic Pricing & Cost Calculation Subsystem.

Provides durable, versioned, and deterministic model pricing resolution.
Adheres strictly to the following architectural invariants:
- Monetary precision is tracked using integer Nano-USD (1 USD = 1,000,000,000 Nano-USD).
- Deterministic multi-tiered resolution hierarchy:
    Tier 0: Local zero-billing detection (LM Studio / Ollama / Local IPs)
    Tier 1: Exact provider + exact model match
    Tier 2: Exact provider + model pattern wildcard match
    Tier 3: Global exact model match
    Tier 4: Global model pattern wildcard match
- Zero-cost basis for local inference engines.
- Soft retirement semantics (no destructive deletes of historical pricing rules).
- Non-destructive repricing simulation without mutating the immutable token_ledger.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field

from peldrun.llm.client import TokenUsage
from peldrun.runtime.usage_store import NANO_USD_PER_USD, is_local_endpoint

logger = logging.getLogger("peldrun.runtime.pricing_store")


class PricingRule(BaseModel):
    """Canonical domain model defining a versioned pricing rule for token billing."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pricing_id: str = Field(default_factory=lambda: f"prc_{uuid.uuid4().hex[:8]}")
    provider: Optional[str] = Field(default=None, description="Target provider ID or None for wildcard")
    model_pattern: str = Field(..., description="Exact model ID or glob pattern, e.g. 'gpt-4o', 'deepseek*'")

    input_price_nano_usd_per_million: int = Field(default=0, description="Cost per 1M input tokens in Nano-USD")
    output_price_nano_usd_per_million: int = Field(default=0, description="Cost per 1M output tokens in Nano-USD")
    cached_input_price_nano_usd_per_million: Optional[int] = Field(
        default=None,
        description="Optional discounted rate for cached input tokens in Nano-USD per 1M",
    )

    currency: str = Field(default="USD")
    effective_from: float = Field(default_factory=time.time)
    effective_to: Optional[float] = Field(default=None, description="Expiration timestamp for retired rules")

    priority: int = Field(default=0, description="Disambiguation priority; higher takes precedence")
    active: bool = Field(default=True, description="Active status flag; false indicates retired rule")

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @property
    def input_price_usd_per_million(self) -> float:
        return self.input_price_nano_usd_per_million / NANO_USD_PER_USD

    @property
    def output_price_usd_per_million(self) -> float:
        return self.output_price_nano_usd_per_million / NANO_USD_PER_USD

    @property
    def cached_input_price_usd_per_million(self) -> Optional[float]:
        if self.cached_input_price_nano_usd_per_million is not None:
            return self.cached_input_price_nano_usd_per_million / NANO_USD_PER_USD
        return None


# Built-in Default Pricing Seed Catalog (Rates in Nano-USD per 1M tokens)
DEFAULT_PRICING_CATALOG: List[PricingRule] = [
    # OpenAI Tiers
    PricingRule(
        pricing_id="prc_seed_gpt4o",
        provider="openai",
        model_pattern="gpt-4o*",
        input_price_nano_usd_per_million=2_500_000_000,      # $2.50 / 1M
        output_price_nano_usd_per_million=10_000_000_000,    # $10.00 / 1M
        cached_input_price_nano_usd_per_million=1_250_000_000, # $1.25 / 1M
        priority=100,
    ),
    PricingRule(
        pricing_id="prc_seed_gpt4o_mini",
        provider="openai",
        model_pattern="gpt-4o-mini*",
        input_price_nano_usd_per_million=150_000_000,       # $0.15 / 1M
        output_price_nano_usd_per_million=600_000_000,      # $0.60 / 1M
        cached_input_price_nano_usd_per_million=75_000_000,  # $0.075 / 1M
        priority=110,
    ),
    # DeepSeek Tiers
    PricingRule(
        pricing_id="prc_seed_deepseek_chat",
        provider="deepseek",
        model_pattern="deepseek-chat*",
        input_price_nano_usd_per_million=140_000_000,       # $0.14 / 1M
        output_price_nano_usd_per_million=280_000_000,      # $0.28 / 1M
        cached_input_price_nano_usd_per_million=14_000_000,  # $0.014 / 1M
        priority=100,
    ),
    PricingRule(
        pricing_id="prc_seed_deepseek_reasoner",
        provider="deepseek",
        model_pattern="deepseek-reasoner*",
        input_price_nano_usd_per_million=550_000_000,       # $0.55 / 1M
        output_price_nano_usd_per_million=2_190_000_000,    # $2.19 / 1M
        cached_input_price_nano_usd_per_million=140_000_000, # $0.14 / 1M
        priority=110,
    ),
    # Anthropic Tiers
    PricingRule(
        pricing_id="prc_seed_claude_35_sonnet",
        provider="anthropic",
        model_pattern="claude-3-5-sonnet*",
        input_price_nano_usd_per_million=3_000_000_000,     # $3.00 / 1M
        output_price_nano_usd_per_million=15_000_000_000,   # $15.00 / 1M
        cached_input_price_nano_usd_per_million=300_000_000, # $0.30 / 1M
        priority=100,
    ),
    # Global Default Fallback
    PricingRule(
        pricing_id="prc_seed_global_wildcard",
        provider=None,
        model_pattern="*",
        input_price_nano_usd_per_million=0,
        output_price_nano_usd_per_million=0,
        priority=-1000,
    ),
]


class SqlitePricingStore:
    """Thread-safe SQLite storage engine managing the model_pricing catalog."""

    _instance: Optional[SqlitePricingStore] = None
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
        """Initialize model_pricing table and seed initial pricing rules if empty."""
        conn = self._get_connection()
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS model_pricing (
                    pricing_id TEXT PRIMARY KEY,
                    provider TEXT,
                    model_pattern TEXT NOT NULL,
                    input_price_nano_usd_per_million INTEGER NOT NULL DEFAULT 0,
                    output_price_nano_usd_per_million INTEGER NOT NULL DEFAULT 0,
                    cached_input_price_nano_usd_per_million INTEGER,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    effective_from REAL NOT NULL,
                    effective_to REAL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_model_pricing_lookup 
                    ON model_pricing(active, provider, priority DESC);
                CREATE INDEX IF NOT EXISTS idx_model_pricing_pattern 
                    ON model_pricing(model_pattern);
            """)

            # Seed default catalog if table is empty
            cursor = conn.execute("SELECT COUNT(*) AS cnt FROM model_pricing")
            row = cursor.fetchone()
            if row and row["cnt"] == 0:
                for rule in DEFAULT_PRICING_CATALOG:
                    self._sync_save_rule(conn, rule)

    def _sync_save_rule(self, conn: sqlite3.Connection, rule: PricingRule) -> None:
        conn.execute(
            """
            INSERT INTO model_pricing (
                pricing_id, provider, model_pattern,
                input_price_nano_usd_per_million, output_price_nano_usd_per_million,
                cached_input_price_nano_usd_per_million, currency,
                effective_from, effective_to, priority, active,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pricing_id) DO UPDATE SET
                provider = excluded.provider,
                model_pattern = excluded.model_pattern,
                input_price_nano_usd_per_million = excluded.input_price_nano_usd_per_million,
                output_price_nano_usd_per_million = excluded.output_price_nano_usd_per_million,
                cached_input_price_nano_usd_per_million = excluded.cached_input_price_nano_usd_per_million,
                currency = excluded.currency,
                effective_from = excluded.effective_from,
                effective_to = excluded.effective_to,
                priority = excluded.priority,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (
                rule.pricing_id,
                rule.provider.strip().lower() if rule.provider else None,
                rule.model_pattern.strip(),
                rule.input_price_nano_usd_per_million,
                rule.output_price_nano_usd_per_million,
                rule.cached_input_price_nano_usd_per_million,
                rule.currency,
                rule.effective_from,
                rule.effective_to,
                rule.priority,
                1 if rule.active else 0,
                rule.created_at,
                rule.updated_at,
            ),
        )

    # ---------------------------------------------------------------- CRUD Operations
    async def save_rule(self, rule: PricingRule) -> None:
        """Persist or update a pricing rule."""
        await asyncio.to_thread(self._sync_save_rule_external, rule)

    def _sync_save_rule_external(self, rule: PricingRule) -> None:
        conn = self._get_connection()
        with conn:
            self._sync_save_rule(conn, rule)

    async def get_rule(self, pricing_id: str) -> Optional[PricingRule]:
        return await asyncio.to_thread(self._sync_get_rule, pricing_id)

    def _sync_get_rule(self, pricing_id: str) -> Optional[PricingRule]:
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM model_pricing WHERE pricing_id = ?", (pricing_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_rule(row)

    async def list_rules(self, active_only: bool = True) -> List[PricingRule]:
        return await asyncio.to_thread(self._sync_list_rules, active_only)

    def _sync_list_rules(self, active_only: bool = True) -> List[PricingRule]:
        conn = self._get_connection()
        query = "SELECT * FROM model_pricing"
        params: List[Any] = []
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY priority DESC, created_at DESC"

        cursor = conn.execute(query, params)
        return [self._row_to_rule(r) for r in cursor.fetchall()]

    async def retire_rule(self, pricing_id: str) -> bool:
        """
        Soft-retire a pricing rule instead of destructive deletion.
        Sets active=0 and records effective_to timestamp.
        """
        return await asyncio.to_thread(self._sync_retire_rule, pricing_id)

    def _sync_retire_rule(self, pricing_id: str) -> bool:
        conn = self._get_connection()
        now = time.time()
        with conn:
            cursor = conn.execute(
                """
                UPDATE model_pricing
                SET active = 0, effective_to = ?, updated_at = ?
                WHERE pricing_id = ? AND active = 1
                """,
                (now, now, pricing_id),
            )
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- Pricing Resolution
    async def resolve_pricing(
        self,
        provider: str,
        model: str,
        timestamp: Optional[float] = None,
        base_url: Optional[str] = None,
    ) -> PricingRule:
        """
        Deterministically resolve authoritative pricing rule for given provider and model.
        Guarantees local zero-billing detection and multi-tiered pattern matching.
        """
        return await asyncio.to_thread(self._sync_resolve_pricing, provider, model, timestamp, base_url)

    def _sync_resolve_pricing(
        self,
        provider: str,
        model: str,
        timestamp: Optional[float] = None,
        base_url: Optional[str] = None,
    ) -> PricingRule:
        norm_provider = (provider or "").strip().lower()
        norm_model = (model or "").strip()
        ref_time = timestamp or time.time()

        # Tier 0: Local inference engines evaluate directly to zero billing basis
        if is_local_endpoint(norm_provider, base_url):
            return PricingRule(
                pricing_id=f"local_zero_{norm_provider or 'edge'}",
                provider=norm_provider,
                model_pattern=norm_model,
                input_price_nano_usd_per_million=0,
                output_price_nano_usd_per_million=0,
                cached_input_price_nano_usd_per_million=0,
                priority=10000,
                active=True,
            )

        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM model_pricing
            WHERE active = 1
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY priority DESC, created_at DESC
            """,
            (ref_time, ref_time),
        )
        candidates = [self._row_to_rule(r) for r in cursor.fetchall()]

        # Tiered Candidate Partitioning
        tier1_exact_prov_exact_model: List[PricingRule] = []
        tier2_exact_prov_pattern: List[PricingRule] = []
        tier3_global_exact_model: List[PricingRule] = []
        tier4_global_pattern: List[PricingRule] = []

        for rule in candidates:
            rule_prov = (rule.provider or "").strip().lower()
            pattern = rule.model_pattern.strip()
            is_wildcard_prov = rule_prov in ("", "*", "all", "none")

            if not is_wildcard_prov and rule_prov == norm_provider:
                if pattern.lower() == norm_model.lower():
                    tier1_exact_prov_exact_model.append(rule)
                elif fnmatch.fnmatch(norm_model.lower(), pattern.lower()):
                    tier2_exact_prov_pattern.append(rule)

            elif is_wildcard_prov:
                if pattern.lower() == norm_model.lower():
                    tier3_global_exact_model.append(rule)
                elif fnmatch.fnmatch(norm_model.lower(), pattern.lower()):
                    tier4_global_pattern.append(rule)

        for bucket in [
            tier1_exact_prov_exact_model,
            tier2_exact_prov_pattern,
            tier3_global_exact_model,
            tier4_global_pattern,
        ]:
            if bucket:
                # Sort by priority DESC, specific pattern length DESC
                bucket.sort(key=lambda r: (r.priority, len(r.model_pattern)), reverse=True)
                return bucket[0]

        # Fail-safe absolute fallback if catalog is cleared
        return PricingRule(
            pricing_id="prc_unpriced_fallback",
            provider=norm_provider,
            model_pattern=norm_model,
            input_price_nano_usd_per_million=0,
            output_price_nano_usd_per_million=0,
            priority=-9999,
            active=True,
        )

    # ---------------------------------------------------------------- Cost Calculation
    async def calculate_cost(
        self,
        usage: TokenUsage,
        provider: str,
        model: str,
        timestamp: Optional[float] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[int, str]:
        """
        Calculate invocation cost in integer Nano-USD.
        Returns: Tuple of (cost_nano_usd, pricing_version_id).
        """
        return await asyncio.to_thread(self._sync_calculate_cost, usage, provider, model, timestamp, base_url)

    def _sync_calculate_cost(
        self,
        usage: TokenUsage,
        provider: str,
        model: str,
        timestamp: Optional[float] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[int, str]:
        rule = self._sync_resolve_pricing(provider, model, timestamp, base_url)

        input_tok = usage.input_tokens or 0
        cached_tok = usage.cached_input_tokens or 0
        output_tok = usage.output_tokens or 0

        # Account for non-cached vs cached input tokens without double deduction
        cached_price = (
            rule.cached_input_price_nano_usd_per_million
            if rule.cached_input_price_nano_usd_per_million is not None
            else rule.input_price_nano_usd_per_million
        )

        cost_nano = 0
        if 0 < cached_tok <= input_tok:
            non_cached = input_tok - cached_tok
            cost_nano += (non_cached * rule.input_price_nano_usd_per_million) // 1_000_000
            cost_nano += (cached_tok * cached_price) // 1_000_000
        else:
            cost_nano += (input_tok * rule.input_price_nano_usd_per_million) // 1_000_000

        cost_nano += (output_tok * rule.output_price_nano_usd_per_million) // 1_000_000

        return int(cost_nano), rule.pricing_id

    # ---------------------------------------------------------------- Non-destructive Repricing
    async def preview_repricing(
        self,
        chat_id: Optional[str] = None,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Simulate repricing of historical ledger facts with active pricing rules.
        Guarantees that historical token_ledger facts remain unmutated.
        """
        return await asyncio.to_thread(self._sync_preview_repricing, chat_id, from_time, to_time)

    def _sync_preview_repricing(
        self,
        chat_id: Optional[str] = None,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        conn = self._get_connection()
        query = "SELECT * FROM token_ledger WHERE 1=1"
        params: List[Any] = []

        if chat_id:
            query += " AND chat_id = ?"
            params.append(chat_id)
        if from_time:
            query += " AND created_at >= ?"
            params.append(from_time)
        if to_time:
            query += " AND created_at <= ?"
            params.append(to_time)

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

        original_total_nano = 0
        recalculated_total_nano = 0
        total_tokens = 0
        invocations_count = len(rows)

        breakdown_by_model: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            orig_cost = row["cost_nano_usd"] or 0
            original_total_nano += orig_cost

            u = TokenUsage(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                total_tokens=row["total_tokens"],
                cached_input_tokens=row["cached_input_tokens"],
                reasoning_output_tokens=row["reasoning_output_tokens"],
            )

            simulated_cost, rule_id = self._sync_calculate_cost(
                usage=u,
                provider=row["provider"],
                model=row["model_requested"],
            )
            recalculated_total_nano += simulated_cost
            total_tokens += row["total_tokens"] or 0

            mod_key = f"{row['provider']}/{row['model_requested']}"
            if mod_key not in breakdown_by_model:
                breakdown_by_model[mod_key] = {
                    "provider": row["provider"],
                    "model": row["model_requested"],
                    "invocations": 0,
                    "original_nano": 0,
                    "recalculated_nano": 0,
                    "tokens": 0,
                }
            breakdown_by_model[mod_key]["invocations"] += 1
            breakdown_by_model[mod_key]["original_nano"] += orig_cost
            breakdown_by_model[mod_key]["recalculated_nano"] += simulated_cost
            breakdown_by_model[mod_key]["tokens"] += row["total_tokens"] or 0

        # Convert breakdown nano values to readable USD
        for b in breakdown_by_model.values():
            b["original_usd"] = round(b["original_nano"] / NANO_USD_PER_USD, 6)
            b["recalculated_usd"] = round(b["recalculated_nano"] / NANO_USD_PER_USD, 6)
            b["difference_usd"] = round((b["recalculated_nano"] - b["original_nano"]) / NANO_USD_PER_USD, 6)

        diff_nano = recalculated_total_nano - original_total_nano
        return {
            "invocations_evaluated": invocations_count,
            "total_tokens": total_tokens,
            "original_cost_nano_usd": original_total_nano,
            "original_cost_usd": round(original_total_nano / NANO_USD_PER_USD, 6),
            "recalculated_cost_nano_usd": recalculated_total_nano,
            "recalculated_cost_usd": round(recalculated_total_nano / NANO_USD_PER_USD, 6),
            "difference_nano_usd": diff_nano,
            "difference_usd": round(diff_nano / NANO_USD_PER_USD, 6),
            "model_breakdown": list(breakdown_by_model.values()),
        }

    def _row_to_rule(self, row: sqlite3.Row) -> PricingRule:
        return PricingRule(
            pricing_id=row["pricing_id"],
            provider=row["provider"],
            model_pattern=row["model_pattern"],
            input_price_nano_usd_per_million=row["input_price_nano_usd_per_million"],
            output_price_nano_usd_per_million=row["output_price_nano_usd_per_million"],
            cached_input_price_nano_usd_per_million=row["cached_input_price_nano_usd_per_million"],
            currency=row["currency"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            priority=row["priority"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def get_pricing_store(db_path: Optional[Union[str, Path]] = None) -> SqlitePricingStore:
    """Singleton provider returning global thread-safe SqlitePricingStore instance."""
    with SqlitePricingStore._lock:
        if SqlitePricingStore._instance is None:
            SqlitePricingStore._instance = SqlitePricingStore(db_path=db_path)
        return SqlitePricingStore._instance


__all__ = [
    "PricingRule",
    "SqlitePricingStore",
    "get_pricing_store",
    "DEFAULT_PRICING_CATALOG",
]