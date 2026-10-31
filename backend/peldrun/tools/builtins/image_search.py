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

import asyncio
import logging
import urllib.parse as _urlparse
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
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

# Import the image proxy URL builder so the tool can present the
# agent with a downloadable proxy link instead of the raw upstream
# URL (which is frequently blocked by hotlink protection: 403/406).
# The import is lazy-friendly — if the omweb layer is not available
# (e.g., running core-only in tests), we fall back to the original
# URL without failing.
try:
    from omweb.routers.image_proxy_rtr import (
        build_proxy_url as _build_proxy_url,
    )
    _HAS_PROXY_RTR = True
except Exception:  # pragma: no cover — graceful fallback
    _build_proxy_url = None  # type: ignore[assignment]
    _HAS_PROXY_RTR = False

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
    auto_download: Optional[bool] = Field(
        default=None,
        description=(
            "Whether to download images into <workspace>/images/. Set to "
            "TRUE when you plan to reference these images in HTML, "
            "Markdown, reports, or other files you will create (broken "
            "file references would result otherwise). Set to FALSE for "
            "purely informational image searches. Omit to inherit the "
            "operator's configured default. Ignored when the operator has "
            "set image_search.auto_download to 'always' or 'never'."
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
    # Auto-download helper
    # ------------------------------------------------------------------
    _DOWNLOAD_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
    _VALID_IMG_EXTS = frozenset(
        {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
    )

    async def _download_to_workspace(
        self,
        url: str,
        filename: str,
        source_host: Optional[str] = None,
    ) -> Optional[str]:
        """
        Download one image into ``<workspace>/images/<filename>``.

        Uses a browser User-Agent and a Referer header derived from the
        source host so that CDNs with hotlink protection (Alamy, Getty,
        Fodrics, iStock, etc.) accept the request.

        Returns the workspace-relative path on success, or None on any
        failure. Failures are logged at debug level; they never abort
        the search itself.
        """
        if not self.workspace_root or not url:
            return None

        try:
            images_dir = Path(self.workspace_root) / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            target = images_dir / filename

            headers = {
                "User-Agent": self._DOWNLOAD_UA,
                "Accept": (
                    "image/avif,image/webp,image/jpeg,image/png,"
                    "image/*;q=0.8,*/*;q=0.5"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
            if source_host:
                headers["Referer"] = f"https://{source_host.strip()}/"

            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                max_redirects=3,
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        logger.debug(
                            "[image_search] Download HTTP %d for %s",
                            resp.status_code,
                            url[:80],
                        )
                        return None

                    ct = (resp.headers.get("content-type") or "").lower()
                    if "image/" not in ct and "octet-stream" not in ct:
                        logger.debug(
                            "[image_search] Non-image content-type %r for %s",
                            ct,
                            url[:80],
                        )
                        return None

                    # Stream to disk in 64 KB chunks; cap at 15 MB.
                    MAX_BYTES = 15 * 1024 * 1024
                    total = 0
                    with target.open("wb") as fp:
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > MAX_BYTES:
                                fp.close()
                                target.unlink(missing_ok=True)
                                return None
                            fp.write(chunk)

            return str(target.relative_to(Path(self.workspace_root))).replace(
                "\\", "/"
            )
        except Exception as exc:
            logger.debug(
                "[image_search] Download failed for %s: %s",
                url[:80],
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _arun(
        self,
        query: str,
        max_results: Optional[int] = None,
        *,
        auto_download: Optional[bool] = None,
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

        # ------------------------------------------------------------------
        # Resolve the effective auto_download behavior.
        #
        # The operator-level setting (image_search.auto_download) accepts:
        #   "always"  -> download every image, unconditionally
        #   "never"   -> never download; only return proxy URLs
        #   "smart"   -> defer to the tool argument (default for new installs)
        #
        # Backward compatibility: legacy boolean values in config.toml
        # map as follows: True -> "always", False -> "never".
        #
        # In "smart" mode the LLM decides per call via the auto_download
        # tool argument:
        #   * auto_download=True  -> the agent will embed the images in a
        #     file it is creating (HTML/Markdown/report); download them.
        #   * auto_download=False OR omitted -> purely informational
        #     search; return proxy URLs for the gallery only.
        # ------------------------------------------------------------------
        _raw_mode = img_cfg.get("auto_download", "smart")
        if isinstance(_raw_mode, bool):
            _resolved_mode = "always" if _raw_mode else "never"
        elif isinstance(_raw_mode, str) and _raw_mode.strip().lower() in (
            "always",
            "smart",
            "never",
        ):
            _resolved_mode = _raw_mode.strip().lower()
        else:
            _resolved_mode = "smart"

        if _resolved_mode == "always":
            effective_auto_download = True
        elif _resolved_mode == "never":
            effective_auto_download = False
        else:  # smart
            effective_auto_download = bool(auto_download) if auto_download is not None else False

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
        # Optional auto-download: fetch every image into the workspace
        # BEFORE building the output so the paths can be inlined in the
        # markdown the agent reads.
        # ------------------------------------------------------------------
        downloaded_local_paths: Dict[int, str] = {}
        if effective_auto_download and self.workspace_root and response.results:
            download_tasks: List[Any] = []
            download_meta: List[Dict[str, Any]] = []

            for idx, item in enumerate(response.results, start=1):
                if not item.image_url:
                    continue

                # Determine filename extension from the URL path.
                try:
                    _parsed = _urlparse.urlsplit(item.image_url)
                    ext = Path(_parsed.path).suffix.lower()
                except Exception:
                    ext = ""
                if ext not in self._VALID_IMG_EXTS:
                    ext = ".jpg"

                filename = f"image_{idx}{ext}"
                download_tasks.append(
                    self._download_to_workspace(
                        item.image_url,
                        filename,
                        source_host=item.source,
                    )
                )
                download_meta.append({"idx": idx, "filename": filename})

            if download_tasks:
                try:
                    results_dl = await asyncio.gather(
                        *download_tasks, return_exceptions=True
                    )
                    for _meta, _result in zip(download_meta, results_dl):
                        if isinstance(_result, str) and _result:
                            downloaded_local_paths[_meta["idx"]] = _result
                except Exception as _dl_err:
                    logger.debug(
                        "[image_search] Batch download error: %s", _dl_err
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

        # Determine the backend base URL for proxy links. The proxy
        # endpoint runs on the same backend that hosts this tool, so
        # a well-known local default is used. Override via the
        # PELDRUN_PROXY_BASE environment variable when the backend
        # runs on a non-default host/port (e.g. in production).
        import os as _os
        _proxy_base = (
            _os.environ.get("PELDRUN_PROXY_BASE", "http://127.0.0.1:8088")
            .strip()
            .rstrip("/")
        )

        _first_proxied_url: Optional[str] = None

        for idx, item in enumerate(response.results, start=1):
            image_urls.append(item.image_url)

            dims = ""
            if item.width and item.height:
                dims = f" ({item.width}x{item.height})"

            # Build a PROXIED URL for the agent to download from.
            # Direct downloads from the original host fail (403/406)
            # because image CDNs block non-browser User-Agents and
            # require a matching Referer. The proxy endpoint adds
            # both headers server-side, guaranteeing the download
            # succeeds.
            _display_img_url = item.image_url
            _display_thumb_url = item.thumbnail_url
            if _HAS_PROXY_RTR and _build_proxy_url is not None:
                try:
                    _px = _build_proxy_url(item.image_url, item.source)
                    if _px:
                        _display_img_url = f"{_proxy_base}{_px}"
                    if item.thumbnail_url:
                        _pt = _build_proxy_url(
                            item.thumbnail_url, item.source
                        )
                        if _pt:
                            _display_thumb_url = f"{_proxy_base}{_pt}"
                except Exception:
                    # Fall back to the original URLs on any proxy
                    # construction failure. Downloads may fail, but
                    # the tool keeps working.
                    _display_img_url = item.image_url
                    _display_thumb_url = item.thumbnail_url

            _local_path = downloaded_local_paths.get(idx)

            if idx == 1 and _display_img_url:
                _first_proxied_url = _display_img_url

            lines.append(f"{idx}. **{item.title}**{dims}")
            # ALWAYS emit "Image URL:" FIRST. The downstream gallery
            # parser in peldrun_engine.py relies on this exact label
            # (r"**Image URL:**") to extract the proxied URL for the
            # visual gallery. It MUST come before any other optional
            # field so the anchored regex matches reliably.
            lines.append(f"   - **Image URL:** {_display_img_url}")
            if _display_thumb_url:
                lines.append(f"   - **Thumbnail:** {_display_thumb_url}")
            if item.source_url:
                lines.append(f"   - **Source Page:** {item.source_url}")
            if item.source:
                lines.append(f"   - **Host:** {item.source}")
            if _local_path:
                # Auto-download succeeded: ALSO give the agent a LOCAL
                # path it can drop directly into HTML/Markdown without
                # any additional step. This is the fast path that
                # avoids 404s from missing files.
                lines.append(
                    f"   - **Local Image Path (ready to use in files):** "
                    f"`{_local_path}`"
                )
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

        if effective_auto_download and downloaded_local_paths:
            # Auto-download succeeded for at least one image: tell the
            # agent exactly what to do, no ambiguity.
            _downloaded_count = len(downloaded_local_paths)
            _total_count = len(response.results)
            _failed_count = _total_count - _downloaded_count

            _local_list = "\n".join(
                f"- `{path}`"
                for _, path in sorted(downloaded_local_paths.items())
            )
            _fail_note = (
                f"\n\n**⚠️ Note:** {_failed_count} image(s) could not be "
                f"downloaded (upstream host blocked the request). Use the "
                f"successfully downloaded {_downloaded_count} image(s) "
                f"listed above. If you need the missing ones, use the "
                f"**Source Page** URLs to fetch them manually."
                if _failed_count > 0
                else ""
            )

            lines.append(
                "\n---\n\n"
                "## ✅ Images already downloaded to your workspace\n\n"
                f"The {_downloaded_count} image file(s) below are ALREADY "
                "saved in your workspace under the `images/` directory. "
                "Use these LOCAL PATHS directly in any HTML, Markdown, or "
                "report you create:\n\n"
                f"{_local_list}\n"
                f"{_fail_note}\n\n"
                "**Example usage in HTML:**\n"
                "```html\n"
                '<img src="images/image_1.jpg" alt="..." />\n'
                '<img src="images/image_2.jpg" alt="..." />\n'
                "```\n\n"
                "**Do NOT** download these again. **Do NOT** reference "
                "upstream URLs (Alamy, Getty, Fodors, etc.). Just use the "
                "`images/image_N.jpg` paths exactly as listed above.\n"
            )
        else:
            # Auto-download disabled or fully failed: give manual
            # instructions with a ready-to-run script.
            _example_cmd = ""
            if _first_proxied_url:
                _example_cmd = (
                    f"mkdir -p images\n"
                    f"curl -L -o images/image_1.jpg '{_first_proxied_url}'"
                )
            else:
                _example_cmd = (
                    "mkdir -p images\n"
                    "curl -L -o images/image_1.jpg '<Image URL from above>'"
                )

            lines.append(
                "\n---\n\n"
                "## How to download these images\n\n"
                "The **Image URL** field above is a **proxied download "
                "link** that routes through the local PELDRUN backend at "
                f"`{_proxy_base}`. It sets the correct `User-Agent` and "
                "`Referer` headers, bypassing hotlink protection.\n\n"
                "**⚠️ Important:** Do NOT construct your own URL from "
                "the `Source Page` or `Host`. Use the exact `Image URL` "
                "string above.\n\n"
                "**Example (bash / curl):**\n"
                "```bash\n"
                f"{_example_cmd}\n"
                "```\n\n"
                "**Example (python_execute):**\n"
                "```python\n"
                "import urllib.request\n"
                "import os\n\n"
                "os.makedirs('images', exist_ok=True)\n"
                "url = (\n"
                f"    '{(_first_proxied_url or '<Image URL from above>')}'\n"
                ")\n"
                "urllib.request.urlretrieve(url, 'images/image_1.jpg')\n"
                "```\n\n"
                "Repeat for each image, incrementing the filename "
                "(`image_2.jpg`, `image_3.jpg`, ...)."
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
