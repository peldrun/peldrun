"""
Run creation endpoint.

Handles ``POST /`` (and ``POST ""``) to start a new agent or direct-chat
execution. The endpoint is responsible for:

* Validating the incoming prompt and execution mode.
* Creating the backing job and chat session records.
* Persisting the resolved LLM overrides on the session.
* Capturing a pre-execution filesystem snapshot so the delta artifacts can
  later be computed for this specific turn.
* Scheduling the actual execution task (agent or direct chat).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException

from omweb.agent_bridge import (
    job_scoped_artifacts,
    run_direct_chat,
    run_instrumented,
)
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager

from .schemas import RunRequest
from .state import (
    ACTIVE_JOB_EVENTS,
    ACTIVE_JOB_TASKS,
    JOB_PRE_SNAPSHOTS,
    JOB_TO_CHAT_ID,
)
from .utils import resolve_chat


router = APIRouter()


@router.post("")
@router.post("/")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks):
    """Create a job, capture a filesystem baseline, and schedule execution."""
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    # Normalize the requested execution mode; anything unknown falls back to "agent".
    exec_mode = "agent"
    raw_mode = getattr(req, "mode", None)
    if isinstance(raw_mode, str) and raw_mode.strip():
        m_val = raw_mode.strip().lower()
        if m_val in ["agent", "chat"]:
            exec_mode = m_val

    # Create the backing job record. Some job_manager versions accept a prompt
    # keyword; older ones only accept an id, so we handle both signatures.
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
    if req.generative_ui:
        llm_override["generative_ui"] = req.generative_ui

    # Resolve or create the chat session.
    existing_chat = None
    if req.chat_id:
        existing_chat, _ = resolve_chat(req.chat_id)

    if existing_chat:
        chat_id = existing_chat.get("id", req.chat_id)
        project_id = existing_chat.get("project_id", req.project_id or "default_project")
        title = existing_chat.get("title") or prompt[:40]
        turns = list(existing_chat.get("turns", []))

        # Flush the previous (unflushed) turn into the turns list so the new
        # turn starts from a clean state.
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
                    "usage": existing_chat.get("usage"),
                })

        # Provide the agent with lightweight context from the previous turn.
        agent_prompt = prompt
        if prev_r:
            clean_prev = prev_r[:350].replace("\n", " ").strip()
            agent_prompt = (
                f"[Context: In previous turn, user asked: '{prev_p}'. "
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
                if "generative_ui" in llm_override:
                    s_data["generative_ui"] = llm_override["generative_ui"]
            session_file.write_text(
                json.dumps(s_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception:
        # Session enrichment is best-effort; execution must not be blocked.
        pass

    # Schedule the actual execution task.
    if exec_mode == "chat":
        exec_task = asyncio.create_task(
            run_direct_chat(actual_job_id, prompt, llm_override)
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
    }
