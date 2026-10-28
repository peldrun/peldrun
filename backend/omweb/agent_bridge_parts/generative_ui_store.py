"""
backend/omweb/agent_bridge_parts/generative_ui_store.py

Disk-backed persistence for the Generative UI rendering settings.

Design rationale
----------------
The rendering configuration is a global user preference (one per
installation), not a per-request payload. It is stored once and read by
every chat and agent dispatch via ``dispatcher.py``. The frontend settings
page pushes updates through ``POST /api/config/generative-ui``; there is no
per-message override path.

Storage location
----------------
``<storage_root>/generative_ui.json``

``<storage_root>`` is resolved via ``peldrun.storage_paths.get_storage_root()``
— the same root used by the token ledger, ``session.json``, and
``models_metadata.json``.

Concurrency
-----------
All writes are atomic (temp file + fsync + os.replace) and serialized by a
module-level RLock, so concurrent POSTs from multiple browser tabs cannot
corrupt the file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from peldrun.storage_paths import get_storage_root

logger = logging.getLogger(__name__)


STORAGE_FILENAME = "generative_ui.json"

# Must stay in sync with:
#   - frontend/src/components/generative-ui/types.ts  (DEFAULT_SETTINGS)
#   - backend/peldrun/context/generative_ui.py        (DEFAULT_FEATURES, DEFAULT_LIMITS)
DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "features": {
        "mermaid": True,
        "latex": True,
        "charts": True,
        "code_highlighting": True,
    },
    "limits": {
        "max_mermaid_nodes": 40,
        "max_chart_points": 30,
        "max_table_rows": 50,
        "max_output_tokens": 4096,
    },
    "max_retries": 1,
}

_MIN_OUTPUT_TOKENS = 512
_MAX_OUTPUT_TOKENS = 8192
_MIN_RETRIES = 0
_MAX_RETRIES = 5
_MAX_PER_FORMAT_LIMIT = 500

_FEATURE_KEYS = ("mermaid", "latex", "charts", "code_highlighting")
_PER_FORMAT_LIMIT_KEYS = ("max_mermaid_nodes", "max_chart_points", "max_table_rows")

_lock = threading.RLock()


def _storage_path() -> Path:
    return get_storage_root() / STORAGE_FILENAME


def _deep_copy_defaults() -> Dict[str, Any]:
    return {
        "enabled": DEFAULT_SETTINGS["enabled"],
        "features": dict(DEFAULT_SETTINGS["features"]),
        "limits": dict(DEFAULT_SETTINGS["limits"]),
        "max_retries": DEFAULT_SETTINGS["max_retries"],
    }


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, parsed))


def _normalize_features(raw: Any) -> Dict[str, bool]:
    out = dict(DEFAULT_SETTINGS["features"])
    if isinstance(raw, dict):
        for key in _FEATURE_KEYS:
            if key in raw:
                out[key] = bool(raw[key])
    return out


def _normalize_limits(raw: Any) -> Dict[str, int]:
    out = dict(DEFAULT_SETTINGS["limits"])
    if isinstance(raw, dict):
        for key in _PER_FORMAT_LIMIT_KEYS:
            if key in raw:
                out[key] = _clamp_int(
                    raw[key], 1, _MAX_PER_FORMAT_LIMIT,
                    DEFAULT_SETTINGS["limits"][key],
                )
        if "max_output_tokens" in raw:
            out["max_output_tokens"] = _clamp_int(
                raw["max_output_tokens"],
                _MIN_OUTPUT_TOKENS, _MAX_OUTPUT_TOKENS,
                DEFAULT_SETTINGS["limits"]["max_output_tokens"],
            )
    return out


def normalize_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce an arbitrary dict into the canonical settings shape."""
    if not isinstance(raw, dict):
        return _deep_copy_defaults()
    return {
        "enabled": bool(raw.get("enabled", DEFAULT_SETTINGS["enabled"])),
        "features": _normalize_features(raw.get("features")),
        "limits": _normalize_limits(raw.get("limits")),
        "max_retries": _clamp_int(
            raw.get("max_retries"),
            _MIN_RETRIES, _MAX_RETRIES,
            DEFAULT_SETTINGS["max_retries"],
        ),
    }


def load_generative_ui_settings() -> Dict[str, Any]:
    """
    Read the persisted GenUI settings from disk.

    Returns defaults when the file is absent, unreadable, or invalid.
    Never raises — a corrupt file must not break chat dispatch.
    """
    path = _storage_path()
    if not path.exists():
        return _deep_copy_defaults()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return normalize_settings(raw)
    except Exception as exc:
        logger.warning("Could not load %s (falling back to defaults): %s", path, exc)
        return _deep_copy_defaults()


def save_generative_ui_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and atomically persist the given settings.

    Returns the normalized settings that were actually written.
    """
    settings = normalize_settings(raw)
    path = _storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with _lock:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    return settings


__all__ = [
    "DEFAULT_SETTINGS",
    "load_generative_ui_settings",
    "normalize_settings",
    "save_generative_ui_settings",
]
