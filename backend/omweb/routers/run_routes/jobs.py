"""
Job inspection endpoints.

Provides read-only access to job metadata and live/stored event streams:

* ``GET /jobs``          - list every known job.
* ``GET /jobs/{job_id}`` - return a consolidated view of a job, merging
  in-memory state, persisted chat session data, and stored job records.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter

from omweb.agent_bridge import job_scoped_artifacts
from omweb.job_manager import job_manager

from .state import ACTIVE_JOB_EVENTS, ACTIVE_JOB_TASKS
from .utils import resolve_chat


router = APIRouter()


def _extract_last_sequence(events: Any) -> int:
    """
    Return the highest monotonic sequence number present in ``events``.

    Sequences appear under two different field names because event
    records are produced by two code paths:
      * ``"seq"``       — assigned by ``streaming.py`` for live events.
      * ``"sequence"``  — assigned by ``RunStore`` for replayed events.

    Nested ``data.seq`` / ``data.sequence`` keys are also inspected,
    since the SSE forwarder hoists them at multiple stages of the
    pipeline. Returns 0 when no sequence is present anywhere (empty
    list, synthetic records, or pre-sequence legacy data).
    """
    if not isinstance(events, list) or not events:
        return 0

    highest = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue

        for val in (ev.get("seq"), ev.get("sequence")):
            if isinstance(val, int) and val > highest:
                highest = val

        data = ev.get("data")
        if isinstance(data, dict):
            for val in (data.get("seq"), data.get("sequence")):
                if isinstance(val, int) and val > highest:
                    highest = val

    return highest


@router.get("/jobs")
@router.get("/jobs/")
async def list_jobs():
    """Return every job currently tracked by the job manager."""
    return {"jobs": job_manager.list_jobs()}


@router.get("/jobs/active")
async def list_active_jobs():
    """
    Return the set of jobs whose execution task is currently alive.

    This endpoint exists so the frontend can resume SSE connections
    after a page reload without needing to know in advance which chat
    the user was viewing. ``runStreamManager.restoreActiveRuns()``
    calls it on application bootstrap and reopens a stream for every
    entry it returns.

    Design notes
    ------------
    * **Deduplication is mandatory.** ``start_run.py`` registers the
      same ``asyncio.Task`` under two keys in ``ACTIVE_JOB_TASKS``: the
      canonical ``job_id`` and its ``chat_id`` alias. Without
      deduplication every active run would appear twice.

    * **Resolution is defensive.** A key in ``ACTIVE_JOB_TASKS`` may be
      either a ``job_id`` (the normal case, set directly by
      ``start_run.py``) or a ``chat_id`` alias. The resolver handles
      both shapes and falls back to a linear scan of ``JOB_TO_CHAT_ID``
      when neither is conclusive.

    * **Dead tasks are filtered out.** A task that has completed but
      whose ``done_callback`` has not yet fired would still be present
      in the registry for a few milliseconds. ``task.done()`` filters
      those out so the response only contains runs that are genuinely
      still executing.

    * **``last_sequence`` is the resume hint.** It is the highest
      sequence number present in ``ACTIVE_JOB_EVENTS`` for the job.
      The frontend passes it as ``?after_sequence=N`` when opening the
      SSE stream, so already-seen events are not replayed.

    Route ordering
    --------------
    This route MUST be declared before ``GET /jobs/{job_id}``.
    FastAPI matches routes in declaration order; the parameterized
    route would otherwise shadow ``/jobs/active`` by capturing
    ``"active"`` as a ``job_id`` value.
    """
    from .state import JOB_TO_CHAT_ID

    seen_tasks: set[int] = set()
    active_jobs: List[Dict[str, Any]] = []

    for key, task in list(ACTIVE_JOB_TASKS.items()):
        if task is None:
            continue

        # Skip completed tasks that are still in the registry pending
        # their done_callback.
        is_done = getattr(task, "done", None)
        if callable(is_done):
            try:
                if is_done():
                    continue
            except Exception:
                pass

        # Deduplicate by task identity. Both the canonical job id and
        # the chat id alias point at the same Task object.
        task_identity = id(task)
        if task_identity in seen_tasks:
            continue
        seen_tasks.add(task_identity)

        # Resolve (job_id, chat_id) from the registry aliases.
        job_id: str = ""
        chat_id: str = ""

        if key in JOB_TO_CHAT_ID:
            # `key` is the canonical job id.
            job_id = key
            chat_id = JOB_TO_CHAT_ID[key]
        else:
            # `key` may be a chat id alias. Ask the resolver.
            chat, resolved_chat_id = resolve_chat(key)
            if chat:
                chat_id = str(chat.get("id") or resolved_chat_id or key)
                job_id = str(chat.get("job_id") or "")
            else:
                # Last-resort fallback: search for the chat alias that
                # points at this same task instance.
                for j_id, c_id in JOB_TO_CHAT_ID.items():
                    if ACTIVE_JOB_TASKS.get(c_id) is task:
                        job_id = j_id
                        chat_id = c_id
                        break

        # Emit only fully-resolved entries. A partial entry cannot be
        # used by the frontend to subscribe (it needs both ids).
        if not job_id or not chat_id:
            continue

        # Highest sequence present in the live event buffer.
        events = (
            ACTIVE_JOB_EVENTS.get(job_id)
            or ACTIVE_JOB_EVENTS.get(chat_id)
            or []
        )
        last_sequence = _extract_last_sequence(events)

        active_jobs.append({
            "job_id": job_id,
            "chat_id": chat_id,
            "status": "running",
            "last_sequence": last_sequence,
        })

    return {"jobs": active_jobs}


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

    # Highest sequence number present in whichever event list was chosen
    # above (live / in-memory / persisted). Used by the frontend stream
    # manager to resume an interrupted SSE connection from the correct
    # point (?after_sequence=N) without replaying already-consumed events.
    last_sequence = _extract_last_sequence(effective_events)

    return {
        "id": actual_job_id,
        "chat_id": chat_id,
        "status": effective_status,
        # Authoritative liveness signal. Distinct from ``status`` because
        # ``status`` can be a stale on-disk value (e.g. "running" while
        # the process was killed). This field is derived from the live
        # ``ACTIVE_JOB_TASKS`` registry and is the only reliable source
        # for "should I open an SSE stream for this job?".
        "is_actively_running": bool(is_actively_running),
        # Resume hint for the SSE client: connect with
        # ``?after_sequence={last_sequence}`` to skip already-seen
        # events and receive only the missing ones.
        "last_sequence": last_sequence,
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
        # Multimodal attachment references for the CURRENT turn (not yet
        # archived into turns[]). Consumed by use-job-details.ts to
        # repopulate submittedFiles after a page reload, so image
        # thumbnails survive a browser refresh even before the turn is
        # flushed into history by the next run.
        #
        # Each entry carries {fileId, mimeType, fileName, width, height}.
        # An empty list is returned when the turn has no images; the
        # frontend treats it identically to a missing key.
        "attachments": chat.get("attachments", []),
    }
