"""
PELDRUN Core MCP SSE Transport.
Communicates with remote MCP servers over HTTP with Server-Sent Events (SSE).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional
import httpx

from peldrun.tools.mcp_client import BaseMCPTransport

logger = logging.getLogger("peldrun.tools.transports.sse")


class SSETransport(BaseMCPTransport):
    """
    HTTP/SSE transport connecting to remote Model Context Protocol endpoints.
    """

    def __init__(
        self,
        endpoint_url: str,
        auth_token: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                headers = {"Content-Type": "application/json"}
                if self.auth_token:
                    headers["Authorization"] = f"Bearer {self.auth_token}"

                self._client = httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                )
            return self._client

    async def aconnect(self) -> None:
        """Establish client session."""
        await self._get_client()

    async def asend_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch JSON-RPC request to the remote endpoint."""
        client = await self._get_client()
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        try:
            resp = await client.post(self.endpoint_url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err = data["error"]
                raise RuntimeError(f"MCP SSE Error [{err.get('code')}]: {err.get('message')}")

            return data.get("result", {})
        except httpx.HTTPError as ex:
            logger.error("HTTP SSE transport failure to %s: %s", self.endpoint_url, ex)
            raise ConnectionError(f"Failed to communicate with MCP SSE server: {ex}") from ex

    async def aclose(self) -> None:
        """Close HTTP client."""
        async with self._lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
                self._client = None