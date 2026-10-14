"""
backend/peldrun/tools/builtins/web_search.py

PELDRUN Core Web Search Orchestrator Tool.
Queries enabled internet search engines through SearchProviderFactory
using one authoritative configuration path, non-blocking telemetry, and
a per-run/cooldown circuit breaker so small local LLMs cannot loop on
repeated failing web_search calls within the same job.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.builtins.search_providers.base import (
    ConfigurationError,
    CostBasis,
    MAX_QUERY_CHARS,
    ProviderStatus,
)
from peldrun.tools.builtins.search_providers.factory import SearchProviderFactory
from peldrun.tools.contract import ToolExecutionPolicy

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

logger = logging.getLogger("peldrun.tools.builtins.web_search")


def resolve_authoritative_config_path() -> Path:
    """
    Resolve the single authoritative config.toml path shared between
    the core runtime and the web Settings API.
    """
    env_override = os.environ.get("PELDRUN_CONFIG_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()

    repo_root = Path(__file__).resolve().parents[4]
    return (repo_root / "config" / "config.toml").resolve()


class WebSearchArgs(BaseModel):
    query: str = Field(
        ...,
        description="The search terms or keyword query.",
    )
    max_results: Optional[int] = Field(
        default=5,
        ge=1,
        le=15,
        description="Maximum snippet results to retrieve.",
    )


class WebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = (
        "Search the live web for recent information, documentation, news, or "
        "technical queries. Returns untrusted external web citations and links. "
        "IMPORTANT: If this tool returns an error or zero results, DO NOT retry "
        "the same or a similar query repeatedly. Report the outcome to the user "
        "after ONE attempt and proceed with your existing knowledge."
    )
    args_schema = WebSearchArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=25.0,
        retryable=False,
        idempotent=True,
        side_effects=False,
    )

    # Per-run and short-window circuit breaker to stop small local LLMs from
    # calling `web_search` 2-3 times in a row after all providers have failed.
    _FAILED_RUN_CACHE: Dict[str, Tuple[float, str]] = {}
    _LAST_GLOBAL_FAILURE: Optional[Tuple[float, str, str]] = None
    _CIRCUIT_COOLDOWN_SECONDS: float = 20.0

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        config_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            execution_policy=self.execution_policy,
        )
        self._explicit_config_path: Optional[Path] = (
            Path(config_path).resolve() if config_path else None
        )

    @classmethod
    def reset_circuit_breaker(cls) -> None:
        """Clear cached failure states (used by tests or when settings change)."""
        cls._FAILED_RUN_CACHE.clear()
        cls._LAST_GLOBAL_FAILURE = None

    @classmethod
    def _check_circuit_breaker(
        cls,
        run_key: Optional[str],
        config_signature: str,
    ) -> Optional[str]:
        now = time.monotonic()
        # Clean expired entries
        expired = [
            k
            for k, (ts, _) in cls._FAILED_RUN_CACHE.items()
            if (now - ts) > cls._CIRCUIT_COOLDOWN_SECONDS
        ]
        for k in expired:
            cls._FAILED_RUN_CACHE.pop(k, None)

        if run_key and run_key in cls._FAILED_RUN_CACHE:
            _, reason = cls._FAILED_RUN_CACHE[run_key]
            return reason

        if cls._LAST_GLOBAL_FAILURE is not None:
            ts, saved_sig, reason = cls._LAST_GLOBAL_FAILURE
            if (
                saved_sig == config_signature
                and (now - ts) < cls._CIRCUIT_COOLDOWN_SECONDS
            ):
                return reason

        return None

    @classmethod
    def _trip_circuit_breaker(
        cls,
        run_key: Optional[str],
        config_signature: str,
        reason: str,
    ) -> None:
        now = time.monotonic()
        if run_key:
            cls._FAILED_RUN_CACHE[run_key] = (now, reason)
        cls._LAST_GLOBAL_FAILURE = (now, config_signature, reason)

    # ------------------------------------------------------------------
    # Authoritative Configuration Loading
    # ------------------------------------------------------------------
    def _load_live_search_config(self) -> Dict[str, Any]:
        """
        Load and parse the [search] table from the authoritative config.toml.
        Raises ConfigurationError if the file exists but contains malformed TOML.
        """
        target_file = self._explicit_config_path or resolve_authoritative_config_path()
        example_file = target_file.parent / "config.example.toml"

        selected_file: Optional[Path] = None
        if target_file.is_file():
            selected_file = target_file
        elif self._explicit_config_path is None and example_file.is_file():
            logger.info(
                f"Authoritative config not found at {target_file}; "
                f"using fallback template {example_file.name}."
            )
            selected_file = example_file

        if selected_file is not None:
            data = self._parse_toml_strict(selected_file)
            search_section = data.get("search")
            if isinstance(search_section, dict):
                return search_section
            return self._default_search_config()

        logger.info(
            f"No config.toml found at {target_file}; using built-in default search config."
        )
        return self._default_search_config()

    @staticmethod
    def _parse_toml_strict(target_file: Path) -> Dict[str, Any]:
        """
        Parse a TOML file strictly, stripping UTF-8 BOM if present.
        Raises ConfigurationError when the TOML syntax is invalid.
        """
        try:
            content_bytes = target_file.read_bytes()
            if content_bytes.startswith(b"\xef\xbb\xbf"):
                content_bytes = content_bytes[3:]
            parsed = tomllib.loads(content_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ConfigurationError(
                    f"Configuration file {target_file.name} did not parse to a TOML table."
                )
            return parsed
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"Malformed TOML in {target_file.name}: {exc}"
            ) from exc

    @staticmethod
    def _default_search_config() -> Dict[str, Any]:
        return {
            "enabled": True,
            "engine": "DuckDuckGo",
            "fallback_engines": [],
            "retry_delay": 2,
            "max_retries": 0,
            "lang": "en",
            "country": "us",
            "total_budget_seconds": 22.0,
        }

    # ------------------------------------------------------------------
    # Telemetry persistence helper
    # ------------------------------------------------------------------
    @staticmethod
    async def _persist_telemetry_async(
        query: str,
        provider: str,
        results_count: int,
        latency_ms: float,
        cost_usd: Optional[float],
        is_success: bool,
        error_message: Optional[str],
        session_id: Optional[str],
        run_id: Optional[str],
        status: ProviderStatus = ProviderStatus.SUCCESS,
        request_count: int = 1,
        usage_units: Optional[float] = None,
        cost_basis: CostBasis = CostBasis.UNKNOWN,
        privacy_mode: bool = False,
    ) -> None:
        """
        Record search telemetry in a worker thread so the event loop is
        never blocked by SQLite I/O. Logs diagnostics on failure without
        breaking search execution.
        """
        def _persist() -> None:
            try:
                from peldrun.runtime.search_usage_store import (
                    get_search_usage_store,
                )
                store = get_search_usage_store()
                sig = inspect.signature(store.record_search)
                call_kwargs: Dict[str, Any] = {
                    "query": query,
                    "provider": provider,
                    "results_count": results_count,
                    "latency_ms": latency_ms,
                    "cost_usd": float(cost_usd) if cost_usd is not None else 0.0,
                    "is_success": is_success,
                    "error_message": error_message,
                    "session_id": session_id,
                    "run_id": run_id,
                }
                if "status" in sig.parameters:
                    call_kwargs["status"] = status.value
                if "request_count" in sig.parameters:
                    call_kwargs["request_count"] = request_count
                if "usage_units" in sig.parameters:
                    call_kwargs["usage_units"] = usage_units
                if "cost_basis" in sig.parameters:
                    call_kwargs["cost_basis"] = cost_basis.value
                if "privacy_mode" in sig.parameters:
                    call_kwargs["privacy_mode"] = privacy_mode

                store.record_search(**call_kwargs)
            except Exception as exc:
                logger.warning(f"Failed to persist search telemetry: {exc}")

        try:
            await asyncio.to_thread(_persist)
        except Exception as exc:
            logger.debug(f"Telemetry worker dispatch failed: {exc}")

    # ------------------------------------------------------------------
    # Main execution entry point
    # ------------------------------------------------------------------
    async def _arun(
        self,
        query: str,
        max_results: Optional[int] = 5,
        **kwargs: Any,
    ) -> ToolResult:
        clean_q = (query or "").strip()

        if not clean_q:
            return ToolResult(
                output="Search query cannot be empty.",
                exit_code=1,
                is_error=True,
            )

        if len(clean_q) > MAX_QUERY_CHARS:
            clean_q = clean_q[:MAX_QUERY_CHARS].rstrip()

        try:
            requested = int(max_results or 5)
        except (TypeError, ValueError):
            requested = 5
        limit = min(max(requested, 1), 15)

        try:
            search_cfg = self._load_live_search_config()
        except ConfigurationError as exc:
            return ToolResult(
                output=(
                    f"⚠️ Web search configuration error: {exc.message}\n"
                    f"Fix the syntax in config/config.toml before running web search. "
                    f"IMPORTANT: Do NOT retry this search."
                ),
                exit_code=1,
                is_error=True,
            )

        if search_cfg.get("enabled") is False:
            return ToolResult(
                output=(
                    "Web search is administratively disabled in this workspace "
                    "(Air-Gapped Mode). Proceed without live search and inform "
                    "the user that live search is unavailable."
                ),
                exit_code=0,
                is_error=False,
            )

        run_key = str(
            kwargs.get("run_id")
            or kwargs.get("job_id")
            or kwargs.get("session_id")
            or ""
        ).strip() or None
        cfg_sig = f"{search_cfg.get('engine')}:{search_cfg.get('fallback_engines')}"

        cached_failure = self._check_circuit_breaker(run_key, cfg_sig)
        if cached_failure is not None:
            logger.info(
                "WebSearchTool circuit breaker blocked repeated search call "
                f"(query={clean_q!r}): {cached_failure}"
            )
            return ToolResult(
                output=(
                    f"⛔ Web search is temporarily unavailable in this run because "
                    f"the configured search provider chain already failed: {cached_failure}\n"
                    f"STOP CALLING 'web_search'. You MUST answer the user immediately "
                    f"using your existing knowledge and state that live web search "
                    f"is currently unreachable."
                ),
                exit_code=1,
                is_error=True,
            )

        try:
            response = await SearchProviderFactory.execute_orchestrated_search(
                query=clean_q,
                search_config=search_cfg,
                max_results=limit,
                lang=str(search_cfg.get("lang", "en")),
                country=str(search_cfg.get("country", "us")),
                **kwargs,
            )
        except Exception as exc:
            logger.error(f"Search orchestration raised an exception: {exc}")
            self._trip_circuit_breaker(run_key, cfg_sig, str(exc))
            return ToolResult(
                output=(
                    f"⚠️ Web search failed due to an internal error: {exc}\n"
                    f"IMPORTANT: Do NOT retry this search. Proceed with your "
                    f"existing knowledge or inform the user that live search "
                    f"is currently unavailable."
                ),
                exit_code=1,
                is_error=True,
            )

        await self._persist_telemetry_async(
            query=clean_q,
            provider=response.provider,
            results_count=len(response.results),
            latency_ms=response.latency_ms,
            cost_usd=response.estimated_cost_usd,
            is_success=response.is_success,
            error_message=response.error_message,
            session_id=kwargs.get("session_id"),
            run_id=kwargs.get("run_id"),
            status=response.status,
            request_count=response.request_count,
            usage_units=response.usage_units,
            cost_basis=response.cost_basis,
            privacy_mode=bool(search_cfg.get("privacy_mode", False)),
        )

        if response.is_success and response.results:
            self.reset_circuit_breaker()
            return ToolResult(
                output=response.format_markdown(),
                exit_code=0,
                is_error=False,
            )

        if response.status == ProviderStatus.EMPTY:
            return ToolResult(
                output=(
                    f"ℹ️ Web search completed via {response.provider} for query "
                    f"'{clean_q}', but returned zero matching results.\n"
                    f"Details: {response.error_message or 'No indexed matches found.'}\n"
                    f"IMPORTANT: Do NOT repeat the exact same search query."
                ),
                exit_code=1,
                is_error=True,
            )

        diagnostic = response.error_message or "Search provider request failed."
        # Trip circuit breaker on hard provider/network failures so the LLM
        # cannot stall the job with 2-3 identical timeout loops.
        self._trip_circuit_breaker(
            run_key,
            cfg_sig,
            f"{response.provider} ({response.status.value}): {diagnostic}",
        )
        return ToolResult(
            output=(
                f"⚠️ Web search failed ({response.status.value}).\n"
                f"Provider: {response.provider}\n"
                f"Query: '{clean_q}'\n"
                f"Reason: {diagnostic}\n"
                f"STOP CALLING 'web_search'. Proceed immediately with your "
                f"existing knowledge and inform the user that live search is unavailable."
            ),
            exit_code=1,
            is_error=True,
        )


__all__ = ["WebSearchArgs", "WebSearchTool", "resolve_authoritative_config_path"]
