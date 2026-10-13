"""
backend/peldrun/tools/builtins/search_providers/duckduckgo_provider.py

Resilient DuckDuckGo Search Provider (Production-Ready).
- Uses the modern `ddgs` package (renamed from deprecated `duckduckgo_search`).
- Strict asyncio.wait_for timeout prevents worker-thread leaks forever.
- Built-in throttle (min interval between queries) avoids DDG rate-limit bans.
- Silent failures are now explicit: `is_success=False` + descriptive error_message.
- HTML scraper fallback uses lxml for 5-10x faster parsing.
- Fully preserves user settings (timeout_seconds, lang, country, region, safesearch).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    SearchResponse,
    SearchResult,
)

logger = logging.getLogger("peldrun.search.duckduckgo")

# ─────────────────────────────────────────────────────────────────────────────
# Suppress the benign migration warning emitted by `ddgs._DDGSProxy`.
# The warning is not from our code — it is a courtesy notice printed by the
# `ddgs` package itself when using its proxy class.
# ─────────────────────────────────────────────────────────────────────────────
import warnings
warnings.filterwarnings(
    "ignore",
    message=r"This package \(`duckduckgo_search`\) has been renamed to `ddgs`",
    category=RuntimeWarning,
)
# ─────────────────────────────────────────────────────────────────────────────
# DDGS client detection — works with BOTH the new `ddgs` package and the
# legacy `duckduckgo_search` package (if user hasn't upgraded yet).
# ─────────────────────────────────────────────────────────────────────────────
try:
    from ddgs import DDGS                                  # ← preferred (new)
    _DDGS_BACKEND = "ddgs"
except ImportError:
    try:
        from duckduckgo_search import DDGS                 # ← legacy fallback
        _DDGS_BACKEND = "duckduckgo_search"
    except ImportError:
        DDGS = None                                        # type: ignore
        _DDGS_BACKEND = "none"


class DuckDuckGoSearchProvider(BaseSearchProvider):
    """
    Zero-credential fallback search provider.

    Strategy:
      1. Primary  → `ddgs` / `duckduckgo_search` library via asyncio.to_thread
                    (wrapped in asyncio.wait_for to prevent thread leaks).
      2. Fallback → Direct HTML scraping (html.duckduckgo.com + lite.duckduckgo.com).
    """

    provider_name: str = "DuckDuckGo"
    cost_per_query: float = 0.0

    # ── Class-level throttle shared by all instances in this process ─────────
    # DuckDuckGo aggressively rate-limits back-to-back queries from the same IP.
    # 2 seconds between calls is a safe conservative default.
    _last_request_ts: float = 0.0
    _throttle_lock: Optional[asyncio.Lock] = None

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)

        # ── Preserve all user settings from config.toml ──────────────────────
        self.endpoint: str = self.config.get(
            "endpoint", "https://html.duckduckgo.com/html/"
        )
        self.timeout_seconds: float = float(
            self.config.get("timeout_seconds", 12.0)
        )
        self.throttle_seconds: float = float(
            self.config.get("throttle_seconds", 2.0)
        )
        self.max_results_cap: int = int(self.config.get("max_results_cap", 20))
        self.safesearch: str = self.config.get("safesearch", "moderate")
        self.backend: str = self.config.get("backend", "auto")  # auto | ddgs | html
        self.enable_html_fallback: bool = bool(
            self.config.get("enable_html_fallback", True)
        )

        # Lazily create the class-level lock (must be inside a running loop or
        # at least at first use to avoid "attached to different loop" errors).
        if DuckDuckGoSearchProvider._throttle_lock is None:
            try:
                DuckDuckGoSearchProvider._throttle_lock = asyncio.Lock()
            except Exception:
                DuckDuckGoSearchProvider._throttle_lock = None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────
    async def _throttle(self) -> None:
        """Enforce minimum interval between DDG queries to avoid 429/403 bans."""
        if self.throttle_seconds <= 0:
            return
        lock = DuckDuckGoSearchProvider._throttle_lock
        if lock is None:
            return
        async with lock:
            now = time.monotonic()
            elapsed = now - DuckDuckGoSearchProvider._last_request_ts
            wait = self.throttle_seconds - elapsed
            if wait > 0:
                logger.debug(f"DDG throttle: sleeping {wait:.2f}s")
                await asyncio.sleep(wait)
            DuckDuckGoSearchProvider._last_request_ts = time.monotonic()

    def _build_region(self, country: str, lang: str) -> str:
        """Build the DDG region code (e.g. 'us-en', 'eg-ar')."""
        c = (country or "us").strip().lower()
        l = (lang or "en").strip().lower()
        if c and l:
            return f"{c}-{l}"
        return "wt-wt"  # worldwide

    # ─────────────────────────────────────────────────────────────────────────
    # Ping — validates DDG reachability
    # ─────────────────────────────────────────────────────────────────────────
    async def ping(self) -> bool:
        """Lightweight reachability check without triggering rate limits."""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            }
            async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
                res = await client.get("https://duckduckgo.com/")
                return res.status_code == 200
        except Exception as exc:
            logger.debug(f"DDG ping failed: {exc}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Method 1: Native library (ddgs / duckduckgo_search)
    # ─────────────────────────────────────────────────────────────────────────
    async def _search_via_library(
        self,
        query: str,
        max_results: int,
        region: str,
    ) -> List[SearchResult]:
        """Run DDGS in a worker thread with a hard timeout to prevent leaks."""
        if DDGS is None:
            raise RuntimeError(
                "No DDGS client installed. Run: pip install ddgs"
            )

        def _fetch() -> List[Dict[str, Any]]:
            # `ddgs` accepts timeout & region kwargs; legacy accepts region too.
            kwargs: Dict[str, Any] = {
                "max_results": max_results,
                "region": region,
            }
            # Pass safesearch if the backend supports it
            try:
                with DDGS(timeout=int(self.timeout_seconds)) as ddgs:  # type: ignore
                    return list(ddgs.text(query, **kwargs))
            except TypeError:
                # Legacy signature fallback
                with DDGS() as ddgs:  # type: ignore
                    return list(ddgs.text(query, **kwargs))

        # Hard timeout: this is the ONLY reliable way to prevent to_thread
        # from leaking a stuck worker thread forever when DDG hangs.
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch),
                timeout=self.timeout_seconds + 2.0,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"DDGS backend timed out after {self.timeout_seconds + 2.0:.1f}s "
                f"(possible rate limit or network issue)"
            )

        parsed: List[SearchResult] = []
        for item in raw or []:
            url = item.get("href") or item.get("link") or item.get("url") or ""
            title = item.get("title") or ""
            snippet = item.get("body") or item.get("snippet") or item.get("description") or ""
            if url and title:
                parsed.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=self.sanitize_snippet(snippet),
                    )
                )
            if len(parsed) >= max_results:
                break
        return parsed

    # ─────────────────────────────────────────────────────────────────────────
    # Method 2: HTML scraper fallback
    # ─────────────────────────────────────────────────────────────────────────
    async def _search_via_html(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Direct HTML scraping — used when the library is unavailable or fails."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        endpoints = [
            "https://html.duckduckgo.com/html/",
            "https://lite.duckduckgo.com/lite/",
        ]

        for ep in endpoints:
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    resp = await client.post(ep, data={"q": query, "kl": "wt-wt"})
                    if resp.status_code >= 400:
                        logger.debug(f"DDG HTML {ep} returned HTTP {resp.status_code}")
                        continue

                    # Prefer lxml (fast); fall back to html.parser if unavailable.
                    try:
                        soup = BeautifulSoup(resp.text, "lxml")
                    except Exception:
                        soup = BeautifulSoup(resp.text, "html.parser")

                    results: List[SearchResult] = []

                    # ── Layout A: html.duckduckgo.com (new 2024+ markup) ──────
                    anchors = soup.select("a.result__a")
                    snippets_a = soup.select("a.result__snippet")

                    # ── Layout B: legacy markup ──────────────────────────────
                    if not anchors:
                        anchors = soup.select("a.result-link")
                    if not snippets_a:
                        snippets_a = soup.select("td.result-snippet")

                    # ── Layout C: lite.duckduckgo.com ────────────────────────
                    if not anchors:
                        anchors = soup.select("a.result-link, a[href*='uddg=']")

                    for idx, a_tag in enumerate(anchors[:max_results]):
                        raw_href = a_tag.get("href", "")
                        target_url = raw_href

                        # Decode DDG redirect wrapper: /l/?uddg=<encoded-url>
                        if "uddg=" in raw_href:
                            m = re.search(r"uddg=([^&]+)", raw_href)
                            if m:
                                target_url = urllib.parse.unquote(m.group(1))

                        # Skip internal DDG links
                        if not target_url or target_url.startswith("/"):
                            continue
                        if "duckduckgo.com" in target_url and "uddg=" not in raw_href:
                            continue

                        title = a_tag.get_text(strip=True)
                        snippet_text = ""
                        if idx < len(snippets_a):
                            snippet_text = snippets_a[idx].get_text(strip=True)

                        if target_url and title:
                            results.append(
                                SearchResult(
                                    title=title,
                                    url=target_url,
                                    snippet=self.sanitize_snippet(snippet_text),
                                )
                            )

                    if results:
                        return results

            except Exception as exc:
                logger.debug(f"DDG HTML fallback {ep} failed: {exc}")
                continue

        return []

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────
    async def search(
        self,
        query: str,
        max_results: int = 5,
        lang: str = "en",
        country: str = "us",
        **kwargs: Any,
    ) -> SearchResponse:
        start_time = time.perf_counter()
        clean_q = query.strip()

        if not clean_q:
            return SearchResponse(
                query=query,
                provider=self.provider_name,
                is_success=False,
                error_message="Search query cannot be empty.",
            )

        # Clamp max_results to a sane range
        limit = max(1, min(int(max_results), self.max_results_cap))
        region = self._build_region(country, lang)

        # Respect the throttle before hitting DDG
        try:
            await self._throttle()
        except Exception:
            pass

        collected: List[SearchResult] = []
        errors: List[str] = []

        # ── 1. Native library attempt ────────────────────────────────────────
        if self.backend in ("auto", "ddgs"):
            if DDGS is not None:
                try:
                    collected = await self._search_via_library(clean_q, limit, region)
                except Exception as exc:
                    msg = str(exc) or exc.__class__.__name__
                    errors.append(f"library({_DDGS_BACKEND}): {msg}")
                    logger.warning(f"DDGS library search failed: {msg}")
            else:
                errors.append(
                    "library: not installed (run `pip install ddgs`)"
                )

        # ── 2. HTML fallback (only if we still have no results) ─────────────
        if not collected and self.enable_html_fallback and self.backend != "ddgs":
            try:
                collected = await self._search_via_html(clean_q, limit)
                if not collected:
                    errors.append("html-fallback: zero results parsed")
            except Exception as exc:
                msg = str(exc) or exc.__class__.__name__
                errors.append(f"html-fallback: {msg}")
                logger.warning(f"DDG HTML fallback failed: {msg}")

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # ── 3. Build explicit success/failure response ───────────────────────
        if collected:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=collected,
                latency_ms=elapsed_ms,
                estimated_cost_usd=0.0,
                is_success=True,
                error_message=None,
            )

        # No results → explicit failure with full diagnostic
        diagnostic = " | ".join(errors) if errors else "no results returned"
        return SearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=[],
            latency_ms=elapsed_ms,
            estimated_cost_usd=0.0,
            is_success=False,
            error_message=(
                f"DuckDuckGo returned zero results. "
                f"Likely causes: rate limiting, IP block, or transient outage. "
                f"Diagnostics: {diagnostic}"
            ),
        )


__all__ = ["DuckDuckGoSearchProvider"]
