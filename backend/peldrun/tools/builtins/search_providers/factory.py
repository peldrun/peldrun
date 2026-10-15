# backend/peldrun/tools/builtins/search_providers/factory.py
"""
Search Provider Factory and Orchestration Layer for PELDRUN.

Responsibilities:
- Authoritative registry of search provider capabilities and technical limits.
- Centralized calculation of effective max_results per provider:
  * Global default: search.default_max_results (default 5, range 1..20)
  * Optional per-provider ceiling: search.<provider>.max_results
  * Optional explicit tool call count: clamped to configured ceiling and capability limit
  * Read-only provider capability limit: max_results_limit
  * Application hard cap: MAX_RESULTS_HARD_CAP = 20
- Per-provider count recalculation inside the primary/failover execution loop.
- Explicit outcome classification (success / empty / retryable / terminal) so that
  a provider returning SearchResponse(is_success=False) triggers failover to the
  next provider instead of being returned to the caller as a successful outcome.
- Defensive output truncation so SearchResponse.results never exceeds effective_max_results.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Type

from peldrun.tools.builtins.search_providers import base as base_mod
from peldrun.tools.builtins.search_providers.base import (
    BaseSearchProvider,
    ProviderCapability,
    ProviderStatus,
    SearchProviderError,
    SearchResponse,
)

logger = logging.getLogger("peldrun.tools.search_providers.factory")

MAX_RESULTS_HARD_CAP: int = getattr(base_mod, "MAX_RESULTS_HARD_CAP", 20)
DEFAULT_MAX_RESULTS: int = getattr(base_mod, "DEFAULT_MAX_RESULTS", 5)
MIN_RESULTS_LIMIT: int = 1

# Canonical provider metadata and module mapping
_PROVIDER_SPECS: Dict[str, Dict[str, Any]] = {
    "duckduckgo": {
        "id": "duckduckgo",
        "name": "DuckDuckGo",
        "module": "peldrun.tools.builtins.search_providers.duckduckgo_provider",
        "class_candidates": ("DuckDuckGoProvider", "DuckDuckGoSearchProvider"),
        "max_results_limit": 20,
        "requires_api_key": False,
        "supports_region": True,
        "supports_language": True,
        "supports_safe_search": True,
        "cost_model": "free",
        "limit_type": "peldrun_cap",
        "notes": "DDGS exposes a max_results argument; actual output can be lower.",
    },
    "searxng": {
        "id": "searxng",
        "id_aliases": ("searx",),
        "name": "SearXNG",
        "module": "peldrun.tools.builtins.search_providers.searxng_provider",
        "class_candidates": ("SearXNGProvider", "SearXNGSearchProvider", "SearxngProvider"),
        "max_results_limit": 20,
        "requires_api_key": False,
        "supports_region": False,
        "supports_language": True,
        "supports_safe_search": True,
        "cost_model": "self_hosted",
        "limit_type": "parse_output_cap",
        "notes": "PELDRUN parse/output cap; instance and upstream engines determine returned count.",
    },
    "tavily": {
        "id": "tavily",
        "name": "Tavily",
        "module": "peldrun.tools.builtins.search_providers.tavily_provider",
        "class_candidates": ("TavilyProvider", "TavilySearchProvider"),
        "max_results_limit": 20,
        "requires_api_key": True,
        "supports_region": False,
        "supports_language": False,
        "supports_safe_search": False,
        "cost_model": "credit_based",
        "limit_type": "upstream_and_peldrun_cap",
        "notes": "Up to 20 results per request; usage and cost depend on search depth.",
    },
    "brave": {
        "id": "brave",
        "name": "Brave",
        "module": "peldrun.tools.builtins.search_providers.brave_provider",
        "class_candidates": ("BraveProvider", "BraveSearchProvider"),
        "max_results_limit": 20,
        "requires_api_key": True,
        "supports_region": True,
        "supports_language": True,
        "supports_safe_search": True,
        "cost_model": "paid_api",
        "limit_type": "upstream_api_limit",
        "notes": "Official Brave Web Search API count range is 1-20; delivered results may be fewer.",
    },
    "google": {
        "id": "google",
        "id_aliases": ("google_cse", "googlecse"),
        "name": "Google",
        "module": "peldrun.tools.builtins.search_providers.google_provider",
        "class_candidates": ("GoogleProvider", "GoogleSearchProvider", "GoogleCSEProvider"),
        "max_results_limit": 10,
        "requires_api_key": True,
        "supports_region": True,
        "supports_language": True,
        "supports_safe_search": True,
        "cost_model": "paid_api",
        "limit_type": "upstream_api_limit",
        "notes": "Google Custom Search JSON API num parameter is strictly constrained to 1-10.",
    },
    "jina": {
        "id": "jina",
        "id_aliases": ("jina_ai", "jinaai"),
        "name": "Jina",
        "module": "peldrun.tools.builtins.search_providers.jina_provider",
        "class_candidates": ("JinaProvider", "JinaSearchProvider"),
        "max_results_limit": 20,
        "requires_api_key": False,
        "supports_region": True,
        "supports_language": True,
        "supports_safe_search": False,
        "cost_model": "freemium_token",
        "limit_type": "peldrun_cap",
        "notes": "PELDRUN adapter cap of 20 for s.jina.ai search requests.",
    },
    "yandex": {
        "id": "yandex",
        "id_aliases": ("yandex_cloud", "yandex_search"),
        "name": "Yandex",
        "module": "peldrun.tools.builtins.search_providers.yandex_provider",
        "class_candidates": ("YandexProvider", "YandexSearchProvider"),
        "max_results_limit": 15,
        "requires_api_key": True,
        "supports_region": True,
        "supports_language": True,
        "supports_safe_search": True,
        "cost_model": "paid_api",
        "limit_type": "peldrun_cap",
        "notes": "Current PELDRUN integration cap is 15 results per query.",
    },
}

_ALIAS_TO_KEY: Dict[str, str] = {}
for _key, _spec in _PROVIDER_SPECS.items():
    _ALIAS_TO_KEY[_key] = _key
    _ALIAS_TO_KEY[_spec["name"].lower()] = _key
    for _alias in _spec.get("id_aliases", ()):
        _ALIAS_TO_KEY[str(_alias).lower()] = _key


# Outcome classifications produced by _classify_response_outcome()
_OUTCOME_SUCCESS = "success"
_OUTCOME_EMPTY = "empty"
_OUTCOME_FAILURE_RETRYABLE = "failure_retryable"
_OUTCOME_FAILURE_TERMINAL = "failure_terminal"

# Provider statuses that should not be retried on the same provider (credentials/permission/quota issues).
_TERMINAL_FAILURE_STATUSES: frozenset = frozenset(
    {
        ProviderStatus.CONFIGURATION_ERROR.value,
        ProviderStatus.AUTHENTICATION_ERROR.value,
        ProviderStatus.PERMISSION_ERROR.value,
        ProviderStatus.QUOTA_EXHAUSTED.value,
        ProviderStatus.DISABLED.value,
    }
)

# Provider statuses that warrant a same-provider retry before failing over.
_RETRYABLE_FAILURE_STATUSES: frozenset = frozenset(
    {
        ProviderStatus.RATE_LIMITED.value,
        ProviderStatus.TIMEOUT.value,
        ProviderStatus.UPSTREAM_ERROR.value,
        ProviderStatus.INVALID_RESPONSE.value,
    }
)


def _parse_strict_positive_int(
    value: Any,
    field_name: str,
    min_value: int = MIN_RESULTS_LIMIT,
    max_value: Optional[int] = None,
) -> int:
    """
    Validate that value is a strict positive integer within bounds.
    Rejects booleans, fractional floats, non-numeric strings, and <= 0 values.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}.")

    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field_name} must be a whole integer, got fractional float {value}.")
        parsed = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or (stripped[0] in "+-" and not stripped[1:].isdigit()) or (
            stripped[0] not in "+-" and not stripped.isdigit()
        ):
            raise ValueError(f"{field_name} must be a valid integer string, got {value!r}.")
        parsed = int(stripped)
    else:
        raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}.")

    if parsed < min_value:
        if max_value is not None:
            raise ValueError(f"{field_name} must be between {min_value} and {max_value}, got {parsed}.")
        raise ValueError(f"{field_name} must be >= {min_value}, got {parsed}.")

    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be between {min_value} and {max_value}, got {parsed}.")

    return parsed


class SearchProviderFactory:
    """Central factory for search provider instantiation, capability authority, and failover execution."""

    _provider_class_cache: Dict[str, Type[BaseSearchProvider]] = {}

    # ------------------------------------------------------------------
    # Provider registry / capability accessors
    # ------------------------------------------------------------------
    @classmethod
    def normalize_provider_key(cls, provider_name: str) -> str:
        """Normalize any provider display name or alias to its canonical lowercase key."""
        if not provider_name or not isinstance(provider_name, str):
            raise ValueError("Provider name must be a non-empty string.")
        cleaned = provider_name.strip().lower()
        if cleaned not in _ALIAS_TO_KEY:
            supported = ", ".join(spec["name"] for spec in _PROVIDER_SPECS.values())
            raise ValueError(f"Unsupported search provider {provider_name!r}. Supported providers: {supported}.")
        return _ALIAS_TO_KEY[cleaned]

    @classmethod
    def get_canonical_display_name(cls, provider_name: str) -> str:
        """Return canonical display name (e.g., 'DuckDuckGo', 'Google', 'SearXNG')."""
        key = cls.normalize_provider_key(provider_name)
        return str(_PROVIDER_SPECS[key]["name"])

    @classmethod
    def list_provider_keys(cls) -> List[str]:
        """Return ordered list of canonical provider keys."""
        return list(_PROVIDER_SPECS.keys())

    @classmethod
    def _load_provider_class(cls, provider_key: str) -> Type[BaseSearchProvider]:
        """Dynamically import and cache the concrete provider implementation class."""
        key = cls.normalize_provider_key(provider_key)
        if key in cls._provider_class_cache:
            return cls._provider_class_cache[key]

        spec = _PROVIDER_SPECS[key]
        module = importlib.import_module(spec["module"])

        for candidate_name in spec["class_candidates"]:
            candidate = getattr(module, candidate_name, None)
            if inspect.isclass(candidate) and issubclass(candidate, BaseSearchProvider):
                cls._provider_class_cache[key] = candidate
                return candidate

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseSearchProvider) and obj is not BaseSearchProvider:
                cls._provider_class_cache[key] = obj
                return obj

        raise ImportError(
            f"No BaseSearchProvider subclass found in module {spec['module']} for provider {key!r}."
        )

    @classmethod
    def get_provider_capability(
        cls,
        provider_name: str,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> ProviderCapability:
        """
        Return the authoritative ProviderCapability descriptor for a given provider.
        Prefers the concrete provider's capability descriptor if defined, while enforcing
        the canonical max_results_limit and application hard cap.
        """
        key = cls.normalize_provider_key(provider_name)
        spec = _PROVIDER_SPECS[key]
        canonical_limit = min(int(spec["max_results_limit"]), MAX_RESULTS_HARD_CAP)

        provider_cls: Optional[Type[BaseSearchProvider]] = None
        try:
            provider_cls = cls._load_provider_class(key)
        except Exception as exc:
            logger.debug("Fallback to static capability spec for %s: %s", key, exc)

        if provider_cls is not None:
            for attr_name in ("get_capability", "capability", "CAPABILITY"):
                attr = getattr(provider_cls, attr_name, None)
                try:
                    cap_obj = attr() if callable(attr) else attr
                    if isinstance(cap_obj, ProviderCapability):
                        if hasattr(cap_obj, "max_results_limit"):
                            object.__setattr__(
                                cap_obj,
                                "max_results_limit",
                                min(int(cap_obj.max_results_limit), canonical_limit, MAX_RESULTS_HARD_CAP),
                            )
                        return cap_obj
                except Exception as exc:
                    logger.debug("Provider %s capability getter %s notice: %s", key, attr_name, exc)

            try:
                provider_cfg = cls._extract_provider_section(key, search_config)
                instance = cls._instantiate_provider(provider_cls, key, provider_cfg, search_config)
                if hasattr(instance, "get_capability") and callable(instance.get_capability):
                    cap_obj = instance.get_capability()
                    if isinstance(cap_obj, ProviderCapability):
                        return cap_obj
                if hasattr(instance, "capability") and isinstance(instance.capability, ProviderCapability):
                    return instance.capability
            except Exception as exc:
                logger.debug("Provider %s instance capability inspection notice: %s", key, exc)

        return cls._build_fallback_capability(key, spec, canonical_limit)

    @classmethod
    def _build_fallback_capability(
        cls,
        key: str,
        spec: Dict[str, Any],
        canonical_limit: int,
    ) -> ProviderCapability:
        """Construct a ProviderCapability compatible with base.ProviderCapability fields."""
        return ProviderCapability(
            id=key,
            config_key=key,
            display_name=spec["name"],
            tagline=spec.get("notes") or f"{spec['name']} search provider integration.",
            docs_url="",
            requires_credentials=bool(spec.get("requires_api_key", True)),
            supports_language=bool(spec.get("supports_language", True)),
            supports_country=bool(spec.get("supports_region", False)),
            supports_safesearch=bool(spec.get("supports_safe_search", False)),
            max_results_limit=canonical_limit,
        )

    # ------------------------------------------------------------------
    # Count resolution helpers
    # ------------------------------------------------------------------
    @classmethod
    def get_global_default_max_results(
        cls,
        search_config: Optional[Dict[str, Any]] = None,
        *,
        strict: bool = False,
    ) -> int:
        """
        Resolve global search.default_max_results (defaults to 5, range 1..20).
        When strict=True, raises ValueError if an invalid value is present in search_config.
        """
        cfg = cls._unwrap_search_section(search_config)
        if "default_max_results" not in cfg or cfg.get("default_max_results") is None:
            return DEFAULT_MAX_RESULTS

        raw_val = cfg.get("default_max_results")
        try:
            parsed = _parse_strict_positive_int(
                raw_val,
                "search.default_max_results",
                min_value=MIN_RESULTS_LIMIT,
                max_value=MAX_RESULTS_HARD_CAP if strict else None,
            )
            return min(parsed, MAX_RESULTS_HARD_CAP)
        except ValueError:
            if strict:
                raise
            logger.warning(
                "Invalid search.default_max_results=%r in config; falling back to default %d.",
                raw_val,
                DEFAULT_MAX_RESULTS,
            )
            return DEFAULT_MAX_RESULTS

    @classmethod
    def get_configured_provider_max_results(
        cls,
        provider_name: str,
        search_config: Optional[Dict[str, Any]] = None,
        *,
        strict: bool = False,
    ) -> Optional[int]:
        """
        Return the explicit per-provider max_results override from [search.<provider>],
        or None if no provider-specific override is configured (meaning it inherits global default).
        Do NOT treat 0 as None: 0 is an invalid configuration value.
        """
        key = cls.normalize_provider_key(provider_name)
        cap = cls.get_provider_capability(key, search_config)
        provider_limit = min(int(getattr(cap, "max_results_limit", MAX_RESULTS_HARD_CAP)), MAX_RESULTS_HARD_CAP)

        provider_cfg = cls._extract_provider_section(key, search_config)
        if "max_results" not in provider_cfg or provider_cfg.get("max_results") is None:
            return None

        raw_val = provider_cfg.get("max_results")
        try:
            parsed = _parse_strict_positive_int(
                raw_val,
                f"search.{key}.max_results",
                min_value=MIN_RESULTS_LIMIT,
                max_value=provider_limit if strict else None,
            )
            return min(parsed, provider_limit, MAX_RESULTS_HARD_CAP)
        except ValueError:
            if strict:
                raise
            raise ValueError(
                f"{cls.get_canonical_display_name(key)} max_results must be an integer between "
                f"{MIN_RESULTS_LIMIT} and {provider_limit}, got {raw_val!r}."
            )

    @classmethod
    def resolve_effective_max_results(
        cls,
        provider_name: str,
        requested: Optional[int] = None,
        search_config: Optional[Dict[str, Any]] = None,
        *,
        strict_config: bool = False,
    ) -> int:
        """
        Calculate the authoritative effective max_results for a specific provider according to
        Section 4.2 of the Search Results Configuration Engineering Plan:

            provider_configured_count = provider_config.get("max_results")
            provider_ceiling = (
                provider_configured_count
                if provider_configured_count is not None
                else global_default
            )
            requested_count = (
                explicit_tool_count
                if explicit_tool_count is not None
                else provider_ceiling
            )
            effective_count = min(
                requested_count,
                provider_ceiling,
                provider_capability.max_results_limit,
                MAX_RESULTS_HARD_CAP,
            )
        """
        key = cls.normalize_provider_key(provider_name)
        cap = cls.get_provider_capability(key, search_config)
        provider_limit = min(int(getattr(cap, "max_results_limit", MAX_RESULTS_HARD_CAP)), MAX_RESULTS_HARD_CAP)

        global_default = cls.get_global_default_max_results(search_config, strict=strict_config)
        provider_configured_count = cls.get_configured_provider_max_results(
            key,
            search_config,
            strict=strict_config,
        )

        provider_ceiling = (
            provider_configured_count
            if provider_configured_count is not None
            else global_default
        )

        if requested is not None:
            requested_count = _parse_strict_positive_int(
                requested,
                "max_results",
                min_value=MIN_RESULTS_LIMIT,
            )
        else:
            requested_count = provider_ceiling

        effective_count = min(
            requested_count,
            provider_ceiling,
            provider_limit,
            MAX_RESULTS_HARD_CAP,
        )
        return max(MIN_RESULTS_LIMIT, effective_count)

    @classmethod
    def resolve_count_Audit_details(
        cls,
        provider_name: str,
        requested: Optional[int] = None,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Return full resolution breakdown for diagnostics, telemetry, and API responses.
        Includes requested_max_results, configured_max_results (None if inherited),
        global_default_max_results, provider_ceiling, max_results_limit, and effective_max_results.
        """
        key = cls.normalize_provider_key(provider_name)
        cap = cls.get_provider_capability(key, search_config)
        provider_limit = min(int(getattr(cap, "max_results_limit", MAX_RESULTS_HARD_CAP)), MAX_RESULTS_HARD_CAP)
        global_default = cls.get_global_default_max_results(search_config)
        configured_override = cls.get_configured_provider_max_results(key, search_config)
        provider_ceiling = configured_override if configured_override is not None else min(global_default, provider_limit)
        effective = cls.resolve_effective_max_results(key, requested=requested, search_config=search_config)

        return {
            "provider": cls.get_canonical_display_name(key),
            "provider_key": key,
            "requested_max_results": requested,
            "configured_max_results": configured_override,
            "inherits_global_default": configured_override is None,
            "global_default_max_results": global_default,
            "provider_ceiling": min(provider_ceiling, provider_limit, MAX_RESULTS_HARD_CAP),
            "max_results_limit": provider_limit,
            "effective_max_results": effective,
            "hard_cap": MAX_RESULTS_HARD_CAP,
        }

    @classmethod
    def get_provider_capabilities(
        cls,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Return a dictionary mapping canonical provider keys to their serialized capability metadata,
        enriched with configured_max_results (None when inheriting) and effective_max_results.
        """
        capabilities: Dict[str, Dict[str, Any]] = {}
        global_default = cls.get_global_default_max_results(search_config)

        for key, spec in _PROVIDER_SPECS.items():
            cap = cls.get_provider_capability(key, search_config)
            if hasattr(cap, "model_dump"):
                cap_dict = cap.model_dump()
            elif hasattr(cap, "dict"):
                cap_dict = cap.dict()
            elif hasattr(cap, "__dict__"):
                cap_dict = dict(vars(cap))
            else:
                cap_dict = {}

            provider_limit = min(
                int(cap_dict.get("max_results_limit") or spec["max_results_limit"]),
                MAX_RESULTS_HARD_CAP,
            )

            configured_override: Optional[int] = None
            try:
                configured_override = cls.get_configured_provider_max_results(key, search_config)
            except ValueError:
                configured_override = None

            effective_default = min(
                configured_override if configured_override is not None else global_default,
                provider_limit,
                MAX_RESULTS_HARD_CAP,
            )

            cap_dict.update(
                {
                    "id": key,
                    "name": spec["name"],
                    "max_results_limit": provider_limit,
                    "configured_max_results": configured_override,
                    "effective_max_results": effective_default,
                    "inherits_global_default": configured_override is None,
                    "default_max_results": global_default,
                    "limit_type": cap_dict.get("limit_type") or spec["limit_type"],
                    "notes": cap_dict.get("notes") or spec["notes"],
                }
            )
            capabilities[key] = cap_dict

        return capabilities

    # ------------------------------------------------------------------
    # Config unwrapping / provider instantiation
    # ------------------------------------------------------------------
    @classmethod
    def _unwrap_search_section(cls, search_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract the [search] table whether passed a full config dict or the [search] dict directly."""
        if not isinstance(search_config, dict):
            return {}
        if "search" in search_config and isinstance(search_config["search"], dict):
            if "engine" in search_config["search"] or "default_max_results" in search_config["search"] or any(
                k in search_config["search"] for k in _PROVIDER_SPECS
            ):
                return dict(search_config["search"])
        return dict(search_config)

    @classmethod
    def _extract_provider_section(
        cls,
        provider_key: str,
        search_config: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Extract the [search.<provider>] sub-table case-insensitively."""
        key = cls.normalize_provider_key(provider_key)
        cfg = cls._unwrap_search_section(search_config)

        direct = cfg.get(key)
        if isinstance(direct, dict):
            return dict(direct)

        display_name = _PROVIDER_SPECS[key]["name"]
        by_display = cfg.get(display_name)
        if isinstance(by_display, dict):
            return dict(by_display)

        for k, v in cfg.items():
            if isinstance(k, str) and k.strip().lower() == key and isinstance(v, dict):
                return dict(v)

        return {}

    @classmethod
    def _instantiate_provider(
        cls,
        provider_cls: Type[BaseSearchProvider],
        provider_key: str,
        provider_cfg: Dict[str, Any],
        search_config: Optional[Dict[str, Any]] = None,
    ) -> BaseSearchProvider:
        """Instantiate a provider class safely regardless of its constructor signature."""
        full_search_cfg = cls._unwrap_search_section(search_config)
        merged_cfg = dict(provider_cfg)
        for global_key in ("lang", "country", "privacy_mode", "total_budget_seconds"):
            if global_key not in merged_cfg and global_key in full_search_cfg:
                merged_cfg[global_key] = full_search_cfg[global_key]

        try:
            sig = inspect.signature(provider_cls.__init__)
            params = sig.parameters
            if "config" in params and "search_config" in params:
                return provider_cls(config=merged_cfg, search_config=full_search_cfg)  # type: ignore[call-arg]
            if "config" in params:
                return provider_cls(config=merged_cfg)  # type: ignore[call-arg]
            if "provider_config" in params:
                return provider_cls(provider_config=merged_cfg)  # type: ignore[call-arg]
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return provider_cls(**merged_cfg)
            filtered = {k: v for k, v in merged_cfg.items() if k in params and k != "self"}
            if filtered:
                return provider_cls(**filtered)
            return provider_cls()
        except TypeError:
            try:
                return provider_cls(merged_cfg)  # type: ignore[call-arg]
            except TypeError:
                return provider_cls()

    @classmethod
    def create_provider(
        cls,
        provider_name: str,
        search_config: Optional[Dict[str, Any]] = None,
    ) -> BaseSearchProvider:
        """Create and configure a concrete BaseSearchProvider instance by provider name."""
        key = cls.normalize_provider_key(provider_name)
        provider_cls = cls._load_provider_class(key)
        provider_cfg = cls._extract_provider_section(key, search_config)
        return cls._instantiate_provider(provider_cls, key, provider_cfg, search_config)

    # Alias for backward compatibility
    get_provider = create_provider

    @classmethod
    def build_execution_chain(
        cls,
        search_config: Optional[Dict[str, Any]] = None,
        override_provider: Optional[str] = None,
    ) -> List[str]:
        """
        Build the ordered list of canonical provider keys to attempt.
        If override_provider is explicitly specified (e.g. diagnostic test), only that provider runs.
        Otherwise uses primary `engine` followed by `fallback_engines`, skipping explicitly disabled providers.
        """
        cfg = cls._unwrap_search_section(search_config)

        if override_provider:
            return [cls.normalize_provider_key(override_provider)]

        primary_raw = cfg.get("engine") or "DuckDuckGo"
        primary_key = cls.normalize_provider_key(str(primary_raw))

        fallbacks_raw = cfg.get("fallback_engines") or []
        if isinstance(fallbacks_raw, str):
            fallbacks_raw = [item.strip() for item in fallbacks_raw.split(",") if item.strip()]

        chain: List[str] = []
        candidates = [primary_key]
        for fb in fallbacks_raw:
            if fb:
                candidates.append(cls.normalize_provider_key(str(fb)))

        for cand_key in candidates:
            if cand_key in chain:
                continue
            prov_section = cls._extract_provider_section(cand_key, cfg)
            # Primary engine is always included so that the caller can surface
            # an explicit "primary disabled" error when there are no fallbacks.
            # Fallback engines are included unless explicitly disabled.
            if "enabled" in prov_section and prov_section["enabled"] is False and cand_key != primary_key:
                logger.debug("Skipping disabled fallback search provider: %s", cand_key)
                continue
            chain.append(cand_key)

        return chain

    # ------------------------------------------------------------------
    # Response outcome classification (P1 fix)
    # ------------------------------------------------------------------
    @classmethod
    def _classify_response_outcome(
        cls,
        response: SearchResponse,
    ) -> Tuple[str, bool]:
        """
        Classify a provider SearchResponse to decide whether the orchestrator should
        accept it, failover to the next provider, or retry the same provider.

        Returns:
            (outcome, should_failover) where outcome is one of:
              - _OUTCOME_SUCCESS           : usable results were returned.
              - _OUTCOME_EMPTY             : valid empty result set; failover may find results.
              - _OUTCOME_FAILURE_RETRYABLE : transient failure; retry then failover.
              - _OUTCOME_FAILURE_TERMINAL  : non-retryable failure; failover immediately.
        """
        results = getattr(response, "results", None) or []
        is_success = bool(getattr(response, "is_success", False))
        status = getattr(response, "status", None)

        # Direct success: results present and provider marked success.
        if results and is_success:
            return (_OUTCOME_SUCCESS, False)

        status_val = (
            status.value if hasattr(status, "value") else str(status or "").strip().lower()
        )

        # Contract-violation safeguard: SUCCESS status but zero results.
        if status_val == ProviderStatus.SUCCESS.value and not results:
            return (_OUTCOME_EMPTY, True)

        if status_val == ProviderStatus.EMPTY.value:
            return (_OUTCOME_EMPTY, True)

        if status_val in _TERMINAL_FAILURE_STATUSES:
            return (_OUTCOME_FAILURE_TERMINAL, True)

        if status_val in _RETRYABLE_FAILURE_STATUSES:
            return (_OUTCOME_FAILURE_RETRYABLE, True)

        # Defensive default: unknown status with no usable results → retryable failure.
        return (_OUTCOME_FAILURE_RETRYABLE, True)

    # ------------------------------------------------------------------
    # Primary failover-aware execution loop
    # ------------------------------------------------------------------
    @classmethod
    async def search(
        cls,
        query: str,
        max_results: Optional[int] = None,
        search_config: Optional[Dict[str, Any]] = None,
        *,
        override_provider: Optional[str] = None,
        lang: Optional[str] = None,
        country: Optional[str] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute a search query across the configured provider chain with per-provider
        effective max_results calculation, retry policy, time budget enforcement,
        explicit outcome classification, and defensive result count truncation.

        Failover semantics (P1 fix):
          - A provider response with is_success=False triggers evaluation instead of being
            returned directly to the caller.
          - EMPTY outcomes allow failover to seek non-empty results from the next provider.
          - Retryable failures (rate limit, timeout, upstream error) retry the same provider
            up to `max_retries` before moving on.
          - Terminal failures (config/auth/permission/quota) failover immediately.
          - If any provider completed the search successfully but returned zero results,
            that valid EMPTY response is returned rather than raising a "all providers failed" error.
        """
        clean_query = (query or "").strip()
        if not clean_query:
            raise SearchProviderError("Search query cannot be empty.")

        cfg = cls._unwrap_search_section(search_config)
        if cfg.get("enabled") is False:
            raise SearchProviderError("External web search is disabled by PELDRUN Search Settings.")

        # Validate configuration result limits upfront so invalid config surfaces clearly
        cls.get_global_default_max_results(cfg, strict=True)

        chain = cls.build_execution_chain(cfg, override_provider=override_provider)
        if not chain:
            raise SearchProviderError("No active search providers available in execution chain.")

        resolved_lang = lang if lang is not None else str(cfg.get("lang") or "en")
        resolved_country = country if country is not None else str(cfg.get("country") or "us")
        privacy_mode = bool(cfg.get("privacy_mode", False))

        total_budget = float(timeout if timeout is not None else (cfg.get("total_budget_seconds") or 22.0))
        max_retries = max(0, int(cfg.get("max_retries") or 0))
        retry_delay = max(0.0, float(cfg.get("retry_delay") or 1.0))

        deadline = time.monotonic() + total_budget
        errors: List[Tuple[str, str]] = []
        last_empty_response: Optional[SearchResponse] = None

        print(
            f"\n[SEARCH-FACTORY] Query: {clean_query!r}\n"
            f"[SEARCH-FACTORY] Chain: {' → '.join(chain)}\n"
            f"[SEARCH-FACTORY] Requested: {max_results} | Budget: {total_budget}s | Retries: {max_retries}",
            flush=True,
        )

        for provider_index, provider_key in enumerate(chain):
            display_name = cls.get_canonical_display_name(provider_key)
            prov_section = cls._extract_provider_section(provider_key, cfg)

            # --- Disabled-provider handling (P1 fix) ---------------------------------
            # override_provider bypasses the disabled flag (explicit diagnostic/test intent).
            # Otherwise, skip disabled providers; if the primary is disabled and there is
            # no fallback available, raise a clear, deterministic error.
            if prov_section.get("enabled") is False and override_provider is None:
                if len(chain) == 1:
                    raise SearchProviderError(
                        f"Search provider '{display_name}' is disabled in PELDRUN Search Settings "
                        f"and no fallback providers are configured."
                    )
                errors.append((display_name, f"Provider '{display_name}' is disabled in search settings."))
                logger.info("Skipping explicitly disabled search provider: %s", display_name)
                continue

            # Compute the authoritative effective max_results specifically for THIS provider
            audit = cls.resolve_count_Audit_details(
                provider_key,
                requested=max_results,
                search_config=cfg,
            )
            effective_count = int(audit["effective_max_results"])
            provider_limit = int(audit["max_results_limit"])

            provider = cls.create_provider(provider_key, cfg)

            for attempt in range(max_retries + 1):
                remaining_budget = deadline - time.monotonic()
                if remaining_budget <= 0:
                    errors.append((display_name, "Total search time budget exhausted."))
                    break

                prov_timeout = float(prov_section.get("timeout") or remaining_budget)
                attempt_timeout = max(0.5, min(remaining_budget, prov_timeout))

                print(
                    f"[SEARCH-FACTORY] → Attempt #{provider_index + 1}.{attempt + 1}: "
                    f"provider={display_name} | effective_max={effective_count} | "
                    f"limit={provider_limit} | timeout≈{attempt_timeout:.1f}s",
                    flush=True,
                )

                # ---- Invoke provider ------------------------------------------------
                try:
                    response = await cls._invoke_provider_search(
                        provider=provider,
                        query=clean_query,
                        effective_count=effective_count,
                        lang=resolved_lang,
                        country=resolved_country,
                        privacy_mode=privacy_mode,
                        timeout=attempt_timeout,
                        extra_kwargs=kwargs,
                    )
                except Exception as exc:
                    err_text = str(exc) or exc.__class__.__name__
                    print(
                        f"[SEARCH-FACTORY] ✗ {display_name} raised exception "
                        f"(attempt {attempt + 1}/{max_retries + 1}): {err_text[:200]}",
                        flush=True,
                    )
                    logger.warning(
                        "Search provider %s raised (attempt %d/%d, effective_max_results=%d): %s",
                        display_name,
                        attempt + 1,
                        max_retries + 1,
                        effective_count,
                        err_text,
                    )
                    if attempt < max_retries and (deadline - time.monotonic()) > retry_delay:
                        await asyncio.sleep(retry_delay)
                        continue
                    errors.append((display_name, err_text))
                    break

                # ---- Defensive result-count truncation ------------------------------
                if hasattr(response, "results") and isinstance(response.results, list):
                    if len(response.results) > effective_count:
                        response.results = list(response.results[:effective_count])

                # ---- Traceability metadata -----------------------------------------
                cls._attach_count_metadata(
                    response=response,
                    provider_name=display_name,
                    provider_key=provider_key,
                    requested_max_results=max_results,
                    effective_max_results=effective_count,
                    configured_max_results=audit["configured_max_results"],
                    max_results_limit=provider_limit,
                    fallback_used=(provider_index > 0),
                    attempt_number=(attempt + 1),
                )

                # ---- Explicit outcome classification (P1 fix) -----------------------
                outcome, should_failover = cls._classify_response_outcome(response)

                if outcome == _OUTCOME_SUCCESS:
                    print(
                        f"[SEARCH-FACTORY] ✓ {display_name} returned "
                        f"{len(response.results)} results in "
                        f"{getattr(response, 'latency_ms', 0):.0f}ms "
                        f"| status={getattr(response.status, 'value', response.status)}",
                        flush=True,
                    )
                    return response

                status_obj = getattr(response, "status", None)
                status_val = (
                    status_obj.value if hasattr(status_obj, "value") else str(status_obj or "unknown")
                )
                err_msg = (
                    getattr(response, "error_message", None)
                    or f"Provider '{display_name}' returned status '{status_val}' with no usable results."
                )

                print(
                    f"[SEARCH-FACTORY] ⚠ {display_name} non-success outcome: "
                    f"{outcome} (status={status_val}) — "
                    f"{'failing over' if should_failover else 'accepting'}",
                    flush=True,
                )
                logger.warning(
                    "Search provider %s non-success outcome=%s status=%s "
                    "(attempt %d/%d, effective_max_results=%d): %s",
                    display_name,
                    outcome,
                    status_val,
                    attempt + 1,
                    max_retries + 1,
                    effective_count,
                    err_msg,
                )

                # Track the most recent valid EMPTY response so we can return it
                # if every remaining provider fails outright.
                if outcome == _OUTCOME_EMPTY:
                    last_empty_response = response

                errors.append((display_name, err_msg))

                # EMPTY and TERMINAL failures do not retry the same provider.
                if outcome in (_OUTCOME_EMPTY, _OUTCOME_FAILURE_TERMINAL):
                    break

                # RETRYABLE failure: retry same provider if budget and retry budget allow.
                if attempt < max_retries and (deadline - time.monotonic()) > retry_delay:
                    await asyncio.sleep(retry_delay)
                    continue
                break

        # ---- All providers exhausted -------------------------------------------
        if last_empty_response is not None:
            print(
                "[SEARCH-FACTORY] ⚠ All providers exhausted; returning valid EMPTY result "
                f"from {last_empty_response.provider}.",
                flush=True,
            )
            return last_empty_response

        summary = "; ".join(f"{prov}: {msg}" for prov, msg in errors) or "Unknown provider failure."
        print(
            f"[SEARCH-FACTORY] ✗✗ ALL PROVIDERS FAILED\n"
            f"[SEARCH-FACTORY] {summary}",
            flush=True,
        )
        raise SearchProviderError(f"All configured search providers failed. Details: {summary}")

    # Aliases to support all runtime and test callers
    execute_search = search
    search_with_failover = search

    # ------------------------------------------------------------------
    # Provider invocation adapter
    # ------------------------------------------------------------------
    @classmethod
    async def _invoke_provider_search(
        cls,
        provider: BaseSearchProvider,
        query: str,
        effective_count: int,
        lang: str,
        country: str,
        privacy_mode: bool,
        timeout: float,
        extra_kwargs: Dict[str, Any],
    ) -> SearchResponse:
        """Invoke provider.search() adapting to the concrete provider's accepted parameters."""
        search_fn = provider.search
        sig = inspect.signature(search_fn)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

        call_kwargs: Dict[str, Any] = {}
        candidate_args = {
            "query": query,
            "max_results": effective_count,
            "lang": lang,
            "language": lang,
            "country": country,
            "region": country,
            "privacy_mode": privacy_mode,
            "timeout": timeout,
            **extra_kwargs,
        }

        for k, v in candidate_args.items():
            if k in params or accepts_var_kw:
                # Avoid passing duplicate aliases unless explicitly declared in signature
                if k in ("language", "region") and k not in params:
                    continue
                call_kwargs[k] = v

        if "query" not in call_kwargs:
            return await asyncio.wait_for(search_fn(query, max_results=effective_count), timeout=timeout + 1.0)

        return await asyncio.wait_for(search_fn(**call_kwargs), timeout=timeout + 1.0)

    # ------------------------------------------------------------------
    # Count metadata attachment
    # ------------------------------------------------------------------
    @classmethod
    def _attach_count_metadata(
        cls,
        response: SearchResponse,
        provider_name: str,
        provider_key: str,
        requested_max_results: Optional[int],
        effective_max_results: int,
        configured_max_results: Optional[int],
        max_results_limit: int,
        fallback_used: bool,
        attempt_number: int,
    ) -> None:
        """Enrich SearchResponse with count resolution metadata without breaking strict models."""
        trace_meta = {
            "provider": provider_name,
            "provider_key": provider_key,
            "requested_max_results": requested_max_results,
            "effective_max_results": effective_max_results,
            "configured_max_results": configured_max_results,
            "max_results_limit": max_results_limit,
            "fallback_used": fallback_used,
            "attempt": attempt_number,
        }

        if hasattr(response, "metadata") and isinstance(getattr(response, "metadata"), dict):
            response.metadata.update(trace_meta)
        else:
            try:
                setattr(response, "metadata", trace_meta)
            except Exception:
                pass

        for attr_key, attr_val in (
            ("requested_max_results", requested_max_results),
            ("effective_max_results", effective_max_results),
            ("configured_max_results", configured_max_results),
            ("max_results_limit", max_results_limit),
        ):
            try:
                setattr(response, attr_key, attr_val)
            except Exception:
                pass


__all__ = [
    "DEFAULT_MAX_RESULTS",
    "MAX_RESULTS_HARD_CAP",
    "MIN_RESULTS_LIMIT",
    "SearchProviderFactory",
]
