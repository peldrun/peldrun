"""
backend/peldrun/tools/builtins/search_providers/tavily_provider.py

Tavily AI Search Provider.
Engineered for AI Agent workflows via https://api.tavily.com/search.

Key guarantees:
- Authenticates via the official `Authorization: Bearer <api_key>` header
  (while retaining `api_key` in the JSON body for backward compatibility).
- Supports all documented `search_depth` modes: 'basic', 'advanced',
  'fast', and 'ultra-fast'.
- Parses provider-reported credit usage (`usage.credits`) from the response
  and calculates cost using CostBasis.PROVIDER_REPORTED when available.
- Records request_count and cost independently of whether the query matched
  zero or N documents.
- Sanitizes and bounds `raw_content` using the shared untrusted-content policy.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from peldrun.tools.builtins.search_providers.base import (
    AuthenticationError,
    BaseSearchProvider,
    ConfigurationError,
    CostBasis,
    DiagnosticCheckType,
    InvalidResponseError,
    MAX_RAW_CONTENT_CHARS,
    MIN_RAW_CONTENT_CHARS,
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
    sanitize_untrusted_text,
)

logger = logging.getLogger("peldrun.search.tavily")


class TavilySearchProvider(BaseSearchProvider):
    """
    Cloud search provider tailored for LLM reasoning agents.
    Default estimated cost: $0.005 per credit.
    """

    provider_name: str = "Tavily"
    cost_per_query: float = 0.005
    default_cost_basis: CostBasis = CostBasis.PROVIDER_REPORTED
    hard_max_results: int = 20

    _VALID_DEPTHS: Tuple[str, ...] = ("basic", "advanced", "fast", "ultra-fast")

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.endpoint: str = (
            str(self.config.get("endpoint", "https://api.tavily.com/search")).strip()
            or "https://api.tavily.com/search"
        )

        requested_depth = str(self.config.get("search_depth", "basic")).lower().strip()
        self.search_depth: str = (
            requested_depth if requested_depth in self._VALID_DEPTHS else "basic"
        )

        self.include_answer: bool = bool(self.config.get("include_answer", False))
        self.include_raw_content: bool = bool(
            self.config.get("include_raw_content", False)
        )

        raw_preview_limit = self.config.get("raw_content_max_chars", 2000)
        try:
            parsed_preview = int(raw_preview_limit)
        except (TypeError, ValueError):
            parsed_preview = 2000
        self.raw_content_max_chars: int = max(
            MIN_RAW_CONTENT_CHARS,
            min(parsed_preview, MAX_RAW_CONTENT_CHARS),
        )

        raw_timeout = self.config.get("timeout_seconds", 15.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 15.0

        raw_cap = self.config.get("max_results_cap", self.hard_max_results)
        try:
            self.max_results_cap: int = max(
                1, min(int(raw_cap), self.hard_max_results)
            )
        except (TypeError, ValueError):
            self.max_results_cap = self.hard_max_results

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _extract_credits_and_cost(
        self,
        data: Dict[str, Any],
        depth_used: str,
    ) -> Tuple[float, float, CostBasis]:
        """
        Extract credit consumption from Tavily's `usage` block when present.
        Falls back to documented depth credit multiplier (2 for advanced, 1 otherwise).
        """
        usage_block = data.get("usage")
        if isinstance(usage_block, dict) and "credits" in usage_block:
            try:
                credits = max(0.0, float(usage_block["credits"]))
                return (
                    credits,
                    round(credits * self.cost_per_query, 6),
                    CostBasis.PROVIDER_REPORTED,
                )
            except (TypeError, ValueError):
                pass

        default_credits = 2.0 if depth_used == "advanced" else 1.0
        return (
            default_credits,
            round(default_credits * self.cost_per_query, 6),
            CostBasis.CONFIGURED_ESTIMATE,
        )

    async def _execute_request(
        self,
        clean_q: str,
        limit: int,
        depth: Optional[str] = None,
        timeout_override: Optional[float] = None,
    ) -> Tuple[List[SearchResult], float, float, CostBasis]:
        effective_depth = depth if depth in self._VALID_DEPTHS else self.search_depth
        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": clean_q,
            "max_results": max(1, min(int(limit), self.hard_max_results)),
            "search_depth": effective_depth,
            "include_answer": self.include_answer,
            "include_raw_content": self.include_raw_content,
        }

        effective_timeout = timeout_override or self.timeout_seconds
        try:
            async with httpx.AsyncClient(
                timeout=effective_timeout,
                headers=self._build_headers(),
            ) as client:
                resp = await client.post(self.endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Tavily request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Tavily network error: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code == 400:
            raise ConfigurationError(
                f"Tavily rejected the request (HTTP 400): {resp.text[:240]}",
                provider=self.provider_name,
                status_code=400,
            )
        if resp.status_code == 401:
            raise AuthenticationError(
                "Tavily rejected the API key (HTTP 401).",
                provider=self.provider_name,
                status_code=401,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "Tavily access forbidden (HTTP 403). Verify API key permissions.",
                provider=self.provider_name,
                status_code=403,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "Tavily rate limit exceeded (HTTP 429). Wait before retrying.",
                provider=self.provider_name,
                status_code=429,
            )
        if resp.status_code in (402, 432, 433):
            raise QuotaExceededError(
                f"Tavily plan quota or pay-as-you-go credit limit exhausted "
                f"(HTTP {resp.status_code}).",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise UpstreamError(
                f"Tavily server error (HTTP {resp.status_code}): {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise UpstreamError(
                f"Tavily API returned HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise InvalidResponseError(
                f"Tavily returned non-JSON response: {exc}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(data, dict):
            raise InvalidResponseError(
                "Tavily JSON response root must be an object.",
                provider=self.provider_name,
            )

        raw_results = data.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise InvalidResponseError(
                "Tavily response 'results' field must be a list.",
                provider=self.provider_name,
            )

        credits, cost_usd, cost_basis = self._extract_credits_and_cost(
            data, effective_depth
        )

        results: List[SearchResult] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            title = item.get("title") or ""
            content = item.get("content") or ""
            published_date = item.get("published_date")

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
                if isinstance(raw_field, str) and raw_field.strip():
                    raw_content_value = sanitize_untrusted_text(
                        raw_field,
                        max_chars=self.raw_content_max_chars,
                    )

            if url and title:
                res_obj = self.make_result(
                    title=title,
                    url=url,
                    snippet=content,
                    raw_content=raw_content_value,
                    score=score,
                    published_at=str(published_date) if published_date else None,
                )
                if res_obj is not None:
                    results.append(res_obj)
            if len(results) >= limit:
                break

        return results, credits, cost_usd, cost_basis

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
                message="Tavily API key is not configured.",
            )

        try:
            _, credits, cost_usd, basis = await self._execute_request(
                clean_q="tavily",
                limit=1,
                depth="basic",
                timeout_override=min(self.timeout_seconds, 6.0),
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
                usage_units=credits,
                estimated_cost_usd=cost_usd,
                cost_basis=basis,
                message=(
                    f"Tavily API key verified (HTTP 200, {credits:g} credit used)."
                ),
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
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
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
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
                message=f"Tavily connection failed: {exc}",
            )

    async def ping(self) -> bool:
        """
        Validates API key status with Tavily.
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
                error_message="Tavily API key is not configured.",
            )

        limit = self.clamp_results_limit(max_results, self.max_results_cap)

        try:
            results, credits, cost_usd, cost_basis = await self._execute_request(
                clean_q=clean_q,
                limit=limit,
            )
        except SearchProviderError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"Tavily search error [{exc.error_code}]: {exc.message}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=exc.status,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
                is_success=False,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"Tavily unexpected error: {exc}")
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.UPSTREAM_ERROR,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
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
            usage_units=credits,
            estimated_cost_usd=cost_usd,
            cost_basis=cost_basis,
            is_success=has_results,
            error_code=None if has_results else "empty_results",
            error_message=None
            if has_results
            else (
                "Tavily completed the search request (HTTP 200) but returned zero "
                "matching results for this query."
            ),
        )


__all__ = ["TavilySearchProvider"]
