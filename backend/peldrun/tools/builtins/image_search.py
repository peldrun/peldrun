# backend/peldrun/tools/builtins/image_search.py
"""
PELDRUN Core Image Search Tool.

Routes image search through SearchProviderFactory, so every image-capable
provider (DuckDuckGo, SearXNG, Brave, Google CSE) is available with the
same failover, cost tracking, and diagnostic contract as text-based
web_search.

Key guarantees:
- Delegates provider selection, chain construction, retry policy, and
  count ceilings to SearchProviderFactory.search_images(), keeping the
  image path in lockstep with the text path.
- Never imports or instantiates any provider directly. Non-image-capable
  providers (Tavily, Jina, Yandex) are automatically skipped by the
  factory chain.
- Preserves backward-compatible metadata (``image_urls``) while adding
  rich per-item metadata (thumbnails, source pages, dimensions) for UI
  integration.
- Preserves ``max_results=None`` when omitted so user-configured
  ``search.image_search.*`` settings govern execution, matching the
  WebSearchTool contract.
- Emits structured error metadata (``error_code``, ``status``, provider)
  instead of opaque string failures.

The agent is responsible for downloading images to the workspace using
bash or python_execute.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool
from peldrun.tools.builtins.search_providers.base import (
    ImageSearchResponse,
    SearchProviderError,
)
from peldrun.tools.builtins.search_providers.factory import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_HARD_CAP,
    MIN_RESULTS_LIMIT,
    SearchProviderFactory,
)
from peldrun.tools.contract import ToolResult

logger = logging.getLogger("peldrun.tools.builtins.image_search")


class ImageSearchArgs(BaseModel):
    """
    Canonical argument schema for the image_search tool.

    ``max_results`` is intentionally optional. When omitted, the factory
    resolves the effective count from ``search.image_search.*`` settings
    so that per-call arguments never silently override operator policy.
    """

    query: str = Field(
        ...,
        min_length=1,
        description="Search query for images.",
    )
    max_results: Optional[int] = Field(
        default=None,
        ge=MIN_RESULTS_LIMIT,
        le=MAX_RESULTS_HARD_CAP,
        description=(
            "Optional desired maximum number of image results. Omit to "
            "inherit PELDRUN Search Settings "
            "(search.image_search.default_max_results). The configured "
            "provider ceiling and provider capability maximum are always "
            "enforced."
        ),
    )


class ImageSearchTool(BaseTool):
    """
    Search the web for images and return direct image URLs.

    Image search is dispatched through SearchProviderFactory so that
    provider selection, failover, count ceilings, and cost accounting
    all follow the same authoritative policy as text-based web_search.
    """

    name: str = "image_search"
    description: str = (
        "Search the web for images and return direct image URLs. "
        "Automatically fails over across image-capable providers "
        "(DuckDuckGo, SearXNG, Brave, Google CSE). Use this when the user "
        "asks to find, download, or see pictures of a specific topic."
    )
    args_schema: type[BaseModel] = ImageSearchArgs

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        search_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        try:
            super().__init__(workspace_root=workspace_root, **kwargs)
        except TypeError:
            try:
                super().__init__()
            except Exception:
                pass

        self.workspace_root = workspace_root
        self._explicit_search_config = search_config

    # ------------------------------------------------------------------
    # Authoritative config resolution (mirrors WebSearchTool precedence)
    # ------------------------------------------------------------------
    def _load_authoritative_search_config(
        self,
        override_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Load the authoritative [search] section from override_config, the
        instance-level explicit config, or config/config.toml on disk.

        Precedence (highest to lowest):
          1. ``override_config`` (per-call argument).
          2. ``self._explicit_search_config`` (constructor argument).
          3. ``omweb.config.load_config()`` when available.
          4. ``config/config.toml`` discovered relative to cwd or repo root.
          5. Safe in-memory defaults.
        """
        if isinstance(override_config, dict):
            return SearchProviderFactory._unwrap_search_section(override_config)

        if isinstance(self._explicit_search_config, dict):
            return SearchProviderFactory._unwrap_search_section(
                self._explicit_search_config
            )

        try:
            from omweb.config import load_config  # type: ignore[import-not-found]

            full_cfg = load_config()
            if isinstance(full_cfg, dict):
                return SearchProviderFactory._unwrap_search_section(full_cfg)
        except Exception:
            pass

        candidate_paths = [
            Path.cwd() / "config" / "config.toml",
            Path.cwd().parent / "config" / "config.toml",
            Path(__file__).resolve().parents[4] / "config" / "config.toml",
        ]
        for path in candidate_paths:
            if path.is_file():
                try:
                    import tomllib  # Python 3.11+

                    with path.open("rb") as f:
                        data = tomllib.load(f)
                    if isinstance(data, dict):
                        return SearchProviderFactory._unwrap_search_section(data)
                except Exception as exc:
                    logger.debug(
                        "Failed reading image search config from %s: %s",
                        path,
                        exc,
                    )

        return {
            "enabled": True,
            "default_max_results": DEFAULT_MAX_RESULTS,
            "lang": "en",
            "country": "us",
            "total_budget_seconds": 20.0,
            "max_retries": 0,
            "retry_delay": 1.0,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _arun(
        self,
        query: str,
        max_results: Optional[int] = None,
        *,
        search_config: Optional[Dict[str, Any]] = None,
        override_provider: Optional[str] = None,
        lang: Optional[str] = None,
        country: Optional[str] = None,
        timeout: Optional[float] = None,
        run_id: Optional[str] = None,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Execute image search through SearchProviderFactory.

        Preserves ``max_results=None`` so settings-based defaults are
        honored. Never raises raw provider exceptions to the caller;
        every failure surfaces as a structured ToolResult with
        ``is_error=True``.

        Execution context parameters (run_id, job_id, session_id, chat_id,
        project_id) are accepted for API parity with WebSearchTool and
        reserved for future run-scoped telemetry wiring.
        """
        clean_q = (query or "").strip()
        if not clean_q:
            return ToolResult(
                output="Image Search Error: Query cannot be empty.",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "EMPTY_QUERY",
                    "requested_max_results": max_results,
                },
            )

        cfg = self._load_authoritative_search_config(search_config)
        if cfg.get("enabled") is False:
            return ToolResult(
                output=(
                    "Image Search Error: External web search is disabled "
                    "in PELDRUN Search Settings."
                ),
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SEARCH_DISABLED",
                    "requested_max_results": max_results,
                },
            )

        img_cfg = (
            cfg.get("image_search")
            if isinstance(cfg.get("image_search"), dict)
            else {}
        )
        if img_cfg.get("enabled") is False:
            return ToolResult(
                output=(
                    "Image Search Error: Image search is disabled "
                    "in PELDRUN Search Settings."
                ),
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "IMAGE_SEARCH_DISABLED",
                    "requested_max_results": max_results,
                },
            )

        try:
            response: ImageSearchResponse = (
                await SearchProviderFactory.search_images(
                    query=clean_q,
                    max_results=max_results,
                    search_config=cfg,
                    override_provider=override_provider,
                    lang=lang,
                    country=country,
                    timeout=timeout,
                    **kwargs,
                )
            )
        except SearchProviderError as exc:
            logger.warning(
                "Image search failed [%s]: %s",
                exc.error_code,
                exc.message,
            )
            return ToolResult(
                output=(
                    f"Image Search Error [{exc.error_code}]: {exc.message}"
                ),
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": exc.error_code,
                    "status": (
                        exc.status.value
                        if hasattr(exc.status, "value")
                        else str(exc.status)
                    ),
                    "requested_max_results": max_results,
                },
            )
        except Exception as exc:
            logger.error("Image search unexpected failure: %s", exc)
            return ToolResult(
                output=f"Image Search Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "IMAGE_SEARCH_EXCEPTION",
                    "requested_max_results": max_results,
                },
            )

        status_val = (
            response.status.value
            if hasattr(response.status, "value")
            else str(response.status)
        )
        cost_basis_val = (
            response.cost_basis.value
            if hasattr(response.cost_basis, "value")
            else str(response.cost_basis)
        )

        # Valid EMPTY outcome (HTTP 200 with zero images): keep as a
        # non-error ToolResult so callers can distinguish "search
        # completed with no matches" from "provider failed".
        if not response.results:
            return ToolResult(
                output=(
                    f"Image Search via {response.provider}: "
                    f"{response.error_message or 'No images returned.'}"
                ),
                exit_code=0,
                is_error=False,
                metadata={
                    "provider": response.provider,
                    "query": clean_q,
                    "status": status_val,
                    "error_code": response.error_code,
                    "latency_ms": response.latency_ms,
                    "request_count": response.request_count,
                    "estimated_cost_usd": response.estimated_cost_usd,
                    "cost_basis": cost_basis_val,
                    "requested_max_results": max_results,
                    "effective_max_results": response.effective_max_results,
                    "max_results_limit": response.max_results_limit,
                    "returned_results_count": 0,
                    "image_urls": [],
                    "images": [],
                },
            )

        # ------------------------------------------------------------------
        # Build rich Markdown output plus structured per-item metadata.
        # ------------------------------------------------------------------
        lines: List[str] = [
            f"## Image Search Results for '{clean_q}'",
            (
                f"*(via {response.provider} | "
                f"{len(response.results)} image(s) | "
                f"{response.latency_ms:.0f}ms | "
                f"est. ${response.estimated_cost_usd or 0.0:.4f})*"
            ),
            "",
        ]

        image_urls: List[str] = []
        image_metadata: List[Dict[str, Any]] = []

        for idx, item in enumerate(response.results, start=1):
            image_urls.append(item.image_url)

            dims = ""
            if item.width and item.height:
                dims = f" ({item.width}x{item.height})"

            lines.append(f"{idx}. **{item.title}**{dims}")
            lines.append(f"   - **Image URL:** {item.image_url}")
            if item.thumbnail_url:
                lines.append(f"   - **Thumbnail:** {item.thumbnail_url}")
            if item.source_url:
                lines.append(f"   - **Source Page:** {item.source_url}")
            if item.source:
                lines.append(f"   - **Host:** {item.source}")
            lines.append("")

            image_metadata.append(
                {
                    "title": item.title,
                    "image_url": item.image_url,
                    "thumbnail_url": item.thumbnail_url,
                    "source_url": item.source_url,
                    "width": item.width,
                    "height": item.height,
                    "source": item.source,
                }
            )

        lines.append(
            "\n**Note:** To download and display these images in the "
            "workspace, use `bash` (`curl`) or `python_execute` to save "
            "the files to the `images/` directory "
            "(e.g., `images/image_1.jpg`)."
        )

        return ToolResult(
            output="\n".join(lines),
            exit_code=0,
            is_error=False,
            metadata={
                "provider": response.provider,
                "query": clean_q,
                "status": status_val,
                "image_urls": image_urls,
                "images": image_metadata,
                "latency_ms": response.latency_ms,
                "request_count": response.request_count,
                "estimated_cost_usd": response.estimated_cost_usd,
                "cost_basis": cost_basis_val,
                "requested_max_results": max_results,
                "effective_max_results": response.effective_max_results,
                "max_results_limit": response.max_results_limit,
                "returned_results_count": len(image_urls),
            },
        )

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------
    async def aexecute(
        self,
        query: Optional[str] = None,
        max_results: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Canonical async entrypoint. Overrides BaseTool.aexecute so that
        runtime-only kwargs (search_config, override_provider, run context)
        flow through to _arun without triggering args_schema validation.

        Mirrors WebSearchTool.aexecute to keep the text and image tool
        contracts identical for callers (adapters, agents, tests).
        """
        if query is None and "q" in kwargs:
            query = kwargs.pop("q")
        return await self._arun(
            query=query if query is not None else "",
            max_results=max_results,
            **kwargs,
        )


__all__ = ["ImageSearchArgs", "ImageSearchTool"]
