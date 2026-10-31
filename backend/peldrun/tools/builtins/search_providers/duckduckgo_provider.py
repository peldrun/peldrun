"""
backend/peldrun/tools/builtins/search_providers/duckduckgo_provider.py

Resilient DuckDuckGo Search Provider.
- Passes configured `safesearch` ('on' | 'moderate' | 'off') and `region`
  directly to the `ddgs` library `text()` call.
- Suppresses legacy `duckduckgo_search` rename RuntimeWarnings inside worker
  threads and explicitly cleans up underlying client resources to avoid
  Windows Proactor ResourceWarnings.
- Classifies HTTP 202/403/429 responses as RateLimitError (ProviderStatus.RATE_LIMITED).
"""

from __future__ import annotations

import asyncio
import gc
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import warnings

from bs4 import BeautifulSoup
import httpx

from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    CostBasis,
    DiagnosticCheckType,
    ImageSearchResponse,
    ImageSearchResult,
    ProviderDiagnosticResult,
    ProviderStatus,
    RateLimitError,
    SearchProviderError,
    SearchResponse,
    SearchResult,
    TimeoutError,
    UpstreamError,
)

logger = logging.getLogger("peldrun.search.duckduckgo")

warnings.filterwarnings(
    "ignore",
    message=r".*duckduckgo_search.*has been renamed to `ddgs`.*",
    category=RuntimeWarning,
)

try:
    from ddgs import DDGS  # type: ignore[import-not-found]
    _DDGS_BACKEND = "ddgs"
except ImportError:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            from duckduckgo_search import DDGS  # type: ignore[import-not-found,no-redef]
        _DDGS_BACKEND = "duckduckgo_search"
    except ImportError:
        DDGS = None  # type: ignore[assignment]
        _DDGS_BACKEND = "none"


class DuckDuckGoSearchProvider(BaseSearchProvider):
    """
    Zero-credential search provider using the `ddgs` package with an optional
    HTML endpoint fallback.
    """

    provider_name: str = "DuckDuckGo"
    cost_per_query: float = 0.0
    default_cost_basis: CostBasis = CostBasis.ASSUMED_ZERO_LOCAL
    hard_max_results: int = 20

    _VALID_SAFESEARCH: Tuple[str, ...] = ("on", "moderate", "off")
    _HTML_KP_BY_SAFESEARCH: Dict[str, str] = {
        "on": "1",
        "moderate": "-1",
        "off": "-2",
    }

    _last_request_ts: float = 0.0
    _throttle_lock: Optional[asyncio.Lock] = None

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)

        self.endpoint: str = (
            str(self.config.get("endpoint", "https://html.duckduckgo.com/html/")).strip()
            or "https://html.duckduckgo.com/html/"
        )

        raw_timeout = self.config.get("timeout_seconds", 10.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 10.0

        raw_throttle = self.config.get("throttle_seconds", 2.0)
        try:
            self.throttle_seconds: float = max(0.0, min(float(raw_throttle), 10.0))
        except (TypeError, ValueError):
            self.throttle_seconds = 2.0

        raw_cap = self.config.get("max_results_cap", self.hard_max_results)
        try:
            self.max_results_cap: int = max(
                1, min(int(raw_cap), self.hard_max_results)
            )
        except (TypeError, ValueError):
            self.max_results_cap = self.hard_max_results

        self.safesearch: str = self._normalize_safesearch(
            self.config.get("safesearch", "moderate")
        )

        raw_backend = str(self.config.get("backend", "auto")).strip().lower()
        self.backend: str = (
            raw_backend if raw_backend in ("auto", "ddgs", "html") else "auto"
        )
        self.enable_html_fallback: bool = bool(
            self.config.get("enable_html_fallback", True)
        )

        if DuckDuckGoSearchProvider._throttle_lock is None:
            try:
                DuckDuckGoSearchProvider._throttle_lock = asyncio.Lock()
            except Exception:
                DuckDuckGoSearchProvider._throttle_lock = None

    @classmethod
    def _normalize_safesearch(cls, raw_value: Any) -> str:
        if isinstance(raw_value, int):
            if raw_value >= 2:
                return "on"
            if raw_value == 1:
                return "moderate"
            return "off"
        norm = str(raw_value or "moderate").strip().lower()
        if norm in ("strict", "true", "1", "on"):
            return "on"
        if norm in ("off", "false", "0", "none"):
            return "off"
        return "moderate"

    async def _throttle(self) -> None:
        """Enforce minimum interval between DDG requests to reduce 429/202 blocks."""
        if self.throttle_seconds <= 0:
            return
        lock = DuckDuckGoSearchProvider._throttle_lock
        if lock is None:
            try:
                lock = asyncio.Lock()
                DuckDuckGoSearchProvider._throttle_lock = lock
            except Exception:
                return
        async with lock:
            now = time.monotonic()
            elapsed = now - DuckDuckGoSearchProvider._last_request_ts
            wait = self.throttle_seconds - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            DuckDuckGoSearchProvider._last_request_ts = time.monotonic()

    @staticmethod
    def _build_region(country: str, lang: str) -> str:
        """Build the DuckDuckGo region code (e.g. 'us-en', 'eg-ar', or 'wt-wt')."""
        c = (country or "").strip().lower()
        l = (lang or "").strip().lower()
        if c == "wt" or l == "wt":
            return "wt-wt"
        c = c or "us"
        l = l or "en"
        return f"{c}-{l}"

    # ------------------------------------------------------------------
    # Reachability & Diagnostics
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        check_type: DiagnosticCheckType = DiagnosticCheckType.REACHABILITY,
    ) -> ProviderDiagnosticResult:
        start_time = time.perf_counter()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        }
        probe_url = (
            self.endpoint
            if self.backend == "html"
            else "https://duckduckgo.com/"
        )
        try:
            async with httpx.AsyncClient(
                timeout=min(self.timeout_seconds, 5.0),
                headers=headers,
                follow_redirects=True,
            ) as client:
                res = await client.get(probe_url)
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            ok = res.status_code == 200
            return ProviderDiagnosticResult(
                ok=ok,
                provider=self.provider_name,
                check_type=DiagnosticCheckType.REACHABILITY,
                status=ProviderStatus.SUCCESS if ok else ProviderStatus.UPSTREAM_ERROR,
                reachable=ok,
                authenticated=None,
                quota_available=True if ok else None,
                search_operational=None,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                message=(
                    "DuckDuckGo endpoint reachable (transport check only; run a test query to verify search parsing)."
                    if ok
                    else f"DuckDuckGo endpoint returned HTTP {res.status_code}."
                ),
            )
        except Exception as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ProviderDiagnosticResult(
                ok=False,
                provider=self.provider_name,
                check_type=DiagnosticCheckType.REACHABILITY,
                status=ProviderStatus.UPSTREAM_ERROR,
                reachable=False,
                consumes_quota=False,
                latency_ms=elapsed,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                message=f"DuckDuckGo reachability check failed: {exc}",
            )

    async def ping(self) -> bool:
        """Lightweight transport reachability check."""
        diag = await self.diagnose(check_type=DiagnosticCheckType.REACHABILITY)
        return diag.ok

    # ------------------------------------------------------------------
    # Method 1: Native library (ddgs / duckduckgo_search)
    # ------------------------------------------------------------------
    async def _search_via_library(
        self,
        query: str,
        max_results: int,
        region: str,
    ) -> List[SearchResult]:
        """
        Execute DDGS search in a worker thread.
        Passes `region`, `safesearch`, and `max_results` to `ddgs.text()`.
        Suppresses deprecation/rename RuntimeWarnings inside the worker thread
        and forces cleanup of synchronous HTTP transports before returning.
        """
        if DDGS is None:
            raise UpstreamError(
                "No DDGS client library is installed. Run: pip install -U ddgs",
                provider=self.provider_name,
            )

        # Use a shorter library-side timeout so that when the `ddgs`
        # library stalls (common under Windows + rate limits), the
        # HTML fallback fires sooner. The outer ``self.timeout_seconds``
        # still bounds the total provider call.
        socket_timeout = max(1, min(int(round(self.timeout_seconds)), 6))

        def _fetch() -> List[Dict[str, Any]]:
            call_kwargs: Dict[str, Any] = {
                "region": region,
                "safesearch": self.safesearch,
                "max_results": max_results,
            }
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                warnings.simplefilter("ignore", ResourceWarning)
                try:
                    with DDGS(timeout=socket_timeout) as ddgs:  # type: ignore[misc]
                        return list(ddgs.text(query, **call_kwargs))
                except TypeError:
                    ddgs_instance = DDGS()  # type: ignore[misc]
                    try:
                        return list(ddgs_instance.text(query, **call_kwargs))
                    finally:
                        close_fn = getattr(ddgs_instance, "__exit__", None)
                        if callable(close_fn):
                            try:
                                close_fn(None, None, None)
                            except Exception:
                                pass
                finally:
                    gc.collect()

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch),
                timeout=self.timeout_seconds + 1.0,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"DDGS backend timed out after {self.timeout_seconds:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except Exception as exc:
            msg_lower = str(exc).lower()
            if "ratelimit" in msg_lower or "202" in msg_lower or "429" in msg_lower:
                raise RateLimitError(
                    f"DuckDuckGo rate-limited the request ({_DDGS_BACKEND}): {exc}",
                    provider=self.provider_name,
                ) from exc
            raise UpstreamError(
                f"DDGS library error ({_DDGS_BACKEND}): {exc}",
                provider=self.provider_name,
            ) from exc

        parsed: List[SearchResult] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            url = item.get("href") or item.get("link") or item.get("url") or ""
            title = item.get("title") or ""
            snippet = (
                item.get("body")
                or item.get("snippet")
                or item.get("description")
                or ""
            )
            if url and title:
                res_obj = self.make_result(
                    title=title,
                    url=url,
                    snippet=snippet,
                )
                if res_obj is not None:
                    parsed.append(res_obj)
            if len(parsed) >= max_results:
                break
        return parsed

    # ------------------------------------------------------------------
    # Method 2: HTML Scraper Fallback
    # ------------------------------------------------------------------
    def _get_html_endpoints(self) -> List[str]:
        """Return ordered HTML endpoints starting with user-configured endpoint."""
        default_endpoints = [
            "https://html.duckduckgo.com/html/",
            "https://lite.duckduckgo.com/lite/",
        ]
        ordered: List[str] = []
        if self.endpoint:
            ordered.append(self.endpoint)
        for ep in default_endpoints:
            if ep not in ordered:
                ordered.append(ep)
        return ordered

    async def _search_via_html(
        self,
        query: str,
        max_results: int,
        region: str,
        lang: str,
    ) -> Tuple[List[SearchResult], int]:
        """
        Fallback HTML form submission honoring configured `endpoint`,
        `region` ('kl'), and `safesearch` ('kp').
        Returns (results, requests_made).
        """
        accept_lang = f"{lang},en-US;q=0.9,en;q=0.8" if lang else "en-US,en;q=0.9"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": accept_lang,
            "Origin": "https://html.duckduckgo.com",
            "Referer": "https://html.duckduckgo.com/",
        }

        kp_value = self._HTML_KP_BY_SAFESEARCH.get(self.safesearch, "-1")
        form_data = {
            "q": query,
            "kl": region or "wt-wt",
            "kp": kp_value,
        }

        endpoints = self._get_html_endpoints()
        requests_made = 0
        last_error: Optional[str] = None
        was_rate_limited = False

        for ep in endpoints:
            requests_made += 1
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    resp = await client.post(ep, data=form_data)
            except httpx.TimeoutException:
                last_error = f"{ep} timed out"
                continue
            except httpx.HTTPError as exc:
                last_error = f"{ep} network error: {exc}"
                continue

            if resp.status_code in (202, 403, 429):
                was_rate_limited = True
                last_error = f"{ep} rate-limited or bot-challenged (HTTP {resp.status_code})"
                continue
            if resp.status_code >= 400:
                last_error = f"{ep} returned HTTP {resp.status_code}"
                continue

            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")

            results: List[SearchResult] = []
            anchors = soup.select("a.result__a")
            snippets_a = soup.select("a.result__snippet, .result__snippet")

            if not anchors:
                anchors = soup.select("a.result-link")
            if not snippets_a:
                snippets_a = soup.select("td.result-snippet")
            if not anchors:
                anchors = soup.select("a[href*='uddg=']")

            for idx, a_tag in enumerate(anchors):
                raw_href = str(a_tag.get("href", "") or "")
                target_url = raw_href

                if "uddg=" in raw_href:
                    match = re.search(r"uddg=([^&]+)", raw_href)
                    if match:
                        target_url = urllib.parse.unquote(match.group(1))

                if not target_url or target_url.startswith("/"):
                    continue
                if "duckduckgo.com" in target_url and "uddg=" not in raw_href:
                    continue

                title = a_tag.get_text(strip=True)
                snippet_text = ""
                if idx < len(snippets_a):
                    snippet_text = snippets_a[idx].get_text(strip=True)

                if target_url and title:
                    item = self.make_result(
                        title=title,
                        url=target_url,
                        snippet=snippet_text,
                    )
                    if item is not None:
                        results.append(item)

                if len(results) >= max_results:
                    break

            if results:
                return results, requests_made

        if last_error:
            if was_rate_limited:
                raise RateLimitError(
                    f"DuckDuckGo HTML fallback rate-limited: {last_error}. "
                    f"Upgrade ddgs (`pip install -U ddgs`) or configure an API-backed provider.",
                    provider=self.provider_name,
                )
            raise UpstreamError(
                f"DuckDuckGo HTML fallback failed: {last_error}",
                provider=self.provider_name,
            )
        return [], requests_made

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

        limit = self.clamp_results_limit(max_results, self.max_results_cap)
        region = self._build_region(country, lang)

        try:
            await self._throttle()
        except Exception:
            pass

        collected: List[SearchResult] = []
        errors: List[str] = []
        last_status: ProviderStatus = ProviderStatus.EMPTY
        request_count = 0

        # 1. Primary native library attempt
        if self.backend in ("auto", "ddgs"):
            request_count += 1
            try:
                collected = await self._search_via_library(
                    query=clean_q,
                    max_results=limit,
                    region=region,
                )
            except SearchProviderError as exc:
                last_status = exc.status
                errors.append(f"library({_DDGS_BACKEND}): {exc.message}")
                logger.warning(f"DDGS library search failed: {exc.message}")
            except Exception as exc:
                last_status = ProviderStatus.UPSTREAM_ERROR
                msg = str(exc) or exc.__class__.__name__
                errors.append(f"library({_DDGS_BACKEND}): {msg}")
                logger.warning(f"DDGS library search failed: {msg}")

        # 2. Optional HTML fallback when library returned no results or is not used
        if not collected and (
            self.backend == "html"
            or (self.enable_html_fallback and self.backend == "auto")
        ):
            try:
                html_results, html_reqs = await self._search_via_html(
                    query=clean_q,
                    max_results=limit,
                    region=region,
                    lang=lang or "en",
                )
                request_count += html_reqs
                collected = html_results
            except SearchProviderError as exc:
                request_count += 1
                last_status = exc.status
                errors.append(f"html-fallback: {exc.message}")
                logger.warning(f"DDG HTML fallback failed: {exc.message}")
            except Exception as exc:
                request_count += 1
                if last_status == ProviderStatus.EMPTY:
                    last_status = ProviderStatus.UPSTREAM_ERROR
                msg = str(exc) or exc.__class__.__name__
                errors.append(f"html-fallback: {msg}")
                logger.warning(f"DDG HTML fallback failed: {msg}")

        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        effective_cost = self.cost_per_query if self.cost_overridden else 0.0
        cost_basis = (
            CostBasis.CONFIGURED_ESTIMATE
            if self.cost_overridden
            else CostBasis.ASSUMED_ZERO_LOCAL
        )

        if collected:
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=collected,
                status=ProviderStatus.SUCCESS,
                latency_ms=elapsed_ms,
                request_count=max(1, request_count),
                usage_units=float(max(1, request_count)),
                estimated_cost_usd=effective_cost,
                cost_basis=cost_basis,
                is_success=True,
                error_code=None,
                error_message=None,
            )

        if errors:
            diagnostic = " | ".join(errors)
            final_status = (
                last_status
                if last_status != ProviderStatus.EMPTY
                else ProviderStatus.UPSTREAM_ERROR
            )
            return SearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=final_status,
                latency_ms=elapsed_ms,
                request_count=max(1, request_count),
                estimated_cost_usd=0.0,
                cost_basis=cost_basis,
                is_success=False,
                error_code=final_status.value,
                error_message=f"DuckDuckGo search failed. Diagnostics: {diagnostic}",
            )

        return SearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=[],
            status=ProviderStatus.EMPTY,
            latency_ms=elapsed_ms,
            request_count=max(1, request_count),
            usage_units=float(max(1, request_count)),
            estimated_cost_usd=effective_cost,
            cost_basis=cost_basis,
            is_success=has_results if "has_results" in locals() else False,
            error_code="empty_results",
            error_message="DuckDuckGo completed the search but returned zero matching results.",
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
        Execute a DuckDuckGo image search via the ddgs library.
        Applies the same throttling and safe-search configuration used by
        text search, and records request_count/cost independently of the
        number of images returned.
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

        if DDGS is None:
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="missing_dependency",
                error_message=(
                    "No DDGS client library is installed. Run: pip install -U ddgs"
                ),
            )

        limit = max(1, min(int(max_results), self.max_results_cap, 20))
        region = self._build_region(country, lang)

        try:
            await self._throttle()
        except Exception:
            pass

        def _fetch() -> List[Dict[str, Any]]:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                warnings.simplefilter("ignore", ResourceWarning)
                try:
                    with DDGS(timeout=max(1, int(self.timeout_seconds))) as ddgs:  # type: ignore[misc]
                        return list(
                            ddgs.images(
                                clean_q,
                                region=region,
                                safesearch=self.safesearch,
                                max_results=limit,
                            )
                        )
                except TypeError:
                    ddgs_instance = DDGS()  # type: ignore[misc]
                    try:
                        return list(
                            ddgs_instance.images(clean_q, max_results=limit)
                        )
                    finally:
                        close_fn = getattr(ddgs_instance, "__exit__", None)
                        if callable(close_fn):
                            try:
                                close_fn(None, None, None)
                            except Exception:
                                pass
                finally:
                    gc.collect()

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch),
                timeout=self.timeout_seconds + 2.0,
            )
        except asyncio.TimeoutError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=ProviderStatus.TIMEOUT,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                is_success=False,
                error_code="timeout",
                error_message=(
                    f"DDGS image search timed out after "
                    f"{self.timeout_seconds:.1f}s."
                ),
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            msg_lower = str(exc).lower()
            is_rate_limited = (
                "ratelimit" in msg_lower
                or "202" in msg_lower
                or "429" in msg_lower
            )
            status = (
                ProviderStatus.RATE_LIMITED
                if is_rate_limited
                else ProviderStatus.UPSTREAM_ERROR
            )
            logger.warning(
                f"DDGS image search failed ({_DDGS_BACKEND}): {exc}"
            )
            return ImageSearchResponse(
                query=clean_q,
                provider=self.provider_name,
                results=[],
                status=status,
                latency_ms=elapsed_ms,
                request_count=1,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                is_success=False,
                error_code=status.value,
                error_message=(
                    f"DDGS image search failed ({_DDGS_BACKEND}): {exc}"
                ),
            )

        results: List[ImageSearchResult] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            parsed = self.make_image_result(
                title=item.get("title") or "",
                image_url=item.get("image") or "",
                thumbnail_url=item.get("thumbnail") or "",
                source_url=item.get("url") or "",
                width=item.get("width"),
                height=item.get("height"),
            )
            if parsed is not None:
                results.append(parsed)
            if len(results) >= limit:
                break

        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        has_results = bool(results)
        effective_cost = self.cost_per_query if self.cost_overridden else 0.0
        cost_basis = (
            CostBasis.CONFIGURED_ESTIMATE
            if self.cost_overridden
            else CostBasis.ASSUMED_ZERO_LOCAL
        )

        return ImageSearchResponse(
            query=clean_q,
            provider=self.provider_name,
            results=results,
            status=ProviderStatus.SUCCESS if has_results else ProviderStatus.EMPTY,
            latency_ms=elapsed_ms,
            request_count=1,
            estimated_cost_usd=effective_cost,
            cost_basis=cost_basis,
            is_success=has_results,
            error_code=None if has_results else "empty_results",
            error_message=None
            if has_results
            else (
                "DuckDuckGo image search completed the request but returned "
                "zero matching images."
            ),
            effective_max_results=limit,
        )


__all__ = ["DuckDuckGoSearchProvider"]
