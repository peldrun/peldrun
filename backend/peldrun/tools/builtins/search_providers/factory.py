"""
backend/peldrun/tools/builtins/search_providers/factory.py

Dynamic Search Provider Factory and Orchestration Engine.
Validates provider enablement, injects user-defined per-query pricing,
and orchestrates fallback chains across multiple search engines.

Fixes applied:
- Corrected Jina operability check (was always returning True).
- Respects user configuration: no silent DuckDuckGo injection when
  the user has explicitly disabled all providers.
- Added global timeout budget across the fallback chain to prevent
  exceeding the outer tool timeout.
- Added URL-based deduplication when aggregating results across
  multiple providers to reduce token waste.
- Explicit Air-Gapped mode behavior preserved.
- Improved credential validation for every provider.
- Type-safe cost override parsing with strict bounds.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Type

from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    SearchResponse,
    SearchResult,
)
from peldrun.tools.builtins.search_providers.duckduckgo_provider import DuckDuckGoSearchProvider
from peldrun.tools.builtins.search_providers.searxng_provider import SearXNGSearchProvider
from peldrun.tools.builtins.search_providers.tavily_provider import TavilySearchProvider
from peldrun.tools.builtins.search_providers.brave_provider import BraveSearchProvider
from peldrun.tools.builtins.search_providers.google_provider import GoogleSearchProvider
from peldrun.tools.builtins.search_providers.jina_provider import JinaSearchProvider
from peldrun.tools.builtins.search_providers.yandex_provider import YandexSearchProvider

logger = logging.getLogger("peldrun.search.factory")


class SearchProviderFactory:
    """Factory creating and orchestrating configured search provider instances."""

    _REGISTRY: Dict[str, Type[BaseSearchProvider]] = {
        "DuckDuckGo": DuckDuckGoSearchProvider,
        "SearXNG": SearXNGSearchProvider,
        "Tavily": TavilySearchProvider,
        "Brave": BraveSearchProvider,
        "Google": GoogleSearchProvider,
        "Jina": JinaSearchProvider,
        "Yandex": YandexSearchProvider,
    }

    # Hard ceiling on total wall-clock time across the entire fallback chain.
    # Must remain strictly below the WebSearchTool.execution_policy.timeout_seconds.
    DEFAULT_TOTAL_BUDGET_SECONDS: float = 22.0

    # Minimum remaining time to attempt another provider in the chain.
    MIN_PROVIDER_BUDGET_SECONDS: float = 3.0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @classmethod
    def list_supported_providers(cls) -> List[str]:
        return list(cls._REGISTRY.keys())

    # ------------------------------------------------------------------
    # Provider instantiation
    # ------------------------------------------------------------------
    @classmethod
    def create_provider(
        cls,
        name: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> BaseSearchProvider:
        """
        Instantiate a provider by name, injecting the effective sub-config
        and honoring any user-defined cost override.
        """
        norm_name = name.strip()
        matched_key = next(
            (k for k in cls._REGISTRY if k.lower() == norm_name.lower()),
            None,
        )
        if not matched_key:
            logger.warning(
                f"Unknown search provider '{name}'. Falling back to DuckDuckGo."
            )
            matched_key = "DuckDuckGo"

        provider_cls = cls._REGISTRY[matched_key]

        # Build sub-config: global search config merged with the engine's
        # dedicated [search.<engine>] table, with the engine table taking
        # precedence for overlapping keys.
        sub_config: Dict[str, Any] = {}
        if config:
            engine_key = matched_key.lower()
            engine_overrides = config.get(engine_key)
            if isinstance(engine_overrides, dict):
                sub_config = {**config, **engine_overrides}
            else:
                sub_config = dict(config)

        provider_instance = provider_cls(config=sub_config)

        # Apply user-configured pricing override with strict validation.
        if "cost_per_query" in sub_config:
            try:
                parsed_cost = float(sub_config["cost_per_query"])
                if parsed_cost >= 0.0:
                    provider_instance.cost_per_query = parsed_cost
                else:
                    logger.warning(
                        f"Negative cost_per_query for {matched_key} ignored."
                    )
            except (ValueError, TypeError):
                logger.warning(
                    f"Invalid cost_per_query for {matched_key}: "
                    f"{sub_config['cost_per_query']!r}"
                )

        return provider_instance

    # ------------------------------------------------------------------
    # Operability checks
    # ------------------------------------------------------------------
    @classmethod
    def is_engine_operable(
        cls,
        engine_name: str,
        search_config: Dict[str, Any],
    ) -> bool:
        """
        Determine whether a search engine is enabled and has the minimum
        credentials required to run a query.
        """
        eng_lower = engine_name.lower()
        sub_cfg = search_config.get(eng_lower) or {}

        # Explicit disablement always wins.
        if sub_cfg.get("enabled") is False:
            return False

        # Per-engine credential requirements.
        if eng_lower == "duckduckgo":
            return True

        if eng_lower == "searxng":
            return bool(str(sub_cfg.get("base_url", "")).strip())

        if eng_lower == "tavily":
            return bool(str(sub_cfg.get("api_key", "")).strip())

        if eng_lower == "brave":
            return bool(str(sub_cfg.get("api_key", "")).strip())

        if eng_lower == "google":
            has_key = bool(str(sub_cfg.get("api_key", "")).strip())
            has_cx = bool(str(sub_cfg.get("cx", "")).strip())
            return has_key and has_cx

        if eng_lower == "jina":
            # Jina works with or without a key, but anonymous mode is
            # heavily rate-limited. Require a key unless the user has
            # explicitly opted into anonymous access.
            has_key = bool(str(sub_cfg.get("api_key", "")).strip())
            allow_no_key = bool(sub_cfg.get("allow_no_key", False))
            return has_key or allow_no_key

        if eng_lower == "yandex":
            return bool(str(sub_cfg.get("api_key", "")).strip())

        # Unknown engine: defer to the caller (assume operable).
        return True

    # ------------------------------------------------------------------
    # Result aggregation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _deduplicate_results(
        results: List[SearchResult],
        seen_urls: set,
    ) -> List[SearchResult]:
        """
        Filter out results whose URL has already been seen across
        previous providers in the same orchestration cycle.
        """
        unique: List[SearchResult] = []
        for item in results:
            url_key = (item.url or "").strip().lower()
            if not url_key or url_key in seen_urls:
                continue
            seen_urls.add(url_key)
            unique.append(item)
        return unique

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    @classmethod
    def _build_candidate_chain(
        cls,
        search_config: Dict[str, Any],
    ) -> List[str]:
        """
        Build the ordered list of engines to try:
          1. Primary engine from config.
          2. User-configured fallback engines (deduplicated).
          3. No silent additions: if all engines are disabled,
             the caller receives an empty list and must handle it.
        """
        primary = search_config.get("engine", "DuckDuckGo")
        fallback = search_config.get("fallback_engines") or []

        chain: List[str] = []
        if primary:
            chain.append(primary)
        for engine in fallback:
            if engine and engine not in chain:
                chain.append(engine)

        return chain

    @classmethod
    async def execute_orchestrated_search(
        cls,
        query: str,
        search_config: Dict[str, Any],
        max_results: int = 5,
        lang: str = "en",
        country: str = "us",
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute a search across the configured fallback chain, returning the
        first provider response that yields usable results.
        """
        # Air-Gapped mode: refuse to touch the network.
        if search_config.get("enabled") is False:
            return SearchResponse(
                query=query,
                provider="AirGapped",
                results=[],
                is_success=False,
                error_message=(
                    "Web search is administratively disabled "
                    "(Air-Gapped Mode)."
                ),
            )

        # Build the raw candidate chain from user configuration.
        raw_chain = cls._build_candidate_chain(search_config)

        # Filter to engines that are both enabled and correctly configured.
        active_chain = [
            engine for engine in raw_chain
            if cls.is_engine_operable(engine, search_config)
        ]

        # Explicit failure when no engine is available. The user may have
        # disabled every provider on purpose; do not silently re-enable DDG.
        if not active_chain:
            return SearchResponse(
                query=query,
                provider="None",
                results=[],
                is_success=False,
                error_message=(
                    "No search provider is currently enabled and configured. "
                    "Enable at least one engine in Search Settings."
                ),
            )

        # Total wall-clock budget across the entire chain.
        total_budget = float(
            search_config.get(
                "total_budget_seconds",
                cls.DEFAULT_TOTAL_BUDGET_SECONDS,
            )
        )
        deadline = time.monotonic() + total_budget

        effective_lang = lang or search_config.get("lang", "en")
        effective_country = country or search_config.get("country", "us")

        seen_urls: set = set()
        aggregated_results: List[SearchResult] = []
        last_response: Optional[SearchResponse] = None

        for engine_name in active_chain:
            remaining = deadline - time.monotonic()

            # Not enough time left to attempt another provider.
            if remaining < cls.MIN_PROVIDER_BUDGET_SECONDS:
                logger.warning(
                    f"Search budget exhausted before trying '{engine_name}'. "
                    f"Remaining: {remaining:.2f}s"
                )
                break

            try:
                provider = cls.create_provider(
                    name=engine_name,
                    config=search_config,
                )

                response = await provider.search(
                    query=query,
                    max_results=max_results,
                    lang=effective_lang,
                    country=effective_country,
                    **kwargs,
                )

                last_response = response

                # Successful provider with results: aggregate and deduplicate.
                if response.is_success and response.results:
                    unique_new = cls._deduplicate_results(
                        response.results,
                        seen_urls,
                    )
                    aggregated_results.extend(unique_new)

                    # A single successful provider is considered sufficient.
                    # Return immediately to keep latency low.
                    if aggregated_results:
                        response.results = aggregated_results
                        return response

            except Exception as exc:
                logger.error(
                    f"Provider '{engine_name}' raised an exception: {exc}"
                )
                last_response = SearchResponse(
                    query=query,
                    provider=engine_name,
                    results=[],
                    is_success=False,
                    error_message=str(exc),
                )

        # No provider returned results: return the last diagnostic response.
        if last_response is not None:
            last_response.results = aggregated_results
            if not last_response.error_message and not aggregated_results:
                last_response.error_message = (
                    "All operable search providers returned no results."
                )
            return last_response

        return SearchResponse(
            query=query,
            provider="None",
            results=[],
            is_success=False,
            error_message=(
                "Search chain produced no response. All configured providers "
                "failed to execute within the allotted budget."
            ),
        )


__all__ = ["SearchProviderFactory"]
