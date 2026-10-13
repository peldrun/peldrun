"""
backend/peldrun/tools/builtins/search_providers/yandex_provider.py

Yandex Search API Integration.
Supports Yandex Search API (XML / REST / Cloud Search API v2) with
localized ranking and Russian/Global indexing.

Fixes applied:
- Explicit success/failure reporting based on actual result count.
- Cost is only charged when results are returned.
- Descriptive error messages for zero-result responses.
- Correct XML namespace handling for Yandex Search API v5+ schema.
- Auto-detection of XML namespaces to support both legacy and modern formats.
- Explicit handling of common HTTP error codes (401, 403, 429, 500).
- Defensive parsing of nested JSON and XML structures.
- Safe XML parse error handling with descriptive diagnostics.
- Centralized result limit clamping against user-configured cap.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import httpx

from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    SearchResponse,
    SearchResult,
)

logger = logging.getLogger("peldrun.search.yandex")


class YandexSearchProvider(BaseSearchProvider):
    """
    Yandex Search API provider supporting both XML and JSON search formats.
    Two operational modes:
      - Cloud Search API v2 (JSON): requires folder_id + api_key.
      - Classic XML interface: requires api_key only.
    Cost: approximately $0.003 per successful query.
    """

    provider_name: str = "Yandex"
    cost_per_query: float = 0.003

    # Common Yandex XML namespaces across schema versions.
    # The provider tries each in order until one yields <group> elements.
    _NAMESPACE_CANDIDATES: Tuple[str, ...] = (
        "http://xml.yandex.ru/schema/5/",
        "http://xml.yandex.ru/schema/4/",
        "http://xml.yandex.ru/schema/3/",
    )

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config=config)
        self.api_key: str = str(self.config.get("api_key", "")).strip()
        self.folder_id: str = str(self.config.get("folder_id", "")).strip()

        default_endpoint = (
            "https://searchapi.yandex.net/v2/web/search"
            if self.folder_id
            else "https://yandex.com/search/xml"
        )
        self.endpoint: str = self.config.get("endpoint", default_endpoint)

        self.timeout_seconds: float = float(self.config.get("timeout_seconds", 12.0))
        self.max_results_cap: int = int(self.config.get("max_results_cap", 15))
        self.region: str = str(self.config.get("region", "")).strip()

    async def ping(self) -> bool:
        """
        Validates Yandex credentials by issuing a minimal probe request.
        Accepts HTTP 200 as success. HTTP 400 is treated as reachable but
        with a malformed probe (still indicates the endpoint is alive).
        """
        if not self.api_key:
            return False
        try:
            headers = {"Authorization": f"Api-Key {self.api_key}"}
            params = {"query": "ping", "max_results": 1}
            async with httpx.AsyncClient(timeout=4.0, headers=headers) as client:
                res = await client.get(self.endpoint, params=params)
                return res.status_code in (200, 400)
        except Exception as exc:
            logger.debug(f"Yandex ping failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_namespace(root: ET.Element) -> Optional[str]:
        """
        Detect the Yandex XML namespace used by the response.
        Returns the namespace URI string, or None if no namespace is present.
        """
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
        """
        Locate all <group> elements regardless of the namespace variant used.
        Tries no-namespace first, then each candidate namespace in order.
        Returns (groups, applied_namespace) where applied_namespace may be None.
        """
        # Attempt 1: no namespace (legacy flat responses)
        groups = root.findall(".//group")
        if groups:
            return groups, None

        # Attempt 2: auto-detect namespace from the root tag
        detected_ns = self._detect_namespace(root)
        if detected_ns:
            groups = root.findall(f".//{{{detected_ns}}}group")
            if groups:
                return groups, detected_ns

        # Attempt 3: try known Yandex schema namespaces
        for ns_uri in self._NAMESPACE_CANDIDATES:
            groups = root.findall(f".//{{{ns_uri}}}group")
            if groups:
                return groups, ns_uri

        return [], None

    @staticmethod
    def _find_child(
        element: ET.Element,
        name: str,
        namespace: Optional[str],
    ) -> Optional[ET.Element]:
        """
        Locate a direct child element by name, honoring an optional namespace.
        Returns None when the child does not exist.
        """
        if namespace:
            found = element.find(f"{{{namespace}}}{name}")
            if found is not None:
                return found
        return element.find(name)

    @staticmethod
    def _extract_text(element: Optional[ET.Element]) -> str:
        """Extract stripped text content from an element, safely."""
        if element is None or element.text is None:
            return ""
        return element.text.strip()

    # ------------------------------------------------------------------
    # Mode A: Cloud Search API v2 (JSON)
    # ------------------------------------------------------------------
    async def _search_cloud_v2(
        self,
        clean_q: str,
        limit: int,
    ) -> List[SearchResult]:
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "folderId": self.folder_id,
            "query": clean_q,
            "page": 0,
            "pageSize": limit,
        }

        results: List[SearchResult] = []
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, headers=headers
        ) as client:
            resp = await client.post(self.endpoint, json=payload)

            if resp.status_code == 401:
                raise RuntimeError(
                    "Yandex Cloud v2 rejected the API key (HTTP 401)."
                )
            if resp.status_code == 403:
                raise RuntimeError(
                    "Yandex Cloud v2 access forbidden (HTTP 403). "
                    "Verify the folder ID and API key permissions."
                )
            if resp.status_code == 429:
                raise RuntimeError(
                    "Yandex Cloud v2 rate limit exceeded (HTTP 429)."
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Yandex v2 API error HTTP {resp.status_code}: {resp.text[:200]}"
                )

            data = resp.json()
            docs = (data.get("response") or {}).get("docs") or []

            for doc in docs:
                url = doc.get("url") or ""
                title = doc.get("title") or ""
                snippet = doc.get("snippet") or ""

                if url and title:
                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            snippet=self.sanitize_snippet(snippet),
                        )
                    )
                if len(results) >= limit:
                    break

        return results

    # ------------------------------------------------------------------
    # Mode B: Classic XML interface
    # ------------------------------------------------------------------
    async def _search_xml(
        self,
        clean_q: str,
        limit: int,
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

        results: List[SearchResult] = []

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.get(self.endpoint, params=params)

            if resp.status_code == 401:
                raise RuntimeError(
                    "Yandex XML rejected the API key (HTTP 401)."
                )
            if resp.status_code == 403:
                raise RuntimeError(
                    "Yandex XML access forbidden (HTTP 403). "
                    "The API key may lack search permissions."
                )
            if resp.status_code == 429:
                raise RuntimeError(
                    "Yandex XML rate limit exceeded (HTTP 429)."
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Yandex XML API error HTTP {resp.status_code}: {resp.text[:200]}"
                )

        # Parse XML outside the async context (CPU-bound work)
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"Yandex XML response could not be parsed: {exc}. "
                f"Response preview: {resp.text[:200]}"
            )

        groups, ns_used = self._extract_groups_with_namespace(root)
        if ns_used:
            logger.debug(f"Yandex XML namespace detected: {ns_used}")

        for group in groups:
            doc = self._find_child(group, "doc", ns_used)
            if doc is None:
                continue

            url_elem = self._find_child(doc, "url", ns_used)
            title_elem = self._find_child(doc, "title", ns_used)
            headline_elem = self._find_child(doc, "headline", ns_used)

            url_text = self._extract_text(url_elem)
            title_text = self._extract_text(title_elem)
            headline_text = self._extract_text(headline_elem)

            if url_text:
                results.append(
                    SearchResult(
                        title=title_text or url_text,
                        url=url_text,
                        snippet=self.sanitize_snippet(headline_text),
                    )
                )
            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # Public entry point
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
                error_message="Yandex Search API key is not configured.",
            )

        limit = max(1, min(int(max_results), self.max_results_cap))

        try:
            if "searchapi.yandex.net" in self.endpoint:
                results = await self._search_cloud_v2(clean_q, limit)
            else:
                results = await self._search_xml(clean_q, limit)

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.warning(f"Yandex search error: {exc}")
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
                "Yandex returned zero results for this query. "
                "The query may have no indexed matches, or the API may "
                "be filtering results for the configured region."
            ),
        )


__all__ = ["YandexSearchProvider"]
