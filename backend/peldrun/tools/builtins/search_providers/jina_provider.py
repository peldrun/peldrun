"""
backend/peldrun/tools/builtins/search_providers/jina_provider.py

Jina AI Search Provider (s.jina.ai).
Queries the Jina Search JSON API and converts results into bounded,
validated SearchResult records.

Key guarantees:
- Defaults to fast SERP mode (`X-Respond-With: no-content`) unless
  `include_raw_content=True` is explicitly configured, avoiding 15s+
  full-page scraping timeouts on standard search queries.
- ping() and diagnose() never report HTTP 401 (unauthorized), 402 (payment/quota),
  or 429 (rate-limited) as operational success.
- Raw content previews are strictly clamped to [MIN_RAW_CONTENT_CHARS,
  MAX_RAW_CONTENT_CHARS] and sanitized with the same untrusted-content policy
  as snippets.
- Captures provider-reported token usage when present in the JSON response
  and records request_count independently of result count.
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

logger = logging.getLogger("peldrun.search.jina")


class JinaSearchProvider(BaseSearchProvider):
    """
    Markdown-first web search provider powered by s.jina.ai.
    Default estimated cost: $0.002 per authenticated request.
    """

    provider_name: str = "Jina"
    cost_per_query: float = 0.002
    default_cost_basis: CostBasis = CostBasis.CONFIGURED_ESTIMATE
    hard_max_results: int = 20

    _JINA_NUM_CAP: int = 20

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        base = str(self.config.get("base_url", "https://s.jina.ai/")).strip().rstrip("/")
        self.base_search_url: str = (base or "https://s.jina.ai") + "/"

        raw_timeout = self.config.get("timeout_seconds", 15.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 15.0

        raw_cap = self.config.get("max_results_cap", 10)
        try:
            self.max_results_cap: int = max(1, min(int(raw_cap), self._JINA_NUM_CAP))
        except (TypeError, ValueError):
            self.max_results_cap = 10

        self.allow_no_key: bool = bool(self.config.get("allow_no_key", False))
        self.include_raw_content: bool = bool(
            self.config.get("include_raw_content", False)
        )
        self.no_cache: bool = bool(self.config.get("no_cache", False))

        raw_preview_chars = self.config.get("raw_content_preview_chars", 1200)
        try:
            parsed_preview = int(raw_preview_chars)
        except (TypeError, ValueError):
            parsed_preview = 1200
        self.raw_content_preview_chars: int = max(
            MIN_RAW_CONTENT_CHARS,
            min(parsed_preview, MAX_RAW_CONTENT_CHARS),
        )

    def _build_headers(self, fast_serp_only: Optional[bool] = None) -> Dict[str, str]:
        """
        Build Jina Search request headers.
        Uses `X-Respond-With: no-content` by default so s.jina.ai returns fast
        search snippets (1-2s) instead of scraping full pages for every result.
        """
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        use_fast_mode = (
            (not self.include_raw_content)
            if fast_serp_only is None
            else bool(fast_serp_only)
        )
        if use_fast_mode:
            headers["X-Respond-With"] = "no-content"

        if self.no_cache:
            headers["X-No-Cache"] = "true"

        return headers

    @staticmethod
    def _extract_usage_tokens(payload: Dict[str, Any]) -> Optional[float]:
        """Extract provider-reported token usage from Jina JSON response if present."""
        usage = payload.get("usage")
        if isinstance(usage, dict) and "tokens" in usage:
            try:
                return float(usage["tokens"])
            except (TypeError, ValueError):
                pass

        meta = payload.get("meta")
        if isinstance(meta, dict):
            meta_usage = meta.get("usage")
            if isinstance(meta_usage, dict) and "tokens" in meta_usage:
                try:
                    return float(meta_usage["tokens"])
                except (TypeError, ValueError):
                    pass
        return None

    async def _execute_request(
        self,
        clean_q: str,
        limit: int,
        lang: str = "en",
        country: str = "us",
        timeout_override: Optional[float] = None,
        fast_serp_only: Optional[bool] = None,
    ) -> Tuple[List[SearchResult], Optional[float]]:
        """
        Execute a POST search request against s.jina.ai and parse results.
        Raises typed SearchProviderError subclasses on HTTP or schema failures.
        """
        api_num = max(1, min(int(limit), self._JINA_NUM_CAP))
        req_body: Dict[str, Any] = {
            "q": clean_q,
            "num": api_num,
        }
        if country and len(country.strip()) >= 2:
            req_body["gl"] = country.strip().lower()[:2]
        if lang and len(lang.strip()) >= 2:
            req_body["hl"] = lang.strip().lower()[:2]

        effective_timeout = timeout_override or self.timeout_seconds
        try:
            async with httpx.AsyncClient(
                timeout=effective_timeout,
                headers=self._build_headers(fast_serp_only=fast_serp_only),
                follow_redirects=True,
            ) as client:
                resp = await client.post(self.base_search_url, json=req_body)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Jina Search request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Jina Search network error: {exc}",
                provider=self.provider_name,
            ) from exc
        except UnicodeEncodeError as exc:
            # httpx/httpcore/h11 can raise UnicodeEncodeError when the
            # query, api_key, or any header value contains non-ASCII
            # characters that HTTP/1.1 transport tries to encode as
            # ASCII, or when a response body contains non-ASCII and an
            # internal stream write fails. Convert to a classified
            # UpstreamError so factory-level failover can handle it
            # instead of crashing the search.
            raise UpstreamError(
                f"Jina Search failed to encode HTTP data: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code == 400:
            raise ConfigurationError(
                f"Jina Search rejected the request parameters (HTTP 400): "
                f"{resp.text[:240]}",
                provider=self.provider_name,
                status_code=400,
            )
        if resp.status_code == 401:
            raise AuthenticationError(
                "Jina Search rejected the Bearer API key (HTTP 401).",
                provider=self.provider_name,
                status_code=401,
            )
        if resp.status_code == 402:
            raise QuotaExceededError(
                "Jina Search payment required or token balance exhausted (HTTP 402).",
                provider=self.provider_name,
                status_code=402,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "Jina Search access forbidden (HTTP 403).",
                provider=self.provider_name,
                status_code=403,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "Jina Search rate limit exceeded (HTTP 429). Configure a valid API key or wait before retrying.",
                provider=self.provider_name,
                status_code=429,
            )
        if resp.status_code >= 500:
            raise UpstreamError(
                f"Jina Search upstream error (HTTP {resp.status_code}): {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise UpstreamError(
                f"Jina Search returned HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            body_json = resp.json()
        except Exception as exc:
            raise InvalidResponseError(
                f"Jina Search returned non-JSON response: {exc}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(body_json, dict):
            raise InvalidResponseError(
                "Jina Search JSON response root must be an object.",
                provider=self.provider_name,
            )

        data_items = body_json.get("data")
        if data_items is None:
            data_items = []
        if not isinstance(data_items, list):
            raise InvalidResponseError(
                "Jina Search response 'data' field must be a list.",
                provider=self.provider_name,
            )

        usage_tokens = self._extract_usage_tokens(body_json)
        results: List[SearchResult] = []

        for item in data_items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            title = item.get("title") or ""
            content = item.get("content") or ""
            description = item.get("description") or ""

            snippet_source = description if description else content[:400]
            raw_preview: Optional[str] = None
            if content:
                raw_preview = sanitize_untrusted_text(
                    content,
                    max_chars=self.raw_content_preview_chars,
                )

            parsed_item = self.make_result(
                title=title,
                url=url,
                snippet=snippet_source,
                raw_content=raw_preview,
            )
            if parsed_item is not None:
                results.append(parsed_item)
            if len(results) >= limit:
                break

        return results, usage_tokens

    # ------------------------------------------------------------------
    # Diagnostics & Health Check
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        check_type: DiagnosticCheckType = DiagnosticCheckType.CREDENTIALS,
    ) -> ProviderDiagnosticResult:
        """
        Validate Jina Search configuration and credentials.
        Distinguishes network reachability from HTTP 401/402/429 failures.
        """
        start_time = time.perf_counter()

        if not self.api_key and not self.allow_no_key:
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
                    "Jina Search API key is not configured (and allow_no_key is false)."
                ),
            )

        try:
            _, usage_tokens = await self._execute_request(
                clean_q="jina",
                limit=1,
                timeout_override=min(self.timeout_seconds, 6.0),
                fast_serp_only=True,
            )
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            charged = self.cost_per_query if (self.api_key or self.cost_overridden) else 0.0
            basis = (
                CostBasis.CONFIGURED_ESTIMATE
                if (self.api_key or self.cost_overridden)
                else CostBasis.ASSUMED_ZERO_LOCAL
            )
            return ProviderDiagnosticResult(
                ok=True,
                provider=self.provider_name,
                check_type=check_type,
                status=ProviderStatus.SUCCESS,
                reachable=True,
                authenticated=True if self.api_key else None,
                quota_available=True,
                search_operational=True,
                consumes_quota=bool(self.api_key),
                latency_ms=elapsed,
                request_count=1,
                usage_units=usage_tokens,
                estimated_cost_usd=charged,
                cost_basis=basis,
                message="Jina Search endpoint and credentials verified (HTTP 200).",
            )
        except SearchProviderError as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            reachable = exc.status in (
                ProviderStatus.AUTHENTICATION_ERROR,
                ProviderStatus.PERMISSION_ERROR,
                ProviderStatus.QUOTA_EXHAUSTED,
                ProviderStatus.RATE_LIMITED,
                ProviderStatus.CONFIGURATION_ERROR,
                ProviderStatus.INVALID_RESPONSE,
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
                        if exc.status in (ProviderStatus.QUOTA_EXHAUSTED,)
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
                message=f"Jina Search connection failed: {exc}",
            )

    async def ping(self) -> bool:
        """
        Return True ONLY when Jina Search responds with HTTP 200 and valid JSON.
        HTTP 401, 402, and 429 return False.
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

        if not self.api_key and not self.allow_no_key:
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
                error_message=(
                    "Jina Search API key is not configured and anonymous access "
                    "is disabled (allow_no_key=false)."
                ),
            )

        limit = self.clamp_results_limit(max_results, self.max_results_cap)

        try:
            results, usage_tokens = await self._execute_request(
                clean_q=clean_q,
                limit=limit,
                lang=lang,
                country=country,
            )
        except SearchProviderError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"Jina Search error [{exc.error_code}]: {exc.message}")
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
            exc_name = type(exc).__name__
            exc_msg = str(exc) or "(no message)"
            logger.warning(f"Jina Search unexpected error [{exc_name}]: {exc_msg}")
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
                error_message=f"Jina Search [{exc_name}]: {exc_msg}",
            )

        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        has_results = bool(results)

        is_billable = bool(self.api_key or self.cost_overridden)
        charged_cost = self.cost_per_query if is_billable else 0.0
        cost_basis = (
            CostBasis.CONFIGURED_ESTIMATE
            if is_billable
            else CostBasis.ASSUMED_ZERO_LOCAL
        )

        return SearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=results,
            status=ProviderStatus.SUCCESS if has_results else ProviderStatus.EMPTY,
            latency_ms=elapsed_ms,
            request_count=1,
            usage_units=usage_tokens if usage_tokens is not None else 1.0,
            estimated_cost_usd=charged_cost,
            cost_basis=cost_basis,
            is_success=has_results,
            error_code=None if has_results else "empty_results",
            error_message=None
            if has_results
            else (
                "Jina Search completed the request (HTTP 200) but returned zero "
                "results for this query."
            ),
        )


__all__ = ["JinaSearchProvider"]
