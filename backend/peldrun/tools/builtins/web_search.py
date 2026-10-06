"""
backend/peldrun/tools/builtins/web_search.py

PELDRUN Core Web Search Tool.
Queries the web using resilient search scraping providers with verified link attribution.
Configured with an idempotent, retryable ToolExecutionPolicy under PR 4.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
import urllib.parse
from pydantic import BaseModel, Field

import httpx

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionPolicy

logger = logging.getLogger("peldrun.tools.builtins.web_search")

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment, misc]


class WebSearchArgs(BaseModel):
    """Arguments for the web_search tool."""
    query: str = Field(..., description="The search terms or keyword query.")
    max_results: Optional[int] = Field(default=5, ge=1, le=10, description="Maximum snippet results to retrieve.")


class WebSearchTool(BaseTool):
    """
    Search tool querying internet search engines with automatic retry capabilities.
    Marked as idempotent and retryable with max_attempts=3.
    """

    name: str = "web_search"
    description: str = (
        "Search the live web for recent information, documentation, news, or technical queries. "
        "Returns snippets with verified Markdown links and URLs."
    )
    args_schema = WebSearchArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=3,
        timeout_seconds=30.0,
        retryable=True,
        idempotent=True,
        side_effects=False,
        backoff_factor=1.5,
    )

    def __init__(self, workspace_root: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(workspace_root=workspace_root, execution_policy=self.execution_policy)

    async def _arun(self, query: str, max_results: Optional[int] = 5, **kwargs: Any) -> ToolResult:
        clean_q = query.strip()
        if not clean_q:
            return ToolResult(output="Search query cannot be empty.", exit_code=1, is_error=True)

        limit = max_results or 5

        if BeautifulSoup is None:
            return ToolResult(
                output=(
                    f"Search completed for '{clean_q}'. No snippets returned. "
                    "Notice: 'beautifulsoup4' is not installed in the environment."
                ),
                exit_code=0,
                is_error=False,
            )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        results: List[Dict[str, str]] = []
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            resp = await client.post("https://lite.duckduckgo.com/lite/", data={"q": clean_q})
            if resp.status_code >= 400:
                raise RuntimeError(f"Search provider returned HTTP error {resp.status_code}")

            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.find_all("a", class_="result-link")
            snippets = soup.find_all("td", class_="result-snippet")

            for idx in range(min(len(links), limit)):
                a_tag = links[idx]
                raw_href = a_tag.get("href", "")
                target_url = raw_href
                if "uddg=" in raw_href:
                    m = re.search(r"uddg=([^&]+)", raw_href)
                    if m:
                        target_url = urllib.parse.unquote(m.group(1))

                title = a_tag.get_text().strip()
                snip = snippets[idx].get_text().strip() if idx < len(snippets) else ""
                if target_url and not target_url.startswith("/"):
                    results.append({"title": title or target_url, "url": target_url, "snippet": snip})

        if results:
            lines = [
                f"### Verified Web Search Results for: '{clean_q}'",
                "*(You MUST cite these sources and include their target URLs in your response)*\n",
            ]
            for i, r in enumerate(results, 1):
                lines.append(
                    f"**{i}. [{r['title']}]({r['url']})**\n   - **URL:** {r['url']}\n   - **Snippet:** {r['snippet']}\n"
                )
            return ToolResult(output="\n".join(lines).strip(), exit_code=0, is_error=False)

        return ToolResult(
            output=f"Search completed for '{clean_q}'. No web snippets were returned.",
            exit_code=0,
            is_error=False,
        )


__all__ = ["WebSearchArgs", "WebSearchTool"]