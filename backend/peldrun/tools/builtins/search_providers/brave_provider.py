"""
backend/peldrun/tools/builtins/search_providers/brave_provider.py

Brave Search API Provider.
Queries Brave's independent web index via
https://api.search.brave.com/res/v1/web/search.

Key guarantees:
- Hard-enforces Brave's official maximum `count` of 20 results per query
  even if a higher cap is written in config.toml.
- Validates and normalizes `country` and `search_lang` codes before sending.
- Records request_count and estimated cost for every processed HTTP 200
  search request independently of whether zero or N results matched.
- Classifies HTTP 400/401/402/403/422/429/5xx into typed provider errors.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

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
    QuotaExceededError,
    RateLimitError,
    SearchProviderError,
    SearchResponse,
    SearchResult,
    TimeoutError,
    UpstreamError,
)

logger = logging.getLogger("peldrun.search.brave")

_COUNTRY_RE = re.compile(r"^[a-zA-Z]{2}$")
_LANG_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?$")


class BraveSearchProvider(BaseSearchProvider):
    """
    Independent Web Index search provider via Brave Search Data API.
    Default estimated cost: $0.005 per query.
    """

    provider_name: str = "Brave"
    cost_per_query: float = 0.005
    default_cost_basis: CostBasis = CostBasis.CONFIGURED_ESTIMATE
    hard_max_results: int = 20

    _BRAVE_HARD_CAP: int = 20

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.endpoint: str = (
            str(
                self.config.get(
                    "endpoint", "https://api.search.brave.com/res/v1/web/search"
                )
            ).strip()
            or "https://api.search.brave.com/res/v1/web/search"
        )

        raw_timeout = self.config.get("timeout_seconds", 10.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 10.0

        raw_cap = self.config.get("max_results_cap", self._BRAVE_HARD_CAP)
        try:
            self.max_results_cap: int = max(
                1, min(int(raw_cap), self._BRAVE_HARD_CAP)
            )
        except (TypeError, ValueError):
            self.max_results_cap = self._BRAVE_HARD_CAP

    @staticmethod
    def _normalize_country(country: str) -> str:
        c = (country or "us").strip().lower()
        if _COUNTRY_RE.match(c):
            return c
        return "us"

    @staticmethod
    def _normalize_lang(lang: str) -> str:
        l = (lang or "en").strip().lower()
        if _LANG_RE.match(l):
            return l
        return "en"

    async def _execute_request(
        self,
        clean_q: str,
        limit: int,
        lang: str = "en",
        country: str = "us",
        timeout_override: Optional[float] = None,
    ) -> List[SearchResult]:
        # Hard-enforce Brave's documented maximum count of 20
        bounded_count = max(
            1, min(int(limit), self.max_results_cap, self._BRAVE_HARD_CAP)
        )

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {
            "q": clean_q,
            "count": bounded_count,
            "search_lang": self._normalize_lang(lang),
            "country": self._normalize_country(country),
        }

        effective_timeout = timeout_override or self.timeout_seconds
        try:
            async with httpx.AsyncClient(
                timeout=effective_timeout,
                headers=headers,
            ) as client:
                resp = await client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Brave Search request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Brave Search network error: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code in (400, 422):
            raise ConfigurationError(
                f"Brave API rejected the query parameters (HTTP {resp.status_code}): "
                f"{resp.text[:240]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code == 401:
            raise AuthenticationError(
                "Brave API rejected the subscription token (HTTP 401).",
                provider=self.provider_name,
                status_code=401,
            )
        if resp.status_code == 402:
            raise QuotaExceededError(
                "Brave API subscription quota or payment limit reached (HTTP 402).",
                provider=self.provider_name,
                status_code=402,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "Brave API access forbidden (HTTP 403). Verify subscription plan permissions.",
                provider=self.provider_name,
                status_code=403,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "Brave API rate limit exceeded (HTTP 429).",
                provider=self.provider_name,
                status_code=429,
            )
        if resp.status_code >= 500:
            raise UpstreamError(
                f"Brave API server error (HTTP {resp.status_code}): {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise UpstreamError(
                f"Brave API returned HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise InvalidResponseError(
                f"Brave API returned non-JSON response: {exc}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(data, dict):
            raise InvalidResponseError(
                "Brave API JSON response root must be an object.",
                provider=self.provider_name,
            )

        web_block = data.get("web")
        if web_block is None:
            web_results: List[Any] = []
        elif isinstance(web_block, dict):
            raw_list = web_block.get("results")
            if raw_list is None:
                web_results = []
            elif isinstance(raw_list, list):
                web_results = raw_list
            else:
                raise InvalidResponseError(
                    "Brave API 'web.results' field must be a list.",
                    provider=self.provider_name,
                )
        else:
            raise InvalidResponseError(
                "Brave API 'web' field must be an object.",
                provider=self.provider_name,
            )

        results: List[SearchResult] = []
        for item in web_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            title = item.get("title") or ""
            description = item.get("description") or ""
            age = item.get("age") or item.get("page_age")

            if url and title:
                res_obj = self.make_result(
                    title=title,
                    url=url,
                    snippet=description,
                    published_at=str(age) if age else None,
                )
                if res_obj is not None:
                    results.append(res_obj)
            if len(results) >= bounded_count:
                break

        return results

    # ------------------------------------------------------------------
    # Diagnostics & Health Check
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        check_type: DiagnosticCheckType = DiagnosticCheckType.CREDENTIALS,
    ) -> ProviderDiagnosticResult:
        start_time = time.perf_counter()
        if not self.api_key:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ProviderDiagnosticResult(
                ok=False,
                provider=self.provider_name,
                check_type=check_type,
                status=ProviderStatus.CONFIGURATION_ERROR,
                reachable=False,
                authenticated=False,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                message="Brave API key (X-Subscription-Token) is not configured.",
            )

        try:
            await self._execute_request(
                clean_q="brave",
                limit=1,
                timeout_override=min(self.timeout_seconds, 5.0),
            )
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ProviderDiagnosticResult(
                ok=True,
                provider=self.provider_name,
                check_type=check_type,
                status=ProviderStatus.SUCCESS,
                reachable=True,
                authenticated=True,
                quota_available=True,
                search_operational=True,
                consumes_quota=True,
                latency_ms=elapsed,
                request_count=1,
                usage_units=1.0,
                estimated_cost_usd=self.cost_per_query,
                cost_basis=self.default_cost_basis,
                message="Brave Search subscription token verified (HTTP 200, 1 query used).",
            )
        except SearchProviderError as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            reachable = exc.status in (
                ProviderStatus.AUTHENTICATION_ERROR,
                ProviderStatus.PERMISSION_ERROR,
                ProviderStatus.QUOTA_EXHAUSTED,
                ProviderStatus.RATE_LIMITED,
                ProviderStatus.CONFIGURATION_ERROR,
            )
            return ProviderDiagnosticResult(
                ok=False,
                provider=self.provider_name,
                check_type=check_type,
                status=exc.status,
                reachable=reachable,
                authenticated=(
                    False
                    if exc.status
                    in (
                        ProviderStatus.AUTHENTICATION_ERROR,
                        ProviderStatus.PERMISSION_ERROR,
                    )
                    else (
                        True
                        if exc.status == ProviderStatus.QUOTA_EXHAUSTED
                        else None
                    )
                ),
                quota_available=(
                    False
                    if exc.status
                    in (ProviderStatus.QUOTA_EXHAUSTED, ProviderStatus.RATE_LIMITED)
                    else None
                ),
                search_operational=False,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
                message=exc.message,
            )
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
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
                message=f"Brave Search connection failed: {exc}",
            )

    async def ping(self) -> bool:
        """
        Validates the Brave API key.
        Returns True only when the API responds with HTTP 200.
        """
        diag = await self.diagnose(check_type=DiagnosticCheckType.CREDENTIALS)
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

        if not self.api_key:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="missing_api_key",
                error_message="Brave API key (X-Subscription-Token) is not configured.",
            )

        limit = self.clamp_results_limit(max_results, self.max_results_cap)

        try:
            results = await self._execute_request(
                clean_q=clean_q,
                limit=limit,
                lang=lang,
                country=country,
            )
        except SearchProviderError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"Brave search error [{exc.error_code}]: {exc.message}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=exc.status,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
                is_success=False,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"Brave unexpected error: {exc}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.UPSTREAM_ERROR,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
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
            estimated_cost_usd=self.cost_per_query,
            cost_basis=self.default_cost_basis,
            is_success=has_results,
            error_code=None if has_results else "empty_results",
            error_message=None
            if has_results
            else (
                "Brave Search completed the request (HTTP 200) but returned zero "
                "matching results for this query."
            ),
        )


__all__ = ["BraveSearchProvider"]
