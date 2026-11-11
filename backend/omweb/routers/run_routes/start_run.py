# backend/omweb/routers/run_routes/start_run.py
"""
Run creation endpoint.

Handles ``POST /`` (and ``POST ""``) to start a new agent or direct-chat
execution. The endpoint is responsible for:

* Validating the incoming prompt and execution mode.
* Handling turn regeneration requests with session history branching.
* Creating the backing job and chat session records.
* Persisting the resolved LLM overrides on the session.
* Capturing a pre-execution filesystem snapshot so the delta artifacts can
  later be computed for this specific turn.
* Registering the execution run with ResourceMonitor in PENDING status.
* Scheduling the actual execution task (agent or direct chat).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Tuple
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi import status as http_status

from omweb.agent_bridge import (
    job_scoped_artifacts,
    run_direct_chat,
    run_instrumented,
)
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager, _atomic_write_json

from .schemas import RunRequest
from .state import (
    ACTIVE_JOB_EVENTS,
    ACTIVE_JOB_TASKS,
    JOB_PRE_SNAPSHOTS,
    JOB_TO_CHAT_ID,
)
from .utils import resolve_chat

# [P0-7] Resume endpoint dependencies. The recovery manager and the
# core executor are imported at module level (not inside the endpoint)
# so that a broken import fails loudly at startup, not at first use.
# The core RunRequest alias avoids the name collision with the API-layer
# ``RunRequest`` imported above from ``.schemas``.
from peldrun.engine.state import ExecutionState
from peldrun.runtime.contract import (
    AgentSpec as CoreAgentSpec,
    RunRequest as CoreRunRequest,
    WorkspaceContext as CoreWorkspaceContext,
)
from peldrun.runtime.executor import core_executor
from peldrun.runtime.recovery_manager import (
    RecoveryManager,
    RecoveryPlan,
    RecoveryReason,
)
from peldrun.runtime.store import get_run_store

logger = logging.getLogger("omweb.routers.run_routes.start_run")

router = APIRouter()


def _project_attachments_for_turn(attachments: Any) -> list:
    """
    Project ``session.json``'s top-level ``attachments`` list into the
    ``AttachedUserFile`` shape consumed by the frontend's ``ChatTurn``
    renderer.

    The top-level ``attachments`` field stores the *latest* turn's image
    references (see ``AttachmentRef`` in ``schemas.py``). When a turn is
    archived into ``turns[]``, this helper converts those references
    into the historical shape so that ``AttachedFileCard`` can render
    thumbnails from ``/api/files/temp/{fileId}`` after a page reload.

    Entries without a valid string ``fileId`` are dropped: they cannot
    be resolved to a physical artifact, so including them would surface
    broken thumbnails. The ``name`` field falls back to the ``fileId``
    when the original filename was not recorded.

    Non-dict entries and non-list inputs are silently ignored so that a
    schema drift in one historical turn never crashes turn archival.

    Every returned item carries ``name`` and ``fileId``. Optional fields
    (``mimeType``, ``width``, ``height``) are included only when they
    hold a well-typed value, keeping the persisted JSON compact.
    """
    if not isinstance(attachments, list):
        return []

    result: list = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        file_id = a.get("fileId")
        if not isinstance(file_id, str) or not file_id:
            continue
        name = a.get("fileName")
        if not isinstance(name, str) or not name:
            name = file_id
        projected: dict = {
            "name": name,
            "fileId": file_id,
        }
        mime_type = a.get("mimeType")
        if isinstance(mime_type, str) and mime_type:
            projected["mimeType"] = mime_type
        width = a.get("width")
        if isinstance(width, int):
            projected["width"] = width
        height = a.get("height")
        if isinstance(height, int):
            projected["height"] = height
        result.append(projected)
    return result


# ---------------------------------------------------------------------------
# [P0-7] Resume endpoint helpers
# ---------------------------------------------------------------------------


def _collect_all_session_files() -> list[Path]:
    """
    Return every ``session.json`` path under both supported layouts.

    Mirrors ``omweb.services.recovery._collect_session_files`` (which
    is not importable across modules without triggering the recovery
    service's lazy import cycle). Duplicating the ~15-line scan is
    preferable to weaving a new public API into the recovery service
    for a single caller.
    """
    paths: list[Path] = []

    chats_dir = project_manager.chats_dir
    if chats_dir.exists():
        for chat_dir in chats_dir.iterdir():
            if not chat_dir.is_dir():
                continue
            sf = chat_dir / "session.json"
            if sf.is_file():
                paths.append(sf)

    projects_dir = project_manager.projects_dir
    if projects_dir.exists():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            proj_chats = proj_dir / "chats"
            if not proj_chats.exists():
                continue
            for chat_dir in proj_chats.iterdir():
                if not chat_dir.is_dir():
                    continue
                sf = chat_dir / "session.json"
                if sf.is_file():
                    paths.append(sf)

    return paths


def _find_session_by_run_id(run_id: str) -> tuple[Path | None, dict | None]:
    """
    Locate the ``session.json`` whose ``id`` or ``job_id`` matches
    ``run_id``.

    Returns ``(path, data)`` on success and ``(None, None)`` when no
    match is found. Both keys (chat_id, job_id) are accepted because
    the HTTP contract allows the client to call ``/resume`` with
    either — a user copying the job id from the UI should not be
    rejected because the session is keyed by chat id.
    """
    if not run_id:
        return (None, None)

    # Fast path: direct directory hit.
    for base in (project_manager.chats_dir,):
        if base.exists():
            candidate = base / run_id / "session.json"
            if candidate.is_file():
                try:
                    return (candidate, json.loads(candidate.read_text(encoding="utf-8")))
                except Exception:
                    return (None, None)

    # Slow path: scan for job_id match.
    for sf in _collect_all_session_files():
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("id") == run_id or data.get("job_id") == run_id:
            return (sf, data)
    return (None, None)


def _build_resume_request(
    state: ExecutionState,
    plan: RecoveryPlan,
    session_file: Path,
) -> CoreRunRequest | None:
    """
    Reconstruct a core ``RunRequest`` for a resumed run.

    The state carries the authoritative message history and progress;
    the request carries identity, workspace, and LLM configuration.
    Every field the executor requires is populated from
    ``session.json`` (for identity + LLM config) and the restored
    state (for agent identity + system prompt fallback).

    Returns ``None`` on any error so the caller can report
    ``REQUEST_BUILD_FAILED`` instead of proceeding with a half-built
    request.
    """
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("resume: could not read session.json: %s", exc)
        return None

    chat_id = str(data.get("id") or state.run_id)
    project_id = str(data.get("project_id") or "default_project")
    job_id = str(data.get("job_id") or state.run_id)
    prompt = str(data.get("prompt") or state.task_prompt or "")
    agent_id = str(data.get("agent_id") or state.agent_name or "peldrun")

    # Extract the system prompt from the restored state. The state's
    # messages were built at run creation and are the authoritative
    # record of what the model saw; a stale session.json copy would
    # diverge if the agent definition was edited since.
    system_prompt = ""
    for msg in (state.messages or []):
        if isinstance(msg, dict):
            role_raw = msg.get("role", "user")
            content = msg.get("content", "")
        else:
            role_raw = getattr(msg, "role", "user")
            content = getattr(msg, "content", "")
        role_str = (
            role_raw.value if hasattr(role_raw, "value") else str(role_raw or "")
        ).lower()
        if role_str == "system":
            system_prompt = content if isinstance(content, str) else ""
            break

    try:
        files_dir = project_manager.get_chat_files_dir(chat_id, project_id)
        files_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("resume: could not resolve workspace root: %s", exc)
        return None

    llm_config: Dict[str, Any] = {
        "model": data.get("model") or "default",
        "provider": data.get("provider") or "openai",
        "provider_name": (
            data.get("provider_name")
            or data.get("provider")
            or "openai"
        ),
    }
    if "reasoning_effort" in data:
        llm_config["reasoning_effort"] = data["reasoning_effort"]
    if "attachments" in data:
        llm_config["attachments"] = data["attachments"]

    try:
        return CoreRunRequest(
            run_id=str(state.run_id),
            job_id=job_id,
            prompt=prompt,
            agent_spec=CoreAgentSpec(
                id=agent_id,
                name=str(state.agent_name or agent_id),
                system_prompt=system_prompt,
                max_steps=int(state.max_steps or 30),
            ),
            workspace=CoreWorkspaceContext(
                workspace_id=chat_id,
                root_path=str(files_dir),
                chat_id=chat_id,
                project_id=project_id,
            ),
            llm_config=llm_config,
            step_timeout_seconds=120.0,
            total_timeout_seconds=None,
            metadata={
                "resumed_from": "resume_endpoint",
                "recovery_reason": (
                    plan.reason.value if plan.reason else "unknown"
                ),
                "next_step": plan.next_step,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("resume: could not construct RunRequest: %s", exc)
        return None


async def _execute_resume_flow(
    run_id: str,
    session_file: Path,
    chat_id: str,
    job_id: str,
) -> None:
    """
    Background task: run ``RecoveryManager.resume_run`` end-to-end,
    then persist the terminal outcome into ``session.json``.

    Two responsibilities:

      1. Bridge the executor's ``restored_state`` parameter to the
         ``resume_run`` contract. ``resume_run`` only passes a
         ``RunRequest`` to ``executor_fn``; we capture the state via
         the request_builder closure so the wrapper can forward it to
         ``core_executor.execute(..., restored_state=state)``.

      2. Write the terminal status, result, and (on success) the
         cleared ``resumable`` flag back into ``session.json``. The
         SSE consumer continues to read ``ACTIVE_JOB_EVENTS`` for
         live events; ``session.json`` is the durable record.
    """
    # [RESOURCE-MONITOR-FIX] Acquire the monitor service and transition
    # the run PENDING -> RUNNING before the executor starts. Without
    # this, the row stays PENDING forever and no TOOL_CALL spans can
    # attach — the exact "No tool calls recorded" symptom in resume.
    _monitor = None
    try:
        from omweb.services.resource_monitor_service import (
            get_resource_monitor_service as _get_monitor,
        )
        _monitor = _get_monitor()
        _monitor.start_run(job_id)
    except Exception as _mon_err:
        logger.warning(
            "resume: monitor start_run failed for job %s: %s",
            job_id,
            _mon_err,
        )
        _monitor = None

    captured: Dict[str, Any] = {}

    def _request_builder(
        state: ExecutionState, plan: RecoveryPlan
    ) -> CoreRunRequest | None:
        captured["state"] = state
        req = _build_resume_request(state, plan, session_file)
        # [RESOURCE-MONITOR-FIX] Attach the passive monitor hook so
        # the executor emits TOOL_CALL spans even though this flow
        # bypasses PeldrunEngine entirely. The hook is synchronous
        # and exception-safe by contract (see ``_make_monitor_hook``).
        if req is not None and _monitor is not None:
            try:
                req.metadata["resource_monitor_hook"] = (
                    _make_monitor_hook(_monitor, job_id)
                )
            except Exception as _hook_err:
                logger.debug(
                    "resume: monitor hook attach failed for job %s: %s",
                    job_id,
                    _hook_err,
                )
        return req

    async def _executor_fn(request: CoreRunRequest):
        return await core_executor.execute(
            request,
            event_listener=_make_event_listener(job_id),
            restored_state=captured.get("state"),
        )

    try:
        store = get_run_store()
        manager = RecoveryManager(persistence=store)
        outcome = await manager.resume_run(
            run_id=run_id,
            executor_fn=_executor_fn,
            request_builder=_request_builder,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("resume: resume_run raised for %s: %s", run_id, exc)
        _finalize_session_file(
            session_file,
            status="failed",
            result="",
            error=f"resume_run raised: {exc}",
        )
        # [RESOURCE-MONITOR-FIX] Mark the run FAILED so it cannot
        # remain stuck in RUNNING after an unexpected crash.
        if _monitor is not None:
            try:
                _monitor.fail_run(
                    job_id, error_message=f"resume_run raised: {exc}"
                )
            except Exception:
                pass
        return

    if outcome.success and outcome.result is not None:
        result = outcome.result
        status_value = (
            result.status.value
            if hasattr(result.status, "value")
            else str(result.status)
        )
        _finalize_session_file(
            session_file,
            status=status_value,
            result=result.output or "",
            error=result.error,
        )
        # [RESOURCE-MONITOR-FIX] Dispatch the correct terminal
        # transition based on the executor's reported status.
        if _monitor is not None:
            try:
                if status_value == "completed":
                    _monitor.complete_run(job_id)
                elif status_value == "cancelled":
                    _monitor.cancel_run(job_id)
                else:
                    _monitor.fail_run(
                        job_id,
                        error_message=(
                            result.error or "Resumed run failed."
                        ),
                    )
            except Exception as _term_err:
                logger.debug(
                    "resume: monitor terminal transition failed for "
                    "job %s: %s",
                    job_id,
                    _term_err,
                )
        logger.info(
            "resume: %s completed with status=%s", run_id, status_value
        )
    else:
        _failure_err = (
            outcome.error_message
            or outcome.reason_text
            or f"resume failed: {outcome.reason.value}"
        )
        _finalize_session_file(
            session_file,
            status="failed",
            result="",
            error=_failure_err,
        )
        # [RESOURCE-MONITOR-FIX] Mark the run FAILED.
        if _monitor is not None:
            try:
                _monitor.fail_run(job_id, error_message=_failure_err)
            except Exception:
                pass
        logger.warning(
            "resume: %s failed (%s): %s",
            run_id,
            outcome.reason.value,
            outcome.error_message or outcome.reason_text,
        )


def _finalize_session_file(
    session_file: Path,
    *,
    status: str,
    result: str,
    error: str | None,
) -> None:
    """Atomic-update session.json after a resume attempt."""
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("resume: cannot finalize session.json: %s", exc)
        return
    if not isinstance(data, dict):
        return
    data["status"] = status
    data["result"] = result
    if error:
        data["error"] = error
    else:
        data.pop("error", None)
    data["resumable"] = False
    data.pop("recovery_info", None)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _atomic_write_json(session_file, data)
    except Exception as exc:  # noqa: BLE001
        logger.error("resume: atomic write failed: %s", exc)


def _make_event_listener(job_id: str):
    """
    Build an event listener that appends to the SSE buffer for this
    run. Mirrors the pattern ``run_instrumented`` uses internally.
    """
    async def _listener(event) -> None:
        try:
            buf = ACTIVE_JOB_EVENTS.get(job_id)
            if buf is not None:
                buf.append(event)
        except Exception:
            pass

    return _listener


def _make_monitor_hook(monitor_service, run_id: str):
    """
    Build a synchronous ``resource_monitor_hook`` for the resumed run.

    The returned callable is handed to ``CoreRuntimeExecutor.execute``
    via ``request.metadata["resource_monitor_hook"]``. The executor
    subscribes it to the emitter and calls it for every
    ``PeldrunEvent`` synchronously (never awaits it). Any exception
    is swallowed by the executor's own bridge — but we still guard
    internally so a single malformed event cannot disable monitoring
    for the whole run.

    The hook mirrors the span-tracking logic used in
    ``peldrun_engine._process_single_core_event`` for TOOL_CALL /
    OBSERVATION only. It intentionally ignores every other event type
    because the resume flow does not need SSE dispatch (the frontend
    polls ``session.json``, not the SSE buffer, for resumed runs).

    Span keys: tool_call_id (primary), tool_name (secondary). Using
    two keys lets a matching OBSERVATION close the right span even
    when the provider omits one of the two fields.
    """
    from omweb.resource_monitor.models import (
        SpanStatus as _SpanStatus,
        SpanType as _SpanType,
    )

    _active_spans: Dict[str, str] = {}

    def _hook(evt) -> None:
        try:
            from peldrun.events.schema import EventType as _ET
        except Exception:
            return

        etype = getattr(evt, "type", None)
        payload = getattr(evt, "payload", {}) or {}

        try:
            if etype == _ET.TOOL_CALL or etype == getattr(
                _ET, "TOOL_CALLED", None
            ):
                tool_name = str(
                    payload.get("tool_name")
                    or payload.get("tool")
                    or "tool"
                )
                tc_id = str(payload.get("tool_call_id") or "")
                span_id = f"span_{uuid.uuid4().hex[:12]}"
                monitor_service.start_span(
                    span_id=span_id,
                    run_id=run_id,
                    target_name=tool_name,
                    span_type=_SpanType.TOOL_CALL,
                )
                _active_spans[tc_id or tool_name] = span_id
                if tc_id and tc_id != tool_name:
                    _active_spans.setdefault(tool_name, span_id)

            elif etype == _ET.OBSERVATION or etype == getattr(
                _ET, "TOOL_COMPLETED", None
            ):
                is_err = bool(payload.get("error"))
                obs_tool = str(payload.get("tool_name") or "")
                obs_tc_id = str(payload.get("tool_call_id") or "")

                span_id = None
                if obs_tc_id and obs_tc_id in _active_spans:
                    span_id = _active_spans[obs_tc_id]
                elif obs_tool and obs_tool in _active_spans:
                    span_id = _active_spans[obs_tool]
                elif _active_spans:
                    span_id = next(iter(_active_spans.values()))

                if span_id:
                    monitor_service.end_span(
                        span_id=span_id,
                        status=(
                            _SpanStatus.FAILED
                            if is_err
                            else _SpanStatus.COMPLETED
                        ),
                    )
                    stale_keys = [
                        k for k, v in _active_spans.items()
                        if v == span_id
                    ]
                    for k in stale_keys:
                        _active_spans.pop(k, None)
        except Exception:
            # Never let a monitor failure propagate — a telemetry hook
            # that crashes the run defeats the entire point.
            pass

    return _hook


@router.post("")
@router.post("/")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks):
    """Create a job, capture a filesystem baseline, and schedule execution."""
    raw_prompt = (req.prompt or "").strip()

    # Resolve existing chat session if chat_id provided
    existing_chat = None
    if req.chat_id:
        existing_chat, _ = resolve_chat(req.chat_id)

    # In regeneration workflows, recover prompt from targeted turn if omitted
    target_turn_prompt = None
    if req.regenerate_turn_id and existing_chat:
        prev_jid = existing_chat.get("job_id", "")
        if str(req.regenerate_turn_id).lower() in (str(prev_jid).lower(), "latest", "-1"):
            target_turn_prompt = existing_chat.get("prompt", "")
        else:
            existing_turns = existing_chat.get("turns", [])
            for idx, t in enumerate(existing_turns):
                if (
                    str(idx) == str(req.regenerate_turn_id)
                    or str(t.get("job_id", "")) == str(req.regenerate_turn_id)
                    or str(t.get("id", "")) == str(req.regenerate_turn_id)
                ):
                    target_turn_prompt = t.get("prompt", "")
                    break

    prompt = raw_prompt or (target_turn_prompt.strip() if target_turn_prompt else "")
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    # Normalize the requested execution mode; anything unknown falls back to "agent".
    exec_mode = "agent"
    raw_mode = getattr(req, "mode", None)
    if isinstance(raw_mode, str) and raw_mode.strip():
        m_val = raw_mode.strip().lower()
        if m_val in ["agent", "chat"]:
            exec_mode = m_val

    # Create the backing job record.
    generated_job_id = f"job_{uuid.uuid4().hex[:12]}"
    try:
        job = job_manager.create_job(generated_job_id, prompt=prompt)
    except TypeError:
        job = job_manager.create_job(generated_job_id)
        if hasattr(job, "prompt"):
            job.prompt = prompt

    actual_job_id = getattr(job, "id", generated_job_id)

    # Collect only the LLM overrides that were actually supplied.
    llm_override: Dict[str, Any] = {}
    if req.model:
        llm_override["model"] = req.model.strip()
    if req.provider:
        llm_override["provider"] = req.provider.strip()
    if req.provider_name:
        llm_override["provider_name"] = req.provider_name.strip()
    elif req.provider:
        llm_override["provider_name"] = req.provider.strip()
    if req.base_url:
        llm_override["base_url"] = req.base_url.strip()
    if req.api_key is not None:
        llm_override["api_key"] = req.api_key.strip()
    if req.api_type:
        llm_override["api_type"] = req.api_type.strip()
    if req.agent_id:
        llm_override["agent_id"] = req.agent_id.strip()
    if req.reasoning_effort:
        llm_override["reasoning_effort"] = req.reasoning_effort.strip()
    if req.is_reasoning_model is not None:
        llm_override["is_reasoning_model"] = req.is_reasoning_model
    # [FIX-INVOKED-SKILLS] Forward explicit ``/<skill_id>`` invocations
    # from the chat composer. The frontend strips the ``/skill_id`` token
    # from the prompt after the user selects it from the typeahead menu,
    # and sends the resolved ID(s) here as ``invoked_skills``. Without
    # this block, the field arrives in the HTTP body but is silently
    # discarded — ``llm_override`` never carries it to the dispatcher,
    # so ``EngineRunContext.invoked_skills`` stays ``[]`` and the skill
    # is never injected.
    #
    # Normalize each entry: strip whitespace, drop empties, deduplicate
    # while preserving order. The dispatcher re-normalizes defensively.
    if req.invoked_skills:
        _normalized_skills: list[str] = []
        _seen_skill_ids: set[str] = set()
        for _raw_sid in req.invoked_skills:
            if not isinstance(_raw_sid, str):
                continue
            _sid = _raw_sid.strip()
            if not _sid or _sid in _seen_skill_ids:
                continue
            _seen_skill_ids.add(_sid)
            _normalized_skills.append(_sid)
        if _normalized_skills:
            llm_override["invoked_skills"] = _normalized_skills
    # Forward structured file references selected by the user via the `@` menu.
    if req.context_refs:
        llm_override["context_refs"] = [
            ref.model_dump(by_alias=True, exclude_none=True)
            for ref in req.context_refs
        ]
    # Forward multimodal attachments (images uploaded via /api/files/upload-temp).
    #
    # These are lightweight references (``file_id`` + optional metadata),
    # NOT inline image bytes. The actual image content is resolved on
    # demand by the context builder using
    # ``omweb.routers.files.resolve_temp_artifact_to_data_uri``. This keeps
    # the run request under a few KB regardless of image size.
    #
    # ``attachments`` travels independently of ``context_refs``: a single
    # turn can carry both (text from an @-mentioned file AND one or more
    # images), and the two flows never overlap in the builder.
    if req.attachments:
        llm_override["attachments"] = [
            att.model_dump(by_alias=True, exclude_none=True)
            for att in req.attachments
        ]

    # Resolve session turns and context
    if existing_chat:
        chat_id = existing_chat.get("id", req.chat_id)
        project_id = existing_chat.get("project_id", req.project_id or "default_project")
        title = existing_chat.get("title") or prompt[:40]
        turns = list(existing_chat.get("turns", []))
        prev_jid = existing_chat.get("job_id", "")

        # Project the current turn's multimodal attachments into the
        # historical ``attachedFiles`` shape so they survive archival
        # into ``turns[]``. Applied identically in both branches below
        # (regenerate fallback + standard sequential run).
        prev_attached_files = _project_attachments_for_turn(
            existing_chat.get("attachments")
        )

        if req.regenerate_turn_id:
            # Handle regeneration branching
            is_latest = str(req.regenerate_turn_id).lower() in (str(prev_jid).lower(), "latest", "-1")
            if is_latest:
                # Discard latest unflushed turn; overwrite in-place
                pass
            else:
                matched_idx = None
                for idx, t in enumerate(turns):
                    if (
                        str(idx) == str(req.regenerate_turn_id)
                        or str(t.get("job_id", "")) == str(req.regenerate_turn_id)
                        or str(t.get("id", "")) == str(req.regenerate_turn_id)
                    ):
                        matched_idx = idx
                        break

                if matched_idx is not None:
                    # Truncate all subsequent turns to branch from this point
                    turns = turns[:matched_idx]
                else:
                    # Fallback to flush previous turn if target not matched
                    prev_p = existing_chat.get("prompt", "")
                    prev_r = existing_chat.get("result", "")
                    prev_ev = existing_chat.get("events", [])
                    if prev_p and (prev_r or prev_ev):
                        if not turns or turns[-1].get("job_id") != prev_jid:
                            turns.append({
                                "job_id": prev_jid,
                                "prompt": prev_p,
                                "result": prev_r,
                                "events": prev_ev,
                                "status": existing_chat.get("status", "completed"),
                                "created_at": existing_chat.get("updated_at") or existing_chat.get("created_at"),
                                "produced_files": job_scoped_artifacts.get(prev_jid, []),
                                "attachedFiles": prev_attached_files,
                                "model": existing_chat.get("model"),
                                "usage": existing_chat.get("usage"),
                            })
        else:
            # Standard sequential run: flush previous unflushed turn into historical turns
            prev_p = existing_chat.get("prompt", "")
            prev_r = existing_chat.get("result", "")
            prev_ev = existing_chat.get("events", [])
            if prev_p and (prev_r or prev_ev):
                if not turns or turns[-1].get("job_id") != prev_jid:
                    turns.append({
                        "job_id": prev_jid,
                        "prompt": prev_p,
                        "result": prev_r,
                        "events": prev_ev,
                        "status": existing_chat.get("status", "completed"),
                        "created_at": existing_chat.get("updated_at") or existing_chat.get("created_at"),
                        "produced_files": job_scoped_artifacts.get(prev_jid, []),
                        "attachedFiles": prev_attached_files,
                        "model": existing_chat.get("model"),
                        "usage": existing_chat.get("usage"),
                    })

        # Provide context from the latest historical turn if available
        agent_prompt = prompt
        if turns:
            last_turn = turns[-1]
            last_p = last_turn.get("prompt", "")
            last_r = last_turn.get("result", "")
            if last_r:
                clean_prev = last_r[:350].replace("\n", " ").strip()
                agent_prompt = (
                    f"[Context: In previous turn, user asked: '{last_p}'. "
                    f"Result: '{clean_prev}']. Follow-up task: {prompt}"
                )
    else:
        chat_id = (
            req.chat_id.strip()
            if req.chat_id and req.chat_id.strip()
            else f"chat_{uuid.uuid4().hex[:10]}"
        )
        project_id = req.project_id or "default_project"
        title = prompt[:40]
        turns = []
        agent_prompt = prompt

    # Ensure the workspace directory exists before snapshotting.
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)
    files_dir.mkdir(parents=True, exist_ok=True)

    # Capture the pre-execution baseline used later for delta artifact detection.
    pre_snap: Dict[str, Tuple[float, int]] = {}
    if files_dir.exists():
        for root, _dirs, f_names in os.walk(files_dir):
            for f in f_names:
                p = Path(root) / f
                try:
                    rel_p = str(p.relative_to(files_dir)).replace("\\", "/")
                    st = p.stat()
                    pre_snap[rel_p] = (st.st_mtime, st.st_size)
                except Exception:
                    pass
    JOB_PRE_SNAPSHOTS[actual_job_id] = pre_snap

    # Link the job to its chat for downstream lookups.
    JOB_TO_CHAT_ID[actual_job_id] = chat_id
    try:
        setattr(job, "chat_id", chat_id)
    except Exception:
        pass

    # Initialize the live event buffer, mirrored by chat id for convenience.
    ACTIVE_JOB_EVENTS[actual_job_id] = []
    if chat_id:
        ACTIVE_JOB_EVENTS[chat_id] = ACTIVE_JOB_EVENTS[actual_job_id]

    effective_agent = (req.agent_id or "peldrun").strip()

    # Persist the initial session document before scheduling execution.
    project_manager.save_chat_session(
        chat_id=chat_id,
        project_id=project_id,
        title=title,
        job_id=actual_job_id,
        prompt=prompt,
        events=[],
        result="",
        status="running",
        agent_id=effective_agent,
        mode=exec_mode,
        turns=turns,
    )

    # Enrich the persisted session with the LLM override fields.
    try:
        session_file = project_manager.get_chat_dir(chat_id, project_id) / "session.json"
        if session_file.exists():
            s_data = json.loads(session_file.read_text(encoding="utf-8"))
            s_data["turns"] = turns
            s_data["mode"] = exec_mode
            if llm_override:
                s_data["model"] = llm_override.get("model")
                s_data["provider"] = llm_override.get("provider")
                s_data["provider_name"] = (
                    llm_override.get("provider_name") or llm_override.get("provider")
                )
                s_data["agent_id"] = effective_agent
                if "reasoning_effort" in llm_override:
                    s_data["reasoning_effort"] = llm_override["reasoning_effort"]
                # Persist multimodal attachments so the engine can re-resolve
                # them when a run is regenerated or resumed from a checkpoint.
                # The stored shape matches ``AttachmentRef.model_dump`` (camel-
                # cased keys, ``None`` fields omitted) so it survives a JSON
                # round-trip without loss.
                if "attachments" in llm_override:
                    s_data["attachments"] = llm_override["attachments"]
                    # [FIX-WORKSPACE-PERSIST] Copy each temp file to
                    # permanent workspace storage under
                    # ``.peldrun/user_uploads/``. This ensures the image
                    # survives even if the temp artifact store is cleaned
                    # up later. The copy is best-effort: a failure here
                    # must NOT break the run (the temp file still exists
                    # for immediate use).
                    try:
                        import shutil as _shutil
                        from peldrun.storage_paths import (
                            get_storage_root as _get_storage_root,
                        )

                        _temp_root = (
                            _get_storage_root() / "artifacts" / "temp"
                        )
                        _user_uploads_dir = (
                            files_dir / ".peldrun" / "user_uploads"
                        )
                        _user_uploads_dir.mkdir(
                            parents=True, exist_ok=True
                        )
                        for _att in llm_override["attachments"]:
                            _fid = _att.get("fileId")
                            if not isinstance(_fid, str) or not _fid:
                                continue
                            if (
                                "/" in _fid
                                or "\\" in _fid
                                or ".." in _fid
                            ):
                                continue
                            _src = _temp_root / _fid
                            if not _src.is_file():
                                continue
                            _dst = _user_uploads_dir / _fid
                            if _dst.exists():
                                continue
                            try:
                                _shutil.copy2(_src, _dst)
                                logger.info(
                                    "[WORKSPACE-PERSIST] Copied "
                                    "attachment %s to workspace",
                                    _fid,
                                )
                                # [TEMP-CLEANUP-AFTER-PERSIST] The
                                # workspace copy is now authoritative.
                                # Remove the temp original so the temp
                                # store does not accumulate duplicates
                                # of every image ever pasted. All
                                # consumers (executor, dispatcher,
                                # frontend) resolve workspace first —
                                # see the companion changes in
                                # ``peldrun/runtime/executor.py``,
                                # ``omweb/agent_bridge_parts/dispatcher.py``
                                # and ``frontend/.../utils.ts``.
                                try:
                                    _src.unlink(missing_ok=True)
                                    logger.debug(
                                        "[TEMP-CLEANUP-AFTER-PERSIST] "
                                        "Removed temp original for %s",
                                        _fid,
                                    )
                                except Exception as _unlink_err:
                                    logger.debug(
                                        "[TEMP-CLEANUP-AFTER-PERSIST] "
                                        "Failed to remove temp original "
                                        "for %s: %s",
                                        _fid,
                                        _unlink_err,
                                    )
                            except Exception as _copy_err:
                                # Copy failed — temp original stays as
                                # the authoritative fallback. Do NOT
                                # attempt to delete it (the unlink is
                                # inside the success branch only).
                                logger.debug(
                                    "[WORKSPACE-PERSIST] Copy failed "
                                    "for %s: %s",
                                    _fid,
                                    _copy_err,
                                )
                    except Exception as _persist_err:
                        logger.debug(
                            "[WORKSPACE-PERSIST] Attachment persist "
                            "pipeline failed: %s",
                            _persist_err,
                        )
            session_file.write_text(
                json.dumps(s_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception:
        pass

    # Web Search is a chat-only feature; ignored in agent mode and never part of llm_override.
    web_search_enabled = bool(req.web_search_enabled) if exec_mode == "chat" else False

    # Register execution run with Resource Monitor in PENDING status prior to task launch
    try:
        from omweb.services.resource_monitor_service import get_resource_monitor_service

        get_resource_monitor_service().create_run(
            run_id=actual_job_id,
            job_id=actual_job_id,
            chat_id=chat_id,
            agent_id=effective_agent,
            engine_type=exec_mode,
        )
    except Exception as exc:
        logger.warning(
            "Failed to register execution run %s in ResourceMonitor: %s",
            actual_job_id,
            exc,
        )

    # Schedule the actual execution task.
    if exec_mode == "chat":
        exec_task = asyncio.create_task(
            run_direct_chat(
                actual_job_id,
                prompt,
                llm_override,
                web_search_enabled=web_search_enabled,
            )
        )
    else:
        exec_task = asyncio.create_task(
            run_instrumented(actual_job_id, agent_prompt, effective_agent, llm_override)
        )

    ACTIVE_JOB_TASKS[actual_job_id] = exec_task
    if chat_id:
        ACTIVE_JOB_TASKS[chat_id] = exec_task

    def _cleanup_task(_):
        ACTIVE_JOB_TASKS.pop(actual_job_id, None)
        if chat_id:
            ACTIVE_JOB_TASKS.pop(chat_id, None)

    exec_task.add_done_callback(_cleanup_task)

    return {
        "job_id": actual_job_id,
        "status": "running",
        "chat_id": chat_id,
        "mode": exec_mode,
        "model": llm_override.get("model"),
        "provider": llm_override.get("provider"),
        "provider_name": llm_override.get("provider_name") or llm_override.get("provider"),
        "agent_id": effective_agent,
        "reasoning_effort": llm_override.get("reasoning_effort"),
        "generative_ui_enabled": bool(req.generative_ui and req.generative_ui.get("enabled")),
        "attachments_count": len(llm_override.get("attachments") or []),
    }


@router.post("/{run_id}/resume", status_code=http_status.HTTP_202_ACCEPTED)
async def resume_run_endpoint(run_id: str):
    """
    Resume an interrupted run from its last checkpoint.

    Fire-and-forget: returns ``202 Accepted`` immediately after
    scheduling the background resume task, then streams progress
    through the normal SSE channel. The durable outcome is written
    to ``session.json`` when the task completes.

    Responses
    ---------
    202 Accepted
        The run is resumable and a background task has been scheduled.
   400 Bad Request
        ``run_id`` is empty.
   404 Not Found
        No session matches ``run_id``.
   409 Conflict
        The session exists but is not resumable. The response body
        carries ``reason`` describing why (``already_terminal``,
        ``cancelled_by_operator``, ``no_checkpoint``,
        ``concurrent_resume``, ...).
    """
    if not run_id:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="run_id is required.",
        )

    session_file, data = _find_session_by_run_id(run_id)
    if session_file is None or data is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' was not found.",
        )

    # Verify the session is in a resumable state per the startup sweep.
    # The startup sweep is authoritative: it consults RecoveryManager
    # and only sets ``resumable=true`` when plan_resume() returned a
    # resumable plan. Bypassing that flag would risk double-executing
    # side effects if the state changed since the sweep.
    if not data.get("resumable", False):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "error": "not_resumable",
                "reason": data.get("status", "unknown"),
                "message": (
                    f"Run '{run_id}' is not resumable. Current status: "
                    f"{data.get('status', 'unknown')}. The startup "
                    f"recovery sweep did not mark this run as "
                    f"resumable."
                ),
            },
        )

    if data.get("status") not in ("interrupted", "waiting_for_input"):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "error": "not_resumable",
                "reason": f"status={data.get('status')}",
                "message": (
                    f"Run '{run_id}' has status "
                    f"'{data.get('status')}', which cannot be "
                    f"resumed. Only 'interrupted' and "
                    f"'waiting_for_input' runs support resume."
                ),
            },
        )

    # Re-run plan_resume now. The startup sweep's ``resumable`` flag
    # can be stale if the state changed since (e.g. another operator
    # already answered a HITL, or the checkpoint was deleted). The
    # manager re-plans under its per-run lock, so this is safe.
    try:
        store = get_run_store()
        manager = RecoveryManager(persistence=store)
        plan = await manager.plan_resume(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("resume: plan_resume failed for %s: %s", run_id, exc)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not plan resume for '{run_id}': {exc}",
        ) from exc

    if not plan.resumable:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "error": "not_resumable",
                "reason": plan.reason.value,
                "message": plan.reason_text or plan.reason.value,
            },
        )

    chat_id = str(data.get("id") or run_id)
    job_id = str(data.get("job_id") or run_id)

    # Idempotency: refuse to double-schedule a run whose task is
    # already alive. The manager's own lock prevents the actual
    # concurrency, but catching it here gives the caller a precise
    # ``409 concurrent_resume`` instead of a background failure
    # they would have to discover from the logs.
    existing_task = ACTIVE_JOB_TASKS.get(job_id) or ACTIVE_JOB_TASKS.get(chat_id)
    if existing_task is not None:
        done_fn = getattr(existing_task, "done", None)
        if callable(done_fn) and not done_fn():
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail={
                    "error": "concurrent_resume",
                    "reason": "task_already_running",
                    "message": (
                        f"Run '{run_id}' already has an active task."
                    ),
                },
            )

    # Schedule the background resume. The task registers itself in
    # ACTIVE_JOB_TASKS under both keys so the SSE endpoint and the
    # cancel endpoint can find it.
    resume_task = asyncio.create_task(
        _execute_resume_flow(
            run_id=run_id,
            session_file=session_file,
            chat_id=chat_id,
            job_id=job_id,
        )
    )
    ACTIVE_JOB_TASKS[job_id] = resume_task
    if chat_id != job_id:
        ACTIVE_JOB_TASKS[chat_id] = resume_task

    def _cleanup_task(_):
        ACTIVE_JOB_TASKS.pop(job_id, None)
        ACTIVE_JOB_TASKS.pop(chat_id, None)

    resume_task.add_done_callback(_cleanup_task)

    # Register with ResourceMonitor so the run appears in the
    # resource dashboard for its lifetime.
    try:
        from omweb.services.resource_monitor_service import (
            get_resource_monitor_service,
        )

        get_resource_monitor_service().create_run(
            run_id=job_id,
            job_id=job_id,
            chat_id=chat_id,
            agent_id=str(data.get("agent_id") or "peldrun"),
            engine_type=str(data.get("mode") or "agent"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "resume: resource monitor registration failed: %s", exc
        )

    logger.info(
        "resume: scheduled for run=%s job=%s chat=%s reason=%s next_step=%d",
        run_id,
        job_id,
        chat_id,
        plan.reason.value,
        plan.next_step,
    )

    return {
        "run_id": run_id,
        "job_id": job_id,
        "chat_id": chat_id,
        "status": "resuming",
        "resume_reason": plan.reason.value,
        "next_step": int(plan.next_step or 0),
        "has_checkpoint": plan.latest_checkpoint is not None,
        "pending_hitl": len(plan.pending_human_requests),
    }
