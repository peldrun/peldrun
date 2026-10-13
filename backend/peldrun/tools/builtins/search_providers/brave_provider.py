"""
backend/peldrun/tools/builtins/search_providers/brave_provider.py

Brave Search API Provider.
High-speed query engine accessing Brave's independent web index.

Fixes applied:
- Explicit success/failure reporting based on actual result count.
- Cost is only charged when results are returned.
- Descriptive error messages for zero-result responses.
- Removed manual Accept-Encoding header (httpx handles this automatically).
- Improved defensive parsing of nested API response structure.
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

logger = logging.getLogger("peldrun.search.brave")


class BraveSearchProvider(BaseSearchProvider):
    """
    Independent Web Index search provider via Brave Search Data API.
    Cost: approximately $0.005 per successful query.
    """

    provider_name: str = "Brave"
    cost_per_query: float = 0.005

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.endpoint: str = self.config.get(
            "endpoint", "https://api.search.brave.com/res/v1/web/search"
        )
        self.timeout_seconds: float = float(self.config.get("timeout_seconds", 10.0))
        self.max_results_cap: int = int(self.config.get("max_results_cap", 20))

    async def ping(self) -> bool:
        """
        Validates the Brave API key by issuing a minimal probe request.
        Returns True only when the API responds with HTTP 200.
        """
        if not self.api_key:
            return False
        try:
            headers = {"X-Subscription-Token": self.api_key}
            params = {"q": "ping", "count": 1}
            async with httpx.AsyncClient(timeout=4.0, headers=headers) as client:
                res = await client.get(self.endpoint, params=params)
                return res.status_code == 200
        except Exception as exc:
            logger.debug(f"Brave ping failed: {exc}")
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

        if not self.api_key:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                is_success=False,
                error_message="Brave API key (X-Subscription-Token) is not configured.",
            )

        limit = max(1, min(int(max_results), self.max_results_cap))

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }

        params = {
            "q": clean_q,
            "count": limit,
            "search_lang": lang or "en",
            "country": country or "us",
        }

        results: List[SearchResult] = []

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, headers=headers
            ) as client:
                resp = await client.get(self.endpoint, params=params)

                if resp.status_code == 401:
                    raise RuntimeError(
                        "Brave API rejected the subscription token (HTTP 401)."
                    )
                if resp.status_code == 429:
                    raise RuntimeError(
                        "Brave API rate limit exceeded (HTTP 429)."
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Brave API returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                data = resp.json()
                web_block = data.get("web") or {}
                web_results = web_block.get("results") or []

                for item in web_results:
                    url = item.get("url") or ""
                    title = item.get("title") or ""
                    description = item.get("description") or ""

                    if url and title:
                        results.append(
                            SearchResult(
                                title=title,
                                url=url,
                                snippet=self.sanitize_snippet(description),
                            )
                        )
                    if len(results) >= limit:
                        break

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.warning(f"Brave search error: {exc}")
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
                "Brave Search returned zero results for this query. "
                "The query may have no indexed matches, or the API may be degraded."
            ),
        )


__all__ = ["BraveSearchProvider"]
