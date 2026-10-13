"""
backend/peldrun/tools/builtins/search_providers/tavily_provider.py

Tavily AI Search Provider.
Engineered specifically for Autonomous AI Agents and LLM reasoning workflows.

Fixes applied:
- Explicit success/failure reporting based on actual result count.
- Cost is only charged when results are returned.
- Descriptive error messages for zero-result responses.
- Explicit handling of common HTTP error codes (400, 401, 403, 429, 432, 433).
- Validated search_depth against the API-supported enum ('basic' | 'advanced').
- Defensive parsing of nested API response structure.
- Optional raw_content inclusion for downstream LLM consumption.
- Safe score conversion with fallback for malformed values.
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

logger = logging.getLogger("peldrun.search.tavily")


class TavilySearchProvider(BaseSearchProvider):
    """
    Cloud-native search provider tailored for LLM reasoning agents.
    Cost: approximately $0.005 per successful query.
    """

    provider_name: str = "Tavily"
    cost_per_query: float = 0.005

    _VALID_DEPTHS = ("basic", "advanced")

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.endpoint: str = self.config.get(
            "endpoint", "https://api.tavily.com/search"
        )

        requested_depth = str(self.config.get("search_depth", "basic")).lower().strip()
        self.search_depth: str = (
            requested_depth if requested_depth in self._VALID_DEPTHS else "basic"
        )

        self.include_answer: bool = bool(self.config.get("include_answer", False))
        self.include_raw_content: bool = bool(
            self.config.get("include_raw_content", False)
        )
        self.timeout_seconds: float = float(self.config.get("timeout_seconds", 15.0))
        self.max_results_cap: int = int(self.config.get("max_results_cap", 20))

    async def ping(self) -> bool:
        """
        Validates API key status with Tavily by issuing a minimal probe.
        Returns True only when the API responds with HTTP 200.
        """
        if not self.api_key:
            return False
        try:
            payload = {
                "api_key": self.api_key,
                "query": "ping",
                "max_results": 1,
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(self.endpoint, json=payload)
                return res.status_code == 200
        except Exception as exc:
            logger.debug(f"Tavily ping failed: {exc}")
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
                error_message="Tavily API key is not configured.",
            )

        limit = max(1, min(int(max_results), self.max_results_cap))

        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": clean_q,
            "max_results": limit,
            "search_depth": self.search_depth,
            "include_answer": self.include_answer,
            "include_raw_content": self.include_raw_content,
        }

        results: List[SearchResult] = []

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(self.endpoint, json=payload)

                if resp.status_code == 400:
                    raise RuntimeError(
                        "Tavily rejected the request (HTTP 400). "
                        "The query or payload may be malformed."
                    )
                if resp.status_code == 401:
                    raise RuntimeError(
                        "Tavily rejected the API key (HTTP 401)."
                    )
                if resp.status_code == 403:
                    raise RuntimeError(
                        "Tavily access forbidden (HTTP 403). "
                        "The API key may lack permissions for this endpoint."
                    )
                if resp.status_code == 429:
                    raise RuntimeError(
                        "Tavily rate limit exceeded (HTTP 429). "
                        "Wait before retrying or upgrade the plan."
                    )
                if resp.status_code == 432:
                    raise RuntimeError(
                        "Tavily plan quota exhausted (HTTP 432). "
                        "Upgrade the plan or wait for the next billing cycle."
                    )
                if resp.status_code == 433:
                    raise RuntimeError(
                        "Tavily pay-as-you-go limit reached (HTTP 433). "
                        "Add credits or upgrade the plan."
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Tavily API returned HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                data = resp.json()
                raw_results = data.get("results") or []

                for item in raw_results:
                    url = item.get("url") or ""
                    title = item.get("title") or ""
                    content = item.get("content") or ""

                    score: Optional[float] = None
                    raw_score = item.get("score")
                    if raw_score is not None:
                        try:
                            score = float(raw_score)
                        except (ValueError, TypeError):
                            score = None

                    raw_content_value: Optional[str] = None
                    if self.include_raw_content:
                        raw_field = item.get("raw_content")
                        if isinstance(raw_field, str) and raw_field:
                            raw_content_value = raw_field[:2000]

                    if url and title:
                        results.append(
                            SearchResult(
                                title=title,
                                url=url,
                                snippet=self.sanitize_snippet(content),
                                raw_content=raw_content_value,
                                score=score,
                            )
                        )
                    if len(results) >= limit:
                        break

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.warning(f"Tavily search error: {exc}")
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
                "Tavily returned zero results for this query. "
                "The query may be too narrow, or the API may be degraded. "
                "Consider broadening the search terms."
            ),
        )


__all__ = ["TavilySearchProvider"]
