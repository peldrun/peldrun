"""
PELDRUN Core Model Context Protocol (MCP) Client.
Provides asynchronous JSON-RPC client integration, dynamic MCP tool generation,
pluggable transport layers, and seamless compatibility with BaseTool and ToolRegistry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union
import httpx
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult

logger = logging.getLogger("peldrun.tools.mcp_client")


class BaseMCPTransport(ABC):
    """Abstract base contract for MCP communication transports."""

    @abstractmethod
    async def aconnect(self) -> None:
        """Establish connection to the MCP server."""
        ...

    @abstractmethod
    async def asend_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a JSON-RPC 2.0 request and return the parsed result."""
        ...

    @abstractmethod
    async def aclose(self) -> None:
        """Close the transport session and release underlying resources."""
        ...


class MCPToolDefinition(BaseModel):
    """Metadata describing an external tool exposed by an MCP server."""
    name: str = Field(..., description="Unique tool identifier")
    description: str = Field(default="", description="Functional description of tool")
    inputSchema: Dict[str, Any] = Field(default_factory=dict, description="JSON Schema of parameters")


class DynamicMCPTool(BaseTool):
    """
    Bridge tool dynamically wrapping a remote MCP server tool.
    Inherits from BaseTool to ensure 100% interoperability with ToolRegistry.
    """

    def __init__(
        self,
        name: str,
        description: str,
        mcp_client: MCPClient,
        input_schema: Optional[Dict[str, Any]] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.name = name
        self.description = description
        self.mcp_client = mcp_client
        self._raw_schema = input_schema or {"type": "object", "properties": {}}

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convert MCP tool JSON schema to standard OpenAI function-calling format."""
        parameters = {
            "type": self._raw_schema.get("type", "object"),
            "properties": self._raw_schema.get("properties", {}),
            "required": self._raw_schema.get("required", []),
        }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": parameters,
            },
        }

    async def _arun(self, **kwargs: Any) -> ToolResult:
        """Forward invocation to the remote MCP server client."""
        return await self.mcp_client.call_tool(self.name, kwargs)


class MCPClient:
    """
    Asynchronous client communicating with an external Model Context Protocol server.
    Supports tool discovery and invocation over JSON-RPC 2.0 via HTTP or pluggable transports.
    """

    def __init__(
        self,
        server_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[BaseMCPTransport] = None,
        http_transport: Optional[httpx.AsyncBaseTransport] = None,
        # ── mTLS support (MCP 2025-11-25 §auth / Enterprise) ────
        client_cert: Optional[Union[str, Tuple[str, str]]] = None,
        ca_bundle: Optional[str] = None,
    ) -> None:
        """
        ``client_cert`` accepts either:
          * a single path to a combined cert+key .pem file, or
          * a ``(cert_path, key_path)`` tuple for separate files.

        ``ca_bundle`` is a custom CA certificate file for servers
        using an internal PKI. When omitted, httpx uses the system
        trust store (ssl.create_default_context()).

        Both arguments are only meaningful for HTTP/SSE transports.
        For stdio transports they are silently ignored — a subprocess
        cannot participate in a TLS handshake with the parent.
        """
        self.server_url = server_url.rstrip("/") if server_url else ""
        self.auth_token = auth_token
        self.timeout = timeout
        self.transport = transport
        self._http_transport = http_transport
        self._client_cert = client_cert
        self._ca_bundle = ca_bundle
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        # MCP Streamable HTTP session management (§4.1).
        # The server issues Mcp-Session-Id on the initialize response;
        # every subsequent request MUST carry it. Servers that use SSE
        # for tools/list will reject requests without it.
        self._session_id: Optional[str] = None
        self._initialized: bool = False
        self._init_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Lazy-initialize HTTP client session for direct HTTP endpoints.

        IMPORTANT — no ``base_url`` is used here, on purpose.
        ``httpx`` merges ``base_url`` with the request path by URL
        joining. A POST with an empty path (``post("")``) resolves to
        ``base_url + "/"``, which breaks Streamable HTTP endpoints
        such as ``https://mcp.notion.com/mcp`` — Notion returns 404
        for ``/mcp/`` (with trailing slash) and 200 for ``/mcp``.

        By omitting ``base_url`` and posting to the absolute URL in
        ``_send_jsonrpc_request``, the exact path is preserved.

        mTLS support (MCP 2025-11-25 §auth): when ``client_cert`` was
        supplied, httpx presents it during the TLS handshake.
        ``ca_bundle`` overrides the trust store when the server uses
        a private CA. Both are only meaningful here — the stdio path
        never reaches this method.
        """
        async with self._lock:
            if self._client is None or self._client.is_closed:
                headers = {
                    "Content-Type": "application/json",
                    # MCP Streamable HTTP §3 requires the client to
                    # advertise both content types. Servers use this
                    # to decide between an SSE-streamed response and
                    # a plain JSON response.
                    "Accept": "application/json, text/event-stream",
                }
                if self.auth_token:
                    headers["Authorization"] = f"Bearer {self.auth_token}"

                client_kwargs: Dict[str, Any] = {
                    "headers": headers,
                    "timeout": httpx.Timeout(self.timeout),
                    "transport": self._http_transport,
                }

                # ── mTLS wiring ──────────────────────────────────
                # httpx passes ``cert`` straight through to
                # ssl.SSLContext.load_cert_chain(), which accepts
                # either a single combined .pem or a (cert, key)
                # pair. We normalise to that contract here so the
                # caller can pass either form without worrying.
                if self._client_cert is not None:
                    try:
                        if (
                            isinstance(self._client_cert, (tuple, list))
                            and len(self._client_cert) == 2
                        ):
                            client_kwargs["cert"] = (
                                str(self._client_cert[0]),
                                str(self._client_cert[1]),
                            )
                        else:
                            client_kwargs["cert"] = str(self._client_cert)
                    except Exception as _cert_err:  # noqa: BLE001
                        # A malformed cert path must not break the
                        # client silently — log a warning and fall
                        # back to no client cert. The subsequent TLS
                        # handshake will fail with a clear SSLError
                        # rather than an opaque TypeError here.
                        logger.warning(
                            "Invalid client_cert for '%s': %s",
                            self.server_url,
                            _cert_err,
                        )

                if self._ca_bundle:
                    # A private CA bundle replaces the default trust
                    # store. httpx accepts a filesystem path here.
                    client_kwargs["verify"] = str(self._ca_bundle)

                self._client = httpx.AsyncClient(**client_kwargs)
            return self._client

    async def close(self) -> None:
        """Release underlying network and transport connections."""
        async with self._lock:
            if self.transport is not None:
                await self.transport.aclose()
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
                self._client = None
            # Clear session state so a fresh client can re-initialize
            # on the next connection without stale Mcp-Session-Id.
            self._session_id = None
            self._initialized = False

    @staticmethod
    def _parse_sse_body(text: str) -> Optional[Dict[str, Any]]:
        """
        Parse an SSE response body and return the first JSON payload.

        MCP Streamable HTTP servers respond to POST requests with
        ``text/event-stream`` containing one or more ``data:`` frames.
        We look for the first frame whose payload is a complete JSON
        object and return it. Non-JSON payloads (e.g. the
        ``notifications/initialized`` ack) are skipped.

        Returns None when no JSON frame exists.
        """
        if not text:
            return None
        # Split on double newline (SSE event separator) OR single newline
        # (some servers omit blank lines between events — defensive).
        candidates: list[str] = []
        for raw_line in text.replace("\r\n", "\n").split("\n"):
            line = raw_line.strip()
            if line.startswith("data:"):
                candidates.append(line[5:].strip())
        for payload in candidates:
            if not payload or payload.startswith("["):
                continue
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
        return None

    async def _parse_response(
        self, response: httpx.Response
    ) -> Dict[str, Any]:
        """
        Extract a JSON-RPC envelope from an HTTP response.

        Handles three shapes:
          1. ``application/json`` — parse as-is.
          2. ``text/event-stream`` — parse the first ``data:`` frame.
          3. Empty body with 202 Accepted — raise a clear error
             (some servers want a GET on a separate stream endpoint,
             which this client does not implement).
        """
        content_type = (response.headers.get("Content-Type") or "").lower()

        if "text/event-stream" in content_type:
            data = self._parse_sse_body(response.text)
            if data is None:
                raise ConnectionError(
                    f"SSE response from {self.server_url} contained no "
                    f"JSON data frames. Body preview: "
                    f"{response.text[:200]!r}"
                )
            return data

        # Try JSON first (most common for tools/list).
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Fall back to SSE parsing (some servers mislabel Content-Type).
        data = self._parse_sse_body(response.text)
        if data is not None:
            return data

        raise ConnectionError(
            f"MCP server {self.server_url} returned an unparseable "
            f"response (status {response.status_code}, "
            f"Content-Type={content_type!r}, "
            f"body preview: {response.text[:200]!r})"
        )

    async def _ensure_initialized(self) -> None:
        """
        Perform the MCP ``initialize`` handshake (once per session).

        MCP Streamable HTTP §4.1 requires the client to call
        ``initialize`` before any other method. The server responds
        with an ``Mcp-Session-Id`` header that must be included on
        every subsequent request. Notion enforces this strictly.

        The subsequent ``notifications/initialized`` message (a
        one-way notification) is sent best-effort; some servers
        require it, but the acknowledgment is not JSON — the client
        must not block on a response.
        """
        async with self._init_lock:
            if self._initialized:
                return

            # Only auto-initialize for direct HTTP mode (not for
            # pluggable transports, which manage their own lifecycle).
            if self.transport is not None:
                self._initialized = True
                return
            if not self.server_url:
                raise ValueError(
                    "MCPClient cannot initialize without a server_url."
                )

            client = await self._get_client()
            request_id = str(uuid.uuid4())
            init_payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "PELDRUN MCP Client",
                        "version": "1.0.0",
                    },
                },
            }

            try:
                resp = await client.post(self.server_url, json=init_payload)
                resp.raise_for_status()

                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                    logger.debug(
                        "MCP session ID acquired from '%s': %s",
                        self.server_url,
                        sid[:12] + "…",
                    )

                # Parse ack (best-effort; content doesn't matter here).
                try:
                    await self._parse_response(resp)
                except Exception as ack_err:  # noqa: BLE001
                    logger.debug(
                        "Initialize ack parse warning for '%s': %s",
                        self.server_url,
                        ack_err,
                    )

                # Send notifications/initialized (one-way).
                notif_headers = dict(resp.headers)
                notif_headers.pop("Content-Length", None)
                notif_headers.pop("Content-Type", None)
                try:
                    await client.post(
                        self.server_url,
                        json={
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        },
                    )
                except Exception as notif_err:  # noqa: BLE001
                    logger.debug(
                        "notifications/initialized failed for '%s': %s",
                        self.server_url,
                        notif_err,
                    )

                self._initialized = True
                logger.info(
                    "MCP client initialized for '%s' (session=%s).",
                    self.server_url,
                    "yes" if self._session_id else "none",
                )

            except httpx.HTTPError as http_err:
                logger.error(
                    "MCP initialize failed for '%s': %s",
                    self.server_url,
                    http_err,
                )
                raise ConnectionError(
                    f"MCP initialize failed: {http_err}"
                ) from http_err

    async def _send_jsonrpc_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch a JSON-RPC 2.0 compliant request payload."""
        if self.transport is not None:
            return await self.transport.asend_request(method, params)

        # Initialize once (unless this IS the initialize call).
        if method != "initialize":
            await self._ensure_initialized()

        client = await self._get_client()
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        extra_headers: Dict[str, str] = {}
        if self._session_id:
            extra_headers["Mcp-Session-Id"] = self._session_id

        try:
            if not self.server_url:
                raise ValueError(
                    "MCPClient cannot send JSON-RPC without a server_url."
                )
            response = await client.post(
                self.server_url,
                json=payload,
                headers=extra_headers,
            )
            response.raise_for_status()

            # Capture a session ID that arrives on any response, not
            # just initialize — some servers only issue it lazily.
            sid = response.headers.get("Mcp-Session-Id")
            if sid and not self._session_id:
                self._session_id = sid

            data = await self._parse_response(response)

            if "error" in data:
                error_obj = data["error"]
                raise RuntimeError(
                    f"MCP RPC Error [{error_obj.get('code')}]: "
                    f"{error_obj.get('message')}"
                )

            return data.get("result", {})
        except httpx.HTTPError as http_err:
            logger.error(
                "HTTP error connecting to MCP server '%s': %s",
                self.server_url,
                http_err,
            )
            raise ConnectionError(
                f"Failed to communicate with MCP server: {http_err}"
            ) from http_err

    async def discover_tools(self, workspace_root: Optional[str] = None) -> List[DynamicMCPTool]:
        """
        Query the MCP server for available tools via 'tools/list'
        and return a list of initialized DynamicMCPTool instances.
        """
        try:
            result = await self._send_jsonrpc_request("tools/list")
            raw_tools = result.get("tools", [])
            tools: List[DynamicMCPTool] = []

            for tool_data in raw_tools:
                definition = MCPToolDefinition(**tool_data)
                tool_instance = DynamicMCPTool(
                    name=definition.name,
                    description=definition.description,
                    mcp_client=self,
                    input_schema=definition.inputSchema,
                    workspace_root=workspace_root,
                )
                tools.append(tool_instance)

            logger.info("Discovered %d tools from MCP server '%s'", len(tools), self.server_url)
            return tools
        except Exception as ex:
            logger.exception("Failed to discover tools from MCP server '%s': %s", self.server_url, ex)
            return []

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> ToolResult:
        """
        Execute an MCP tool remotely via 'tools/call'.
        Returns a structured ToolResult.

        MCP 2025-11-25 §7.2 allows the server to return BOTH:
          * ``content``            — human-readable blocks
                                     (text/image/resource).
          * ``structuredContent``  — machine-readable JSON matching
                                     the tool's ``outputSchema``.

        The previous implementation only read ``content``. Modern
        servers (Chainstack, Supabase, and many MCP 2025+ builds)
        put the authoritative result in ``structuredContent`` and
        use ``content`` only as a summary. Dropping
        ``structuredContent`` therefore discarded the entire result.

        Preferred output order:
          1. ``structuredContent`` (when present) — serialized with
             json.dumps so the LLM receives real, parseable data.
          2. ``content`` text blocks (fallback).
          3. Full raw result (last resort).

        Both forms are captured in ``metadata`` so callers can
        inspect whichever they need without re-parsing the output.
        """
        try:
            params = {
                "name": tool_name,
                "arguments": arguments,
            }
            result = await self._send_jsonrpc_request("tools/call", params)
            is_error = bool(result.get("isError", False))

            structured = result.get("structuredContent")
            content_blocks = result.get("content", []) or []

            text_outputs: List[str] = []
            for block in content_blocks:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        text_outputs.append(
                            block.get("text", "") or ""
                        )
                    elif btype == "resource":
                        # Embedded resource — stringify the payload
                        # so the LLM at least sees its shape. Binary
                        # blobs are represented by the serializer
                        # without inlining raw bytes.
                        res = block.get("resource")
                        text_outputs.append(
                            json.dumps(res, ensure_ascii=False)
                            if res is not None else ""
                        )
                    elif btype == "image":
                        # Binary image — reference only, never inline.
                        # The MIME type and approximate byte count
                        # are enough for the LLM to decide whether
                        # it wants to ask for a different format.
                        data_str = block.get("data") or ""
                        text_outputs.append(
                            f"[image {block.get('mimeType', 'unknown')} "
                            f"~{len(data_str)} bytes]"
                        )
                    else:
                        text_outputs.append(
                            json.dumps(block, ensure_ascii=False)
                        )
                else:
                    text_outputs.append(str(block))

            # Prefer structuredContent when present — this is the
            # authoritative payload the tool declared via
            # outputSchema. An empty dict (``{}``) is treated as
            # "no structured result" because some servers emit it
            # as a default placeholder.
            has_structured = (
                isinstance(structured, (dict, list))
                and structured != {}
                and structured != []
            )
            if has_structured:
                primary = json.dumps(structured, ensure_ascii=False)
                # Append the human summary after the structured
                # block so the LLM sees both the authoritative
                # JSON and the friendly text. A blank separator
                # keeps the two visually distinct.
                if text_outputs:
                    combined_output = (
                        primary + "\n\n" + "\n".join(text_outputs)
                    )
                else:
                    combined_output = primary
            else:
                combined_output = (
                    "\n".join(text_outputs) if text_outputs else str(result)
                )

            return ToolResult(
                output=combined_output,
                exit_code=1 if is_error else 0,
                is_error=is_error,
                metadata={
                    "mcp_server": self.server_url,
                    "raw_result": result,
                    "has_structured_content": has_structured,
                },
            )
        except Exception as ex:
            logger.exception("MCP call failed for tool '%s': %s", tool_name, ex)
            return ToolResult(
                output=f"MCP Invocation Error: {str(ex)}",
                exit_code=1,
                is_error=True,
                metadata={
                    "mcp_server": self.server_url,
                    "error_type": type(ex).__name__,
                },
            )

    # ─────────────────────────────────────────────────────────────
    # Resources API (MCP 2025-11-25 §5)
    # ─────────────────────────────────────────────────────────────

    async def list_resources(self) -> List[Dict[str, Any]]:
        """
        List readable resources exposed by this server.

        Resources are immutable, addressable data (files, documents,
        database rows) the agent can fetch by URI. Unlike tools they
        are not invoked with arguments — you read a specific URI.

        Returns a list of resource descriptors. Empty list when the
        server does not support resources or when the call fails.
        A failure here is not fatal: many servers (GitHub, Filesystem,
        WordPress) expose tools but no resources at all, and that
        is a perfectly valid MCP deployment.
        """
        try:
            result = await self._send_jsonrpc_request("resources/list")
            items = (
                result.get("resources", [])
                if isinstance(result, dict)
                else []
            )
            cleaned: List[Dict[str, Any]] = []
            for r in items:
                if not isinstance(r, dict):
                    continue
                uri = r.get("uri")
                if not uri:
                    continue
                cleaned.append({
                    "uri": str(uri),
                    "name": str(r.get("name") or uri),
                    "description": str(r.get("description") or ""),
                    "mimeType": str(r.get("mimeType") or "text/plain"),
                })
            return cleaned
        except Exception as ex:  # noqa: BLE001
            logger.debug(
                "resources/list failed for '%s' (may not be supported): %s",
                self.server_url,
                ex,
            )
            return []

    async def read_resource(self, uri: str) -> Optional[Dict[str, Any]]:
        """
        Read the contents of a resource by URI.

        Returns ``{"contents": [{uri, mimeType, text|blob}]}`` on
        success, or None on failure. The caller should treat None
        as "resource unavailable" — not as a fatal error, since some
        URIs are ephemeral or permission-gated.

        The response shape mirrors MCP's ``resources/read`` verbatim
        so callers can rely on the standard schema without
        re-wrapping.
        """
        try:
            result = await self._send_jsonrpc_request(
                "resources/read", {"uri": uri}
            )
            if not isinstance(result, dict):
                return None
            contents = result.get("contents")
            if not isinstance(contents, list):
                return None
            return {"contents": contents}
        except Exception as ex:  # noqa: BLE001
            logger.debug(
                "resources/read '%s' failed on '%s': %s",
                uri,
                self.server_url,
                ex,
            )
            return None

    # ─────────────────────────────────────────────────────────────
    # Prompts API (MCP 2025-11-25 §6)
    # ─────────────────────────────────────────────────────────────

    async def list_prompts(self) -> List[Dict[str, Any]]:
        """
        List parameterized prompt templates exposed by this server.

        Prompts are reusable templates (with optional arguments)
        that produce a sequence of messages. They are not invoked
        by the LLM directly — they appear in the UI as starting
        points for the user.

        Returns an empty list when the server does not support
        prompts, which is common for tool-only servers.
        """
        try:
            result = await self._send_jsonrpc_request("prompts/list")
            items = (
                result.get("prompts", [])
                if isinstance(result, dict)
                else []
            )
            cleaned: List[Dict[str, Any]] = []
            for p in items:
                if not isinstance(p, dict):
                    continue
                name = p.get("name")
                if not name:
                    continue
                args_raw = p.get("arguments") or []
                args_clean: List[Dict[str, Any]] = []
                if isinstance(args_raw, list):
                    for a in args_raw:
                        if isinstance(a, dict) and a.get("name"):
                            args_clean.append({
                                "name": str(a["name"]),
                                "description": str(
                                    a.get("description") or ""
                                ),
                                "required": bool(
                                    a.get("required", False)
                                ),
                            })
                cleaned.append({
                    "name": str(name),
                    "description": str(p.get("description") or ""),
                    "arguments": args_clean,
                })
            return cleaned
        except Exception as ex:  # noqa: BLE001
            logger.debug(
                "prompts/list failed for '%s' (may not be supported): %s",
                self.server_url,
                ex,
            )
            return []

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Materialize a prompt template with concrete argument values.

        Returns ``{description, messages: [{role, content}]}`` or
        None when the prompt is unavailable. The caller is
        responsible for rendering ``messages`` into the chat input.

        ``arguments`` is optional: a prompt with no declared
        arguments can be fetched with ``{}`` (or omitted entirely).
        """
        try:
            params: Dict[str, Any] = {"name": name}
            if arguments:
                params["arguments"] = arguments
            result = await self._send_jsonrpc_request(
                "prompts/get", params
            )
            if not isinstance(result, dict):
                return None
            return {
                "description": str(result.get("description") or ""),
                "messages": result.get("messages") or [],
            }
        except Exception as ex:  # noqa: BLE001
            logger.debug(
                "prompts/get '%s' failed on '%s': %s",
                name,
                self.server_url,
                ex,
            )
            return None
