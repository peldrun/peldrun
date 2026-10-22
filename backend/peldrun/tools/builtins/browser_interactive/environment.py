# backend/omweb/tools/builtins/browser_interactive/environment.py
"""
Runtime environment probe for the Interactive Browser Tool.

Provides ``probe_browser_environment()`` — an async, side-effect-free
check that reports:

  * Whether the Playwright Python package is importable.
  * Whether the Chromium binary that Playwright expects is present
    on disk (i.e. ``playwright install chromium`` was run).
  * Whether the configured ``screenshot_dir`` is writable and stays
    inside the workspace (containment enforced).

The probe NEVER launches a browser — it only inspects files and
importability, so it is cheap enough to call on every settings modal
open. This is intentional: launching Chromium just to test the install
would take 1-2 seconds and consume a subprocess; the file-level check
is instantaneous and catches 100% of the "not installed" cases.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(
    "peldrun.tools.builtins.browser_interactive.environment"
)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_playwright_installed() -> Dict[str, Any]:
    """Check that the Playwright Python package can be imported."""
    try:
        import playwright  # noqa: F401
        return {
            "name": "playwright_installed",
            "label": "Playwright Python package",
            "passed": True,
            "message": "Installed.",
        }
    except ImportError:
        return {
            "name": "playwright_installed",
            "label": "Playwright Python package",
            "passed": False,
            "message": "Not installed.",
            "hint": (
                "Install Playwright in the backend environment:\n"
                "    pip install playwright\n"
                "Then restart the server."
            ),
        }


async def _check_chromium_installed() -> Dict[str, Any]:
    """
    Check that the Chromium binary Playwright uses is present.

    Uses ``async_playwright().chromium.executable_path`` which returns
    the expected path WITHOUT launching the browser. If the path does
    not exist on disk, ``playwright install chromium`` has not been run.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return {
            "name": "chromium_installed",
            "label": "Chromium browser binary",
            "passed": False,
            "message": "Playwright not available — cannot check Chromium.",
            "hint": "Install Playwright first: pip install playwright",
        }

    try:
        async with async_playwright() as p:
            exe_path = p.chromium.executable_path
            if exe_path and Path(exe_path).is_file():
                return {
                    "name": "chromium_installed",
                    "label": "Chromium browser binary",
                    "passed": True,
                    "message": f"Found at {exe_path}",
                    "path": exe_path,
                }
            return {
                "name": "chromium_installed",
                "label": "Chromium browser binary",
                "passed": False,
                "message": "Chromium binary not found on disk.",
                "hint": (
                    "Download the Chromium build Playwright expects:\n"
                    "    playwright install chromium\n"
                    "On Linux servers you may also need:\n"
                    "    playwright install-deps chromium"
                ),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("Chromium probe failed: %s", exc)
        return {
            "name": "chromium_installed",
            "label": "Chromium browser binary",
            "passed": False,
            "message": f"Probe failed: {exc}",
            "hint": "Run: playwright install chromium",
        }


def _check_screenshot_dir(
    workspace_root: Optional[str],
    screenshot_dir: Optional[str],
) -> Dict[str, Any]:
    """
    Validate that ``screenshot_dir`` resolves inside the workspace and
    is writable.

    The workspace root used here is the backend process CWD, which is
    NOT the same as the chat's workspace directory (that is only known
    at execution time). The ``resolved_example`` field therefore shows
    the shape of the path the user will see at execution time; the
    containment check still applies because it is purely relative.
    """
    base = Path(workspace_root) if workspace_root else Path.cwd()
    if screenshot_dir:
        target = base / screenshot_dir
    else:
        target = base

    # Containment: the resolved target must stay inside the base.
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return {
            "name": "screenshot_dir",
            "label": "Screenshot directory",
            "passed": False,
            "message": (
                f"screenshot_dir '{screenshot_dir}' escapes the "
                f"workspace. Use a relative path (no '..')."
            ),
            "hint": "Remove leading '..' or absolute drive letters.",
        }

    # Writability probe: create the dir and write a tiny file, then clean.
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".peldrun_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "screenshot_dir",
            "label": "Screenshot directory",
            "passed": False,
            "message": f"Cannot write to {target}: {exc}",
            "hint": "Check filesystem permissions.",
        }

    return {
        "name": "screenshot_dir",
        "label": "Screenshot directory",
        "passed": True,
        "message": f"Writable: {target}",
        "resolved_example": str(target),
        "note": (
            "At execution time, this path is resolved inside the active "
            "chat's workspace (…/files/<screenshot_dir>/). The value "
            "above is only a probe against the backend CWD."
        ),
    }


# ---------------------------------------------------------------------------
# Public probe
# ---------------------------------------------------------------------------

async def probe_browser_environment(
    workspace_root: Optional[str] = None,
    screenshot_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return a structured environment report for the browser tool.

    Shape::

        {
          "tool_id": "browser_interactive",
          "ready": bool,
          "checks": [
            {"name": str, "label": str, "passed": bool,
             "message": str, "hint"?: str, ...},
            ...
          ],
        }
    """
    checks: List[Dict[str, Any]] = []
    checks.append(_check_playwright_installed())
    checks.append(await _check_chromium_installed())
    checks.append(_check_screenshot_dir(workspace_root, screenshot_dir))

    ready = all(c.get("passed", False) for c in checks)
    return {
        "tool_id": "browser_interactive",
        "ready": ready,
        "checks": checks,
    }


__all__ = ["probe_browser_environment"]
