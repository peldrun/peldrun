"""
backend/omweb/sse_events.py

Authoritative Server-Sent Events (SSE) contract and dispatching layer.
Provides:
- Strongly typed SSE event models with lifecycle and artifact enum discriminators.
- Isolated per-job listener queues guaranteeing strict FIFO delivery fan-out.
- Asynchronous event stream generator with automatic keepalive heartbeat (PING).
- Deterministic listener registration and cleanup to prevent memory leaks.
- Full support for TOOL_RETRY and CANCELLED event discriminators.
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
    TOOL_CALL = "tool_call"
    TOOL_CALLED = "tool_called"  # Compatibility alias
    TOOL_RETRY = "tool_retry"    # Centralized retry event
    OBSERVATION = "observation"

    # Artifact Lifecycle Events (Phase M2 Push Streaming)
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_UPDATED = "artifact_updated"
    ARTIFACT_DELETED = "artifact_deleted"
    ARTIFACT_MOVED = "artifact_moved"

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
    """
    Register a new SSE stream listener queue for a specific job.

    Args:
        job_id: Unique identifier of the job being monitored.

    Returns:
        An isolated asyncio.Queue receiving live dispatched SSEEvents.
    """
    async with _listeners_lock:
        queue: asyncio.Queue[SSEEvent] = asyncio.Queue()
        _job_listeners.setdefault(job_id, []).append(queue)
        return queue


async def unregister_job_listener(job_id: str, queue: asyncio.Queue[SSEEvent]) -> None:
    """
    Unregister an SSE stream listener queue and purge empty containers.

    Args:
        job_id: Unique identifier of the job.
        queue: Active queue instance to remove.
    """
    async with _listeners_lock:
        listeners = _job_listeners.get(job_id, [])
        if queue in listeners:
            listeners.remove(queue)
        if not listeners:
            _job_listeners.pop(job_id, None)


async def dispatch_event(job_id: str, event: SSEEvent) -> None:
    """
    Dispatch an SSE event to all connected listeners for the given job.
    Guarantees non-blocking fan-out across all active stream consumers.

    Args:
        job_id: Target job identifier.
        event: SSEEvent payload to broadcast.
    """
    async with _listeners_lock:
        listeners = list(_job_listeners.get(job_id, []))

    for queue in listeners:
        try:
            queue.put_nowait(event)
        except Exception as exc:
            logger.warning(f"[SSE] Failed to enqueue event for job {job_id}: {exc}")


async def subscribe_events(
    job_id: str,
    keepalive_interval: float = 15.0,
) -> AsyncGenerator[SSEEvent, None]:
    """
    Asynchronous generator yielding live SSEEvents for a specified job.

    Maintains a dedicated listener queue and periodically emits keepalive PING events
    during idle intervals. Automatically cleans up listener registrations when
    consumer finishes or connection drops.
    """
    queue = await register_job_listener(job_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=keepalive_interval)
                yield event

                # Stop iteration upon reaching a canonical terminal state
                if event.type in (
                    SSEEventType.FINAL,
                    SSEEventType.ERROR,
                    SSEEventType.CANCELLED,
                    SSEEventType.DONE,
                ):
                    break
            except asyncio.TimeoutError:
                # Dispatch keepalive heartbeat
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
    "subscribe_events",
]