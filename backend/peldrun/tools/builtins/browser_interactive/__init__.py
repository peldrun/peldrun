# backend/peldrun/tools/builtins/browser_interactive/__init__.py
"""
PELDRUN Interactive Browser Tool Package.

This package provides a real Chromium-based browser tool for autonomous
agents. It is intentionally split into three modules:

  * ``security``  — SSRF protection and route-level URL validation.
  * ``session``   — singleton BrowserSessionManager managing a single
                    shared Chromium browser and per-run BrowserContexts.
  * ``tool``      — the ``BrowserInteractiveTool`` (BaseTool subclass)
                    exposing the public action surface to agents.

Importing this package does NOT launch Playwright or a browser. The
session manager is lazily initialized on first tool invocation, so that
importing PELDRUN in a test environment or on a host without Chromium
installed does not fail.
"""

from __future__ import annotations

from .tool import BrowserInteractiveTool
from .session import BrowserSessionManager, get_session_manager
from .security import (
    SSRFProtectionError,
    is_safe_url,
    validate_url_or_raise,
)

__all__ = [
    "BrowserInteractiveTool",
    "BrowserSessionManager",
    "get_session_manager",
    "SSRFProtectionError",
    "is_safe_url",
    "validate_url_or_raise",
]
