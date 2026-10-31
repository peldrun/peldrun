# backend/omweb/_unicode_setup.py
"""
Force UTF-8 encoding on all console streams and logging handlers.

On Windows, sys.stdout and sys.stderr default to cp1252 (or ascii in
some terminal contexts). Any provider that returns non-ASCII content
(Jina, SearXNG, Google, Yandex, Brave) triggers UnicodeEncodeError the
moment that content is logged or printed — surfacing as a spurious
"provider failure" even though the HTTP request itself succeeded.

This module MUST be imported before any other module that emits
output (imports, logging configuration, uvicorn setup). Call
``install_utf8_console()`` at the very top of ``omweb/main.py``.

The install is idempotent and safe to call multiple times.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from typing import Optional

# Set UTF-8 mode for any child subprocesses or libraries that read
# these variables at import time (httpx, httpcore, h11, mcp, etc.).
# The parent process itself already started with whatever encoding it
# has, so these only help subprocesses — but they cost nothing and
# prevent a class of downstream UnicodeEncodeError issues.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")


def _reconfigure_stream(stream: Optional[object]) -> Optional[object]:
    """
    Force UTF-8 encoding on a single text stream.

    Strategy:
      1. Prefer TextIOWrapper.reconfigure() — mutates in place, so every
         handler holding a reference to the same object benefits.
      2. Fall back to wrapping the underlying buffer with a fresh
         UTF-8 TextIOWrapper for streams that don't support reconfigure.
      3. If neither works, return the stream unchanged (best effort).
    """
    if stream is None:
        return None

    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
            return stream
        except Exception:
            pass

    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            return io.TextIOWrapper(
                buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
        except Exception:
            pass

    return stream


def _reconfigure_handler(handler: logging.Handler) -> None:
    """Force UTF-8 on a logging StreamHandler's stream, if applicable."""
    if not isinstance(handler, logging.StreamHandler):
        return
    stream = getattr(handler, "stream", None)
    if stream is None:
        return
    if stream is sys.stdout or stream is sys.stderr:
        # Already reconfigured in place by _reconfigure_stream.
        return
    new_stream = _reconfigure_stream(stream)
    if new_stream is not None and new_stream is not stream:
        handler.stream = new_stream  # type: ignore[assignment]


def install_utf8_console() -> None:
    """
    Install UTF-8 encoding on sys.stdout, sys.stderr, and every existing
    logging StreamHandler. Idempotent and exception-safe.

    Call this as the very first executable statement in the ASGI entry
    point (``omweb/main.py``), before any logging or printing occurs.
    """
    # 1. Reconfigure the canonical streams in place.
    new_stdout = _reconfigure_stream(sys.stdout)
    if new_stdout is not None:
        sys.stdout = new_stdout  # type: ignore[assignment]
    new_stderr = _reconfigure_stream(sys.stderr)
    if new_stderr is not None:
        sys.stderr = new_stderr  # type: ignore[assignment]

    # 2. Walk every logging handler in the root and named loggers.
    for handler in list(logging.root.handlers):
        _reconfigure_handler(handler)

    for logger_name in list(logging.root.manager.loggerDict.keys()):
        logger = logging.getLogger(logger_name)
        for handler in list(logger.handlers):
            _reconfigure_handler(handler)

    # 3. Silence verbose HTTP client loggers that would otherwise try to
    #    write full request/response bodies (including non-ASCII) to the
    #    console. Only WARNING and above are useful in production; DEBUG
    #    request/response logging is what triggers UnicodeEncodeError on
    #    Windows when the upstream returns Cyrillic/Arabic/CJK text.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)


__all__ = ["install_utf8_console"]
