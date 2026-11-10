# Path: backend/peldrun/tools/sdk.py
"""
PELDRUN Tool SDK
================

Public entry point for third-party tool authors. Import from this module
rather than from internal ``peldrun.tools.*`` modules: the symbols re-
exported here form a stable contract, and the underlying implementation
modules may be refactored between releases without notice.

What this SDK provides
----------------------

* ``BaseTool``        -- the class every tool must subclass.
* ``ToolResult``      -- the structured return value every ``_arun`` must
                         produce (or a compatible dict/str, which the
                         framework coerces).
* ``ToolExecutionContext`` -- the opt-in hook for streaming intermediate
                         output WHILE the tool is running.
* ``ToolOutputChunk`` -- the wire shape of a single streamed fragment
                         (informational; tools do not construct it
                         directly, the framework does).

Quick start (no streaming)
--------------------------

    from peldrun.tools.sdk import BaseTool, ToolResult

    class EchoTool(BaseTool):
        name = "echo"
        description = "Echo the input back to the caller."

        async def _arun(self, text: str) -> ToolResult:
            return ToolResult(output=text, exit_code=0)

Quick start (with streaming)
----------------------------

    from peldrun.tools.sdk import BaseTool, ToolResult

    class SlowReportTool(BaseTool):
        name = "slow_report"
        description = "Generate a report, emitting progress as it goes."

        async def _arun(
            self,
            topic: str,
            context=None,   # <-- opt-in: name MUST be exactly "context"
        ) -> ToolResult:
            if context is not None:
                await context.emit_output(
                    f"Preparing report for {topic!r}...\\n",
                    stream="stdout",
                )
                await context.emit_progress(
                    current=0, total=100, phase="prepare",
                )

            chunks = [f"section {i}\\n" for i in range(1, 6)]
            for i, chunk in enumerate(chunks, start=1):
                await context.emit_output(chunk, stream="stdout")
                await context.emit_progress(
                    current=i * 20, total=100, phase="generate",
                )

            return ToolResult(
                output="Report complete.\\n",
                exit_code=0,
            )

Opt-in mechanics
----------------

The framework inspects the signature of ``_arun`` before forwarding the
context. Two conditions must BOTH be true for a tool to receive a live
context:

1. The deployment must have set
   ``PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING=1`` (or ``true`` / ``yes`` /
   ``on``). This is a global kill-switch; when off, ALL tools -- including
   ones that opted in -- run without a context.

2. The tool's ``_arun`` must declare an explicit ``context`` parameter
   (by name). A tool whose signature is ``_arun(self, **kwargs)`` is NOT
   considered opt-in: the framework refuses to inject ``context`` into
   ``**kwargs`` because that would leak it into the tool's domain
   arguments. This is a deliberate guardrail.

When both conditions hold, ``context`` is a live ``ToolExecutionContext``
instance. When either fails, ``context`` is ``None``. Tools that opt in
must therefore treat ``context`` as optional:

    if context is not None:
        await context.emit_output("hello")

This is not defensive style -- it is the required pattern. The feature
flag exists precisely so that operators can disable streaming without
patching every tool.

Synchronous tools
-----------------

A tool whose ``_arun`` runs on a worker thread (via the framework's
``asyncio.to_thread`` offload) cannot ``await`` the async emit methods.
Such tools MUST use the thread-safe sync variants instead:

    context.emit_output_sync("chunk", stream="stdout")
    context.emit_progress_sync(current=1, total=10, phase="init")

These methods are duck-typed -- they are not part of the
``ToolExecutionContext`` Protocol (which is async-only by design, so
that the async contract stays minimal and clear). A tool that is
unsure whether it runs on the main loop or a worker thread should
prefer the async variant; the framework only offloads tools that are
themselves synchronous end-to-end.

Contract guarantees
-------------------

``emit_output`` and ``emit_progress`` (async) and
``emit_output_sync`` / ``emit_progress_sync`` (sync) MUST NOT raise.
If the underlying event queue is full, the run is cancelled, or the
event loop has shut down, the chunk is silently dropped. The tool is
never failed because of a streaming problem -- losing a progress tick
is always preferable to aborting a running tool.

The consumer is guaranteed to see chunks in the order they were emitted
per (tool_call_id, attempt) pair: the context assigns a strictly
monotonic ``sequence`` to each chunk, and the agent drains the queue
FIFO. If an SSE reconnect happens mid-stream, the frontend reducer
drops chunks whose ``sequence`` is not greater than the last one it
processed, so no duplicates are ever rendered.

Versioning
----------

``__version__`` is the SDK contract version, NOT the PELDRUN platform
version. It bumps on any non-backward-compatible change to the symbols
exported here. Tool authors may pin against it:

    from peldrun.tools import sdk as peldrun_sdk
    if not peldrun_sdk.is_compatible("1.x"):
        raise RuntimeError("This tool requires PELDRUN SDK 1.x")

``is_compatible`` accepts ``"<major>.x"`` or an exact version string,
and is intentionally permissive -- it errs on the side of running the
tool rather than rejecting it, unless the major version explicitly
differs.
"""

from __future__ import annotations

from typing import Union

# Public surface: contract types.
from peldrun.tools.contract import (
    ToolExecutionContext,
    ToolOutputChunk,
)

# Public surface: base classes and value types a tool must interact with.
from peldrun.tools.base import BaseTool
from peldrun.tools.contract import ToolResult


# ---------------------------------------------------------------------------
# SDK contract version
# ---------------------------------------------------------------------------
#
# History:
#   1.0.0 -- Initial release. Re-exports BaseTool, ToolResult,
#            ToolExecutionContext, ToolOutputChunk. Adds is_compatible.
#
# When a breaking change is made to any exported symbol, the major
# component is bumped and a migration note is appended here.
SDK_VERSION: str = "1.0.0"
SDK_MAJOR: int = 1


def is_compatible(constraint: str) -> bool:
    """
    Return True if this SDK satisfies ``constraint``.

    Supported forms
    ---------------
    * ``"<major>.x"``  -- matches any ``<major>.y.z`` (e.g. ``"1.x"``).
    * ``"<major>"``    -- shorthand for ``"<major>.x"``.
    * ``"<major>.<minor>.<patch>"`` -- exact version match.

    Unknown forms return True (permissive default): the intent is to
    run the tool, and a malformed constraint is more likely a typo
    than a genuine incompatibility signal.

    The comparison is intentionally minimal. SDK authors who need a
    full semver range library should depend on ``packaging`` directly;
    this helper covers the common case without pulling in a dependency.
    """
    raw = (constraint or "").strip().lower()
    if not raw:
        return True

    # "<major>.x" or "<major>.X"
    if raw.endswith(".x"):
        head = raw[:-2].strip()
        if not head:
            return True
        try:
            return int(head) == SDK_MAJOR
        except ValueError:
            return True

    # Bare "<major>"
    if "." not in raw:
        try:
            return int(raw) == SDK_MAJOR
        except ValueError:
            return True

    # Exact "<major>.<minor>.<patch>"
    parts = raw.split(".")
    if len(parts) == 3:
        try:
            major = int(parts[0])
            minor = int(parts[1])
            patch = int(parts[2])
        except ValueError:
            return True
        if major != SDK_MAJOR:
            return False
        sdk_parts = SDK_VERSION.split(".")
        try:
            sdk_minor = int(sdk_parts[1])
            sdk_patch = int(sdk_parts[2])
        except (IndexError, ValueError):
            return True
        return (minor, patch) == (sdk_minor, sdk_patch)

    # Unknown shape -- permissive.
    return True


__all__ = [
    # Contract types
    "ToolExecutionContext",
    "ToolOutputChunk",
    # Base classes and value types
    "BaseTool",
    "ToolResult",
    # Versioning
    "SDK_VERSION",
    "SDK_MAJOR",
    "is_compatible",
]
