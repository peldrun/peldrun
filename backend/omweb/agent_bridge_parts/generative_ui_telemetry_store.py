"""
backend/omweb/agent_bridge_parts/generative_ui_telemetry_store.py

Disk-backed persistence for Generative UI telemetry counters.

Storage location
----------------
``<storage_root>/generative_ui_telemetry.json``

Separate file from ``generative_ui.json`` on purpose:
    - Settings change rarely (user toggles, presets).
    - Telemetry changes on every render.
    Mixing them would rewrite the settings file on every render, amplifying
    the risk of a partial write in an unrelated code path.

Schema
------
    {
      "totalRenders": 0,
      "successfulRenders": 0,
      "mermaidErrors": 0,
      "chartErrors": 0,
      "latexErrors": 0,
      "lastRenderLatencyMs": 0
    }

Concurrency
-----------
Atomic writes (temp + fsync + os.replace) serialized by an RLock. Never
raises on read — a corrupt file yields the empty counters so chat dispatch
is never blocked by a broken telemetry file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

from peldrun.storage_paths import get_storage_root

logger = logging.getLogger(__name__)

STORAGE_FILENAME = "generative_ui_telemetry.json"

EMPTY_TELEMETRY: Dict[str, Any] = {
    "totalRenders": 0,
    "successfulRenders": 0,
    "mermaidErrors": 0,
    "chartErrors": 0,
    "latexErrors": 0,
    "lastRenderLatencyMs": 0,
}

_lock = threading.RLock()


def _storage_path() -> Path:
    return get_storage_root() / STORAGE_FILENAME


def _clamp_nonneg(value: Any, default: int = 0) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if v >= 0 else 0


def normalize_telemetry(raw: Any) -> Dict[str, int]:
    if not isinstance(raw, dict):
        return dict(EMPTY_TELEMETRY)
    return {key: _clamp_nonneg(raw.get(key)) for key in EMPTY_TELEMETRY.keys()}


def load_telemetry() -> Dict[str, int]:
    path = _storage_path()
    if not path.exists():
        return dict(EMPTY_TELEMETRY)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return normalize_telemetry(json.load(f))
    except Exception as exc:
        logger.warning("Could not load telemetry from %s: %s", path, exc)
        return dict(EMPTY_TELEMETRY)


def save_telemetry(raw: Dict[str, Any]) -> Dict[str, int]:
    payload = normalize_telemetry(raw)
    path = _storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

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

    return payload


def reset_telemetry() -> Dict[str, int]:
    return save_telemetry(dict(EMPTY_TELEMETRY))


__all__ = [
    "EMPTY_TELEMETRY",
    "load_telemetry",
    "normalize_telemetry",
    "reset_telemetry",
    "save_telemetry",
]
