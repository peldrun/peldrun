"""
backend/peldrun/tools/builtins/search_providers/google_provider.py

Google Custom Search JSON API Provider.
Direct integration with the official Google Programmable Search Engine (CSE).

Fixes applied:
- Explicit success/failure reporting based on actual result count.
- Cost is only charged when results are returned.
- Descriptive error messages for zero-result responses.
- Explicit handling of common HTTP error codes (400, 401, 403, 429).
- Defensive parsing of nested API response structure.
- Centralized result limit clamping against user-configured cap.
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

logger = logging.getLogger("peldrun.search.google")


class GoogleSearchProvider(BaseSearchProvider):
    """
    Official Google Custom Search JSON API integration.
    Requires both an API key and a Search Engine ID (cx).
    Cost: approximately $0.005 per successful query beyond the free tier.
    """

    provider_name: str = "Google"
    cost_per_query: float = 0.005

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.cx: str = str(self.config.get("cx", "")).strip()
        self.endpoint: str = self.config.get(
            "endpoint", "https://www.googleapis.com/customsearch/v1"
        )
        self.timeout_seconds: float = float(self.config.get("timeout_seconds", 12.0))
        self.max_results_cap: int = int(self.config.get("max_results_cap", 10))

    async def ping(self) -> bool:
        """
        Validates Google API key and CX configuration with a minimal probe.
        Returns True only when the API responds with HTTP 200.
        """
        if not self.api_key or not self.cx:
            return False
        try:
            params = {"key": self.api_key, "cx": self.cx, "q": "ping", "num": 1}
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(self.endpoint, params=params)
                return res.status_code == 200
        except Exception as exc:
            logger.debug(f"Google ping failed: {exc}")
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

        if not self.api_key or not self.cx:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                is_success=False,
                error_message=(
                    "Google Custom Search requires both 'api_key' and "
                    "Search Engine ID ('cx'). One or both are missing."
                ),
            )

        limit = max(1, min(int(max_results), self.max_results_cap))

        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": clean_q,
            "num": limit,
            "hl": lang or "en",
            "gl": country or "us",
        }

        results: List[SearchResult] = []

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(self.endpoint, params=params)

                if resp.status_code == 400:
                    raise RuntimeError(
                        "Google CSE rejected the request (HTTP 400). "
                        "Verify the API key, CX ID, and query parameters."
                    )
                if resp.status_code == 401:
                    raise RuntimeError(
                        "Google CSE rejected the API key (HTTP 401)."
                    )
                if resp.status_code == 403:
                    raise RuntimeError(
                        "Google CSE access denied (HTTP 403). "
                        "The API key may be restricted or quota exhausted."
                    )
                if resp.status_code == 429:
                    raise RuntimeError(
                        "Google CSE rate limit exceeded (HTTP 429)."
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Google CSE API returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                data = resp.json()
                items = data.get("items") or []

                for item in items:
                    link = item.get("link") or ""
                    title = item.get("title") or ""
                    snippet = item.get("snippet") or ""

                    if link and title:
                        results.append(
                            SearchResult(
                                title=title,
                                url=link,
                                snippet=self.sanitize_snippet(snippet),
                            )
                        )
                    if len(results) >= limit:
                        break

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.warning(f"Google search error: {exc}")
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
            estimated_cost_usd=self.cost_per_query if has_results else 0.0,
            is_success=has_results,
            error_message=None if has_results else (
                "Google CSE returned zero results for this query. "
                "The query may have no indexed matches, or the CSE may need "
                "to be reconfigured to include more sites."
            ),
        )


__all__ = ["GoogleSearchProvider"]
