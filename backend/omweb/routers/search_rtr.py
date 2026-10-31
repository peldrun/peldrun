# backend/omweb/routers/search_rtr.py
"""
PELDRUN Search Settings, Capability Registry, Diagnostics, and Telemetry Router.

Canonical field names align with the frontend SearchTab.tsx contract:
- safe_search key is exposed to providers as "safesearch"
- cost key is exposed as "cost_per_query"

Hardening notes:
- /test classifies the provider response using response.is_success AND response.status
  (not merely the presence of results), so a partial-result response with a failure
  status is reported as a failure and never as a full success.
- /test-connection exposes a dedicated provider diagnostic endpoint (credentials /
  reachability / live-search) that SearchTab.tsx can drive independently.
- Exception branches of /test no longer assert request_count=1 / estimated_cost_usd=0.0
  when those numbers cannot be proven; they return usage_unknown=True instead.
- The preservation of request_count=0 (no more `or 1` coercion) keeps usage counts accurate.
- /metrics resolves `days` into an effective period and never falls through to "all records"
  for unrecognized `period` values.
"""

from __future__ import annotations

import copy
import inspect
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from peldrun.tools.builtins.search_providers.base import (
    DiagnosticCheckType,
    ProviderStatus,
    SearchProviderError,
)
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

# Provider status values that denote a genuine, non-empty failure.
_FAILURE_STATUS_VALUES = frozenset(
    {
        ProviderStatus.CONFIGURATION_ERROR.value,
        ProviderStatus.AUTHENTICATION_ERROR.value,
        ProviderStatus.PERMISSION_ERROR.value,
        ProviderStatus.RATE_LIMITED.value,
        ProviderStatus.QUOTA_EXHAUSTED.value,
        ProviderStatus.TIMEOUT.value,
        ProviderStatus.UPSTREAM_ERROR.value,
        ProviderStatus.INVALID_RESPONSE.value,
        ProviderStatus.DISABLED.value,
    }
)

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

# Defaults for the [search.image_search] sub-table. This is a settings
# block, not a provider, so it is handled separately from
# _DEFAULT_PROVIDER_CONFIGS.
_DEFAULT_IMAGE_SEARCH_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "default_max_results": DEFAULT_MAX_RESULTS,
    "max_results_cap": MAX_RESULTS_HARD_CAP,
    "engine": "",
    "fallback_engines": [],
    "lang": "",
    "country": "",
    "total_budget_seconds": 20.0,
    "max_retries": 0,
    "retry_delay": 1.0,
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


class TestConnectionRequest(BaseModel):
    """
    Parameters accepted by POST /test-connection.

    - `provider` / `engine` : provider to test. Defaults to the configured primary engine.
    - `config` / `search_config` : optional search-config snapshot (same shape as /config).
    - `check_type` : "credentials" (default), "reachability", or "live_search".
    """
    provider: Optional[str] = None
    engine: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    search_config: Optional[Dict[str, Any]] = None
    check_type: str = Field(default="credentials")


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
# Response classification — used by /test and any other read-side diagnostic
# ---------------------------------------------------------------------------
def _classify_provider_response(
    response: Any,
) -> Tuple[str, str, int]:
    """
    Classify a SearchResponse into one of three outcome classes:
        - "success" : usable results were returned and the provider declared success.
        - "empty"   : the search completed but returned zero matches.
        - "failure" : the provider declared a genuine error (auth, timeout, quota, etc.).

    Classification prioritizes the provider's own is_success/status over the mere
    presence of results, so partial results arriving alongside a failure status are
    reported as a failure, not as a success.

    Returns (outcome_class, provider_status_value, result_count).
    """
    status_obj = getattr(response, "status", None)
    status_value = (
        status_obj.value if hasattr(status_obj, "value")
        else str(status_obj or "").strip().lower()
    )
    is_success = bool(getattr(response, "is_success", False))
    raw_results = list(getattr(response, "results", []) or [])
    result_count = len(raw_results)

    # 1. Explicit failure status → failure (even if partial results accompany it).
    if status_value in _FAILURE_STATUS_VALUES:
        return ("failure", status_value, result_count)

    # 2. Explicit empty status → empty (no results).
    if status_value == ProviderStatus.EMPTY.value:
        return ("empty", status_value, 0)

    # 3. Provider declared success.
    if is_success:
        if result_count > 0:
            return ("success", status_value or ProviderStatus.SUCCESS.value, result_count)
        # Contract violation (is_success=True but zero results) → empty.
        return ("empty", status_value or ProviderStatus.EMPTY.value, 0)

    # 4. is_success=False but status=success (contract violation) → failure.
    if status_value == ProviderStatus.SUCCESS.value:
        return ("failure", "invalid_response", result_count)

    # 5. is_success=False with an unknown/missing status → failure.
    return ("failure", status_value or "unknown", result_count)


def _resolve_effective_metrics_period(period: str, days: int) -> str:
    """
    Normalize (period, days) into a single period keyword that drives store lookups.

    - If `period` is a recognized keyword, it wins.
    - Otherwise, derive the period from `days`, so an unrecognized `period` value
      never silently falls through to "all records".
    """
    recognized = {"day", "week", "month", "all"}
    p = (period or "").strip().lower()
    if p in recognized:
        return p

    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    d = max(1, min(d, 365))

    if d <= 1:
        return "day"
    if d <= 7:
        return "week"
    if d <= 30:
        return "month"
    return "all"


def _extract_exception_usage_evidence(exc: BaseException) -> Dict[str, Any]:
    """
    Derive a conservative usage-evidence bundle from an exception that escaped the
    factory. We only assert a request happened when the exception's own status
    proves that a transport round-trip occurred (auth/permission/rate/quota/upstream/timeout).
    Otherwise we explicitly flag the accounting as unknown.
    """
    if isinstance(exc, SearchProviderError):
        status_obj = getattr(exc, "status", None)
        status_val = (
            status_obj.value if hasattr(status_obj, "value") else str(status_obj or "").strip().lower()
        )
        # Statuses that imply the provider responded (or the network round-trip happened).
        if status_val in (
            ProviderStatus.AUTHENTICATION_ERROR.value,
            ProviderStatus.PERMISSION_ERROR.value,
            ProviderStatus.RATE_LIMITED.value,
            ProviderStatus.QUOTA_EXHAUSTED.value,
            ProviderStatus.TIMEOUT.value,
            ProviderStatus.UPSTREAM_ERROR.value,
            ProviderStatus.INVALID_RESPONSE.value,
        ):
            return {
                "request_count": 1,
                "estimated_cost_usd": 0.0,
                "cost_basis": "unknown",
                "usage_unknown": True,
            }
        # Configuration errors clearly did not issue a physical request.
        if status_val == ProviderStatus.CONFIGURATION_ERROR.value:
            return {
                "request_count": 0,
                "estimated_cost_usd": 0.0,
                "cost_basis": "not_billable_local",
                "usage_unknown": False,
            }

    # Unknown origin — be conservative and flag the accounting.
    return {
        "request_count": 0,
        "estimated_cost_usd": 0.0,
        "cost_basis": "unknown",
        "usage_unknown": True,
    }


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

    # ----- [search.image_search] sub-table -----
    raw_image = (
        raw.get("image_search")
        if isinstance(raw.get("image_search"), dict)
        else {}
    )
    image_cfg: Dict[str, Any] = copy.deepcopy(_DEFAULT_IMAGE_SEARCH_CONFIG)

    if isinstance(raw_image.get("enabled"), bool):
        image_cfg["enabled"] = raw_image["enabled"]

    for numeric_key in ("default_max_results", "max_results_cap"):
        if numeric_key in raw_image and raw_image[numeric_key] is not None:
            try:
                image_cfg[numeric_key] = _parse_strict_int(
                    raw_image[numeric_key],
                    f"image_search.{numeric_key}",
                    MIN_RESULTS_LIMIT,
                    MAX_RESULTS_HARD_CAP,
                )
            except SearchConfigValidationError:
                pass

    if isinstance(raw_image.get("engine"), str) and raw_image["engine"].strip():
        try:
            image_cfg["engine"] = SearchProviderFactory.get_canonical_display_name(
                raw_image["engine"]
            )
        except ValueError:
            image_cfg["engine"] = ""

    raw_image_fb = raw_image.get("fallback_engines")
    if isinstance(raw_image_fb, list):
        image_fbs: List[str] = []
        for item in raw_image_fb:
            if not item:
                continue
            try:
                disp = SearchProviderFactory.get_canonical_display_name(str(item))
                if disp not in image_fbs:
                    image_fbs.append(disp)
            except ValueError:
                continue
        image_cfg["fallback_engines"] = image_fbs

    if isinstance(raw_image.get("lang"), str):
        image_cfg["lang"] = raw_image["lang"].strip().lower()
    if isinstance(raw_image.get("country"), str):
        image_cfg["country"] = raw_image["country"].strip().lower()

    for fkey in ("total_budget_seconds", "retry_delay"):
        if fkey in raw_image and raw_image[fkey] is not None:
            try:
                image_cfg[fkey] = _parse_strict_float(
                    raw_image[fkey],
                    f"image_search.{fkey}",
                    0.0 if fkey == "retry_delay" else 1.0,
                    300.0,
                )
            except SearchConfigValidationError:
                pass

    if "max_retries" in raw_image and raw_image["max_retries"] is not None:
        try:
            image_cfg["max_retries"] = _parse_strict_int(
                raw_image["max_retries"], "image_search.max_retries", 0, 10
            )
        except SearchConfigValidationError:
            pass

    enriched["image_search"] = image_cfg

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

    # ----- [search.image_search] sub-table (validate + persist) -----
    raw_image_in = incoming.get("image_search")

    image_cfg: Dict[str, Any] = copy.deepcopy(_DEFAULT_IMAGE_SEARCH_CONFIG)

    # Start from existing validated state so omitted fields are preserved
    existing_image = base_existing.get("image_search")
    if isinstance(existing_image, dict):
        for k, v in existing_image.items():
            if k in image_cfg:
                image_cfg[k] = v

    if isinstance(raw_image_in, dict):
        if "enabled" in raw_image_in:
            if not isinstance(raw_image_in["enabled"], bool):
                raise SearchConfigValidationError(
                    "image_search.enabled must be a boolean."
                )
            image_cfg["enabled"] = raw_image_in["enabled"]

        for numeric_key in ("default_max_results", "max_results_cap"):
            if (
                numeric_key in raw_image_in
                and raw_image_in[numeric_key] not in (None, "")
            ):
                image_cfg[numeric_key] = _parse_strict_int(
                    raw_image_in[numeric_key],
                    f"image_search.{numeric_key}",
                    MIN_RESULTS_LIMIT,
                    MAX_RESULTS_HARD_CAP,
                )

        if "engine" in raw_image_in:
            raw_eng = raw_image_in["engine"]
            if raw_eng is None or raw_eng == "":
                image_cfg["engine"] = ""
            elif isinstance(raw_eng, str):
                try:
                    eng_key = SearchProviderFactory.normalize_provider_key(raw_eng)
                    if not SearchProviderFactory.provider_supports_images(eng_key):
                        raise SearchConfigValidationError(
                            f"image_search.engine '{raw_eng}' does not support "
                            f"image search. Supported: DuckDuckGo, SearXNG, "
                            f"Brave, Google."
                        )
                    image_cfg["engine"] = (
                        SearchProviderFactory.get_canonical_display_name(eng_key)
                    )
                except ValueError as exc:
                    raise SearchConfigValidationError(str(exc)) from exc
            else:
                raise SearchConfigValidationError(
                    "image_search.engine must be a string."
                )

        if "fallback_engines" in raw_image_in:
            raw_fb = raw_image_in["fallback_engines"]
            if raw_fb is None:
                image_cfg["fallback_engines"] = []
            else:
                if isinstance(raw_fb, str):
                    raw_fb_list = [
                        p.strip() for p in raw_fb.split(",") if p.strip()
                    ]
                elif isinstance(raw_fb, list):
                    raw_fb_list = raw_fb
                else:
                    raise SearchConfigValidationError(
                        "image_search.fallback_engines must be a list."
                    )
                parsed_fbs: List[str] = []
                for fb in raw_fb_list:
                    if not isinstance(fb, str) or not fb.strip():
                        continue
                    try:
                        fb_key = SearchProviderFactory.normalize_provider_key(fb)
                        if not SearchProviderFactory.provider_supports_images(fb_key):
                            raise SearchConfigValidationError(
                                f"image_search.fallback_engines '{fb}' does "
                                f"not support image search."
                            )
                        disp = SearchProviderFactory.get_canonical_display_name(fb_key)
                        if disp not in parsed_fbs:
                            parsed_fbs.append(disp)
                    except ValueError as exc:
                        raise SearchConfigValidationError(str(exc)) from exc
                image_cfg["fallback_engines"] = parsed_fbs

        if "lang" in raw_image_in:
            val = raw_image_in["lang"]
            if val is None:
                image_cfg["lang"] = ""
            elif isinstance(val, str):
                image_cfg["lang"] = val.strip().lower()
            else:
                raise SearchConfigValidationError(
                    "image_search.lang must be a string."
                )

        if "country" in raw_image_in:
            val = raw_image_in["country"]
            if val is None:
                image_cfg["country"] = ""
            elif isinstance(val, str):
                image_cfg["country"] = val.strip().lower()
            else:
                raise SearchConfigValidationError(
                    "image_search.country must be a string."
                )

        if (
            "total_budget_seconds" in raw_image_in
            and raw_image_in["total_budget_seconds"] is not None
        ):
            image_cfg["total_budget_seconds"] = _parse_strict_float(
                raw_image_in["total_budget_seconds"],
                "image_search.total_budget_seconds",
                1.0, 300.0,
            )

        if "max_retries" in raw_image_in and raw_image_in["max_retries"] is not None:
            image_cfg["max_retries"] = _parse_strict_int(
                raw_image_in["max_retries"], "image_search.max_retries", 0, 10
            )

        if "retry_delay" in raw_image_in and raw_image_in["retry_delay"] is not None:
            image_cfg["retry_delay"] = _parse_strict_float(
                raw_image_in["retry_delay"], "image_search.retry_delay", 0.0, 60.0
            )

    normalized["image_search"] = image_cfg

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
# Period helper for metrics queries
# ---------------------------------------------------------------------------
def _period_to_cutoff(period: str) -> float:
    """Map a period keyword to a Unix timestamp cutoff."""
    now = time.time()
    p = (period or "all").strip().lower()
    if p == "day":
        return now - 86400.0
    if p == "week":
        return now - (7 * 86400.0)
    if p == "month":
        return now - (30 * 86400.0)
    return 0.0


def _record_search_telemetry(
    *,
    query: str,
    provider: str,
    results_count: int,
    latency_ms: float,
    cost_usd: float,
    is_success: bool,
    status: str,
    error_message: Optional[str],
    request_count: int,
    cost_basis: str,
    privacy_mode: bool,
) -> None:
    """Persist a search telemetry fact to the local SQLite ledger.

    This helper centralizes the recording call so both /test outcome
    branches (success and failure) share exactly the same persistence
    logic. Failures are logged at DEBUG level so a broken store cannot
    break the user-facing response.
    """
    try:
        from peldrun.runtime.search_usage_store import get_search_usage_store

        get_search_usage_store().record_search(
            query=query,
            provider=provider,
            results_count=results_count,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            is_success=is_success,
            status=status,
            error_message=error_message,
            request_count=request_count,
            cost_basis=cost_basis,
            privacy_mode=privacy_mode,
        )
    except Exception as exc:
        logger.debug("Failed to record search usage: %s", exc)


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
async def get_search_metrics(
    period: str = Query(default="week"),
    days: int = Query(default=7, ge=1, le=365),
) -> Dict[str, Any]:
    """
    Return search telemetry shaped for the frontend SearchTelemetryPage.

    Response shape matches:
      - summary       : KPI object
      - breakdown     : per-provider array
      - timeseries    : time-bucketed array
      - privacy_mode  : boolean
    """
    full_cfg = _load_full_config_dict()
    raw_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}
    enriched = _enrich_search_config_with_defaults(raw_search)
    capabilities = SearchProviderFactory.get_provider_capabilities(enriched)

    # Resolve `days` into the effective period so an unrecognized `period`
    # never silently widens to "all records".
    effective_period = _resolve_effective_metrics_period(period, days)

    empty_summary: Dict[str, Any] = {
        "total_queries": 0,
        "total_requests": 0,
        "total_cost_usd": 0.0,
        "successful_queries": 0,
        "empty_queries": 0,
        "failed_queries": 0,
        "success_rate_pct": 0.0,
        "avg_latency_ms": 0.0,
        "total_results": 0,
    }

    summary_data: Dict[str, Any] = dict(empty_summary)
    breakdown_data: List[Dict[str, Any]] = []
    timeseries_data: List[Dict[str, Any]] = []
    privacy_mode = bool(enriched.get("privacy_mode", False))

    try:
        from peldrun.runtime import search_usage_store as sus_mod

        getter = getattr(sus_mod, "get_search_usage_store", None)
        if callable(getter):
            store = getter()

            # 1. Summary metrics
            summary_fn = getattr(store, "get_summary_metrics", None)
            if callable(summary_fn):
                try:
                    res = summary_fn(period=effective_period)
                    if inspect.isawaitable(res):
                        res = await res
                    if isinstance(res, dict):
                        summary_data.update(res)
                except Exception as exc:
                    logger.debug("summary metrics notice: %s", exc)

            # 2. Per-provider breakdown
            breakdown_fn = getattr(store, "get_provider_breakdown", None)
            if callable(breakdown_fn):
                try:
                    res = breakdown_fn(period=effective_period)
                    if inspect.isawaitable(res):
                        res = await res
                    if isinstance(res, list):
                        breakdown_data = res
                except Exception as exc:
                    logger.debug("provider breakdown notice: %s", exc)

            # 3. Time series
            timeseries_fn = getattr(store, "get_timeseries", None)
            if callable(timeseries_fn):
                try:
                    res = timeseries_fn(period=effective_period)
                    if inspect.isawaitable(res):
                        res = await res
                    if isinstance(res, list):
                        timeseries_data = res
                except Exception as exc:
                    logger.debug("timeseries notice: %s", exc)
    except Exception as exc:
        logger.debug("Search metrics store access notice: %s", exc)

    return {
        "status": "ok",
        "period": effective_period,
        "requested_period": period,
        "days": days,
        "summary": summary_data,
        "breakdown": breakdown_data,
        "timeseries": timeseries_data,
        "privacy_mode": privacy_mode,
        "default_max_results": enriched["default_max_results"],
        "max_results_hard_cap": MAX_RESULTS_HARD_CAP,
        "provider_capabilities": capabilities,
        "providers": _build_provider_status_list(enriched),
    }

@router.delete("/metrics")
async def clear_search_metrics() -> Dict[str, Any]:
    """
    Delete all persisted search usage records from the local SQLite ledger.
    Called by the frontend "Clear Ledger" button on the Search Telemetry page.
    """
    try:
        from peldrun.runtime.search_usage_store import get_search_usage_store

        store = get_search_usage_store()
        deleted = store.clear_ledger()
    except Exception as exc:
        logger.error("Failed to clear search usage ledger: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clear search usage ledger: {exc}",
        )

    WebSearchTool.reset_circuit_breakers()

    return {
        "ok": True,
        "status": "ok",
        "deleted_count": int(deleted or 0),
        "message": f"Cleared {int(deleted or 0)} search usage record(s) from the local ledger.",
    }

@router.post("/test")
@router.post("/test-query")
async def run_diagnostic_search_test(request: TestQueryRequest) -> Dict[str, Any]:
    """
    Execute a diagnostic search query and return a rich, frontend-ready result.

    Outcome classification respects response.is_success and response.status
    (never merely the presence of results). A provider that returns partial results
    alongside a failure status is reported as a failure, not as a success.

    Response includes:
      - status: 'ok' | 'empty' | 'error'
      - ok / success: bool
      - provider: resolved provider display name
      - results: list of {title, url, snippet, ...}
      - request_count, estimated_cost_usd, cost_basis, latency_ms
      - effective_max_results, max_results_limit
      - usage_unknown: True when request/cost accounting could not be determined
      - error_message: present when status != 'ok'
    """
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
    except SearchProviderError as exc:
        latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        usage_evidence = _extract_exception_usage_evidence(exc)
        _record_search_telemetry(
            query=request.query,
            provider=pre_audit["provider"],
            results_count=0,
            latency_ms=latency_ms,
            cost_usd=float(usage_evidence["estimated_cost_usd"]),
            is_success=False,
            status="error",
            error_message=str(exc),
            request_count=int(usage_evidence["request_count"]),
            cost_basis=usage_evidence["cost_basis"],
            privacy_mode=bool(active_search_cfg.get("privacy_mode", False)),
        )
        return {
            "status": "error",
            "ok": False,
            "success": False,
            "error": str(exc),
            "error_message": str(exc),
            "provider": pre_audit["provider"],
            "query": request.query,
            "requested_max_results": request.max_results,
            "effective_max_results": pre_audit["effective_max_results"],
            "max_results_limit": pre_audit["max_results_limit"],
            "results_count": 0,
            "results": [],
            "request_count": usage_evidence["request_count"],
            "usage_unknown": usage_evidence["usage_unknown"],
            "latency_ms": latency_ms,
            "estimated_cost_usd": usage_evidence["estimated_cost_usd"],
            "cost_usd": usage_evidence["estimated_cost_usd"],
            "cost_basis": usage_evidence["cost_basis"],
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        err_msg = str(exc) or type(exc).__name__
        usage_evidence = _extract_exception_usage_evidence(exc)
        _record_search_telemetry(
            query=request.query,
            provider=pre_audit["provider"],
            results_count=0,
            latency_ms=latency_ms,
            cost_usd=float(usage_evidence["estimated_cost_usd"]),
            is_success=False,
            status="error",
            error_message=err_msg,
            request_count=int(usage_evidence["request_count"]),
            cost_basis=usage_evidence["cost_basis"],
            privacy_mode=bool(active_search_cfg.get("privacy_mode", False)),
        )
        return {
            "status": "error",
            "ok": False,
            "success": False,
            "error": err_msg,
            "error_message": err_msg,
            "provider": pre_audit["provider"],
            "query": request.query,
            "requested_max_results": request.max_results,
            "effective_max_results": pre_audit["effective_max_results"],
            "max_results_limit": pre_audit["max_results_limit"],
            "results_count": 0,
            "results": [],
            "request_count": usage_evidence["request_count"],
            "usage_unknown": usage_evidence["usage_unknown"],
            "latency_ms": latency_ms,
            "estimated_cost_usd": usage_evidence["estimated_cost_usd"],
            "cost_usd": usage_evidence["estimated_cost_usd"],
            "cost_basis": usage_evidence["cost_basis"],
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

    # -------------------------------------------------------------------
    # Authoritative outcome classification — respects is_success and status,
    # NOT merely the presence of results.
    # -------------------------------------------------------------------
    outcome_class, provider_status_value, _reported_count = _classify_provider_response(response)

    is_ok = outcome_class == "success"
    is_empty = outcome_class == "empty"
    is_failure = outcome_class == "failure"

    # Extract rich response attributes with safe fallbacks; preserve 0 when
    # the provider explicitly reported 0 requests.
    raw_request_count = getattr(response, "request_count", None)
    if isinstance(raw_request_count, (int, float)) and not isinstance(raw_request_count, bool):
        response_request_count = int(raw_request_count)
    else:
        response_request_count = 0

    response_cost_usd = float(
        getattr(response, "estimated_cost_usd", None)
        if getattr(response, "estimated_cost_usd", None) is not None
        else (resp_meta.get("estimated_cost_usd") if resp_meta.get("estimated_cost_usd") is not None else 0.0)
    )
    response_cost_basis = str(
        getattr(response, "cost_basis", None)
        or resp_meta.get("cost_basis", "unknown")
        or "unknown"
    )

    error_message: Optional[str] = None
    if is_failure:
        error_message = (
            getattr(response, "error_message", None)
            or f"Provider '{resolved_provider_name}' returned failure status '{provider_status_value}'."
        )
    elif is_empty:
        error_message = (
            getattr(response, "error_message", None)
            or "Provider responded with HTTP 200 but returned zero matching documents."
        )

    # When a failure still carries partial results we surface them, but status
    # remains "error" — the tool has explicitly declared the underlying call failed.
    final_status = "ok" if is_ok else ("empty" if is_empty else "error")

    # Record the telemetry fact so /metrics and the Search Intelligence
    # page can show this test alongside agent-driven searches. The
    # helper is a no-op on store failure so a persistence hiccup cannot
    # break the diagnostic response the user is waiting for.
    _record_search_telemetry(
        query=request.query,
        provider=resolved_provider_name,
        results_count=len(serialized),
        latency_ms=float(getattr(response, "latency_ms", None) or latency_ms),
        cost_usd=response_cost_usd,
        is_success=is_ok,
        status=provider_status_value or final_status,
        error_message=error_message,
        request_count=response_request_count,
        cost_basis=response_cost_basis,
        privacy_mode=bool(active_search_cfg.get("privacy_mode", False)),
    )

    return {
        "status": final_status,
        "ok": is_ok,
        "success": is_ok,
        "provider": resolved_provider_name,
        "provider_status": provider_status_value or "unknown",
        "query": request.query,
        "requested_max_results": request.max_results,
        "effective_max_results": effective_max,
        "max_results_limit": post_audit["max_results_limit"],
        "results_count": len(serialized),
        "results": serialized,
        "request_count": response_request_count,
        "usage_unknown": False,
        "latency_ms": getattr(response, "latency_ms", None) or latency_ms,
        "estimated_cost_usd": response_cost_usd,
        "cost_usd": response_cost_usd,
        "cost_basis": response_cost_basis,
        "error_message": error_message,
    }


@router.post("/test-connection")
async def test_provider_connection(request: TestConnectionRequest) -> Dict[str, Any]:
    """
    Execute a structured diagnostic check against a specific provider and return a
    frontend-ready ProviderDiagnosticResult payload.

    This endpoint is separate from /test: /test runs a live search and returns
    search results, whereas /test-connection exercises the provider's own
    `diagnose()` contract (reachability / credentials / live-search) without
    attempting to interpret search results.

    Response includes:
      - status: 'ok' | 'error'
      - ok: bool
      - provider: resolved provider display name
      - check_type: 'reachability' | 'credentials' | 'live_search'
      - reachable / authenticated / quota_available / search_operational
      - consumes_quota, latency_ms, request_count, estimated_cost_usd, cost_basis
      - message: human-readable diagnostic summary
    """
    full_cfg = _load_full_config_dict()
    persisted_search = full_cfg.get("search") if isinstance(full_cfg.get("search"), dict) else {}
    enriched_persisted = _enrich_search_config_with_defaults(persisted_search)

    snapshot = request.config if request.config is not None else request.search_config
    if snapshot is not None:
        active_search_cfg = _validate_and_normalize_search_config(snapshot, existing_search=enriched_persisted)
    else:
        active_search_cfg = enriched_persisted

    target_provider_raw = request.provider or request.engine
    if target_provider_raw:
        try:
            target_key = SearchProviderFactory.normalize_provider_key(target_provider_raw)
        except ValueError as exc:
            raise SearchConfigValidationError(str(exc)) from exc
    else:
        target_key = SearchProviderFactory.normalize_provider_key(
            str(active_search_cfg.get("engine") or "DuckDuckGo")
        )

    display_name = SearchProviderFactory.get_canonical_display_name(target_key)

    # Normalize the requested check_type against the DiagnosticCheckType enum.
    raw_check = (request.check_type or "credentials").strip().lower()
    check_type_map = {
        "reachability": DiagnosticCheckType.REACHABILITY,
        "credentials": DiagnosticCheckType.CREDENTIALS,
        "auth": DiagnosticCheckType.CREDENTIALS,
        "live_search": DiagnosticCheckType.LIVE_SEARCH,
        "live": DiagnosticCheckType.LIVE_SEARCH,
        "search": DiagnosticCheckType.LIVE_SEARCH,
    }
    check_type_enum = check_type_map.get(raw_check, DiagnosticCheckType.CREDENTIALS)

    try:
        provider = SearchProviderFactory.create_provider(target_key, active_search_cfg)
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        return {
            "status": "error",
            "ok": False,
            "success": False,
            "provider": display_name,
            "provider_key": target_key,
            "check_type": check_type_enum.value,
            "reachable": False,
            "authenticated": None,
            "quota_available": None,
            "search_operational": None,
            "consumes_quota": False,
            "latency_ms": 0.0,
            "request_count": 0,
            "usage_unknown": True,
            "estimated_cost_usd": 0.0,
            "cost_usd": 0.0,
            "cost_basis": "unknown",
            "message": f"Failed to instantiate provider '{display_name}': {err_msg}",
            "error_message": err_msg,
        }

    try:
        diag = await provider.diagnose(check_type=check_type_enum)
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        usage_evidence = _extract_exception_usage_evidence(exc)
        return {
            "status": "error",
            "ok": False,
            "success": False,
            "provider": display_name,
            "provider_key": target_key,
            "check_type": check_type_enum.value,
            "reachable": False,
            "authenticated": None,
            "quota_available": None,
            "search_operational": None,
            "consumes_quota": False,
            "latency_ms": 0.0,
            "request_count": usage_evidence["request_count"],
            "usage_unknown": usage_evidence["usage_unknown"],
            "estimated_cost_usd": usage_evidence["estimated_cost_usd"],
            "cost_usd": usage_evidence["estimated_cost_usd"],
            "cost_basis": usage_evidence["cost_basis"],
            "message": f"Diagnostic check raised: {err_msg}",
            "error_message": err_msg,
        }

    # Serialize the ProviderDiagnosticResult.
    if hasattr(diag, "model_dump"):
        diag_dict = diag.model_dump()
    elif hasattr(diag, "dict"):
        diag_dict = diag.dict()
    else:
        diag_dict = dict(vars(diag))

    status_val = diag_dict.get("status")
    if hasattr(status_val, "value"):
        status_val = status_val.value
    status_str = str(status_val or "unknown")

    cost_basis_val = diag_dict.get("cost_basis")
    if hasattr(cost_basis_val, "value"):
        cost_basis_val = cost_basis_val.value
    cost_basis_str = str(cost_basis_val or "unknown")

    check_type_val = diag_dict.get("check_type")
    if hasattr(check_type_val, "value"):
        check_type_val = check_type_val.value

    ok_flag = bool(diag_dict.get("ok", False))

    return {
        "status": "ok" if ok_flag else "error",
        "ok": ok_flag,
        "success": ok_flag,
        "provider": str(diag_dict.get("provider") or display_name),
        "provider_key": target_key,
        "check_type": str(check_type_val or check_type_enum.value),
        "provider_status": status_str,
        "reachable": bool(diag_dict.get("reachable", False)),
        "authenticated": diag_dict.get("authenticated"),
        "quota_available": diag_dict.get("quota_available"),
        "search_operational": diag_dict.get("search_operational"),
        "consumes_quota": bool(diag_dict.get("consumes_quota", False)),
        "latency_ms": float(diag_dict.get("latency_ms") or 0.0),
        "request_count": int(diag_dict.get("request_count") or 0),
        "usage_unknown": False,
        "usage_units": diag_dict.get("usage_units"),
        "estimated_cost_usd": float(diag_dict.get("estimated_cost_usd") or 0.0),
        "cost_usd": float(diag_dict.get("estimated_cost_usd") or 0.0),
        "cost_basis": cost_basis_str,
        "message": str(diag_dict.get("message") or ""),
        "error_message": None if ok_flag else str(diag_dict.get("message") or ""),
    }


__all__ = [
    "SearchConfigValidationError",
    "TestQueryRequest",
    "TestConnectionRequest",
    "_enrich_search_config_with_defaults",
    "_validate_and_normalize_search_config",
    "router",
]
