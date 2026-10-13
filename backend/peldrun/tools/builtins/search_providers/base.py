"""
backend/peldrun/tools/builtins/search_providers/base.py

Abstract Base Interface and Contract for PELDRUN Search Providers.
Defines canonical schemas, defensive prompt injection sanitizers,
and uniform execution responses adhering to 2026 AI Agent tool standards.
"""

from __future__ import annotations

import html
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """
    Standardized atomic result item returned by any search provider.
    """
    title: str = Field(..., description="Document title or sanitized resource name.")
    url: str = Field(..., description="Target destination verified HTTP/HTTPS link.")
    snippet: str = Field(..., description="Cleaned, injection-sanitized summary or Markdown extract.")
    raw_content: Optional[str] = Field(default=None, description="Optional raw markdown page content.")
    score: Optional[float] = Field(default=None, description="Relevance ranking score if provided.")


class SearchResponse(BaseModel):
    """
    Structured outcome model for search provider invocations.
    Carries audit metrics, latency telemetry, and provider attribution.
    """
    query: str = Field(..., description="Original search keywords evaluated.")
    provider: str = Field(..., description="Identifier of the search provider that handled the request.")
    results: List[SearchResult] = Field(default_factory=list, description="Array of verified search results.")
    latency_ms: float = Field(default=0.0, description="End-to-end execution latency in milliseconds.")
    estimated_cost_usd: float = Field(default=0.0, description="Calculated operational cost in USD.")
    is_success: bool = Field(default=True, description="True if results were retrieved without unrecoverable errors.")
    error_message: Optional[str] = Field(default=None, description="Detailed diagnostic error message on failure.")

    def format_markdown(self) -> str:
        """
        Formats search results into LLM-optimized Markdown with explicit attribution instructions.
        """
        if not self.results:
            return f"Search completed via {self.provider} for query '{self.query}'. No verified results were found."

        lines: List[str] = [
            f"### Verified Web Search Results via {self.provider} for: '{self.query}'",
            "*(You MUST cite these sources and preserve their target URLs in your response)*\n",
        ]
        for idx, item in enumerate(self.results, start=1):
            lines.append(f"**{idx}. [{item.title}]({item.url})**")
            lines.append(f"   - **URL:** {item.url}")
            lines.append(f"   - **Snippet:** {item.snippet}")
            if item.raw_content:
                truncated_content = item.raw_content[:800].replace("\n", " ").strip()
                lines.append(f"   - **Content Preview:** {truncated_content}...")
            lines.append("")

        return "\n".join(lines).strip()


class BaseSearchProvider(ABC):
    """
    Abstract search provider strategy.
    Encapsulates HTTP transport, defensive input sanitization, and cost accounting.
    """

    provider_name: str = "base"
    cost_per_query: float = 0.0

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = config or {}

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 5,
        lang: str = "en",
        country: str = "us",
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Executes web search and returns structured SearchResponse.
        Must be implemented by concrete engine adapters.
        """
        raise NotImplementedError("Search provider must implement 'search' method.")

    @abstractmethod
    async def ping(self) -> bool:
        """
        Validates provider configuration, API key, and network reachability.
        Returns True if operational, False otherwise.
        """
        raise NotImplementedError("Search provider must implement 'ping' method.")

    def sanitize_snippet(self, raw_text: str) -> str:
        """
        Defensive prompt injection mitigation for 2026 AI Agent architectures.
        Neutralizes hidden markdown commands, system prompt override markers,
        and unescaped system tokens embedded in scraped web content.
        """
        if not raw_text:
            return ""

        cleaned = html.unescape(raw_text)

        # Neutralize common indirect prompt injection triggers
        cleaned = re.sub(
            r"(?i)(ignore (all )?previous instructions|system override|you are now|disregard prior directions)",
            r"[FILTERED_PROMPT_INJECTION]",
            cleaned,
        )

        # Neutralize ChatML / delimiter tokens
        cleaned = re.sub(r"(<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[SYSTEM\])", r"[\1_ESCAPED]", cleaned)

        # Collapse excessive whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned
