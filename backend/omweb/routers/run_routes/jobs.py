"""
Job inspection endpoints.

Provides read-only access to job metadata and live/stored event streams:

* ``GET /jobs``          - list every known job.
* ``GET /jobs/{job_id}`` - return a consolidated view of a job, merging
  in-memory state, persisted chat session data, and stored job records.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from omweb.agent_bridge import job_scoped_artifacts
from omweb.job_manager import job_manager

from .state import ACTIVE_JOB_EVENTS, ACTIVE_JOB_TASKS
from .utils import resolve_chat


router = APIRouter()


@router.get("/jobs")
@router.get("/jobs/")
async def list_jobs():
    """Return every job currently tracked by the job manager."""
    return {"jobs": job_manager.list_jobs()}


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: str):
    """
    Return a consolidated view of a job.

    The response merges data from three sources, in order of precedence:

    1. The live in-memory event buffer (if the job is currently running).
    2. The persisted job record in the job manager.
    3. The persisted chat session document.

    Additionally resolves the effective status (running vs terminal) and
    surfaces the produced files associated with the job.
    """
    chat, chat_id = resolve_chat(job_id)
    chat = chat or {}

    # The canonical job id may differ from the identifier the caller passed
    # (e.g. the caller used a chat id).
    actual_job_id = job_id
    if chat and chat.get("job_id"):
        actual_job_id = chat.get("job_id")

    job = job_manager.get_job(job_id) or job_manager.get_job(actual_job_id)

    # Prefer the live buffer, then the in-memory job events, then storage.
    live_events = (
        ACTIVE_JOB_EVENTS.get(actual_job_id)
        or ACTIVE_JOB_EVENTS.get(job_id)
        or (ACTIVE_JOB_EVENTS.get(chat_id) if chat_id else None)
    )

    stored_events = chat.get("events", [])
    memory_events = getattr(job, "events", []) if job else []

    if live_events and len(live_events) > 0:
        effective_events = live_events
    elif memory_events:
        effective_events = memory_events
    else:
        effective_events = stored_events

    effective_prompt = (getattr(job, "prompt", "") if job else "") or chat.get("prompt", "")

    # A job is "running" if its task is still registered, regardless of the
    # status stored in the job record.
    is_actively_running = (
        job_id in ACTIVE_JOB_TASKS
        or actual_job_id in ACTIVE_JOB_TASKS
        or (chat_id and chat_id in ACTIVE_JOB_TASKS)
    )

    if is_actively_running:
        effective_status = "running"
    else:
        effective_status = (
            (getattr(job, "status", "") if job else "")
            or chat.get("status", "completed")
        )

    effective_result = (
        (getattr(job, "result", None) if job else None)
        or chat.get("result", "")
    )

    # Fall back to the last "final" event if the result field is empty.
    if not effective_result and effective_events:
        for ev in reversed(effective_events):
            ev_type = ev.get("type", "") if isinstance(ev, dict) else getattr(ev, "type", "")
            if str(ev_type).lower() == "final":
                data_part = (
                    ev.get("data", {}) if isinstance(ev, dict) else getattr(ev, "data", {})
                )
                if isinstance(data_part, dict):
                    effective_result = data_part.get("result", "")
                elif isinstance(data_part, str):
                    effective_result = data_part
                break

    # Resolve the produced-files list from the most specific key available.
    resolved_files = (
        job_scoped_artifacts.get(job_id)
        or job_scoped_artifacts.get(actual_job_id)
        or (job_scoped_artifacts.get(chat_id) if chat_id else [])
        or []
    )

    return {
        "id": actual_job_id,
        "chat_id": chat_id,
        "status": effective_status,
        "prompt": effective_prompt,
        "result": effective_result,
        "events": effective_events,
        "turns": chat.get("turns", []),
        "usage_summary": chat.get("usage_summary", {}),
        "created_at": chat.get("created_at"),
        "agent_id": chat.get("agent_id", "peldrun"),
        "model": chat.get("model"),
        "mode": chat.get("mode", "agent"),
        "produced_files": resolved_files,
        "generative_ui": chat.get("generative_ui"),
    }
