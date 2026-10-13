"""
Job control endpoints.

Exposes operations that alter the lifecycle of a running job:

* ``POST /jobs/{job_id}/stop``   - stop a running job.
* ``POST /jobs/{job_id}/cancel`` - alias for ``stop``.
* ``POST /jobs/{job_id}/pause``  - pause a running job.
* ``POST /jobs/{job_id}/resume`` - resume a paused job.

Stopping a job cancels the in-memory task, updates the persisted status,
patches the chat session document, and dispatches a ``CANCELLED`` SSE event
so connected clients can close their streams promptly.

Pausing suspends execution at the current step boundary, takes a safe checkpoint,
and emits a ``PAUSED`` event. Resuming unblocks the execution cycle and emits
a ``RESUMED`` event.
"""

from __future__ import annotations

import json

from fastapi import APIRouter

from omweb.job_manager import job_manager
from omweb.project_manager import project_manager

from .schemas import JobControlResponse
from .state import ACTIVE_JOB_TASKS
from .utils import resolve_chat


router = APIRouter()


@router.post("/jobs/{job_id}/stop", response_model=JobControlResponse)
@router.post("/jobs/{job_id}/cancel", response_model=JobControlResponse)
async def stop_job(job_id: str):
    """Stop/cancel a running job by job id or chat id."""
    chat, chat_id = resolve_chat(job_id)

    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    # If the job is paused, resume signals first to let cancellation drain smoothly
    runner = job_manager.get_runner(target_job_id) or job_manager.get_runner(job_id)
    if runner:
        if hasattr(runner, "cancel"):
            runner.cancel()
        if hasattr(runner, "resume"):
            runner.resume()

    # Cancel the in-memory task, checking every known alias.
    task = (
        ACTIVE_JOB_TASKS.get(job_id)
        or ACTIVE_JOB_TASKS.get(target_job_id)
        or (ACTIVE_JOB_TASKS.get(chat_id) if chat_id else None)
    )
    task_cancelled = False
    if task and not task.done():
        task.cancel()
        task_cancelled = True

    # Update the persisted job status.
    job_manager.update_status(
        target_job_id, status="stopped", error="Task stopped by user"
    )
    if job_id != target_job_id:
        job_manager.update_status(
            job_id, status="stopped", error="Task stopped by user"
        )

    # Reflect the stop in the persisted session document.
    if chat_id:
        try:
            target_chat, _ = resolve_chat(job_id)
            target_pid = (target_chat or {}).get("project_id", "default_project")
            session_file = (
                project_manager.get_chat_dir(chat_id, target_pid) / "session.json"
            )
            if session_file.exists():
                s_data = json.loads(session_file.read_text(encoding="utf-8"))
                s_data["status"] = "stopped"
                session_file.write_text(
                    json.dumps(s_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            pass

    # Notify connected SSE clients that the job was cancelled.
    try:
        from omweb.sse_events import SSEEvent, SSEEventType
        from omweb.agent_bridge import dispatch_event

        await dispatch_event(
            target_job_id,
            SSEEvent(
                type=SSEEventType.CANCELLED,
                data={"status": "stopped", "message": "Task stopped by user"},
            ),
        )
    except Exception:
        pass

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "status": "stopped",
        "task_cancelled": task_cancelled,
        "message": "Task stopped successfully",
    }


@router.post("/jobs/{job_id}/pause", response_model=JobControlResponse)
async def pause_job(job_id: str):
    """Pause a running job by job id or chat id."""
    chat, chat_id = resolve_chat(job_id)

    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    # Update state in job manager and invoke active runner pause
    job_manager.pause_job(target_job_id)
    if job_id != target_job_id:
        job_manager.pause_job(job_id)

    runner = job_manager.get_runner(target_job_id) or job_manager.get_runner(job_id)
    if runner and hasattr(runner, "pause"):
        runner.pause()

    # Reflect pause in session document
    if chat_id:
        try:
            target_chat, _ = resolve_chat(job_id)
            target_pid = (target_chat or {}).get("project_id", "default_project")
            session_file = (
                project_manager.get_chat_dir(chat_id, target_pid) / "session.json"
            )
            if session_file.exists():
                s_data = json.loads(session_file.read_text(encoding="utf-8"))
                s_data["status"] = "paused"
                session_file.write_text(
                    json.dumps(s_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            pass

    # Dispatch PAUSED event to connected clients
    try:
        from omweb.sse_events import SSEEvent, SSEEventType
        from omweb.agent_bridge import dispatch_event

        event_type = getattr(SSEEventType, "PAUSED", "paused")
        await dispatch_event(
            target_job_id,
            SSEEvent(
                type=event_type,
                data={"status": "paused", "message": "Task paused by user"},
            ),
        )
    except Exception:
        pass

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "status": "paused",
        "message": "Task paused successfully",
    }


@router.post("/jobs/{job_id}/resume", response_model=JobControlResponse)
async def resume_job(job_id: str):
    """Resume a paused job by job id or chat id."""
    chat, chat_id = resolve_chat(job_id)

    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    # Update state in job manager and invoke active runner resume
    job_manager.resume_job(target_job_id)
    if job_id != target_job_id:
        job_manager.resume_job(job_id)

    runner = job_manager.get_runner(target_job_id) or job_manager.get_runner(job_id)
    if runner and hasattr(runner, "resume"):
        runner.resume()

    # Reflect running status in session document
    if chat_id:
        try:
            target_chat, _ = resolve_chat(job_id)
            target_pid = (target_chat or {}).get("project_id", "default_project")
            session_file = (
                project_manager.get_chat_dir(chat_id, target_pid) / "session.json"
            )
            if session_file.exists():
                s_data = json.loads(session_file.read_text(encoding="utf-8"))
                s_data["status"] = "running"
                session_file.write_text(
                    json.dumps(s_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            pass

    # Dispatch RESUMED event to connected clients
    try:
        from omweb.sse_events import SSEEvent, SSEEventType
        from omweb.agent_bridge import dispatch_event

        event_type = getattr(SSEEventType, "RESUMED", "resumed")
        await dispatch_event(
            target_job_id,
            SSEEvent(
                type=event_type,
                data={"status": "running", "message": "Task resumed by user"},
            ),
        )
    except Exception:
        pass

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "status": "running",
        "message": "Task resumed successfully",
    }
