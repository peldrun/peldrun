"""
backend/peldrun/tools/builtins/search_providers/searxng_provider.py

SearXNG Self-Hosted Meta-Search Provider.
Queries local or private SearXNG JSON API endpoints with zero tracking
and full privacy sovereignty.

Fixes applied:
- Explicit success/failure reporting based on actual result count.
- Descriptive error messages for zero-result responses.
- Explicit handling of common HTTP error codes (403, 404, 429, 500+).
- Defensive parsing of nested API response structure.
- Safe score conversion with fallback for malformed values.
- Configurable result cap, categories, safesearch and time range.
- Clear hint when JSON output format is disabled on the instance.
- Cost is always zero (self-hosted, no billing).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    SearchResponse,
    SearchResult,
)

logger = logging.getLogger("peldrun.search.searxng")


class SearXNGSearchProvider(BaseSearchProvider):
    """
    Self-hosted meta-search provider delivering aggregated multi-engine
    results without quota restrictions. Cost: $0.00 per query.
    """

    provider_name: str = "SearXNG"
    cost_per_query: float = 0.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.base_url: str = str(
            self.config.get("base_url", "http://localhost:8080")
        ).rstrip("/")
        self.categories: str = str(self.config.get("categories", "general"))
        self.safesearch: int = int(self.config.get("safesearch", 0))
        self.time_format: str = str(self.config.get("time_format", ""))
        self.timeout_seconds: float = float(self.config.get("timeout_seconds", 10.0))
        self.max_results_cap: int = int(self.config.get("max_results_cap", 20))

    async def ping(self) -> bool:
        """
        Validates SearXNG instance connectivity by hitting the /config endpoint.
        Returns True only when the instance responds with HTTP 200.
        """
        if not self.base_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(f"{self.base_url}/config")
                return res.status_code == 200
        except Exception as exc:
            logger.debug(f"SearXNG ping failed: {exc}")
            return False

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

        if not self.base_url:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                is_success=False,
                error_message="SearXNG base_url is not configured.",
            )

        limit = max(1, min(int(max_results), self.max_results_cap))

        params: Dict[str, Any] = {
            "q": clean_q,
            "format": "json",
            "categories": self.categories,
            "language": lang or "en",
            "safesearch": self.safesearch,
        }
        if self.time_format:
            params["time_range"] = self.time_format

        results: List[SearchResult] = []

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True
            ) as client:
                resp = await client.get(f"{self.base_url}/search", params=params)

                if resp.status_code == 403:
                    raise RuntimeError(
                        "SearXNG rejected the request (HTTP 403). "
                        "The instance may have JSON format disabled. "
                        "Enable 'json' under 'search.formats' in settings.yml."
                    )
                if resp.status_code == 404:
                    raise RuntimeError(
                        "SearXNG endpoint not found (HTTP 404). "
                        "Verify that base_url points to a valid instance."
                    )
                if resp.status_code == 429:
                    raise RuntimeError(
                        "SearXNG rate limit exceeded (HTTP 429)."
                    )
                if resp.status_code >= 500:
                    raise RuntimeError(
                        f"SearXNG instance returned HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"SearXNG returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                data = resp.json()
                raw_results = data.get("results") or []

                for item in raw_results:
                    url = item.get("url") or ""
                    title = item.get("title") or ""
                    raw_content = item.get("content") or ""

                    score: Optional[float] = None
                    raw_score = item.get("score")
                    if raw_score is not None:
                        try:
                            score = float(raw_score)
                        except (ValueError, TypeError):
                            score = None

                    if url and title:
                        results.append(
                            SearchResult(
                                title=title,
                                url=url,
                                snippet=self.sanitize_snippet(raw_content),
                                score=score,
                            )
                        )
                    if len(results) >= limit:
                        break

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.warning(f"SearXNG search error: {exc}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                latency_ms=elapsed_ms,
                estimated_cost_usd=0.0,
                is_success=False,
                error_message=str(exc),
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        has_results = bool(results)

        return SearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=results,
            latency_ms=elapsed_ms,
            estimated_cost_usd=0.0,
            is_success=has_results,
            error_message=None if has_results else (
                "SearXNG returned zero results for this query. "
                "Verify that the instance has upstream engines enabled "
                "and that the query has not been filtered by safesearch."
            ),
        )


__all__ = ["SearXNGSearchProvider"]
