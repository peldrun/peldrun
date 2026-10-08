"""
backend/omweb/routers/chats.py

Chats & Project Router - Integrated with ChatStorageEngine for disk governance.
All static routes (/projects, /storage, /all) are strictly declared before /{chat_id}
to avoid FastAPI path parameter shadowing and 404 collisions.
Enhanced with resilient session log discovery and synthesized replay.
"""

import os
import mimetypes
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from omweb.project_manager import project_manager
from omweb.chat_storage_engine import chat_storage_engine

router = APIRouter()


class ChatCreateRequest(BaseModel):
    project_id: Optional[str] = "default_project"
    title: Optional[str] = "New Session"
    job_id: Optional[str] = ""
    prompt: Optional[str] = ""
    agent_id: Optional[str] = "peldrun"


class ChatRenameRequest(BaseModel):
    title: str = Field(..., min_length=1)


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = ""


# ============================================================================
# 1. STATIC ROUTES (MUST BE DECLARED BEFORE ANY /{chat_id} PARAMETER ROUTE)
# ============================================================================

# Projects Endpoints
@router.get("/projects")
async def list_projects():
    """List all projects from storage index."""
    return {"projects": project_manager.list_projects()}


@router.post("/projects")
async def create_project(req: ProjectCreateRequest):
    proj = project_manager.create_project(name=req.name, description=req.description)
    return {"status": "ok", "project": proj}


@router.get("/projects/{project_id}")
async def get_project_detail(project_id: str):
    proj = project_manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    chats = project_manager.list_chats(project_id=project_id)
    return {"status": "ok", "project": proj, "chats": chats}


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    project_manager.delete_project(project_id)
    return {"status": "ok", "message": f"Project {project_id} deleted"}


# Storage Metrics & Orphan Sweep Endpoints
@router.get("/storage/stats")
async def get_storage_stats():
    return {"status": "ok", "stats": chat_storage_engine.get_storage_metrics()}


@router.post("/storage/sweep")
async def sweep_orphans():
    report = chat_storage_engine.sweep_orphaned_storage()
    return {"status": "ok", "report": report}


# Bulk Delete & Purge All Endpoint
@router.delete("/all")
async def delete_all_chats():
    report = chat_storage_engine.delete_all_chats()
    return {"status": "ok", "report": report}


# ============================================================================
# 2. ROOT CHAT LIST & CREATION ENDPOINTS
# ============================================================================

@router.get("")
@router.get("/")
async def list_chats(
    project_id: Optional[str] = None,
    include_archived: bool = Query(False),
    pinned_only: bool = Query(False),
    search: Optional[str] = Query(None)
):
    chats = chat_storage_engine.list_chats(
        project_id=project_id,
        include_archived=include_archived,
        pinned_only=pinned_only,
        search=search
    )
    return {"chats": chats}


@router.post("")
@router.post("/")
async def create_chat(req: ChatCreateRequest):
    import uuid
    chat_id = f"chat_{uuid.uuid4().hex[:10]}"
    chat = project_manager.save_chat_session(
        chat_id=chat_id,
        project_id=req.project_id or "default_project",
        title=req.title,
        job_id=req.job_id or "",
        prompt=req.prompt or "",
        events=[],
        result="",
        status="active",
        agent_id=req.agent_id or "peldrun"
    )
    return {"status": "ok", "chat": chat}


# ============================================================================
# 3. CHAT ACTIONS & SUB-RESOURCE ENDPOINTS
# ============================================================================

@router.patch("/{chat_id}/rename")
@router.put("/{chat_id}/title")
async def rename_chat(chat_id: str, req: ChatRenameRequest):
    updated = chat_storage_engine.rename_chat(chat_id, req.title)
    if not updated:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {"status": "ok", "chat": updated}


@router.post("/{chat_id}/pin")
async def toggle_pin_chat(chat_id: str):
    updated = chat_storage_engine.toggle_pin(chat_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {"status": "ok", "chat": updated}


@router.post("/{chat_id}/archive")
async def toggle_archive_chat(chat_id: str):
    updated = chat_storage_engine.toggle_archive(chat_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {"status": "ok", "chat": updated}


@router.get("/{chat_id}/files")
async def get_chat_files(chat_id: str):
    chat = project_manager.get_chat(chat_id)
    project_id = chat.get("project_id", "default_project") if chat else "default_project"
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    if not files_dir.exists():
        return {"chat_id": chat_id, "files": []}

    chat_files = []
    for root, dirs, files in os.walk(files_dir):
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(files_dir)
            rel_str = str(rel).replace("\\", "/")
            stat = p.stat()
            chat_files.append({
                "name": f,
                "path": rel_str,
                "size": stat.st_size,
                "modified": int(stat.st_mtime)
            })
    return {"chat_id": chat_id, "files": chat_files}


@router.get("/{chat_id}/raw/{filepath:path}")
async def get_chat_raw_file(chat_id: str, filepath: str):
    chat = project_manager.get_chat(chat_id)
    project_id = chat.get("project_id", "default_project") if chat else "default_project"
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    clean_rel = filepath.lstrip("/\\")
    target = (files_dir / clean_rel).resolve()
    if not target.is_relative_to(files_dir) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in chat deliverables")

    content_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(target, media_type=content_type or "application/octet-stream")


@router.get("/{chat_id}/log")
async def get_chat_log(chat_id: str):
    """Retrieve execution log file or synthesize from session records."""
    try:
        chat = project_manager.get_chat(chat_id)
        project_id = chat.get("project_id", "default_project") if chat else "default_project"
        chat_dir = project_manager.get_chat_dir(chat_id, project_id)
        
        candidate_paths = [
            chat_dir / "chat.log",
            chat_storage_engine.chats_dir / chat_id / "chat.log",
        ]
        if chat and chat.get("id"):
            candidate_paths.append(chat_storage_engine.chats_dir / str(chat["id"]) / "chat.log")
            candidate_paths.append(project_manager.get_chat_dir(str(chat["id"]), project_id) / "chat.log")
        if chat and chat.get("job_id"):
            candidate_paths.append(chat_storage_engine.chats_dir / str(chat["job_id"]) / "chat.log")

        for p in candidate_paths:
            if p.exists() and p.is_file():
                return {"chat_id": chat_id, "log": p.read_text(encoding="utf-8", errors="replace")}

        # Fallback: Synthesize log lines from session records if log file not yet flushed
        if chat:
            synthesized_lines: List[str] = []
            turns = chat.get("turns", [])
            for t in turns:
                t_ts = t.get("timestamp") or ""
                p_text = t.get("prompt") or t.get("user") or ""
                if p_text:
                    synthesized_lines.append(f"[{t_ts}] [SYSTEM] User Prompt: {p_text}")
                for ev in (t.get("steps") or t.get("events") or []):
                    ev_type = str(ev.get("type", "step")).upper()
                    ev_content = ev.get("content") or ev.get("thought") or ev.get("output") or ev.get("data") or ""
                    if ev_content:
                        synthesized_lines.append(f"[{ev.get('timestamp') or t_ts}] [{ev_type}] {ev_content}")
                res = t.get("result") or t.get("assistant") or ""
                if res:
                    synthesized_lines.append(f"[{t_ts}] [FINAL] {res}")

            if synthesized_lines:
                return {"chat_id": chat_id, "log": "\n".join(synthesized_lines)}

        return {"chat_id": chat_id, "log": ""}
    except Exception as e:
        return {"chat_id": chat_id, "log": f"[Error reading session log: {str(e)}]"}


@router.get("/{chat_id}/log/download")
async def download_chat_log(chat_id: str):
    """Download session log file directly as plain text."""
    chat = project_manager.get_chat(chat_id)
    project_id = chat.get("project_id", "default_project") if chat else "default_project"
    chat_dir = project_manager.get_chat_dir(chat_id, project_id)

    candidate_paths = [
        chat_dir / "chat.log",
        chat_storage_engine.chats_dir / chat_id / "chat.log",
    ]
    if chat and chat.get("id"):
        candidate_paths.append(chat_storage_engine.chats_dir / str(chat["id"]) / "chat.log")
        candidate_paths.append(project_manager.get_chat_dir(str(chat["id"]), project_id) / "chat.log")

    for log_file in candidate_paths:
        if log_file.exists() and log_file.is_file():
            return FileResponse(
                path=log_file,
                filename=f"chat_{chat_id}.log",
                media_type="text/plain; charset=utf-8"
            )

    # Fallback to synthesized log download
    log_detail = await get_chat_log(chat_id)
    log_text = log_detail.get("log", "")
    if log_text:
        return Response(
            content=log_text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=chat_{chat_id}.log"}
        )

    return PlainTextResponse(f"No log file found for chat {chat_id}", status_code=404)


# ============================================================================
# 4. SINGLE CHAT PARAMETERIZED ENDPOINTS (DECLARED LAST)
# ============================================================================

@router.get("/{chat_id}")
async def get_chat_detail(chat_id: str):
    chat = project_manager.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {"status": "ok", "chat": chat}


@router.delete("/{chat_id}")
async def delete_single_chat(chat_id: str):
    res = chat_storage_engine.deep_delete_chat(chat_id)
    return {"status": "ok", "report": res}