"""
Text sanitization helpers for the PELDRUN Universal Agent Bridge.
"""

from __future__ import annotations

import re
from typing import Any


def sanitize_final_result_text(raw_text: Any, latest_thought: str = "") -> str:
    """Sanitize a raw final result string before showing it to the user.

    Prevents internal ToolResult dumps (e.g. ``output='...'`` /
    ``'terminated': True``) from leaking into the user-facing chat.

    Args:
        raw_text:       The raw result string (may be a ToolResult repr or None).
        latest_thought: Fallback text used when sanitization strips everything.

    Returns:
        A clean, user-facing result string.
    """
    if not raw_text:
        return latest_thought or "Task completed successfully."

    text = str(raw_text).strip()
    if text.startswith("output=") or "ToolResult" in text or "'terminated': True" in text:
        m = re.search(r"output=['\"](.*?)['\"]", text)
        if m:
            extracted = m.group(1).strip()
            if extracted and extracted != "Task execution finished with status: success":
                return extracted
        if latest_thought:
            return latest_thought
        return "Task completed successfully."

    return text