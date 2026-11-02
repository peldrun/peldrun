# Path: backend/omweb/routers/run_routes/control.py
"""
Job control endpoints.

Exposes operations that alter the lifecycle of a running job:

* ``POST /jobs/{job_id}/stop``   - stop/cancel a running job.
* ``POST /jobs/{job_id}/cancel`` - alias for ``stop``.
* ``POST /jobs/{job_id}/pause``  - pause a running job.
* ``POST /jobs/{job_id}/resume`` - resume a paused job.

All operations follow the canonical state contract defined in
``docs/state-contracts.md``:

    * Pause dispatches an SSE ``status`` event with ``data.status="paused"``.
    * Resume dispatches an SSE ``status`` event with ``data.status="running"``.
    * Stop dispatches a terminal ``cancelled`` SSE event and persists
      ``cancelled`` (NOT ``stopped``) as the session status.

Historical bugs fixed in this revision
--------------------------------------
    * ``dispatch_event`` was imported from ``omweb.agent_bridge`` — which
      does not export it. The import lived inside a ``try/except`` block,
      so every SSE dispatch failed silently and connected clients never
      received control events.
    * ``getattr(SSEEventType, "PAUSED", "paused")`` returned the string
      ``"paused"`` instead of an enum member, causing Pydantic validation
      to fail on dispatch. Replaced with ``SSEEventType.STATUS`` and a
      ``data.status`` payload, which is the canonical form.
    * ``status="stopped"`` was persisted to ``session.json`` and to
      ``job_manager``. Replaced with the canonical ``"cancelled"``.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter

from omweb.job_manager import job_manager
from omweb.project_manager import project_manager, _atomic_write_json
from omweb.sse_events import (
    SSEEvent,
    SSEEventType,
    dispatch_event,
    dispatch_terminal_event,
)

from .schemas import JobControlResponse
from .state import ACTIVE_JOB_TASKS
from .utils import resolve_chat


logger = logging.getLogger("omweb.routers.run_routes.control")

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _dispatch_control_event(
    job_id: str,
    event_type: SSEEventType,
    data: dict,
) -> None:
    """
    Best-effort dispatch of a control event to the SSE bus.

    Failures are logged at WARNING level so operators can diagnose them,
    but the caller never raises — the HTTP response reflects the actual
    state change (which has already been applied), not the best-effort
    notification. A dispatched event that fails to reach a listener is
    equivalent to a listener that was not connected; it is not a job
    control failure.
    """
    try:
        await dispatch_event(
            job_id,
            SSEEvent(type=event_type, step=0, data=data),
        )
    except Exception as exc:
        logger.warning(
            "Failed to dispatch %s event for job %s: %s",
            event_type,
            job_id,
            exc,
        )




async def _dispatch_terminal_event(
    job_id: str,
    event_type: SSEEventType,
    data: dict,
) -> None:
    """
    Dispatch a terminal event, bypassing any pending queue backlog.

    This is used for events that end the SSE stream (notably
    ``CANCELLED``). Unlike ``_dispatch_control_event``, this calls
    ``dispatch_terminal_event``, which drains pending ``chunk`` and
    ``thought`` events from the queue before enqueuing the terminal
    event. Without the drain, a user who clicks Stop while the LLM
    is actively streaming would wait 10+ seconds for the queue to
    catch up before the UI shows "cancelled".

    See ``dispatch_terminal_event`` in sse_events.py for details.
    """
    try:
        await dispatch_terminal_event(
            job_id,
            SSEEvent(type=event_type, step=0, data=data),
        )
    except Exception as exc:
        logger.warning(
            "Failed to dispatch terminal %s event for job %s: %s",
            event_type,
            job_id,
            exc,
        )



def _persist_session_status(
    chat_id: str,
    status: str,
    *,
    project_id: str = "default_project",
) -> None:
    """
    Atomically update ``session.json`` with the new status.

    This is the authoritative persistence path. The in-memory
    ``job_manager`` status is a cache; the on-disk session document is
    the source of truth for reload-after-restart scenarios.

    Never raises: a failed write is logged and swallowed because the HTTP
    response reflects the in-memory state change, which has already been
    applied. The recovery pass on next backend start will repair any
    divergence between memory and disk.
    """
    try:
        session_file = (
            project_manager.get_chat_dir(chat_id, project_id) / "session.json"
        )
        if not session_file.exists():
            return
        s_data = json.loads(session_file.read_text(encoding="utf-8"))
        if not isinstance(s_data, dict):
            return
        s_data["status"] = status
        _atomic_write_json(session_file, s_data)
    except Exception as exc:
        logger.warning(
            "Failed to persist session status %r for chat %s: %s",
            status,
            chat_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Stop / Cancel
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_id}/stop", response_model=JobControlResponse)
@router.post("/jobs/{job_id}/cancel", response_model=JobControlResponse)
async def stop_job(job_id: str):
    """
    Stop/cancel a running job by job id or chat id.

    The canonical status written to both ``job_manager`` and
    ``session.json`` is ``"cancelled"`` — NOT ``"stopped"``. This aligns
    with ``SSEEventType.CANCELLED`` and the frontend's ``SessionStatus``
    contract (see ``docs/state-contracts.md`` §3).
    """
    chat, chat_id = resolve_chat(job_id)

    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    # Unblock any paused runner BEFORE cancelling the task. A paused
    # AgentRunner will not honour CancelledError until it has drained the
    # pause gate, so resume first to allow a clean cancellation.
    runner = (
        job_manager.get_runner(target_job_id)
        or job_manager.get_runner(job_id)
    )
    if runner:
        if hasattr(runner, "resume"):
            try:
                runner.resume()
            except Exception as exc:
                logger.debug("runner.resume() raised during stop: %s", exc)
        if hasattr(runner, "cancel"):
            try:
                runner.cancel()
            except Exception as exc:
                logger.debug("runner.cancel() raised during stop: %s", exc)

    # Cancel the in-memory asyncio task.
    task = (
        ACTIVE_JOB_TASKS.get(job_id)
        or ACTIVE_JOB_TASKS.get(target_job_id)
        or (ACTIVE_JOB_TASKS.get(chat_id) if chat_id else None)
    )
    task_cancelled = False
    if task and not task.done():
        task.cancel()
        task_cancelled = True

    # Update the in-memory job status. Canonical value is `cancelled`.
    job_manager.update_status(
        target_job_id,
        status="cancelled",
        error="Task cancelled by user",
    )
    if job_id != target_job_id:
        job_manager.update_status(
            job_id,
            status="cancelled",
            error="Task cancelled by user",
        )

    # Persist to session.json. Only when we actually resolved a real
    # chat — synthetic fallback ids must never create phantom sessions.
    if chat_id and chat:
        chat_pid = chat.get("project_id", "default_project")
        _persist_session_status(
            chat_id,
            "cancelled",
            project_id=chat_pid,
        )

    # Signal cancellation to the Resource Monitor subsystem.
    try:
        from omweb.services.resource_monitor_service import (
            get_resource_monitor_service,
        )

        get_resource_monitor_service().cancel_run(target_job_id)
    except Exception as exc:
        logger.debug(
            "Failed to signal cancel_run to ResourceMonitor for %s: %s",
            target_job_id,
            exc,
        )

    # Dispatch the terminal CANCELLED SSE event so connected clients
    # close their streams and reflect the new state.
    #
    # Use the TERMINAL variant: the queue may contain dozens of pending
    # chunk events from an LLM stream that is slow to cancel (the LLM
    # client's cancellation propagation is a separate, known backend
    # bug — see docs/state-contracts.md §11). Draining the queue first
    # ensures the frontend sees "cancelled" within ~100ms of the click
    # instead of waiting for the backlog to drain.
    await _dispatch_terminal_event(
        target_job_id,
        SSEEventType.CANCELLED,
        {
            "status": "cancelled",
            "message": "Task cancelled by user",
        },
    )

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "status": "cancelled",
        "task_cancelled": task_cancelled,
        "message": "Task cancelled successfully",
    }


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_id}/pause", response_model=JobControlResponse)
async def pause_job(job_id: str):
    """
    Pause a running job by job id or chat id.

    Dispatches a canonical ``status`` event with ``data.status="paused"``.
    This is NOT a terminal event — the run continues to be tracked by the
    stream manager and will resume when ``/resume`` is called.
    """
    chat, chat_id = resolve_chat(job_id)

    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    # Update in-memory state and propagate to the active runner.
    job_manager.pause_job(target_job_id)
    if job_id != target_job_id:
        job_manager.pause_job(job_id)

    runner = (
        job_manager.get_runner(target_job_id)
        or job_manager.get_runner(job_id)
    )
    if runner and hasattr(runner, "pause"):
        try:
            runner.pause()
        except Exception as exc:
            logger.debug("runner.pause() raised: %s", exc)

    # Persist to session.json.
    if chat_id and chat:
        chat_pid = chat.get("project_id", "default_project")
        _persist_session_status(
            chat_id,
            "paused",
            project_id=chat_pid,
        )

    # Canonical `status` event with data.status="paused".
    await _dispatch_control_event(
        target_job_id,
        SSEEventType.STATUS,
        {
            "status": "paused",
            "message": "Task paused by user",
        },
    )

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "status": "paused",
        "message": "Task paused successfully",
    }


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_id}/resume", response_model=JobControlResponse)
async def resume_job(job_id: str):
    """
    Resume a paused job by job id or chat id.

    Dispatches a canonical ``status`` event with ``data.status="running"``.
    """
    chat, chat_id = resolve_chat(job_id)

    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    # Update in-memory state and propagate to the active runner.
    job_manager.resume_job(target_job_id)
    if job_id != target_job_id:
        job_manager.resume_job(job_id)

    runner = (
        job_manager.get_runner(target_job_id)
        or job_manager.get_runner(job_id)
    )
    if runner and hasattr(runner, "resume"):
        try:
            runner.resume()
        except Exception as exc:
            logger.debug("runner.resume() raised: %s", exc)

    # Persist to session.json.
    if chat_id and chat:
        chat_pid = chat.get("project_id", "default_project")
        _persist_session_status(
            chat_id,
            "running",
            project_id=chat_pid,
        )

    # Canonical `status` event with data.status="running".
    await _dispatch_control_event(
        target_job_id,
        SSEEventType.STATUS,
        {
            "status": "running",
            "message": "Task resumed by user",
        },
    )

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "status": "running",
        "message": "Task resumed successfully",
    }
