# backend/peldrun/tools/builtins/browser_interactive/tool.py
"""
Interactive Browser Tool (BaseTool implementation).

Provides a real Chromium-based browser to agents, with token-efficient
state extraction via the accessibility tree (snapshot) instead of costly
screenshots. Implements 9 actions:

  * navigate      — open a URL, with SSRF protection.
  * snapshot      — extract the accessibility tree as YAML, populate refs.
  * get_content   — extract visible text from the page body.
  * click         — click an element by ref or CSS selector.
  * fill          — type into an input by ref or CSS selector.
  * scroll        — scroll the viewport.
  * screenshot    — save a PNG to the workspace (never sent to the model).
  * back          — navigate back in history.
  * forward       — navigate forward in history.

All actions return a canonical ``ToolResult``. No exceptions escape.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool
from peldrun.tools.contract import ToolExecutionPolicy, ToolResult

from .security import (
    SSRFProtectionError,
    is_safe_resolved_url,
    validate_url_or_raise,
)
from .session import get_session_manager, _ContextState

logger = logging.getLogger("peldrun.tools.builtins.browser_interactive.tool")


# ---------------------------------------------------------------------------
# Argument schema
# ---------------------------------------------------------------------------

class BrowserInteractiveArgs(BaseModel):
    """Canonical argument schema for the browser_interactive tool."""

    action: str = Field(
        ...,
        description=(
            "Operation to perform: navigate, snapshot, get_content, click, "
            "fill, scroll, screenshot, back, forward."
        ),
    )
    url: Optional[str] = Field(
        default=None,
        description="Target URL (required for 'navigate').",
    )
    ref: Optional[str] = Field(
        default=None,
        description="Element reference ID from a previous snapshot (e.g. 'e7').",
    )
    selector: Optional[str] = Field(
        default=None,
        description="CSS selector for the target element (fallback if ref missing).",
    )
    value: Optional[str] = Field(
        default=None,
        description="Text to enter when action is 'fill'.",
    )
    direction: Optional[str] = Field(
        default="down",
        description="Scroll direction: 'down', 'up', 'top', 'bottom'.",
    )
    full_page: bool = Field(
        default=False,
        description="Capture full scrollable page (true) or viewport (false).",
    )
    max_chars: int = Field(
        default=8000,
        ge=500,
        le=50000,
        description="Maximum characters to return for get_content or snapshot.",
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

class BrowserInteractiveTool(BaseTool):
    """
    Control a real Chromium browser for dynamic page interaction.
    """

    name: str = "browser_interactive"
    description: str = (
        "Control a real Chromium browser to interact with dynamic web pages. "
        "Use 'navigate' to open a URL, 'snapshot' to get a token-efficient "
        "accessibility tree with element references (refs), 'click' or 'fill' "
        "using those refs, 'get_content' for visible text, 'scroll' to reveal "
        "more, and 'screenshot' to save a PNG image to the workspace. "
        "Screenshots are saved as artifacts and are NOT returned to the model."
    )
    args_schema: type[BaseModel] = BrowserInteractiveArgs

    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        timeout_seconds=120.0,   # longer than http_fetch due to browser overhead
        retryable=False,
        idempotent=False,        # click/fill are stateful
        side_effects=True,       # interacts with external sites
    )

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, workspace_root: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(workspace_root=workspace_root, **kwargs)
        self._run_id: Optional[str] = None

    def set_run_id(self, run_id: str) -> None:
        """Bind this tool instance to a specific run_id for session isolation."""
        self._run_id = run_id

    # ------------------------------------------------------------------
    # BaseTool contract
    # ------------------------------------------------------------------

    async def _arun(
        self,
        action: str,
        url: Optional[str] = None,
        ref: Optional[str] = None,
        selector: Optional[str] = None,
        value: Optional[str] = None,
        direction: str = "down",
        full_page: bool = False,
        max_chars: int = 8000,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Dispatch to the appropriate action handler.

        All exceptions are caught and returned as structured ToolResults.
        """
        if not self._run_id:
            return ToolResult(
                output=(
                    "Browser Error: no run_id is bound to this tool instance. "
                    "This is an internal configuration error."
                ),
                exit_code=1,
                is_error=True,
                metadata={"error_code": "MISSING_RUN_ID"},
            )

        action = (action or "").strip().lower()

        try:
            if action == "navigate":
                return await self._navigate(url)
            if action == "snapshot":
                return await self._snapshot(max_chars)
            if action == "get_content":
                return await self._get_content(max_chars)
            if action == "click":
                return await self._click(ref, selector)
            if action == "fill":
                return await self._fill(ref, selector, value)
            if action == "scroll":
                return await self._scroll(direction)
            if action == "screenshot":
                return await self._screenshot(full_page)
            if action == "back":
                return await self._back()
            if action == "forward":
                return await self._forward()

            return ToolResult(
                output=f"Browser Error: unknown action {action!r}.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "UNKNOWN_ACTION", "action": action},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "browser_interactive action %r failed: %s", action, exc
            )
            return ToolResult(
                output=f"Browser Error ({action}): {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "ACTION_EXCEPTION",
                    "action": action,
                    "exception_type": type(exc).__name__,
                },
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_page_and_state(self) -> tuple[Any, _ContextState]:
        """
        Ensure the session context exists and the active page is available.
        Creates a new page if the context has none.
        """
        mgr = get_session_manager()
        state = await mgr.get_context(self._run_id)
        pages = state.context.pages
        if not pages:
            page = await state.context.new_page()
        else:
            page = pages[-1]  # most recently opened
        return page, state

    async def _check_final_url(self, page: Any) -> Optional[ToolResult]:
        """
        After navigation, verify the final URL is still safe (Layer 3).
        Returns a ToolResult on violation, or None if safe.
        """
        final_url = page.url or ""
        if not is_safe_resolved_url(final_url):
            try:
                await page.goto("about:blank")
            except Exception:
                pass
            return ToolResult(
                output=(
                    f"Browser Blocked: navigation landed on unsafe URL "
                    f"{final_url!r} (possibly via redirect)."
                ),
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SSRF_REDIRECT_BLOCKED",
                    "final_url": final_url,
                },
            )
        return None

    async def _resolve_element(
        self,
        page: Any,
        state: _ContextState,
        ref: Optional[str],
        selector: Optional[str],
    ) -> tuple[Optional[Any], Optional[ToolResult]]:
        """
        Resolve an element by ref (preferred) or CSS selector.
        Returns (element, None) on success, or (None, error_result).
        """
        # 1. ref lookup (preferred)
        if ref:
            element = state.ref_map.get(ref)
            if element is None:
                return None, ToolResult(
                    output=(
                        f"Browser Error: ref {ref!r} not found. "
                        f"Call 'snapshot' first to refresh element references."
                    ),
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "REF_NOT_FOUND", "ref": ref},
                )
            return element, None

        # 2. CSS selector fallback
        if selector:
            try:
                element = await page.query_selector(selector)
            except Exception as exc:
                return None, ToolResult(
                    output=f"Browser Error: invalid selector {selector!r}: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "INVALID_SELECTOR", "selector": selector},
                )
            if element is None:
                return None, ToolResult(
                    output=f"Browser Error: no element matches {selector!r}.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "SELECTOR_NOT_FOUND", "selector": selector},
                )
            return element, None

        return None, ToolResult(
            output="Browser Error: either 'ref' or 'selector' is required.",
            exit_code=1,
            is_error=True,
            metadata={"error_code": "MISSING_TARGET"},
        )

    # ------------------------------------------------------------------
    # Action: navigate
    # ------------------------------------------------------------------

    async def _navigate(self, url: Optional[str]) -> ToolResult:
        target = (url or "").strip()
        if not target:
            return ToolResult(
                output="Browser Error: 'url' is required for navigate.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "MISSING_URL"},
            )

        try:
            validate_url_or_raise(target)
        except SSRFProtectionError as exc:
            return ToolResult(
                output=f"Browser Blocked: {exc.reason}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SSRF_BLOCKED",
                    "url": target,
                    "reason": exc.reason,
                },
            )

        page, state = await self._get_page_and_state()
        state.touch()

        try:
            response = await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: navigation to {target!r} failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "NAVIGATION_FAILED",
                    "url": target,
                    "exception_type": type(exc).__name__,
                },
            )

        # Layer 3: verify final URL after redirects.
        violation = await self._check_final_url(page)
        if violation is not None:
            return violation

        status = response.status if response else 0
        title = await page.title()

        return ToolResult(
            output=(
                f"Navigated to {page.url}\n"
                f"Title: {title}\n"
                f"Status: {status}"
            ),
            exit_code=0,
            is_error=False,
            metadata={
                "url": page.url,
                "title": title,
                "status_code": status,
            },
        )

    # ------------------------------------------------------------------
    # Action: snapshot
    # ------------------------------------------------------------------

    async def _snapshot(self, max_chars: int) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        if page.url in ("about:blank", ""):
            return ToolResult(
                output="Browser Error: no page loaded. Call 'navigate' first.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "NO_PAGE_LOADED"},
            )

        # Clear old ref map — refs are snapshot-scoped.
        state.ref_map.clear()

        try:
            # Build a YAML representation of the accessibility tree with refs.
            snapshot_text, ref_map = await self._build_accessibility_yaml(page, max_chars)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: snapshot failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "SNAPSHOT_FAILED",
                    "exception_type": type(exc).__name__,
                },
            )

        state.ref_map.update(ref_map)

        truncated = len(snapshot_text) > max_chars
        text = snapshot_text[:max_chars]

        output_parts = [f"Page: {page.url}"]
        if truncated:
            output_parts.append(
                f"Note: snapshot truncated to {max_chars} characters."
            )
        output_parts.append("")
        output_parts.append(text)

        return ToolResult(
            output="\n".join(output_parts),
            exit_code=0,
            is_error=False,
            metadata={
                "url": page.url,
                "refs_count": len(ref_map),
                "truncated": truncated,
            },
        )

    async def _build_accessibility_yaml(
        self, page: Any, max_chars: int
    ) -> tuple[str, dict[str, Any]]:
        """
        Construct a compact YAML representation of interactive page elements.

        Strategy:
          * Query all interactive elements (buttons, links, inputs, etc.).
          * Assign stable refs (e1, e2, ...) to each.
          * Emit one YAML line per element with role, name, and ref.
          * Cap the number of elements to avoid runaway output.
        """
        ref_map: dict[str, Any] = {}

        # Broaden the selector to cover common interactive + meaningful elements.
        selector = (
            "a[href], button, input, textarea, select, [role='button'], "
            "[role='link'], [role='textbox'], [role='checkbox'], "
            "[role='radio'], [role='combobox'], [role='menuitem'], "
            "h1, h2, h3, img[alt], form, label"
        )
        elements = await page.query_selector_all(selector)

        lines: list[str] = ["- role: heading", "  name: \"Page Structure\""]
        emitted = 0
        max_elements = 200  # hard cap to prevent pathological pages

        for el in elements:
            if emitted >= max_elements:
                lines.append(f"- note: reached element cap ({max_elements})")
                break

            try:
                tag = (await el.evaluate("e => e.tagName.toLowerCase()")) or ""
                role = await self._infer_role(el, tag)
                name = await self._infer_name(el, tag)
                if not name:
                    continue  # skip unnamed elements (noise)

                emitted += 1
                ref_id = f"e{emitted}"
                ref_map[ref_id] = el

                name_escaped = name.replace('"', '\\"').replace("\n", " ")[:120]
                lines.append(f"- role: {role}")
                lines.append(f"  name: \"{name_escaped}\"")
                lines.append(f"  ref: {ref_id}")
            except Exception:
                continue  # skip individual element on error

        return "\n".join(lines), ref_map

    @staticmethod
    async def _infer_role(el: Any, tag: str) -> str:
        """Best-effort ARIA role inference from element + tag."""
        try:
            explicit = await el.get_attribute("role")
            if explicit:
                return explicit
        except Exception:
            pass
        return {
            "a": "link",
            "button": "button",
            "input": "textbox",
            "textarea": "textbox",
            "select": "combobox",
            "h1": "heading",
            "h2": "heading",
            "h3": "heading",
            "img": "image",
            "form": "form",
            "label": "label",
        }.get(tag, "element")

    @staticmethod
    async def _infer_name(el: Any, tag: str) -> str:
        """Best-effort accessible name inference."""
        try:
            text = (await el.inner_text()) or ""
            if text.strip():
                return text.strip()
        except Exception:
            pass
        for attr in ("aria-label", "title", "alt", "placeholder", "value", "name", "href"):
            try:
                val = await el.get_attribute(attr)
                if val and val.strip():
                    return val.strip()
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------------
    # Action: get_content
    # ------------------------------------------------------------------

    async def _get_content(self, max_chars: int) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        if page.url in ("about:blank", ""):
            return ToolResult(
                output="Browser Error: no page loaded. Call 'navigate' first.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "NO_PAGE_LOADED"},
            )

        try:
            text = await page.evaluate(
                """() => {
                    const body = document.body;
                    if (!body) return "";
                    const clone = body.cloneNode(true);
                    clone.querySelectorAll('script, style, noscript, svg').forEach(e => e.remove());
                    return clone.innerText || "";
                }"""
            )
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: content extraction failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "CONTENT_EXTRACTION_FAILED"},
            )

        normalized = " ".join((text or "").split())
        truncated = len(normalized) > max_chars
        content = normalized[:max_chars]

        output_parts = [f"URL: {page.url}"]
        if truncated:
            output_parts.append(
                f"Note: content truncated to {max_chars} characters "
                f"(original: {len(normalized)})."
            )
        output_parts.append("")
        output_parts.append("Content:")
        output_parts.append(content)

        return ToolResult(
            output="\n".join(output_parts),
            exit_code=0,
            is_error=False,
            metadata={
                "url": page.url,
                "original_length": len(normalized),
                "returned_length": len(content),
                "truncated": truncated,
            },
        )

    # ------------------------------------------------------------------
    # Action: click
    # ------------------------------------------------------------------

    async def _click(
        self, ref: Optional[str], selector: Optional[str]
    ) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        element, error = await self._resolve_element(page, state, ref, selector)
        if error is not None:
            return error

        try:
            await element.click(timeout=10000)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: click failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "CLICK_FAILED",
                    "exception_type": type(exc).__name__,
                },
            )

        # Wait briefly for any navigation triggered by the click.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        # Layer 3: re-validate URL (click might have navigated).
        violation = await self._check_final_url(page)
        if violation is not None:
            return violation

        return ToolResult(
            output=f"Clicked element. Current URL: {page.url}",
            exit_code=0,
            is_error=False,
            metadata={"url": page.url},
        )

    # ------------------------------------------------------------------
    # Action: fill
    # ------------------------------------------------------------------

    async def _fill(
        self,
        ref: Optional[str],
        selector: Optional[str],
        value: Optional[str],
    ) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        if value is None:
            return ToolResult(
                output="Browser Error: 'value' is required for fill.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "MISSING_VALUE"},
            )

        element, error = await self._resolve_element(page, state, ref, selector)
        if error is not None:
            return error

        try:
            await element.fill(value, timeout=10000)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: fill failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={
                    "error_code": "FILL_FAILED",
                    "exception_type": type(exc).__name__,
                },
            )

        return ToolResult(
            output=f"Filled element with {len(value)} characters.",
            exit_code=0,
            is_error=False,
            metadata={"url": page.url, "value_length": len(value)},
        )

    # ------------------------------------------------------------------
    # Action: scroll
    # ------------------------------------------------------------------

    async def _scroll(self, direction: str) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        d = (direction or "down").lower()
        try:
            if d == "down":
                await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            elif d == "up":
                await page.evaluate("window.scrollBy(0, -window.innerHeight * 0.8)")
            elif d == "top":
                await page.evaluate("window.scrollTo(0, 0)")
            elif d == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            else:
                return ToolResult(
                    output=f"Browser Error: invalid direction {direction!r}.",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "INVALID_DIRECTION"},
                )
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: scroll failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "SCROLL_FAILED"},
            )

        return ToolResult(
            output=f"Scrolled {d}.",
            exit_code=0,
            is_error=False,
            metadata={"url": page.url, "direction": d},
        )

    # ------------------------------------------------------------------
    # Action: screenshot
    # ------------------------------------------------------------------

    async def _screenshot(self, full_page: bool) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        workspace = Path(self.workspace_root) if self.workspace_root else Path.cwd()
        workspace.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        filepath = workspace / filename

        try:
            await page.screenshot(path=str(filepath), full_page=full_page)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: screenshot failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "SCREENSHOT_FAILED"},
            )

        if not filepath.exists():
            return ToolResult(
                output="Browser Error: screenshot file was not created.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "SCREENSHOT_MISSING"},
            )

        size_bytes = filepath.stat().st_size

        return ToolResult(
            output=(
                f"Screenshot saved: {filename} "
                f"({size_bytes} bytes, full_page={full_page}). "
                f"View it in the Artifacts tab."
            ),
            exit_code=0,
            is_error=False,
            artifacts=[filename],
            metadata={
                "url": page.url,
                "path": str(filepath),
                "size_bytes": size_bytes,
                "full_page": full_page,
            },
        )

    # ------------------------------------------------------------------
    # Action: back / forward
    # ------------------------------------------------------------------

    async def _back(self) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        try:
            response = await page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: back navigation failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "BACK_FAILED"},
            )

        if response is None:
            return ToolResult(
                output="Browser Error: no previous page in history.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "NO_HISTORY_BACK"},
            )

        violation = await self._check_final_url(page)
        if violation is not None:
            return violation

        return ToolResult(
            output=f"Went back to {page.url}",
            exit_code=0,
            is_error=False,
            metadata={"url": page.url},
        )

    async def _forward(self) -> ToolResult:
        page, state = await self._get_page_and_state()
        state.touch()

        try:
            response = await page.go_forward(wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            return ToolResult(
                output=f"Browser Error: forward navigation failed: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "FORWARD_FAILED"},
            )

        if response is None:
            return ToolResult(
                output="Browser Error: no forward page in history.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "NO_HISTORY_FORWARD"},
            )

        violation = await self._check_final_url(page)
        if violation is not None:
            return violation

        return ToolResult(
            output=f"Went forward to {page.url}",
            exit_code=0,
            is_error=False,
            metadata={"url": page.url},
        )


__all__ = ["BrowserInteractiveArgs", "BrowserInteractiveTool"]
