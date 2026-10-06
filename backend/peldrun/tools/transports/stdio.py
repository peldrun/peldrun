"""
PELDRUN Core MCP Stdio Transport.
Executes external MCP servers as local subprocesses and communicates over standard input/output
using newline-delimited JSON-RPC 2.0 messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import uuid
from typing import Any, Dict, List, Optional

from peldrun.tools.mcp_client import BaseMCPTransport

logger = logging.getLogger("peldrun.tools.transports.stdio")


class StdioTransport(BaseMCPTransport):
    """
    Subprocess transport communicating with local MCP servers via standard input/output streams.
    """

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    async def aconnect(self) -> None:
        """Start the subprocess server."""
        async with self._lock:
            if self._process is None or self._process.returncode is not None:
                full_cmd = [self.command] + self.args
                merged_env = os.environ.copy()
                if self.env:
                    merged_env.update(self.env)

                logger.debug("Launching MCP stdio subprocess: %s", full_cmd)
                self._process = await asyncio.create_subprocess_exec(
                    *full_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=merged_env,
                    cwd=self.cwd,
                )

    async def asend_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Write JSON-RPC line to stdin and await response line from stdout."""
        await self.aconnect()

        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise ConnectionError("Stdio subprocess is not running.")

        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        encoded_line = (json.dumps(payload) + "\n").encode("utf-8")

        async with self._lock:
            try:
                self._process.stdin.write(encoded_line)
                await self._process.stdin.drain()

                raw_line = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=self.timeout,
                )
                if not raw_line:
                    raise ConnectionError("MCP stdio process closed stdout unexpectedly.")

                data = json.loads(raw_line.decode("utf-8").strip())
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"MCP Stdio Error [{err.get('code')}]: {err.get('message')}")

                return data.get("result", {})
            except asyncio.TimeoutError as timeout_err:
                logger.error("Stdio request timed out for method '%s'", method)
                raise TimeoutError(f"MCP stdio request timed out after {self.timeout}s") from timeout_err

    async def aclose(self) -> None:
        """Terminate the subprocess and release streams."""
        async with self._lock:
            if self._process is not None:
                try:
                    if self._process.stdin:
                        self._process.stdin.close()
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except Exception as ex:
                    logger.debug("Force killing stdio process: %s", ex)
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                finally:
                    self._process = None