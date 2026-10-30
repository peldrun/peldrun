# backend/omweb/extensions/mcp_pool.py
"""
Centralized Connection Pool and Runtime Session Manager for MCP Servers.

Responsibilities:
  * Maintain persistent MCPClient instances over stdio and sse/http transports.
  * Integrate runtime configuration and environment variables from McpSettingsManager.
  * Execute real JSON-RPC handshakes and dynamic tool discovery.
  * Provide robust process cleanup and prevent leaked background subprocesses.
  * Cache tool discovery results with explicit invalidation and a TTL fallback.
  * Serialize session lifecycle operations to prevent concurrent-close races
    (a close_session racing with an in-flight get_or_create_client would
    otherwise leave an orphaned subprocess behind).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from peldrun.tools.mcp_client import BaseMCPTransport, MCPClient
from peldrun.tools.transports.stdio import StdioTransport
from omweb.extensions.settings_manager import mcp_settings_manager

try:
    from peldrun.tools.transports.sse import SSETransport
except ImportError:
    SSETransport = None

logger = logging.getLogger("omweb.extensions.mcp_pool")


# How long a discovered tool list is considered fresh. After this window
# the next get_tools() call re-probes the server and refreshes the cache.
# Explicit invalidation (invalidate_cache) bypasses this window.
DEFAULT_CACHE_TTL_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Per-chat credential override contract
# ---------------------------------------------------------------------------
#
# When ``env_override`` is passed to get_or_create_client / build_transport,
# it FULLY REPLACES the manifest's global env. This is the per-chat
# isolation contract: a chat that chose "custom" credentials must not
# silently inherit any global variable.
#
# Two keys inside ``env_override`` are treated as META-keys rather than
# environment variables:
#
#   _oauth_access_token : Bearer token for HTTP/SSE transports.
#                         Never forwarded as an env var to stdio
#                         subprocesses (there is no HTTP there).
#   _oauth_token_type   : Authorization scheme, default "Bearer".
#
# Both participate in the session-key fingerprint so a token refresh
# produces a fresh client instead of silently reusing a stale session.
_OAUTH_ACCESS_TOKEN_KEY = "_oauth_access_token"
_OAUTH_TOKEN_TYPE_KEY = "_oauth_token_type"
_OAUTH_PREFIX = "_oauth_"


def _split_env_override(
    env_override: Optional[Dict[str, str]],
) -> Tuple[Optional[Dict[str, str]], Optional[str], str]:
    """
    Split an ``env_override`` into three components.

    Returns ``(real_env, access_token, token_type)``:

      * ``real_env``     — environment variables, or None if empty.
      * ``access_token`` — Bearer token for HTTP/SSE Authorization
                           header, or None if not supplied.
      * ``token_type``   — scheme, defaults to "Bearer".

    ``_oauth_*`` keys are stripped from ``real_env`` so a stdio
    subprocess never sees them as environment variables — a token that
    only makes sense for HTTP must not leak into a process env dump.
    """
    if not env_override:
        return None, None, "Bearer"

    real_env: Dict[str, str] = {}
    access_token: Optional[str] = None
    token_type = "Bearer"

    for key, value in env_override.items():
        sk = str(key)
        if sk == _OAUTH_ACCESS_TOKEN_KEY:
            access_token = str(value) if value is not None else None
            continue
        if sk == _OAUTH_TOKEN_TYPE_KEY:
            if value:
                token_type = str(value)
            continue
        if sk.startswith(_OAUTH_PREFIX):
            # Unknown meta-key — consumed here, never forwarded.
            continue
        real_env[str(sk)] = str(value) if value is not None else ""

    return (real_env or None), access_token, token_type


def _compute_creds_hash(
    env_override: Optional[Dict[str, str]],
) -> Optional[str]:
    """
    Fingerprint the effective credentials for session-key purposes.

    The hash covers ALL keys — including ``_oauth_access_token`` — so
    a token refresh yields a new session-key and a fresh client. The
    hash is truncated to 12 hex chars; collisions at this scale are
    astronomically unlikely within a single server pool.
    """
    if not env_override:
        return None
    try:
        payload = json.dumps(
            env_override, sort_keys=True, ensure_ascii=False
        )
    except (TypeError, ValueError):
        # Defensive fallback for pathological inputs (non-serialisable
        # values that should never occur given the type contract).
        payload = repr(
            sorted(
                ((str(k), str(v)) for k, v in env_override.items()),
                key=lambda kv: kv[0],
            )
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class McpSessionPool:
    """Manages persistent active connections and dynamic sessions for MCP servers."""

    def __init__(self, cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._clients: Dict[str, MCPClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

        # Session keys currently being torn down. get_or_create_client must
        # refuse to hand out a client whose key is in this set — otherwise
        # it would race with close_session / close_server_sessions and
        # leak a subprocess.
        self._closing: Set[str] = set()

        # Per-server cache of the discovered tools list.
        # Structure: {server_id: {"tools": [...], "fetched_at": float}}
        self._tool_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = float(cache_ttl_seconds)

    # ─────────────────────────────────────────────────────────────
    # Key construction
    # ─────────────────────────────────────────────────────────────

    def _get_session_key(
        self,
        server_id: str,
        workspace_root: Optional[str] = None,
        creds_hash: Optional[str] = None,
    ) -> str:
        """
        Construct a unique cache key per server, workspace, and
        credential set. The creds_hash segment is omitted when no
        env_override is in play, preserving the historical key shape
        for the common global-settings case.
        """
        normalized_root = str(Path(workspace_root).resolve()) if workspace_root else "default"
        base = f"{server_id}:{normalized_root}"
        return f"{base}:{creds_hash}" if creds_hash else base

    def _server_prefix(self, server_id: str) -> str:
        return f"{server_id}:"

    # ─────────────────────────────────────────────────────────────
    # Command preparation
    # ─────────────────────────────────────────────────────────────

    def _build_command_args(
        self,
        command_str: str,
        settings: Dict[str, Any],
        workspace_root: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        """
        Parse raw command string and append configured arguments and directory targets.

        Security / robustness hardening:
          * On Windows, shlex is invoked with posix=False; surrounding quotes
            are then stripped manually so that paths with spaces (e.g.
            "C:\\Program Files\\...") resolve correctly.
          * The resolved executable must exist in PATH or as a concrete file;
            otherwise we raise immediately with a clear error rather than
            spawning a doomed subprocess whose failure surface is opaque.
        """
        if os.name == "nt":
            cmd_parts = [
                p.strip('"').strip("'")
                for p in shlex.split(command_str, posix=False)
            ]
        else:
            cmd_parts = shlex.split(command_str)

        if not cmd_parts:
            raise ValueError("Command string cannot be empty.")

        executable = cmd_parts[0]
        resolved_exe = shutil.which(executable)
        if not resolved_exe:
            candidate = Path(executable)
            if candidate.is_file():
                resolved_exe = str(candidate.resolve())
            else:
                raise ValueError(
                    f"MCP server executable '{executable}' was not found in PATH "
                    f"and is not a valid file path."
                )

        args = cmd_parts[1:]

        # Handle filesystem server allowed directories binding
        allowed_dirs = settings.get("allowed_directories", [])
        if isinstance(allowed_dirs, list):
            for directory in allowed_dirs:
                if str(directory) not in args:
                    args.append(str(directory))

        # Fallback to workspace root if no directories specified for filesystem server
        if "@modelcontextprotocol/server-filesystem" in command_str and not allowed_dirs and workspace_root:
            if str(workspace_root) not in args:
                args.append(str(workspace_root))

        return resolved_exe, args

    # ─────────────────────────────────────────────────────────────
    # Transport building
    # ─────────────────────────────────────────────────────────────

    def build_transport(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        env_override: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[BaseMCPTransport], Optional[str], Optional[str]]:
        """
        Construct the designated MCP transport based on manifest specification.

        Returns ``(transport_instance, fallback_http_url, auth_token)``.

        ``env_override`` semantics (per-chat credential isolation):

          * When provided, it FULLY REPLACES the manifest's global env.
            No merge — a chat that chose "custom" credentials sees only
            those, never a mix.
          * Meta-keys (``_oauth_access_token``, ``_oauth_token_type``)
            are extracted here, not forwarded to the subprocess. The
            access token becomes an ``Authorization`` header for
            HTTP/SSE transports — this is the correct OAuth 2.1 wire
            format and is what the server expects.
          * For stdio transports, the access token is simply dropped:
            a stdio MCP server has no HTTP stack to receive it, and
            forwarding it as an env var would leak the secret into a
            process env dump.

        The third tuple slot (``auth_token``) is only non-None for the
        fallback HTTP JSON-RPC path — the SSE and stdio paths carry
        auth inside the transport instance.
        """
        server_id = manifest.get("id", "unknown")
        transport_type = manifest.get("transport", "stdio").lower()
        command = manifest.get("command")
        target_url = manifest.get("url")

        runtime_config = mcp_settings_manager.get_effective_runtime_config(server_id, manifest)
        settings = runtime_config.get("settings", {})

        if env_override is not None:
            # Full replacement: per-chat isolation.
            env_vars, oauth_token, token_type = _split_env_override(env_override)
            env_vars = env_vars or {}
        else:
            # Global fallback.
            env_vars = {str(k): str(v) for k, v in runtime_config.get("env", {}).items()}
            oauth_token = None
            token_type = "Bearer"

        if transport_type == "stdio":
            if not command:
                raise ValueError(
                    f"MCP server '{server_id}' specifies 'stdio' transport but lacks 'command'."
                )

            executable, args = self._build_command_args(command, settings, workspace_root)
            from omweb.extensions.mcp_subsystem_settings import (
                get_startup_timeout_seconds as _startup_timeout,
            )
            transport = StdioTransport(
                command=executable,
                args=args,
                env=env_vars if env_vars else None,
                cwd=workspace_root,
                timeout=float(
                    manifest.get("timeout") or _startup_timeout()
                ),
            )
            # Note: oauth_token is intentionally NOT forwarded to a
            # stdio subprocess — it is an HTTP-layer credential.
            return transport, None, None

        elif transport_type in ("sse", "http"):
            if not target_url:
                raise ValueError(
                    f"MCP server '{server_id}' specifies '{transport_type}' transport but lacks 'url'."
                )

            # Compose the outbound headers: static manifest headers
            # first, then the Authorization header from OAuth. The
            # merge order means a manifest-declared Authorization
            # header is overridden by a per-chat OAuth token — that
            # is deliberate: the per-chat decision is user-driven
            # and supersedes a static placeholder.
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            manifest_headers = manifest.get("headers")
            if isinstance(manifest_headers, dict):
                for hk, hv in manifest_headers.items():
                    headers[str(hk)] = str(hv)
            if oauth_token:
                headers["Authorization"] = f"{token_type} {oauth_token}"

            if transport_type == "sse" and SSETransport is not None:
                from omweb.extensions.mcp_subsystem_settings import (
                    get_startup_timeout_seconds as _startup_timeout,
                )
                transport = SSETransport(
                    url=target_url,
                    headers=headers,
                    timeout=float(
                        manifest.get("timeout") or _startup_timeout()
                    ),
                )
                return transport, None, None

            # Fallback to direct HTTP JSON-RPC client. The auth token
            # is surfaced so get_or_create_client can pass it to
            # MCPClient, which builds its own Authorization header.
            return None, target_url, oauth_token

        raise ValueError(
            f"Unsupported transport type '{transport_type}' for MCP server '{server_id}'."
        )

    # ─────────────────────────────────────────────────────────────
    # Client lifecycle
    # ─────────────────────────────────────────────────────────────

    async def get_or_create_client(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        env_override: Optional[Dict[str, str]] = None,
    ) -> MCPClient:
        """
        Retrieve an active MCPClient instance or initialize a new one thread-safely.

        ``env_override`` (per-chat credential isolation):

          * When provided, it fully replaces the manifest's global env
            (see build_transport for the exact contract).
          * The full override — including meta-keys — participates in
            the session-key hash, so a token refresh (which mutates
            ``_oauth_access_token``) produces a fresh client rather
            than reusing a session bound to the expired token.

        Concurrency model:
          1. Acquire the per-session lock (serialises lifecycle work for a key).
          2. Under the global lock, refuse if the key is being closed —
             this is the guard that prevents a close_session racing with
             a get_or_create_client from leaking a subprocess.
          3. Reuse healthy cached clients; re-create dead stdio processes.
          4. Before storing the new client, re-check the closing set under
             the global lock.
        """
        creds_hash = _compute_creds_hash(env_override)
        session_key = self._get_session_key(server_id, workspace_root, creds_hash)

        async with self._global_lock:
            if session_key not in self._locks:
                self._locks[session_key] = asyncio.Lock()
            session_lock = self._locks[session_key]

        async with session_lock:
            # Guard against concurrent close_session / close_server_sessions.
            async with self._global_lock:
                if session_key in self._closing:
                    raise RuntimeError(
                        f"MCP session '{session_key}' was closed concurrently; "
                        f"retry the operation."
                    )

            existing_client = self._clients.get(session_key)
            if existing_client is not None:
                # Validate if stdio subprocess is still healthy.
                if isinstance(existing_client.transport, StdioTransport):
                    proc = existing_client.transport._process
                    if proc is not None and proc.returncode is None:
                        return existing_client
                    logger.warning(
                        "MCP subprocess for '%s' terminated. Re-initializing...",
                        server_id,
                    )
                    await self._safe_close_client(existing_client)
                else:
                    return existing_client

            transport, fallback_url, effective_auth_token = self.build_transport(
                manifest, workspace_root, env_override
            )
            # A per-chat OAuth token supersedes the manifest's static
            # auth_token. Falling back to manifest.auth_token preserves
            # the historical behaviour for HTTP servers that were
            # pre-configured with a baked-in token.
            auth_token = effective_auth_token or manifest.get("auth_token")

            client = MCPClient(
                server_url=fallback_url,
                auth_token=auth_token,
                timeout=float(manifest.get("timeout", 30.0)),
                transport=transport,
            )

            # Eagerly connect transport if applicable.
            if transport is not None:
                await transport.aconnect()

            # Store under global lock and re-verify the closing guard.
            async with self._global_lock:
                if session_key in self._closing:
                    # Someone called close_* while we were connecting.
                    # Tear down what we just created and refuse to hand it out.
                    await self._safe_close_client(client)
                    raise RuntimeError(
                        f"MCP session '{session_key}' was closed concurrently; "
                        f"retry the operation."
                    )
                self._clients[session_key] = client

            logger.info(
                "Initialized active MCPClient session for '%s' (key: %s)",
                server_id,
                session_key,
            )
            return client

    # ─────────────────────────────────────────────────────────────
    # Tool discovery (cached)
    # ─────────────────────────────────────────────────────────────

    async def get_tools(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        *,
        force_refresh: bool = False,
        env_override: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return the discovered tool list for a server.

        Layered cache strategy:

          1. Subsystem kill-switch check — returns [] immediately when
             `enabled=False` in MCP subsystem settings.
          2. Persistent cache (survives backend restarts) — fast path,
             no disk I/O beyond reading one small JSON file.
          3. In-memory per-credential cache — fast path within a single
             process lifecycle (short TTL, ~5 minutes).
          4. Live discovery — spawns the subprocess, calls tools/list,
             merges result into the persistent cache (preserving
             per-tool `user_enabled` decisions), and mirrors the merged
             list into the in-memory cache.

        The returned list includes a `user_enabled` boolean per tool.
        The caller (peldrun_engine) uses it to decide whether to
        advertise the tool to the LLM. Disabled tools are still
        returned so the "Manage Tools" UI can display the full set.

        Set ``force_refresh=True`` to bypass both caches and re-probe
        the server (used by the "Refresh Tools" button in the UI).

        A failed ``tools/list`` raises. Failed probes are never cached;
        only successful discoveries update the persistent cache.
        """
        # Lazy imports keep mcp_pool importable without dragging the
        # whole settings/cache stack at module load time.
        from omweb.extensions.mcp_subsystem_settings import (
            is_enabled as _subsystem_enabled,
            get_tool_cache_ttl_seconds,
            get_max_tools_per_server,
            is_send_all_by_default,
        )
        from omweb.extensions.mcp_tool_cache import (
            compute_config_hash,
            load_cache as _load_persistent_cache,
            is_cache_fresh,
            store_discovered_tools,
        )

        # ── 1. Subsystem kill switch ─────────────────────────────
        if not _subsystem_enabled():
            logger.debug(
                "MCP subsystem is disabled; returning [] for '%s'.",
                server_id,
            )
            return []

        config_hash = compute_config_hash(manifest)
        ttl_seconds = get_tool_cache_ttl_seconds()

        # ── 2. Persistent cache fast path ────────────────────────
        if not force_refresh:
            persistent = _load_persistent_cache(server_id)
            if is_cache_fresh(persistent, config_hash, ttl_seconds):
                age = time.time() - float(persistent.get("fetched_at", 0))
                logger.debug(
                    "Persistent cache HIT for '%s' (age: %.1fs, tools: %d).",
                    server_id,
                    age,
                    len(persistent.get("tools", [])),
                )
                return list(persistent["tools"])

        # ── 3. In-memory cache fast path ─────────────────────────
        creds_hash = _compute_creds_hash(env_override)
        cache_key = f"{server_id}:{creds_hash}" if creds_hash else server_id

        if not force_refresh:
            cached = self._tool_cache.get(cache_key)
            if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
                return list(cached["tools"])

        # ── 4. Live discovery ────────────────────────────────────
        probe_key = f"tool_probe:{server_id}"
        async with self._global_lock:
            if probe_key not in self._locks:
                self._locks[probe_key] = asyncio.Lock()
            probe_lock = self._locks[probe_key]

        async with probe_lock:
            # Double-check both caches under the lock so concurrent
            # callers collapse into a single discovery.
            if not force_refresh:
                persistent = _load_persistent_cache(server_id)
                if is_cache_fresh(persistent, config_hash, ttl_seconds):
                    return list(persistent["tools"])

                cached = self._tool_cache.get(cache_key)
                if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
                    return list(cached["tools"])

            client = await self.get_or_create_client(
                server_id=server_id,
                manifest=manifest,
                workspace_root=workspace_root,
                env_override=env_override,
            )

            # Direct JSON-RPC call so failures surface to the caller.
            result = await client._send_jsonrpc_request("tools/list")
            raw_tools = result.get("tools", []) if isinstance(result, dict) else []

            # Cap at max_tools_per_server to protect against a
            # malformed server that returns thousands of entries.
            max_tools = get_max_tools_per_server()
            discovered: List[Dict[str, Any]] = []
            for td in raw_tools:
                if not isinstance(td, dict):
                    continue
                name = td.get("name")
                if not name:
                    continue
                discovered.append({
                    "name": str(name),
                    "description": str(td.get("description") or ""),
                    "parameters": td.get("inputSchema") or {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                })
                if len(discovered) >= max_tools:
                    logger.warning(
                        "Server '%s' returned more tools than max_tools_per_server=%d; capping.",
                        server_id,
                        max_tools,
                    )
                    break

            # Merge into persistent cache (preserves per-tool
            # user_enabled decisions; new tools default per
            # send_all_tools_by_default).
            default_enabled = is_send_all_by_default()
            merged = store_discovered_tools(
                server_id,
                config_hash,
                discovered,
                default_enabled,
            )

            # Mirror into in-memory cache so subsequent calls in the
            # same process avoid touching the disk.
            self._tool_cache[cache_key] = {
                "tools": merged,
                "fetched_at": time.monotonic(),
            }

            logger.info(
                "Discovered %d tool(s) for '%s' (enabled: %d, disabled: %d).",
                len(merged),
                server_id,
                sum(1 for t in merged if t.get("user_enabled")),
                sum(1 for t in merged if not t.get("user_enabled")),
            )

            # Lazy-start behavior: when enabled, close the discovery
            # subprocess so it does not sit idle. The next tool call
            # will spawn fresh. Best-effort — a failure to close does
            # not affect the returned tool list.
            try:
                from omweb.extensions.mcp_subsystem_settings import (
                    is_lazy_start_enabled as _is_lazy,
                )

                if _is_lazy():
                    await self.close_session(server_id, workspace_root)
                    logger.debug(
                        "Lazy start: closed discovery session for '%s'.",
                        server_id,
                    )
            except Exception as lazy_err:  # noqa: BLE001
                logger.debug(
                    "Lazy-start cleanup notice for '%s': %s",
                    server_id,
                    lazy_err,
                )

            return list(merged)

    async def invalidate_cache(self, server_id: str) -> None:
        """
        Drop every cached tool list for the given server, in BOTH the
        in-memory cache and the persistent on-disk cache.

        Used when the server's configuration changes in a way that
        could invalidate every cred-scoped entry at once (bundle
        reinstall, settings update, status toggle).
        """
        # 1. Drop the persistent cache file (best-effort).
        try:
            from omweb.extensions.mcp_tool_cache import (
                invalidate as _invalidate_persistent,
            )

            _invalidate_persistent(server_id)
        except Exception as persist_err:  # noqa: BLE001
            logger.debug(
                "Failed to invalidate persistent cache for '%s': %s",
                server_id,
                persist_err,
            )

        # 2. Drop every in-memory cred-scoped variant.
        prefix = f"{server_id}:"
        async with self._global_lock:
            keys_to_drop = [
                k for k in list(self._tool_cache.keys())
                if k == server_id or k.startswith(prefix)
            ]
            for k in keys_to_drop:
                self._tool_cache.pop(k, None)

    def invalidate_cache_sync(self, server_id: str) -> None:
        """
        Synchronous cache invalidation for callers outside the async loop.

        Used by ``McpBundleManager.install_bundle`` (synchronous by
        contract) to evict every cred-scoped variant of a stale tool
        list when a bundle is being re-uploaded under the same id.

        Invalidates BOTH the persistent on-disk cache and the
        in-memory cache. The underlying dict mutations are atomic under
        the GIL, so no lock is required here — and acquiring the async
        lock from a sync context is impossible without an event loop.
        """
        # 1. Persistent cache (best-effort).
        try:
            from omweb.extensions.mcp_tool_cache import (
                invalidate as _invalidate_persistent,
            )

            _invalidate_persistent(server_id)
        except Exception as persist_err:  # noqa: BLE001
            logger.debug(
                "Failed to invalidate persistent cache for '%s': %s",
                server_id,
                persist_err,
            )

        # 2. In-memory cred-scoped variants.
        prefix = f"{server_id}:"
        keys_to_drop = [
            k for k in list(self._tool_cache.keys())
            if k == server_id or k.startswith(prefix)
        ]
        for k in keys_to_drop:
            self._tool_cache.pop(k, None)

    def get_cached_tools(
        self, server_id: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Return the cached tool list for a server without spawning a
        subprocess.

        Layered lookup:
          1. In-memory entry for the bare server id.
          2. Any fresh cred-scoped in-memory variant.
          3. Persistent on-disk cache (used by the delegation resolver,
             which is synchronous and cannot `await` a live probe).

        Returns ``None`` when no cache (memory or disk) is available,
        so the caller can fall back gracefully.
        """
        # 1. In-memory (default key).
        cached = self._tool_cache.get(server_id)
        if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
            return list(cached["tools"])

        # 2. Any fresh cred-scoped in-memory variant.
        prefix = f"{server_id}:"
        for key, entry in self._tool_cache.items():
            if key.startswith(prefix) and (
                time.monotonic() - entry["fetched_at"]
            ) < self._cache_ttl:
                return list(entry["tools"])

        # 3. Persistent on-disk fallback (survives restarts).
        try:
            from omweb.extensions.mcp_tool_cache import (
                load_cache as _load_persistent_cache,
            )

            persistent = _load_persistent_cache(server_id)
            if persistent and isinstance(persistent.get("tools"), list):
                return list(persistent["tools"])
        except Exception as persist_err:  # noqa: BLE001
            logger.debug(
                "Persistent cache read failed for '%s': %s",
                server_id,
                persist_err,
            )

        return None

    # ─────────────────────────────────────────────────────────────
    # Probe (strict one-shot diagnostic)
    # ─────────────────────────────────────────────────────────────

    async def probe_connection(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
        env_override: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Perform an isolated one-shot connectivity check and discover exposed tools.

        This is a STRICT diagnostic: any failure (spawn error, RPC error,
        timeout) surfaces as {"status": "error"}. Previous behaviour would
        mask a failed tools/list as {"status": "ok", "tools": []} because
        MCPClient.discover_tools swallows exceptions internally.

        Guarantees prompt cleanup of any spawned subprocess.
        """
        server_id = manifest.get("id", "probe_test")
        client: Optional[MCPClient] = None
        try:
            transport, fallback_url, effective_auth_token = self.build_transport(
                manifest, workspace_root, env_override
            )
            client = MCPClient(
                server_url=fallback_url,
                auth_token=effective_auth_token or manifest.get("auth_token"),
                timeout=float(manifest.get("timeout", 15.0)),
                transport=transport,
            )

            if transport is not None:
                await transport.aconnect()

            result = await client._send_jsonrpc_request("tools/list")
            raw_tools = result.get("tools", []) if isinstance(result, dict) else []

            tool_summaries: List[Dict[str, Any]] = []
            for td in raw_tools:
                if not isinstance(td, dict):
                    continue
                name = td.get("name")
                if not name:
                    continue
                tool_summaries.append({
                    "name": str(name),
                    "description": str(td.get("description") or ""),
                    "parameters": td.get("inputSchema") or {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                })

            return {
                "status": "ok",
                "message": (
                    f"Successfully connected to MCP server. "
                    f"Discovered {len(tool_summaries)} tool(s)."
                ),
                "tools": tool_summaries,
            }
        except Exception as exc:
            logger.warning("MCP connection probe failed for '%s': %s", server_id, exc)
            return {
                "status": "error",
                "message": f"Connection failed: {str(exc)}",
                "tools": [],
            }
        finally:
            if client is not None:
                await self._safe_close_client(client)

    # ─────────────────────────────────────────────────────────────
    # Session teardown
    # ─────────────────────────────────────────────────────────────

    async def _safe_close_client(self, client: MCPClient) -> None:
        """Safely close client transport and ignore benign teardown exceptions."""
        try:
            await client.close()
        except Exception as exc:
            logger.debug("Error while closing MCPClient session: %s", exc)

    async def _close_session_by_key(self, session_key: str) -> None:
        """
        Internal teardown routine shared by close_session /
        close_server_sessions / close_all.

        Marks the key as closing BEFORE popping the client so that any
        concurrent get_or_create_client bails out instead of creating a
        second client for the same key.
        """
        async with self._global_lock:
            self._closing.add(session_key)
            client = self._clients.pop(session_key, None)

        try:
            if client is not None:
                await self._safe_close_client(client)
                logger.info("Closed active MCP session for key '%s'", session_key)
        finally:
            async with self._global_lock:
                self._closing.discard(session_key)
                self._locks.pop(session_key, None)

    async def close_session(
        self,
        server_id: str,
        workspace_root: Optional[str] = None,
    ) -> None:
        """Terminate and remove a specific active MCP session."""
        session_key = self._get_session_key(server_id, workspace_root)
        await self._close_session_by_key(session_key)

    async def close_server_sessions(self, server_id: str) -> None:
        """
        Terminate every active session belonging to a server, across every
        bound workspace_root. Used by delete_bundle / delete_extension /
        toggle / settings-update paths so that stale per-workspace
        subprocesses never survive a configuration change.
        """
        prefix = self._server_prefix(server_id)

        async with self._global_lock:
            session_keys = [
                k for k in list(self._clients.keys()) if k.startswith(prefix)
            ]

        # Also invalidate any cached tool list so the next call re-probes.
        await self.invalidate_cache(server_id)

        for key in session_keys:
            await self._close_session_by_key(key)

        # Drop any leftover probe lock for the server.
        async with self._global_lock:
            self._locks.pop(f"tool_probe:{server_id}", None)

        if session_keys:
            logger.info(
                "Closed %d session(s) for server '%s' across all workspaces.",
                len(session_keys),
                server_id,
            )

    async def close_all(self) -> None:
        """
        Terminate all active MCP sessions across the platform.

        Called from the FastAPI lifespan shutdown hook. Marking every key
        as closing before popping ensures no concurrent caller can sneak
        in a fresh session while we tear things down.
        """
        async with self._global_lock:
            session_keys = list(self._clients.keys())
            clients_to_close = list(self._clients.values())
            self._closing.update(session_keys)
            self._clients.clear()
            self._locks.clear()
            self._tool_cache.clear()

        for client in clients_to_close:
            await self._safe_close_client(client)

        async with self._global_lock:
            self._closing.clear()

        logger.info(
            "Closed %d active MCP pool session(s) during shutdown.",
            len(clients_to_close),
        )


mcp_session_pool = McpSessionPool()

__all__ = [
    "McpSessionPool",
    "mcp_session_pool",
    "DEFAULT_CACHE_TTL_SECONDS",
]
