# backend/peldrun/tools/builtins/browser_interactive/session.py
"""
Browser Session Manager for the Interactive Browser Tool.

Manages a single shared Chromium browser process and per-run isolated
BrowserContexts. This architecture provides three key properties:

  * Performance: Launching Chromium takes ~1-2 seconds. Sharing one
    browser across runs amortizes this cost while still isolating state.

  * Isolation: Each ``run_id`` gets its own BrowserContext (cookies,
    localStorage, cache are all isolated). Two concurrent agents cannot
    see each other's authenticated sessions.

  * Lifecycle: Idle contexts are closed automatically after
    ``IDLE_TIMEOUT_SECONDS`` (default: 15 minutes) to reclaim memory
    without disrupting active runs.

Playwright is imported lazily to allow PELDRUN to start on hosts
without Chromium installed. Importing this module never launches a
browser; the singleton is initialized on first use.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .security import make_route_handler

logger = logging.getLogger("peldrun.tools.builtins.browser_interactive.session")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IDLE_TIMEOUT_SECONDS: int = 15 * 60  # 15 minutes
CLEANUP_INTERVAL_SECONDS: int = 60   # check every minute
DEFAULT_VIEWPORT: dict[str, int] = {"width": 1280, "height": 720}
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 PELDRUN/1.0"
)


# ---------------------------------------------------------------------------
# Per-context state
# ---------------------------------------------------------------------------

@dataclass
class _ContextState:
    """Holds a BrowserContext and the last-activity timestamp."""

    context: Any  # playwright.async_api.BrowserContext
    last_used: float = field(default_factory=time.monotonic)
    ref_map: dict[str, Any] = field(default_factory=dict)
    """
    Maps stable element references (e.g. 'e7') to Playwright ElementHandle
    objects. Populated by 'snapshot' and consumed by 'click'/'fill'.
    Cleared whenever a new snapshot is taken (refs are snapshot-scoped).
    """

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used


# ---------------------------------------------------------------------------
# Browser Session Manager
# ---------------------------------------------------------------------------

class BrowserSessionManager:
    """
    Owns the shared Chromium browser and all per-run contexts.

    Usage::

        mgr = get_session_manager()
        ctx_state = await mgr.get_context("run_abc")
        page = await ctx_state.context.new_page()
        ...
        ctx_state.touch()
    """

    def __init__(self) -> None:
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._contexts: dict[str, _ContextState] = {}
        self._browser_lock = asyncio.Lock()
        self._contexts_lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> Any:
        """
        Lazily launch the shared Chromium browser.

        Safe to call concurrently. The lock ensures exactly one launch
        happens even if 10 coroutines call this simultaneously.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        async with self._browser_lock:
            # Re-check after acquiring the lock (another coroutine may
            # have finished launching while we waited).
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            if self._closed:
                raise RuntimeError(
                    "BrowserSessionManager has been shut down and cannot "
                    "launch new browsers."
                )

            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright is not installed. Run: "
                    "pip install playwright && playwright install chromium"
                ) from exc

            logger.info("Launching shared Chromium browser for browser_interactive.")
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                ],
            )
            logger.info("Chromium browser launched (pid=%s).", self._browser.version)

            # Start the idle-context reaper once.
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            return self._browser

    async def close(self) -> None:
        """
        Gracefully shut down all contexts, the browser, and Playwright.
        Called on application shutdown.
        """
        async with self._contexts_lock:
            self._closed = True
            for run_id, state in list(self._contexts.items()):
                try:
                    await state.context.close()
                except Exception as exc:
                    logger.warning(
                        "Failed to close context for run %s: %s", run_id, exc
                    )
            self._contexts.clear()

        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None

        if self._browser is not None:
            try:
                # Explicitly disconnect to prevent Windows Proactor deadlocks
                await self._browser.close()
            except Exception as exc:
                logger.warning("Failed to close browser: %s", exc)
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning("Failed to stop Playwright: %s", exc)
            self._playwright = None

        logger.info("BrowserSessionManager shut down cleanly.")
    # ------------------------------------------------------------------
    # Context acquisition
    # ------------------------------------------------------------------

    async def get_context(self, run_id: str) -> _ContextState:
        """
        Return the BrowserContext state for the given run_id, creating
        a fresh isolated context if none exists.

        The returned object is a ``_ContextState`` with attributes:
          * ``context``: the Playwright BrowserContext.
          * ``last_used``: monotonic timestamp of last activity.
          * ``ref_map``: dict of element refs from the last snapshot.
          * ``touch()``: refresh last-used timestamp.
        """
        if not run_id:
            raise ValueError("run_id is required to acquire a browser context.")

        # Fast path: existing context.
        async with self._contexts_lock:
            existing = self._contexts.get(run_id)
            if existing is not None:
                # Verify the underlying context hasn't been closed externally.
                try:
                    _ = existing.context.pages  # raises if closed
                    existing.touch()
                    return existing
                except Exception:
                    # Stale context — drop it and create a fresh one.
                    self._contexts.pop(run_id, None)

        # Slow path: create a new context.
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport=DEFAULT_VIEWPORT,
            user_agent=DEFAULT_USER_AGENT,
            ignore_https_errors=False,
            java_script_enabled=True,
        )

        # Apply SSRF protection on EVERY request.
        await context.route("**/*", make_route_handler())

        state = _ContextState(context=context)

        async with self._contexts_lock:
            self._contexts[run_id] = state

        logger.info("Created browser context for run %s.", run_id)
        return state

    async def close_context(self, run_id: str) -> bool:
        """Explicitly close the context for the given run_id."""
        async with self._contexts_lock:
            state = self._contexts.pop(run_id, None)
        if state is None:
            return False
        try:
            await state.context.close()
        except Exception as exc:
            logger.warning(
                "Error closing context for run %s: %s", run_id, exc
            )
        logger.info("Closed browser context for run %s.", run_id)
        return True

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """
        Periodically close idle contexts. Runs every
        CLEANUP_INTERVAL_SECONDS. Should never crash the manager.
        """
        try:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                try:
                    await self._reap_idle_contexts()
                except Exception as exc:
                    logger.warning("Idle context reaper iteration failed: %s", exc)
        except asyncio.CancelledError:
            return

    async def _reap_idle_contexts(self) -> None:
        """Close any contexts idle for longer than IDLE_TIMEOUT_SECONDS."""
        stale_ids: list[str] = []
        async with self._contexts_lock:
            for run_id, state in self._contexts.items():
                if state.idle_seconds >= IDLE_TIMEOUT_SECONDS:
                    stale_ids.append(run_id)

        for run_id in stale_ids:
            logger.info(
                "Closing idle context for run %s (idle %.0fs).",
                run_id,
                IDLE_TIMEOUT_SECONDS,
            )
            await self.close_context(run_id)

    # ------------------------------------------------------------------
    # Introspection (for tests and diagnostics)
    # ------------------------------------------------------------------

    def active_run_ids(self) -> list[str]:
        return list(self._contexts.keys())

    def __len__(self) -> int:
        return len(self._contexts)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_session_manager: Optional[BrowserSessionManager] = None


def get_session_manager() -> BrowserSessionManager:
    """Return the process-wide BrowserSessionManager singleton."""
    global _session_manager
    if _session_manager is None:
        _session_manager = BrowserSessionManager()
    return _session_manager


__all__ = [
    "BrowserSessionManager",
    "get_session_manager",
    "_ContextState",
    "IDLE_TIMEOUT_SECONDS",
]
