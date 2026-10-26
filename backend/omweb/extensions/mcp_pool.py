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

    def _get_session_key(self, server_id: str, workspace_root: Optional[str] = None) -> str:
        """Construct a unique cache key per server and isolated workspace root."""
        normalized_root = str(Path(workspace_root).resolve()) if workspace_root else "default"
        return f"{server_id}:{normalized_root}"

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
    ) -> Tuple[Optional[BaseMCPTransport], Optional[str]]:
        """
        Construct the designated MCP transport based on manifest specification.
        Returns a tuple of (transport_instance, fallback_http_url).
        """
        server_id = manifest.get("id", "unknown")
        transport_type = manifest.get("transport", "stdio").lower()
        command = manifest.get("command")
        target_url = manifest.get("url")

        runtime_config = mcp_settings_manager.get_effective_runtime_config(server_id, manifest)
        settings = runtime_config.get("settings", {})
        env_vars = {str(k): str(v) for k, v in runtime_config.get("env", {}).items()}

        if transport_type == "stdio":
            if not command:
                raise ValueError(
                    f"MCP server '{server_id}' specifies 'stdio' transport but lacks 'command'."
                )

            executable, args = self._build_command_args(command, settings, workspace_root)
            transport = StdioTransport(
                command=executable,
                args=args,
                env=env_vars if env_vars else None,
                cwd=workspace_root,
                timeout=float(manifest.get("timeout", 30.0)),
            )
            return transport, None

        elif transport_type in ("sse", "http"):
            if not target_url:
                raise ValueError(
                    f"MCP server '{server_id}' specifies '{transport_type}' transport but lacks 'url'."
                )

            if transport_type == "sse" and SSETransport is not None:
                transport = SSETransport(
                    url=target_url,
                    headers={"Content-Type": "application/json"},
                    timeout=float(manifest.get("timeout", 30.0)),
                )
                return transport, None

            # Fallback to direct HTTP JSON-RPC client
            return None, target_url

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
    ) -> MCPClient:
        """
        Retrieve an active MCPClient instance or initialize a new one thread-safely.

        Concurrency model:
          1. Acquire the per-session lock (serialises lifecycle work for a key).
          2. Under the global lock, refuse if the key is being closed —
             this is the guard that prevents a close_session racing with
             a get_or_create_client from leaking a subprocess.
          3. Reuse healthy cached clients; re-create dead stdio processes.
          4. Before storing the new client, re-check the closing set under
             the global lock.
        """
        session_key = self._get_session_key(server_id, workspace_root)

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

            transport, fallback_url = self.build_transport(manifest, workspace_root)
            auth_token = manifest.get("auth_token")

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
    ) -> List[Dict[str, Any]]:
        """
        Return the discovered tool list for a server, using a per-server cache.

        Unlike probe_connection (which is a one-shot diagnostic), this
        method is intended for the hot path: it does NOT spawn a fresh
        subprocess on every call and it does NOT silently swallow errors.
        A failed tools/list raises — callers may then surface the error
        or fall back to a degraded toolset. Failed probes are never cached.

        Cache invalidation channels:
          * Explicit: invalidate_cache(server_id) — invoked by
            ExtensionRegistry.save_extension / toggle_status when the
            transport configuration changes.
          * TTL fallback: after `cache_ttl_seconds` a fresh tools/list
            is issued on the next call.
        """
        cache_key = server_id

        if not force_refresh:
            cached = self._tool_cache.get(cache_key)
            if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
                return list(cached["tools"])

        # Serialise concurrent probes for the same server so N callers
        # produce 1 network round-trip instead of N.
        probe_key = f"tool_probe:{server_id}"
        async with self._global_lock:
            if probe_key not in self._locks:
                self._locks[probe_key] = asyncio.Lock()
            probe_lock = self._locks[probe_key]

        async with probe_lock:
            # Double-check under lock (another caller may have just populated it).
            if not force_refresh:
                cached = self._tool_cache.get(cache_key)
                if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
                    return list(cached["tools"])

            client = await self.get_or_create_client(
                server_id=server_id,
                manifest=manifest,
                workspace_root=workspace_root,
            )

            # Direct JSON-RPC call so that failures are surfaced to the
            # caller — MCPClient.discover_tools would silently return [].
            result = await client._send_jsonrpc_request("tools/list")
            raw_tools = result.get("tools", []) if isinstance(result, dict) else []

            tools: List[Dict[str, Any]] = []
            for td in raw_tools:
                if not isinstance(td, dict):
                    continue
                name = td.get("name")
                if not name:
                    continue
                tools.append({
                    "name": str(name),
                    "description": str(td.get("description") or ""),
                    "parameters": td.get("inputSchema") or {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                })

            self._tool_cache[cache_key] = {
                "tools": tools,
                "fetched_at": time.monotonic(),
            }
            logger.info(
                "Cached %d tool(s) for server '%s' (workspace: %s)",
                len(tools),
                server_id,
                workspace_root or "default",
            )
            return list(tools)

    async def invalidate_cache(self, server_id: str) -> None:
        """Drop any cached tool list for the given server."""
        async with self._global_lock:
            self._tool_cache.pop(server_id, None)

    def invalidate_cache_sync(self, server_id: str) -> None:
        """
        Synchronous cache invalidation for callers outside the async loop.

        Used by ``McpBundleManager.install_bundle`` (synchronous by
        contract) to evict a stale tool list when a bundle is being
        re-uploaded under the same id. The underlying ``dict.pop`` is
        atomic under the GIL, so no lock is required here — and
        acquiring the async lock from a sync context is impossible
        without an event loop.
        """
        self._tool_cache.pop(server_id, None)

    def get_cached_tools(
        self, server_id: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Return the cached tool list for a server without any I/O.

        Used by the delegation resolver (which is synchronous by design)
        to expand bare server IDs into concrete MCP tool IDs. Returns
        ``None`` if the cache is cold or expired, so callers can fall
        back gracefully instead of blocking on a subprocess spawn.
        """
        cached = self._tool_cache.get(server_id)
        if cached and (time.monotonic() - cached["fetched_at"]) < self._cache_ttl:
            return list(cached["tools"])
        return None

    # ─────────────────────────────────────────────────────────────
    # Probe (strict one-shot diagnostic)
    # ─────────────────────────────────────────────────────────────

    async def probe_connection(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
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
            transport, fallback_url = self.build_transport(manifest, workspace_root)
            client = MCPClient(
                server_url=fallback_url,
                auth_token=manifest.get("auth_token"),
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
