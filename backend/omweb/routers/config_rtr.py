"""
backend/omweb/routers/config_rtr.py

PELDRUN Web Configuration Router.

Manages application configuration, secret masking, LLM provider testing,
and models metadata persistence. Uses one authoritative configuration path
shared with the core runtime and surfaces TOML parse errors diagnostically.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException
import httpx
from omweb.config import BACKEND_DIR, PROJECT_ROOT, STORAGE_ROOT
from pydantic import BaseModel
import toml

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger("omweb.routers.config_rtr")

router = APIRouter()

CONFIG_PATH = (PROJECT_ROOT / "config" / "config.toml").resolve()
CONFIG_EXAMPLE_PATH = (PROJECT_ROOT / "config" / "config.example.toml").resolve()
BACKUP_PATH = (PROJECT_ROOT / "config" / "config.toml.bak").resolve()
METADATA_CACHE = STORAGE_ROOT / "models_metadata.json"
BUDGET_CACHE = STORAGE_ROOT / "models_budget.json"

SENSITIVE_KEY_TOKENS = ("key", "password", "secret", "token")
MASK_TOKENS = ("••••", "****", "***")

_LAST_CONFIG_ERROR: Optional[str] = None


def get_authoritative_config_path() -> Path:
    """
    Return the canonical config.toml path used by both web routes and runtime.
    Honors PELDRUN_CONFIG_PATH when set (e.g., in isolated tests).
    """
    env_override = os.environ.get("PELDRUN_CONFIG_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    return CONFIG_PATH


def get_last_config_error() -> Optional[str]:
    """Return the most recent TOML parse/read error, or None if valid."""
    return _LAST_CONFIG_ERROR


def _resolve_readable_target(explicit_path: Optional[Path] = None) -> Tuple[Optional[Path], bool]:
    """
    Locate the configuration file to read.
    Returns (path, is_fallback_example).
    """
    primary = (explicit_path or get_authoritative_config_path()).resolve()
    if primary.is_file():
        return primary, False
    if explicit_path is None and CONFIG_EXAMPLE_PATH.is_file():
        logger.info(
            "Primary config.toml not found; falling back to config.example.toml."
        )
        return CONFIG_EXAMPLE_PATH, True
    return None, False


def read_raw_config_strict(explicit_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Read and parse the authoritative TOML configuration strictly.
    Raises ValueError with diagnostic details if the TOML file is malformed.
    """
    global _LAST_CONFIG_ERROR
    target, _ = _resolve_readable_target(explicit_path=explicit_path)
    if target is None:
        _LAST_CONFIG_ERROR = None
        return {}

    try:
        content_bytes = target.read_bytes()
        if content_bytes.startswith(b"\xef\xbb\xbf"):
            content_bytes = content_bytes[3:]
        parsed = tomllib.loads(content_bytes.decode("utf-8"))
        _LAST_CONFIG_ERROR = None
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        err_msg = f"Invalid TOML in {target.name}: {exc}"
        _LAST_CONFIG_ERROR = err_msg
        logger.error(err_msg)
        raise ValueError(err_msg) from exc


def read_raw_config(explicit_path: Optional[Path] = None) -> dict:
    """
    Read raw TOML configuration with automatic UTF-8 BOM stripping.
    Records diagnostic state in _LAST_CONFIG_ERROR if parsing fails.
    """
    try:
        return read_raw_config_strict(explicit_path=explicit_path)
    except ValueError:
        return {}


def write_raw_config(data: dict, explicit_path: Optional[Path] = None) -> None:
    """Write central configuration atomically with automatic backup."""
    global _LAST_CONFIG_ERROR
    target = (explicit_path or get_authoritative_config_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        backup = target.with_suffix(".toml.bak")
        try:
            shutil.copy2(target, backup)
        except Exception as exc:
            logger.debug(f"Failed to create config backup at {backup}: {exc}")

    clean_data: Dict[str, Any] = {}
    for k, v in data.items():
        if v is not None:
            clean_data[k] = v

    tmp_path = target.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        toml.dump(clean_data, handle)

    # Verify that the generated TOML is valid before replacing the target file.
    raw_written = tmp_path.read_text(encoding="utf-8")
    tomllib.loads(raw_written)
    tmp_path.replace(target)
    _LAST_CONFIG_ERROR = None


def _is_secret_field(key: str) -> bool:
    key_lower = key.lower()
    return any(token in key_lower for token in SENSITIVE_KEY_TOKENS)


def is_masked_secret_value(value: Any) -> bool:
    """Return True if the string represents a UI-masked secret placeholder."""
    if not isinstance(value, str):
        return False
    return any(mask in value for mask in MASK_TOKENS)


def mask_secrets(data: dict) -> dict:
    """Recursively mask sensitive API keys and tokens in configuration dictionaries."""
    masked: Dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            masked[key] = mask_secrets(val)
        elif isinstance(key, str) and _is_secret_field(key):
            if isinstance(val, str) and len(val) > 8:
                masked[key] = val[:4] + "••••••••" + val[-4:]
            elif isinstance(val, str) and val:
                masked[key] = "••••••••"
            else:
                masked[key] = val
        else:
            masked[key] = val
    return masked


def merge_unmasked(
    new_dict: dict,
    old_dict: dict,
    preserve_on_empty: bool = False,
) -> dict:
    """
    Merge newly submitted config with existing config:
    - Preserves non-submitted keys in old_dict.
    - Preserves existing secret values when a masked placeholder ('••••') is supplied.
    - Allows intentional secret clearing when an empty string '' is submitted
      (unless preserve_on_empty=True is explicitly requested for read-only probes).
    """
    result: Dict[str, Any] = dict(old_dict) if isinstance(old_dict, dict) else {}
    for k, v in new_dict.items():
        old_val = result.get(k)
        if isinstance(v, dict) and isinstance(old_val, dict):
            result[k] = merge_unmasked(
                v,
                old_val,
                preserve_on_empty=preserve_on_empty,
            )
        elif isinstance(k, str) and _is_secret_field(k) and isinstance(v, str):
            stripped = v.strip()
            if is_masked_secret_value(stripped):
                result[k] = old_val if old_val is not None else ""
            elif not stripped and preserve_on_empty and old_val:
                result[k] = old_val
            else:
                result[k] = stripped
        else:
            result[k] = v
    return result


def save_metadata_cache(new_meta: dict) -> None:
    """Persist models metadata cache to disk."""
    data = {}
    if METADATA_CACHE.exists():
        try:
            with open(METADATA_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data.update(new_meta)
    try:
        METADATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(METADATA_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Error saving metadata cache: {e}")


@router.get("")
@router.get("/")
async def get_config():
    """Retrieve active system configuration with masked credentials and budget limits."""
    raw = read_raw_config()
    custom_models = []
    if "llm" in raw and isinstance(raw["llm"], dict):
        for sub_key, sub_val in raw["llm"].items():
            if sub_key != "vision" and isinstance(sub_val, dict):
                custom_models.append({
                    "id": f"custom_{sub_key}",
                    "name": sub_key,
                    "model": sub_val.get("model", ""),
                    "base_url": sub_val.get("base_url", ""),
                    "api_key": sub_val.get("api_key", ""),
                    "max_tokens": sub_val.get("max_tokens", 8192),
                    "context_window": sub_val.get("context_window", sub_val.get("max_tokens", 8192)),
                    "max_output_tokens": sub_val.get("max_output_tokens", 1500),
                    "temperature": sub_val.get("temperature", 0.0),
                    "api_type": sub_val.get("api_type", ""),
                })

    return {
        "config": mask_secrets(raw),
        "custom_models": mask_secrets({"models": custom_models}).get("models", []),
        "raw_path": str(get_authoritative_config_path()),
        "config_error": get_last_config_error(),
    }


@router.post("")
@router.post("/")
@router.put("")
@router.put("/")
async def save_config(payload: Dict[str, Any]):
    """Update and persist system configuration."""
    current = read_raw_config()
    merged = merge_unmasked(payload, current, preserve_on_empty=False)
    write_raw_config(merged)
    return {
        "status": "saved",
        "config": mask_secrets(merged),
        "raw_path": str(get_authoritative_config_path()),
    }


@router.get("/models-metadata")
async def get_models_metadata():
    """Retrieve cached metadata for available models."""
    if METADATA_CACHE.exists():
        try:
            with open(METADATA_CACHE, "r", encoding="utf-8") as f:
                return {"ok": True, "metadata": json.load(f)}
        except Exception as e:
            return {"ok": False, "error": str(e), "metadata": {}}
    return {"ok": True, "metadata": {}}


class UpdateMetadataRequest(BaseModel):
    metadata: Dict[str, Any]


@router.post("/models-metadata")
async def update_models_metadata(payload: UpdateMetadataRequest):
    """Update models metadata cache."""
    try:
        save_metadata_cache(payload.metadata)
        return {"ok": True, "message": "Metadata persisted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class TestLLMRequest(BaseModel):
    base_url: str
    api_key: str
    model: str
    api_type: Optional[str] = ""


@router.post("/test-llm")
async def test_llm_connection(payload: TestLLMRequest):
    """Test LLM endpoint connectivity and measure latency."""
    api_key = payload.api_key.strip()
    if not api_key or is_masked_secret_value(api_key):
        current = read_raw_config()
        api_key = current.get("llm", {}).get("api_key", "")

    base = payload.base_url.strip().rstrip("/")
    p_type = (payload.api_type or "").lower()
    headers = {"Content-Type": "application/json"}

    if "anthropic.com" in base or "anthropic" in p_type:
        test_url = "https://api.anthropic.com/v1/models"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif "googleapis.com" in base or "gemini" in p_type:
        test_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        headers["x-goog-api-key"] = api_key
    else:
        test_url = base if base.endswith("/models") else f"{base}/models"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if "openrouter.ai" in base:
            headers["HTTP-Referer"] = "https://github.com/peldrun/peldrun"
            headers["X-Title"] = "PELDRUN"

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(test_url, headers=headers)
            elapsed_ms = round((time.time() - start_time) * 1000)
            return {
                "ok": resp.status_code in (200, 201),
                "status_code": resp.status_code,
                "latency_ms": elapsed_ms,
                "message": f"Responded with HTTP {resp.status_code} in {elapsed_ms}ms",
            }
    except Exception as e:
        return {"ok": False, "status_code": 0, "latency_ms": 0, "message": str(e)}


class FetchModelsRequest(BaseModel):
    base_url: str
    api_key: Optional[str] = ""
    provider_type: Optional[str] = ""
    provider_id: Optional[str] = ""
    # Optional explicit metadata endpoint supplied by the frontend from the
    # vault's `metadataEndpoint` field. When present, it takes priority over
    # `<base_url>/models`. This lets providers whose `/models` route returns
    # only IDs (LM Studio, Google) point to their richer endpoint without
    # hardcoded branches here.
    metadata_endpoint: Optional[str] = ""


@router.post("/fetch-models")
async def fetch_available_models(payload: FetchModelsRequest):
    """Query endpoint for dynamically discovered model identifiers and metadata."""
    api_key = (payload.api_key or "").strip()
    if is_masked_secret_value(api_key):
        current = read_raw_config()
        api_key = current.get("llm", {}).get("api_key", "")

    base = payload.base_url.strip().rstrip("/")
    p_id = (payload.provider_id or "").lower()
    p_type = (payload.provider_type or "").lower()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # ------------------------------------------------------------------
    # Helper: parse a list of model dicts and extract context metadata.
    # Shared by all branches to keep parsing consistent.
    # ------------------------------------------------------------------
    def _parse_model_list(raw_list: list) -> tuple:
        ids: list = []
        meta: dict = {}
        for m in raw_list:
            if not isinstance(m, dict):
                continue
            mid = m.get("id") or m.get("name") or m.get("key")
            if not mid:
                continue
            mid_str = str(mid)
            ids.append(mid_str)

            # Also index the short form for "models/xxx" style IDs.
            if "/" in mid_str:
                short = mid_str.rsplit("/", 1)[-1]
                if short and short != mid_str:
                    ids.append(short)

            ctx = (
                m.get("max_context_length")
                or m.get("context_length")
                or m.get("context_window")
                or m.get("inputTokenLimit")
                or m.get("max_tokens")
            )
            out = (
                m.get("max_output_tokens")
                or m.get("max_completion_tokens")
                or m.get("outputTokenLimit")
            )
            if ctx is not None or out is not None:
                entry = {
                    "key": mid_str,
                    "type": m.get("type", "llm"),
                    "display_name": m.get("display_name")
                    or m.get("displayName")
                    or mid_str,
                    "max_context_length": int(ctx) if ctx else None,
                    "max_output_tokens": int(out) if out else None,
                }
                meta[mid_str] = entry
                if "/" in mid_str:
                    short = mid_str.rsplit("/", 1)[-1]
                    if short and short not in meta:
                        meta[short] = {**entry, "key": short}
        return sorted(set(ids)), meta

    # ------------------------------------------------------------------
    # Branch 1: Explicit metadata endpoint from the vault (highest priority).
    # Zero hardcoded provider logic.
    # ------------------------------------------------------------------
    explicit_meta = (payload.metadata_endpoint or "").strip()
    if explicit_meta:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(explicit_meta, headers=headers)
                if resp.status_code != 200:
                    return {
                        "ok": False,
                        "error": f"HTTP {resp.status_code} from metadata endpoint",
                        "models": [],
                        "models_metadata": {},
                    }
                data = resp.json()
                raw_list = data.get("data") or data.get("models") or []
                model_ids, metadata = _parse_model_list(raw_list)
                if metadata:
                    save_metadata_cache(metadata)
                return {
                    "ok": True,
                    "models": model_ids,
                    "models_metadata": metadata,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Metadata endpoint failed: {exc}",
                "models": [],
                "models_metadata": {},
            }

    # ------------------------------------------------------------------
    # Branch 2: LM Studio fallback when no explicit endpoint is set.
    # Tries /api/v1/models, /api/v0/models, then <base>/models.
    # This is best-effort only; the preferred path is Branch 1.
    # ------------------------------------------------------------------
    if "lmstudio" in p_id or "lmstudio" in p_type or "1234" in base:
        clean_base = base.replace("/v1", "").rstrip("/")
        for ep in (
            f"{clean_base}/api/v1/models",
            f"{clean_base}/api/v0/models",
            f"{base}/models" if not base.endswith("/models") else base,
        ):
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    resp = await client.get(ep, headers=headers)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    raw_models = data.get("models") or data.get("data") or []
                    if not raw_models:
                        continue
                    model_ids, metadata = _parse_model_list(raw_models)
                    if metadata:
                        save_metadata_cache(metadata)
                    return {
                        "ok": True,
                        "models": model_ids,
                        "models_metadata": metadata,
                    }
            except Exception:
                continue
        # If all LM Studio endpoints failed, fall through to Branch 3.

    # ------------------------------------------------------------------
    # Branch 3: Generic `<base>/models` — works for DeepSeek, Anthropic,
    # OpenAI-compat servers. Providers that don't return context there
    # simply yield empty `models_metadata`; the UI reports this honestly.
    # ------------------------------------------------------------------
    url = base if base.endswith("/models") else f"{base}/models"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status_code}",
                    "models": [],
                    "models_metadata": {},
                }
            data = resp.json()
            raw_list = data.get("data", []) or data.get("models", [])
            model_ids, metadata = _parse_model_list(raw_list)
            if metadata:
                save_metadata_cache(metadata)
            return {
                "ok": True,
                "models": model_ids,
                "models_metadata": metadata,
            }
    except Exception as e:
        return {
            "ok": False,
            "error": f"Failed: {str(e)}",
            "models": [],
            "models_metadata": {},
        }

    # ------------------------------------------------------------------
    # Query the endpoint EXACTLY as configured in the vault. No provider
    # branching, no URL rewriting, no field-name guessing per provider.
    #
    # The vault's `baseUrl` is the single source of truth. If a provider
    # doesn't return context info from `<baseUrl>/models`, that is the
    # provider's limitation — the caller (UI) reports it honestly.
    # ------------------------------------------------------------------
    url = base if base.endswith("/models") else f"{base}/models"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status_code}",
                    "models": [],
                    "models_metadata": {},
                }

            data = resp.json()
            raw_list = data.get("data", []) or data.get("models", [])

            model_ids: list = []
            metadata: dict = {}

            for m in raw_list:
                if not isinstance(m, dict):
                    continue

                mid = m.get("id") or m.get("name")
                if not mid:
                    continue
                mid_str = str(mid)
                model_ids.append(mid_str)

                # If the model id contains a slash (Google's "models/xxx"
                # format), also index it by the short name so lookups by
                # either form succeed.
                if "/" in mid_str:
                    short = mid_str.rsplit("/", 1)[-1]
                    if short and short != mid_str:
                        model_ids.append(short)

                # Extract context window from any field present. The
                # caller decides what to do if none exists — we do not
                # fabricate values.
                ctx = (
                    m.get("max_context_length")
                    or m.get("context_length")
                    or m.get("context_window")
                    or m.get("inputTokenLimit")
                    or m.get("max_tokens")
                )
                out = (
                    m.get("max_output_tokens")
                    or m.get("max_completion_tokens")
                    or m.get("outputTokenLimit")
                )

                if ctx is not None or out is not None:
                    entry = {
                        "key": mid_str,
                        "type": m.get("type", "llm"),
                        "display_name": m.get("display_name")
                        or m.get("displayName")
                        or mid_str,
                        "max_context_length": int(ctx) if ctx else None,
                        "max_output_tokens": int(out) if out else None,
                    }
                    metadata[mid_str] = entry
                    if "/" in mid_str:
                        short = mid_str.rsplit("/", 1)[-1]
                        if short and short not in metadata:
                            metadata[short] = {**entry, "key": short}

            if metadata:
                save_metadata_cache(metadata)

            return {
                "ok": True,
                "models": sorted(list(set(model_ids))),
                "models_metadata": metadata,
            }
    except Exception as e:
        return {
            "ok": False,
            "error": f"Failed: {str(e)}",
            "models": [],
            "models_metadata": {},
        }


# ============================================================
# File-Based Persistent Models Vault (No Browser Dependence)
# ============================================================
VAULT_DIR = BACKEND_DIR / "data"
VAULT_FILE = VAULT_DIR / "models_vault.json"

DEFAULT_VAULT = {
    "cloud_vault": [],
    "custom_endpoints": [],
    "lmstudio_vault": {
        "baseUrl": "http://127.0.0.1:1234/v1",
        "model": "qwen3-vl-8b-instruct",
        "apiKey": "",
        "savedModels": ["qwen3-vl-8b-instruct"],
    },
    "ollama_vault": {
        "baseUrl": "http://127.0.0.1:11434/v1",
        "model": "",
        "apiKey": "",
        "savedModels": [],
    },
    "scanned_models": [],
}


def load_vault_from_disk() -> dict:
    """Load persisted models vault from disk."""
    try:
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        if not VAULT_FILE.exists():
            return dict(DEFAULT_VAULT)
        with open(VAULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in DEFAULT_VAULT.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as e:
        logger.warning(f"[VAULT] Error reading vault file: {e}")
        return dict(DEFAULT_VAULT)


def save_vault_to_disk(vault_data: dict) -> None:
    """Persist models vault safely using temporary file replacement."""
    try:
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = VAULT_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(vault_data, f, indent=2, ensure_ascii=False)
        temp_file.replace(VAULT_FILE)
    except Exception as e:
        logger.warning(f"[VAULT] Error saving vault file: {e}")


@router.get("/vault")
async def get_models_vault():
    """Retrieve all saved AI providers, models, and API keys from persistent storage."""
    return load_vault_from_disk()


@router.post("/vault")
async def save_models_vault(payload: dict = Body(...)):
    """Save or update AI providers, models, and API keys in persistent storage."""
    current = load_vault_from_disk()
    for key in ["cloud_vault", "custom_endpoints", "lmstudio_vault", "ollama_vault", "scanned_models"]:
        if key in payload:
            current[key] = payload[key]
    save_vault_to_disk(current)
    return {"status": "success", "vault": current}


# ============================================================
# Per-Model Context Window & Token Budget Management
# ============================================================
def load_model_budgets_from_disk() -> dict:
    """Read saved per-model token budget configurations from disk."""
    if BUDGET_CACHE.exists():
        try:
            with open(BUDGET_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[BUDGET] Error loading budget cache: {e}")
    return {}


def save_model_budgets_to_disk(budgets_data: dict) -> None:
    """Safely persist per-model token budget configurations."""
    try:
        BUDGET_CACHE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = BUDGET_CACHE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(budgets_data, f, indent=2, ensure_ascii=False)
        temp_file.replace(BUDGET_CACHE)
    except Exception as e:
        logger.warning(f"[BUDGET] Error writing budget cache: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/model-budgets")
async def get_model_budgets():
    """Retrieve all per-model context windows and output limits."""
    return {"ok": True, "budgets": load_model_budgets_from_disk()}


class SaveModelBudgetsRequest(BaseModel):
    budgets: Dict[str, Dict[str, Any]]


@router.post("/model-budgets")
async def save_model_budgets(payload: SaveModelBudgetsRequest):
    """Save or update per-model context windows and token budgets."""
    current = load_model_budgets_from_disk()
    current.update(payload.budgets)
    save_model_budgets_to_disk(current)
    return {"ok": True, "message": "Model budgets saved successfully", "budgets": current}
