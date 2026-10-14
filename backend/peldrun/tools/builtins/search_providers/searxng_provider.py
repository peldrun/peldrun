"""
backend/peldrun/tools/builtins/search_providers/searxng_provider.py

SearXNG Self-Hosted Meta-Search Provider.
Queries local or private SearXNG JSON API endpoints (/search?format=json).

Key guarantees:
- Validates base_url format before issuing requests.
- Separates transport reachability (/config) from JSON search readiness
  (/search?format=json) during diagnostics, immediately surfacing HTTP 403
  when 'json' is missing from SearXNG's `search.formats` setting.
- Defaults to $0.00 (CostBasis.ASSUMED_ZERO_LOCAL) while honoring an explicit
  non-zero user cost override when configured.
- Distinguishes valid empty search results (ProviderStatus.EMPTY) from
  instance or upstream failures.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

import httpx

from peldrun.tools.builtins.search_providers.base import (
    AuthenticationError,
    BaseSearchProvider,
    ConfigurationError,
    CostBasis,
    DiagnosticCheckType,
    InvalidResponseError,
    PermissionError,
    ProviderDiagnosticResult,
    ProviderStatus,
    RateLimitError,
    SearchProviderError,
    SearchResponse,
    SearchResult,
    TimeoutError,
    UpstreamError,
)

logger = logging.getLogger("peldrun.search.searxng")


class SearXNGSearchProvider(BaseSearchProvider):
    """
    Self-hosted meta-search provider querying a SearXNG instance's JSON API.
    Default cost: $0.00 per query (self-hosted local/private instance).
    """

    provider_name: str = "SearXNG"
    cost_per_query: float = 0.0
    default_cost_basis: CostBasis = CostBasis.ASSUMED_ZERO_LOCAL
    hard_max_results: int = 20

    _VALID_TIME_RANGES: Tuple[str, ...] = ("", "day", "week", "month", "year")

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        raw_base = str(self.config.get("base_url", "http://localhost:8080")).strip()
        self.base_url: str = raw_base.rstrip("/")
        self.categories: str = (
            str(self.config.get("categories", "general")).strip() or "general"
        )

        raw_safesearch = self.config.get("safesearch", 0)
        try:
            self.safesearch: int = max(0, min(int(raw_safesearch), 2))
        except (TypeError, ValueError):
            self.safesearch = 0

        raw_time = str(self.config.get("time_format", "")).strip().lower()
        self.time_format: str = (
            raw_time if raw_time in self._VALID_TIME_RANGES else ""
        )

        raw_timeout = self.config.get("timeout_seconds", 10.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 10.0

        raw_cap = self.config.get("max_results_cap", self.hard_max_results)
        try:
            self.max_results_cap: int = max(
                1, min(int(raw_cap), self.hard_max_results)
            )
        except (TypeError, ValueError):
            self.max_results_cap = self.hard_max_results

    def _validate_base_url(self) -> None:
        """Validate that base_url is a well-formed http/https URL."""
        if not self.base_url:
            raise ConfigurationError(
                "SearXNG base_url is not configured.",
                provider=self.provider_name,
            )
        try:
            parsed = urllib.parse.urlsplit(self.base_url)
        except Exception as exc:
            raise ConfigurationError(
                f"SearXNG base_url is malformed: {exc}",
                provider=self.provider_name,
            ) from exc

        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigurationError(
                f"SearXNG base_url '{self.base_url}' must start with http:// or https:// "
                f"and include a valid hostname.",
                provider=self.provider_name,
            )

    def _resolve_cost_and_basis(self) -> Tuple[float, CostBasis]:
        """
        Return effective cost and basis.
        Self-hosted SearXNG defaults to $0.00 (ASSUMED_ZERO_LOCAL), but if the
        user explicitly configured a positive estimate, honor it consistently.
        """
        if self.cost_overridden and self.cost_per_query > 0.0:
            return self.cost_per_query, CostBasis.CONFIGURED_ESTIMATE
        return 0.0, CostBasis.ASSUMED_ZERO_LOCAL

    async def _execute_search_request(
        self,
        clean_q: str,
        limit: int,
        lang: str = "en",
        timeout_override: Optional[float] = None,
    ) -> List[SearchResult]:
        params: Dict[str, Any] = {
            "q": clean_q,
            "format": "json",
            "categories": self.categories,
            "language": (lang or "en").strip(),
            "safesearch": self.safesearch,
        }
        if self.time_format:
            params["time_range"] = self.time_format

        effective_timeout = timeout_override or self.timeout_seconds
        headers = {"Accept": "application/json"}

        try:
            async with httpx.AsyncClient(
                timeout=effective_timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(f"{self.base_url}/search", params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"SearXNG request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Failed to connect to SearXNG instance at {self.base_url}: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code == 401:
            raise AuthenticationError(
                "SearXNG instance requires authentication (HTTP 401).",
                provider=self.provider_name,
                status_code=401,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "SearXNG rejected the JSON search request (HTTP 403). "
                "Ensure 'json' is enabled under 'search.formats' in the instance's "
                "settings.yml and that bot/limiter rules permit local API requests.",
                provider=self.provider_name,
                status_code=403,
            )
        if resp.status_code == 404:
            raise ConfigurationError(
                f"SearXNG search endpoint not found at {self.base_url}/search (HTTP 404). "
                f"Verify that base_url points to the root of a valid SearXNG instance.",
                provider=self.provider_name,
                status_code=404,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "SearXNG instance rate limit or limiter threshold exceeded (HTTP 429).",
                provider=self.provider_name,
                status_code=429,
            )
        if resp.status_code >= 500:
            raise UpstreamError(
                f"SearXNG instance returned server error HTTP {resp.status_code}: "
                f"{resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise UpstreamError(
                f"SearXNG returned unexpected HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise InvalidResponseError(
                "SearXNG returned a non-JSON response. Verify that 'json' format is "
                "enabled in SearXNG settings.yml.",
                provider=self.provider_name,
            ) from exc

        if not isinstance(data, dict):
            raise InvalidResponseError(
                "SearXNG JSON response root must be an object.",
                provider=self.provider_name,
            )

        raw_results = data.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise InvalidResponseError(
                "SearXNG response 'results' field must be a list.",
                provider=self.provider_name,
            )

        results: List[SearchResult] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            title = item.get("title") or ""
            raw_content = item.get("content") or ""
            published_Date = item.get("publishedDate") or item.get("pubdate")

            score: Optional[float] = None
            raw_score = item.get("score")
            if raw_score is not None:
                try:
                    score = float(raw_score)
                except (ValueError, TypeError):
                    score = None

            if url and title:
                res_obj = self.make_result(
                    title=title,
                    url=url,
                    snippet=raw_content,
                    score=score,
                    published_at=str(published_Date) if published_Date else None,
                )
                if res_obj is not None:
                    results.append(res_obj)
            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # Diagnostics & Health Check
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        check_type: DiagnosticCheckType = DiagnosticCheckType.REACHABILITY,
    ) -> ProviderDiagnosticResult:
        """
        Two-stage SearXNG diagnostic:
          1. Transport reachability check against /config.
          2. JSON search format verification against /search?format=json so that
             disabled JSON format (HTTP 403) is caught immediately.
        """
        start_time = time.perf_counter()
        try:
            self._validate_base_url()
        except ConfigurationError as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ProviderDiagnosticResult(
                ok=False,
                provider=self.provider_name,
                check_type=check_type,
                status=ProviderStatus.CONFIGURATION_ERROR,
                reachable=False,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                message=exc.message,
            )

        probe_timeout = min(self.timeout_seconds, 5.0)
        config_reachable = False
        requests_made = 0

        try:
            requests_made += 1
            async with httpx.AsyncClient(
                timeout=probe_timeout,
                follow_redirects=True,
            ) as client:
                cfg_res = await client.get(f"{self.base_url}/config")
                config_reachable = cfg_res.status_code == 200
        except Exception as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ProviderDiagnosticResult(
                ok=False,
                provider=self.provider_name,
                check_type=check_type,
                status=ProviderStatus.UPSTREAM_ERROR,
                reachable=False,
                search_operational=False,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=requests_made,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                message=f"SearXNG instance unreachable at {self.base_url}: {exc}",
            )

        # Verify that /search?format=json is permitted on the instance
        try:
            requests_made += 1
            await self._execute_search_request(
                clean_q="searxng",
                limit=1,
                lang="en",
                timeout_override=probe_timeout,
            )
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            cost_val, basis = self._resolve_cost_and_basis()
            return ProviderDiagnosticResult(
                ok=True,
                provider=self.provider_name,
                check_type=check_type,
                status=ProviderStatus.SUCCESS,
                reachable=True,
                authenticated=True,
                quota_available=True,
                search_operational=True,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=requests_made,
                estimated_cost_usd=cost_val,
                cost_basis=basis,
                message="SearXNG instance reachable and JSON search format verified.",
            )
        except SearchProviderError as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ProviderDiagnosticResult(
                ok=False,
                provider=self.provider_name,
                check_type=check_type,
                status=exc.status,
                reachable=config_reachable or exc.status == ProviderStatus.PERMISSION_ERROR,
                authenticated=False if exc.status == ProviderStatus.AUTHENTICATION_ERROR else None,
                search_operational=False,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=requests_made,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                message=exc.message,
            )

    async def ping(self) -> bool:
        """
        Return True only when the SearXNG instance is reachable and accepts
        JSON search requests.
        """
        diag = await self.diagnose(check_type=DiagnosticCheckType.REACHABILITY)
        return diag.ok

    # ------------------------------------------------------------------
    # Public Search Entry Point
    # ------------------------------------------------------------------
    async def search(
        self,
        query: str,
        max_results: int = 5,
        lang: str = "en",
        country: str = "us",
        **kwargs: Any,
    ) -> SearchResponse:
        start_time = time.perf_counter()
        clean_q = (query or "").strip()

        if not clean_q:
            return SearchResponse(
                query=query or "",
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="empty_query",
                error_message="Search query cannot be empty.",
            )

        try:
            self._validate_base_url()
        except ConfigurationError as exc:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code=exc.error_code,
                error_message=exc.message,
            )

        limit = self.clamp_results_limit(max_results, self.max_results_cap)
        cost_val, cost_basis = self._resolve_cost_and_basis()

        try:
            results = await self._execute_search_request(
                clean_q=clean_q,
                limit=limit,
                lang=lang,
            )
        except SearchProviderError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"SearXNG search error [{exc.error_code}]: {exc.message}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=exc.status,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=cost_basis,
                is_success=False,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"SearXNG unexpected error: {exc}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.UPSTREAM_ERROR,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=cost_basis,
                is_success=False,
                error_code="upstream_error",
                error_message=str(exc),
            )

        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        has_results = bool(results)

        return SearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=results,
            status=ProviderStatus.SUCCESS if has_results else ProviderStatus.EMPTY,
            latency_ms=elapsed_ms,
            request_count=1,
            usage_units=1.0,
            estimated_cost_usd=cost_val,
            cost_basis=cost_basis,
            is_success=has_results,
            error_code=None if has_results else "empty_results",
            error_message=None
            if has_results
            else (
                "SearXNG completed the request (HTTP 200) but returned zero results. "
                "Verify that upstream engines are enabled and responsive on the instance."
            ),
        )


__all__ = ["SearXNGSearchProvider"]
