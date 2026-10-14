"""
backend/peldrun/tools/builtins/search_providers/factory.py

Dynamic Search Provider Factory and First-Success Failover Orchestrator.

Key guarantees:
- Unknown provider names raise UnknownProviderError explicitly (never silently
  falling back to DuckDuckGo).
- Monotonic deadline is strictly enforced around every individual provider call
  via asyncio.wait_for and per-call timeout clamping.
- Empty fallback_engines=[] is preserved for single-provider execution.
- First-success failover is the default execution model; configuration and
  authentication errors do not silently trigger secondary providers unless
  explicitly enabled via fallback_on_auth_error=true.
- Exposes authoritative provider capabilities and effective pricing metadata
  for the Settings UI and API routes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set, Type
import urllib.parse

from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    ConfigurationError,
    CostBasis,
    PricingMode,
    ProviderCapability,
    ProviderFieldSpec,
    ProviderLifecycle,
    ProviderStatus,
    SearchProviderError,
    SearchResponse,
    SearchResult,
    UnknownProviderError,
)
from peldrun.tools.builtins.search_providers.brave_provider import BraveSearchProvider
from peldrun.tools.builtins.search_providers.duckduckgo_provider import (
    DuckDuckGoSearchProvider,
)
from peldrun.tools.builtins.search_providers.google_provider import GoogleSearchProvider
from peldrun.tools.builtins.search_providers.jina_provider import JinaSearchProvider
from peldrun.tools.builtins.search_providers.searxng_provider import (
    SearXNGSearchProvider,
)
from peldrun.tools.builtins.search_providers.tavily_provider import TavilySearchProvider
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

    # Hard ceiling on total wall-clock time across the entire failover chain.
    # Kept strictly below WebSearchTool.execution_policy.timeout_seconds (25.0s).
    DEFAULT_TOTAL_BUDGET_SECONDS: float = 22.0
    MAX_TOTAL_BUDGET_SECONDS: float = 23.5

    # Minimum remaining time required to start a new provider attempt.
    MIN_PROVIDER_BUDGET_SECONDS: float = 1.5

    # ------------------------------------------------------------------
    # Introspection & Capability Metadata
    # ------------------------------------------------------------------
    @classmethod
    def list_supported_providers(cls) -> List[str]:
        return list(cls._REGISTRY.keys())

    @classmethod
    def resolve_provider_key(cls, name: str) -> str:
        """
        Resolve a provider name case-insensitively against the registry.
        Raises UnknownProviderError if the name is empty or not registered.
        """
        norm_name = (name or "").strip()
        if not norm_name:
            raise UnknownProviderError(
                "Search provider name cannot be empty.",
                provider=name,
            )
        matched_key = next(
            (k for k in cls._REGISTRY if k.lower() == norm_name.lower()),
            None,
        )
        if not matched_key:
            supported = ", ".join(cls._REGISTRY.keys())
            raise UnknownProviderError(
                f"Unknown search provider '{name}'. Supported providers: {supported}.",
                provider=name,
            )
        return matched_key

    @classmethod
    def get_effective_cost(
        cls,
        provider_name: str,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Return the effective per-query cost in USD for a provider,
        combining class defaults with any valid user override in search_config.
        """
        matched_key = cls.resolve_provider_key(provider_name)
        provider_cls = cls._REGISTRY[matched_key]
        default_cost = float(getattr(provider_cls, "cost_per_query", 0.0))

        if not search_config or not isinstance(search_config, dict):
            return default_cost

        sub_cfg = search_config.get(matched_key.lower())
        if isinstance(sub_cfg, dict) and "cost_per_query" in sub_cfg:
            raw_val = sub_cfg.get("cost_per_query")
            if raw_val is not None and raw_val != "":
                try:
                    parsed = float(raw_val)
                    if parsed >= 0.0:
                        return parsed
                except (TypeError, ValueError):
                    pass
        return default_cost

    @classmethod
    def get_provider_capabilities(
        cls,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build authoritative capability descriptors for all supported providers,
        including effective pricing and lifecycle notices.
        """
        cfg = search_config or {}
        capabilities: List[ProviderCapability] = [
            ProviderCapability(
                id="DuckDuckGo",
                config_key="duckduckgo",
                display_name="DuckDuckGo",
                tagline="Zero-credential fallback engine with regional & SafeSearch support",
                docs_url="https://github.com/deedy5/ddgs",
                lifecycle=ProviderLifecycle.SUPPORTED,
                requires_credentials=False,
                supports_language=True,
                supports_country=True,
                supports_safesearch=True,
                supports_raw_content=False,
                max_results_limit=20,
                pricing_mode=PricingMode.FREE_PUBLIC,
                default_cost_per_query=0.0,
                effective_cost_per_query=cls.get_effective_cost("DuckDuckGo", cfg),
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                cost_editable=True,
                supports_non_billable_reachability=True,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=False,
                test_button_label="Check Reachability",
                fields=[],
            ),
            ProviderCapability(
                id="SearXNG",
                config_key="searxng",
                display_name="SearXNG",
                tagline="Self-hosted meta-search aggregator (requires JSON format enabled)",
                docs_url="https://docs.searxng.org/dev/search_api.html",
                lifecycle=ProviderLifecycle.SUPPORTED,
                requires_credentials=True,
                supports_language=True,
                supports_country=False,
                supports_safesearch=True,
                supports_raw_content=False,
                max_results_limit=20,
                pricing_mode=PricingMode.SELF_HOSTED_ZERO,
                default_cost_per_query=0.0,
                effective_cost_per_query=cls.get_effective_cost("SearXNG", cfg),
                cost_basis=CostBasis.ASSUMED_ZERO_LOCAL,
                cost_editable=True,
                supports_non_billable_reachability=True,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=False,
                test_button_label="Check Instance & JSON",
                fields=[
                    ProviderFieldSpec(
                        key="base_url",
                        label="Instance Base URL",
                        field_type="text",
                        required=True,
                        placeholder="http://localhost:8080",
                        mono=True,
                    ),
                ],
            ),
            ProviderCapability(
                id="Tavily",
                config_key="tavily",
                display_name="Tavily AI",
                tagline="LLM-optimized search API with credit usage reporting",
                docs_url="https://docs.tavily.com/documentation/api-reference/endpoint/search",
                lifecycle=ProviderLifecycle.SUPPORTED,
                requires_credentials=True,
                supports_language=False,
                supports_country=False,
                supports_safesearch=False,
                supports_raw_content=True,
                max_results_limit=20,
                pricing_mode=PricingMode.PER_CREDIT,
                default_cost_per_query=0.005,
                effective_cost_per_query=cls.get_effective_cost("Tavily", cfg),
                cost_basis=CostBasis.PROVIDER_REPORTED,
                cost_editable=True,
                supports_non_billable_reachability=False,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=True,
                diagnostic_warning="Executes a minimal search probe that may consume 1 API credit.",
                test_button_label="Verify Key (1 Credit)",
                supported_modes=["basic", "advanced", "fast", "ultra-fast"],
                fields=[
                    ProviderFieldSpec(
                        key="api_key",
                        label="API Key (Bearer)",
                        field_type="password",
                        required=True,
                        placeholder="tvly-••••••",
                        secret_key="tavily",
                    ),
                    ProviderFieldSpec(
                        key="search_depth",
                        label="Search Depth",
                        field_type="select",
                        required=False,
                        placeholder="basic",
                        options=["basic", "advanced", "fast", "ultra-fast"],
                    ),
                ],
            ),
            ProviderCapability(
                id="Brave",
                config_key="brave",
                display_name="Brave Search",
                tagline="Independent web index (max 20 results per query)",
                docs_url="https://api.search.brave.com/app/documentation/web-search/get-started",
                lifecycle=ProviderLifecycle.SUPPORTED,
                requires_credentials=True,
                supports_language=True,
                supports_country=True,
                supports_safesearch=False,
                supports_raw_content=False,
                max_results_limit=20,
                pricing_mode=PricingMode.PER_REQUEST,
                default_cost_per_query=0.005,
                effective_cost_per_query=cls.get_effective_cost("Brave", cfg),
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
                cost_editable=True,
                supports_non_billable_reachability=False,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=True,
                diagnostic_warning="Executes a minimal search probe that counts as 1 request against quota.",
                test_button_label="Verify Token (Uses Quota)",
                fields=[
                    ProviderFieldSpec(
                        key="api_key",
                        label="Subscription Token",
                        field_type="password",
                        required=True,
                        placeholder="BSA••••••",
                        secret_key="brave",
                    ),
                ],
            ),
            ProviderCapability(
                id="Google",
                config_key="google",
                display_name="Google CSE (Legacy)",
                tagline="Programmable Search Engine JSON API (max 10 results/query)",
                docs_url="https://developers.google.com/custom-search/v1/overview",
                lifecycle=ProviderLifecycle.LEGACY,
                lifecycle_notice=(
                    "Google Custom Search JSON API is closed to new customers and "
                    "scheduled for retirement on 2027-01-01. Plan migration to Brave, "
                    "Tavily, or SearXNG."
                ),
                requires_credentials=True,
                supports_language=True,
                supports_country=True,
                supports_safesearch=False,
                supports_raw_content=False,
                max_results_limit=10,
                pricing_mode=PricingMode.PER_REQUEST,
                default_cost_per_query=0.005,
                effective_cost_per_query=cls.get_effective_cost("Google", cfg),
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
                cost_editable=True,
                supports_non_billable_reachability=False,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=True,
                diagnostic_warning="Executes a minimal CSE query that counts toward daily API quota.",
                test_button_label="Verify CSE (Uses Quota)",
                fields=[
                    ProviderFieldSpec(
                        key="api_key",
                        label="API Key",
                        field_type="password",
                        required=True,
                        placeholder="AIza••••",
                        secret_key="google",
                    ),
                    ProviderFieldSpec(
                        key="cx",
                        label="Search Engine ID (cx)",
                        field_type="text",
                        required=True,
                        placeholder="0123456789abcdef...",
                        mono=True,
                    ),
                ],
            ),
            ProviderCapability(
                id="Jina",
                config_key="jina",
                display_name="Jina Search",
                tagline="Markdown web search endpoint via s.jina.ai",
                docs_url="https://api.jina.ai/docs",
                lifecycle=ProviderLifecycle.SUPPORTED,
                requires_credentials=True,
                supports_language=True,
                supports_country=True,
                supports_safesearch=False,
                supports_raw_content=True,
                max_results_limit=20,
                pricing_mode=PricingMode.PER_REQUEST,
                default_cost_per_query=0.002,
                effective_cost_per_query=cls.get_effective_cost("Jina", cfg),
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
                cost_editable=True,
                supports_non_billable_reachability=False,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=True,
                diagnostic_warning="Executes a minimal search request against s.jina.ai that may consume tokens.",
                test_button_label="Verify Key & Search",
                fields=[
                    ProviderFieldSpec(
                        key="api_key",
                        label="Bearer Token",
                        field_type="password",
                        required=True,
                        placeholder="jina_••••",
                        secret_key="jina",
                    ),
                ],
            ),
            ProviderCapability(
                id="Yandex",
                config_key="yandex",
                display_name="Yandex Search",
                tagline="Yandex Cloud Search API v2 (POST rawData) & Classic XML",
                docs_url="https://yandex.cloud/en/docs/search-api/api-ref/SyncSearch",
                lifecycle=ProviderLifecycle.SUPPORTED,
                requires_credentials=True,
                supports_language=True,
                supports_country=True,
                supports_safesearch=False,
                supports_raw_content=False,
                max_results_limit=15,
                pricing_mode=PricingMode.PER_REQUEST,
                default_cost_per_query=0.003,
                effective_cost_per_query=cls.get_effective_cost("Yandex", cfg),
                cost_basis=CostBasis.CONFIGURED_ESTIMATE,
                cost_editable=True,
                supports_non_billable_reachability=False,
                supports_non_billable_auth_check=False,
                diagnostic_consumes_quota=True,
                diagnostic_warning="Executes a minimal search request that counts toward Yandex Search API usage.",
                test_button_label="Verify Credentials (Uses Quota)",
                supported_modes=["cloud_v2", "classic_xml"],
                fields=[
                    ProviderFieldSpec(
                        key="mode",
                        label="API Mode",
                        field_type="select",
                        required=True,
                        placeholder="cloud_v2",
                        options=["cloud_v2", "classic_xml"],
                    ),
                    ProviderFieldSpec(
                        key="api_key",
                        label="API Key",
                        field_type="password",
                        required=True,
                        placeholder="AQVN••••",
                        secret_key="yandex",
                    ),
                    ProviderFieldSpec(
                        key="folder_id",
                        label="Folder ID (Required for Cloud v2)",
                        field_type="text",
                        required=False,
                        placeholder="b1g••••",
                        mono=True,
                    ),
                ],
            ),
        ]
        return [cap.model_dump() for cap in capabilities]

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
        Raises UnknownProviderError if `name` is not a registered provider.
        """
        matched_key = cls.resolve_provider_key(name)
        provider_cls = cls._REGISTRY[matched_key]

        sub_config: Dict[str, Any] = {}
        has_explicit_cost_override = False
        explicit_cost_value: Optional[float] = None

        if config and isinstance(config, dict):
            engine_key = matched_key.lower()
            engine_overrides = config.get(engine_key)
            if isinstance(engine_overrides, dict):
                sub_config = {**config, **engine_overrides}
                if "cost_per_query" in engine_overrides:
                    raw_cost = engine_overrides["cost_per_query"]
                    if raw_cost is not None and raw_cost != "":
                        try:
                            parsed = float(raw_cost)
                            if parsed >= 0.0:
                                has_explicit_cost_override = True
                                explicit_cost_value = parsed
                            else:
                                logger.warning(
                                    f"Negative cost_per_query for {matched_key} ignored."
                                )
                        except (ValueError, TypeError):
                            logger.warning(
                                f"Invalid cost_per_query for {matched_key}: {raw_cost!r}"
                            )
            else:
                sub_config = dict(config)
                if "cost_per_query" in sub_config:
                    raw_cost = sub_config["cost_per_query"]
                    if raw_cost is not None and raw_cost != "":
                        try:
                            parsed = float(raw_cost)
                            if parsed >= 0.0:
                                has_explicit_cost_override = True
                                explicit_cost_value = parsed
                        except (ValueError, TypeError):
                            pass

        provider_instance = provider_cls(config=sub_config)

        if has_explicit_cost_override and explicit_cost_value is not None:
            provider_instance.cost_per_query = explicit_cost_value
            provider_instance.cost_overridden = True

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
        Determine whether a registered search engine is enabled and has the
        minimum credentials required to run a query.
        Raises UnknownProviderError if `engine_name` is not registered.
        """
        matched_key = cls.resolve_provider_key(engine_name)
        eng_lower = matched_key.lower()
        sub_cfg = search_config.get(eng_lower)
        if not isinstance(sub_cfg, dict):
            sub_cfg = {}

        if sub_cfg.get("enabled") is False:
            return False

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
            has_key = bool(str(sub_cfg.get("api_key", "")).strip())
            allow_no_key = bool(sub_cfg.get("allow_no_key", False))
            return has_key or allow_no_key

        if eng_lower == "yandex":
            has_key = bool(str(sub_cfg.get("api_key", "")).strip())
            if not has_key:
                return False
            mode = str(sub_cfg.get("mode", "")).strip().lower()
            folder_id = str(sub_cfg.get("folder_id", "")).strip()
            if mode == "classic_xml":
                return True
            if mode == "cloud_v2":
                return bool(folder_id)
            # When mode is unspecified, Cloud v2 requires folder_id while XML does not
            endpoint = str(sub_cfg.get("endpoint", "")).strip().lower()
            if "yandex.com/search/xml" in endpoint or "yandex.ru/search/xml" in endpoint:
                return True
            return bool(folder_id) or True

        return False

    # ------------------------------------------------------------------
    # URL normalization & deduplication helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_url_for_dedup(raw_url: str) -> str:
        """
        Normalize URL for deduplication without corrupting case-sensitive
        path or query parameters:
        - Lowercases scheme and hostname only.
        - Preserves path and query case.
        """
        candidate = (raw_url or "").strip()
        if not candidate:
            return ""
        try:
            parsed = urllib.parse.urlsplit(candidate)
            scheme = (parsed.scheme or "https").lower()
            netloc = (parsed.netloc or "").lower()
            path = parsed.path or "/"
            if path != "/" and path.endswith("/"):
                path = path.rstrip("/")
            return urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))
        except Exception:
            return candidate

    @classmethod
    def _deduplicate_results(
        cls,
        results: List[SearchResult],
        seen_urls: Set[str],
    ) -> List[SearchResult]:
        """Filter out duplicate URLs within a single provider response."""
        unique: List[SearchResult] = []
        for item in results:
            url_key = cls._normalize_url_for_dedup(item.url)
            if not url_key or url_key in seen_urls:
                continue
            seen_urls.add(url_key)
            unique.append(item)
        return unique

    # ------------------------------------------------------------------
    # Candidate chain construction & validation
    # ------------------------------------------------------------------
    @classmethod
    def _build_candidate_chain(
        cls,
        search_config: Dict[str, Any],
    ) -> List[str]:
        """
        Build and validate the ordered list of engines to try:
          1. Primary engine from config.
          2. User-configured fallback_engines (deduplicated).
          3. Never injects unrequested fallback engines when fallback_engines=[].
        Raises UnknownProviderError if any configured engine name is invalid.
        """
        raw_primary = search_config.get("engine")
        primary = str(raw_primary).strip() if raw_primary is not None else "DuckDuckGo"
        if not primary:
            raise ConfigurationError("Primary search engine ('engine') cannot be empty.")

        canonical_primary = cls.resolve_provider_key(primary)

        raw_fallbacks = search_config.get("fallback_engines")
        if raw_fallbacks is None:
            fallback_list: List[Any] = []
        elif isinstance(raw_fallbacks, list):
            fallback_list = raw_fallbacks
        else:
            raise ConfigurationError(
                "'fallback_engines' must be a list of provider names."
            )

        chain: List[str] = [canonical_primary]
        for eng in fallback_list:
            if not eng or not str(eng).strip():
                continue
            canonical_eng = cls.resolve_provider_key(str(eng))
            if canonical_eng not in chain:
                chain.append(canonical_eng)

        return chain

    # ------------------------------------------------------------------
    # Orchestration (First-Success Failover with Strict Deadline)
    # ------------------------------------------------------------------
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
        Execute a search across the validated candidate chain using a
        first-success failover strategy and a strict monotonic time budget.
        """
        if search_config.get("enabled") is False:
            return SearchResponse(
                query=query,
                provider="AirGapped",
                results=[],
                status=ProviderStatus.DISABLED,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="air_gapped",
                error_message="Web search is administratively disabled (Air-Gapped Mode).",
            )

        try:
            raw_chain = cls._build_candidate_chain(search_config)
        except SearchProviderError as exc:
            return SearchResponse(
                query=query,
                provider=exc.provider or "InvalidProvider",
                results=[],
                status=exc.status,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code=exc.error_code,
                error_message=exc.message,
            )

        active_chain: List[str] = []
        for engine in raw_chain:
            if cls.is_engine_operable(engine, search_config):
                active_chain.append(engine)

        if not active_chain:
            primary_name = raw_chain[0] if raw_chain else "None"
            return SearchResponse(
                query=query,
                provider=primary_name,
                results=[],
                status=ProviderStatus.CONFIGURATION_ERROR,
                request_count=0,
                estimated_cost_usd=0.0,
                cost_basis=CostBasis.NOT_BILLABLE_LOCAL,
                is_success=False,
                error_code="no_operable_provider",
                error_message=(
                    f"Provider '{primary_name}' is not enabled or is missing required "
                    f"credentials, and no operable fallback providers are configured."
                ),
            )

        try:
            requested_budget = float(
                search_config.get("total_budget_seconds", cls.DEFAULT_TOTAL_BUDGET_SECONDS)
            )
        except (TypeError, ValueError):
            requested_budget = cls.DEFAULT_TOTAL_BUDGET_SECONDS
        total_budget = max(1.0, min(requested_budget, cls.MAX_TOTAL_BUDGET_SECONDS))

        try:
            min_budget = float(
                search_config.get("min_provider_budget_seconds", cls.MIN_PROVIDER_BUDGET_SECONDS)
            )
        except (TypeError, ValueError):
            min_budget = cls.MIN_PROVIDER_BUDGET_SECONDS
        min_budget = max(0.2, min(min_budget, total_budget))

        # Bounded retry controls: default to 0 retries to prevent unrequested
        # duplicate billable queries. If user sets max_retries > 0, only transient
        # errors (timeout/upstream/rate-limit) are retried within the time budget.
        try:
            max_retries = max(0, min(int(search_config.get("max_retries", 0)), 2))
        except (TypeError, ValueError):
            max_retries = 0

        try:
            retry_delay = max(0.0, min(float(search_config.get("retry_delay", 1.0)), 5.0))
        except (TypeError, ValueError):
            retry_delay = 1.0

        fallback_on_auth_error = bool(search_config.get("fallback_on_auth_error", False))

        start_monotonic = time.monotonic()
        deadline = start_monotonic + total_budget

        effective_lang = lang or str(search_config.get("lang", "en"))
        effective_country = country or str(search_config.get("country", "us"))

        total_requests = 0
        accumulated_cost = 0.0
        last_response: Optional[SearchResponse] = None

        for engine_name in active_chain:
            remaining = deadline - time.monotonic()
            if remaining < min_budget:
                logger.warning(
                    f"Search budget exhausted before attempting '{engine_name}'. "
                    f"Remaining: {remaining:.2f}s"
                )
                if last_response is None:
                    last_response = SearchResponse(
                        query=query,
                        provider=engine_name,
                        results=[],
                        status=ProviderStatus.TIMEOUT,
                        latency_ms=round((time.monotonic() - start_monotonic) * 1000.0, 2),
                        request_count=total_requests,
                        estimated_cost_usd=accumulated_cost,
                        cost_basis=CostBasis.UNKNOWN,
                        is_success=False,
                        error_code="budget_exhausted",
                        error_message=(
                            f"Search time budget ({total_budget:.1f}s) exhausted before "
                            f"provider '{engine_name}' could be called."
                        ),
                    )
                break

            provider = cls.create_provider(name=engine_name, config=search_config)

            for attempt in range(max_retries + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0.1:
                    last_response = SearchResponse(
                        query=query,
                        provider=engine_name,
                        results=[],
                        status=ProviderStatus.TIMEOUT,
                        latency_ms=round((time.monotonic() - start_monotonic) * 1000.0, 2),
                        request_count=total_requests,
                        estimated_cost_usd=accumulated_cost,
                        cost_basis=provider.default_cost_basis,
                        is_success=False,
                        error_code="timeout",
                        error_message=(
                            f"Provider '{engine_name}' exceeded the remaining search "
                            f"budget ({total_budget:.1f}s)."
                        ),
                    )
                    break

                # Clamp the adapter's internal HTTP timeout to fit inside remaining budget
                if hasattr(provider, "timeout_seconds"):
                    try:
                        current_timeout = float(getattr(provider, "timeout_seconds"))
                        setattr(
                            provider,
                            "timeout_seconds",
                            max(0.5, min(current_timeout, remaining)),
                        )
                    except (TypeError, ValueError):
                        pass

                call_start = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        provider.search(
                            query=query,
                            max_results=max_results,
                            lang=effective_lang,
                            country=effective_country,
                            **kwargs,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    elapsed_ms = round((time.perf_counter() - call_start) * 1000.0, 2)
                    total_requests += 1
                    last_response = SearchResponse(
                        query=query,
                        provider=provider.provider_name,
                        results=[],
                        status=ProviderStatus.TIMEOUT,
                        latency_ms=elapsed_ms,
                        request_count=total_requests,
                        estimated_cost_usd=accumulated_cost,
                        cost_basis=provider.default_cost_basis,
                        is_success=False,
                        error_code="timeout",
                        error_message=(
                            f"Provider '{provider.provider_name}' timed out after "
                            f"{remaining:.1f}s (global budget {total_budget:.1f}s)."
                        ),
                    )
                    break
                except SearchProviderError as exc:
                    elapsed_ms = round((time.perf_counter() - call_start) * 1000.0, 2)
                    total_requests += 1
                    last_response = SearchResponse(
                        query=query,
                        provider=provider.provider_name,
                        results=[],
                        status=exc.status,
                        latency_ms=elapsed_ms,
                        request_count=total_requests,
                        estimated_cost_usd=accumulated_cost,
                        cost_basis=provider.default_cost_basis,
                        is_success=False,
                        error_code=exc.error_code,
                        error_message=exc.message,
                    )
                except Exception as exc:
                    elapsed_ms = round((time.perf_counter() - call_start) * 1000.0, 2)
                    total_requests += 1
                    logger.error(f"Provider '{engine_name}' raised an exception: {exc}")
                    last_response = SearchResponse(
                        query=query,
                        provider=provider.provider_name,
                        results=[],
                        status=ProviderStatus.UPSTREAM_ERROR,
                        latency_ms=elapsed_ms,
                        request_count=total_requests,
                        estimated_cost_usd=accumulated_cost,
                        cost_basis=provider.default_cost_basis,
                        is_success=False,
                        error_code="upstream_exception",
                        error_message=str(exc),
                    )
                else:
                    total_requests += max(1, int(response.request_count or 1))
                    if response.estimated_cost_usd:
                        accumulated_cost += float(response.estimated_cost_usd)

                    response.request_count = total_requests
                    if response.estimated_cost_usd is not None:
                        response.estimated_cost_usd = round(accumulated_cost, 6)
                    last_response = response

                    if response.is_success and response.results:
                        seen_urls: Set[str] = set()
                        response.results = cls._deduplicate_results(
                            response.results,
                            seen_urls,
                        )
                        if response.results:
                            response.status = ProviderStatus.SUCCESS
                            return response

                # Check if same-provider retry is permitted and feasible
                if last_response is not None and attempt < max_retries:
                    retryable_statuses = {
                        ProviderStatus.TIMEOUT,
                        ProviderStatus.UPSTREAM_ERROR,
                        ProviderStatus.RATE_LIMITED,
                    }
                    if last_response.status in retryable_statuses:
                        remaining_after = deadline - time.monotonic()
                        if remaining_after > (retry_delay + min_budget):
                            if retry_delay > 0:
                                await asyncio.sleep(retry_delay)
                            continue
                break

            # Do not mask configuration or authentication errors with a silent
            # fallback unless the user explicitly opted into fallback_on_auth_error.
            if (
                last_response is not None
                and last_response.status
                in (
                    ProviderStatus.CONFIGURATION_ERROR,
                    ProviderStatus.AUTHENTICATION_ERROR,
                    ProviderStatus.PERMISSION_ERROR,
                )
                and not fallback_on_auth_error
            ):
                return last_response

        if last_response is not None:
            last_response.request_count = total_requests
            if last_response.estimated_cost_usd is not None:
                last_response.estimated_cost_usd = round(accumulated_cost, 6)
            return last_response

        return SearchResponse(
            query=query,
            provider="None",
            results=[],
            status=ProviderStatus.TIMEOUT,
            request_count=total_requests,
            estimated_cost_usd=round(accumulated_cost, 6),
            cost_basis=CostBasis.UNKNOWN,
            is_success=False,
            error_code="budget_exhausted",
            error_message=(
                "Search chain produced no response within the allotted time budget."
            ),
        )


__all__ = ["SearchProviderFactory"]
