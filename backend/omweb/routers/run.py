"""
backend/omweb/routers/run.py

PELDRUN Universal Run Router.
Manages job lifecycles, chat associations, real-time SSE streaming, and deliverable downloads.
Hardened under Phase M2 & Defect Resolution:
- Resilient deliverable resolution: Eliminates ghost 0-byte file shadowing when nested deliverables exist.
- Intelligent content fallback: Automatically resolves subfolder deliverables if root file is empty or missing.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
import zipfile

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from omweb.agent_bridge import (
    active_tasks,
    human_answers,
    human_data,
    job_scoped_artifacts,
    run_direct_chat,
    run_instrumented,
)
from omweb.config import get_storage_root
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager
from omweb.sse_events import SSEEventType, subscribe_events
from peldrun.tools.builtins.human_input import HumanInputRegistry

router = APIRouter()

JOB_TO_CHAT_ID: Dict[str, str] = {}
ACTIVE_JOB_TASKS: Dict[str, asyncio.Task] = {}
ACTIVE_JOB_EVENTS: Dict[str, List[Dict[str, Any]]] = {}


class RunRequest(BaseModel):
    prompt: str
    project_id: Optional[str] = "default_project"
    max_steps: Optional[int] = 30
    agent_id: Optional[str] = "peldrun"
    chat_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    provider_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_type: Optional[str] = None
    mode: Optional[str] = "agent"


class FileContentPayload(BaseModel):
    path: str
    content: str


class HumanResponsePayload(BaseModel):
    answer: str


def resolve_chat(identifier: str) -> Tuple[Optional[Dict[str, Any]], str]:
    if not identifier:
        return None, ""
    direct_chat = project_manager.get_chat(identifier)
    if direct_chat:
        return direct_chat, direct_chat.get("id", identifier)
    if identifier in JOB_TO_CHAT_ID:
        cid = JOB_TO_CHAT_ID[identifier]
        return project_manager.get_chat(cid), cid
    job = job_manager.get_job(identifier)
    if job and hasattr(job, "chat_id") and getattr(job, "chat_id"):
        cid = getattr(job, "chat_id")
        return project_manager.get_chat(cid), cid
    for c_item in project_manager.list_chats():
        cid = c_item.get("id")
        if cid:
            fc = project_manager.get_chat(cid)
            if fc:
                if fc.get("job_id") == identifier:
                    return fc, cid
                for turn in fc.get("turns", []):
                    if turn.get("job_id") == identifier:
                        return fc, cid
    fallback_id = identifier if identifier.startswith("chat_") else f"chat_{identifier}"
    return None, fallback_id


@router.post("")
@router.post("/")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks):
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    exec_mode = "agent"
    raw_mode = getattr(req, "mode", None)
    if isinstance(raw_mode, str) and raw_mode.strip():
        m_val = raw_mode.strip().lower()
        if m_val in ["agent", "chat"]:
            exec_mode = m_val

    generated_job_id = f"job_{uuid.uuid4().hex[:12]}"
    try:
        job = job_manager.create_job(generated_job_id, prompt=prompt)
    except TypeError:
        job = job_manager.create_job(generated_job_id)
        if hasattr(job, "prompt"):
            job.prompt = prompt

    actual_job_id = getattr(job, "id", generated_job_id)

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

    existing_chat = None
    if req.chat_id:
        existing_chat, _ = resolve_chat(req.chat_id)

    if existing_chat:
        chat_id = existing_chat.get("id", req.chat_id)
        project_id = existing_chat.get("project_id", req.project_id or "default_project")
        title = existing_chat.get("title") or prompt[:40]
        turns = list(existing_chat.get("turns", []))

        prev_p = existing_chat.get("prompt", "")
        prev_r = existing_chat.get("result", "")
        prev_ev = existing_chat.get("events", [])
        prev_jid = existing_chat.get("job_id", "")
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
                    "model": existing_chat.get("model"),
                })

        agent_prompt = prompt
        if prev_r:
            clean_prev = prev_r[:350].replace("\n", " ").strip()
            agent_prompt = f"[Context: In previous turn, user asked: '{prev_p}'. Result: '{clean_prev}']. Follow-up task: {prompt}"
    else:
        chat_id = req.chat_id.strip() if req.chat_id and req.chat_id.strip() else f"chat_{uuid.uuid4().hex[:10]}"
        project_id = req.project_id or "default_project"
        title = prompt[:40]
        turns = []
        agent_prompt = prompt

    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)
    files_dir.mkdir(parents=True, exist_ok=True)

    JOB_TO_CHAT_ID[actual_job_id] = chat_id
    try:
        setattr(job, "chat_id", chat_id)
    except Exception:
        pass

    ACTIVE_JOB_EVENTS[actual_job_id] = []
    if chat_id:
        ACTIVE_JOB_EVENTS[chat_id] = ACTIVE_JOB_EVENTS[actual_job_id]

    effective_agent = (req.agent_id or "peldrun").strip()

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
    )

    try:
        session_file = project_manager.get_chat_dir(chat_id, project_id) / "session.json"
        if session_file.exists():
            s_data = json.loads(session_file.read_text(encoding="utf-8"))
            s_data["turns"] = turns
            s_data["mode"] = exec_mode
            if llm_override:
                s_data["model"] = llm_override.get("model")
                s_data["provider"] = llm_override.get("provider")
                s_data["provider_name"] = llm_override.get("provider_name") or llm_override.get("provider")
                s_data["agent_id"] = effective_agent
            session_file.write_text(json.dumps(s_data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    if exec_mode == "chat":
        exec_task = asyncio.create_task(run_direct_chat(actual_job_id, prompt, llm_override))
    else:
        exec_task = asyncio.create_task(run_instrumented(actual_job_id, agent_prompt, effective_agent, llm_override))

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
    }


@router.get("/jobs")
@router.get("/jobs/")
async def list_jobs():
    return {"jobs": job_manager.list_jobs()}


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: str):
    chat, chat_id = resolve_chat(job_id)
    chat = chat or {}

    actual_job_id = job_id
    if chat and chat.get("job_id"):
        actual_job_id = chat.get("job_id")

    job = job_manager.get_job(job_id) or job_manager.get_job(actual_job_id)

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

    is_actively_running = (
        job_id in ACTIVE_JOB_TASKS
        or actual_job_id in ACTIVE_JOB_TASKS
        or (chat_id and chat_id in ACTIVE_JOB_TASKS)
    )

    if is_actively_running:
        effective_status = "running"
    else:
        effective_status = (getattr(job, "status", "") if job else "") or chat.get("status", "completed")

    effective_result = (getattr(job, "result", None) if job else None) or chat.get("result", "")

    if not effective_result and effective_events:
        for ev in reversed(effective_events):
            ev_type = ev.get("type", "") if isinstance(ev, dict) else getattr(ev, "type", "")
            if str(ev_type).lower() == "final":
                data_part = ev.get("data", {}) if isinstance(ev, dict) else getattr(ev, "data", {})
                if isinstance(data_part, dict):
                    effective_result = data_part.get("result", "")
                elif isinstance(data_part, str):
                    effective_result = data_part
                break

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
        "created_at": chat.get("created_at"),
        "agent_id": chat.get("agent_id", "peldrun"),
        "model": chat.get("model"),
        "mode": chat.get("mode", "agent"),
        "produced_files": resolved_files,
    }


@router.get("/jobs/{job_id}/files")
async def get_job_files(job_id: str):
    """
    Retrieve deliverables list for a job or chat.
    Intelligently deduplicates 0-byte ghost files if an equivalent nested deliverable exists.
    """
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    if not files_dir.exists():
        return {"job_id": job_id, "chat_id": chat_id, "files": [], "all_files": []}

    all_files_list = []
    for root, dirs, files in os.walk(files_dir):
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(files_dir)
            all_files_list.append({
                "name": f,
                "path": str(rel).replace("\\", "/"),
                "size": p.stat().st_size,
            })

    # Find all file names that have non-empty versions in nested subfolders
    nested_non_empty = {
        item["name"]
        for item in all_files_list
        if "/" in item["path"] and item["size"] > 0
    }

    # Suppress root 0-byte ghost files that are shadowed by nested deliverables
    sanitized_files: List[Dict[str, Any]] = []
    for item in all_files_list:
        if "/" not in item["path"] and item["size"] == 0 and item["name"] in nested_non_empty:
            continue
        sanitized_files.append(item)

    actual_job_id = chat.get("job_id") or job_id
    turn_files = (
        job_scoped_artifacts.get(job_id)
        or job_scoped_artifacts.get(actual_job_id)
        or (job_scoped_artifacts.get(chat_id) if chat_id else None)
    )

    if turn_files is not None and len(turn_files) > 0:
        scoped_list = [
            f for f in sanitized_files
            if f["path"] in turn_files or f["name"] in turn_files
        ]
    else:
        scoped_list = sanitized_files

    # Sort files to prioritize non-empty deliverables
    scoped_list.sort(key=lambda x: (0 if x["size"] > 0 else 1, x["path"]))

    return {
        "job_id": actual_job_id,
        "chat_id": chat_id,
        "files": scoped_list,
        "all_files": sanitized_files,
    }


def _resolve_physical_file(files_dir: Path, requested_path: str) -> Optional[Path]:
    """
    Intelligently resolves requested relative path against files_dir.
    If the requested path is a 0-byte file in root, checks if a non-empty
    nested file with the same name exists (e.g. todo-app/index.html).
    """
    clean_rel = requested_path.lstrip("/\\")
    direct_target = (files_dir / clean_rel).resolve()

    # If direct target exists and has content, return it
    if direct_target.is_relative_to(files_dir) and direct_target.is_file():
        if direct_target.stat().st_size > 0 or "/" in clean_rel:
            return direct_target

    # Fallback search: Look for non-empty nested matching file
    target_name = Path(clean_rel).name
    for sub_file in files_dir.rglob(target_name):
        if sub_file.is_file() and sub_file.stat().st_size > 0:
            return sub_file

    # If direct target exists even if 0-byte, fallback to it
    if direct_target.is_relative_to(files_dir) and direct_target.is_file():
        return direct_target

    return None


@router.get("/jobs/{job_id}/content")
async def get_job_file_content(job_id: str, path: str = Query(...)):
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    target = _resolve_physical_file(files_dir, path)
    if not target or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in job deliverables")

    try:
        content = target.read_text(encoding="utf-8")
        rel_str = str(target.relative_to(files_dir)).replace("\\", "/")
        return {"path": rel_str, "content": content}
    except Exception as e:
        return {"path": path, "content": f"Binary content: {str(e)}"}


@router.get("/jobs/{job_id}/raw/{filepath:path}")
async def get_job_raw_file(job_id: str, filepath: str):
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    target = _resolve_physical_file(files_dir, filepath)
    if not target or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in job deliverables")

    content_type, _ = mimetypes.guess_type(str(target))
    ext = target.suffix.lower()
    if ext == ".css":
        content_type = "text/css"
    elif ext in [".js", ".mjs"]:
        content_type = "application/javascript"
    elif ext in [".html", ".htm"]:
        content_type = "text/html"
    elif ext == ".png":
        content_type = "image/png"
    elif ext in [".jpg", ".jpeg"]:
        content_type = "image/jpeg"
    elif ext == ".webp":
        content_type = "image/webp"
    elif ext == ".svg":
        content_type = "image/svg+xml"
    elif ext == ".pdf":
        content_type = "application/pdf"
    elif ext == ".mp4":
        content_type = "video/mp4"

    return FileResponse(target, media_type=content_type or "application/octet-stream")


@router.get("/jobs/{job_id}/download-zip")
async def download_job_zip(job_id: str):
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    if not files_dir.exists():
        raise HTTPException(status_code=404, detail="No files found for this job")

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(files_dir):
            for f in files:
                fp = Path(root) / f
                arcname = fp.relative_to(files_dir)
                zip_file.write(fp, arcname=str(arcname))

    zip_buffer.seek(0)
    filename = f"{chat_id}_deliverables.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/jobs/{job_id}/stream")
async def stream_job_events(job_id: str):
    job = job_manager.get_job(job_id)
    chat, chat_id = resolve_chat(job_id)
    chat = chat or {}

    job_mem_status = getattr(job, "status", None)

    if job and job_mem_status in ["completed", "failed"]:
        memory_events = getattr(job, "events", [])
        async def replay_generator():
            for ev in memory_events:
                ev_type = ev.get("type", "thought") if isinstance(ev, dict) else "thought"
                ev_data = json.dumps(ev, ensure_ascii=False) if isinstance(ev, dict) else str(ev)
                yield {"event": str(ev_type), "data": ev_data}
            yield {"event": "done", "data": json.dumps({"status": job_mem_status})}
        return EventSourceResponse(replay_generator())

    collected_events: List[Dict[str, Any]] = []
    current_step = 1

    ACTIVE_JOB_EVENTS[job_id] = collected_events
    if chat_id:
        ACTIVE_JOB_EVENTS[chat_id] = collected_events

    async def event_generator():
        nonlocal current_step
        async for event in subscribe_events(job_id):
            ev_type = event.type.value if hasattr(event.type, "value") else str(event.type)
            ev_step = getattr(event, "step", None)
            if ev_step:
                current_step = ev_step

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

            for k, v in data_payload.items():
                if k not in raw_dict:
                    raw_dict[k] = v

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

            event_record = {
                "id": raw_dict["id"],
                "type": str(ev_type),
                "step": current_step,
                "content": raw_dict["content"],
                "toolName": raw_dict.get("toolName"),
                "data": data_payload,
            }

            if str(ev_type).lower() not in ["ping"]:
                collected_events.append(event_record)
                if job:
                    try:
                        job_manager.append_event(job_id, event_record)
                    except Exception:
                        pass

            is_term = str(ev_type).lower() in ["final", "error", "done"]
            if is_term:
                res_data = raw_dict.get("data", {})
                final_res = res_data.get("result", "") if isinstance(res_data, dict) else str(res_data)

                if not final_res or final_res == "{}":
                    for e in reversed(collected_events):
                        if e.get("type") == "thought":
                            t_val = e.get("data", {}).get("thought") or e.get("content")
                            if t_val:
                                final_res = t_val
                                break

                p_id = chat.get("project_id", "default_project")
                prompt_val = getattr(job, "prompt", None) or chat.get("prompt", "")
                final_status = "completed" if str(ev_type).lower() in ["final", "done"] else "failed"

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
                )

                try:
                    s_file = project_manager.get_chat_dir(chat_id, p_id) / "session.json"
                    if s_file.exists():
                        c_json = json.loads(s_file.read_text(encoding="utf-8"))
                        c_json["turns"] = chat.get("turns", [])
                        c_json["produced_files"] = (
                            job_scoped_artifacts.get(job_id)
                            or (job_scoped_artifacts.get(chat_id) if chat_id else [])
                            or []
                        )
                        s_file.write_text(json.dumps(c_json, indent=2, ensure_ascii=False), encoding="utf-8")
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


class HumanRespondRequest(BaseModel):
    answer: str
    request_id: Optional[str] = None


@router.post("/jobs/{job_id}/stop")
@router.post("/jobs/{job_id}/cancel")
async def stop_job(job_id: str):
    """Stop/cancel a running job by job_id or chat_id."""
    chat, chat_id = resolve_chat(job_id)
    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    task = (
        ACTIVE_JOB_TASKS.get(job_id)
        or ACTIVE_JOB_TASKS.get(target_job_id)
        or (ACTIVE_JOB_TASKS.get(chat_id) if chat_id else None)
    )
    task_cancelled = False
    if task and not task.done():
        task.cancel()
        task_cancelled = True

    job_manager.update_status(target_job_id, status="stopped", error="Task stopped by user")
    if job_id != target_job_id:
        job_manager.update_status(job_id, status="stopped", error="Task stopped by user")

    if chat_id:
        try:
            target_chat, _ = resolve_chat(job_id)
            target_pid = (target_chat or {}).get("project_id", "default_project")
            session_file = project_manager.get_chat_dir(chat_id, target_pid) / "session.json"
            if session_file.exists():
                s_data = json.loads(session_file.read_text(encoding="utf-8"))
                s_data["status"] = "stopped"
                session_file.write_text(json.dumps(s_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    try:
        from omweb.sse_events import SSEEvent, SSEEventType
        from omweb.agent_bridge import dispatch_event
        await dispatch_event(
            target_job_id,
            SSEEvent(type=SSEEventType.STATUS, data={"status": "stopped", "message": "Task stopped by user"}),
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


@router.post("/jobs/{job_id}/respond")
async def respond_to_human_prompt(job_id: str, req: HumanRespondRequest):
    """Receive human input response for ask_human tool requests in both Core and Legacy engines."""
    chat, chat_id = resolve_chat(job_id)
    target_job_id = job_id
    if chat and chat.get("job_id"):
        target_job_id = chat.get("job_id")

    answer = req.answer.strip()
    core_resolved = False

    if req.request_id:
        core_resolved = HumanInputRegistry.resolve_request(req.request_id, answer)
    elif len(HumanInputRegistry._pending_requests) == 1:
        single_req_id = list(HumanInputRegistry._pending_requests.keys())[0]
        core_resolved = HumanInputRegistry.resolve_request(single_req_id, answer)

    legacy_event = human_answers.get(target_job_id) or human_answers.get(job_id)
    if legacy_event and not legacy_event.is_set():
        human_data[target_job_id] = answer
        human_data[job_id] = answer
        legacy_event.set()

    job_manager.append_event(target_job_id, {
        "type": "human_response",
        "answer": answer,
        "request_id": req.request_id,
        "timestamp": time.time(),
    })

    return {
        "success": True,
        "job_id": target_job_id,
        "chat_id": chat_id,
        "answer": answer,
        "core_resolved": core_resolved,
        "message": "Response recorded and agent unblocked successfully",
    }