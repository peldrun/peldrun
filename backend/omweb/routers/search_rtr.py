"""
backend/omweb/routers/search_rtr.py

PELDRUN Search Orchestration, Diagnostics & Telemetry Router.
Provides endpoints for:
  - GET    /api/search/providers        -> provider capabilities & effective pricing
  - GET    /api/search/metrics          -> aggregated usage telemetry & breakdown
  - DELETE /api/search/metrics          -> clear local search usage ledger
  - GET    /api/search/config           -> current search config (masked + effective defaults)
  - POST   /api/search/config           -> validate & persist search config to config.toml
  - POST   /api/search/test-connection  -> structured provider reachability/credential check
  - POST   /api/search/test-query       -> bounded live diagnostic search query

All SQLite and file I/O run inside worker threads so the asyncio event loop
is never blocked.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional
import urllib.parse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from omweb.routers.config_rtr import (
    get_authoritative_config_path,
    get_last_config_error,
    mask_secrets,
    merge_unmasked,
    read_raw_config,
    read_raw_config_strict,
    write_raw_config,
)
from peldrun.runtime.search_usage_store import get_search_usage_store
from peldrun.tools.builtins.search_providers.base import (
    ConfigurationError,
    CostBasis,
    DiagnosticCheckType,
    MAX_QUERY_CHARS,
    ProviderStatus,
    SearchProviderError,
    UnknownProviderError,
    sanitize_error_text,
)
from peldrun.tools.builtins.search_providers.factory import SearchProviderFactory

logger = logging.getLogger("omweb.routers.search_rtr")

router = APIRouter()

# Per-provider lock to prevent accidental concurrent diagnostic probes
# from double-clicks or multiple open settings tabs.
_DIAGNOSTIC_LOCKS: Dict[str, asyncio.Lock] = {}


def _get_diagnostic_lock(provider_key: str) -> asyncio.Lock:
    key = provider_key.strip().lower()
    lock = _DIAGNOSTIC_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DIAGNOSTIC_LOCKS[key] = lock
    return lock


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------
class TestConnectionRequest(BaseModel):
    provider: str = Field(..., description="Canonical or case-insensitive provider name.")
    config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional unsaved search config snapshot from the Settings UI.",
    )
    check_type: Optional[str] = Field(
        default="credentials",
        description="Diagnostic level: 'reachability', 'credentials', or 'live_search'.",
    )


class TestQueryRequest(BaseModel):
    provider: str = Field(..., description="Provider name to execute the test query against.")
    query: str = Field(..., description="Test search keyword query.")
    max_results: Optional[int] = Field(
        default=3,
        ge=1,
        le=15,
        description="Maximum number of results to parse (1..15).",
    )
    config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional unsaved search config snapshot from the Settings UI.",
    )


class SearchConfigPayload(BaseModel):
    """Payload for validating and updating the [search] table in config.toml."""
    search_config: Dict[str, Any] = Field(
        ...,
        description="Search sub-table settings matching [search] in config.toml.",
    )


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------
async def _get_store_async():
    """Obtain the singleton usage store from a worker thread."""
    return await asyncio.to_thread(get_search_usage_store)


def _record_usage_compat(
    store: Any,
    *,
    query: str,
    provider: str,
    results_count: int,
    latency_ms: float,
    cost_usd: Optional[float],
    is_success: bool,
    error_message: Optional[str],
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    status: ProviderStatus = ProviderStatus.SUCCESS,
    request_count: int = 1,
    usage_units: Optional[float] = None,
    cost_basis: CostBasis = CostBasis.UNKNOWN,
    privacy_mode: bool = False,
) -> None:
    """
    Invoke store.record_search with full forward/backward signature compatibility.
    """
    sig = inspect.signature(store.record_search)
    kwargs: Dict[str, Any] = {
        "query": "[REDACTED_PRIVACY_MODE]" if privacy_mode else query,
        "provider": provider,
        "results_count": results_count,
        "latency_ms": latency_ms,
        "cost_usd": float(cost_usd) if cost_usd is not None else 0.0,
        "is_success": is_success,
        "error_message": sanitize_error_text(error_message) if error_message else None,
        "session_id": session_id,
        "run_id": run_id,
    }
    if "status" in sig.parameters:
        kwargs["status"] = status.value
    if "request_count" in sig.parameters:
        kwargs["request_count"] = request_count
    if "usage_units" in sig.parameters:
        kwargs["usage_units"] = usage_units
    if "cost_basis" in sig.parameters:
        kwargs["cost_basis"] = cost_basis.value
    if "privacy_mode" in sig.parameters:
        kwargs["query"] = query
        kwargs["privacy_mode"] = privacy_mode

    store.record_search(**kwargs)


def _clear_store_ledger_sync(store: Any) -> int:
    """
    Clear all records from the local search usage ledger safely.
    Returns the number of deleted rows.
    """
    for method_name in ("clear_ledger", "clear_usage", "reset_ledger"):
        method = getattr(store, method_name, None)
        if callable(method):
            res = method()
            return int(res) if isinstance(res, int) else 0

    db_lock = getattr(store, "_db_lock", None)
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        return 0

    def _delete_rows() -> int:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cursor = conn.execute("DELETE FROM search_usage_ledger;")
            deleted = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
            conn.commit()
            return deleted
        finally:
            conn.close()

    if db_lock is not None:
        with db_lock:
            return _delete_rows()
    return _delete_rows()


def _enrich_search_config_with_defaults(search_section: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure every supported provider sub-table in the returned config carries its
    effective cost_per_query and default mode so the UI never displays $0.00
    for providers whose runtime default is non-zero.
    """
    enriched: Dict[str, Any] = dict(search_section or {})
    if "enabled" not in enriched:
        enriched["enabled"] = True
    if "engine" not in enriched or not str(enriched.get("engine", "")).strip():
        enriched["engine"] = "DuckDuckGo"
    if "fallback_engines" not in enriched or not isinstance(enriched.get("fallback_engines"), list):
        enriched["fallback_engines"] = []
    if "retry_delay" not in enriched:
        enriched["retry_delay"] = 2
    if "max_retries" not in enriched:
        enriched["max_retries"] = 0
    if "lang" not in enriched:
        enriched["lang"] = "en"
    if "country" not in enriched:
        enriched["country"] = "us"
    if "total_budget_seconds" not in enriched:
        enriched["total_budget_seconds"] = SearchProviderFactory.DEFAULT_TOTAL_BUDGET_SECONDS
    if "privacy_mode" not in enriched:
        enriched["privacy_mode"] = False

    for prov_id in SearchProviderFactory.list_supported_providers():
        cfg_key = prov_id.lower()
        sub = dict(enriched.get(cfg_key) or {}) if isinstance(enriched.get(cfg_key), dict) else {}
        effective_cost = SearchProviderFactory.get_effective_cost(prov_id, enriched)
        if "cost_per_query" not in sub or sub.get("cost_per_query") is None or sub.get("cost_per_query") == "":
            sub["cost_per_query"] = effective_cost
        if cfg_key == "yandex" and not sub.get("mode"):
            sub["mode"] = "cloud_v2"
        if cfg_key == "tavily" and not sub.get("search_depth"):
            sub["search_depth"] = "basic"
        if cfg_key == "searxng" and not sub.get("base_url"):
            sub["base_url"] = "http://localhost:8080"
        enriched[cfg_key] = sub

    return enriched


def _validate_url_field(url_value: str, field_label: str, allow_http: bool = True) -> str:
    """Validate an endpoint or base_url setting."""
    cleaned = url_value.strip()
    if not cleaned:
        return ""
    try:
        parsed = urllib.parse.urlsplit(cleaned)
    except Exception as exc:
        raise ValueError(f"Invalid URL for {field_label}: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    allowed_schemes = ("http", "https") if allow_http else ("https",)
    if scheme not in allowed_schemes:
        schemes_str = "http:// or https://" if allow_http else "https://"
        raise ValueError(f"{field_label} must start with {schemes_str}.")
    if not parsed.hostname:
        raise ValueError(f"{field_label} must include a valid hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_label} must not contain embedded credentials.")
    return cleaned


def _validate_and_normalize_search_config(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate user-submitted search settings before writing to config.toml.
    Raises ValueError with an actionable message on invalid inputs.
    """
    if not isinstance(raw_cfg, dict):
        raise ValueError("search_config must be a JSON object.")

    cleaned: Dict[str, Any] = dict(raw_cfg)

    # 1. Validate primary engine
    primary_raw = str(cleaned.get("engine", "DuckDuckGo")).strip()
    if not primary_raw:
        raise ValueError("Primary search engine ('engine') cannot be empty.")
    try:
        cleaned["engine"] = SearchProviderFactory.resolve_provider_key(primary_raw)
    except UnknownProviderError as exc:
        raise ValueError(exc.message) from exc

    # 2. Validate fallback_engines (preserve empty list [] for single-provider testing)
    fallbacks_raw = cleaned.get("fallback_engines")
    if fallbacks_raw is None:
        cleaned["fallback_engines"] = []
    elif not isinstance(fallbacks_raw, list):
        raise ValueError("'fallback_engines' must be a list of provider names.")
    else:
        normalized_fallbacks: List[str] = []
        for item in fallbacks_raw:
            if not item or not str(item).strip():
                continue
            try:
                canon = SearchProviderFactory.resolve_provider_key(str(item))
            except UnknownProviderError as exc:
                raise ValueError(exc.message) from exc
            if canon not in normalized_fallbacks:
                normalized_fallbacks.append(canon)
        cleaned["fallback_engines"] = normalized_fallbacks

    # 3. Validate global numeric bounds
    if "retry_delay" in cleaned and cleaned["retry_delay"] is not None:
        try:
            rd = int(float(cleaned["retry_delay"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("'retry_delay' must be a valid number.") from exc
        if rd < 0 or rd > 30:
            raise ValueError("'retry_delay' must be between 0 and 30 seconds.")
        cleaned["retry_delay"] = rd

    if "max_retries" in cleaned and cleaned["max_retries"] is not None:
        try:
            mr = int(float(cleaned["max_retries"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("'max_retries' must be an integer.") from exc
        if mr < 0 or mr > 3:
            raise ValueError("'max_retries' must be between 0 and 3.")
        cleaned["max_retries"] = mr

    if "total_budget_seconds" in cleaned and cleaned["total_budget_seconds"] is not None:
        try:
            tb = float(cleaned["total_budget_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("'total_budget_seconds' must be a number.") from exc
        if tb < 2.0 or tb > SearchProviderFactory.MAX_TOTAL_BUDGET_SECONDS:
            raise ValueError(
                f"'total_budget_seconds' must be between 2.0 and "
                f"{SearchProviderFactory.MAX_TOTAL_BUDGET_SECONDS} seconds."
            )
        cleaned["total_budget_seconds"] = round(tb, 1)

    if "privacy_mode" in cleaned:
        cleaned["privacy_mode"] = bool(cleaned["privacy_mode"])

    # 4. Validate per-provider sub-tables
    for prov_id in SearchProviderFactory.list_supported_providers():
        cfg_key = prov_id.lower()
        sub = cleaned.get(cfg_key)
        if not isinstance(sub, dict):
            continue
        sub_clean = dict(sub)

        if "cost_per_query" in sub_clean and sub_clean["cost_per_query"] is not None and sub_clean["cost_per_query"] != "":
            try:
                cost_val = float(sub_clean["cost_per_query"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid cost_per_query for {prov_id}.") from exc
            if cost_val < 0.0 or cost_val > 10.0:
                raise ValueError(
                    f"cost_per_query for {prov_id} must be between 0.0 and 10.0 USD."
                )
            sub_clean["cost_per_query"] = round(cost_val, 6)

        if "timeout_seconds" in sub_clean and sub_clean["timeout_seconds"] is not None:
            try:
                to_val = float(sub_clean["timeout_seconds"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid timeout_seconds for {prov_id}.") from exc
            if to_val < 1.0 or to_val > 60.0:
                raise ValueError(
                    f"timeout_seconds for {prov_id} must be between 1.0 and 60.0 seconds."
                )
            sub_clean["timeout_seconds"] = round(to_val, 1)

        if cfg_key == "searxng" and "base_url" in sub_clean and sub_clean["base_url"]:
            sub_clean["base_url"] = _validate_url_field(
                str(sub_clean["base_url"]),
                "SearXNG base_url",
                allow_http=True,
            )

        if "endpoint" in sub_clean and sub_clean["endpoint"]:
            sub_clean["endpoint"] = _validate_url_field(
                str(sub_clean["endpoint"]),
                f"{prov_id} endpoint",
                allow_http=(cfg_key == "searxng"),
            )

        if cfg_key == "yandex" and "mode" in sub_clean and sub_clean["mode"]:
            mode_val = str(sub_clean["mode"]).strip().lower()
            if mode_val not in ("cloud_v2", "classic_xml"):
                raise ValueError(
                    "Yandex mode must be either 'cloud_v2' or 'classic_xml'."
                )
            sub_clean["mode"] = mode_val

        if cfg_key == "tavily" and "search_depth" in sub_clean and sub_clean["search_depth"]:
            depth_val = str(sub_clean["search_depth"]).strip().lower()
            if depth_val not in ("basic", "advanced", "fast", "ultra-fast"):
                raise ValueError(
                    "Tavily search_depth must be one of: basic, advanced, fast, ultra-fast."
                )
            sub_clean["search_depth"] = depth_val

        cleaned[cfg_key] = sub_clean

    return cleaned


def _build_effective_costs_map(search_section: Dict[str, Any]) -> Dict[str, float]:
    return {
        prov_id: SearchProviderFactory.get_effective_cost(prov_id, search_section)
        for prov_id in SearchProviderFactory.list_supported_providers()
    }


# ---------------------------------------------------------------------------
# Provider Capabilities Endpoint (Phase 5.1)
# ---------------------------------------------------------------------------
@router.get("/providers")
async def get_search_providers_metadata():
    """Return authoritative provider capabilities, limits, lifecycle, and effective pricing."""
    try:
        raw = await asyncio.to_thread(read_raw_config)
        search_section = raw.get("search", {}) if isinstance(raw, dict) else {}
        capabilities = SearchProviderFactory.get_provider_capabilities(search_section)
        return {
            "ok": True,
            "supported_providers": SearchProviderFactory.list_supported_providers(),
            "provider_capabilities": capabilities,
            "effective_costs": _build_effective_costs_map(search_section),
        }
    except Exception as exc:
        logger.error(f"Failed to load search provider capabilities: {exc}")
        raise HTTPException(status_code=500, detail=sanitize_error_text(str(exc)))


# ---------------------------------------------------------------------------
# Metrics Endpoints (Read & Clear)
# ---------------------------------------------------------------------------
@router.get("/metrics")
async def get_search_metrics(
    period: str = Query(
        "all",
        description="Aggregation timeframe: 'day', 'week', 'month', or 'all'",
    ),
):
    """Return aggregated telemetry: summary, breakdown, timeseries, and provider metadata."""
    try:
        store = await _get_store_async()
        summary = await asyncio.to_thread(store.get_summary_metrics, period)
        breakdown = await asyncio.to_thread(store.get_provider_breakdown, period)
        timeseries_period = "month" if period.lower() == "all" else period.lower()
        timeseries = await asyncio.to_thread(store.get_timeseries, timeseries_period)

        raw = await asyncio.to_thread(read_raw_config)
        search_section = raw.get("search", {}) if isinstance(raw, dict) else {}
        supported_providers = SearchProviderFactory.list_supported_providers()

        return {
            "ok": True,
            "period": period,
            "summary": summary,
            "breakdown": breakdown,
            "timeseries": timeseries,
            "supported_providers": supported_providers,
            "provider_capabilities": SearchProviderFactory.get_provider_capabilities(search_section),
            "effective_costs": _build_effective_costs_map(search_section),
            "privacy_mode": bool(search_section.get("privacy_mode", False)),
            "ledger_path": str(getattr(store, "db_path", "")),
        }
    except Exception as exc:
        logger.error(f"Failed to compile search metrics: {exc}")
        raise HTTPException(status_code=500, detail=sanitize_error_text(str(exc)))


@router.delete("/metrics")
@router.post("/metrics/clear")
async def clear_search_metrics():
    """Delete all persisted search usage records from the local SQLite ledger."""
    try:
        store = await _get_store_async()
        deleted_count = await asyncio.to_thread(_clear_store_ledger_sync, store)
        return {
            "ok": True,
            "deleted_records": deleted_count,
            "message": f"Cleared {deleted_count} search usage record(s) from the local ledger.",
        }
    except Exception as exc:
        logger.error(f"Failed to clear search usage ledger: {exc}")
        raise HTTPException(status_code=500, detail=sanitize_error_text(str(exc)))


# ---------------------------------------------------------------------------
# Configuration: Read & Write
# ---------------------------------------------------------------------------
@router.get("/config")
async def get_search_configuration():
    """
    Return the current [search] table with masked secrets, effective default
    costs populated, and provider capability metadata.
    """
    config_error: Optional[str] = None
    try:
        raw = await asyncio.to_thread(read_raw_config_strict)
    except ValueError as exc:
        config_error = str(exc)
        raw = {}
    except Exception as exc:
        logger.error(f"Failed to read search configuration: {exc}")
        raise HTTPException(status_code=500, detail=sanitize_error_text(str(exc)))

    search_section = raw.get("search", {}) if isinstance(raw, dict) else {}
    if not isinstance(search_section, dict):
        search_section = {}

    enriched = _enrich_search_config_with_defaults(search_section)
    capabilities = SearchProviderFactory.get_provider_capabilities(enriched)

    return {
        "ok": config_error is None,
        "search_config": mask_secrets(enriched),
        "supported_providers": SearchProviderFactory.list_supported_providers(),
        "provider_capabilities": capabilities,
        "effective_costs": _build_effective_costs_map(enriched),
        "raw_path": str(get_authoritative_config_path()),
        "config_error": config_error or get_last_config_error(),
    }


@router.post("/config")
async def update_search_configuration(payload: SearchConfigPayload):
    """
    Validate and persist the [search] table to the authoritative config.toml atomically.
    Existing API keys are preserved when masked placeholders are submitted,
    and cleared when an empty string is explicitly submitted.
    """
    try:
        validated_incoming = _validate_and_normalize_search_config(payload.search_config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        current_full_config = await asyncio.to_thread(read_raw_config)
        if not isinstance(current_full_config, dict):
            current_full_config = {}
        current_search_section = current_full_config.get("search", {})
        if not isinstance(current_search_section, dict):
            current_search_section = {}

        merged_search = merge_unmasked(
            validated_incoming,
            current_search_section,
            preserve_on_empty=False,
        )
        current_full_config["search"] = merged_search

        await asyncio.to_thread(write_raw_config, current_full_config)

        enriched = _enrich_search_config_with_defaults(merged_search)
        logger.info(
            "Search configuration persisted to %s. engine=%s fallback=%s",
            get_authoritative_config_path(),
            enriched.get("engine"),
            enriched.get("fallback_engines"),
        )

        return {
            "ok": True,
            "status": "saved",
            "search_config": mask_secrets(enriched),
            "provider_capabilities": SearchProviderFactory.get_provider_capabilities(enriched),
            "effective_costs": _build_effective_costs_map(enriched),
            "raw_path": str(get_authoritative_config_path()),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to save search configuration: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist search config: {sanitize_error_text(str(exc))}",
        )


# ---------------------------------------------------------------------------
# Diagnostics: Structured Connection / Credential Check
# ---------------------------------------------------------------------------
@router.post("/test-connection")
async def test_search_provider_connection(payload: TestConnectionRequest):
    """
    Run a structured diagnostic check against a provider.
    Distinguishes network reachability, credential validity, quota state,
    and whether the probe consumed quota.
    """
    try:
        canonical_provider = SearchProviderFactory.resolve_provider_key(payload.provider)
    except UnknownProviderError as exc:
        return {
            "ok": False,
            "provider": payload.provider,
            "status": ProviderStatus.CONFIGURATION_ERROR.value,
            "check_type": DiagnosticCheckType.REACHABILITY.value,
            "reachable": False,
            "authenticated": False,
            "quota_available": None,
            "search_operational": False,
            "consumes_quota": False,
            "latency_ms": 0.0,
            "request_count": 0,
            "estimated_cost_usd": 0.0,
            "cost_basis": CostBasis.NOT_BILLABLE_LOCAL.value,
            "message": exc.message,
        }

    lock = _get_diagnostic_lock(canonical_provider)
    if lock.locked():
        return {
            "ok": False,
            "provider": canonical_provider,
            "status": ProviderStatus.RATE_LIMITED.value,
            "check_type": DiagnosticCheckType.REACHABILITY.value,
            "reachable": False,
            "consumes_quota": False,
            "latency_ms": 0.0,
            "request_count": 0,
            "estimated_cost_usd": 0.0,
            "cost_basis": CostBasis.NOT_BILLABLE_LOCAL.value,
            "message": f"A diagnostic check for {canonical_provider} is already in progress.",
        }

    async with lock:
        raw_config = await asyncio.to_thread(read_raw_config)
        system_search_config = (
            raw_config.get("search", {}) if isinstance(raw_config, dict) else {}
        )
        target_config = system_search_config
        tested_unsaved = False
        if payload.config and isinstance(payload.config, dict):
            target_config = merge_unmasked(
                payload.config,
                system_search_config,
                preserve_on_empty=True,
            )
            tested_unsaved = True

        req_check_type = (payload.check_type or "credentials").strip().lower()
        try:
            check_enum = DiagnosticCheckType(req_check_type)
        except ValueError:
            check_enum = DiagnosticCheckType.CREDENTIALS

        start_time = time.perf_counter()
        try:
            provider = SearchProviderFactory.create_provider(
                name=canonical_provider,
                config=target_config,
            )
            diag = await asyncio.wait_for(
                provider.diagnose(check_type=check_enum),
                timeout=8.0,
            )

            # Record billable diagnostic probes in the usage ledger so quota usage is auditable
            if diag.request_count > 0 and diag.consumes_quota:
                try:
                    store = await _get_store_async()
                    await asyncio.to_thread(
                        _record_usage_compat,
                        store,
                        query=f"[diagnostic:{check_enum.value}]",
                        provider=diag.provider,
                        results_count=1 if diag.ok else 0,
                        latency_ms=diag.latency_ms,
                        cost_usd=diag.estimated_cost_usd,
                        is_success=diag.ok,
                        error_message=None if diag.ok else diag.message,
                        run_id="diagnostic_connection_check",
                        status=diag.status,
                        request_count=diag.request_count,
                        usage_units=diag.usage_units,
                        cost_basis=diag.cost_basis,
                        privacy_mode=bool(target_config.get("privacy_mode", False)),
                    )
                except Exception as log_exc:
                    logger.debug(f"Failed to log diagnostic probe telemetry: {log_exc}")

            result_dict = diag.model_dump()
            result_dict["tested_unsaved_config"] = tested_unsaved
            return result_dict

        except asyncio.TimeoutError:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return {
                "ok": False,
                "provider": canonical_provider,
                "status": ProviderStatus.TIMEOUT.value,
                "check_type": check_enum.value,
                "reachable": False,
                "authenticated": None,
                "quota_available": None,
                "search_operational": False,
                "consumes_quota": False,
                "latency_ms": elapsed_ms,
                "request_count": 1,
                "estimated_cost_usd": 0.0,
                "cost_basis": CostBasis.UNKNOWN.value,
                "tested_unsaved_config": tested_unsaved,
                "message": f"{canonical_provider} diagnostic check timed out after 8.0s.",
            }
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return {
                "ok": False,
                "provider": canonical_provider,
                "status": ProviderStatus.UPSTREAM_ERROR.value,
                "check_type": check_enum.value,
                "reachable": False,
                "authenticated": None,
                "quota_available": None,
                "search_operational": False,
                "consumes_quota": False,
                "latency_ms": elapsed_ms,
                "request_count": 0,
                "estimated_cost_usd": 0.0,
                "cost_basis": CostBasis.UNKNOWN.value,
                "tested_unsaved_config": tested_unsaved,
                "message": f"Connection error: {sanitize_error_text(str(exc))}",
            }


# ---------------------------------------------------------------------------
# Diagnostics: Live Search Query Test (Phase 5.3 & 5.4)
# ---------------------------------------------------------------------------
@router.post("/test-query")
async def test_search_provider_query(payload: TestQueryRequest):
    """
    Execute a live search query against the selected provider.
    Supports testing an optional unsaved UI configuration snapshot with safe
    secret merging, and returns full status and cost-basis telemetry.
    """
    clean_q = (payload.query or "").strip()
    if not clean_q:
        raise HTTPException(status_code=400, detail="Test search query cannot be empty.")
    if len(clean_q) > MAX_QUERY_CHARS:
        clean_q = clean_q[:MAX_QUERY_CHARS].rstrip()

    try:
        canonical_provider = SearchProviderFactory.resolve_provider_key(payload.provider)
    except UnknownProviderError as exc:
        return {
            "ok": False,
            "provider": payload.provider,
            "status": ProviderStatus.CONFIGURATION_ERROR.value,
            "latency_ms": 0.0,
            "results_count": 0,
            "request_count": 0,
            "usage_units": None,
            "estimated_cost_usd": 0.0,
            "cost_basis": CostBasis.NOT_BILLABLE_LOCAL.value,
            "results": [],
            "error_message": exc.message,
        }

    lock = _get_diagnostic_lock(f"query_{canonical_provider}")
    if lock.locked():
        return {
            "ok": False,
            "provider": canonical_provider,
            "status": ProviderStatus.RATE_LIMITED.value,
            "latency_ms": 0.0,
            "results_count": 0,
            "request_count": 0,
            "usage_units": None,
            "estimated_cost_usd": 0.0,
            "cost_basis": CostBasis.NOT_BILLABLE_LOCAL.value,
            "results": [],
            "error_message": f"A test query for {canonical_provider} is already running.",
        }

    async with lock:
        raw_config = await asyncio.to_thread(read_raw_config)
        system_search_config = (
            raw_config.get("search", {}) if isinstance(raw_config, dict) else {}
        )
        target_config = system_search_config
        tested_unsaved = False
        if payload.config and isinstance(payload.config, dict):
            target_config = merge_unmasked(
                payload.config,
                system_search_config,
                preserve_on_empty=True,
            )
            tested_unsaved = True

        limit = min(max(int(payload.max_results or 3), 1), 15)
        start_time = time.perf_counter()

        try:
            provider = SearchProviderFactory.create_provider(
                name=canonical_provider,
                config=target_config,
            )
            response = await asyncio.wait_for(
                provider.search(
                    query=clean_q,
                    max_results=limit,
                    lang=str(target_config.get("lang", "en")),
                    country=str(target_config.get("country", "us")),
                ),
                timeout=15.0,
            )

            try:
                store = await _get_store_async()
                await asyncio.to_thread(
                    _record_usage_compat,
                    store,
                    query=clean_q,
                    provider=response.provider,
                    results_count=len(response.results),
                    latency_ms=response.latency_ms,
                    cost_usd=response.estimated_cost_usd,
                    is_success=response.is_success,
                    error_message=response.error_message,
                    run_id="diagnostic_test_query",
                    status=response.status,
                    request_count=response.request_count,
                    usage_units=response.usage_units,
                    cost_basis=response.cost_basis,
                    privacy_mode=bool(target_config.get("privacy_mode", False)),
                )
            except Exception as log_exc:
                logger.warning(f"Failed to persist diagnostic query telemetry: {log_exc}")

            return {
                "ok": response.is_success,
                "provider": response.provider,
                "status": response.status.value,
                "latency_ms": round(response.latency_ms, 2),
                "results_count": len(response.results),
                "request_count": response.request_count,
                "usage_units": response.usage_units,
                "estimated_cost_usd": response.estimated_cost_usd,
                "cost_basis": response.cost_basis.value,
                "tested_unsaved_config": tested_unsaved,
                "results": [r.model_dump() for r in response.results],
                "error_code": response.error_code,
                "error_message": response.error_message,
            }

        except asyncio.TimeoutError:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return {
                "ok": False,
                "provider": canonical_provider,
                "status": ProviderStatus.TIMEOUT.value,
                "latency_ms": elapsed_ms,
                "results_count": 0,
                "request_count": 1,
                "usage_units": None,
                "estimated_cost_usd": 0.0,
                "cost_basis": CostBasis.UNKNOWN.value,
                "tested_unsaved_config": tested_unsaved,
                "results": [],
                "error_code": "timeout",
                "error_message": f"{canonical_provider} test query timed out after 15.0s.",
            }
        except SearchProviderError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return {
                "ok": False,
                "provider": canonical_provider,
                "status": exc.status.value,
                "latency_ms": elapsed_ms,
                "results_count": 0,
                "request_count": 1,
                "usage_units": None,
                "estimated_cost_usd": 0.0,
                "cost_basis": CostBasis.UNKNOWN.value,
                "tested_unsaved_config": tested_unsaved,
                "results": [],
                "error_code": exc.error_code,
                "error_message": exc.message,
            }
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return {
                "ok": False,
                "provider": canonical_provider,
                "status": ProviderStatus.UPSTREAM_ERROR.value,
                "latency_ms": elapsed_ms,
                "results_count": 0,
                "request_count": 1,
                "usage_units": None,
                "estimated_cost_usd": 0.0,
                "cost_basis": CostBasis.UNKNOWN.value,
                "tested_unsaved_config": tested_unsaved,
                "results": [],
                "error_code": "upstream_error",
                "error_message": sanitize_error_text(str(exc)),
            }


__all__ = ["router"]
