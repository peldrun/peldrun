"""
backend/omweb/services/telemetry_service.py

PELDRUN Web Telemetry & Usage Analytics Domain Service.

Coordinates consumption analytics across UsageStore and PricingStore.
Serves dashboard KPIs, timeseries metrics, multi-dimensional breakdowns,
chat-specific reports, non-destructive repricing simulations,
and structured report exports (CSV / JSON).
Strictly respects Web-Core architectural boundaries (P1-02).
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from peldrun.runtime.pricing_store import PricingRule, get_pricing_store
from peldrun.runtime.usage_store import get_usage_store

logger = logging.getLogger("omweb.services.telemetry_service")


class TelemetryService:
    """Authoritative domain service providing usage telemetry queries for the Web layer."""

    def __init__(self) -> None:
        self.usage_store = get_usage_store()
        self.pricing_store = get_pricing_store()

    async def get_dashboard_summary(
        self,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        project_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile complete top-level KPI metrics for the analytics dashboard."""
        return await self.usage_store.query_summary(
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            model=model,
            mode=mode,
        )

    async def get_timeseries_metrics(
        self,
        granularity: str = "day",
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        project_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve aggregated time-series buckets (day, week, month)."""
        safe_granularity = granularity.lower() if granularity.lower() in ("day", "week", "month") else "day"
        return await self.usage_store.query_timeseries(
            granularity=safe_granularity,
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            model=model,
            mode=mode,
        )

    async def get_dimension_breakdown(
        self,
        group_by: str = "model",
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        project_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve consumption breakdown grouped by model, provider, mode, or project."""
        safe_group = group_by.lower() if group_by.lower() in ("model", "provider", "mode", "project") else "model"
        return await self.usage_store.query_breakdown(
            group_by=safe_group,
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            mode=mode,
        )

    async def get_chat_telemetry(self, chat_id: str) -> Dict[str, Any]:
        """Retrieve comprehensive historical token telemetry for a specific chat."""
        summary = await self.usage_store.get_chat_usage_summary(chat_id)
        invocations = await self.usage_store.list_chat_invocations(chat_id)
        return {
            "summary": summary,
            "invocations": invocations,
        }

    async def list_invocations_paginated(
        self,
        limit: int = 50,
        cursor: Optional[float] = None,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch cursor-paginated list of individual LLM invocation ledger records."""
        items, next_cursor, has_more = await self.usage_store.query_invocations(
            limit=limit,
            cursor=cursor,
            from_time=from_time,
            to_time=to_time,
            chat_id=chat_id,
            project_id=project_id,
            provider=provider,
            model=model,
            mode=mode,
        )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit,
        }

    async def list_pricing_rules(self, active_only: bool = True) -> List[PricingRule]:
        """Retrieve configured pricing catalog."""
        return await self.pricing_store.list_rules(active_only=active_only)

    async def save_pricing_rule(self, rule: PricingRule) -> None:
        """Register or update a model pricing rule."""
        await self.pricing_store.save_rule(rule)

    async def retire_pricing_rule(self, pricing_id: str) -> bool:
        """Soft-retire a pricing rule without destructive deletion."""
        return await self.pricing_store.retire_rule(pricing_id)

    async def simulate_repricing(
        self,
        chat_id: Optional[str] = None,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Simulate repricing without mutating immutable historical facts."""
        return await self.pricing_store.preview_repricing(
            chat_id=chat_id,
            from_time=from_time,
            to_time=to_time,
        )

    # ---------------------------------------------------------------- Report Exports (P1-03F)
    async def export_report_json(
        self,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile a canonical JSON export report combining summary, timeseries, and breakdowns."""
        summary = await self.get_dashboard_summary(from_time=from_time, to_time=to_time, chat_id=chat_id)
        timeseries = await self.get_timeseries_metrics(granularity="day", from_time=from_time, to_time=to_time, chat_id=chat_id)
        models_breakdown = await self.get_dimension_breakdown(group_by="model", from_time=from_time, to_time=to_time, chat_id=chat_id)
        providers_breakdown = await self.get_dimension_breakdown(group_by="provider", from_time=from_time, to_time=to_time, chat_id=chat_id)

        return {
            "report_title": "PELDRUN Usage & Cost Telemetry Report",
            "generated_at": time.time(),
            "generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "filters": {
                "from_time": from_time,
                "to_time": to_time,
                "chat_id": chat_id,
            },
            "summary": summary,
            "timeseries_daily": timeseries,
            "breakdown_by_model": models_breakdown,
            "breakdown_by_provider": providers_breakdown,
        }

    async def export_report_csv(
        self,
        from_time: Optional[float] = None,
        to_time: Optional[float] = None,
        chat_id: Optional[str] = None,
    ) -> str:
        """Export raw invocations matching filter into a standardized CSV string."""
        invocations, _, _ = await self.usage_store.query_invocations(
            limit=5000,
            from_time=from_time,
            to_time=to_time,
            chat_id=chat_id,
        )

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "invocation_id",
            "created_at",
            "chat_id",
            "mode",
            "provider",
            "model_requested",
            "model_returned",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
            "cost_usd",
            "latency_ms",
            "ttft_ms",
            "status",
            "estimated",
        ])

        for inv in invocations:
            writer.writerow([
                inv.get("invocation_id"),
                inv.get("created_at"),
                inv.get("chat_id"),
                inv.get("mode"),
                inv.get("provider"),
                inv.get("model_requested"),
                inv.get("model_returned"),
                inv.get("input_tokens"),
                inv.get("output_tokens"),
                inv.get("total_tokens"),
                inv.get("cached_input_tokens"),
                inv.get("reasoning_output_tokens"),
                inv.get("cost_usd"),
                inv.get("latency_ms"),
                inv.get("ttft_ms"),
                inv.get("status"),
                inv.get("estimated"),
            ])

        return output.getvalue()


telemetry_service = TelemetryService()

__all__ = ["TelemetryService", "telemetry_service"]