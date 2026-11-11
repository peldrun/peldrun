# backend/peldrun/tools/builtins/http_fetch.py
"""
PELDRUN Core HTTP Content Fetcher Tool.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any, List, Optional
from urllib.parse import urlparse

# Move imports to the top level to prevent reloading issues in asyncio loops
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool
from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

logger = logging.getLogger("peldrun.tools.builtins.http_fetch")


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------
#
# Any URL that resolves (via DNS) to one of these networks is rejected
# BEFORE the HTTP request is dispatched, and again after every redirect.
# Rationale: an agent must never be able to read loopback services, cloud
# metadata endpoints (AWS/GCP/Azure 169.254.169.254), private intranets,
# or link-local addresses via the fetch tool. Without this guard, a
# single prompt injection can exfiltrate credentials from the host.
_FORBIDDEN_NETWORKS: List[ipaddress._BaseNetwork] = [
    # IPv4 — special-use ranges per RFC 6890 + cloud metadata.
    ipaddress.ip_network("0.0.0.0/8"),        # "This" network
    ipaddress.ip_network("10.0.0.0/8"),       # Private (RFC 1918)
    ipaddress.ip_network("100.64.0.0/10"),    # Shared address space (CGNAT)
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local (cloud metadata)
    ipaddress.ip_network("172.16.0.0/12"),    # Private (RFC 1918)
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),   # Private (RFC 1918)
    ipaddress.ip_network("198.18.0.0/15"),    # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # Multicast
    ipaddress.ip_network("240.0.0.0/4"),      # Reserved (includes broadcast)
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6 — special-use ranges per RFC 4291 + RFC 4193.
    ipaddress.ip_network("::/128"),           # Unspecified
    ipaddress.ip_network("::1/128"),          # Loopback
    ipaddress.ip_network("::ffff:0:0/96"),    # IPv4-mapped (checked below)
    ipaddress.ip_network("64:ff9b::/96"),     # IPv4/IPv6 translation
    ipaddress.ip_network("fc00::/7"),         # Unique local (RFC 4193)
    ipaddress.ip_network("fe80::/10"),        # Link-local
    ipaddress.ip_network("ff00::/8"),         # Multicast
]


def _is_forbidden_ip(ip_str: str) -> bool:
    """
    Return True when ``ip_str`` belongs to a network the fetcher must
    never reach.

    IPv4-mapped IPv6 addresses (``::ffff:1.2.3.4``) are unwrapped to
    their IPv4 form before the check so that a server cannot bypass
    the guard by returning an AAAA record that maps to a forbidden
    IPv4 range.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Unparseable → treat as forbidden (fail closed).
        return True

    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1 → 127.0.0.1).
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    for network in _FORBIDDEN_NETWORKS:
        try:
            if ip in network:
                return True
        except TypeError:
            # IPv4 in IPv6 network or vice versa — not a match.
            continue
    return False


async def _resolve_and_validate_host(hostname: str) -> Optional[str]:
    """
    Resolve ``hostname`` via DNS and reject when ANY returned address
    is in a forbidden network.

    Returns ``None`` on success, or a human-readable error string when
    the hostname must be blocked. Non-blocking: uses
    ``loop.getaddrinfo`` so the event loop stays responsive.
    """
    if not hostname:
        return "hostname is empty."

    # Reject literal IPs immediately — no DNS round-trip needed.
    try:
        ipaddress.ip_address(hostname)
        if _is_forbidden_ip(hostname):
            return (
                f"Access to {hostname!r} is forbidden: "
                f"the address is in a restricted range."
            )
        return None
    except ValueError:
        # Not a literal IP → resolve below.
        pass

    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        return f"Could not resolve hostname {hostname!r}: {exc}"
    except Exception as exc:
        return f"DNS resolution failed for {hostname!r}: {exc}"

    if not infos:
        return f"Hostname {hostname!r} resolved to no addresses."

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = sockaddr[0]
        if _is_forbidden_ip(ip_str):
            return (
                f"Access to {hostname!r} is forbidden: it resolves to "
                f"a restricted IP address ({ip_str})."
            )

    return None


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
    async def _validate_url_async(url: str) -> Optional[str]:
        """
        Full URL safety check: scheme, host presence, and DNS resolution
        against the forbidden-network list.

        This is the single gate every outgoing request (initial URL and
        every redirect hop) MUST pass. Returning ``None`` means the URL
        is safe to fetch; returning a string means the caller must
        refuse the request with that error message.
        """
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

        # Extract hostname without port; urlparse keeps the port in
        # netloc but we want the DNS-resolvable part only.
        hostname = parsed.hostname
        if not hostname:
            return "url must include a resolvable hostname."

        # Delegate to the shared resolver (checks every A/AAAA record).
        return await _resolve_and_validate_host(hostname)

    async def _fetch_with_redirect_validation(
        self,
        client: httpx.AsyncClient,
        initial_url: str,
        *,
        max_redirects: int,
    ) -> Any:
        """
        Perform one GET and follow any redirects manually, re-validating
        the target of every hop against the SSRF guard.

        Returns either the final ``httpx.Response`` (2xx/4xx/5xx that is
        NOT a redirect) or a ``ToolResult`` describing the failure. The
        caller distinguishes the two by isinstance check.

        The redirect loop is bounded by ``max_redirects``. A redirect
        chain longer than that produces a TOO_MANY_REDIRECTS result —
        never an infinite loop.
        """
        current_url = initial_url
        redirect_count = 0

        while True:
            try:
                resp = await client.get(current_url)
            except Exception as exc:
                return ToolResult(
                    output=f"HTTP Fetch Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "FETCH_FAILED",
                        "url": current_url,
                        "exception_type": type(exc).__name__,
                    },
                )

            if not resp.is_redirect:
                return resp

            # Redirect detected — validate before following.
            if redirect_count >= max_redirects:
                return ToolResult(
                    output=(
                        f"HTTP Fetch Error: exceeded {max_redirects} "
                        f"redirect(s). Aborted at {current_url}."
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "TOO_MANY_REDIRECTS",
                        "last_url": current_url,
                        "redirect_count": redirect_count,
                    },
                )

            location = resp.headers.get("Location")
            if not location:
                return ToolResult(
                    output=(
                        "HTTP Fetch Error: redirect response missing "
                        "Location header."
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "MISSING_REDIRECT_LOCATION",
                        "url": current_url,
                        "status_code": resp.status_code,
                    },
                )

            # Resolve relative redirect targets against the current URL.
            if not location.startswith(("http://", "https://")):
                try:
                    location = str(resp.url.join(location))
                except Exception as exc:
                    return ToolResult(
                        output=(
                            f"HTTP Fetch Error: could not resolve "
                            f"redirect target {location!r}: {exc}"
                        ),
                        exit_code=1,
                        is_error=True,
                        metadata={
                            "error_code": "INVALID_REDIRECT_TARGET",
                            "last_url": current_url,
                        },
                    )

            # Re-validate the redirect destination (scheme + DNS + IP).
            redirect_err = await self._validate_url_async(location)
            if redirect_err is not None:
                logger.warning(
                    "Blocked SSRF redirect %s -> %s: %s",
                    current_url,
                    location,
                    redirect_err,
                )
                return ToolResult(
                    output=(
                        f"HTTP Fetch Error: redirect blocked "
                        f"({redirect_err})"
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={
                        "error_code": "FORBIDDEN_REDIRECT",
                        "redirect_target": location,
                        "reason": redirect_err,
                    },
                )

            current_url = location
            redirect_count += 1

    async def _arun(
        self,
        url: str,
        max_chars: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        # [FIX-SSRF] Full URL safety check (scheme + DNS + IP ranges)
        # BEFORE any network activity is initiated.
        error = await self._validate_url_async(url)
        if error is not None:
            return ToolResult(
                output=f"HTTP Fetch Error: {error}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "INVALID_URL",
                    "url": str(url or ""),
                    "reason": error,
                },
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
            # [FIX-SSRF] Manual redirect following. httpx's built-in
            # follow_redirects bypasses the SSRF guard because it
            # dispatches the new request internally without giving us
            # a chance to re-validate the Location header. We disable
            # it here and follow redirects ourselves, validating every
            # hop against the forbidden-network list before the next
            # request is dispatched.
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._request_timeout, connect=connect_timeout
                ),
                follow_redirects=False,
                headers={"User-Agent": "PELDRUN-HttpFetch/1.0"},
            )

            resp_or_error = await self._fetch_with_redirect_validation(
                client, target_url, max_redirects=self._max_redirects
            )
            if isinstance(resp_or_error, ToolResult):
                # Redirect validation or fetch failure — return the
                # structured error as-is. The client is still open and
                # must be closed.
                try:
                    await client.aclose()
                except Exception:
                    pass
                return resp_or_error

            resp = resp_or_error
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
