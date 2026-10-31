"""
backend/peldrun/tools/builtins/search_providers/google_provider.py

Google Custom Search JSON API Provider (Legacy Integration).
Direct integration with Google Programmable Search Engine (CSE)
via https://www.googleapis.com/customsearch/v1.

Key guarantees:
- Hard-enforces Google's documented maximum `num` of 10 results per query
  even if a higher cap is written in config.toml.
- Parses structured Google JSON error payloads (`error.errors[].reason`,
  `error.status`) to accurately distinguish quota exhaustion, invalid API keys,
  disabled APIs, and invalid CX identifiers.
- Records request_count and estimated cost for every processed HTTP 200
  request independently of whether zero or N items matched.
- Carries explicit legacy lifecycle metadata noting Google's 2027-01-01
  retirement schedule for Custom Search JSON API.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from peldrun.tools.builtins.search_providers.base import (
    AuthenticationError,
    BaseSearchProvider,
    ConfigurationError,
    CostBasis,
    DiagnosticCheckType,
    ImageSearchResponse,
    ImageSearchResult,
    InvalidResponseError,
    PermissionError,
    ProviderDiagnosticResult,
    ProviderLifecycle,
    ProviderStatus,
    QuotaExceededError,
    RateLimitError,
    SearchProviderError,
    SearchResponse,
    SearchResult,
    TimeoutError,
    UpstreamError,
)

logger = logging.getLogger("peldrun.search.google")


class GoogleSearchProvider(BaseSearchProvider):
    """
    Google Custom Search JSON API integration (Legacy — scheduled for
    retirement on 2027-01-01).
    Requires both an API key ('api_key') and a Search Engine ID ('cx').
    Default estimated cost: $0.005 per query.
    """

    provider_name: str = "Google"
    cost_per_query: float = 0.005
    default_cost_basis: CostBasis = CostBasis.CONFIGURED_ESTIMATE
    hard_max_results: int = 10

    _GOOGLE_HARD_CAP: int = 10
    LIFECYCLE: ProviderLifecycle = ProviderLifecycle.LEGACY
    LEGACY_RETIREMENT_DATE: str = "2027-01-01"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.cx: str = str(self.config.get("cx", "")).strip()
        self.endpoint: str = (
            str(
                self.config.get(
                    "endpoint", "https://www.googleapis.com/customsearch/v1"
                )
            ).strip()
            or "https://www.googleapis.com/customsearch/v1"
        )

        raw_timeout = self.config.get("timeout_seconds", 12.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 12.0

        raw_cap = self.config.get("max_results_cap", self._GOOGLE_HARD_CAP)
        try:
            self.max_results_cap: int = max(
                1, min(int(raw_cap), self._GOOGLE_HARD_CAP)
            )
        except (TypeError, ValueError):
            self.max_results_cap = self._GOOGLE_HARD_CAP

    def _raise_classified_google_error(self, resp: httpx.Response) -> None:
        """
        Inspect Google's structured JSON error envelope to classify 400/401/403/429
        into specific typed exceptions.
        """
        status_code = resp.status_code
        reason = ""
        message = ""
        google_status = ""

        try:
            payload = resp.json()
            if isinstance(payload, dict):
                err_obj = payload.get("error")
                if isinstance(err_obj, dict):
                    message = str(err_obj.get("message") or "").strip()
                    google_status = str(err_obj.get("status") or "").strip()
                    errors_list = err_obj.get("errors")
                    if isinstance(errors_list, list) and errors_list:
                        first_err = errors_list[0]
                        if isinstance(first_err, dict):
                            reason = str(first_err.get("reason") or "").strip()
        except Exception:
            pass

        detail = message or resp.text[:200] or f"HTTP {status_code}"
        reason_lower = reason.lower()
        msg_lower = detail.lower()

        if status_code == 400:
            if "keyinvalid" in reason_lower or "api key not valid" in msg_lower:
                raise AuthenticationError(
                    f"Google CSE rejected the API key: {detail}",
                    provider=self.provider_name,
                    status_code=400,
                )
            raise ConfigurationError(
                f"Google CSE rejected the request parameters (HTTP 400): {detail}",
                provider=self.provider_name,
                status_code=400,
            )

        if status_code == 401:
            raise AuthenticationError(
                f"Google CSE authentication failed (HTTP 401): {detail}",
                provider=self.provider_name,
                status_code=401,
            )

        if status_code == 403:
            if reason_lower in (
                "dailylimitexceeded",
                "quotaexceeded",
                "userratelimitexceeded",
                "ratelimitexceeded",
            ) or "quota" in msg_lower or "daily limit" in msg_lower:
                raise QuotaExceededError(
                    f"Google CSE quota exhausted (HTTP 403 / {reason or 'quotaExceeded'}): "
                    f"{detail}",
                    provider=self.provider_name,
                    status_code=403,
                )
            if "keyinvalid" in reason_lower or "api key not valid" in msg_lower:
                raise AuthenticationError(
                    f"Google CSE rejected the API key (HTTP 403): {detail}",
                    provider=self.provider_name,
                    status_code=403,
                )
            raise PermissionError(
                f"Google CSE access denied (HTTP 403 / {reason or google_status or 'forbidden'}): "
                f"{detail}",
                provider=self.provider_name,
                status_code=403,
            )

        if status_code == 429:
            raise RateLimitError(
                f"Google CSE rate limit exceeded (HTTP 429): {detail}",
                provider=self.provider_name,
                status_code=429,
            )

        if status_code >= 500:
            raise UpstreamError(
                f"Google CSE server error (HTTP {status_code}): {detail}",
                provider=self.provider_name,
                status_code=status_code,
            )

        raise UpstreamError(
            f"Google CSE returned HTTP {status_code}: {detail}",
            provider=self.provider_name,
            status_code=status_code,
        )

    async def _execute_request(
        self,
        clean_q: str,
        limit: int,
        lang: str = "en",
        country: str = "us",
        timeout_override: Optional[float] = None,
    ) -> List[SearchResult]:
        # Hard-enforce Google CSE's documented maximum `num` of 10
        bounded_num = max(
            1, min(int(limit), self.max_results_cap, self._GOOGLE_HARD_CAP)
        )

        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": clean_q,
            "num": bounded_num,
            "hl": (lang or "en").strip(),
            "gl": (country or "us").strip(),
        }

        effective_timeout = timeout_override or self.timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Google CSE request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Google CSE network error: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code != 200:
            self._raise_classified_google_error(resp)

        try:
            data = resp.json()
        except Exception as exc:
            raise InvalidResponseError(
                f"Google CSE returned non-JSON response: {exc}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(data, dict):
            raise InvalidResponseError(
                "Google CSE JSON response root must be an object.",
                provider=self.provider_name,
            )

        items = data.get("items")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise InvalidResponseError(
                "Google CSE response 'items' field must be a list.",
                provider=self.provider_name,
            )

        results: List[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            link = item.get("link") or ""
            title = item.get("title") or ""
            snippet = item.get("snippet") or ""

            if link and title:
                res_obj = self.make_result(
                    title=title,
                    url=link,
                    snippet=snippet,
                )
                if res_obj is not None:
                    results.append(res_obj)
            if len(results) >= bounded_num:
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
        if not self.api_key or not self.cx:
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
                message=(
                    "Google Custom Search requires both 'api_key' and "
                    "Search Engine ID ('cx')."
                ),
            )

        try:
            await self._execute_request(
                clean_q="google",
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
                message=(
                    f"Google CSE verified (HTTP 200, 1 quota unit used; "
                    f"legacy API retires {self.LEGACY_RETIREMENT_DATE})."
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
                message=f"Google CSE connection failed: {exc}",
            )

    async def ping(self) -> bool:
        """
        Validates Google API key and CX configuration.
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

        if not self.api_key or not self.cx:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="missing_credentials",
                error_message=(
                    "Google Custom Search requires both 'api_key' and "
                    "Search Engine ID ('cx'). One or both are missing."
                ),
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
            logger.warning(f"Google search error [{exc.error_code}]: {exc.message}")
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
            logger.warning(f"Google unexpected error: {exc}")
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
                "Google CSE completed the request (HTTP 200) but returned zero "
                "results for this query."
            ),
        )

    # ------------------------------------------------------------------
    # Image Search
    # ------------------------------------------------------------------
    async def image_search(
        self,
        query: str,
        max_results: int = 3,
        lang: str = "en",
        country: str = "us",
        **kwargs: Any,
    ) -> ImageSearchResponse:
        """
        Execute a Google CSE image search via the same customsearch endpoint
        with searchType=image. Google's JSON API caps 'num' at 10 for image
        requests just like web search; that limit is hard-enforced here.
        """
        start_time = time.perf_counter()
        clean_q = (query or "").strip()

        if not clean_q:
            return ImageSearchResponse(
                query=query or "",
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="empty_query",
                error_message="Image search query cannot be empty.",
            )

        if not self.api_key or not self.cx:
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="missing_credentials",
                error_message=(
                    "Google Custom Search requires both 'api_key' and "
                    "Search Engine ID ('cx'). One or both are missing."
                ),
            )

        bounded_num = max(
            1, min(int(max_results), self.max_results_cap, self._GOOGLE_HARD_CAP)
        )

        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": clean_q,
            "num": bounded_num,
            "searchType": "image",
            "hl": (lang or "en").strip(),
            "gl": (country or "us").strip(),
            "safe": "active",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.TIMEOUT,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
                is_success=False,
                error_code="timeout",
                error_message=(
                    f"Google image search timed out after "
                    f"{self.timeout_seconds:.1f}s."
                ),
            )
        except httpx.HTTPError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ImageSearchResponse(
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
                error_message=f"Google image search network error: {exc}",
            )

        if resp.status_code != 200:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            try:
                self._raise_classified_google_error(resp)
            except SearchProviderError as exc:
                logger.warning(
                    f"Google image search error [{exc.error_code}]: {exc.message}"
                )
                return ImageSearchResponse(
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
            return ImageSearchResponse(
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
                error_message=(
                    f"Google image search returned unexpected HTTP "
                    f"{resp.status_code}: {resp.text[:200]}"
                ),
            )

        try:
            data = resp.json()
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.INVALID_RESPONSE,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
                is_success=False,
                error_code="invalid_response",
                error_message=f"Google returned non-JSON image response: {exc}",
            )

        if not isinstance(data, dict):
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.INVALID_RESPONSE,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=self.default_cost_basis,
                is_success=False,
                error_code="invalid_response",
                error_message=(
                    "Google image search JSON response root must be an object."
                ),
            )

        items = data.get("items")
        if not isinstance(items, list):
            items = []

        results: List[ImageSearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            image_obj = item.get("image") if isinstance(item.get("image"), dict) else {}
            img_url = item.get("link") or image_obj.get("thumbnailLink") or ""
            if not img_url:
                continue

            parsed = self.make_image_result(
                title=item.get("title") or item.get("snippet") or "",
                image_url=img_url,
                thumbnail_url=image_obj.get("thumbnailLink") or "",
                source_url=image_obj.get("contextLink") or "",
                width=image_obj.get("width"),
                height=image_obj.get("height"),
            )
            if parsed is not None:
                results.append(parsed)
            if len(results) >= bounded_num:
                break

        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        has_results = bool(results)

        return ImageSearchResponse(
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
                "Google CSE image search completed the request (HTTP 200) but "
                "returned zero matching images for this query."
            ),
            effective_max_results=bounded_num,
        )


__all__ = ["GoogleSearchProvider"]
