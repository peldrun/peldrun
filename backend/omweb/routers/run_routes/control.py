"""
Job control endpoints.

Exposes operations that alter the lifecycle of a running job:

* ``POST /jobs/{job_id}/stop``   - stop a running job.
* ``POST /jobs/{job_id}/cancel`` - alias for ``stop``.

Stopping a job cancels the in-memory task, updates the persisted status,
patches the chat session document, and dispatches a ``CANCELLED`` SSE event
so connected clients can close their streams promptly.

Note:
    ``dispatch_event`` and the SSE event classes are imported lazily inside
    the cancellation notification block, mirroring the original monolithic
    router. ``agent_bridge`` does not export ``dispatch_event`` in every
    deployment, and a missing symbol must degrade gracefully rather than
    break module import.
"""

from __future__ import annotations

import json

from fastapi import APIRouter

from omweb.job_manager import job_manager
from omweb.project_manager import project_manager

from .state import ACTIVE_JOB_TASKS
from .utils import resolve_chat


router = APIRouter()


@router.post("/jobs/{job_id}/stop")
@router.post("/jobs/{job_id}/cancel")
async def stop_job(job_id: str):
    """Stop/cancel a running job by job id or chat id."""
    chat, chat_id = resolve_chat(job_id)

    # The canonical job id may differ from the caller-supplied identifier.
    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

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
            # Session patch is best-effort; the API response is authoritative.
            pass

    # Notify any connected SSE clients that the job was cancelled.
    # The imports are deferred because agent_bridge does not reliably export
    # dispatch_event in every deployment.
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
