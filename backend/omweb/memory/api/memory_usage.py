# backend/omweb/memory/api/memory_usage.py
"""
PELDRUN Memory Platform - Memory Usage Monitoring HTTP Router.

Read-only HTTP surface over ``SqliteMemoryUsageStore``. Powers the
monitoring dashboard's summary cards, per-dimension breakdowns,
and time-series charts.

Responsibilities (deliberately NARROW)
--------------------------------------

Three things:

    1. **Validate query params.** Same error envelope as
       ``memory_import`` and ``memory_export`` -- structured
       ``{"detail": {"error": ..., "message": ...}}`` on 400,
       never FastAPI's default 422.

    2. **Query the usage store.** Delegates to
       ``query_summary`` / ``query_breakdown`` / ``query_timeseries``.
       No aggregation logic here; the store owns it.

    3. **Shape the response.** Adds a small ``{status, period,
       ...}`` wrapper so the frontend sees a uniform envelope
       across all three endpoints.

Mode policy -- no mode check
----------------------------

Unlike the import and export routers, monitoring does NOT
consult ``MemoryMode``. Rationale:

    * Monitoring shows HISTORICAL data. An operator who
      disables memory today still wants to see "how much did
      we use last week?" -- refusing the query would hide
      past usage and make root-cause analysis impossible.

    * The store is read-only. A monitoring query cannot
      change any state, regardless of mode.

    * An empty store (memory never enabled) returns zeros --
      the caller gets a well-formed response, not an error.

This is a deliberate deviation from the mode-checking pattern
in import/export. The two routers WRITE (or read-for-write);
this one only reads.

Endpoints
---------

    GET /api/memory/usage
        Summary KPIs across a period. Query params:
            period    -- day | week | month | all (default all)
            chat_id   -- optional filter
            project_id-- optional filter

    GET /api/memory/usage/breakdown
        Per-dimension token breakdown. Query params:
            by        -- scope | source | project | chat
                         (default scope)
                         (source is an alias for scope)
            period    -- same as summary
            chat_id   -- optional filter
            project_id-- optional filter

    GET /api/memory/usage/timeseries
        Time-bucketed series. Query params:
            period      -- day | week | month | all (default month)
            granularity -- day | week | month (default day)

Period semantics
----------------

    day   -- last 24 hours
    week  -- last 7 days
    month -- last 30 days
    all   -- no cutoff

The cutoffs are SLIDING WINDOWS, not calendar boundaries.
"month" means "the last 30 days from now", not "calendar
month". This matches what operators expect from monitoring
dashboards -- "how much did I use this month?" usually means
"the trailing 30 days", not "since the 1st".

Response shapes
---------------

All three endpoints return ``{"status": "ok", ...}`` on
success. The ``status`` field is the SAME envelope as import
and export, so the frontend's error-handling code is
identical across the three subsystems.

Summary::

    {
      "status": "ok",
      "period": "month",
      "run_count": 47,
      "total_tokens": 15230,
      "retrieval_tokens": 0,
      "context_tokens": 14200,
      "overhead_tokens": 1030,
      "user_tokens": 4200,
      "project_tokens": 9400,
      "chat_tokens": 600,
      "agent_tokens": 0,
      "budget_tokens": 47104,
      "budget_utilization": 0.3233,
      "entries_retrieved": 240,
      "entries_used": 187,
      "entries_dropped": 53,
      "drop_rate": 0.2208
    }

Breakdown::

    {
      "status": "ok",
      "by": "scope",
      "period": "month",
      "buckets": [
        {"dimension": "scope", "key": "user", "tokens": 4200, "runs": 47, "pct": 28.4},
        {"dimension": "scope", "key": "project", "tokens": 9400, "runs": 47, "pct": 63.6},
        {"dimension": "scope", "key": "chat", "tokens": 600, "runs": 47, "pct": 4.1},
        {"dimension": "scope", "key": "agent", "tokens": 0, "runs": 0, "pct": 0.0}
      ]
    }

Timeseries::

    {
      "status": "ok",
      "period": "month",
      "granularity": "day",
      "points": [
        {"bucket": "2026-11-01", "runs": 3, "total_tokens": 1200, ...},
        {"bucket": "2026-11-02", "runs": 5, "total_tokens": 2100, ...}
      ]
    }

Error mapping
-------------

Same taxonomy as the other routers:

    * ``MemoryValidationError`` -- not applicable here (the
      store does not raise it).
    * ``TypeError`` / unexpected -> 500.
    * Query-param validation -> 400 with the structured
      envelope (never FastAPI's 422).

Store access
------------

Uses ``get_memory_usage_store()`` -- the process-wide
singleton. Tests that need isolation reset the singleton,
re-create it against a temp path, and reset again on
teardown (see ``tests/memory/test_usage_api.py`` for the
exact pattern).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, status as http_status
from fastapi.responses import JSONResponse

from peldrun.memory.domain import (
    MemoryUnavailableError,
    MemoryValidationError,
)

from omweb.memory.monitoring.memory_usage import (
    get_memory_usage_store,
)


logger = logging.getLogger("omweb.memory.api.usage")


router = APIRouter()


# ---------------------------------------------------------------------------
# Accepted vocabulary
# ---------------------------------------------------------------------------


_ACCEPTED_PERIODS: frozenset = frozenset(
    {"day", "week", "month", "all"}
)

_ACCEPTED_BREAKDOWN_DIMENSIONS: frozenset = frozenset(
    {"scope", "source", "project", "chat"}
)

_ACCEPTED_GRANULARITIES: frozenset = frozenset(
    {"day", "week", "month"}
)


#: Map an accepted alias to the store's canonical dimension name.
#: ``source`` is accepted as a synonym for ``scope`` so callers
#: that think in terms of "where did the memory originate" are
#: not surprised by a "source is not a valid dimension" error.
_DIMENSION_ALIASES: Dict[str, str] = {
    "source": "scope",
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_period(value: str) -> str:
    """Validate the ``period`` query parameter.

    Returns the canonical lowercase value. Any value outside
    ``_ACCEPTED_PERIODS`` raises a 400 with the structured
    envelope (never FastAPI's default 422).
    """
    if not isinstance(value, str):
        _raise_400(
            error="invalid_period",
            message=(
                f"'period' must be one of "
                f"{sorted(_ACCEPTED_PERIODS)}"
            ),
        )
    normalized = value.strip().lower()
    if normalized not in _ACCEPTED_PERIODS:
        _raise_400(
            error="invalid_period",
            message=(
                f"unknown period {value!r}; expected one of "
                f"{sorted(_ACCEPTED_PERIODS)}"
            ),
        )
    return normalized


def _validate_dimension(value: str) -> str:
    """Validate ``by`` and normalize aliases.

    ``source`` is accepted as an alias for ``scope`` -- the
    caller may think in terms of "where did the memory come
    from" (scope) rather than "what is the dimension name".
    """
    if not isinstance(value, str):
        _raise_400(
            error="invalid_by",
            message=(
                f"'by' must be one of "
                f"{sorted(_ACCEPTED_BREAKDOWN_DIMENSIONS)}"
            ),
        )
    normalized = value.strip().lower()
    if normalized not in _ACCEPTED_BREAKDOWN_DIMENSIONS:
        _raise_400(
            error="invalid_by",
            message=(
                f"unknown 'by' dimension {value!r}; expected one "
                f"of {sorted(_ACCEPTED_BREAKDOWN_DIMENSIONS)}"
            ),
        )
    return _DIMENSION_ALIASES.get(normalized, normalized)


def _validate_granularity(value: str) -> str:
    """Validate the ``granularity`` query parameter."""
    if not isinstance(value, str):
        _raise_400(
            error="invalid_granularity",
            message=(
                f"'granularity' must be one of "
                f"{sorted(_ACCEPTED_GRANULARITIES)}"
            ),
        )
    normalized = value.strip().lower()
    if normalized not in _ACCEPTED_GRANULARITIES:
        _raise_400(
            error="invalid_granularity",
            message=(
                f"unknown granularity {value!r}; expected one of "
                f"{sorted(_ACCEPTED_GRANULARITIES)}"
            ),
        )
    return normalized


def _raise_400(*, error: str, message: str) -> None:
    """Raise a structured 400 that matches the memory API envelope."""
    from fastapi import HTTPException

    raise HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


# ---------------------------------------------------------------------------
# Error mapping (mirrors import/export routers)
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map a memory-subsystem exception to an HTTP response.

    The store never raises ``MemoryValidationError`` (it catches
    internally and returns empty results). The remaining cases
    are ``MemoryUnavailableError`` (backend failure) and
    unexpected exceptions (500).
    """
    if isinstance(exc, MemoryUnavailableError):
        logger.error(
            "Memory usage %s hit backend failure: %s",
            operation,
            exc,
        )
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "operation": operation,
                "error": "MemoryUnavailableError",
                "message": (
                    "Memory usage backend unavailable. Try again "
                    "shortly."
                ),
            },
        )

    if isinstance(exc, TypeError):
        logger.error(
            "Memory usage %s hit a programmer error: %s",
            operation,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "operation": operation,
                "error": "InternalError",
                "message": "Internal error during usage query.",
            },
        )

    logger.exception(
        "Memory usage %s failed unexpectedly: %s",
        operation,
        exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during usage query.",
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="Memory usage summary",
    response_model=None,
)
async def summary_endpoint(
    # ``Optional[str] = Query(None)`` -- see the import router's
    # comment for why we do not use ``str = Query(...)``: FastAPI
    # would short-circuit our handler with a 422 before our own
    # validation runs, which breaks the uniform error envelope.
    # Here the params have DEFAULT values (``period=all``), so
    # they are always present -- but the pattern is still
    # consistent with the rest of the memory API.
    period: str = Query(
        "all",
        description=(
            "Time window: day (24h) | week (7d) | month (30d) | "
            "all (no cutoff)"
        ),
    ),
    chat_id: Optional[str] = Query(
        None,
        description="Optional chat filter",
    ),
    project_id: Optional[str] = Query(
        None,
        description="Optional project filter",
    ),
) -> JSONResponse:
    """Aggregated memory usage KPIs across a time window.

    Powers the monitoring dashboard's summary cards:

      * "This month: 47 runs, 15.2K tokens, 32% of budget"
      * "Average drop rate: 22%"
      * "47 retrieved, 187 used, 53 dropped"

    See the module docstring for the response shape.
    """
    try:
        clean_period = _validate_period(period)
    except Exception:
        # _validate_period raises HTTPException; let FastAPI
        # handle it (it already has the right status/detail).
        raise

    try:
        store = get_memory_usage_store()
        summary = await store.query_summary(
            period=clean_period,
            chat_id=chat_id,
            project_id=project_id,
        )
    except MemoryValidationError as exc:
        return _error_response(exc, operation="summary")
    except Exception as exc:
        return _error_response(exc, operation="summary")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "summary",
            **summary,
        },
    )


@router.get(
    "/breakdown",
    summary="Memory usage breakdown by dimension",
    response_model=None,
)
async def breakdown_endpoint(
    by: str = Query(
        "scope",
        description=(
            "Dimension: scope | source | project | chat. "
            "``source`` is an alias for ``scope``."
        ),
    ),
    period: str = Query(
        "all",
        description=(
            "Time window: day | week | month | all"
        ),
    ),
    chat_id: Optional[str] = Query(
        None,
        description="Optional chat filter",
    ),
    project_id: Optional[str] = Query(
        None,
        description="Optional project filter",
    ),
) -> JSONResponse:
    """Per-dimension usage breakdown.

    Powers the monitoring dashboard's breakdown pie/bar chart.

    Dimensions:

      * ``scope`` (alias ``source``) -- the four memory
        scope buckets (user / project / chat / agent). The
        tokens for each bucket sum exactly to
        ``context_tokens`` in the summary response -- see
        ``MemoryUsage._check_scope_sum``'s validator.

      * ``project`` -- per-project usage, useful when a
        user has multiple active projects.

      * ``chat`` -- per-chat usage, useful for identifying
        a chat that is consuming a disproportionate share.

    See the module docstring for the response shape.
    """
    try:
        clean_by = _validate_dimension(by)
        clean_period = _validate_period(period)
    except Exception:
        raise

    try:
        store = get_memory_usage_store()
        buckets: List[Dict[str, Any]] = await store.query_breakdown(
            by=clean_by,
            period=clean_period,
            chat_id=chat_id,
            project_id=project_id,
        )
    except MemoryValidationError as exc:
        return _error_response(exc, operation="breakdown")
    except Exception as exc:
        return _error_response(exc, operation="breakdown")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "breakdown",
            "by": clean_by,
            "period": clean_period,
            "buckets": buckets,
        },
    )


@router.get(
    "/timeseries",
    summary="Memory usage time series",
    response_model=None,
)
async def timeseries_endpoint(
    period: str = Query(
        "month",
        description=(
            "Time window: day | week | month | all"
        ),
    ),
    granularity: str = Query(
        "day",
        description=(
            "Bucket size: day | week | month"
        ),
    ),
) -> JSONResponse:
    """Bucketed time series of token consumption.

    Powers the monitoring dashboard's line/area chart. Each
    point carries the runs, total tokens, and per-bucket
    breakdown for one time window.

    Default period is ``month`` (30 days) -- more useful for
    a time series than ``all`` (which would produce one
    data point per day going back to the first recorded run).

    The pair (period, granularity) is not cross-validated.
    A caller who asks for ``period=day&granularity=month``
    gets one bucket -- that is a legitimate (if unusual)
    query and the store does not need to reject it. The
    frontend's chart just renders a single point.
    """
    try:
        clean_period = _validate_period(period)
        clean_granularity = _validate_granularity(granularity)
    except Exception:
        raise

    try:
        store = get_memory_usage_store()
        points: List[Dict[str, Any]] = (
            await store.query_timeseries(
                period=clean_period,
                granularity=clean_granularity,
            )
        )
    except MemoryValidationError as exc:
        return _error_response(exc, operation="timeseries")
    except Exception as exc:
        return _error_response(exc, operation="timeseries")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "timeseries",
            "period": clean_period,
            "granularity": clean_granularity,
            "points": points,
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
