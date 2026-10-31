# Path: backend/omweb/services/runtime_settings.py
"""
Runtime-configurable settings for PELDRUN.

Provides a persistent, user-editable settings store with a strict
three-tier resolution model:

    runtime_settings.json  >  environment variable  >  hardcoded default

The JSON file is authoritative once a user saves a value from the
Settings UI. Until then, environment variables act as deployment
overrides, and hardcoded defaults are the last resort. This mirrors
the precedence already used by ``models_budget.json`` and
``generative_ui.json`` in the codebase.

All consumers must call ``resolve()`` fresh on every access instead of
caching values at import time. This is what makes settings changes
take effect immediately without a server restart.

Storage
-------
``<storage_root>/runtime_settings.json`` — a single JSON document::

    {
        "llm_providers": {
            "lmstudio_base_url": "http://127.0.0.1:1234/v1",
            "ollama_host": "http://127.0.0.1:11434",
            "default_base_url": "http://127.0.0.1:1234/v1",
            "default_api_key": "EMPTY"
        },
        "cleanup": {
            "enabled": true,
            "interval_hours": 6.0,
            "max_age_hours": 24.0
        },
        "streaming": {
            "flush_interval_seconds": 0.05,
            "flush_chars": 20
        }
    }

Missing keys are treated as "not overridden" and fall through to the
next tier. Concurrency is handled by an ``RLock`` + atomic temp-file
replacement, matching the pattern in ``generative_ui_store``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("omweb.services.runtime_settings")


STORAGE_FILENAME = "runtime_settings.json"
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Schema — one model per section, used by the API for validation
# ---------------------------------------------------------------------------


class CleanupSettings(BaseModel):
    """Temp artifact and checkpoint cache cleanup schedule."""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Master switch for the automatic cleanup loop. "
            "Overrides PELDRUN_TEMP_CLEANUP_ENABLED."
        ),
    )
    interval_hours: Optional[float] = Field(
        default=None,
        ge=0.016,
        le=168.0,
        description=(
            "How often the sweeper runs. "
            "Overrides PELDRUN_TEMP_CLEANUP_INTERVAL_HOURS. "
            "Range: 1 minute – 7 days."
        ),
    )
    max_age_hours: Optional[float] = Field(
        default=None,
        ge=0.1,
        le=720.0,
        description=(
            "Maximum age of a temp file before deletion. "
            "Overrides PELDRUN_TEMP_CLEANUP_MAX_AGE_HOURS. "
            "Range: 6 minutes – 30 days."
        ),
    )


class StreamingSettings(BaseModel):
    """Per-token flush tuning for the Agent-mode streaming pipeline."""

    model_config = ConfigDict(extra="forbid")

    flush_interval_seconds: Optional[float] = Field(
        default=None,
        ge=0.01,
        le=1.0,
        description=(
            "Minimum seconds between SSE flushes. "
            "Overrides PELDRUN_STREAM_FLUSH_INTERVAL."
        ),
    )
    flush_chars: Optional[int] = Field(
        default=None,
        ge=5,
        le=200,
        description=(
            "Minimum buffered characters before an SSE flush. "
            "Overrides PELDRUN_STREAM_FLUSH_CHARS."
        ),
    )


class TimeoutSettings(BaseModel):
    """
    Wall-clock ceilings for LLM requests, agent steps, and entire runs.

    Three independent timeouts govern the agent lifecycle:

      * ``llm_request_timeout_seconds`` — HTTP timeout for EACH LLM
        provider request. Enforced by ``AsyncLLMClient``.
      * ``step_timeout_seconds`` — timeout for ONE agent step (a single
        LLM call + its tool executions). Enforced by ``AgentRunner``.
      * ``total_timeout_seconds`` — wall-clock ceiling for the WHOLE
        run across all steps. Enforced by ``AgentRunner``.

    Interaction model: a ``step_timeout_seconds`` that exceeds the
    remaining ``total_timeout_seconds`` is effectively capped by the
    total ceiling. Typical values (120 / 600) leave room for 5 full
    steps before the total ceiling triggers.
    """

    model_config = ConfigDict(extra="forbid")

    llm_request_timeout_seconds: Optional[float] = Field(
        default=None,
        ge=1.0,
        le=3600.0,
        description=(
            "Per-request HTTP timeout to the LLM provider. "
            "Overrides PELDRUN_LLM_TIMEOUT and config.toml [llm] timeout. "
            "Range: 1s - 1h."
        ),
    )
    step_timeout_seconds: Optional[float] = Field(
        default=None,
        ge=1.0,
        le=1800.0,
        description=(
            "Timeout for a single agent step (one LLM call + its tools). "
            "Overrides PELDRUN_STEP_TIMEOUT and config.toml [llm] timeout. "
            "Range: 1s - 30min."
        ),
    )
    total_timeout_seconds: Optional[float] = Field(
        default=None,
        ge=10.0,
        le=7200.0,
        description=(
            "Wall-clock ceiling for the entire run. "
            "Overrides PELDRUN_TOTAL_TIMEOUT and config.toml [llm] total_timeout. "
            "Range: 10s - 2h."
        ),
    )


class RuntimeSettingsDocument(BaseModel):
    """Root document persisted to ``runtime_settings.json``.

    NOTE: LLM provider settings (LM Studio URL, Ollama host, default
    endpoint, default API key) are intentionally NOT part of this
    document. They are owned by the dedicated Models settings page
    (``/settings/setup/tabs/llm/``) and persisted via ``config.toml``.
    Duplicating them here created a second source of truth that was
    silently ignored by every runtime consumer.
    """

    model_config = ConfigDict(extra="forbid")

    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)
    streaming: StreamingSettings = Field(default_factory=StreamingSettings)


# ---------------------------------------------------------------------------
# Hardcoded defaults per field — single source of truth for both the
# resolution logic below and the schema endpoint that the UI consumes.
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "timeouts": {
        "llm_request_timeout_seconds": 300.0,
        "step_timeout_seconds": 120.0,
        "total_timeout_seconds": 600.0,
    },
    "cleanup": {
        "enabled": True,
        "interval_hours": 6.0,
        "max_age_hours": 24.0,
    },
    "streaming": {
        "flush_interval_seconds": 0.05,
        "flush_chars": 20,
    },
}

# Mapping (section, key) → env var name. Empty string = no env override.
ENV_VARS: Dict[str, Dict[str, str]] = {
    "timeouts": {
        "llm_request_timeout_seconds": "PELDRUN_LLM_TIMEOUT",
        "step_timeout_seconds": "PELDRUN_STEP_TIMEOUT",
        "total_timeout_seconds": "PELDRUN_TOTAL_TIMEOUT",
    },
    "cleanup": {
        "enabled": "PELDRUN_TEMP_CLEANUP_ENABLED",
        "interval_hours": "PELDRUN_TEMP_CLEANUP_INTERVAL_HOURS",
        "max_age_hours": "PELDRUN_TEMP_CLEANUP_MAX_AGE_HOURS",
    },
    "streaming": {
        "flush_interval_seconds": "PELDRUN_STREAM_FLUSH_INTERVAL",
        "flush_chars": "PELDRUN_STREAM_FLUSH_CHARS",
    },
}


# ---------------------------------------------------------------------------
# Cast helpers — safe coercion from JSON/env values to typed Python values.
# ---------------------------------------------------------------------------


def _cast_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _cast_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in ("", "default"):
        return None
    return s not in ("0", "false", "no", "off", "disabled")


def _cast_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cast_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_CASTERS: Dict[Tuple[str, str], Callable[[Any], Any]] = {
    ("timeouts", "llm_request_timeout_seconds"): _cast_float,
    ("timeouts", "step_timeout_seconds"): _cast_float,
    ("timeouts", "total_timeout_seconds"): _cast_float,
    ("cleanup", "enabled"): _cast_bool,
    ("cleanup", "interval_hours"): _cast_float,
    ("cleanup", "max_age_hours"): _cast_float,
    ("streaming", "flush_interval_seconds"): _cast_float,
    ("streaming", "flush_chars"): _cast_int,
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _storage_path() -> Path:
    from peldrun.storage_paths import get_storage_root

    return get_storage_root() / STORAGE_FILENAME


def _read_file_raw() -> Dict[str, Any]:
    """Return the parsed JSON, or ``{}`` when the file is absent/corrupt."""
    path = _storage_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def load_document() -> RuntimeSettingsDocument:
    """
    Load the settings document, tolerating missing keys and legacy
    fields.

    Migration note
    --------------
    Older versions of ``runtime_settings.json`` (built before the
    ``llm_providers`` section was removed) may still carry an
    ``llm_providers`` dict on disk. That section was always dead code:
    the runtime reads provider URLs from ``config.toml``, not from this
    file. We strip it silently on load so the migration requires no
    user action, and so ``RuntimeSettingsDocument`` (which has
    ``extra="forbid"``) does not reject the legacy file.
    """
    raw = _read_file_raw()
    # Strip the dead `llm_providers` section from legacy files.
    raw.pop("llm_providers", None)
    try:
        return RuntimeSettingsDocument.model_validate(raw)
    except Exception as exc:
        logger.warning("Invalid runtime_settings.json, using empty: %s", exc)
        return RuntimeSettingsDocument()


def save_document(doc: RuntimeSettingsDocument) -> RuntimeSettingsDocument:
    """Persist the document atomically."""
    path = _storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = doc.model_dump(exclude_none=True, mode="json")

    with _lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
    return doc


def reset_document() -> RuntimeSettingsDocument:
    """Delete the file and return an empty document."""
    path = _storage_path()
    with _lock:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete %s: %s", path, exc)
    return RuntimeSettingsDocument()


# ---------------------------------------------------------------------------
# Resolution — the single function every consumer should call
# ---------------------------------------------------------------------------


def resolve(
    section: str,
    key: str,
    cast: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """
    Return the effective value of a setting, honoring the precedence:

        runtime_settings.json  >  environment variable  >  hardcoded default

    A ``None`` returned at any tier falls through to the next — so
    explicitly blanking a field in the UI is equivalent to "use env, then
    default", never "hardcode None".

    Parameters
    ----------
    section : str
        One of ``llm_providers``, ``cleanup``, ``streaming``.
    key : str
        Field name inside the section.
    cast : Callable[[Any], Any], optional
        Explicit caster. When omitted, the module picks the correct
        caster from ``_CASTERS``. Passing a caster is only needed for
        fields not yet registered there.

    Returns
    -------
    Any
        The resolved value. Never ``None`` for fields that have a
        hardcoded default.
    """
    caster = cast or _CASTERS.get((section, key)) or (lambda x: x)

    # Tier 1: JSON file
    doc = load_document()
    section_obj = getattr(doc, section, None)
    if section_obj is not None:
        raw = getattr(section_obj, key, None)
        casted = caster(raw)
        if casted is not None:
            return casted

    # Tier 2: env var
    env_name = ENV_VARS.get(section, {}).get(key)
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val is not None:
            casted = caster(env_val)
            if casted is not None:
                return casted

    # Tier 3: hardcoded default
    return DEFAULTS.get(section, {}).get(key)


def get_effective_snapshot() -> Dict[str, Any]:
    """
    Return a diagnostic snapshot of every setting, including which tier
    it resolved from. Used by the API to power the UI's source badges.
    """
    doc = load_document()
    snapshot: Dict[str, Any] = {
        "values": {},
        "defaults": dict(DEFAULTS),
        "sources": {},
    }

    for section, keys in DEFAULTS.items():
        snapshot["values"][section] = {}
        snapshot["sources"][section] = {}
        section_obj = getattr(doc, section, None)

        for key in keys:
            caster = _CASTERS.get((section, key)) or (lambda x: x)

            # File
            file_val = None
            if section_obj is not None:
                file_val = caster(getattr(section_obj, key, None))
            if file_val is not None:
                snapshot["values"][section][key] = file_val
                snapshot["sources"][section][key] = "file"
                continue

            # Env
            env_name = ENV_VARS.get(section, {}).get(key)
            env_val = None
            if env_name:
                raw_env = os.environ.get(env_name)
                if raw_env is not None:
                    env_val = caster(raw_env)
            if env_val is not None:
                snapshot["values"][section][key] = env_val
                snapshot["sources"][section][key] = f"env:{env_name}"
                continue

            # Default
            snapshot["values"][section][key] = DEFAULTS[section][key]
            snapshot["sources"][section][key] = "default"

    return snapshot


def update_section(
    section: str,
    updates: Dict[str, Any],
) -> RuntimeSettingsDocument:
    """
    Merge partial updates into a section and persist.

    Keys not present in ``updates`` are left unchanged. Setting a key to
    ``None`` (or an empty string) explicitly reverts that field to the
    env/default fallback.
    """
    if section not in DEFAULTS:
        raise ValueError(f"Unknown settings section: {section}")

    doc = load_document()
    section_obj = getattr(doc, section)

    # Merge into the pydantic model — setattr on a frozen model is
    # blocked, so rebuild from a merged dict.
    existing = section_obj.model_dump()
    for k, v in updates.items():
        if k in existing:
            existing[k] = v

    new_section_cls = type(section_obj)
    new_section = new_section_cls(**existing)

    # Assign back via a full-document rebuild (doc is mutable — pydantic
    # BaseModel with extra="forbid" allows field assignment).
    setattr(doc, section, new_section)
    save_document(doc)
    return doc


# ``mask_api_key`` was removed alongside the ``llm_providers``
# section. LLM provider API keys are now exclusively managed by the
# Models settings page and are never surfaced through this API.


__all__ = [
    "CleanupSettings",
    "DEFAULTS",
    "ENV_VARS",
    "RuntimeSettingsDocument",
    "StreamingSettings",
    "TimeoutSettings",
    "get_effective_snapshot",
    "load_document",
    "reset_document",
    "resolve",
    "save_document",
    "update_section",
]
