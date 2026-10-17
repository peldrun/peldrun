# backend/peldrun/tools/builtins/browser_interactive/security.py
"""
SSRF Protection for the Interactive Browser Tool.

This module provides three layers of defense against Server-Side Request
Forgery (SSRF) attacks, following 2026 industry best practices:

  Layer 1 (Pre-flight):
      ``is_safe_url()`` / ``validate_url_or_raise()`` validate a URL string
      BEFORE the browser attempts to navigate to it.

  Layer 2 (Runtime interception):
      ``make_route_handler()`` produces a Playwright route handler that
      intercepts EVERY network request issued by the page, including
      redirects and subresource loads. Private/internal targets are
      aborted immediately.

  Layer 3 (Post-navigation):
      ``is_safe_resolved_url()`` re-validates the final page URL after
      redirects, catching cases where a benign URL redirected to an
      internal address.

Attack categories covered:
  * Cloud metadata endpoints (AWS, GCP, Azure, Alibaba, DigitalOcean).
  * Loopback addresses (127.0.0.0/8, ::1).
  * RFC 1918 private networks (10/8, 172.16/12, 192.168/16).
  * Link-local addresses (169.254.0.0/16, fe80::/10).
  * Unique-local IPv6 (fc00::/7).
  * IPv4-mapped IPv6 (::ffff:0:0/96).
  * Dangerous URL schemes (file, ftp, data, javascript, vbscript, about).
  * DNS rebinding via per-request re-resolution in the route handler.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("peldrun.tools.builtins.browser_interactive.security")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SSRFProtectionError(ValueError):
    """Raised when a URL is rejected by the SSRF protection layer."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"SSRF protection blocked {url!r}: {reason}")
        self.url = url
        self.reason = reason


# ---------------------------------------------------------------------------
# Blocklists
# ---------------------------------------------------------------------------

_BLOCKED_SCHEMES: frozenset[str] = frozenset({
    "file", "ftp", "ftps", "sftp", "gopher", "data",
    "javascript", "vbscript", "about", "chrome", "edge",
    "ws", "wss",
})

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata.goog",
})

_BLOCKED_IPV4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),          # "This host"
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),        # Loopback
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local (incl. AWS metadata)
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved
)

_BLOCKED_IPV6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("::/128"),             # Unspecified
    ipaddress.ip_network("::1/128"),            # Loopback
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped
    ipaddress.ip_network("64:ff9b::/96"),       # IPv4/IPv6 translation
    ipaddress.ip_network("100::/64"),           # Discard
    ipaddress.ip_network("2001::/32"),          # Teredo
    ipaddress.ip_network("2001:db8::/32"),      # Documentation
    ipaddress.ip_network("2002::/16"),          # 6to4
    ipaddress.ip_network("fc00::/7"),           # Unique-local
    ipaddress.ip_network("fe80::/10"),          # Link-local
    ipaddress.ip_network("ff00::/8"),           # Multicast
)


# ---------------------------------------------------------------------------
# IP classification
# ---------------------------------------------------------------------------

def _is_blocked_ip(ip_str: str) -> bool:
    """
    Return True if the IP is in any blocked network (v4 or v6).
    Handles IPv4-mapped IPv6 by extracting the embedded IPv4.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    # Handle IPv4-mapped IPv6 (::ffff:1.2.3.4)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_ip(str(ip.ipv4_mapped))

    networks = (
        _BLOCKED_IPV4_NETWORKS
        if isinstance(ip, ipaddress.IPv4Address)
        else _BLOCKED_IPV6_NETWORKS
    )
    return any(ip in net for net in networks)


# ---------------------------------------------------------------------------
# Layer 1: Pre-flight URL validation
# ---------------------------------------------------------------------------

def is_safe_url(url: str) -> bool:
    """
    Return True if the URL passes all static SSRF checks.

    This function performs NO network I/O. It only validates the scheme,
    hostname, and (if the host is a literal IP) the IP itself. It does
    NOT resolve hostnames — that happens in the route handler on each
    request to mitigate DNS rebinding.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    # 1. Scheme check
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False
    if scheme in _BLOCKED_SCHEMES:
        return False

    # 2. Hostname presence
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return False

    # 3. Explicit hostname blocklist
    if host in _BLOCKED_HOSTNAMES:
        return False
    if host.endswith(".localhost") or host.endswith(".internal"):
        return False

    # 4. If the host is a literal IP, check it directly
    try:
        ipaddress.ip_address(host)
        if _is_blocked_ip(host):
            return False
    except ValueError:
        # Not a literal IP — hostname. DNS resolution is deferred to the
        # route handler for per-request enforcement (DNS rebinding defense).
        pass

    return True


def validate_url_or_raise(url: str) -> None:
    """Raise SSRFProtectionError if the URL is unsafe."""
    if not is_safe_url(url):
        raise SSRFProtectionError(
            url,
            "URL failed pre-flight SSRF validation "
            "(scheme, hostname, or literal IP rejected)",
        )


# ---------------------------------------------------------------------------
# Layer 2: Runtime route handler
# ---------------------------------------------------------------------------

def _resolve_and_check(host: str) -> tuple[bool, str]:
    """
    Resolve a hostname to all its IPs and verify every one is safe.

    Returns (ok, reason). If any resolved IP is blocked, returns
    (False, reason). If resolution fails, returns (False, reason) to
    fail safe — we never allow an unresolvable host through.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed: {exc}"

    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        if _is_blocked_ip(ip_str):
            return False, f"hostname {host!r} resolves to blocked IP {ip_str}"

    if not seen:
        return False, f"hostname {host!r} did not resolve to any IP"
    return True, ""


def make_route_handler():
    """
    Create a Playwright route handler that enforces SSRF protection on
    every network request issued by the page.

    Usage in session.py::

        await context.route("**/*", make_route_handler())
    """
    async def _route_handler(route):
        request = route.request
        url = request.url

        try:
            parsed = urlparse(url)
        except Exception:
            await route.abort("blockedbyclient")
            return

        scheme = (parsed.scheme or "").lower()

        # Allow browser-internal schemes needed for page rendering.
        if scheme in ("data", "blob"):
            await route.continue_()
            return

        # Block any scheme not explicitly allowed.
        if scheme not in ("http", "https"):
            logger.warning("SSRF route: blocked scheme %r for %s", scheme, url)
            await route.abort("blockedbyclient")
            return

        host = (parsed.hostname or "").lower().strip("[]")
        if not host:
            await route.abort("blockedbyclient")
            return

        # Explicit hostname blocklist.
        if host in _BLOCKED_HOSTNAMES:
            logger.warning("SSRF route: blocked hostname %r", host)
            await route.abort("blockedbyclient")
            return
        if host.endswith(".localhost") or host.endswith(".internal"):
            logger.warning("SSRF route: blocked internal suffix %r", host)
            await route.abort("blockedbyclient")
            return

        # Literal IP check (fast path).
        try:
            ipaddress.ip_address(host)
            if _is_blocked_ip(host):
                logger.warning("SSRF route: blocked literal IP %r", host)
                await route.abort("blockedbyclient")
                return
        except ValueError:
            # Hostname: resolve and check on EVERY request (DNS rebinding defense).
            ok, reason = _resolve_and_check(host)
            if not ok:
                logger.warning("SSRF route: %s", reason)
                await route.abort("blockedbyclient")
                return

        await route.continue_()

    return _route_handler


# ---------------------------------------------------------------------------
# Layer 3: Post-navigation validation
# ---------------------------------------------------------------------------

def is_safe_resolved_url(url: str) -> bool:
    """
    Re-validate the final URL after redirects.

    Redirects are common (http→https, short URLs). The page may have
    landed on an internal address if the initial URL was benign. This
    function catches that case and is called by the tool right after
    ``page.goto()`` completes.
    """
    return is_safe_url(url)


__all__ = [
    "SSRFProtectionError",
    "is_safe_url",
    "validate_url_or_raise",
    "make_route_handler",
    "is_safe_resolved_url",
]
