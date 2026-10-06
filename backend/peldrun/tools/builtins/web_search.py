"""
PELDRUN Core Web Search Tool.
Provides resilient multi-engine web search with direct citations, link attribution,
anti-thrashing loop prevention, and graceful multi-provider fallbacks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Type
import httpx
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult

logger = logging.getLogger("peldrun.tools.builtins.web_search")


class WebSearchArgs(BaseModel):
    """Input arguments schema for web search queries."""
    query: str = Field(
        ...,
        description="The search query terms to look up on the live web."
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=15,
        description="Maximum number of search results to retrieve (default: 5)."
    )


class WebSearchTool(BaseTool):
    """
    Core tool for searching the web and retrieving relevant external information.
    Includes active link attribution, structured snippets, and duplicate query guards.
    """

    name: str = "web_search"
    description: str = (
        "Searches the web for up-to-date news, technical documentation, and real-time facts. "
        "Returns titles, direct URLs, and concise snippets for relevant query results."
    )
    args_schema: Optional[Type[BaseModel]] = WebSearchArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        timeout: float = 12.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.timeout = timeout
        self._transport = transport
        self._searched_queries: Set[str] = set()
        self._query_counter: int = 0

    async def _arun(
        self,
        query: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute web search asynchronously and format results with links."""
        clean_query = query.strip()
        if not clean_query:
            return ToolResult(
                output="Search query cannot be empty.",
                exit_code=1,
                is_error=True,
            )

        self._query_counter += 1
        normalized_q = clean_query.lower()

        # Anti-thrashing guard: Check for identical repetitive searches
        if normalized_q in self._searched_queries and self._query_counter > 3:
            return ToolResult(
                output=(
                    f"Notice: You have already searched for '{clean_query}'. "
                    "Do not repeat the same search. Please formulate your final response to the user "
                    "or proceed with the next autonomous step."
                ),
                exit_code=0,
                is_error=False,
                metadata={"query": clean_query, "duplicate": True},
            )

        self._searched_queries.add(normalized_q)
        logger.info("Executing web search query #%d: '%s' (max_results=%d)", self._query_counter, clean_query, max_results)

        items: List[Dict[str, str]] = []

        # Attempt 1: Official ddgs / duckduckgo_search package
        
        if self._transport is None:
            try:
                try:
                    from ddgs import DDGS
                except ImportError:
                    from duckduckgo_search import DDGS

                def _sync_ddgs_search() -> List[Dict[str, str]]:
                    parsed = []
                    with DDGS() as ddgs:
                        for item in ddgs.text(clean_query, max_results=max_results):
                            href = item.get("href") or item.get("link") or ""
                            if href:
                                parsed.append({
                                    "title": item.get("title") or "Web Result",
                                    "url": href,
                                    "snippet": item.get("body") or item.get("snippet") or "",
                                })
                    return parsed

                items = await asyncio.to_thread(_sync_ddgs_search)
            except Exception as ddg_err:
                logger.debug("ddgs/duckduckgo_search library search unavailable: %s", ddg_err)

        # Attempt 2: Resilient DuckDuckGo Lite Interface
        if not items:
            try:
                items = await self._search_duckduckgo_lite(clean_query, max_results)
            except Exception as lite_err:
                logger.debug("DuckDuckGo Lite search error: %s", lite_err)

        # Attempt 3: Direct DuckDuckGo HTML Fallback
        if not items:
            try:
                items = await self._fallback_http_search(clean_query, max_results)
            except Exception as html_err:
                logger.debug("DuckDuckGo HTML search error: %s", html_err)

        # Format results or provide meaningful non-looping guidance
        if items:
            return self._format_results(items, clean_query)

        return ToolResult(
            output=(
                f"Search completed for '{clean_query}'. No concise web snippets were returned by search engines. "
                "Guidance: Do not continue spamming search queries. Synthesize your final answer with available "
                "knowledge, or ask the user for specific URLs or clarification via ask_human."
            ),
            exit_code=0,
            is_error=False,
            metadata={"query": clean_query, "count": 0},
        )

    def _run(self, query: str, max_results: int = 5, **kwargs: Any) -> ToolResult:
        """Synchronous tool invocation implementation."""
        return super()._run(query=query, max_results=max_results, **kwargs)

    async def _search_duckduckgo_lite(self, query: str, max_results: int) -> List[Dict[str, str]]:
        """Query DuckDuckGo Lite endpoint (clean HTML, highly resilient against bot challenges)."""
        url = "https://lite.duckduckgo.com/lite/"
        data = {"q": query}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            resp = await client.post(url, data=data, headers=headers)
            if resp.status_code != 200:
                return []
            html_text = resp.text

        results: List[Dict[str, str]] = []
        # Parse result links and snippets from DuckDuckGo Lite layout
        link_pattern = re.compile(r'<a class=["\']result-link["\'] href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.DOTALL)
        snippet_pattern = re.compile(r'<td class=["\']result-snippet["\'][^>]*>(.*?)</td>', re.DOTALL)

        raw_links = link_pattern.findall(html_text)
        raw_snippets = snippet_pattern.findall(html_text)

        limit = min(len(raw_links), max_results)
        for i in range(limit):
            href, raw_title = raw_links[i]
            # Extract actual target url from redirect parameter if present
            clean_url = href
            if "uddg=" in href:
                match = re.search(r'uddg=([^&]+)', href)
                if match:
                    clean_url = urllib.parse.unquote(match.group(1))

            title = re.sub(r'<[^>]+>', '', raw_title).strip()
            snippet = ""
            if i < len(raw_snippets):
                snippet = re.sub(r'<[^>]+>', '', raw_snippets[i]).strip()

            if clean_url and not clean_url.startswith("/"):
                results.append({
                    "title": title or clean_url,
                    "url": clean_url,
                    "snippet": snippet,
                })

        return results

    async def _fallback_http_search(self, query: str, max_results: int) -> List[Dict[str, str]]:
        """Query DuckDuckGo standard HTML interface via async HTTP client as fallback."""
        url = "https://html.duckduckgo.com/html/"
        data = {"q": query}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        }

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            resp = await client.post(url, data=data, headers=headers)
            if resp.status_code != 200:
                return []
            html_text = resp.text

        results: List[Dict[str, str]] = []
        link_matches = re.findall(
            r'<a class="result__url" href="([^"]+)".*?>(.*?)</a>',
            html_text,
            re.DOTALL,
        )
        snippet_matches = re.findall(
            r'<a class="result__snippet[^"]*"[^>]*>(.*?)</a>',
            html_text,
            re.DOTALL,
        )

        total = min(len(link_matches), max_results)
        for i in range(total):
            raw_url, raw_title = link_matches[i]
            url_match = re.search(r'uddg=([^&]+)', raw_url)
            actual_url = urllib.parse.unquote(url_match.group(1)) if url_match else raw_url.strip()
            title = re.sub(r'<[^>]+>', '', raw_title).strip()
            snippet = ""
            if i < len(snippet_matches):
                snippet = re.sub(r'<[^>]+>', '', snippet_matches[i]).strip()

            results.append({
                "title": title or actual_url,
                "url": actual_url,
                "snippet": snippet,
            })

        return results

    def _format_results(self, items: List[Dict[str, str]], query: str) -> ToolResult:
        """
        Format parsed search items into structured output with visible links and markdown citations.
        """
        formatted_lines = [
            f"### Verified Web Search Results for: '{query}'",
            "*(You MUST cite these sources and include their URLs in your final response to the user)*\n"
        ]

        for idx, item in enumerate(items, start=1):
            title = item.get("title", "Article")
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            
            # Extract clean domain for citation label
            domain = ""
            try:
                domain = urllib.parse.urlparse(url).netloc
            except Exception:
                pass

            domain_tag = f"[{domain}]" if domain else ""
            formatted_lines.append(
                f"**{idx}. [{title}]({url})** {domain_tag}\n"
                f"   - **URL:** {url}\n"
                f"   - **Snippet:** {snippet}\n"
            )

        output_str = "\n".join(formatted_lines).strip()
        return ToolResult(
            output=output_str,
            exit_code=0,
            is_error=False,
            metadata={"query": query, "count": len(items), "results": items},
        )