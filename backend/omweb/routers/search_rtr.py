# backend/omweb/routers/search_rtr.py
"""
PELDRUN Search Settings, Capability Registry, Diagnostics, and Telemetry Router.

Canonical field names align with the frontend SearchTab.tsx contract:
- safe_search key is exposed to providers as "safesearch"
- cost key is exposed as "cost_per_query"
"""

from __future__ import annotations

import copy
import inspect
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from peldrun.tools.builtins.search_providers.base import SearchProviderError
from peldrun.tools.builtins.search_providers.factory import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_HARD_CAP,
    MIN_RESULTS_LIMIT,
    SearchProviderFactory,
)
from peldrun.tools.builtins.web_search import WebSearchTool

logger = logging.getLogger("omweb.routers.search_rtr")

router = APIRouter(tags=["search"])

_MASKED_SECRET_PLACEHOLDERS = {"********", "••••••••", "***", "<redacted>", "REDACTED"}
_SECRET_FIELD_NAMES = {"api_key", "cx", "search_engine_id", "folder_id", "secret_key", "token"}

# Canonical provider defaults — field names match the frontend SearchTab.tsx.
_DEFAULT_PROVIDER_CONFIGS: Dict[str, Dict[str, Any]] = {
    "duckduckgo": {
        "enabled": True,
        "safesearch": "moderate",
        "backend": "auto",
        "timeout": 15.0,
        "cost_per_query": 0.0,
    },
    "searxng": {
        "enabled": False,
        "base_url": "http://127.0.0.1:8080",
        "categories": "general",
        "timeout": 15.0,
        "cost_per_query": 0.0,
    },
    "tavily": {
        "enabled": False,
        "api_key": "",
        "search_depth": "basic",
        "timeout": 20.0,
        "cost_per_query": 0.005,
    },
    "brave": {
        "enabled": False,
        "api_key": "",
        "timeout": 15.0,
        "cost_per_query": 0.005,
    },
    "google": {
        "enabled": False,
        "api_key": "",
        "cx": "",
        "timeout": 15.0,
        "cost_per_query": 0.005,
    },
    "jina": {
        "enabled": False,
        "api_key": "",
        "timeout": 20.0,
        "cost_per_query": 0.002,
    },
    "yandex": {
        "enabled": False,
        "api_key": "",
        "folder_id": "",
        "mode": "cloud_v2",
        "region": "",
        "timeout": 20.0,
        "cost_per_query": 0.003,
    },
}

# Fields that must never be written back by the client payload.
_READONLY_CAPABILITY_KEYS = {
    "maxResultsLimit",
    "max_results_limit",
    "configured_max_results",
    "effective_max_results",
    "inherits_global_default",
    "limit_type",
    "requires_api_key",
    "requires_credentials",
    "supports_region",
    "supports_country",
    "supports_language",
    "supports_safe_search",
    "supports_safesearch",
    "cost_model",
    "provider_id",
    "display_name",
    "tagline",
    "docs_url",
    "lifecycle",
    "lifecycle_notice",
    "default_max_results",
    "max_results",
}


class SearchConfigValidationError(HTTPException, ValueError):
    """Exception that is simultaneously a FastAPI HTTPException and a Python ValueError."""

    def __init__(self, detail: str, status_code: int = 400) -> None:
        HTTPException.__init__(self, status_code=status_code, detail=detail)
        ValueError.__init__(self, detail)


class TestQueryRequest(BaseModel):
    query: str = Field(default="PELDRUN AI architecture", min_length=1)
    provider: Optional[str] = None
    engine: Optional[str] = None
    max_results: Optional[int] = Field(default=3, ge=MIN_RESULTS_LIMIT, le=MAX_RESULTS_HARD_CAP)
    lang: Optional[str] = None
    country: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    search_config: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Config file I/O — direct TOML access, no dependency on dead omweb.config API
# ---------------------------------------------------------------------------
def _resolve_config_toml_path() -> Path:
    """Resolve the authoritative config/config.toml path."""
    try:
        from omweb import config as omweb_cfg
        config_dir = getattr(omweb_cfg, "CONFIG_DIR", None)
        if config_dir:
            return Path(config_dir) / "config.toml"
    except Exception:
        pass

    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "config" / "config.toml"


def _load_full_config_dict() -> Dict[str, Any]:
    """Read the full config.toml (with fallback to tomllib)."""
    cfg_path = _resolve_config_toml_path()
    if not cfg_path.is_file():
        logger.warning("config.toml not found at %s", cfg_path)
        return {}

    try:
        import tomllib
        with cfg_path.open("rb") as f:
            data = tomllib.load(f)
        if isinstance(data, dict):
            return copy.deepcopy(data)
    except Exception as exc:
        logger.error("Failed to read %s: %s", cfg_path, exc)

    return {}


def _toml_primitive(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(val, (list, tuple)):
        inner = ", ".join(_toml_primitive(v) for v in val)
        return f"[{inner}]"
    return f'"{val}"'


def _serialize_to_toml(data: Dict[str, Any], prefix: str = "") -> List[str]:
    lines: List[str] = []
    scalars: List[tuple] = []
    tables: List[tuple] = []

    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            scalars.append((k, v))

    if prefix and (scalars or not tables):
        lines.append(f"[{prefix}]")

    for k, v in scalars:
        lines.append(f"{k} = {_toml_primitive(v)}")

    if scalars and tables:
        lines.append("")

    for idx, (k, sub) in enumerate(tables):
        sub_prefix = f"{prefix}.{k}" if prefix else k
        lines.extend(_serialize_to_toml(sub, prefix=sub_prefix))
        if idx < len(tables) - 1:
            lines.append("")

    return lines


def _save_full_config_dict(full_config: Dict[str, Any]) -> None:
    """Write the full config.toml atomically. Uses tomli_w if available, else fallback serializer."""
    cfg_path = _resolve_config_toml_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import tomli_w
        tmp = cfg_path.with_name(cfg_path.name + ".tmp")
        with tmp.open("wb") as f:
            tomli_w.dump(full_config, f)
        tmp.replace(cfg_path)
        return
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("tomli_w write failed: %s", exc)

    lines = _serialize_to_toml(full_config)
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(cfg_path)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _parse_strict_int(value: Any, field_label: str, min_value: int, max_value: int) -> int:
    if isinstance(value, bool) or value is None:
        raise SearchConfigValidationError(
            f"{field_label} must be between {min_value} and {max_value}."
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise SearchConfigValidationError(
                f"{field_label} must be a whole integer."
            )
        parsed = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or not (stripped.lstrip("+-").isdigit()):
            raise SearchConfigValidationError(
                f"{field_label} must be a valid integer."
            )
        parsed = int(stripped)
    else:
        raise SearchConfigValidationError(f"{field_label} must be an integer.")

    if parsed < min_value or parsed > max_value:
        raise SearchConfigValidationError(
            f"{field_label} must be between {min_value} and {max_value}, got {parsed}."
        )
    return parsed


def _parse_strict_float(value: Any, field_label: str, min_value: float, max_value: Optional[float] = None) -> float:
    if isinstance(value, bool) or value is None:
        raise SearchConfigValidationError(f"{field_label} must be a number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise SearchConfigValidationError(f"{field_label} must be a valid number.")
    if parsed < min_value or (max_value is not None and parsed > max_value):
        desc = f"between {min_value} and {max_value}" if max_value is not None else f">= {min_value}"
        raise SearchConfigValidationError(f"{field_label} must be {desc}, got {parsed}.")
    return parsed


def _validate_http_url(url_value: Any, field_label: str) -> str:
    if not isinstance(url_value, str) or not url_value.strip():
        raise SearchConfigValidationError(f"{field_label} must be a non-empty URL.")
    cleaned = url_value.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SearchConfigValidationError(f"{field_label} must be http:// or https://.")
    return cleaned


# ---------------------------------------------------------------------------
# Config enrichment / normalization
# ---------------------------------------------------------------------------
def _enrich_search_config_with_defaults(search_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Produce a canonical, complete search config dict.
    All provider sub-tables use canonical field names matching the frontend.
    """
    raw = SearchProviderFactory._unwrap_search_section(search_cfg)

    try:
        primary_display = SearchProviderFactory.get_canonical_display_name(
            str(raw.get("engine") or "DuckDuckGo")
        )
    except ValueError:
        primary_display = "DuckDuckGo"

    raw_fallbacks = raw.get("fallback_engines")
    if not isinstance(raw_fallbacks, list):
        raw_fallbacks = []

    normalized_fallbacks: List[str] = []
    for item in raw_fallbacks:
        if not item:
            continue
        try:
            disp = SearchProviderFactory.get_canonical_display_name(str(item))
            if disp != primary_display and disp not in normalized_fallbacks:
                normalized_fallbacks.append(disp)
        except ValueError:
            continue

    enabled_raw = raw.get("enabled", True)
    if isinstance(enabled_raw, bool):
        enabled_val = enabled_raw
    elif isinstance(enabled_raw, str):
        enabled_val = enabled_raw.strip().lower() != "false"
    else:
        enabled_val = True

    enriched: Dict[str, Any] = {
        "engine": primary_display,
        "fallback_engines": normalized_fallbacks,
        "enabled": enabled_val,
        "lang": str(raw.get("lang") or "en").strip() or "en",
        "country": str(raw.get("country") or "us").strip() or "us",
        "default_max_results": SearchProviderFactory.get_global_default_max_results(raw, strict=False),
        "total_budget_seconds": float(raw.get("total_budget_seconds", 22.0) or 22.0),
        "max_retries": int(raw.get("max_retries", 0) or 0),
        "retry_delay": float(raw.get("retry_delay", 1.0) if raw.get("retry_delay") is not None else 1.0),
        "privacy_mode": bool(raw.get("privacy_mode", False)),
    }

    for prov_key, defaults in _DEFAULT_PROVIDER_CONFIGS.items():
        existing_prov = SearchProviderFactory._extract_provider_section(prov_key, raw)
        merged = copy.deepcopy(defaults)

        for k, v in existing_prov.items():
            if k in _READONLY_CAPABILITY_KEYS:
                continue
            if k == "max_results":
                if v is None or v == "":
                    merged.pop("max_results", None)
                else:
                    try:
                        cap_limit = SearchProviderFactory.get_provider_capability(prov_key, raw).max_results_limit
                        merged["max_results"] = _parse_strict_int(
                            v,
                            f"{SearchProviderFactory.get_canonical_display_name(prov_key)} max_results",
                            MIN_RESULTS_LIMIT,
                            min(int(cap_limit), MAX_RESULTS_HARD_CAP),
                        )
                    except SearchConfigValidationError:
                        merged.pop("max_results", None)
            else:
                merged[k] = v

        enriched[prov_key] = merged

    return enriched


def _validate_and_normalize_search_config(
    raw_search: Dict[str, Any],
    existing_search: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate + persist incoming search config. Provider sub-tables use canonical
    field names matching frontend SearchTab.tsx.
    """
    if not isinstance(raw_search, dict):
        raise SearchConfigValidationError("Search configuration payload must be a JSON object.")

    incoming = SearchProviderFactory._unwrap_search_section(raw_search)
    base_existing = _enrich_search_config_with_defaults(existing_search)

    # 1. Engine
    raw_engine = incoming.get("engine", base_existing["engine"])
    if not isinstance(raw_engine, str) or not raw_engine.strip():
        raise SearchConfigValidationError("Primary search engine must be a non-empty string.")
    try:
        primary_key = SearchProviderFactory.normalize_provider_key(raw_engine)
        primary_display = SearchProviderFactory.get_canonical_display_name(primary_key)
    except ValueError as exc:
        raise SearchConfigValidationError(str(exc)) from exc

    # 2. Fallbacks
    raw_fallbacks = incoming.get("fallback_engines", base_existing["fallback_engines"])
    if isinstance(raw_fallbacks, str):
        raw_fallbacks = [p.strip() for p in raw_fallbacks.split(",") if p.strip()]
    if not isinstance(raw_fallbacks, list):
        raise SearchConfigValidationError("fallback_engines must be a list.")

    normalized_fallbacks: List[str] = []
    for fb in raw_fallbacks:
        if not isinstance(fb, str) or not fb.strip():
            continue
        try:
            fb_key = SearchProviderFactory.normalize_provider_key(fb)
            fb_display = SearchProviderFactory.get_canonical_display_name(fb_key)
        except ValueError as exc:
            raise SearchConfigValidationError(str(exc)) from exc
        if fb_display != primary_display and fb_display not in normalized_fallbacks:
            normalized_fallbacks.append(fb_display)

    # 3. Global defaults
    if "default_max_results" in incoming and incoming["default_max_results"] is not None:
        default_max_results = _parse_strict_int(
            incoming["default_max_results"], "default_max_results",
            MIN_RESULTS_LIMIT, MAX_RESULTS_HARD_CAP,
        )
    else:
        default_max_results = int(base_existing.get("default_max_results", DEFAULT_MAX_RESULTS))

    # 4. Toggles
    enabled_val = incoming.get("enabled", base_existing["enabled"])
    if not isinstance(enabled_val, bool):
        raise SearchConfigValidationError("search.enabled must be a boolean.")

    privacy_val = incoming.get("privacy_mode", base_existing["privacy_mode"])
    if not isinstance(privacy_val, bool):
        raise SearchConfigValidationError("search.privacy_mode must be a boolean.")

    lang_val = incoming.get("lang", base_existing["lang"])
    if not isinstance(lang_val, str) or not lang_val.strip():
        raise SearchConfigValidationError("search.lang must be a non-empty string.")

    country_val = incoming.get("country", base_existing["country"])
    if not isinstance(country_val, str) or not country_val.strip():
        raise SearchConfigValidationError("search.country must be a non-empty string.")

    total_budget = _parse_strict_float(
        incoming.get("total_budget_seconds", base_existing["total_budget_seconds"]),
        "total_budget_seconds", 1.0, 300.0,
    )
    max_retries = _parse_strict_int(
        incoming.get("max_retries", base_existing["max_retries"]),
        "max_retries", 0, 10,
    )
    retry_delay = _parse_strict_float(
        incoming.get("retry_delay", base_existing["retry_delay"]),
        "retry_delay", 0.0, 60.0,
    )

    normalized: Dict[str, Any] = {
        "engine": primary_display,
        "fallback_engines": normalized_fallbacks,
        "enabled": enabled_val,
        "lang": lang_val.strip().lower(),
        "country": country_val.strip().lower(),
        "default_max_results": default_max_results,
        "total_budget_seconds": total_budget,
        "max_retries": max_retries,
        "retry_delay": retry_delay,
        "privacy_mode": privacy_val,
    }

    # 5. Per-provider sub-tables
    for prov_key, defaults in _DEFAULT_PROVIDER_CONFIGS.items():
        display_name = SearchProviderFactory.get_canonical_display_name(prov_key)
        cap = SearchProviderFactory.get_provider_capability(prov_key)
        prov_limit = min(int(cap.max_results_limit), MAX_RESULTS_HARD_CAP)

        existing_prov = copy.deepcopy(base_existing.get(prov_key, defaults))

        # Find incoming sub-table (accept lowercase key, display name, or case variants)
        incoming_prov_raw: Optional[Dict[str, Any]] = None
        for cand_key in (prov_key, display_name, prov_key.lower(), display_name.lower()):
            if cand_key in incoming and isinstance(incoming[cand_key], dict):
                incoming_prov_raw = incoming[cand_key]
                break
        if incoming_prov_raw is None:
            for k, v in incoming.items():
                if isinstance(k, str) and k.strip().lower() == prov_key and isinstance(v, dict):
                    incoming_prov_raw = v
                    break

        if incoming_prov_raw is None:
            normalized[prov_key] = existing_prov
            continue

        merged = copy.deepcopy(defaults)
        # Start with existing validated state (canonical names)
        for k, v in existing_prov.items():
            if k in _READONLY_CAPABILITY_KEYS:
                continue
            merged[k] = v

        # Handle max_results override / inheritance
        if "max_results" not in incoming_prov_raw or incoming_prov_raw.get("max_results") in (None, ""):
            merged.pop("max_results", None)
        else:
            merged["max_results"] = _parse_strict_int(
                incoming_prov_raw["max_results"],
                f"{display_name} max_results",
                MIN_RESULTS_LIMIT, prov_limit,
            )

        # Handle all other fields
        for field_key, field_val in incoming_prov_raw.items():
            if field_key == "max_results" or field_key in _READONLY_CAPABILITY_KEYS:
                continue

            if field_key in _SECRET_FIELD_NAMES:
                if isinstance(field_val, str) and field_val.strip() in _MASKED_SECRET_PLACEHOLDERS:
                    merged[field_key] = existing_prov.get(field_key, "")
                elif field_val is None:
                    merged[field_key] = ""
                else:
                    merged[field_key] = str(field_val).strip()
                continue

            if field_key == "enabled":
                if not isinstance(field_val, bool):
                    raise SearchConfigValidationError(f"{display_name} enabled must be a boolean.")
                merged["enabled"] = field_val
                continue

            if field_key == "timeout":
                if field_val is not None and field_val != "":
                    merged["timeout"] = _parse_strict_float(
                        field_val, f"{display_name} timeout", 1.0, 180.0,
                    )
                continue

            if field_key == "cost_per_query":
                if field_val is not None and field_val != "":
                    merged["cost_per_query"] = _parse_strict_float(
                        field_val, f"{display_name} cost_per_query", 0.0, 10.0,
                    )
                continue

            if field_key in ("base_url", "instance_url", "endpoint"):
                if field_val is not None and str(field_val).strip():
                    merged["base_url"] = _validate_http_url(field_val, f"{display_name} base_url")
                continue

            if field_key == "search_depth" and prov_key == "tavily":
                val = str(field_val).lower()
                if val not in ("basic", "advanced", "fast", "ultra-fast"):
                    raise SearchConfigValidationError("Tavily search_depth must be 'basic' or 'advanced'.")
                merged["search_depth"] = val
                continue

            if field_key == "mode" and prov_key == "yandex":
                val = str(field_val).lower()
                if val not in ("cloud_v2", "classic_xml", "xml", "v2", "default"):
                    raise SearchConfigValidationError("Yandex mode must be 'cloud_v2' or 'classic_xml'.")
                merged["mode"] = val
                continue

            # Default: accept verbatim
            merged[field_key] = field_val

        normalized[prov_key] = merged

    return normalized


def _mask_secrets_for_client(search_cfg: Dict[str, Any]) -> Dict[str, Any]:
    masked = copy.deepcopy(search_cfg)
    for prov_key in SearchProviderFactory.list_provider_keys():
        table = masked.get(prov_key)
        if not isinstance(table, dict):
            continue
        for sk in list(table.keys()):
            if sk in _SECRET_FIELD_NAMES:
                raw = table.get(sk)
                table[sk] = "********" if isinstance(raw, str) and raw.strip() else ""
    return masked


def _build_provider_status_list(search_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    capabilities = SearchProviderFactory.get_provider_capabilities(search_cfg)
    primary_key = SearchProviderFactory.normalize_provider_key(
        str(search_cfg.get("engine") or "DuckDuckGo")
    )
    fallback_keys = [
        SearchProviderFactory.normalize_provider_key(fb)
        for fb in (search_cfg.get("fallback_engines") or [])
        if fb
    ]

    providers_out: List[Dict[str, Any]] = []
    for key in SearchProviderFactory.list_provider_keys():
        cap = capabilities[key]
        table = SearchProviderFactory._extract_provider_section(key, search_cfg)
        requires_key = bool(cap.get("requires_api_key", False) or cap.get("requires_credentials", False))
        has_key = bool(str(table.get("api_key") or "").strip())
        if key == "google":
            has_key = has_key and bool(str(table.get("cx") or "").strip())

        providers_out.append({
            **cap,
            "id": key,
            "key": key,
            "name": cap.get("name") or cap.get("display_name") or key,
            "enabled": bool(table.get("enabled", key == primary_key)),
            "configured": (not requires_key) or has_key,
            "has_api_key": has_key,
            "is_primary": key == primary_key,
            "is_fallback": key in fallback_keys,
        })
    return providers_out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/config")
async def get_search_config() -> Dict[str, Any]:
    full_cfg = _load_full_config_dict()
    raw_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}
    enriched = _enrich_search_config_with_defaults(raw_search)
    capabilities = SearchProviderFactory.get_provider_capabilities(enriched)
    masked = _mask_secrets_for_client(enriched)

    return {
        "status": "ok",
        "search": masked,
        "config": masked,
        "default_max_results": enriched["default_max_results"],
        "max_results_hard_cap": MAX_RESULTS_HARD_CAP,
        "provider_capabilities": capabilities,
        "providers": _build_provider_status_list(enriched),
    }


@router.post("/config")
@router.put("/config")
async def update_search_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    full_cfg = _load_full_config_dict()
    existing_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}

    candidate = payload.get("search") if isinstance(payload.get("search"), dict) else payload
    if isinstance(candidate, dict) and "config" in candidate and isinstance(candidate["config"], dict):
        if "engine" in candidate["config"] or "default_max_results" in candidate["config"]:
            candidate = candidate["config"]

    normalized = _validate_and_normalize_search_config(candidate, existing_search=existing_search)
    full_cfg["search"] = normalized
    _save_full_config_dict(full_cfg)

    WebSearchTool.reset_circuit_breakers()

    capabilities = SearchProviderFactory.get_provider_capabilities(normalized)
    masked = _mask_secrets_for_client(normalized)

    return {
        "status": "ok",
        "message": "Search configuration saved successfully.",
        "search": masked,
        "config": masked,
        "default_max_results": normalized["default_max_results"],
        "max_results_hard_cap": MAX_RESULTS_HARD_CAP,
        "provider_capabilities": capabilities,
        "providers": _build_provider_status_list(normalized),
    }


@router.get("/providers")
async def get_search_providers() -> Dict[str, Any]:
    full_cfg = _load_full_config_dict()
    raw_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}
    enriched = _enrich_search_config_with_defaults(raw_search)
    capabilities = SearchProviderFactory.get_provider_capabilities(enriched)

    return {
        "status": "ok",
        "default_max_results": enriched["default_max_results"],
        "max_results_hard_cap": MAX_RESULTS_HARD_CAP,
        "provider_capabilities": capabilities,
        "providers": _build_provider_status_list(enriched),
    }


@router.get("/metrics")
async def get_search_metrics(days: int = Query(default=30, ge=1, le=365)) -> Dict[str, Any]:
    full_cfg = _load_full_config_dict()
    raw_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}
    enriched = _enrich_search_config_with_defaults(raw_search)
    capabilities = SearchProviderFactory.get_provider_capabilities(enriched)

    metrics_summary: Dict[str, Any] = {
        "total_queries": 0, "successful_queries": 0, "failed_queries": 0,
        "total_cost_usd": 0.0, "avg_latency_ms": 0.0,
        "by_provider": {}, "recent": [],
    }

    try:
        from peldrun.runtime import search_usage_store as sus_mod
        getter = getattr(sus_mod, "get_search_usage_store", None)
        if callable(getter):
            store = getter()
            for m in ("get_metrics", "get_summary", "summarize", "get_summary_metrics"):
                fn = getattr(store, m, None)
                if callable(fn):
                    sig = inspect.signature(fn)
                    res = fn(days=days) if "days" in sig.parameters else (fn(period="all") if "period" in sig.parameters else fn())
                    if inspect.isawaitable(res):
                        res = await res
                    if isinstance(res, dict):
                        metrics_summary.update(res)
                    break
    except Exception as exc:
        logger.debug("Search metrics store notice: %s", exc)

    return {
        "status": "ok",
        "days": days,
        "default_max_results": enriched["default_max_results"],
        "max_results_hard_cap": MAX_RESULTS_HARD_CAP,
        "provider_capabilities": capabilities,
        "providers": _build_provider_status_list(enriched),
        "metrics": metrics_summary,
        **metrics_summary,
    }


@router.post("/test")
@router.post("/test-query")
async def run_diagnostic_search_test(request: TestQueryRequest) -> Dict[str, Any]:
    full_cfg = _load_full_config_dict()
    persisted_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}
    enriched_persisted = _enrich_search_config_with_defaults(persisted_search)

    snapshot = request.config if request.config is not None else request.search_config
    if snapshot is not None:
        active_search_cfg = _validate_and_normalize_search_config(snapshot, existing_search=enriched_persisted)
    else:
        active_search_cfg = enriched_persisted

    target_provider_raw = request.provider or request.engine
    target_key: Optional[str] = None
    if target_provider_raw:
        try:
            target_key = SearchProviderFactory.normalize_provider_key(target_provider_raw)
        except ValueError as exc:
            raise SearchConfigValidationError(str(exc)) from exc

    audit_key = target_key or SearchProviderFactory.normalize_provider_key(
        str(active_search_cfg.get("engine") or "DuckDuckGo")
    )
    pre_audit = SearchProviderFactory.resolve_count_Audit_details(
        audit_key, requested=request.max_results, search_config=active_search_cfg,
    )

    start_ts = time.perf_counter()
    try:
        response = await SearchProviderFactory.search(
            query=request.query,
            max_results=request.max_results,
            search_config=active_search_cfg,
            override_provider=target_key,
            lang=request.lang,
            country=request.country,
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        return {
            "status": "error", "ok": False, "success": False,
            "error": str(exc) or type(exc).__name__,
            "provider": pre_audit["provider"],
            "query": request.query,
            "requested_max_results": request.max_results,
            "effective_max_results": pre_audit["effective_max_results"],
            "max_results_limit": pre_audit["max_results_limit"],
            "results_count": 0, "results": [],
            "latency_ms": latency_ms,
        }

    latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
    resp_meta = (
        dict(response.metadata)
        if hasattr(response, "metadata") and isinstance(getattr(response, "metadata"), dict)
        else {}
    )
    resolved_provider_name = str(
        getattr(response, "provider", None) or resp_meta.get("provider") or pre_audit["provider"]
    )
    post_audit = SearchProviderFactory.resolve_count_Audit_details(
        resolved_provider_name, requested=request.max_results, search_config=active_search_cfg,
    )
    effective_max = int(
        getattr(response, "effective_max_results", None)
        or resp_meta.get("effective_max_results")
        or post_audit["effective_max_results"]
    )

    serialized: List[Dict[str, Any]] = []
    for item in list(getattr(response, "results", []) or [])[:effective_max]:
        if hasattr(item, "model_dump"):
            serialized.append(item.model_dump())
        elif hasattr(item, "dict"):
            serialized.append(item.dict())
        else:
            serialized.append({
                "title": getattr(item, "title", ""),
                "url": getattr(item, "url", ""),
                "snippet": getattr(item, "snippet", ""),
            })

    return {
        "status": "ok", "ok": True, "success": True,
        "provider": resolved_provider_name,
        "query": request.query,
        "requested_max_results": request.max_results,
        "effective_max_results": effective_max,
        "max_results_limit": post_audit["max_results_limit"],
        "results_count": len(serialized),
        "results": serialized,
        "latency_ms": getattr(response, "latency_ms", None) or latency_ms,
        "cost_usd": float(getattr(response, "estimated_cost_usd", 0.0) or 0.0),
    }


__all__ = [
    "SearchConfigValidationError",
    "TestQueryRequest",
    "_enrich_search_config_with_defaults",
    "_validate_and_normalize_search_config",
    "router",
]
