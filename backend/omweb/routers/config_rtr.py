"""
backend/omweb/routers/config_rtr.py

PELDRUN Web Configuration Router.

Manages application configuration, secret masking, LLM provider testing,
and models metadata persistence. Uses one authoritative configuration path
shared with the core runtime and surfaces TOML parse errors diagnostically.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException
import httpx
from omweb.config import BACKEND_DIR, PROJECT_ROOT, STORAGE_ROOT
from pydantic import BaseModel
import toml

# Live-provider capability query — Ollama /api/show, LM Studio
# /api/v0/models, Google per-model endpoint. Used by /fetch-models to
# enrich the model list before persisting.
from peldrun.llm.catalog.provider_live import query_providers_batch
from peldrun.llm.contracts import ConnectionContext
# M11: adapter-based model discovery.
from peldrun.llm.providers import (
    ensure_external_providers_discovered,
    provider_factory,
)

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
    """
    Persist models metadata cache to disk (DESTRUCTIVE merge).

    Used by POST /api/config/models-metadata — the frontend's manual
    save path. The frontend sends complete entries that already
    include `capability_sources = "manual"`, so a straight update is
    correct here.

    For the Scan Models flow, use `save_metadata_cache_merge` instead
    so that manual overrides survive a rescan.
    """
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


# ---------------------------------------------------------------------------
# Merge-aware persistence (used by /fetch-models)
# ---------------------------------------------------------------------------

_CAPABILITY_FIELDS = ("vision", "trained_for_tool_use", "reasoning")

_SCALAR_FIELDS = (
    "max_context_length",
    "max_output_tokens",
    "params_string",
    "publisher",
    "architecture",
    "family",
    "display_name",
    "type",
)


def _merge_metadata_entries(existing: dict, new: dict) -> dict:
    """
    Merge a freshly scanned metadata entry into an existing one,
    preserving any user-authored manual overrides.

    Rules
    -----
    * Scalar fields (context, params, quantization, arch, publisher,
      family, display_name, type): new value wins if present.
    * Capability booleans (vision, trained_for_tool_use, reasoning):
        - If the existing entry has ``capability_sources[field] ==
          "manual"``, the manual value is preserved unconditionally.
        - Otherwise, the new value wins when it is declared by a
          source (``provider_live``, ``provider_catalog``, etc.).
    * ``capability_details.reasoning``: preserved when the existing
      reasoning source is ``manual``; otherwise replaced by the new
      details block.
    """
    if not isinstance(existing, dict):
        return dict(new) if isinstance(new, dict) else {}
    if not isinstance(new, dict):
        return dict(existing)

    result = dict(existing)

    # Scalar fields: new wins if present and not None.
    for key in _SCALAR_FIELDS:
        if key in new and new[key] is not None:
            result[key] = new[key]

    if isinstance(new.get("quantization"), dict):
        result["quantization"] = new["quantization"]

    # Defensive extraction: a hand-edited or legacy entry may store
    # ``capabilities`` as a list (e.g. LM Studio's raw ["tool_use"])
    # instead of the dict of booleans our parser produces. dict(list)
    # would raise ValueError and propagate to Starlette as plain-text
    # "Internal Server Error" — exactly the failure mode observed
    # when scanning Gemini/Google.
    def _safe_dict(val: Any) -> Dict[str, Any]:
        return dict(val) if isinstance(val, dict) else {}

    existing_caps = _safe_dict(result.get("capabilities"))
    new_caps = _safe_dict(new.get("capabilities"))
    existing_sources = _safe_dict(result.get("capability_sources"))
    new_sources = _safe_dict(new.get("capability_sources"))

    for field in _CAPABILITY_FIELDS:
        if field not in new_caps:
            continue
        if existing_sources.get(field) == "manual":
            # Preserve user override; do not touch value or source.
            continue
        existing_caps[field] = new_caps[field]
        if field in new_sources:
            existing_sources[field] = new_sources[field]

    result["capabilities"] = existing_caps
    result["capability_sources"] = existing_sources

    # capability_details: same manual-preservation rule.
    existing_details = dict(result.get("capability_details") or {})
    new_details = dict(new.get("capability_details") or {})

    if "reasoning" in new_details:
        if existing_sources.get("reasoning") != "manual":
            existing_details["reasoning"] = new_details["reasoning"]

    result["capability_details"] = existing_details

    return result


# ---------------------------------------------------------------------------
# [M11] Adapter-based model discovery
# ---------------------------------------------------------------------------

#: Mapping from user-facing provider_id (or provider_type) to the
#: canonical native adapter ID that owns discovery for it.
#:
#: Only providers that have a REGISTERED native adapter are mapped.
#: Custom endpoints, DeepSeek, Mistral, Groq, OpenRouter, etc. are
#: NOT in this map — they fall through to the legacy HTTP branches,
#: which already work for OpenAI-compatible endpoints.
#:
#: "openai", "openai_compat", and "default" are deliberately NOT
#: mapped: users often set one of them while pointing at a non-OpenAI
#: endpoint (LM Studio proxy, DeepSeek, custom server). Mapping them
#: to the native OpenAI SDK would change behavior for those users
#: without their knowledge.
# ---------------------------------------------------------------------------
# Provider base-URL normalization
# ---------------------------------------------------------------------------
#
# Every local provider exposes multiple endpoint surfaces under one
# server root:
#
#   LM Studio:   /v1/*        (OpenAI-compat shim)
#                /api/v1/*    (native v1 - recent versions)
#                /api/v0/*    (native v0 - older versions)
#
#   Ollama:      /v1/*        (OpenAI-compat shim)
#                /api/*       (native)
#
# Users routinely paste the base URL with one of these suffixes already
# attached. Any code that then constructs `<base>/<another-suffix>`
# must strip the pre-existing suffix first, or it produces a double
# path like `/api/api/v0/models` - the exact failure observed in the
# LM Studio server log when `baseUrl` was set to
# `http://host:1235/api/v1/`.
#
# The previous implementation used `.replace("/v1", "")`, which only
# removed the literal `/v1` substring and left `/api` behind. The
# regex below removes every known suffix idempotently.
_PROVIDER_SUFFIX_PATTERN = re.compile(
    r"/(?:api/v0|api/v1|openai/v1|openai|v1beta|v1|api)/?$",
    re.IGNORECASE,
)


def _normalize_provider_base(base_url: str) -> str:
    """Strip any known endpoint suffix from a provider base URL.

    Idempotent. Safe to call on the raw value from the vault; the
    result is always the server root (host + optional port + path
    prefix), never an endpoint-specific URL.

    Examples
    --------
        http://127.0.0.1:1234              -> http://127.0.0.1:1234
        http://127.0.0.1:1234/             -> http://127.0.0.1:1234
        http://127.0.0.1:1234/v1           -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api          -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api/v0       -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api/v1       -> http://127.0.0.1:1234
        http://127.0.0.1:1234/api/api/v0   -> http://127.0.0.1:1234
    """
    cleaned = (base_url or "").strip().rstrip("/")

    # Collapse any literal double /api/ left behind by the legacy
    # client, so that even a pre-broken value heals here.
    while "/api/api/" in cleaned:
        cleaned = cleaned.replace("/api/api/", "/api/")

    # Strip known suffixes, possibly repeated (e.g. "/v1/api/v1").
    while True:
        stripped = _PROVIDER_SUFFIX_PATTERN.sub("", cleaned)
        if stripped == cleaned or not stripped:
            break
        cleaned = stripped.rstrip("/")

    return cleaned


_NATIVE_PROVIDER_MAP: Dict[str, str] = {
    "lmstudio": "lmstudio_native",
    "lm_studio": "lmstudio_native",
    "lm-studio": "lmstudio_native",
    "lmstudio_native": "lmstudio_native",
    "ollama": "ollama_native",
    "ollama_native": "ollama_native",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "google_genai": "google_genai",
    "google-genai": "google_genai",
    "google": "google_genai",
    "gemini": "google_genai",
    "openai_native": "openai_native",
}


async def _discover_via_external_package(package_id: str) -> list:
    """
    Discover models for an installed Provider Package by delegating to
    its adapter via the plugin host.

    The adapter runs in its own worker subprocess (isolated venv) and
    owns its own authentication (OAuth, SigV4, etc.) and response
    parsing — so this helper only needs to construct the descriptor
    dict that tells the plugin host which adapter to load.

    Returns the raw model list the adapter produced, or ``[]`` on any
    failure. The caller falls back to legacy HTTP branches when this
    returns an empty list, so misconfigured packages never break the
    Scan Models flow.
    """
    try:
        from omweb.provider_packages.package_manager import (
            provider_package_manager as _ppm,
        )
    except Exception as exc:
        logger.debug(
            "[fetch-models] package_manager unavailable: %s", exc
        )
        return []

    entry = _ppm.get_package(package_id)
    if entry is None or not entry.enabled:
        logger.debug(
            "[fetch-models] Package %r not installed or disabled.",
            package_id,
        )
        return []

    descriptor = _ppm.get_active_adapter_descriptor(package_id)
    if descriptor is None:
        logger.debug(
            "[fetch-models] Package %r has no active adapter descriptor.",
            package_id,
        )
        return []

    connection = get_active_connection_for_package(package_id)
    if connection is None:
        logger.debug(
            "[fetch-models] Package %r has no active connection.",
            package_id,
        )
        return []

    # Lazy import to avoid the circular dependency:
    #   config_rtr.py ← providers.py
    # (providers.py imports list_provider_connections & friends from
    # THIS module at load time, so we must defer the reverse import).
    try:
        from omweb.routers.store.providers import (
            _inject_installed_certificates,
            _try_read_manifest,
        )
    except Exception as exc:
        logger.debug(
            "[fetch-models] Could not import provider helpers: %s", exc
        )
        return []

    try:
        manifest = _try_read_manifest(package_id, entry.active_version)
        fields = dict(connection.get("fields") or {})
        fields = _inject_installed_certificates(
            package_id, fields, manifest
        )
        base_url = fields.pop("base_url", None)
        api_key = fields.pop("api_key", None)

        # Serialize the descriptor into the shape the plugin host
        # expects. Mirrors ``providers.py:test_connection()``.
        fields["_peldrun_plugin"] = {
            "provider_id": descriptor.provider_id,
            "version": descriptor.version,
            "hash": descriptor.hash,
            "transport": descriptor.transport,
            "api_contract": descriptor.api_contract,
            "entry_point": descriptor.entry_point,
            "class_name": descriptor.class_name,
            "base_adapter": descriptor.base_adapter,
            "package_dir": str(descriptor.package_dir),
            "venv_python": (
                str(descriptor.venv_python)
                if descriptor.venv_python is not None
                else None
            ),
            "features": dict(descriptor.features or {}),
            "permissions": dict(descriptor.permissions or {}),
            "enabled": descriptor.enabled,
        }

        ctx = ConnectionContext(
            connection_id=connection["id"],
            provider_id=package_id,
            base_url=base_url or None,
            api_key=api_key or None,
            timeout=60.0,
            extra_params=fields,
        )

        adapter = provider_factory.create(package_id, ctx)
        try:
            models = await adapter.discover_models(ctx)
            if not isinstance(models, list):
                return []
            return models
        finally:
            try:
                await adapter.close()
            except Exception:
                pass
    except Exception as exc:
        logger.warning(
            "[fetch-models] External package discovery failed for %r: %s",
            package_id,
            exc,
        )
        return []


async def _discover_via_adapter(
    adapter_id: str,
    base_url: str,
    api_key: str,
) -> list:
    """
    Try to use a registered adapter's ``discover_models()`` method.

    Returns the raw list the adapter produces, or ``[]`` on any
    failure (adapter not registered, missing SDK, network error,
    auth failure, empty response). The caller MUST fall through to
    the legacy HTTP branches when this returns ``[]`` so that
    custom endpoints and unreachable providers keep working exactly
    as before.

    Why this matters
    ----------------
    The legacy ``fetch-models`` HTTP path sends
    ``Authorization: Bearer <key>`` to ``<base>/models``. That works
    for OpenAI-compatible providers. It DOES NOT work for:

      * **Anthropic** — needs ``x-api-key`` (not Bearer).
      * **Google**    — needs ``x-goog-api-key`` (not Bearer).
      * **OpenAI**    — the official SDK handles pagination, retries,
                        and headers more robustly than raw HTTP.

    Adapters own their own auth, so this path fixes those providers
    without any per-provider branching in the route handler.
    """
    try:
        connection = ConnectionContext(
            connection_id="discovery",
            provider_id=adapter_id,
            base_url=base_url or None,
            api_key=api_key or None,
            timeout=15.0,
        )
        adapter = provider_factory.create(adapter_id, connection)
    except BaseException as exc:
        logger.error(
            "[fetch-models] Adapter creation failed for %r: %s",
            adapter_id,
            exc,
            exc_info=True,
        )
        return []

    try:
        models = await adapter.discover_models(connection)
        if not isinstance(models, list):
            return []
        return models
    except Exception as exc:
        logger.debug(
            "[fetch-models] Adapter discover_models failed for %r: %s",
            adapter_id,
            exc,
        )
        return []
    finally:
        try:
            await adapter.close()
        except Exception:
            pass


def _parse_adapter_model_list(raw_list: list) -> Tuple[list, dict]:
    """
    Normalize the heterogeneous model list shapes returned by native
    adapters into the ``models_metadata.json`` format.

    Handles all five current shapes:

      * **OpenAI**     — {id, object, created, owned_by}
      * **Anthropic**  — {id, display_name, created_at, type}
      * **Google**     — {id, full_name, display_name, description,
                          input_token_limit, output_token_limit,
                          supported_generation_methods}
      * **Ollama**     — {id, size, digest, family, parameter_size,
                          quantization_level}
      * **LM Studio**  — {id, type, publisher, arch, quantization,
                          max_context_length, capabilities}

    Google embeddings-only models (no ``generateContent`` method) are
    excluded from the returned list — they cannot serve chat requests
    and would clutter the UI with unusable entries.

    Returns ``(sorted_unique_model_ids, metadata_dict)``. The metadata
    dict is keyed by model ID and already in the shape that
    ``save_metadata_cache_merge`` expects.
    """
    model_ids: list = []
    metadata: dict = {}

    if not isinstance(raw_list, list):
        logger.warning("[fetch-models] Adapter returned non-list: %r", type(raw_list))
        return [], {}

    for item in raw_list:
        if not isinstance(item, dict):
            continue

        # LM Studio's /api/v1/models uses ``key``; /api/v0/models uses
        # ``id``. OpenAI / Anthropic / Google adapters all use ``id``.
        # Accept all three forms.
        mid = (
            item.get("id")
            or item.get("key")
            or item.get("name")
        )
        if not isinstance(mid, str) or not mid:
            continue

        # Generic embedding filter.
        #
        # Some providers (GigaChat, and any future provider whose
        # /models endpoint does not declare a type discriminator)
        # return chat AND embedding models in the same list. Embedding
        # models cannot serve /chat/completions — selecting one in the
        # UI would produce a provider error on the first message.
        #
        # Detection is by name pattern only because no provider-side
        # signal exists. This is a heuristic; it matches every
        # GigaChat embedding variant (Embeddings, Embeddings-2,
        # EmbeddingsGigaR, GigaEmbeddings-3B-*) and OpenAI-style
        # embeddings (text-embedding-3-small, etc.) while never
        # matching a real chat model.
        #
        # Adapters that DO emit an explicit ``type: "embeddings"``
        # field (like LM Studio) are also caught by the second
        # condition, so this filter is safe to leave in place even
        # after future adapter improvements.
        if "embedding" in mid.lower():
            logger.debug(
                "[fetch-models] Skipping embedding model %r", mid
            )
            continue
        if item.get("type") == "embeddings":
            logger.debug(
                "[fetch-models] Skipping embedding model %r (type)",
                mid,
            )
            continue

        # Google-only filter: skip embeddings-only models. The
        # ``supported_generation_methods`` field is only present on
        # Google responses; other adapters don't set it, so this
        # branch is a no-op for them.
        #
        # Accept multiple shapes: plain strings ("generateContent"),
        # and enums (which may stringify to "GenerationMethod.GENERATE_CONTENT"
        # in some Python/SDK versions). Check both the raw and the
        # camelCase form.
        methods = item.get("supported_generation_methods")
        if isinstance(methods, list) and methods:
            has_generate = False
            for m in methods:
                if not isinstance(m, str):
                    m = str(m)
                if "generatecontent" in m.lower().replace("_", ""):
                    has_generate = True
                    break
            if not has_generate:
                continue

        model_ids.append(mid)

        entry: Dict[str, Any] = {
            "key": mid,
            "type": "llm",
            "display_name": (
                item.get("display_name") or mid
            ),
        }

        # Context / output limits — multiple field names across adapters.
        ctx = (
            item.get("max_context_length")
            or item.get("input_token_limit")
            or item.get("context_length")
            or item.get("context_window")
        )
        if isinstance(ctx, int) and ctx > 0:
            entry["max_context_length"] = int(ctx)

        out = (
            item.get("max_output_tokens")
            or item.get("output_token_limit")
        )
        if isinstance(out, int) and out > 0:
            entry["max_output_tokens"] = int(out)

        # ------------------------------------------------------------------
        # Provider-specific descriptive fields.
        #
        # LM Studio returns:
        #   "params_string": "4.0B"
        #   "architecture": "nemotron_h"
        #   "quantization": {"name": "Q4_K_M", "bits_per_weight": 4}
        #
        # Ollama returns:
        #   "parameter_size": "8.2B"
        #   "family": "qwen3"
        #   "quantization_level": "Q4_K_M"    (string)
        #
        # The previous version of this function:
        #   * checked only ``arch`` (missing LM Studio's ``architecture``);
        #   * only accepted ``quantization`` as a STRING (missing the
        #     dict shape LM Studio returns);
        #   * checked only ``parameter_size`` (missing LM Studio's
        #     ``params_string``).
        #
        # Result: LM Studio models showed correct Context Window and
        # Publisher but "Not reported" for Parameters, Quantization,
        # and Architecture. The extraction below handles both shapes.
        # ------------------------------------------------------------------
        publisher = item.get("publisher")
        if isinstance(publisher, str) and publisher:
            entry["publisher"] = publisher

        # Architecture — LM Studio uses "architecture"; Ollama uses
        # "family"; older LM Studio versions used "arch". Accept all.
        arch = item.get("architecture") or item.get("arch")
        if isinstance(arch, str) and arch:
            entry["architecture"] = arch

        family = item.get("family")
        if isinstance(family, str) and family:
            entry["family"] = family
            # For Ollama, "family" doubles as architecture; only fill
            # architecture from family when no explicit architecture
            # was set above.
            if "architecture" not in entry:
                entry["architecture"] = family

        # Parameters — LM Studio uses "params_string"; Ollama uses
        # "parameter_size".
        params = item.get("params_string") or item.get("parameter_size")
        if isinstance(params, str) and params:
            entry["params_string"] = params

        # Quantization — LM Studio returns a dict; Ollama returns a
        # string in "quantization_level". Preserve LM Studio's dict
        # shape verbatim (it carries bits_per_weight); synthesize a
        # minimal {"name": ...} for string-shaped providers.
        quant = item.get("quantization") or item.get("quantization_level")
        if isinstance(quant, dict):
            entry["quantization"] = dict(quant)
        elif isinstance(quant, str) and quant:
            entry["quantization"] = {"name": quant}

        # LM Studio ``type`` field overrides the default "llm".
        model_type = item.get("type")
        if isinstance(model_type, str) and model_type:
            entry["type"] = model_type

        # ------------------------------------------------------------------
        # [FIX-LMSTUDIO-CAPS-V2] Extract the provider's capabilities.
        #
        # Two schemas coexist across LM Studio releases:
        #
        #   V2 (0.3.x+, /api/v1/models) — dict:
        #     "capabilities": {
        #       "vision": true,
        #       "trained_for_tool_use": true,
        #       "reasoning": {
        #         "allowed_options": ["off", "on"],
        #         "default": "on"
        #       }
        #     }
        #
        #   V1 (older, /api/v0/models) — list of tokens:
        #     "capabilities": ["completion", "tool_use", "vision"]
        #
        # Both are authoritative. The V2 dict is preferred because it
        # distinguishes "reasoning supported but always-on" (no toggle
        # allowed) from "reasoning supported and user-toggleable" — a
        # distinction the V1 token list cannot express.
        #
        # The output uses the canonical PELDRUN vocabulary with
        # ``source = "provider_live"`` so the resolver prioritizes
        # these facts (priority 95, below manual override, above
        # every other automated source).
        # ------------------------------------------------------------------
        raw_caps = item.get("capabilities")
        caps_dict: Optional[Dict[str, Any]] = None
        caps_set: Optional[set] = None

        if isinstance(raw_caps, dict):
            caps_dict = raw_caps
        elif isinstance(raw_caps, list) and raw_caps:
            caps_set = {
                str(c).strip().lower()
                for c in raw_caps
                if isinstance(c, str) and c.strip()
            }

        if caps_dict is not None or caps_set is not None:
            entry.setdefault("capabilities", {})
            entry.setdefault("capability_sources", {})

            # ------------------------------------------------------------------
            # [FIX-PACKAGE-SOURCES] Respect the adapter's own source hints.
            #
            # Different providers declare capabilities at different levels:
            #
            #   * LM Studio / Ollama — derived from a LIVE endpoint
            #     (GET /api/v1/models, POST /api/show). The correct
            #     provenance is "provider_live".
            #
            #   * Provider Packages (GigaChat, future plugins) — the
            #     adapter's `discover_models()` returns a static list
            #     built from the package's manifest.features. It is
            #     NOT live-queried. The correct provenance is
            #     "provider_catalog" (or whatever the adapter declared
            #     in its own response).
            #
            # When the adapter explicitly sets `capability_sources`,
            # honor it. Otherwise fall back to "provider_live" (which
            # matches the pre-existing behavior for LM Studio/Ollama).
            # ------------------------------------------------------------------
            adapter_sources = item.get("capability_sources")
            if not isinstance(adapter_sources, dict):
                adapter_sources = {}

            def _src(field: str) -> str:
                """Return the source label to record for a capability field."""
                raw = adapter_sources.get(field)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
                return "provider_live"

            # ---- Vision ----
            if caps_dict is not None:
                vision_val = caps_dict.get("vision")
                if isinstance(vision_val, bool):
                    entry["capabilities"]["vision"] = vision_val
                    entry["capability_sources"]["vision"] = _src("vision")
            elif caps_set is not None:
                if "vision" in caps_set or "vlm" in caps_set or model_type == "vlm":
                    entry["capabilities"]["vision"] = True
                    entry["capability_sources"]["vision"] = _src("vision")
                else:
                    entry["capabilities"]["vision"] = False
                    entry["capability_sources"]["vision"] = _src("vision")

            # ---- Tools ----
            if caps_dict is not None:
                tools_val = caps_dict.get("trained_for_tool_use")
                if isinstance(tools_val, bool):
                    entry["capabilities"]["trained_for_tool_use"] = tools_val
                    entry["capability_sources"]["trained_for_tool_use"] = _src("trained_for_tool_use")
            elif caps_set is not None:
                if "tool_use" in caps_set or "tools" in caps_set:
                    entry["capabilities"]["trained_for_tool_use"] = True
                else:
                    entry["capabilities"]["trained_for_tool_use"] = False
                entry["capability_sources"]["trained_for_tool_use"] = _src("trained_for_tool_use")

            # ---- Reasoning ----
            reasoning_obj = caps_dict.get("reasoning") if caps_dict is not None else None
            if isinstance(reasoning_obj, dict):
                allowed = reasoning_obj.get("allowed_options")
                mode = "toggle"
                if isinstance(allowed, list):
                    allowed_set = {str(a).strip().lower() for a in allowed if isinstance(a, str)}
                    if "off" in allowed_set and "on" in allowed_set:
                        mode = "toggle"
                    elif allowed_set == {"on"}:
                        mode = "automatic"
                    elif allowed_set == {"off"}:
                        mode = "none"
                entry["capabilities"]["reasoning"] = (mode != "none")
                entry["capability_sources"]["reasoning"] = _src("reasoning")
                entry.setdefault("capability_details", {})
                entry["capability_details"]["reasoning"] = {
                    "mode": mode,
                    "state": "supported" if mode != "none" else "unsupported",
                    "allowed_options": list(allowed) if isinstance(allowed, list) else [],
                    "default": reasoning_obj.get("default"),
                }
            elif isinstance(reasoning_obj, bool):
                entry["capabilities"]["reasoning"] = reasoning_obj
                entry["capability_sources"]["reasoning"] = _src("reasoning")
                if reasoning_obj:
                    entry.setdefault("capability_details", {})
                    entry["capability_details"]["reasoning"] = {
                        "mode": "toggle",
                        "state": "supported",
                    }
            elif caps_set is not None:
                has_reasoning = "reasoning" in caps_set or "thinking" in caps_set
                entry["capabilities"]["reasoning"] = has_reasoning
                entry["capability_sources"]["reasoning"] = _src("reasoning")
                if has_reasoning:
                    entry.setdefault("capability_details", {})
                    entry["capability_details"]["reasoning"] = {
                        "mode": "toggle",
                        "state": "supported",
                    }

        metadata[mid] = entry

    return sorted(set(model_ids)), metadata


def save_metadata_cache_merge(new_meta: dict) -> None:
    """
    Merge new metadata into the cache while preserving manual overrides.

    Used exclusively by ``/fetch-models`` so that a rescan does not
    clobber any capability the user set by hand. Entries not present
    in the cache are added verbatim; existing entries are merged
    field by field via ``_merge_metadata_entries``.
    """
    data: dict = {}
    if METADATA_CACHE.exists():
        try:
            with open(METADATA_CACHE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    for key, new_entry in new_meta.items():
        try:
            if key in data and isinstance(data[key], dict):
                data[key] = _merge_metadata_entries(data[key], new_entry)
            else:
                data[key] = new_entry
        except Exception as merge_err:
            # Never abort the whole merge because of a single entry.
            logger.warning(
                "merge_metadata_entries failed for key %r: %s — "
                "storing new entry verbatim",
                key,
                merge_err,
            )
            data[key] = new_entry

    try:
        METADATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = METADATA_CACHE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(METADATA_CACHE)
    except Exception as e:
        logger.warning(f"Error saving merged metadata cache: {e}")


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
    # Optional explicit metadata endpoint supplied by the frontend from
    # the vault's `metadataEndpoint` field. When present, the probe
    # hits this URL verbatim instead of guessing `<base>/models` - which
    # is what caused the `/api/api/v0/models` failure.
    metadata_endpoint: Optional[str] = ""


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

    # Explicit metadata endpoint from the vault takes priority.
    #
    # The user configured this URL themselves in the settings UI, so
    # it is the authoritative probe target. Falling back to a
    # constructed URL is only necessary when the field is empty -
    # which is the case for legacy custom endpoints that have no
    # dedicated metadata route.
    explicit_meta = (payload.metadata_endpoint or "").strip()

    if "anthropic.com" in base or "anthropic" in p_type:
        test_url = explicit_meta or "https://api.anthropic.com/v1/models"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif "googleapis.com" in base or "gemini" in p_type:
        test_url = explicit_meta or (
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        )
        headers["x-goog-api-key"] = api_key
    else:
        if explicit_meta:
            test_url = explicit_meta
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
    """
    Discover available models from the provider endpoint, then — for
    providers with a capability endpoint — query each model's
    capabilities live and cache the results.

    Flow
    ----
        1. GET ``<metadata_endpoint or base>/models`` → list of model
           IDs plus any metadata the provider exposes there.

        2. Live capability query per model:
             * Ollama     → ``POST /api/show``
             * LM Studio  → ``GET /api/v0/models``
             * Google     → ``GET /v1beta/models/{name}``
             * OpenAI / Anthropic / DeepSeek / others → skipped
               (no capability endpoint exists; the entries are saved
               with only what ``/models`` reported.)

        3. Merge results into ``models_metadata.json`` in a MERGE-AWARE
           fashion: any capability the user has previously set manually
           (``capability_sources[field] == "manual"``) is preserved,
           even when the live scan disagrees.

    The frontend calls this via the "Scan Models" button. The result is
    a complete per-model metadata snapshot ready to be used by
    ``lookup_model_facts`` (and therefore by the runtime) without any
    further network calls.
    """
    try:
        return await _fetch_available_models_impl(payload)
    except BaseException as exc:
        # The exception handler itself must NEVER raise — otherwise the
        # original error becomes a 500 with a plain-text body.
        try:
            logger.exception(
                "Unhandled error in fetch_available_models for provider %r: %s",
                getattr(payload, "provider_id", "?"),
                exc,
            )
        except Exception:
            pass

        try:
            err_type = type(exc).__name__
            err_msg = str(exc)
        except Exception:
            err_type = "UnknownError"
            err_msg = "<exception stringification failed>"

        return {
            "ok": False,
            "error": f"Internal error: {err_type}: {err_msg}",
            "models": [],
            "models_metadata": {},
        }


async def _fetch_available_models_impl(payload: FetchModelsRequest):
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

    # Ensure any externally-installed provider packages (entry points
    # pip-installed into the main environment) are registered before
    # discovery. Idempotent after the first call — negligible cost on
    # repeated requests, but critical for the "install a package, then
    # immediately scan for models" workflow.
    try:
        ensure_external_providers_discovered()
    except Exception as exc:
        logger.debug(
            "External provider discovery failed (non-fatal): %s", exc
        )

    # ------------------------------------------------------------------
    # Helper: parse a /models response into (ids, metadata).
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

            # Build the entry even when no context/output tokens are
            # reported, so that capability facts from the same response
            # are not lost. The prior `if ctx is not None or out is
            # not None:` guard silently discarded models whose
            # /models response carried only capabilities.
            entry: Dict[str, Any] = {
                "key": mid_str,
                "type": m.get("type", "llm"),
                "display_name": m.get("display_name")
                or m.get("displayName")
                or mid_str,
            }
            if ctx:
                entry["max_context_length"] = int(ctx)
            if out:
                entry["max_output_tokens"] = int(out)

            # Preserve and normalize capabilities when present. This is
            # the same dual-shape extraction performed in
            # ``_parse_adapter_model_list``; keeping the two in sync
            # ensures Branch 2 produces metadata identical to Branch 0.
            raw_caps = m.get("capabilities")
            if isinstance(raw_caps, dict):
                entry.setdefault("capabilities", {})
                entry.setdefault("capability_sources", {})
                for field, target_field in (
                    ("vision", "vision"),
                    ("trained_for_tool_use", "trained_for_tool_use"),
                ):
                    v = raw_caps.get(field)
                    if isinstance(v, bool):
                        entry["capabilities"][target_field] = v
                        entry["capability_sources"][target_field] = "provider_live"
                reason_obj = raw_caps.get("reasoning")
                if isinstance(reason_obj, dict):
                    allowed = reason_obj.get("allowed_options")
                    mode = "toggle"
                    if isinstance(allowed, list):
                        allowed_set = {str(a).strip().lower() for a in allowed if isinstance(a, str)}
                        if "off" in allowed_set and "on" in allowed_set:
                            mode = "toggle"
                        elif allowed_set == {"on"}:
                            mode = "automatic"
                        elif allowed_set == {"off"}:
                            mode = "none"
                    entry["capabilities"]["reasoning"] = (mode != "none")
                    entry["capability_sources"]["reasoning"] = "provider_live"
                    entry.setdefault("capability_details", {})
                    entry["capability_details"]["reasoning"] = {
                        "mode": mode,
                        "state": "supported" if mode != "none" else "unsupported",
                        "allowed_options": list(allowed) if isinstance(allowed, list) else [],
                        "default": reason_obj.get("default"),
                    }
            elif isinstance(raw_caps, list) and raw_caps:
                caps_set = {str(c).strip().lower() for c in raw_caps if isinstance(c, str)}
                entry.setdefault("capabilities", {})
                entry.setdefault("capability_sources", {})
                entry["capabilities"]["vision"] = (
                    "vision" in caps_set or "vlm" in caps_set
                    or m.get("type") == "vlm"
                )
                entry["capabilities"]["trained_for_tool_use"] = (
                    "tool_use" in caps_set or "tools" in caps_set
                )
                has_reasoning = "reasoning" in caps_set or "thinking" in caps_set
                entry["capabilities"]["reasoning"] = has_reasoning
                for f in ("vision", "trained_for_tool_use", "reasoning"):
                    entry["capability_sources"][f] = "provider_live"
                if has_reasoning:
                    entry.setdefault("capability_details", {})
                    entry["capability_details"]["reasoning"] = {
                        "mode": "toggle",
                        "state": "supported",
                    }

            meta[mid_str] = entry
            if "/" in mid_str:
                short = mid_str.rsplit("/", 1)[-1]
                if short and short not in meta:
                    meta[short] = {**entry, "key": short}
        return sorted(set(ids)), meta

    # ------------------------------------------------------------------
    # Step 1 — discover model list.
    #
    # [M11] Branch 0 takes priority: adapter-based discovery via the
    # provider factory. Native adapters own their own SDK, auth
    # headers, and response parsing, so this is the ONLY path that
    # works correctly for Anthropic (needs x-api-key), Google (needs
    # x-goog-api-key), and any future provider with a non-Bearer
    # auth scheme.
    #
    # On any failure (unknown provider, adapter crash, empty
    # response) we fall through to the legacy HTTP branches below,
    # so custom OpenAI-compatible endpoints keep working exactly as
    # they did before M11.
    # ------------------------------------------------------------------
    model_ids: list = []
    metadata: dict = {}
    discovery_error: Optional[str] = None
    discovery_method: str = "http"

    native_id = _NATIVE_PROVIDER_MAP.get(p_id) or _NATIVE_PROVIDER_MAP.get(p_type)
    if native_id:
        adapter_models = await _discover_via_adapter(native_id, base, api_key)
        if adapter_models:
            adapter_ids, adapter_meta = _parse_adapter_model_list(adapter_models)
            if adapter_ids:
                model_ids = adapter_ids
                metadata = adapter_meta
                discovery_method = "adapter"
                logger.info(
                    "[fetch-models] Adapter %r discovered %d model(s).",
                    native_id,
                    len(adapter_ids),
                )
            else:
                logger.debug(
                    "[fetch-models] Adapter %r returned %d raw entries, "
                    "none parsable.",
                    native_id,
                    len(adapter_models),
                )

    # ------------------------------------------------------------------
    # Branch 0b — external Provider Package.
    #
    # Packages installed via the Store (M9) are NOT in
    # _NATIVE_PROVIDER_MAP. Detect them through the runtime registry
    # (source == "external") and delegate to their adapter. This makes
    # GigaChat (and any future package) work through the EXACT SAME
    # /fetch-models flow as built-in providers — no per-provider
    # branching needed in this route.
    # ------------------------------------------------------------------
    if not model_ids and p_id:
        logger.warning(
            "[fetch-models] Branch 0b START: p_id=%r", p_id
        )
        try:
            from peldrun.llm.providers.registry import (
                SOURCE_EXTERNAL,
                provider_registry as _pkg_registry,
            )

            _reg = _pkg_registry.find(p_id)
            logger.warning(
                "[fetch-models] Branch 0b: registry_hit=%r source=%r",
                _reg is not None,
                (_reg.source if _reg is not None else None),
            )

            if _reg is not None and _reg.source == SOURCE_EXTERNAL:
                _ext_models = await _discover_via_external_package(p_id)
                logger.warning(
                    "[fetch-models] Branch 0b: "
                    "_discover_via_external_package returned type=%s "
                    "len=%d",
                    type(_ext_models).__name__,
                    len(_ext_models) if isinstance(_ext_models, list) else -1,
                )
                if _ext_models:
                    _ext_ids, _ext_meta = _parse_adapter_model_list(
                        _ext_models
                    )
                    if _ext_ids:
                        model_ids = _ext_ids
                        metadata = _ext_meta
                        discovery_method = "external_package"
                        logger.warning(
                            "[fetch-models] Branch 0b SUCCESS: "
                            "External package %r discovered %d model(s).",
                            p_id,
                            len(_ext_ids),
                        )
        except Exception as exc:
            logger.warning(
                "[fetch-models] Branch 0b EXCEPTION for %r: %s: %s",
                p_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )

    explicit_meta = (payload.metadata_endpoint or "").strip()

    if model_ids:
        # Branch 0 already produced the list — skip legacy branches.
        pass
    elif explicit_meta:
        # Branch 1 — explicit metadata endpoint from the vault.
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
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Metadata endpoint failed: {exc}",
                "models": [],
                "models_metadata": {},
            }

    elif "lmstudio" in p_id or "lmstudio" in p_type or "1234" in base:
        # Branch 2 — LM Studio. Try /api/v1/models, /api/v0/models,
        # then /models in that order.
        #
        # URL normalization MUST strip any suffix the caller already
        # appended (e.g. `/api/v1`), otherwise the result becomes
        # `/api/api/v0/models` — the exact failure observed in the
        # LM Studio server log.
        clean_base = _normalize_provider_base(base)
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
                    break
            except Exception:
                continue

        if not model_ids:
            # Fall back to generic /models probe.
            url = base if base.endswith("/models") else f"{base}/models"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_list = data.get("data", []) or data.get("models", [])
                        model_ids, metadata = _parse_model_list(raw_list)
            except Exception:
                pass

    elif "ollama" in p_id or "ollama" in p_type or "11434" in base:
        # Branch 2b — Ollama. Ollama does NOT expose `/models` at all.
        # It offers two discovery endpoints:
        #   * /api/tags  (native)  -> {models: [{name, model, ...}]}
        #   * /v1/models (OpenAI-compat shim) -> {data: [{id, ...}]}
        # We try native first (richer response), then the compat shim.
        # Use the shared normalization helper — Ollama's base URL may
        # also carry a trailing `/v1` from the OpenAI-compat shim.
        clean_base = _normalize_provider_base(base)
        candidate_urls = [
            f"{clean_base}/api/tags",
            f"{clean_base}/v1/models",
            f"{base}/models" if base.endswith("/v1") else None,
        ]
        for ep in candidate_urls:
            if ep is None:
                continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(ep, headers=headers)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    raw_models = data.get("models") or data.get("data") or []
                    if not raw_models:
                        continue
                    model_ids, metadata = _parse_model_list(raw_models)
                    if model_ids:
                        break
            except Exception:
                continue

    else:
        # Branch 3 — generic <base>/models.
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
        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed: {str(e)}",
                "models": [],
                "models_metadata": {},
            }

    if not model_ids:
        return {
            "ok": False,
            "error": discovery_error or "No models discovered from provider.",
            "models": [],
            "models_metadata": {},
        }

    # ------------------------------------------------------------------
    # Step 2 — live capability enrichment.
    #
    # Only three providers expose a capability endpoint; the rest skip
    # this step and are saved with whatever /models reported. The
    # merge helper preserves any existing manual overrides.
    # ------------------------------------------------------------------
    # Live enrichment per-model is ONLY useful when the discovery layer
    # did NOT already return rich metadata. For Google, the adapter's
    # `discover_models()` already returns input_token_limit /
    # output_token_limit / supported_generation_methods — re-querying
    # 60 models at 2s each is pure latency (34s+) for zero gain.
    #
    # Ollama and LM Studio DO need enrichment: their `/models` returns
    # only IDs, and `/api/show` / `/api/v0/models` add capabilities.
    #
    # Google is intentionally excluded. See comment above.
    live_supported = p_id in (
        "ollama",
        "lmstudio",
        "lm_studio",
        "lm-studio",
        "ollama_native",
        "lmstudio_native",
    )

    if live_supported:
        try:
            # All providers in ``live_supported`` are local engines
            # (LM Studio, Ollama) whose base URL may carry a stale
            # endpoint suffix from the user's input. Normalize before
            # passing to ``provider_live.py`` so it does not
            # reconstruct a double-suffixed path like
            # `/api/api/v0/models` for every model in the list.
            live_base = _normalize_provider_base(base)
            connection = ConnectionContext(
                connection_id=p_id or "default",
                provider_id=p_id or "custom",
                base_url=live_base,
                api_key=api_key or None,
            )
            live_results = await query_providers_batch(
                provider_id=p_id,
                model_ids=model_ids,
                connection=connection,
            )
            for mid, live_result in live_results.items():
                live_meta = live_result.to_metadata_dict()
                if mid in metadata:
                    metadata[mid] = _merge_metadata_entries(
                        metadata[mid], live_meta
                    )
                else:
                    metadata[mid] = live_meta
            logger.info(
                "[fetch-models] Live enrichment for %s: %d/%d models "
                "returned capability data.",
                p_id,
                len(live_results),
                len(model_ids),
            )
        except Exception as exc:
            # Live query is best-effort — the /models data is still
            # persisted and returned.
            logger.warning(
                "[fetch-models] Live capability enrichment failed for "
                "provider %s: %s",
                p_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Step 3 — merge-aware persistence.
    #
    # Preserves manual overrides. The frontend's `ModelInfoModal` is
    # the only writer of manual sources, so rescanning cannot clobber
    # the user's explicit choices.
    # ------------------------------------------------------------------
    if metadata:
        try:
            save_metadata_cache_merge(metadata)
        except BaseException as persist_err:
            # Persistence failure must NEVER break the response —
            # the caller can still use the returned models_metadata.
            logger.warning(
                "[fetch-models] Persistence failed (non-fatal): %s",
                persist_err,
                exc_info=True,
            )

    # ----------------------------------------------------------------
    # Guard against non-serializable values from provider SDKs.
    # Google's `google-genai` SDK sometimes returns pydantic models,
    # enums, or wrapper objects in `display_name` / `family` / etc.
    # These pass through `_parse_adapter_model_list` (which only
    # checks `isinstance(..., str)` for some fields), and can later
    # fail FastAPI's jsonable_encoder — producing an opaque HTTP 500
    # with a plaintext "Internal Server Error" body.
    # ----------------------------------------------------------------
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        if hasattr(value, "value"):
            try:
                return _json_safe(value.value)
            except Exception:
                pass
        try:
            return str(value)
        except Exception:
            return "<unserializable>"

    try:
        json.dumps({"models": model_ids, "models_metadata": metadata})
    except TypeError as _ser_err:
        logger.warning(
            "[fetch-models] Non-JSON-serializable value detected — "
            "sanitizing response. Detail: %s",
            _ser_err,
        )
        return {
            "ok": True,
            "models": _json_safe(model_ids),
            "models_metadata": _json_safe(metadata),
        }

    return {
        "ok": True,
        "models": model_ids,
        "models_metadata": metadata,
    }


# ============================================================
# File-Based Persistent Models Vault (No Browser Dependence)
# ============================================================
VAULT_DIR = BACKEND_DIR / "data"
VAULT_FILE = VAULT_DIR / "models_vault.json"

DEFAULT_VAULT = {
    "cloud_vault": [],
    "custom_endpoints": [],
    # Provider Package connections (M9/M10).
    #
    # Each entry is a saved connection for an installed Provider Package.
    # It is completely separate from ``custom_endpoints`` above, which
    # serves the built-in provider flows (openai_compat, anthropic,
    # google_genai, ollama_native, lmstudio_native) managed through
    # the LLM Settings UI.
    #
    # Why separate?
    #   * The LLM Settings UI shows ``custom_endpoints`` for manual
    #     user-defined providers. If package connections lived there,
    #     they would appear twice (once in LLM Settings, once in Store
    #     → Providers), confusing the user.
    #   * Packages need to be managed exclusively from the Store, with
    #     their own lifecycle (enable/disable per connection, multiple
    #     connections per package, custom fields from the manifest).
    #
    # Entry shape::
    #
    #     {
    #       "id": "conn_<hex>",                    # stable UUID
    #       "provider_package_id": "gigachat",     # manifest.id
    #       "name": "Personal Account",            # user label
    #       "enabled": true,                       # active flag
    #       "is_active": true,                     # selected primary
    #       "fields": {                            # per ConnectionFieldSpec
    #         "base_url": "https://...",
    #         "api_key": "...",
    #         "credentials": "...",
    #         "scope": "PERS",
    #         ...
    #       },
    #       "created_at": "2025-...",
    #       "updated_at": "2025-..."
    #     }
    "provider_connections": [],
    # Per-package model selections.
    #
    # Structure:
    #   {
    #     "<provider_package_id>": {
    #       "savedModels": ["model-a", "model-b"],
    #       "model": "model-a"
    #     }
    #   }
    #
    # Completely separate from `cloud_vault` (which manages built-in
    # providers like OpenAI / Anthropic via the Store-independent LLM
    # Settings flow). This key stores ONLY model selections for
    # packages installed from Store → Providers (GigaChat, etc.).
    #
    # The connection credentials themselves live in
    # `provider_connections` above — this key only remembers which
    # models the user picked for each package.
    "provider_package_models": {},
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
    """
    Load persisted models vault from disk.

    Every returned value is a DEEP copy, never a shallow one. This is
    critical because callers (``save_provider_connection``,
    ``save_models_vault``, the connection helpers below) mutate the
    nested ``provider_connections`` / ``provider_package_models`` /
    ``cloud_vault`` containers IN PLACE.

    A shallow copy (``dict(DEFAULT_VAULT)``) only duplicates the outer
    mapping; the nested lists and dicts remain shared by reference with
    the module-level ``DEFAULT_VAULT`` constant. Any in-place mutation
    would therefore leak across:

      * subsequent requests in the same worker process,
      * tests running back-to-back in a single Python interpreter,
      * worker forks in a multi-process Uvicorn / Gunicorn deployment
        (once polluted pre-fork, all children inherit the polluted state),
      * schema upgrades (an old vault file gets the new default keys
        injected by reference, then mutated by the next save).

    Deep copying eliminates all of the above. ``DEFAULT_VAULT`` is
    JSON-safe (only list / dict / str / int / bool / None), so
    ``copy.deepcopy`` is cheap and fully sufficient.
    """
    try:
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        if not VAULT_FILE.exists():
            return copy.deepcopy(DEFAULT_VAULT)
        with open(VAULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in DEFAULT_VAULT.items():
                if k not in data:
                    # Deep copy per-key for the same reason as above:
                    # the value taken from DEFAULT_VAULT must never be
                    # shared with it by reference, otherwise a mutation
                    # of ``data[k]`` would mutate ``DEFAULT_VAULT[k]``.
                    data[k] = copy.deepcopy(v)
            return data
    except Exception as e:
        logger.warning(f"[VAULT] Error reading vault file: {e}")
        return copy.deepcopy(DEFAULT_VAULT)


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
    for key in [
        "cloud_vault",
        "custom_endpoints",
        "provider_connections",
        "provider_package_models",
        "lmstudio_vault",
        "ollama_vault",
        "scanned_models",
    ]:
        if key in payload:
            current[key] = payload[key]
    save_vault_to_disk(current)
    return {"status": "success", "vault": current}


# ============================================================
# Provider Package Connections (M9/M10)
# ============================================================
#
# These helpers manage the ``provider_connections`` array inside
# ``models_vault.json``. They are the ONLY write path for package
# connections — the Store → Providers UI talks to them via the
# ``/api/store/providers/{id}/connections/*`` endpoints (see
# ``routers/store/providers.py``).
#
# Design:
#   * Every connection has a stable UUID (``id``). The package id
#     (``provider_package_id``) is the manifest's canonical id, NOT
#     the connection id.
#   * Multiple connections per package are supported (personal /
#     work / sandbox), with exactly one marked ``is_active``.
#   * ``fields`` is a free-form dict matching the package's
#     ``connection_fields`` schema. Values are stored verbatim (no
#     masking) so the adapter can read them at request time —
#     masking happens ONLY at the API response layer, never here.
#   * All writes are atomic (``save_vault_to_disk`` uses tmp+replace).


def list_provider_connections(
    package_id: Optional[str] = None,
) -> list:
    """
    Return every saved connection, optionally filtered by package id.

    Parameters
    ----------
    package_id
        When provided, only connections whose ``provider_package_id``
        matches are returned. When ``None``, all connections are
        returned.

    Returns
    -------
    list
        A list of connection dicts. Empty list when the vault has
        no connections or the section is missing.
    """
    vault = load_vault_from_disk()
    raw = vault.get("provider_connections")
    if not isinstance(raw, list):
        return []
    if package_id is None:
        return [dict(c) for c in raw if isinstance(c, dict)]
    return [
        dict(c)
        for c in raw
        if isinstance(c, dict) and c.get("provider_package_id") == package_id
    ]


def get_provider_connection(connection_id: str) -> Optional[dict]:
    """
    Return a single connection by its UUID, or ``None`` if missing.
    """
    if not connection_id:
        return None
    for conn in list_provider_connections():
        if conn.get("id") == connection_id:
            return conn
    return None


def save_provider_connection(connection: dict) -> dict:
    """
    Insert or update a connection.

    The connection dict MUST contain at least ``id``,
    ``provider_package_id``, and ``fields``. ``name``, ``enabled``,
    ``is_active``, ``created_at``, and ``updated_at`` are filled in
    with defaults when missing.

    When ``is_active`` is true, every OTHER connection of the same
    ``provider_package_id`` is set to ``is_active: False`` — exactly
    one active connection per package at any time.

    Returns the saved connection (with all fields populated).
    """
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn = dict(connection) if isinstance(connection, dict) else {}

    # Validate required fields.
    if not conn.get("id"):
        conn["id"] = f"conn_{uuid.uuid4().hex[:16]}"
    if not conn.get("provider_package_id"):
        raise HTTPException(
            status_code=400,
            detail="provider_package_id is required.",
        )
    if not isinstance(conn.get("fields"), dict):
        conn["fields"] = {}

    # Fill in defaults.
    conn.setdefault("name", conn["provider_package_id"])
    conn.setdefault("enabled", True)
    conn.setdefault("is_active", False)
    conn.setdefault("created_at", now)
    conn["updated_at"] = now

    # Load the full vault, mutate the connection list, save.
    vault = load_vault_from_disk()
    existing = vault.get("provider_connections")
    if not isinstance(existing, list):
        existing = []

    # If activating this connection, deactivate siblings first.
    if conn.get("is_active"):
        for other in existing:
            if (
                isinstance(other, dict)
                and other.get("provider_package_id")
                == conn["provider_package_id"]
                and other.get("id") != conn["id"]
            ):
                other["is_active"] = False

    # Replace or append.
    #
    # On UPDATE, we MERGE fields instead of replacing the whole
    # record. The frontend sends only changed fields (to preserve
    # masked secrets), so a naive replace would wipe out:
    #   - credentials the user did not edit (they were masked in
    #     the UI and not resubmitted);
    #   - the "active" status if the user did not toggle it;
    #   - the "enabled" status if the user did not toggle it;
    #   - the original "created_at" timestamp.
    #
    # We also preserve name if it was not sent (rare, but happens
    # if a caller updates only fields).
    replaced = False
    for i, other in enumerate(existing):
        if not isinstance(other, dict) or other.get("id") != conn["id"]:
            continue

        # 1. Merge fields: existing values, then override with any
        #    key present in the new payload. New value wins per-key.
        existing_fields = other.get("fields") or {}
        if not isinstance(existing_fields, dict):
            existing_fields = {}
        new_fields = conn.get("fields") or {}
        merged_fields = dict(existing_fields)
        for fk, fv in new_fields.items():
            merged_fields[fk] = fv
        conn["fields"] = merged_fields

        # 2. Preserve metadata from the existing record when the
        #    caller did not explicitly override it. This is a
        #    setdefault-style fill AFTER the merge, so explicitly
        #    passed values still win.
        if "created_at" not in conn or not conn.get("created_at"):
            conn["created_at"] = other.get("created_at", now)
        if "name" not in conn or not conn.get("name"):
            conn["name"] = other.get("name", conn["provider_package_id"])
        if "enabled" not in conn:
            conn["enabled"] = other.get("enabled", True)
        # is_active is the trickiest: only preserve the old value
        # when the caller did NOT pass `is_active`. Passing
        # `is_active: false` explicitly must still deactivate.
        if "is_active" not in conn:
            conn["is_active"] = other.get("is_active", False)

        existing[i] = conn
        replaced = True
        break

    if not replaced:
        existing.append(conn)

    vault["provider_connections"] = existing
    save_vault_to_disk(vault)
    return conn


def delete_provider_connection(connection_id: str) -> bool:
    """
    Delete a connection by its UUID.

    Returns ``True`` when the connection existed and was removed,
    ``False`` when no connection with that id exists.
    """
    if not connection_id:
        return False
    vault = load_vault_from_disk()
    existing = vault.get("provider_connections")
    if not isinstance(existing, list):
        return False

    before = len(existing)
    filtered = [
        c for c in existing
        if isinstance(c, dict) and c.get("id") != connection_id
    ]
    if len(filtered) == before:
        return False

    vault["provider_connections"] = filtered
    save_vault_to_disk(vault)
    return True


def set_provider_connection_enabled(
    connection_id: str, enabled: bool
) -> Optional[dict]:
    """
    Toggle the ``enabled`` flag for a connection.

    Returns the updated connection, or ``None`` when not found.
    """
    conn = get_provider_connection(connection_id)
    if conn is None:
        return None
    conn["enabled"] = bool(enabled)
    return save_provider_connection(conn)


def set_active_provider_connection(
    connection_id: str,
) -> Optional[dict]:
    """
    Mark a connection as the active one for its package.

    Deactivates every sibling connection of the same package.
    Returns the updated connection, or ``None`` when not found.
    """
    conn = get_provider_connection(connection_id)
    if conn is None:
        return None
    conn["is_active"] = True
    conn["enabled"] = True  # activating implies enabling
    return save_provider_connection(conn)


def get_active_connection_for_package(
    package_id: str,
) -> Optional[dict]:
    """
    Return the currently-active connection for a package, or ``None``.

    "Active" means: ``is_active == True`` AND ``enabled == True``.
    An active-but-disabled connection is treated as "no active
    connection" — the runtime will refuse to use it.

    Used by ``dispatcher._resolve_provider_and_extras`` to build
    the ConnectionContext for a package-backed provider.
    """
    for conn in list_provider_connections(package_id):
        if conn.get("is_active") and conn.get("enabled"):
            return conn
    return None


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
