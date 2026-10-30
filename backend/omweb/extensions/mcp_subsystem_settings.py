# backend/omweb/extensions/mcp_subsystem_settings.py
"""
Global MCP Subsystem Settings.

This module owns the SINGLE source of truth for MCP-wide runtime
behavior. Every knob here affects the whole MCP subsystem, not one
server. Per-server credentials live in mcp_auth.json; per-tool toggles
live in the persistent tool cache; per-chat auth decisions live in
chat_mcp_auth.json. This file is orthogonal to all of them.

Storage:
    backend/storage/mcp/settings.json

Concurrency:
    Writes are atomic (temp + fsync + os.replace) and serialized by a
    module-level RLock. Reads are lock-free; a corrupt file yields
    defaults so a broken settings file never blocks MCP dispatch.

Design invariants:
    * Every field is optional in the on-disk file. Missing fields fall
      back to the canonical default defined here, never to "unset".
    * Values are validated and clamped to safe ranges so a bad edit
      cannot set call_timeout_seconds to 0 or negative.
    * Adding a new setting requires adding a DEFAULT + a clamp rule +
      (optionally) an entry in the fast-path accessor below.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from omweb.config import STORAGE_ROOT

logger = logging.getLogger("omweb.extensions.mcp_subsystem_settings")


# ─────────────────────────────────────────────────────────────────
# Canonical defaults and bounds
# ─────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: Dict[str, Any] = {
    # Master kill switch. When False, the entire MCP subsystem is
    # disabled: no discovery, no subprocess spawning, no tool injection
    # into agent manifests. Existing sessions are closed on toggle.
    "enabled": True,

    # How long a cached tool list is considered authoritative. After
    # this window the next discovery re-probes the server. A manual
    # "Refresh Tools" always bypasses this. Range: 1..168 hours.
    "tool_cache_ttl_hours": 24,

    # Maximum wall-clock time (seconds) allowed for spawning a stdio
    # subprocess or establishing an HTTP/SSE connection. Range: 5..300.
    "startup_timeout_seconds": 120,

    # Maximum wall-clock time (seconds) allowed for a single tool call
    # including response parsing. Range: 10..1800.
    "call_timeout_seconds": 120,

    # Upper bound on the number of tools accepted from a single server.
    # Protects against malformed servers that return thousands of fake
    # tools. Range: 10..1000.
    "max_tools_per_server": 100,

    # Close idle stdio subprocesses after this many minutes. A pool
    # process is considered idle if no call has touched it in this
    # window. Range: 1..1440 (24 hours).
    "auto_cleanup_idle_minutes": 30,

    # When True, stdio subprocesses are spawned on demand (first tool
    # call) rather than eagerly during discovery. Recommended for
    # servers whose startup is expensive but whose usage is sporadic.
    "lazy_start_on_demand": False,

    # Controls the default `user_enabled` flag assigned to NEW tools
    # discovered on the next refresh. It does NOT affect existing
    # tools — those retain their explicit user_enabled value.
    #
    # True  → new tools are auto-enabled (opt-out model).
    # False → new tools are auto-disabled (opt-in model).
    "send_all_tools_by_default": True,
}


_MIN_MAX = {
    "tool_cache_ttl_hours": (1, 168),
    "startup_timeout_seconds": (5, 300),
    "call_timeout_seconds": (10, 1800),
    "max_tools_per_server": (10, 1000),
    "auto_cleanup_idle_minutes": (1, 1440),
}

_BOOL_KEYS = frozenset({
    "enabled",
    "lazy_start_on_demand",
    "send_all_tools_by_default",
})

_INT_KEYS = frozenset(_MIN_MAX.keys())


# ─────────────────────────────────────────────────────────────────
# Storage location
# ─────────────────────────────────────────────────────────────────

def _settings_path() -> Path:
    root = Path(STORAGE_ROOT) / "mcp"
    root.mkdir(parents=True, exist_ok=True)
    return root / "settings.json"


_lock = threading.RLock()


# ─────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────

def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """Coerce to int within [lo, hi]; on any failure return default."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, parsed))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce any dict into the canonical, validated shape."""
    raw = raw if isinstance(raw, dict) else {}

    out: Dict[str, Any] = {}
    # Booleans
    for key in _BOOL_KEYS:
        out[key] = _coerce_bool(raw.get(key), DEFAULT_SETTINGS[key])
    # Integers with bounds
    for key in _INT_KEYS:
        lo, hi = _MIN_MAX[key]
        out[key] = _clamp_int(raw.get(key), lo, hi, DEFAULT_SETTINGS[key])
    return out


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def load_settings() -> Dict[str, Any]:
    """
    Return the effective MCP subsystem settings.

    Never raises: a corrupt or missing file yields the canonical
    defaults so a broken settings file cannot disable MCP accidentally.
    """
    path = _settings_path()
    if not path.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return normalize_settings(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not load MCP subsystem settings from %s "
            "(falling back to defaults): %s",
            path,
            exc,
        )
        return dict(DEFAULT_SETTINGS)


def save_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and atomically persist settings.

    Returns the normalized settings that were actually written.
    """
    settings = normalize_settings(raw)
    path = _settings_path()
    tmp = path.with_suffix(path.suffix + ".tmp")

    with _lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            logger.info(
                "MCP subsystem settings updated: %s",
                {k: v for k, v in settings.items() if k != "enabled"},
            )
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
    return settings


# ─────────────────────────────────────────────────────────────────
# Hot-path accessors (used inside request handlers and dispatchers)
# ─────────────────────────────────────────────────────────────────
#
# Callers on the hot path (peldrun_engine, mcp_pool) should use these
# accessors rather than load_settings() to avoid re-reading the file
# repeatedly. Each accessor is a cheap in-process load of a single key
# and never raises.

def is_enabled() -> bool:
    """True unless the user has explicitly disabled the MCP subsystem."""
    return bool(load_settings().get("enabled", True))


def get_tool_cache_ttl_seconds() -> int:
    """Return tool-cache TTL in seconds (converted from hours)."""
    return int(load_settings().get("tool_cache_ttl_hours", 24)) * 3600


def get_startup_timeout_seconds() -> float:
    return float(load_settings().get("startup_timeout_seconds", 30))


def get_call_timeout_seconds() -> float:
    return float(load_settings().get("call_timeout_seconds", 120))


def get_max_tools_per_server() -> int:
    return int(load_settings().get("max_tools_per_server", 100))


def get_auto_cleanup_idle_seconds() -> int:
    return int(load_settings().get("auto_cleanup_idle_minutes", 30)) * 60


def is_lazy_start_enabled() -> bool:
    return bool(load_settings().get("lazy_start_on_demand", False))


def is_send_all_by_default() -> bool:
    return bool(load_settings().get("send_all_tools_by_default", True))


__all__ = [
    "DEFAULT_SETTINGS",
    "load_settings",
    "save_settings",
    "normalize_settings",
    "is_enabled",
    "get_tool_cache_ttl_seconds",
    "get_startup_timeout_seconds",
    "get_call_timeout_seconds",
    "get_max_tools_per_server",
    "get_auto_cleanup_idle_seconds",
    "is_lazy_start_enabled",
    "is_send_all_by_default",
]
