"""
backend/omweb/routers/telemetry.py

FastAPI HTTP Router for PELDRUN Telemetry, Accounting & Analytics.

Provides canonical REST endpoints for:
- Summary dashboard metrics (/api/telemetry/summary)
- Time-series token analytics (/api/telemetry/timeseries)
- Multi-dimensional breakdown (/api/telemetry/breakdown)
- Cursor-paginated invocation logs (/api/telemetry/invocations)
- Chat-scoped accounting detail (/api/telemetry/chats/{chat_id})
- Model pricing management & non-destructive repricing preview
- Structured Report Exports in JSON & CSV (/api/telemetry/reports/export)
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from omweb.services.telemetry_service import telemetry_service
from peldrun.runtime.pricing_store import NANO_USD_PER_USD, PricingRule

router = APIRouter()


class PricingRuleCreateDTO(BaseModel):
    """Payload DTO for registering or updating a pricing rule."""

    pricing_id: Optional[str] = None
    provider: Optional[str] = None
    model_pattern: str = Field(..., description="Exact model ID or glob pattern, e.g. 'gpt-4o', 'deepseek*'")
    input_price_usd_per_million: float = Field(default=0.0, ge=0.0)
    output_price_usd_per_million: float = Field(default=0.0, ge=0.0)
    cached_input_price_usd_per_million: Optional[float] = Field(default=None, ge=0.0)
    priority: int = Field(default=0)


class RepriceRequestDTO(BaseModel):
    """Payload DTO for triggering non-destructive repricing preview."""

    chat_id: Optional[str] = None
    from_time: Optional[float] = None
    to_time: Optional[float] = None


@router.get("/summary")
async def get_telemetry_summary(
    from_time: Optional[float] = Query(None, description="Start UTC epoch timestamp"),
    to_time: Optional[float] = Query(None, description="End UTC epoch timestamp"),
    project_id: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    mode: Optional[str] = Query(None, description="'agent' or 'chat'"),
):
    """Retrieve top-level KPI summary metrics across all or filtered LLM invocations."""
    try:
        return await telemetry_service.get_dashboard_summary(
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            model=model,
            mode=mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to compile telemetry summary: {exc}")


@router.get("/timeseries")
async def get_telemetry_timeseries(
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    from_time: Optional[float] = Query(None),
    to_time: Optional[float] = Query(None),
    project_id: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
):
    """Retrieve time-series consumption metrics aggregated by day, week, or month."""
    try:
        return await telemetry_service.get_timeseries_metrics(
            granularity=granularity,
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            model=model,
            mode=mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate timeseries metrics: {exc}")


@router.get("/breakdown")
async def get_telemetry_breakdown(
    group_by: str = Query("model", pattern="^(model|provider|mode|project)$"),
    from_time: Optional[float] = Query(None),
    to_time: Optional[float] = Query(None),
    project_id: Optional[str] = Query(None),
    chat_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
):
    """Retrieve dimensional consumption breakdown by model, provider, mode, or project."""
    try:
        return await telemetry_service.get_dimension_breakdown(
            group_by=group_by,
            from_time=from_time,
            to_time=to_time,
            project_id=project_id,
            chat_id=chat_id,
            provider=provider,
            mode=mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate dimensional breakdown: {exc}")


@router.get("/invocations")
async def get_telemetry_invocations(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[float] = Query(None, description="Created_at timestamp for cursor pagination"),
    from_time: Optional[float] = Query(None),
    to_time: Optional[float] = Query(None),
    chat_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
):
    """Retrieve cursor-paginated list of individual LLM invocation ledger records."""
    try:
        return await telemetry_service.list_invocations_paginated(
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list invocation facts: {exc}")


@router.get("/chats/{chat_id}")
async def get_chat_telemetry_detail(chat_id: str):
    """Retrieve complete usage summary and individual invocation facts for a specific chat."""
    try:
        return await telemetry_service.get_chat_telemetry(chat_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch chat telemetry: {exc}")


@router.get("/pricing")
async def list_pricing_rules(active_only: bool = Query(True)):
    """Retrieve configured pricing catalog rules."""
    try:
        rules = await telemetry_service.list_pricing_rules(active_only=active_only)
        return [
            {
                "pricing_id": r.pricing_id,
                "provider": r.provider,
                "model_pattern": r.model_pattern,
                "input_price_usd_per_million": r.input_price_usd_per_million,
                "output_price_usd_per_million": r.output_price_usd_per_million,
                "cached_input_price_usd_per_million": r.cached_input_price_usd_per_million,
                "input_price_nano_usd": r.input_price_nano_usd_per_million,
                "output_price_nano_usd": r.output_price_nano_usd_per_million,
                "priority": r.priority,
                "active": r.active,
                "effective_from": r.effective_from,
                "effective_to": r.effective_to,
            }
            for r in rules
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list pricing catalog: {exc}")


@router.post("/pricing")
async def create_or_update_pricing_rule(dto: PricingRuleCreateDTO = Body(...)):
    """Create or update a model pricing rule."""
    try:
        input_nano = int(dto.input_price_usd_per_million * NANO_USD_PER_USD)
        output_nano = int(dto.output_price_usd_per_million * NANO_USD_PER_USD)
        cached_nano = (
            int(dto.cached_input_price_usd_per_million * NANO_USD_PER_USD)
            if dto.cached_input_price_usd_per_million is not None
            else None
        )

        rule = PricingRule(
            pricing_id=dto.pricing_id or f"prc_{dto.model_pattern.replace('*', 'wild')}_{int(time.time())}",
            provider=dto.provider.strip().lower() if dto.provider else None,
            model_pattern=dto.model_pattern.strip(),
            input_price_nano_usd_per_million=input_nano,
            output_price_nano_usd_per_million=output_nano,
            cached_input_price_nano_usd_per_million=cached_nano,
            priority=dto.priority,
            active=True,
        )
        await telemetry_service.save_pricing_rule(rule)
        return {"ok": True, "pricing_id": rule.pricing_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save pricing rule: {exc}")


@router.post("/pricing/{pricing_id}/retire")
async def retire_pricing_rule(pricing_id: str):
    """Soft-retire a model pricing rule without destructive deletion."""
    try:
        success = await telemetry_service.retire_pricing_rule(pricing_id)
        if not success:
            raise HTTPException(status_code=404, detail="Pricing rule not found or already retired.")
        return {"ok": True, "retired_id": pricing_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to retire pricing rule: {exc}")


@router.post("/reprice")
async def preview_repricing_simulation(dto: RepriceRequestDTO = Body(...)):
    """Preview repricing of historical ledger facts with active pricing rules (non-destructive)."""
    try:
        return await telemetry_service.simulate_repricing(
            chat_id=dto.chat_id,
            from_time=dto.from_time,
            to_time=dto.to_time,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to simulate repricing: {exc}")


@router.get("/reports/export")
async def export_telemetry_report(
    format: str = Query("json", pattern="^(json|csv)$"),
    from_time: Optional[float] = Query(None),
    to_time: Optional[float] = Query(None),
    chat_id: Optional[str] = Query(None),
):
    """Export complete usage telemetry report as canonical JSON or downloadable CSV."""
    try:
        if format.lower() == "csv":
            csv_content = await telemetry_service.export_report_csv(
                from_time=from_time,
                to_time=to_time,
                chat_id=chat_id,
            )
            filename = f"peldrun_usage_report_{int(time.time())}.csv"
            return PlainTextResponse(
                content=csv_content,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        report_json = await telemetry_service.export_report_json(
            from_time=from_time,
            to_time=to_time,
            chat_id=chat_id,
        )
        return report_json
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to export report: {exc}")