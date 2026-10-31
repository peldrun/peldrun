"""
Server-Sent Events streaming endpoint.

Implements ``GET /jobs/{job_id}/stream`` which replays historical events
from the run store and then continuously forwards live events published on
the SSE bus until the job reaches a terminal state.

On terminal events the endpoint also:

* Computes the turn-specific file delta and stores it against the job.
* Persists the final session document (result, usage, events).
* Writes per-turn token usage back into the persisted session.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
import uuid

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from omweb.agent_bridge import job_scoped_artifacts
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager
from omweb.sse_events import subscribe_events
from peldrun.runtime.store import get_run_store
from peldrun.runtime.usage_store import get_usage_store

from .state import (
    ACTIVE_JOB_EVENTS,
    ACTIVE_JOB_TASKS,
    JOB_DELTA_ARTIFACTS,
)
from .utils import compute_job_delta_files, resolve_chat


router = APIRouter()


@router.get("/jobs/{job_id}/stream")
async def stream_job_events(job_id: str, after_sequence: int = Query(0, ge=0)):
    """
    Stream job events over SSE.

    Historical events stored in the run store are replayed first (filtered by
    ``after_sequence``), then live events are forwarded until a terminal event
    is observed.
    """
    job = job_manager.get_job(job_id)
    chat, chat_id = resolve_chat(job_id)
    chat = chat or {}

    actual_job_id = (chat.get("job_id") if chat else None) or job_id
    store = get_run_store()
    usage_store = get_usage_store()

    # Shared event buffer, used to accumulate the events of this turn.
    collected_events: List[Dict[str, Any]] = (
        ACTIVE_JOB_EVENTS.get(actual_job_id)
        or ACTIVE_JOB_EVENTS.get(job_id)
        or []
    )
    if not collected_events:
        ACTIVE_JOB_EVENTS[actual_job_id] = collected_events
        ACTIVE_JOB_EVENTS[job_id] = collected_events

    async def event_generator():
        current_step = 1
        max_seen_sequence = after_sequence

        # ------------------------------------------------------------------
        # Phase 1: replay stored events from the run store.
        # ------------------------------------------------------------------
        replayed_records: List[Dict[str, Any]] = []
        try:
            stored_raw = await store.get_events_after(
                actual_job_id, sequence=after_sequence
            )
            if not stored_raw and actual_job_id != job_id:
                stored_raw = await store.get_events_after(
                    job_id, sequence=after_sequence
                )

            for item in stored_raw:
                seq = item["sequence"]
                ev_type = item["type"]
                payload = item["payload"]
                step_val = item["step"] or 1
                ev_id = item["event_id"]

                content_val = (
                    payload.get("content")
                    or payload.get("thought")
                    or payload.get("output")
                    or payload.get("arguments")
                    or ""
                )
                tool_val = payload.get("tool_name") or payload.get("tool") or ""

                event_dict = {
                    "id": ev_id,
                    "type": str(ev_type),
                    "step": step_val,
                    "content": str(content_val),
                    "toolName": str(tool_val) if tool_val else None,
                    "data": payload,
                    "seq": seq,
                }
                replayed_records.append(event_dict)
        except Exception:
            # Replay is best-effort; live streaming remains available.
            pass

        # Fallback: if the store had nothing, replay from the in-memory buffer.
        if not replayed_records and collected_events:
            for ev in collected_events:
                ev_seq = ev.get("seq") or ev.get("sequence") or 0
                if ev_seq > after_sequence:
                    replayed_records.append(ev)

        for rep in replayed_records:
            seq_val = rep.get("seq", 0)
            if seq_val > max_seen_sequence:
                max_seen_sequence = seq_val

            ev_type_str = rep.get("type", "thought")
            ev_json = json.dumps(rep, ensure_ascii=False)
            yield {"event": str(ev_type_str), "data": ev_json}

            if str(ev_type_str).lower() in ("final", "error", "cancelled", "done"):
                return

        # If the job is not running and there is nothing new to replay,
        # there is no point keeping the connection open.
        is_actively_running = (
            job_id in ACTIVE_JOB_TASKS
            or actual_job_id in ACTIVE_JOB_TASKS
            or (chat_id and chat_id in ACTIVE_JOB_TASKS)
        )
        if not is_actively_running and replayed_records:
            return

        # ------------------------------------------------------------------
        # Phase 2: forward live events until a terminal event is seen.
        # ------------------------------------------------------------------
        async for event in subscribe_events(job_id):
            ev_type = (
                event.type.value
                if hasattr(event.type, "value")
                else str(event.type)
            )
            ev_step = getattr(event, "step", None)
            if ev_step:
                current_step = ev_step

            # Normalize the raw event payload into a plain dict.
            if hasattr(event, "to_json"):
                try:
                    raw_dict = json.loads(event.to_json())
                except Exception:
                    raw_dict = {"data": getattr(event, "data", {})}
            else:
                raw_dict = {"data": getattr(event, "data", {})}

            data_payload = raw_dict.get("data", {})
            if not isinstance(data_payload, dict):
                data_payload = {"content": str(data_payload)}
                raw_dict["data"] = data_payload

            # Hoist nested data keys to the top level for client convenience.
            for k, v in data_payload.items():
                if k not in raw_dict:
                    raw_dict[k] = v

            # Enforce monotonic sequence numbers so replays and live events
            # never overlap or move backwards.
            live_seq = data_payload.get("seq") or raw_dict.get("seq") or 0
            if isinstance(live_seq, int) and live_seq > 0:
                if live_seq <= max_seen_sequence:
                    continue
                max_seen_sequence = live_seq
            else:
                max_seen_sequence += 1
                live_seq = max_seen_sequence
                raw_dict["seq"] = live_seq
                data_payload["seq"] = live_seq

            raw_dict["id"] = raw_dict.get("id") or f"evt_{uuid.uuid4().hex[:8]}"
            raw_dict["type"] = str(ev_type)
            raw_dict["step"] = current_step

            content_val = (
                raw_dict.get("content")
                or data_payload.get("content")
                or data_payload.get("thought")
                or data_payload.get("output")
                or data_payload.get("arguments")
                or ""
            )
            raw_dict["content"] = str(content_val)

            tool_name_val = (
                raw_dict.get("toolName")
                or data_payload.get("toolName")
                or raw_dict.get("name")
                or data_payload.get("name")
                or ""
            )
            if tool_name_val:
                raw_dict["toolName"] = str(tool_name_val)

            ev_data_str = json.dumps(raw_dict, ensure_ascii=False)

            # Persist a condensed copy in the collected buffer.
            event_record = {
                "id": raw_dict["id"],
                "type": str(ev_type),
                "step": current_step,
                "content": raw_dict["content"],
                "toolName": raw_dict.get("toolName"),
                "data": data_payload,
                "seq": live_seq,
            }

            if str(ev_type).lower() not in ["ping"]:
                collected_events.append(event_record)
                if job:
                    try:
                        job_manager.append_event(job_id, event_record)
                    except Exception:
                        pass

            # --------------------------------------------------------------
            # Terminal event handling: persist the final session state.
            # --------------------------------------------------------------
            is_term = str(ev_type).lower() in ["final", "error", "cancelled", "done"]
            if is_term:
                res_data = raw_dict.get("data", {})
                final_res = (
                    res_data.get("result", "")
                    if isinstance(res_data, dict)
                    else str(res_data)
                )

                # Fall back to the last meaningful thought as the result.
                if not final_res or final_res == "{}":
                    for e in reversed(collected_events):
                        if e.get("type") == "thought":
                            t_val = e.get("data", {}).get("thought") or e.get("content")
                            if t_val:
                                final_res = t_val
                                break

                p_id = chat.get("project_id", "default_project")
                prompt_val = getattr(job, "prompt", None) or chat.get("prompt", "")
                final_status = (
                    "completed"
                    if str(ev_type).lower() in ["final", "done"]
                    else "failed"
                )

                fresh_summary = await usage_store.get_chat_usage_summary(chat_id)
                turn_usage = (
                    res_data.get("usage") if isinstance(res_data, dict) else None
                )

                # Compute the delta of files for this specific turn.
                files_dir_for_delta = project_manager.get_chat_files_dir(chat_id, p_id)
                calculated_delta = compute_job_delta_files(job_id, files_dir_for_delta)
                JOB_DELTA_ARTIFACTS[job_id] = calculated_delta
                job_scoped_artifacts[job_id] = calculated_delta

                # [FIX-ATTACHMENTS-PERSIST] Capture the previous turn's
                # multimodal attachments BEFORE save_chat_session overwrites
                # the file. save_chat_session performs a full-file overwrite
                # without an attachments parameter, so without this capture
                # the multimodal references are silently lost on every
                # terminal event.
                _prev_attachments_for_persist = None
                try:
                    _prev_session_path = (
                        project_manager.get_chat_dir(chat_id, p_id) / "session.json"
                    )
                    if _prev_session_path.exists():
                        _prev_data = json.loads(
                            _prev_session_path.read_text(encoding="utf-8")
                        )
                        _prev_attachments_for_persist = _prev_data.get(
                            "attachments"
                        )
                except Exception:
                    _prev_attachments_for_persist = None

                project_manager.save_chat_session(
                    chat_id=chat_id,
                    project_id=p_id,
                    title=prompt_val[:40] if prompt_val else chat_id,
                    job_id=job_id,
                    prompt=prompt_val,
                    events=collected_events,
                    result=final_res,
                    status=final_status,
                    agent_id=chat.get("agent_id", "peldrun"),
                    mode=chat.get("mode", "agent"),
                    usage_summary=fresh_summary,
                    turns=chat.get("turns", []),
                )

                # Patch the session with the latest usage data, restore
                # the multimodal attachments captured above, and attach
                # per-turn usage to the last turn.
                try:
                    s_file = project_manager.get_chat_dir(chat_id, p_id) / "session.json"
                    if s_file.exists():
                        s_data = json.loads(s_file.read_text(encoding="utf-8"))
                        if fresh_summary and fresh_summary.get("total_tokens", 0) > 0:
                            s_data["usage_summary"] = fresh_summary
                        if turn_usage:
                            s_data["usage"] = turn_usage
                            turns_list = s_data.get("turns", [])
                            if turns_list:
                                turns_list[-1]["usage"] = turn_usage
                            s_data["turns"] = turns_list
                        # [FIX-ATTACHMENTS-PERSIST] Restore.
                        if _prev_attachments_for_persist is not None:
                            s_data["attachments"] = _prev_attachments_for_persist
                        s_file.write_text(
                            json.dumps(s_data, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                except Exception:
                    pass

            yield {"event": str(ev_type), "data": ev_data_str}
            if is_term:
                break

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
