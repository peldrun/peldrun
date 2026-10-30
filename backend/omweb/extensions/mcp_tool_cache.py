# backend/omweb/extensions/mcp_tool_cache.py
"""
Persistent MCP Tool Cache.

Solves two distinct problems:

  1. **Cold-start latency.** Every backend restart previously forced a
     fresh ``tools/list`` round-trip (stdio spawn + JSON-RPC handshake)
     per server, delaying the first user message by 2-5 seconds. The
     cache lets discovery return instantly until the TTL expires or the
     manifest changes.

  2. **Per-tool control.** Users can now enable or disable individual
     tools. A disabled tool is never injected into the LLM's AgentSpec,
     which cuts tool-definition tokens by 40-85% for large servers
     like GitHub (50+ tools) while keeping the ones the user actually
     needs.

Storage layout:

    backend/storage/mcp/tool_cache/{server_id}.json

On-disk schema:

    {
      "version": 1,
      "server_id": "mcp_github",
      "server_config_hash": "<sha256>",
      "fetched_at": 1730000000.0,
      "tools": [
        {
          "name": "search_repositories",
          "description": "...",
          "parameters": { "type": "object", ... },
          "user_enabled": true,
          "first_seen_at": 1730000000.0,
          "last_seen_at": 1730000000.0
        },
        ...
      ]
    }

Merge semantics on re-discovery:

  * Tools present in BOTH old and new cache:
      - ``user_enabled`` is PRESERVED from the old cache
      - ``description`` and ``parameters`` are UPDATED from the new cache
      - ``first_seen_at`` preserved; ``last_seen_at`` refreshed
  * Tools present in NEW only:
      - ``user_enabled`` = current value of ``send_all_tools_by_default``
  * Tools present in OLD only:
      - DROPPED (the server no longer exposes them)

Concurrency:
    All writes are atomic (temp + fsync + os.replace) and serialized by
    a module-level RLock. Reads are lock-free.

Security:
    The cache NEVER contains credentials. Only tool names, JSON schemas,
    and boolean flags. Credentials live exclusively in ``mcp_auth.json``
    and are managed by ``chat_mcp_auth.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.config import STORAGE_ROOT

logger = logging.getLogger("omweb.extensions.mcp_tool_cache")


# ─────────────────────────────────────────────────────────────────
# Storage location
# ─────────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    root = Path(STORAGE_ROOT) / "mcp" / "tool_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_path(server_id: str) -> Path:
    safe = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
    if not safe or safe != server_id:
        raise ValueError(f"Invalid server_id: {server_id!r}")
    return _cache_dir() / f"{safe}.json"


_lock = threading.RLock()

# Cache container schema version. Bump this whenever the shape of the
# data inside the cache changes — ``is_cache_fresh`` checks it, so any
# cache written under an older version is treated as stale and gets
# re-discovered on the next call. This is how we automatically roll out
# the $ref sanitizer to every user: old caches carry ``version: 1`` and
# are rejected; new caches written after this patch carry ``version: 2``.
# v4: cache carries `resources` and `prompts` alongside `tools`
# (MCP 2025-11-25 §5, §6). Bumping this version is what makes the
# rollout automatic for existing installs: every cache file written
# under v3 fails the ``is_cache_fresh`` version check on the next
# read, forcing a re-discovery that populates the new fields.
CACHE_SCHEMA_VERSION = 4


# ─────────────────────────────────────────────────────────────────
# Config hash
# ─────────────────────────────────────────────────────────────────

# Fields included in the config hash. Deliberately omits status
# (active / ready), description, name, and other UI-only fields,
# because they do not affect the tool surface exposed by the server.
_HASH_FIELDS = ("command", "url", "transport", "env_schema")


def compute_config_hash(manifest: Dict[str, Any]) -> str:
    """
    Fingerprint the parts of a manifest that determine the tool surface.

    A change in any of these fields means the server might now expose a
    different set of tools (e.g. user changed the command to point at a
    newer version, or added env vars that unlock additional endpoints).
    The caller uses this hash to decide whether the cached tool list is
    still trustworthy.
    """
    parts: List[str] = []
    for field in _HASH_FIELDS:
        value = manifest.get(field)
        if isinstance(value, dict):
            # Stable serialization for dicts (env_schema, headers).
            parts.append(f"{field}={json.dumps(value, sort_keys=True)}")
        elif value is not None:
            parts.append(f"{field}={value}")
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


# ─────────────────────────────────────────────────────────────────
# Atomic write helper (mirrors project_manager)
# ─────────────────────────────────────────────────────────────────

def _atomic_write_json(target: Path, data: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


# ─────────────────────────────────────────────────────────────────
# Load / invalidate
# ─────────────────────────────────────────────────────────────────

def load_cache(server_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the raw cache container for a server, or None when absent.

    Never raises: a corrupt file is treated as "no cache" so a bad
    write cannot brick the MCP subsystem.
    """
    try:
        path = _cache_path(server_id)
    except ValueError as exc:
        logger.warning("load_cache rejected invalid server_id: %s", exc)
        return None

    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Corrupt tool cache for '%s' at %s: %s",
            server_id,
            path,
            exc,
        )
        return None

    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        logger.warning("Malformed tool cache for '%s'; ignoring.", server_id)
        return None

    return data


def invalidate(server_id: str) -> bool:
    """Delete the cache for a server. Returns True if a file existed."""
    try:
        path = _cache_path(server_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    try:
        path.unlink()
        logger.info("Invalidated MCP tool cache for '%s'.", server_id)
        return True
    except OSError as exc:
        logger.warning("Failed to remove tool cache for '%s': %s", server_id, exc)
        return False


# ─────────────────────────────────────────────────────────────────
# Freshness check
# ─────────────────────────────────────────────────────────────────

def is_cache_fresh(
    cache: Optional[Dict[str, Any]],
    config_hash: str,
    ttl_seconds: int,
) -> bool:
    """
    Decide whether a loaded cache is still authoritative.

    A cache is fresh iff:
      * it exists and is structurally valid,
      * its ``version`` matches ``CACHE_SCHEMA_VERSION``,
      * its stored ``server_config_hash`` matches the current manifest,
      * its ``fetched_at`` is within ``ttl_seconds`` of now.

    The version check is what makes schema-affecting patches
    (e.g. the $ref sanitizer that changed the shape of cached
    ``parameters``) apply automatically without asking the user to
    clear caches by hand.
    """
    if not cache:
        return False
    if int(cache.get("version", 0)) != CACHE_SCHEMA_VERSION:
        return False
    if cache.get("server_config_hash") != config_hash:
        return False
    try:
        fetched_at = float(cache.get("fetched_at", 0))
    except (TypeError, ValueError):
        return False
    if fetched_at <= 0:
        return False
    return (time.time() - fetched_at) < ttl_seconds


# ─────────────────────────────────────────────────────────────────
# Merge (newly-discovered tools vs. existing cache)
# ─────────────────────────────────────────────────────────────────

def merge_tools(
    existing: Optional[Dict[str, Any]],
    new_tools: List[Dict[str, Any]],
    default_enabled: bool,
) -> List[Dict[str, Any]]:
    """
    Combine freshly discovered tools with the previous cache.

    Returns the merged list, ready to be written back to disk.

    Rules (per design decisions D3/D8):
      * Existing tool + same name → preserve ``user_enabled`` and
        ``first_seen_at``; refresh ``description``, ``parameters``,
        ``output_schema``, and ``last_seen_at``.
      * New tool not in existing cache → create with
        ``user_enabled = default_enabled``.
      * Old tool not in new discovery → dropped (server no longer
        exposes it).

    ``output_schema`` (MCP 2025-11-25 §7.2) is treated like
    ``parameters`` — the fresh value from the server is always the
    authoritative one. If the server drops the field, the cache
    drops it too (no stale carry-over).

    Input tools are the raw dicts produced by ``mcp_pool.get_tools``
    (they carry ``name``, ``description``, ``parameters``).
    """
    now = time.time()

    # Index existing tools by name for O(1) lookup.
    prev_by_name: Dict[str, Dict[str, Any]] = {}
    if existing and isinstance(existing.get("tools"), list):
        for entry in existing["tools"]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name:
                prev_by_name[name] = entry

    merged: List[Dict[str, Any]] = []
    seen: set = set()

    for tool in new_tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)

        prev = prev_by_name.get(name)

        # Base fields come from the fresh discovery.
        entry: Dict[str, Any] = {
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": (
                tool.get("parameters")
                if isinstance(tool.get("parameters"), dict)
                else {"type": "object", "properties": {}, "required": []}
            ),
        }

        # Preserve output_schema when present (MCP 2025-11-25 §7.2).
        # Accept both snake_case (our normal form) and the raw MCP
        # camelCase spelling so a direct probe result can be fed here
        # without an intermediate translation step.
        raw_out_schema = (
            tool.get("output_schema") or tool.get("outputSchema")
        )
        if isinstance(raw_out_schema, dict) and raw_out_schema:
            entry["output_schema"] = raw_out_schema

        if prev is not None:
            # Preserve the user's previous decision.
            entry["user_enabled"] = bool(prev.get("user_enabled", default_enabled))
            try:
                first_seen = float(prev.get("first_seen_at", now))
            except (TypeError, ValueError):
                first_seen = now
            entry["first_seen_at"] = first_seen
            entry["last_seen_at"] = now
        else:
            # New tool — apply the current default policy.
            entry["user_enabled"] = bool(default_enabled)
            entry["first_seen_at"] = now
            entry["last_seen_at"] = now

        merged.append(entry)

    # Sort alphabetically by name for deterministic output. Frontend
    # can override with its own sort (search, recently used, etc.)
    # but a stable base ordering makes diffing and log output nicer.
    merged.sort(key=lambda t: t["name"].lower())
    return merged


# ─────────────────────────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────────────────────────

def save_cache(
    server_id: str,
    config_hash: str,
    tools: List[Dict[str, Any]],
) -> None:
    """
    Persist the merged tool list for a server.

    The caller is expected to pass the output of ``merge_tools`` so
    that ``user_enabled`` decisions are preserved. This function does
    not re-merge — it trusts the caller to have done so.
    """
    container = {
        "version": CACHE_SCHEMA_VERSION,
        "server_id": server_id,
        "server_config_hash": config_hash,
        "fetched_at": time.time(),
        "tools": tools,
    }
    path = _cache_path(server_id)

    with _lock:
        try:
            _atomic_write_json(path, container)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to save tool cache for '%s' at %s: %s",
                server_id,
                path,
                exc,
            )
            raise


# ─────────────────────────────────────────────────────────────────
# Convenience: write a freshly discovered tool list end-to-end
# ─────────────────────────────────────────────────────────────────

def store_discovered_tools(
    server_id: str,
    config_hash: str,
    tools: List[Dict[str, Any]],
    default_enabled: bool,
) -> List[Dict[str, Any]]:
    """
    Merge the freshly discovered tools with the existing cache,
    persist the result, and return the merged list.

    This is the main entry point used by ``mcp_pool.get_tools`` after a
    successful discovery. It performs all three steps in one atomic
    operation:
      1. Load existing cache (may be None).
      2. Merge with the fresh discovery.
      3. Persist the merged list.

    The returned list is what the caller should use for the current
    request — it reflects the user's decisions immediately, without
    requiring a re-read.
    """
    existing = load_cache(server_id)
    merged = merge_tools(existing, tools, default_enabled)
    save_cache(server_id, config_hash, merged)
    logger.info(
        "Stored %d tool(s) for '%s' (enabled: %d, disabled: %d).",
        len(merged),
        server_id,
        sum(1 for t in merged if t.get("user_enabled")),
        sum(1 for t in merged if not t.get("user_enabled")),
    )
    return merged


# ─────────────────────────────────────────────────────────────────
# Per-tool enable/disable
# ─────────────────────────────────────────────────────────────────

def set_tool_enabled(
    server_id: str,
    tool_name: str,
    enabled: bool,
) -> bool:
    """
    Flip a single tool's ``user_enabled`` flag.

    Returns True when the tool was found and updated, False otherwise.
    """
    cache = load_cache(server_id)
    if not cache:
        return False

    found = False
    for entry in cache["tools"]:
        if isinstance(entry, dict) and entry.get("name") == tool_name:
            entry["user_enabled"] = bool(enabled)
            found = True
            break
    if not found:
        return False

    path = _cache_path(server_id)
    with _lock:
        _atomic_write_json(path, cache)
    logger.info(
        "Tool '%s' on server '%s' %s.",
        tool_name,
        server_id,
        "enabled" if enabled else "disabled",
    )
    return True


def set_all_tools_enabled(server_id: str, enabled: bool) -> int:
    """
    Flip ``user_enabled`` for every tool of a server.

    Returns the number of tools that were updated.
    """
    cache = load_cache(server_id)
    if not cache:
        return 0

    count = 0
    for entry in cache["tools"]:
        if isinstance(entry, dict):
            entry["user_enabled"] = bool(enabled)
            count += 1

    if count == 0:
        return 0

    path = _cache_path(server_id)
    with _lock:
        _atomic_write_json(path, cache)
    logger.info(
        "Set %d tool(s) on server '%s' to %s.",
        count,
        server_id,
        "enabled" if enabled else "disabled",
    )
    return count


# ─────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────

def get_enabled_tools(server_id: str) -> List[Dict[str, Any]]:
    """
    Return only the tools the user has marked ``user_enabled=True``.

    Used by ``peldrun_engine`` to build ``AgentSpec.tools`` — disabled
    tools are simply not advertised to the LLM at all, per design
    decision D2.
    """
    cache = load_cache(server_id)
    if not cache:
        return []
    return [
        t for t in cache["tools"]
        if isinstance(t, dict) and t.get("user_enabled")
    ]


def list_cached_servers() -> List[Dict[str, Any]]:
    """
    Summary view for the settings UI: one entry per cached server.
    Does not include tool details (the UI fetches those per-server).
    """
    out: List[Dict[str, Any]] = []
    for path in sorted(_cache_dir().glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            continue
        enabled = sum(1 for t in tools if isinstance(t, dict) and t.get("user_enabled"))
        out.append({
            "server_id": data.get("server_id") or path.stem,
            "fetched_at": data.get("fetched_at"),
            "total_tools": len(tools),
            "enabled_tools": enabled,
            "disabled_tools": len(tools) - enabled,
        })
    return out


# ─────────────────────────────────────────────────────────────────
# Resources & Prompts (MCP 2025-11-25 §5, §6)
# ─────────────────────────────────────────────────────────────────
#
# Resources and prompts share the per-server cache container with
# tools, but live in their own top-level keys (``resources`` and
# ``prompts``). They are never mixed with tools because the tool
# toggle UI operates on tools only — resources/prompts are
# read-only metadata.
#
# Rationale for a single file:
#   * A single read serves ``/tools/cached``, ``/resources/cached``,
#     and ``/prompts/cached`` — three endpoints, one disk touch.
#   * Cache freshness (TTL + config hash + schema version) is
#     evaluated once for all three primitives. There is no way for
#     tools to be fresh while resources are stale.
#   * Deleting a server invalidates one file, not three.
# ─────────────────────────────────────────────────────────────────

def get_resources(server_id: str) -> List[Dict[str, Any]]:
    """
    Return cached resources for a server, or an empty list.

    Never raises: a missing or malformed cache yields an empty list
    so the caller can fall back to live discovery or show "none".
    """
    cache = load_cache(server_id)
    if not cache:
        return []
    items = cache.get("resources")
    if not isinstance(items, list):
        return []
    return [r for r in items if isinstance(r, dict)]


def get_prompts(server_id: str) -> List[Dict[str, Any]]:
    """
    Return cached prompts for a server, or an empty list.

    Mirrors ``get_resources``: fail-soft, never raises.
    """
    cache = load_cache(server_id)
    if not cache:
        return []
    items = cache.get("prompts")
    if not isinstance(items, list):
        return []
    return [p for p in items if isinstance(p, dict)]


def store_discovered_resources(
    server_id: str,
    config_hash: str,
    resources: List[Dict[str, Any]],
) -> None:
    """
    Persist resources for a server.

    Merges into the existing cache container so tools/prompts
    written under the same version are preserved. The
    ``fetched_at`` field is NOT updated here — the caller controls
    that (typically ``store_discovered_tools`` runs first and sets
    it, then resources/prompts are attached to the same snapshot).

    A missing container is created fresh — this happens when the
    very first discovery is a resources-only server with zero
    tools, an edge case that would otherwise leave no file on disk.
    """
    container = load_cache(server_id) or {
        "version": CACHE_SCHEMA_VERSION,
        "server_id": server_id,
        "server_config_hash": config_hash,
        "fetched_at": time.time(),
        "tools": [],
        "resources": [],
        "prompts": [],
    }
    container["resources"] = [
        r for r in resources if isinstance(r, dict)
    ]
    container["version"] = CACHE_SCHEMA_VERSION
    container["server_config_hash"] = config_hash
    path = _cache_path(server_id)
    with _lock:
        _atomic_write_json(path, container)
    logger.info(
        "Stored %d resource(s) for '%s'.",
        len(container["resources"]),
        server_id,
    )


def store_discovered_prompts(
    server_id: str,
    config_hash: str,
    prompts: List[Dict[str, Any]],
) -> None:
    """
    Persist prompts for a server (mirrors
    ``store_discovered_resources`` — same merge semantics, same
    fail-soft on malformed input).
    """
    container = load_cache(server_id) or {
        "version": CACHE_SCHEMA_VERSION,
        "server_id": server_id,
        "server_config_hash": config_hash,
        "fetched_at": time.time(),
        "tools": [],
        "resources": [],
        "prompts": [],
    }
    container["prompts"] = [
        p for p in prompts if isinstance(p, dict)
    ]
    container["version"] = CACHE_SCHEMA_VERSION
    container["server_config_hash"] = config_hash
    path = _cache_path(server_id)
    with _lock:
        _atomic_write_json(path, container)
    logger.info(
        "Stored %d prompt(s) for '%s'.",
        len(container["prompts"]),
        server_id,
    )


__all__ = [
    "compute_config_hash",
    "load_cache",
    "invalidate",
    "is_cache_fresh",
    "merge_tools",
    "save_cache",
    "store_discovered_tools",
    "set_tool_enabled",
    "set_all_tools_enabled",
    "get_enabled_tools",
    "list_cached_servers",
    # ── MCP 2025-11-25 §5/§6: resources + prompts ──
    "get_resources",
    "get_prompts",
    "store_discovered_resources",
    "store_discovered_prompts",
]
