# backend/omweb/extensions/mcp_pool.py
"""
Centralized Connection Pool and Runtime Session Manager for MCP Servers.

Responsibilities:
  * Maintain persistent MCPClient instances over stdio and sse/http transports.
  * Integrate runtime configuration and environment variables from McpSettingsManager.
  * Execute real JSON-RPC handshakes and dynamic tool discovery.
  * Provide robust process cleanup and prevent leaked background subprocesses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from peldrun.tools.mcp_client import BaseMCPTransport, DynamicMCPTool, MCPClient
from peldrun.tools.transports.stdio import StdioTransport
from omweb.extensions.settings_manager import mcp_settings_manager

try:
    from peldrun.tools.transports.sse import SSETransport
except ImportError:
    SSETransport = None

logger = logging.getLogger("omweb.extensions.mcp_pool")


class McpSessionPool:
    """Manages persistent active connections and dynamic sessions for MCP servers."""

    def __init__(self) -> None:
        self._clients: Dict[str, MCPClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _get_session_key(self, server_id: str, workspace_root: Optional[str] = None) -> str:
        """Construct a unique cache key per server and isolated workspace root."""
        normalized_root = str(Path(workspace_root).resolve()) if workspace_root else "default"
        return f"{server_id}:{normalized_root}"

    def _build_command_args(
        self,
        command_str: str,
        settings: Dict[str, Any],
        workspace_root: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        """
        Parse raw command string and append configured arguments and directory targets.
        """
        if os.name == "nt":
            cmd_parts = shlex.split(command_str, posix=False)
        else:
            cmd_parts = shlex.split(command_str)

        if not cmd_parts:
            raise ValueError("Command string cannot be empty.")

        executable = cmd_parts[0]
        resolved_exe = shutil.which(executable) or executable
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
                raise ValueError(f"MCP server '{server_id}' specifies 'stdio' transport but lacks 'command'.")

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
                raise ValueError(f"MCP server '{server_id}' specifies '{transport_type}' transport but lacks 'url'.")

            if transport_type == "sse" and SSETransport is not None:
                transport = SSETransport(
                    url=target_url,
                    headers={"Content-Type": "application/json"},
                    timeout=float(manifest.get("timeout", 30.0)),
                )
                return transport, None

            # Fallback to direct HTTP JSON-RPC client
            return None, target_url

        raise ValueError(f"Unsupported transport type '{transport_type}' for MCP server '{server_id}'.")

    async def get_or_create_client(
        self,
        server_id: str,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
    ) -> MCPClient:
        """
        Retrieve an active MCPClient instance or initialize a new one thread-safely.
        """
        session_key = self._get_session_key(server_id, workspace_root)

        async with self._global_lock:
            if session_key not in self._locks:
                self._locks[session_key] = asyncio.Lock()
            session_lock = self._locks[session_key]

        async with session_lock:
            existing_client = self._clients.get(session_key)
            if existing_client is not None:
                # Validate if stdio subprocess is still healthy
                if isinstance(existing_client.transport, StdioTransport):
                    proc = existing_client.transport._process
                    if proc is not None and proc.returncode is None:
                        return existing_client
                    logger.warning("MCP subprocess for '%s' terminated. Re-initializing...", server_id)
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

            # Eagerly connect transport if applicable
            if transport is not None:
                await transport.aconnect()

            self._clients[session_key] = client
            logger.info("Initialized active MCPClient session for '%s' (key: %s)", server_id, session_key)
            return client

    async def probe_connection(
        self,
        manifest: Dict[str, Any],
        workspace_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Perform an isolated one-shot connectivity check and discover exposed tools.
        Guarantees prompt cleanup of spawned subprocesses.
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

            discovered_tools = await client.discover_tools(workspace_root=workspace_root)
            tool_summaries = [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": getattr(t, "_raw_schema", {}),
                }
                for t in discovered_tools
            ]

            return {
                "status": "ok",
                "message": f"Successfully connected to MCP server. Discovered {len(discovered_tools)} tool(s).",
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

    async def _safe_close_client(self, client: MCPClient) -> None:
        """Safely close client transport and ignore benign teardown exceptions."""
        try:
            await client.close()
        except Exception as exc:
            logger.debug("Error while closing MCPClient session: %s", exc)

    async def close_session(self, server_id: str, workspace_root: Optional[str] = None) -> None:
        """Terminate and remove a specific active MCP session."""
        session_key = self._get_session_key(server_id, workspace_root)
        async with self._global_lock:
            client = self._clients.pop(session_key, None)
            self._locks.pop(session_key, None)

        if client is not None:
            await self._safe_close_client(client)
            logger.info("Closed active MCP session for key '%s'", session_key)

    async def close_all(self) -> None:
        """Terminate all active MCP sessions across the platform."""
        async with self._global_lock:
            clients_to_close = list(self._clients.values())
            self._clients.clear()
            self._locks.clear()

        for client in clients_to_close:
            await self._safe_close_client(client)
        logger.info("Closed all active MCP pool sessions.")


mcp_session_pool = McpSessionPool()

__all__ = ["McpSessionPool", "mcp_session_pool"]
