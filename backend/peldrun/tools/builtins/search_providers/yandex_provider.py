"""
backend/peldrun/tools/builtins/search_providers/yandex_provider.py

Yandex Search API Provider Integration.
Supports two explicit, validated operational modes:
  1. cloud_v2    : Yandex Cloud Search API v2 synchronous POST endpoint
                   (https://searchapi.api.cloud.yandex.net/v2/web/search)
                   with nested query payload and Base64-encoded rawData XML.
  2. classic_xml : Legacy Yandex XML GET interface
                   (https://yandex.com/search/xml).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

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

logger = logging.getLogger("peldrun.search.yandex")


class YandexSearchProvider(BaseSearchProvider):
    """
    Yandex Search API provider supporting Cloud Search API v2 and Classic XML.
    Default estimated cost: $0.003 per billable request.
    """

    provider_name: str = "Yandex"
    cost_per_query: float = 0.003
    default_cost_basis: CostBasis = CostBasis.CONFIGURED_ESTIMATE
    hard_max_results: int = 15

    OFFICIAL_CLOUD_V2_ENDPOINT: str = (
        "https://searchapi.api.cloud.yandex.net/v2/web/search"
    )
    LEGACY_BROKEN_CLOUD_ENDPOINT: str = (
        "https://searchapi.yandex.net/v2/web/search"
    )
    DEFAULT_CLASSIC_XML_ENDPOINT: str = "https://yandex.com/search/xml"

    _VALID_MODES: Tuple[str, ...] = ("cloud_v2", "classic_xml")

    _NAMESPACE_CANDIDATES: Tuple[str, ...] = (
        "http://xml.yandex.ru/schema/5/",
        "http://xml.yandex.ru/schema/4/",
        "http://xml.yandex.ru/schema/3/",
    )

    _SEARCH_TYPE_BY_COUNTRY: Dict[str, str] = {
        "ru": "SEARCH_TYPE_RU",
        "by": "SEARCH_TYPE_RU",
        "kz": "SEARCH_TYPE_KK",
        "tr": "SEARCH_TYPE_TR",
        "com": "SEARCH_TYPE_COM",
        "us": "SEARCH_TYPE_COM",
        "gb": "SEARCH_TYPE_COM",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.folder_id: str = str(self.config.get("folder_id", "")).strip()
        self.region: str = str(self.config.get("region", "")).strip()

        raw_timeout = self.config.get("timeout_seconds", 12.0)
        try:
            self.timeout_seconds: float = max(1.0, min(float(raw_timeout), 60.0))
        except (TypeError, ValueError):
            self.timeout_seconds = 12.0

        raw_cap = self.config.get("max_results_cap", self.hard_max_results)
        try:
            self.max_results_cap: int = max(
                1, min(int(raw_cap), self.hard_max_results)
            )
        except (TypeError, ValueError):
            self.max_results_cap = self.hard_max_results

        raw_endpoint = str(self.config.get("endpoint", "")).strip()
        raw_mode = str(self.config.get("mode", "")).strip().lower()

        self.mode: str = self._resolve_mode(raw_mode, raw_endpoint)
        self.endpoint: str = self._resolve_endpoint(self.mode, raw_endpoint)

    def _resolve_mode(self, raw_mode: str, raw_endpoint: str) -> str:
        """
        Determine operational mode explicitly without relying on fragile
        host-substring checks.
        """
        if raw_mode:
            if raw_mode in self._VALID_MODES:
                return raw_mode
            return f"invalid:{raw_mode}"

        ep_lower = raw_endpoint.lower()
        if "search/xml" in ep_lower and not self.folder_id:
            return "classic_xml"
        if self.folder_id or "searchapi" in ep_lower or "/v2/web/search" in ep_lower:
            return "cloud_v2"
        return "cloud_v2"

    def _resolve_endpoint(self, mode: str, raw_endpoint: str) -> str:
        """
        Resolve the target endpoint for the selected mode, automatically
        upgrading the legacy non-official Cloud host if present.
        """
        if mode == "classic_xml":
            if raw_endpoint and "searchapi" not in raw_endpoint.lower():
                return raw_endpoint
            return self.DEFAULT_CLASSIC_XML_ENDPOINT

        # Cloud v2 mode
        if not raw_endpoint or raw_endpoint == self.LEGACY_BROKEN_CLOUD_ENDPOINT:
            return self.OFFICIAL_CLOUD_V2_ENDPOINT
        return raw_endpoint

    def _validate_configuration(self) -> None:
        """Raise ConfigurationError if mode or required credentials are invalid."""
        if self.mode.startswith("invalid:"):
            bad_mode = self.mode.split(":", 1)[1]
            raise ConfigurationError(
                f"Invalid Yandex mode '{bad_mode}'. Supported modes: "
                f"{', '.join(self._VALID_MODES)}.",
                provider=self.provider_name,
            )

        if not self.api_key:
            raise ConfigurationError(
                "Yandex Search API key is not configured.",
                provider=self.provider_name,
            )

        if self.mode == "cloud_v2" and not self.folder_id:
            raise ConfigurationError(
                "Yandex Cloud Search API v2 requires 'folder_id' in addition "
                "to 'api_key' (or switch mode to 'classic_xml').",
                provider=self.provider_name,
            )

    def _resolve_search_type(self, country: str, lang: str) -> str:
        """Map locale hints to a documented Yandex Cloud SearchType enum."""
        configured_type = str(self.config.get("search_type", "")).strip().upper()
        if configured_type.startswith("SEARCH_TYPE_"):
            return configured_type

        c_norm = (country or "").strip().lower()
        l_norm = (lang or "").strip().lower()
        if c_norm in self._SEARCH_TYPE_BY_COUNTRY:
            return self._SEARCH_TYPE_BY_COUNTRY[c_norm]
        if l_norm == "ru":
            return "SEARCH_TYPE_RU"
        if l_norm == "tr":
            return "SEARCH_TYPE_TR"
        return "SEARCH_TYPE_COM"

    def _build_cloud_v2_payload(
        self,
        query_text: str,
        limit: int,
        lang: str = "en",
        country: str = "us",
    ) -> Dict[str, Any]:
        """
        Build request payload adhering to the official Yandex Cloud Search API v2
        SyncSearch specification.
        """
        payload: Dict[str, Any] = {
            "query": {
                "searchType": self._resolve_search_type(country, lang),
                "queryText": query_text,
                "familyMode": "FAMILY_MODE_MODERATE",
                "page": 0,
            },
            "groupSpec": {
                "groupMode": "GROUP_MODE_DEEP",
                "groupsOnPage": max(1, min(int(limit), self.hard_max_results)),
                "docsInGroup": 1,
            },
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
            "userAgent": "PELDRUN-Search/1.0",
        }
        if self.region:
            payload["region"] = self.region
        return payload

    # ------------------------------------------------------------------
    # Diagnostics & Health Check
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        check_type: DiagnosticCheckType = DiagnosticCheckType.CREDENTIALS,
    ) -> ProviderDiagnosticResult:
        start_time = time.perf_counter()
        try:
            self._validate_configuration()
        except ConfigurationError as exc:
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
                message=exc.message,
            )

        try:
            if self.mode == "cloud_v2":
                await self._search_cloud_v2(
                    clean_q="yandex",
                    limit=1,
                    lang="en",
                    country="us",
                    probe_timeout=min(self.timeout_seconds, 6.0),
                )
            else:
                await self._search_xml(
                    clean_q="yandex",
                    limit=1,
                    probe_timeout=min(self.timeout_seconds, 6.0),
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
                estimated_cost_usd=self.cost_per_query,
                cost_basis=self.default_cost_basis,
                message=f"Yandex ({self.mode}) credentials and search response verified.",
            )
        except SearchProviderError as exc:
            elapsed = round((time.perf_counter() - start_time) * 1000.0, 2)
            reachable = exc.status in (
                ProviderStatus.AUTHENTICATION_ERROR,
                ProviderStatus.PERMISSION_ERROR,
                ProviderStatus.RATE_LIMITED,
                ProviderStatus.QUOTA_EXHAUSTED,
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
                    else (True if reachable else None)
                ),
                quota_available=(
                    False
                    if exc.status
                    in (ProviderStatus.RATE_LIMITED, ProviderStatus.QUOTA_EXHAUSTED)
                    else None
                ),
                search_operational=False,
                consumes_quota=reachable,
                latency_ms=elapsed,
                request_count=1 if reachable else 0,
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
                message=f"Yandex connection failed: {exc}",
            )

    async def ping(self) -> bool:
        """
        Validate Yandex configuration and endpoint readiness.
        Never treats HTTP 400/401/403/429 as operational success.
        """
        diag = await self.diagnose(check_type=DiagnosticCheckType.CREDENTIALS)
        return diag.ok

    # ------------------------------------------------------------------
    # XML Parsing Helpers (Shared by Cloud v2 rawData and Classic XML)
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_namespace(root: ET.Element) -> Optional[str]:
        tag = root.tag or ""
        if tag.startswith("{"):
            end = tag.find("}")
            if end > 0:
                return tag[1:end]
        return None

    def _extract_groups_with_namespace(
        self,
        root: ET.Element,
    ) -> Tuple[List[ET.Element], Optional[str]]:
        groups = root.findall(".//group")
        if groups:
            return groups, None

        detected_ns = self._detect_namespace(root)
        if detected_ns:
            groups = root.findall(f".//{{{detected_ns}}}group")
            if groups:
                return groups, detected_ns

        for ns_uri in self._NAMESPACE_CANDIDATES:
            groups = root.findall(f".//{{{ns_uri}}}group")
            if groups:
                return groups, ns_uri

        return [], detected_ns

    @staticmethod
    def _find_child(
        element: ET.Element,
        name: str,
        namespace: Optional[str],
    ) -> Optional[ET.Element]:
        if namespace:
            found = element.find(f"{{{namespace}}}{name}")
            if found is not None:
                return found
        return element.find(name)

    @staticmethod
    def _extract_full_text(element: Optional[ET.Element]) -> str:
        """
        Extract full text including nested <hlword> highlight elements.
        """
        if element is None:
            return ""
        return " ".join(
            part.strip() for part in element.itertext() if part and part.strip()
        ).strip()

    def _check_xml_error_element(
        self,
        root: ET.Element,
        namespace: Optional[str],
    ) -> None:
        """
        Inspect Yandex XML <response><error> element.
        Code 15 in Yandex XML indicates 'no matching documents found' (valid empty
        search), whereas other codes indicate credential/quota/request errors.
        """
        error_elem = root.find(".//error")
        if error_elem is None and namespace:
            error_elem = root.find(f".//{{{namespace}}}error")
        if error_elem is None:
            return

        err_code = str(error_elem.attrib.get("code", "")).strip()
        err_text = self._extract_full_text(error_elem) or "Yandex XML error"

        # Code 15 = no documents found for query -> valid empty result
        if err_code == "15" or "nothing found" in err_text.lower() or "не주" in err_text.lower():
            return

        if err_code in ("32", "33", "55"):
            raise QuotaExceededError(
                f"Yandex API quota/rate limit error (code {err_code}): {err_text}",
                provider=self.provider_name,
            )
        if err_code in ("42", "43", "44"):
            raise AuthenticationError(
                f"Yandex API key/permission error (code {err_code}): {err_text}",
                provider=self.provider_name,
            )

        raise UpstreamError(
            f"Yandex XML returned error (code {err_code or 'unknown'}): {err_text}",
            provider=self.provider_name,
        )

    def _parse_yandex_xml_document(
        self,
        xml_text: str,
        limit: int,
    ) -> List[SearchResult]:
        """Parse Yandex XML payload into validated SearchResult objects."""
        if not xml_text or not xml_text.strip():
            raise InvalidResponseError(
                "Yandex returned an empty XML payload.",
                provider=self.provider_name,
            )

        try:
            root = ET.fromstring(xml_text.strip())
        except ET.ParseError as exc:
            raise InvalidResponseError(
                f"Yandex XML response could not be parsed: {exc}. "
                f"Preview: {xml_text[:180]}",
                provider=self.provider_name,
            ) from exc

        groups, ns_used = self._extract_groups_with_namespace(root)
        if not groups:
            self._check_xml_error_element(root, ns_used)
            return []

        results: List[SearchResult] = []
        for group in groups:
            doc = self._find_child(group, "doc", ns_used)
            if doc is None:
                continue

            url_elem = self._find_child(doc, "url", ns_used)
            title_elem = self._find_child(doc, "title", ns_used)
            headline_elem = self._find_child(doc, "headline", ns_used)

            url_text = self._extract_full_text(url_elem)
            title_text = self._extract_full_text(title_elem)
            snippet_text = self._extract_full_text(headline_elem)

            if not snippet_text:
                passages_elem = self._find_child(doc, "passages", ns_used)
                if passages_elem is not None:
                    passage_parts: List[str] = []
                    for child in list(passages_elem):
                        p_text = self._extract_full_text(child)
                        if p_text:
                            passage_parts.append(p_text)
                    snippet_text = " ... ".join(passage_parts)

            if url_text:
                item = self.make_result(
                    title=title_text or url_text,
                    url=url_text,
                    snippet=snippet_text,
                )
                if item is not None:
                    results.append(item)

            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # Mode A: Cloud Search API v2 (POST + Base64 rawData)
    # ------------------------------------------------------------------
    async def _search_cloud_v2(
        self,
        clean_q: str,
        limit: int,
        lang: str = "en",
        country: str = "us",
        probe_timeout: Optional[float] = None,
    ) -> List[SearchResult]:
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = self._build_cloud_v2_payload(
            query_text=clean_q,
            limit=limit,
            lang=lang,
            country=country,
        )

        effective_timeout = probe_timeout or self.timeout_seconds
        try:
            async with httpx.AsyncClient(
                timeout=effective_timeout,
                headers=headers,
            ) as client:
                resp = await client.post(self.endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Yandex Cloud v2 request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Yandex Cloud v2 network error: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code == 400:
            raise ConfigurationError(
                f"Yandex Cloud v2 rejected the request parameters (HTTP 400): "
                f"{resp.text[:240]}",
                provider=self.provider_name,
                status_code=400,
            )
        if resp.status_code == 401:
            raise AuthenticationError(
                "Yandex Cloud v2 rejected the API key (HTTP 401).",
                provider=self.provider_name,
                status_code=401,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "Yandex Cloud v2 access forbidden (HTTP 403). "
                "Verify the folder_id and service account roles (search-api.webSearch.user).",
                provider=self.provider_name,
                status_code=403,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "Yandex Cloud v2 rate limit exceeded (HTTP 429).",
                provider=self.provider_name,
                status_code=429,
            )
        if resp.status_code >= 500:
            raise UpstreamError(
                f"Yandex Cloud v2 server error (HTTP {resp.status_code}): "
                f"{resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            raise UpstreamError(
                f"Yandex Cloud v2 returned HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise InvalidResponseError(
                f"Yandex Cloud v2 returned non-JSON response: {exc}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(data, dict):
            raise InvalidResponseError(
                "Yandex Cloud v2 response root must be a JSON object.",
                provider=self.provider_name,
            )

        # Primary official contract: Base64-encoded rawData
        raw_data_b64 = data.get("rawData")
        if isinstance(raw_data_b64, str) and raw_data_b64.strip():
            try:
                decoded_bytes = base64.b64decode(raw_data_b64, validate=False)
                decoded_xml = decoded_bytes.decode("utf-8", errors="replace")
            except Exception as exc:
                raise InvalidResponseError(
                    f"Failed to decode Yandex Cloud v2 Base64 rawData: {exc}",
                    provider=self.provider_name,
                ) from exc
            return self._parse_yandex_xml_document(decoded_xml, limit=limit)

        # Compatibility fallback if a gateway/proxy already unwrapped docs
        response_obj = data.get("response")
        if isinstance(response_obj, dict) and isinstance(response_obj.get("docs"), list):
            results: List[SearchResult] = []
            for doc in response_obj["docs"]:
                if not isinstance(doc, dict):
                    continue
                item = self.make_result(
                    title=doc.get("title") or "",
                    url=doc.get("url") or "",
                    snippet=doc.get("snippet") or doc.get("headline") or "",
                )
                if item is not None:
                    results.append(item)
                if len(results) >= limit:
                    break
            return results

        raise InvalidResponseError(
            "Yandex Cloud v2 response did not contain the expected 'rawData' field.",
            provider=self.provider_name,
        )

    # ------------------------------------------------------------------
    # Mode B: Classic XML Interface
    # ------------------------------------------------------------------
    async def _search_xml(
        self,
        clean_q: str,
        limit: int,
        probe_timeout: Optional[float] = None,
    ) -> List[SearchResult]:
        params: Dict[str, Any] = {
            "apikey": self.api_key,
            "query": clean_q,
            "page": 0,
        }
        if self.folder_id:
            params["folderid"] = self.folder_id
        if self.region:
            params["lr"] = self.region

        effective_timeout = probe_timeout or self.timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Yandex XML request timed out after {effective_timeout:.1f}s.",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Yandex XML network error: {exc}",
                provider=self.provider_name,
            ) from exc

        if resp.status_code == 400:
            raise ConfigurationError(
                f"Yandex XML rejected request parameters (HTTP 400): {resp.text[:200]}",
                provider=self.provider_name,
                status_code=400,
            )
        if resp.status_code == 401:
            raise AuthenticationError(
                "Yandex XML rejected the API key (HTTP 401).",
                provider=self.provider_name,
                status_code=401,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "Yandex XML access forbidden (HTTP 403). Verify key permissions and IP allowlist.",
                provider=self.provider_name,
                status_code=403,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "Yandex XML rate limit exceeded (HTTP 429).",
                provider=self.provider_name,
                status_code=429,
            )
        if resp.status_code != 200:
            raise UpstreamError(
                f"Yandex XML API error HTTP {resp.status_code}: {resp.text[:200]}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        return self._parse_yandex_xml_document(resp.text, limit=limit)

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
            self._validate_configuration()
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

        try:
            if self.mode == "cloud_v2":
                results = await self._search_cloud_v2(
                    clean_q=clean_q,
                    limit=limit,
                    lang=lang,
                    country=country,
                )
            else:
                results = await self._search_xml(clean_q=clean_q, limit=limit)
        except SearchProviderError as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
            logger.warning(f"Yandex search error [{exc.error_code}]: {exc.message}")
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
            logger.warning(f"Yandex unexpected error: {exc}")
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
                f"Yandex ({self.mode}) completed the request but returned zero "
                f"matching documents for this query."
            ),
        )


__all__ = ["YandexSearchProvider"]
