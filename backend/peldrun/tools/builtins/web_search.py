"""
backend/peldrun/tools/builtins/web_search.py

PELDRUN Core Web Search Orchestrator Tool.
Queries enabled internet search engines through the provider factory
without deadlocking the event loop or triggering agent retry loops.

Fixes applied:
- Critical: exit_code=1 and is_error=True when the search fails,
  preventing the LLM from entering an infinite retry loop.
- Explicit failure message instructs the LLM not to retry the same query.
- Telemetry persistence runs in a worker thread (non-blocking).
- Removed hardcoded Windows paths from config discovery.
- Config file discovery order is now deterministic and portable.
- Improved tool description to warn the LLM about retry behavior.
- Query and result limit validation is centralized.
- Air-Gapped mode returns a clear success-shaped response (no error).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionPolicy
from peldrun.tools.builtins.search_providers.factory import SearchProviderFactory

logger = logging.getLogger("peldrun.tools.builtins.web_search")


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
        "technical queries. Returns verified citations and clean Markdown links. "
        "IMPORTANT: If this tool returns an error or no results, DO NOT retry "
        "the same or a similar query repeatedly. Report the failure to the user "
        "after ONE attempt and proceed with your existing knowledge."
    )
    args_schema = WebSearchArgs

    # A single attempt is enforced here because SearchProviderFactory already
    # orchestrates its own internal fallback chain across multiple engines.
    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=25.0,
        retryable=False,
        idempotent=True,
        side_effects=False,
    )

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            execution_policy=self.execution_policy,
        )

    # ------------------------------------------------------------------
    # Configuration loading
    # ------------------------------------------------------------------
    def _load_live_search_config(self) -> Dict[str, Any]:
        """
        Discover and parse the [search] table from config.toml.
        Search order is deterministic and portable across platforms.
        Falls back to a safe DuckDuckGo-only default when no config is found.
        """
        candidates: List[Path] = [
            Path("config/config.toml"),
            Path("../config/config.toml"),
            Path("../../config/config.toml"),
        ]

        # Compute a repository-relative path based on this file's location.
        try:
            module_relative = (
                Path(__file__).resolve().parent.parent.parent.parent.parent
                / "config"
                / "config.toml"
            )
            candidates.append(module_relative)
        except Exception:
            pass

        target_file: Optional[Path] = None
        for candidate in candidates:
            try:
                if candidate.is_file():
                    target_file = candidate
                    break
            except OSError:
                continue

        if target_file is not None:
            data = self._parse_toml(target_file)
            if isinstance(data, dict):
                search_section = data.get("search")
                if isinstance(search_section, dict):
                    return search_section

        logger.debug(
            "No config.toml found in any candidate path; using built-in defaults."
        )
        return self._default_search_config()

    @staticmethod
    def _parse_toml(target_file: Path) -> Optional[Dict[str, Any]]:
        """
        Parse a TOML file using the standard library when available,
        otherwise fall back to the third-party tomli package.
        """
        try:
            import tomllib  # type: ignore[import-not-found]
            with open(target_file, "rb") as handle:
                return tomllib.load(handle)
        except Exception:
            pass

        try:
            import tomli  # type: ignore[import-not-found]
            with open(target_file, "rb") as handle:
                return tomli.load(handle)
        except Exception as exc:
            logger.debug(f"Failed to parse config.toml at {target_file}: {exc}")
            return None

    @staticmethod
    def _default_search_config() -> Dict[str, Any]:
        return {
            "enabled": True,
            "engine": "DuckDuckGo",
            "fallback_engines": ["DuckDuckGo"],
            "retry_delay": 5,
            "max_retries": 3,
            "lang": "en",
            "country": "us",
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
        cost_usd: float,
        is_success: bool,
        error_message: Optional[str],
        session_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        """
        Record search telemetry in a worker thread so the event loop is
        never blocked by SQLite I/O. Failures are silently ignored.
        """
        def _persist() -> None:
            try:
                from peldrun.runtime.search_usage_store import (
                    get_search_usage_store,
                )
                store = get_search_usage_store()
                store.record_search(
                    query=query,
                    provider=provider,
                    results_count=results_count,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                    is_success=is_success,
                    error_message=error_message,
                    session_id=session_id,
                    run_id=run_id,
                )
            except Exception:
                # Telemetry failures must never break search execution.
                pass

        try:
            await asyncio.to_thread(_persist)
        except Exception:
            pass

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

        # Clamp the requested result count to a safe range.
        try:
            requested = int(max_results or 5)
        except (TypeError, ValueError):
            requested = 5
        limit = min(max(requested, 1), 15)

        search_cfg = self._load_live_search_config()

        # Air-Gapped mode is a valid, non-error administrative state.
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

        # Execute the orchestrated search across all configured providers.
        try:
            response = await SearchProviderFactory.execute_orchestrated_search(
                query=clean_q,
                search_config=search_cfg,
                max_results=limit,
                lang=search_cfg.get("lang", "en"),
                country=search_cfg.get("country", "us"),
                **kwargs,
            )
        except Exception as exc:
            logger.error(f"Search orchestration raised an exception: {exc}")
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

        # Persist telemetry without blocking the event loop.
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
        )

        # Success path: results were returned by the orchestrator.
        if response.is_success and response.results:
            return ToolResult(
                output=response.format_markdown(),
                exit_code=0,
                is_error=False,
            )

        # Failure path: explicit error signal prevents LLM retry loops.
        diagnostic = response.error_message or "No results were returned."
        return ToolResult(
            output=(
                f"⚠️ Web search did not return any results.\n"
                f"Provider: {response.provider}\n"
                f"Query: '{clean_q}'\n"
                f"Reason: {diagnostic}\n"
                f"IMPORTANT: Do NOT retry this search with the same or a "
                f"similar query. Either proceed with your existing knowledge "
                f"or clearly inform the user that live search is unavailable."
            ),
            exit_code=1,
            is_error=True,
        )


__all__ = ["WebSearchArgs", "WebSearchTool"]
