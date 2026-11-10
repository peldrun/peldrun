# backend/omweb/sse_events.py
"""
Authoritative Server-Sent Events (SSE) contract and dispatching layer.
Provides:
- Strongly typed SSE event models with lifecycle and artifact enum discriminators.
- Isolated per-job listener queues guaranteeing strict FIFO delivery fan-out.
- Asynchronous event stream generator with automatic keepalive heartbeat (PING).
- Deterministic listener registration and cleanup to prevent memory leaks.
- Full support for multi-agent delegation, approval gates, and child run lifecycle streaming (Phase M3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class SSEEventType(str, Enum):
    """Authoritative SSE event types for PELDRUN runtime streaming."""

    # Control Plane Events
    STATUS = "status"
    RUN_ACCEPTED = "run_accepted"
    RUN_STARTED = "run_started"

    # Execution Plane Lifecycle Events (Owned by Core)
    STEP_START = "step_start"
    STEP_END = "step_end"
    THOUGHT = "thought"
    CHUNK = "chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALLED = "tool_called"  # Compatibility alias
    TOOL_RETRY = "tool_retry"    # Centralized retry event

    # Tool Output Streaming Events (opt-in, see TOOL_OUTPUT_STREAMING flag)
    #
    # Emitted by the runtime WHILE a tool is executing, between the
    # TOOL_CALL that started it and the OBSERVATION that closes it.
    # Both events carry `tool_call_id` so the frontend can patch the
    # correct tool card even when several tools run concurrently.
    #
    # TOOL_OUTPUT   -- raw stdout/stderr chunks. The frontend appends
    #                  `chunk` to the card's `liveOutput` buffer and
    #                  splits it into `liveStreams[stream]`. Ordering
    #                  is guaranteed per (tool_call_id, attempt) via
    #                  the `sequence` field; duplicates after an SSE
    #                  reconnect are dropped by the reducer.
    #
    # TOOL_PROGRESS -- structured progress update (current/total/
    #                  phase/message). Rendered as a progress bar,
    #                  NOT appended to the text area. Uses the same
    #                  sequence/attempt contract.
    #
    # Neither event is terminal: the tool's final output and status
    # still arrive via OBSERVATION. The frontend keeps both
    # `liveOutput` (streamed) and `output` (final) side by side; the
    # two fields are independent.
    #
    # Payload shapes:
    #   TOOL_OUTPUT:   {tool_call_id, tool_name, stream,
    #                   chunk, sequence, attempt}
    #   TOOL_PROGRESS: {tool_call_id, tool_name, chunk,
    #                   sequence, attempt,
    #                   current?, total?, phase?}
    #
    # When PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING is off (default),
    # neither event is ever emitted and existing subscribers are
    # unaffected.
    TOOL_OUTPUT = "tool_output"
    TOOL_PROGRESS = "tool_progress"

    OBSERVATION = "observation"

    # Artifact Lifecycle Events (Phase M2 Push Streaming)
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_UPDATED = "artifact_updated"
    ARTIFACT_DELETED = "artifact_deleted"
    ARTIFACT_MOVED = "artifact_moved"

    # Image Search Results Event
    #
    # Emitted after a successful image_search invocation so the frontend
    # can render a rich visual gallery independently of any Generative UI
    # rendering. The event is orthogonal to OBSERVATION: it carries
    # structured image metadata (image_url, thumbnail_url, source_url,
    # width, height, host) that is not otherwise surfaced in the text
    # observation payload.
    #
    # Two emission sites:
    #   * omweb/agent_bridge_parts/dispatcher.py  (direct chat mode)
    #   * omweb/engines/peldrun_engine.py          (agent mode, parsed
    #                                                from image_search
    #                                                observation output)
    #
    # Payload shape (data):
    #   {
    #     "query": str,
    #     "provider": str,
    #     "images": [
    #       {
    #         "title": str,
    #         "image_url": str,        # /api/search/image-proxy?... URL
    #         "thumbnail_url": str|null,
    #         "source_url": str|null,
    #         "width": int|null,
    #         "height": int|null,
    #         "source": str|null
    #       },
    #       ...
    #     ],
    #     "count": int,
    #     "latency_ms": float,
    #     "estimated_cost_usd": float
    #   }
    #
    # The frontend renders this as a responsive image grid with lazy
    # loading, lightbox, and download actions. Text in the same turn is
    # shown side-by-side; the LLM is instructed NOT to duplicate image
    # URLs in its response.
    IMAGE_RESULTS = "image_results"

    # Multi-Agent Lineage & Approval Events (Phase M3)
    DELEGATION_REQUESTED = "delegation_requested"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    CHILD_RUN_STARTED = "child_run_started"
    CHILD_RUN_STEP = "child_run_step"
    CHILD_RUN_COMPLETED = "child_run_completed"
    CHILD_RUN_FAILED = "child_run_failed"
    CHILD_RUN_CANCELLED = "child_run_cancelled"

    # Context Resolution Events
    # [FIX-5.1] Emitted by the dispatcher after the user's file references
    # (`@filename` mentions) have been resolved against the workspace.
    # The payload carries the list of refs that passed validation, allowing
    # the frontend to display a "Referenced files" chip in the timeline.
    REFS_RESOLVED = "refs_resolved"

    # MCP Authentication Events
    # Emitted by the execution engine when an MCP server requires
    # user-provided credentials (OAuth or env vars) and no decision
    # has been recorded for the current chat. The frontend responds
    # by opening the per-chat auth modal.
    #
    # Payload shape:
    #   {
    #     "server_id": "<id>",
    #     "server_name": "<display name>",
    #     "auth_method": "oauth" | "env",
    #     "env_schema": {...} | None,
    #     "oauth_supported": bool,
    #     "chat_id": "<id>",
    #     "model": "<model name>"
    #   }
    MCP_AUTH_REQUIRED = "mcp_auth_required"

    # Terminal Events
    FINAL = "final"
    ERROR = "error"
    CANCELLED = "cancelled"
    DONE = "done"

    # Transport / Heartbeat Events
    PING = "ping"


class SSEEvent(BaseModel):
    """Data payload transfer object for Server-Sent Events."""

    model_config = ConfigDict(extra="allow")

    type: SSEEventType
    step: int = Field(default=0)
    data: Dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        """
        Serialize event envelope into a JSON string formatted for HTTP transport.
        Ensures compatibility with downstream FastAPI event routers.
        """
        type_str = self.type.value if hasattr(self.type, "value") else str(self.type)
        return json.dumps(
            {
                "type": type_str,
                "step": self.step,
                "data": self.data,
            },
            ensure_ascii=False,
        )


# In-memory per-job event listeners for SSE streaming
_job_listeners: Dict[str, List[asyncio.Queue[SSEEvent]]] = {}
_listeners_lock = asyncio.Lock()


async def register_job_listener(job_id: str) -> asyncio.Queue[SSEEvent]:
    """Register a new SSE stream listener queue for a specific job."""
    async with _listeners_lock:
        queue: asyncio.Queue[SSEEvent] = asyncio.Queue()
        _job_listeners.setdefault(job_id, []).append(queue)
        return queue


async def unregister_job_listener(job_id: str, queue: asyncio.Queue[SSEEvent]) -> None:
    """Unregister an SSE stream listener queue and purge empty containers."""
    async with _listeners_lock:
        listeners = _job_listeners.get(job_id, [])
        if queue in listeners:
            listeners.remove(queue)
        if not listeners:
            _job_listeners.pop(job_id, None)


async def dispatch_event(job_id: str, event: SSEEvent) -> None:
    """Dispatch an SSE event to all connected listeners for the given job."""
    async with _listeners_lock:
        listeners = list(_job_listeners.get(job_id, []))

    for queue in listeners:
        try:
            queue.put_nowait(event)
        except Exception as exc:
            logger.warning(f"[SSE] Failed to enqueue event for job {job_id}: {exc}")


async def dispatch_terminal_event(job_id: str, event: SSEEvent) -> None:
    """
    Dispatch a TERMINAL event, bypassing any pending queue backlog.

    ## Why this exists

    ``dispatch_event`` uses FIFO semantics. When a user clicks Stop while
    the LLM stream is actively producing chunks, the queue contains many
    ``chunk`` events. A terminal ``cancelled`` event dispatched via
    ``dispatch_event`` lands at the END of the queue, so the SSE generator
    (``streaming.py``) must process every pending chunk before it sees the
    cancellation. The result is a 10+ second delay between the user's
    click and the UI reflecting "cancelled" — during which the UI appears
    frozen on "STOPPING…".

    ## What this does

    1. Iterates every listener queue for the given job.
    2. Drains all pending non-terminal events (discarding them).
    3. Enqueues the terminal event as the first (and only) item.

    ## When to use

    Use this ONLY for events that end the SSE stream:
      * ``CANCELLED`` — user-initiated or system-initiated stop.
      * ``FINAL`` — successful completion (typically arrives when the
        LLM stream is already idle, but safe to use anyway).
      * ``ERROR`` — unrecoverable failure.
      * ``DONE`` — alias for ``final``.
      * ``MCP_AUTH_REQUIRED`` — terminal for the current run.

    Non-terminal events (``status``, ``thought``, ``chunk``, ``step_start``,
    etc.) MUST continue to use ``dispatch_event``. Draining the queue for
    a non-terminal event would cause the user to miss those events.

    ## Interaction with streaming.py

    After this function enqueues the terminal event, the SSE generator
    (which is awaiting ``queue.get()``) receives it on the very next
    iteration. ``streaming.py`` then persists the final session state
    and terminates the response. No changes needed in ``streaming.py``.

    ## Interaction with control.py

    ``control.py`` calls this function for the ``cancelled`` event. Other
    producers (``dispatcher.py`` for ``final``/``error``) can keep using
    ``dispatch_event`` because the LLM stream is already idle by the time
    those fire — but switching them is harmless and future-proof.
    """
    async with _listeners_lock:
        listeners = list(_job_listeners.get(job_id, []))

    for queue in listeners:
        # Drain any pending non-terminal events.
        drained = 0
        while not queue.empty():
            try:
                queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        # Enqueue the terminal event as the only item.
        try:
            queue.put_nowait(event)
            if drained > 0:
                logger.info(
                    "[SSE] Dropped %d pending event(s) before terminal %s "
                    "for job %s",
                    drained,
                    event.type.value
                    if hasattr(event.type, "value")
                    else event.type,
                    job_id,
                )
        except Exception as exc:
            logger.warning(
                "[SSE] Failed to enqueue terminal event for job %s: %s",
                job_id,
                exc,
            )


async def subscribe_events(
    job_id: str,
    keepalive_interval: float = 15.0,
) -> AsyncGenerator[SSEEvent, None]:
    """Asynchronous generator yielding live SSEEvents for a specified job."""
    queue = await register_job_listener(job_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
                yield event
                if event.type in (
                    SSEEventType.FINAL,
                    SSEEventType.ERROR,
                    SSEEventType.CANCELLED,
                    SSEEventType.DONE,
                    # MCP_AUTH_REQUIRED is terminal from the stream's
                    # perspective: the underlying job has already been
                    # failed server-side (see resolve_tool_runtime's
                    # lazy MCP auth check). No further events will arrive
                    # on this stream, so the generator must exit rather
                    # than keep emitting PING heartbeats forever.
                    SSEEventType.MCP_AUTH_REQUIRED,
                ):
                    break
            except asyncio.TimeoutError:
                yield SSEEvent(
                    type=SSEEventType.PING,
                    step=0,
                    data={"ping": True, "timestamp": asyncio.get_event_loop().time()},
                )
    finally:
        await unregister_job_listener(job_id, queue)


__all__ = [
    "SSEEventType",
    "SSEEvent",
    "register_job_listener",
    "unregister_job_listener",
    "dispatch_event",
    "dispatch_terminal_event",
    "subscribe_events",
]
