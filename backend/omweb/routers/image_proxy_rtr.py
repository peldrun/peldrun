# backend/omweb/routers/image_proxy_rtr.py
"""
Image proxy endpoint for the image search gallery.

Why this exists
---------------
Direct hotlinking of image URLs from search results fails for many
providers because:

  * Many image hosts (Alamy, Getty, Shutterstock, iStockPhoto, etc.)
    block requests whose `Referer` header does not originate from their
    own domain.
  * Some hosts redirect to interstitial pages instead of the raw image.
  * Browsers enforce cross-origin restrictions on some CDNs.

The proxy fetches the image server-side (with a neutral User-Agent and
a Referer spoofed to the source host) and streams it back to the
client. This also gives us a single place to enforce:

  * Content-Type allowlist (image/* only, no SVG, no HTML).
  * Response size limit (10 MB hard cap) to bound memory.
  * Timeout enforcement (8 s per image).
  * Cache-Control headers for browser-side caching.
  * Per-process LRU cache for popular images (200 entries, 1 h TTL).

Public API
----------
  GET /api/search/image-proxy?url=<urlencoded_url>&source=<host>

  `url` is the raw external image URL. `source` is optional but
  recommended; when present the proxy sets `Referer: https://<source>`
  to satisfy hotlink checks.

Helper exported for backend callers
-----------------------------------
  ``build_proxy_url(original_url, source_host)`` returns the fully
  formed proxy URL that should be embedded in the IMAGE_RESULTS event.
  This keeps the URL-swap logic centralized.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
from collections import OrderedDict
from typing import Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from peldrun.tools.builtins.search_providers.base import validate_search_url

logger = logging.getLogger("omweb.routers.image_proxy")

router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ALLOWED_IMAGE_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/avif",
    }
)

# 10 MB hard cap on the buffered body we are willing to hold in memory.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 8 s timeout for the upstream fetch; gallery cards render in parallel
# so we do not need a per-image budget larger than this.
_UPSTREAM_TIMEOUT = 8.0

# User-Agent for upstream image fetches.
#
# Wikimedia's User-Agent policy
# (https://meta.wikimedia.org/wiki/User-Agent_policy) requires a
# descriptive UA that identifies the application and provides a
# contact URL. A generic browser UA is silently rejected with 403.
#
# The descriptive first segment satisfies Wikimedia and other strict
# providers, while the trailing browser token keeps CDNs that prefer
# browser-shaped UAs happy.
_UPSTREAM_UA = (
    "PELDRUN-ImageProxy/1.0 "
    "(https://peldrun.ai; support@peldrun.ai) "
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Simple in-process LRU cache. Keyed by SHA-256 of the URL. Values are
# (body_bytes, content_type, expires_at_monotonic). 200 entries caps
# memory at ~200 MB worst-case; realistic galleries load far fewer.
_CACHE_MAX_ENTRIES = 200
_CACHE_TTL_SECONDS = 3600  # 1 hour
_cache: "OrderedDict[str, Tuple[bytes, str, float]]" = OrderedDict()


# ---------------------------------------------------------------------------
# Public helper used by dispatcher / engine
# ---------------------------------------------------------------------------

def build_proxy_url(original_url: str, source_host: Optional[str] = None) -> str:
    """
    Build a fully qualified ``/api/search/image-proxy?...`` URL.

    Returns an empty string when ``original_url`` is empty so callers can
    assign the result unconditionally without null-checks.
    """
    if not original_url or not isinstance(original_url, str):
        return ""
    params = {"url": original_url}
    if source_host and isinstance(source_host, str):
        host = source_host.strip().lower()
        if host:
            params["source"] = host
    return "/api/search/image-proxy?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> Optional[Tuple[bytes, str]]:
    entry = _cache.get(key)
    if entry is None:
        return None
    body, content_type, expires_at = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return body, content_type


def _cache_put(key: str, body: bytes, content_type: str) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)
    _cache[key] = (body, content_type, time.monotonic() + _CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

def _reject_private_host(hostname: str) -> None:
    """
    Reject loopback and RFC 1918 private hosts to prevent SSRF.

    ``validate_search_url`` already rejects non-http(s), embedded
    credentials, and empty hostnames. This extra guard catches
    ``http://127.0.0.1/admin`` style probes.
    """
    host = (hostname or "").lower().strip()
    if not host:
        raise HTTPException(status_code=400, detail="Image URL has no host")

    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise HTTPException(
            status_code=400,
            detail="Image URL points to a loopback host",
        )

    if host.startswith(("10.", "192.168.", "169.254.")):
        raise HTTPException(
            status_code=400,
            detail="Image URL points to a private host",
        )

    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                second = int(parts[1])
                if 16 <= second <= 31:
                    raise HTTPException(
                        status_code=400,
                        detail="Image URL points to a private host",
                    )
            except (ValueError, IndexError):
                pass


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/image-proxy")
async def proxy_image(
    url: str = Query(..., description="URL-encoded image URL to fetch"),
    source: Optional[str] = Query(
        default=None,
        description="Optional source host used as Referer header",
    ),
) -> Response:
    """
    Fetch an external image server-side and stream it back to the client.
    """
    # 1. Validate URL
    try:
        clean_url = validate_search_url(url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image URL: {exc}",
        )

    try:
        parsed = urllib.parse.urlsplit(clean_url)
        _reject_private_host(parsed.hostname or "")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed image URL")

    # 2. Cache lookup
    cache_key = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        body, content_type = cached
        return Response(
            content=body,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Cache": "HIT",
            },
        )

    # 3. Build upstream headers
    headers = {
        "User-Agent": _UPSTREAM_UA,
        "Accept": (
            "image/avif,image/webp,image/jpeg,image/png,image/*;q=0.8,"
            "*/*;q=0.5"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if source and isinstance(source, str):
        host = source.strip().lower()
        if host and "://" not in host:
            headers["Referer"] = f"https://{host}/"
    if "Referer" not in headers:
        try:
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        except Exception:
            pass

    # 4. Fetch upstream
    try:
        async with httpx.AsyncClient(
            timeout=_UPSTREAM_TIMEOUT,
            follow_redirects=True,
            max_redirects=3,
        ) as client:
            async with client.stream(
                "GET", clean_url, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=(
                            f"Upstream returned HTTP {resp.status_code} "
                            f"for image {clean_url[:80]}"
                        ),
                    )

                raw_ct = (resp.headers.get("content-type") or "").lower()
                content_type = raw_ct.split(";")[0].strip()
                if content_type not in _ALLOWED_IMAGE_TYPES:
                    raise HTTPException(
                        status_code=415,
                        detail=(
                            f"Unsupported content type "
                            f"'{content_type or 'unknown'}' from upstream"
                        ),
                    )

                body = bytearray()
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    body.extend(chunk)
                    if len(body) > _MAX_IMAGE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Image exceeds {_MAX_IMAGE_BYTES} bytes"
                            ),
                        )

                body_bytes = bytes(body)

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504, detail="Upstream image fetch timed out"
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Upstream fetch failed: {exc}"
        )

    # 5. Cache and return
    _cache_put(cache_key, body_bytes, content_type)
    return Response(
        content=body_bytes,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache": "MISS",
        },
    )


__all__ = ["router", "build_proxy_url"]
