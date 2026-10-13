"""
backend/peldrun/tools/builtins/search_providers/jina_provider.py

Jina AI Reader & Search Provider.
Retrieves and converts live web queries into clean, LLM-tokenized Markdown.

Fixes applied:
- Switched to POST request with JSON body as per official Jina AI Search API.
- Query string is now passed as the 'q' body parameter, not in the URL path.
- Added required 'Content-Type: application/json' header.
- Passes 'num' to the API (capped at 20) for server-side result limiting.
- Supports 'gl' (country) and 'hl' (language) parameters.
- URL-encoding is no longer needed for the query since it's sent in the body.
- Diagnostic logging that always includes the exception class name.
- Explicit success/failure reporting based on actual result count.
- Cost is only charged when results are returned and a key is present.
- Descriptive error messages for zero-result responses.
- Explicit handling of common HTTP error codes (401, 402, 403, 429).
- Supports optional API key usage (rate-limited without key).
- Defensive parsing of nested API response structure.
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

logger = logging.getLogger("peldrun.search.jina")


class JinaSearchProvider(BaseSearchProvider):
    """
    Markdown-first reader and search engine powered by s.jina.ai.
    Operates in two modes:
      - Authenticated: requires an API key (higher rate limits, lower latency).
      - Anonymous: works without a key but is heavily rate-limited.
    Cost: approximately $0.002 per successful query when authenticated.
    """

    provider_name: str = "Jina"
    cost_per_query: float = 0.002

    # Jina's s.jina.ai validator rejects num > 20 with HTTP 400.
    # Requesting the default page (30 results) and slicing client-side
    # is safer, but we still pass a capped num to hint the server.
    _JINA_NUM_CAP: int = 20

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        base = str(self.config.get("base_url", "https://s.jina.ai/")).rstrip("/")
        self.base_search_url: str = base + "/"
        self.timeout_seconds: float = float(self.config.get("timeout_seconds", 15.0))
        self.max_results_cap: int = int(self.config.get("max_results_cap", 10))
        self.allow_no_key: bool = bool(self.config.get("allow_no_key", True))
        self.raw_content_preview_chars: int = int(
            self.config.get("raw_content_preview_chars", 1200)
        )

    async def ping(self) -> bool:
        """
        Validates reachability of the Jina search endpoint.

        Since s.jina.ai does not expose a lightweight /ping endpoint,
        this sends a minimal POST search request with num=1 and a very
        short timeout. Returns True for HTTP 200 (operational) or
        HTTP 401/402/429 (reachable but rate-limited or unauthorized,
        still indicates the service is alive).
        """
        try:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=4.0, headers=headers) as client:
                res = await client.post(
                    self.base_search_url,
                    json={"q": "ping", "num": 1},
                )
                return res.status_code in (200, 401, 402, 429)
        except Exception as exc:
            logger.debug(
                f"Jina ping failed [{type(exc).__name__}]: {exc or 'no message'}"
            )
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

        if not self.api_key and not self.allow_no_key:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                is_success=False,
                error_message=(
                    "Jina API key is not configured and anonymous access is "
                    "disabled (allow_no_key=false)."
                ),
            )

        # Clamp to our configured cap first, then to Jina's hard cap.
        limit = max(1, min(int(max_results), self.max_results_cap))
        api_num = min(limit, self._JINA_NUM_CAP)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",  # Required for POST + JSON body
            "X-No-Cache": "true",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Official Jina Search API: POST https://s.jina.ai/
        # Body parameters: q (required), num, page, gl, hl
        # See: https://docs.jina.ai / https://s.jina.ai/docs
        payload: Dict[str, Any] = {
            "q": clean_q,
            "num": api_num,
        }
        # Jina uses ISO 3166-1 alpha-2 for gl and ISO 639-1 for hl.
        if country:
            payload["gl"] = country.lower()[:2]
        if lang:
            payload["hl"] = lang.lower()[:2]

        results: List[SearchResult] = []

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=headers,
                follow_redirects=True,
            ) as client:
                resp = await client.post(
                    self.base_search_url,
                    json=payload,
                )

                if resp.status_code == 401:
                    raise RuntimeError(
                        "Jina AI rejected the API key (HTTP 401)."
                    )
                if resp.status_code == 402:
                    raise RuntimeError(
                        "Jina AI requires payment or a valid key (HTTP 402)."
                    )
                if resp.status_code == 403:
                    raise RuntimeError(
                        "Jina AI access forbidden (HTTP 403). "
                        "The endpoint may be restricted for this account."
                    )
                if resp.status_code == 429:
                    raise RuntimeError(
                        "Jina AI rate limit exceeded (HTTP 429). "
                        "Wait a few seconds or configure an API key."
                    )
                if resp.status_code == 400:
                    # Jina returns 400 for invalid parameters (e.g. num > 20).
                    body_preview = resp.text[:300]
                    raise RuntimeError(
                        f"Jina AI rejected the request parameters (HTTP 400): "
                        f"{body_preview}"
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Jina AI API returned HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )

                payload = resp.json()
                data_items = payload.get("data") or []

                for item in data_items:
                    url = item.get("url") or ""
                    title = item.get("title") or ""
                    content = item.get("content") or ""
                    description = item.get("description") or ""

                    snippet_text = description if description else content[:400]

                    if url and title:
                        raw_preview: Optional[str] = None
                        if content:
                            raw_preview = content[: self.raw_content_preview_chars]

                        results.append(
                            SearchResult(
                                title=title,
                                url=url,
                                snippet=self.sanitize_snippet(snippet_text),
                                raw_content=raw_preview,
                            )
                        )
                    if len(results) >= limit:
                        break

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            # Always log the exception class name, even when str(exc) is empty.
            exc_name = type(exc).__name__
            exc_msg = str(exc) or "(no message)"
            logger.warning(f"Jina search error [{exc_name}]: {exc_msg}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                latency_ms=elapsed_ms,
                estimated_cost_usd=0.0,
                is_success=False,
                error_message=f"Jina AI [{exc_name}]: {exc_msg}",
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        has_results = bool(results)

        # Jina charges only when authenticated AND results are actually returned
        charged_cost = self.cost_per_query if (has_results and self.api_key) else 0.0

        return SearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=results,
            latency_ms=elapsed_ms,
            estimated_cost_usd=charged_cost,
            is_success=has_results,
            error_message=None if has_results else (
                "Jina AI returned zero results for this query. "
                "The query may be too narrow, or the endpoint may be "
                "temporarily degraded. Try again shortly."
            ),
        )


__all__ = ["JinaSearchProvider"]
