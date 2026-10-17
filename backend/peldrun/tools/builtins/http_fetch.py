# backend/peldrun/tools/builtins/http_fetch.py
"""
PELDRUN Core HTTP Content Fetcher Tool.

Provides a single-capability tool that fetches static HTML content from
a URL over HTTP(S) and returns the visible text extracted from the DOM.

Design scope (intentionally narrow):
  * Performs exactly one HTTP GET request (with redirects followed).
  * Strips <script>, <style>, and <noscript> before text extraction.
  * Does NOT execute JavaScript.
  * Does NOT interact with DOM elements (click, fill, scroll).
  * Does NOT capture screenshots.

For dynamic pages or interactive workflows, use the separate
``browser_interactive`` tool.

The tool satisfies the ``BaseTool`` contract:
  * ``name``, ``description``, ``args_schema`` are declared class-level.
  * ``execution_policy`` is declared to bound timeout and side-effects.
  * ``_arun()`` implements the asynchronous execution logic and returns
    a canonical ``ToolResult``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool
from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

logger = logging.getLogger("peldrun.tools.builtins.http_fetch")


# ---------------------------------------------------------------------------
# Argument schema
# ---------------------------------------------------------------------------

class HttpFetchArgs(BaseModel):
    """Canonical argument schema for the http_fetch tool."""

    url: str = Field(
        ...,
        min_length=1,
        description="Target HTTP or HTTPS URL to fetch.",
    )
    max_chars: int = Field(
        default=8000,
        ge=500,
        le=50000,
        description=(
            "Maximum number of visible text characters to return. "
            "Defaults to 8000; range 500..50000."
        ),
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

class HttpFetchTool(BaseTool):
    """
    Fetch static HTML content over HTTP(S) and extract visible text.

    This tool is intentionally single-purpose: it fetches, extracts, and
    returns. It does not execute JavaScript or interact with the page.
    """

    name: str = "http_fetch"
    description: str = (
        "Fetch static HTML content from a URL and extract its readable "
        "text. Performs a single HTTP GET, follows redirects, strips "
        "scripts and styles, and returns the visible text. Does NOT "
        "execute JavaScript or interact with the page. Use "
        "'browser_interactive' for dynamic pages."
    )
    args_schema: type[BaseModel] = HttpFetchArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=30.0,
        retryable=False,
        idempotent=True,
        side_effects=False,
    )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_url(url: str) -> Optional[str]:
        """
        Validate the URL and return an error message, or None if valid.

        Only http and https schemes are allowed; other schemes (file,
        ftp, javascript, data) are rejected to prevent unexpected
        behavior. The host must be non-empty.
        """
        raw = (url or "").strip()
        if not raw:
            return "url is required and must not be empty."

        try:
            parsed = urlparse(raw)
        except Exception as exc:
            return f"url could not be parsed: {exc}"

        if parsed.scheme.lower() not in ("http", "https"):
            return (
                f"url scheme {parsed.scheme!r} is not supported; "
                f"only 'http' and 'https' are allowed."
            )

        if not parsed.netloc:
            return "url must include a host (e.g. 'https://example.com')."

        return None

    # ------------------------------------------------------------------
    # BaseTool contract
    # ------------------------------------------------------------------

    async def _arun(
        self,
        url: str,
        max_chars: int = 8000,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Fetch the target URL and extract visible text.

        Returns a canonical ToolResult:
          * success (exit_code=0, is_error=False): output contains the
            page text, metadata contains status_code, final_url, title.
          * failure (exit_code=1, is_error=True): output contains a
            human-readable error, metadata contains error_code.
        """
        # 1. URL validation
        error = self._validate_url(url)
        if error is not None:
            return ToolResult(
                output=f"HTTP Fetch Error: {error}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "INVALID_URL",
                    "url": str(url or ""),
                },
            )

        target_url = url.strip()

        # 2. Perform the fetch
        try:
            import httpx
            from bs4 import BeautifulSoup
        except ImportError as imp_err:
            return ToolResult(
                output=(
                    f"HTTP Fetch Error: required library is not available: "
                    f"{imp_err}"
                ),
                exit_code=1,
                is_error=True,
                metadata={"error_code": "MISSING_DEPENDENCY"},
            )

        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "PELDRUN-HttpFetch/1.0"},
            ) as client:
                resp = await client.get(target_url)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("HTTP fetch failed for %r: %s", target_url, exc)
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

        # 3. Extract visible text
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()

            title = (
                soup.title.string.strip()
                if soup.title and soup.title.string
                else ""
            )

            # Normalize whitespace and cap at max_chars.
            raw_text = " ".join(soup.get_text(separator=" ").split())
            truncated = len(raw_text) > max_chars
            text = raw_text[:max_chars]
        except Exception as exc:
            logger.warning(
                "HTML extraction failed for %r: %s", target_url, exc
            )
            return ToolResult(
                output=f"HTTP Fetch Error: could not parse HTML: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "PARSE_FAILED",
                    "url": target_url,
                    "exception_type": type(exc).__name__,
                },
            )

        # 4. Build the success output
        parts: list[str] = []
        if title:
            parts.append(f"Title: {title}")
        parts.append(f"URL: {str(resp.url)}")
        parts.append(f"Status: {resp.status_code}")
        if truncated:
            parts.append(
                f"Note: content truncated to {max_chars} characters "
                f"(original length: {len(raw_text)})."
            )
        parts.append("")
        parts.append("Content:")
        parts.append(text)
        output = "\n".join(parts)

        return ToolResult(
            output=output,
            exit_code=0,
            is_error=False,
            metadata={
                "url": str(resp.url),
                "status_code": resp.status_code,
                "title": title,
                "original_length": len(raw_text),
                "returned_length": len(text),
                "truncated": truncated,
                "max_chars": max_chars,
            },
        )


__all__ = ["HttpFetchArgs", "HttpFetchTool"]
