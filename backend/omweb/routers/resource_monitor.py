# backend/omweb/routers/resource_monitor.py
"""FastAPI router exposing telemetry and lifecycle endpoints for PELDRUN Resource Monitor.

Provides complete REST endpoints for real-time snapshots, historical timeseries queries,
process tree inspection, orphan remediation, tool consumption summaries, and data exports.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..resource_monitor.models import (
    CurrentResourceSnapshot,
    DiagnosticEventModel,
    DiagnosticEventType,
    DiagnosticSeverity,
    EnvironmentSnapshotModel,
    ExecutionRunModel,
    MonitorSettingsUpdate,
    ProcessInstanceModel,
    RunStatus,
    TerminateOrphanRequest,
    TerminateOrphanResponse,
    TimeseriesResponse,
    ToolConsumptionSummary,
)
from ..services.resource_monitor_service import (
    ResourceMonitorService,
    get_resource_monitor_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/resource-monitor",
    tags=["Resource Monitor"],
)


@router.get(
    "/environment",
    response_model=Optional[EnvironmentSnapshotModel],
    summary="Get environment and hardware profile",
)
async def get_environment(
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Optional[EnvironmentSnapshotModel]:
    """Retrieve host specifications, container constraints, and detected platform capabilities."""
    snapshot = service.get_environment()
    if not snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No environment snapshot recorded for active session",
        )
    return snapshot


@router.get(
    "/current",
    response_model=CurrentResourceSnapshot,
    summary="Get instantaneous resource metrics",
)
async def get_current_snapshot(
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> CurrentResourceSnapshot:
    """Collect real-time host, process tree, and active children telemetry snapshot."""
    return service.get_current_snapshot()


@router.get(
    "/processes",
    response_model=List[ProcessInstanceModel],
    summary="List tracked subprocesses",
)
async def list_processes(
    include_terminated: bool = Query(
        False,
        description="When True, includes terminated subprocesses cataloged in current session",
    ),
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> List[ProcessInstanceModel]:
    """List tracked subprocesses with orphan detection indicators."""
    return service.list_processes(include_terminated=include_terminated)


@router.get(
    "/runs",
    response_model=List[ExecutionRunModel],
    summary="Query execution runs history",
)
async def list_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    run_status: Optional[RunStatus] = Query(None, alias="status"),
    agent_id: Optional[str] = Query(None),
    include_all_sessions: bool = Query(False, description="Include runs from previous sessions"),
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> List[ExecutionRunModel]:
    """Query paginated execution runs with optional status and agent filters."""
    return service.list_runs(
        limit=limit,
        offset=offset,
        status=run_status,
        agent_id=agent_id,
        include_all_sessions=include_all_sessions,
    )


@router.get(
    "/runs/{run_id}",
    response_model=Dict[str, Any],
    summary="Get detailed run telemetry and spans",
)
async def get_run_details(
    run_id: str,
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Dict[str, Any]:
    """Retrieve run metadata correlated with its granular execution spans and resource consumption."""
    run_details = service.get_run(run_id)
    if not run_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution run '{run_id}' not found",
        )
    return {
        "run": run_details["run"].model_dump(),
        "spans": [s.model_dump() for s in run_details["spans"]],
    }


@router.get(
    "/runs/{run_id}/timeline",
    response_model=Dict[str, Any],
    summary="Get complete timeline for a specific run",
)
async def get_run_timeline(
    run_id: str,
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Dict[str, Any]:
    """Retrieve run metadata, granular execution spans, and bounded timeseries for detailed analysis."""
    run_data = service.get_run_timeline(run_id)
    if not run_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution run '{run_id}' not found",
        )

    return {
        "run": run_data["run"].model_dump(),
        "spans": [s.model_dump() for s in run_data["spans"]],
        "timeseries": [p.model_dump() for p in run_data["timeseries"]],
    }


@router.get(
    "/runs/{run_id}/timeline",
    response_model=Dict[str, Any],
    summary="Get complete timeline for a specific run",
)
async def get_run_timeline(
    run_id: str,
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Dict[str, Any]:
    """Retrieve run metadata, granular execution spans, and bounded timeseries for detailed analysis."""
    run_data = service.get_run_timeline(run_id)
    if not run_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution run '{run_id}' not found",
        )

    return {
        "run": run_data["run"].model_dump(),
        "spans": [s.model_dump() for s in run_data["spans"]],
        "timeseries": [p.model_dump() for p in run_data["timeseries"]],
    }


@router.get(
    "/timeseries",
    response_model=TimeseriesResponse,
    summary="Query downsampled timeseries telemetry",
)
async def query_timeseries(
    from_time: Optional[str] = Query(None, description="ISO timestamp for window start"),
    to_time: Optional[str] = Query(None, description="ISO timestamp for window end"),
    from_epoch_ms: Optional[int] = Query(None, description="Epoch milliseconds for window start"),
    to_epoch_ms: Optional[int] = Query(None, description="Epoch milliseconds for window end"),
    resolution_sec: int = Query(10, ge=1, le=3600, description="Bucket resolution in seconds"),
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> TimeseriesResponse:
    """Fetch aggregated timeseries points for charting CPU, RAM, and active run distributions."""
    now_dt = datetime.now(timezone.utc)
    target_to_ms = to_epoch_ms

    if target_to_ms is None:
        if to_time:
            try:
                target_to_ms = int(datetime.fromisoformat(to_time).timestamp() * 1000)
            except ValueError:
                target_to_ms = int(now_dt.timestamp() * 1000)
        else:
            target_to_ms = int(now_dt.timestamp() * 1000)

    target_from_ms = from_epoch_ms
    if target_from_ms is None:
        if from_time:
            try:
                target_from_ms = int(datetime.fromisoformat(from_time).timestamp() * 1000)
            except ValueError:
                target_from_ms = int((now_dt - timedelta(hours=1)).timestamp() * 1000)
        else:
            target_from_ms = int((now_dt - timedelta(hours=1)).timestamp() * 1000)

    if target_from_ms > target_to_ms:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_time must precede to_time",
        )

    return service.query_timeseries(
        from_epoch_ms=target_from_ms,
        to_epoch_ms=target_to_ms,
        resolution_seconds=resolution_sec,
    )


@router.get(
    "/tools/summary",
    response_model=List[ToolConsumptionSummary],
    summary="Get aggregated tool resource consumption",
)
async def get_tool_consumption_summary(
    from_time: Optional[str] = Query(None),
    to_time: Optional[str] = Query(None),
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> List[ToolConsumptionSummary]:
    """Retrieve cumulative resource consumption metrics grouped by tool name."""
    now_dt = datetime.now(timezone.utc)
    resolved_to = to_time or now_dt.isoformat()
    resolved_from = from_time or (now_dt - timedelta(days=7)).isoformat()

    return service.get_tool_consumption_summary(
        from_time=resolved_from,
        to_time=resolved_to,
    )


@router.get(
    "/diagnostics",
    response_model=List[DiagnosticEventModel],
    summary="List diagnostic anomaly events",
)
async def list_diagnostic_events(
    severity: Optional[DiagnosticSeverity] = Query(None),
    event_type: Optional[DiagnosticEventType] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> List[DiagnosticEventModel]:
    """Query diagnostic alert events regarding leaks, saturation, and orphaned processes."""
    return service.list_diagnostic_events(
        severity=severity,
        event_type=event_type,
        limit=limit,
    )


@router.post(
    "/actions/terminate-orphan",
    response_model=TerminateOrphanResponse,
    summary="Terminate an orphaned subprocess",
)
async def terminate_orphan_process(
    req: TerminateOrphanRequest,
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> TerminateOrphanResponse:
    """Terminate an identified orphaned subprocess safely."""
    result = service.terminate_orphan(pid=req.pid, force=req.force)
    if not result.terminated and "Cannot terminate" in result.message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message,
        )
    return result


@router.get(
    "/settings",
    response_model=Dict[str, Any],
    summary="Get resource monitor runtime settings",
)
async def get_settings(
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Dict[str, Any]:
    """Retrieve active sampling rates, retention durations, and threshold configurations."""
    return service.get_settings()


@router.patch(
    "/settings",
    response_model=Dict[str, Any],
    summary="Update resource monitor runtime settings",
)
async def update_settings(
    updates: MonitorSettingsUpdate,
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Dict[str, Any]:
    """Update runtime configuration settings and persist overrides to database."""
    return service.update_settings(updates)


@router.get(
    "/export",
    summary="Export telemetry records",
)
async def export_data(
    format_type: str = Query("json", alias="format", pattern="^(json|csv)$"),
    from_time: Optional[str] = Query(None),
    to_time: Optional[str] = Query(None),
    service: ResourceMonitorService = Depends(get_resource_monitor_service),
) -> Response:
    """Export recorded runs and metrics in JSON or CSV format."""
    exported = service.export_data(
        format_type=format_type,
        from_time=from_time,
        to_time=to_time,
    )

    if format_type.lower() == "csv":
        return Response(
            content=exported,
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="peldrun_resource_monitor_export.csv"',
            },
        )

    # Serialize dictionary to valid JSON format using json.dumps
    json_content = (
        exported
        if isinstance(exported, str)
        else json.dumps(exported, indent=2, default=str)
    )
    return Response(
        content=json_content,
        media_type="application/json",
    )
