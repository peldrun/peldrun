"""
backend/omweb/routers/search_rtr.py

PELDRUN Search Orchestration & Telemetry Router.
Provides endpoints for:
  - GET  /api/search/metrics          → aggregated telemetry
  - GET  /api/search/config           → current search config (masked)
  - POST /api/search/config           → persist search config to config.toml
  - POST /api/search/test-connection  → ping a provider
  - POST /api/search/test-query       → run a live diagnostic query

All SQLite and file I/O run inside worker threads so the event loop is
never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from omweb.routers.config_rtr import (
    mask_secrets,
    merge_unmasked,
    read_raw_config,
    write_raw_config,
)
from peldrun.runtime.search_usage_store import get_search_usage_store
from peldrun.tools.builtins.search_providers.factory import SearchProviderFactory

logger = logging.getLogger("omweb.routers.search_rtr")

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class TestConnectionRequest(BaseModel):
    provider: str
    config: Optional[Dict[str, Any]] = None


class TestQueryRequest(BaseModel):
    provider: str
    query: str
    max_results: Optional[int] = 3


class SearchConfigPayload(BaseModel):
    """Payload for updating the [search] table in config.toml."""
    search_config: Dict[str, Any] = Field(
        ...,
        description="Search sub-table settings matching [search] in config.toml",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get_store_async():
    """
    Obtain the singleton store from a worker thread so that the first-time
    initialization (file I/O, WAL setup, table creation) never runs on the
    event loop.
    """
    return await asyncio.to_thread(get_search_usage_store)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@router.get("/metrics")
async def get_search_metrics(
    period: str = Query(
        "all",
        description="Aggregation timeframe: 'day', 'week', 'month', or 'all'",
    ),
):
    """Return aggregated telemetry: summary, breakdown, and timeseries."""
    try:
        store = await _get_store_async()
        summary = await asyncio.to_thread(store.get_summary_metrics, period)
        breakdown = await asyncio.to_thread(store.get_provider_breakdown, period)
        timeseries_period = "month" if period.lower() == "all" else period.lower()
        timeseries = await asyncio.to_thread(store.get_timeseries, timeseries_period)
        supported_providers = SearchProviderFactory.list_supported_providers()

        return {
            "ok": True,
            "period": period,
            "summary": summary,
            "breakdown": breakdown,
            "timeseries": timeseries,
            "supported_providers": supported_providers,
        }
    except Exception as exc:
        logger.error(f"Failed to compile search metrics: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Configuration: read
# ---------------------------------------------------------------------------
@router.get("/config")
async def get_search_configuration():
    """Return the current [search] table with secrets masked."""
    try:
        raw = await asyncio.to_thread(read_raw_config)
        search_section = raw.get("search", {})
        return {
            "ok": True,
            "search_config": mask_secrets(search_section),
            "supported_providers": SearchProviderFactory.list_supported_providers(),
        }
    except Exception as exc:
        logger.error(f"Failed to read search configuration: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Configuration: write (CRITICAL — was missing, caused HTTP 404)
# ---------------------------------------------------------------------------
@router.post("/config")
async def update_search_configuration(payload: SearchConfigPayload):
    """
    Persist the [search] table to config.toml atomically.
    Existing API keys are preserved when the incoming value is masked.
    """
    try:
        current_full_config = await asyncio.to_thread(read_raw_config)
        current_search_section = current_full_config.get("search", {})

        # Merge incoming values on top of existing config.
        # merge_unmasked() keeps the existing secret whenever the incoming
        # value looks like a mask (e.g. "••••••").
        merged_search = merge_unmasked(
            payload.search_config,
            current_search_section,
        )
        current_full_config["search"] = merged_search

        # Write back to disk from a worker thread.
        await asyncio.to_thread(write_raw_config, current_full_config)

        logger.info(
            "Search configuration persisted. "
            f"engine={merged_search.get('engine')} "
            f"fallback={merged_search.get('fallback_engines')}"
        )

        return {
            "ok": True,
            "status": "saved",
            "search_config": mask_secrets(merged_search),
        }
    except Exception as exc:
        logger.error(f"Failed to save search configuration: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist search config: {exc}",
        )


# ---------------------------------------------------------------------------
# Diagnostics: ping a provider
# ---------------------------------------------------------------------------
@router.post("/test-connection")
async def test_search_provider_connection(payload: TestConnectionRequest):
    """Ping a provider with a short timeout."""
    raw_config = await asyncio.to_thread(read_raw_config)
    system_search_config = raw_config.get("search", {})
    target_config = system_search_config
    if payload.config:
        target_config = merge_unmasked(payload.config, system_search_config)

    start_time = time.perf_counter()
    try:
        provider = SearchProviderFactory.create_provider(
            name=payload.provider,
            config=target_config,
        )
        is_alive = await asyncio.wait_for(provider.ping(), timeout=6.0)
        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "ok": is_alive,
            "provider": provider.provider_name,
            "latency_ms": elapsed_ms,
            "message": "Connection verified." if is_alive else "Ping returned false.",
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        return {
            "ok": False,
            "provider": payload.provider,
            "latency_ms": elapsed_ms,
            "message": f"Connection error: {str(exc)}",
        }


# ---------------------------------------------------------------------------
# Diagnostics: run a live query
# ---------------------------------------------------------------------------
@router.post("/test-query")
async def test_search_provider_query(payload: TestQueryRequest):
    """Run a live query bounded by a strict timeout."""
    raw_config = await asyncio.to_thread(read_raw_config)
    system_search_config = raw_config.get("search", {})

    try:
        provider = SearchProviderFactory.create_provider(
            name=payload.provider,
            config=system_search_config,
        )
        response = await asyncio.wait_for(
            provider.search(
                query=payload.query,
                max_results=payload.max_results or 3,
                lang=system_search_config.get("lang", "en"),
                country=system_search_config.get("country", "us"),
            ),
            timeout=10.0,
        )

        # Persist the diagnostic attempt for accounting (best-effort).
        try:
            store = await _get_store_async()
            await asyncio.to_thread(
                store.record_search,
                query=payload.query,
                provider=response.provider,
                results_count=len(response.results),
                latency_ms=response.latency_ms,
                cost_usd=response.estimated_cost_usd,
                is_success=response.is_success,
                error_message=response.error_message,
                run_id="diagnostic_test",
            )
        except Exception:
            pass

        return {
            "ok": response.is_success,
            "provider": response.provider,
            "latency_ms": response.latency_ms,
            "results_count": len(response.results),
            "results": [r.model_dump() for r in response.results],
            "error_message": response.error_message,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": payload.provider,
            "latency_ms": 0.0,
            "results_count": 0,
            "results": [],
            "error_message": str(exc),
        }


__all__ = ["router"]
