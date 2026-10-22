# backend/peldrun/tools/builtins/http_fetch.py
"""
PELDRUN Core HTTP Content Fetcher Tool.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

# Move imports to the top level to prevent reloading issues in asyncio loops
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool
from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

logger = logging.getLogger("peldrun.tools.builtins.http_fetch")


class HttpFetchArgs(BaseModel):
    """Canonical argument schema for the http_fetch tool."""
    url: str = Field(
        ...,
        min_length=1,
        description="Target HTTP or HTTPS URL to fetch.",
    )
    max_chars: Optional[int] = Field(
        default=None,
        ge=500,
        le=50000,
        description=(
            "Maximum number of visible text characters to return. "
            "Omit this argument to use the configured default from "
            "tool settings (8000 by default)."
        ),
    )


class HttpFetchTool(BaseTool):
    """Fetch static HTML content over HTTP(S) and extract visible text."""

    name: str = "http_fetch"
    description: str = (
        "Fetch static HTML content from a URL and extract its readable "
        "text. Performs a single HTTP GET, follows redirects, strips "
        "scripts and styles, and returns the visible text."
    )
    args_schema: type[BaseModel] = HttpFetchArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=30.0,
        retryable=False,
        idempotent=True,
        side_effects=False,
    )

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the tool with optional user-supplied settings (M7.9).

        Settings are injected by the Web layer via the adapter factory
        (from ``settings_schema`` in the manifest). Missing keys fall
        back to the schema defaults. Values are validated upstream by
        ``ToolSettingsManager.save_settings`` via jsonschema, but the
        parser here is defensive against a corrupted settings file.
        """
        super().__init__(workspace_root=workspace_root, **kwargs)
        s: Dict[str, Any] = settings or {}

        def _get_int(key: str, default: int) -> int:
            try:
                return int(s.get(key, default))
            except (TypeError, ValueError):
                return default

        self._default_max_chars: int = _get_int("default_max_chars", 8000)
        self._request_timeout: float = float(
            _get_int("request_timeout_seconds", 20)
        )
        self._follow_redirects: bool = bool(
            s.get("follow_redirects", True)
        )
        self._max_redirects: int = _get_int("max_redirects", 5)

    @staticmethod
    def _validate_url(url: str) -> Optional[str]:
        raw = (url or "").strip()
        if not raw:
            return "url is required and must not be empty."
        try:
            parsed = urlparse(raw)
        except Exception as exc:
            return f"url could not be parsed: {exc}"
        if parsed.scheme.lower() not in ("http", "https"):
            return f"url scheme {parsed.scheme!r} is not supported."
        if not parsed.netloc:
            return "url must include a host."
        return None

    async def _arun(
        self,
        url: str,
        max_chars: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        error = self._validate_url(url)
        if error is not None:
            return ToolResult(
                output=f"HTTP Fetch Error: {error}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "INVALID_URL", "url": str(url or "")},
            )

        target_url = url.strip()
        client = None

        # Resolve effective max_chars: an explicit argument always wins;
        # otherwise fall back to the value from user settings (M7.9).
        effective_max_chars: int = (
            int(max_chars) if max_chars is not None else self._default_max_chars
        )
        # The connect timeout must never exceed the overall request timeout.
        connect_timeout: float = min(10.0, self._request_timeout)

        try:
            # Create client without 'async with' to avoid Windows asyncio deadlocks
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._request_timeout, connect=connect_timeout
                ),
                follow_redirects=self._follow_redirects,
                max_redirects=self._max_redirects,
                headers={"User-Agent": "PELDRUN-HttpFetch/1.0"},
            )
            resp = await client.get(target_url)
            resp.raise_for_status()

        except Exception as exc:
            logger.warning("HTTP fetch failed for %r: %s", target_url, exc)
            if client:
                try:
                    await client.aclose()
                except Exception:
                    pass
            return ToolResult(
                output=f"HTTP Fetch Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "FETCH_FAILED",
                    "url": target_url,
                    "exception_type": type(exc).__name__,
                },
            )

        # Extract content
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            title = (
                soup.title.string.strip()
                if soup.title and soup.title.string
                else ""
            )

            raw_text = " ".join(soup.get_text(separator=" ").split())
            truncated = len(raw_text) > effective_max_chars
            text = raw_text[:effective_max_chars]
        except Exception as exc:
            logger.warning("HTML extraction failed for %r: %s", target_url, exc)
            if client:
                try:
                    await client.aclose()
                except Exception:
                    pass
            return ToolResult(
                output=f"HTTP Fetch Error: could not parse HTML: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "PARSE_FAILED"},
            )

        # Ensure the client is closed safely and definitively
        if client:
            try:
                await client.aclose()
            except Exception:
                pass

        parts: list[str] = []
        if title:
            parts.append(f"Title: {title}")
        parts.append(f"URL: {str(resp.url)}")
        parts.append(f"Status: {resp.status_code}")
        if truncated:
            parts.append(
                f"Note: content truncated to {effective_max_chars} characters."
            )
        parts.append("")
        parts.append("Content:")
        parts.append(text)

        return ToolResult(
            output="\n".join(parts),
            exit_code=0,
            is_error=False,
            metadata={
                "url": str(resp.url),
                "status_code": resp.status_code,
                "title": title,
                "truncated": truncated,
            },
        )


__all__ = ["HttpFetchArgs", "HttpFetchTool"]
