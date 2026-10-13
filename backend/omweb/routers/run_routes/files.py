"""
Workspace file endpoints.

Exposes listing and reading/writing operations for the per-chat workspace:

* ``GET  /jobs/{job_id}/files``   - list every file in the workspace, plus
  the delta of files created/modified during the current job.
* ``GET  /jobs/{job_id}/content`` - read a single text file.
* ``POST /jobs/{job_id}/content`` - persist edited text back to a file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List
import urllib.parse

from fastapi import APIRouter, HTTPException, Query

from omweb.agent_bridge import job_scoped_artifacts
from omweb.project_manager import project_manager

from .schemas import FileContentPayload
from .state import JOB_DELTA_ARTIFACTS
from .utils import compute_job_delta_files, resolve_chat, resolve_physical_file


router = APIRouter()


@router.get("/jobs/{job_id}/files")
async def get_job_files(job_id: str):
    """
    List every file in the chat workspace plus the job-specific delta.

    The response contains three equivalent views:

    * ``files`` / ``all_files``: the complete listing.
    * ``delta_files``: files created or modified during the current job.
    """
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    if not files_dir.exists():
        return {
            "job_id": job_id,
            "chat_id": chat_id,
            "files": [],
            "all_files": [],
            "delta_files": [],
        }

    all_files_list: List[Dict[str, Any]] = []
    for root, _dirs, files in os.walk(files_dir):
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(files_dir)
            all_files_list.append({
                "name": f,
                "path": str(rel).replace("\\", "/"),
                "size": p.stat().st_size,
            })

    # Non-empty files first, then alphabetical for stable ordering.
    all_files_list.sort(key=lambda x: (0 if x["size"] > 0 else 1, x["path"]))

    actual_job_id = chat.get("job_id") or job_id
    delta_files = compute_job_delta_files(actual_job_id, files_dir)
    JOB_DELTA_ARTIFACTS[actual_job_id] = delta_files

    return {
        "job_id": actual_job_id,
        "chat_id": chat_id,
        "files": all_files_list,
        "all_files": all_files_list,
        "delta_files": delta_files,
    }


@router.get("/jobs/{job_id}/content")
async def get_job_file_content(job_id: str, path: str = Query(...)):
    """Return the UTF-8 text content of a single workspace file."""
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    target = resolve_physical_file(files_dir, path)
    if not target or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in job deliverables")

    try:
        content = target.read_text(encoding="utf-8")
        rel_str = str(target.relative_to(files_dir)).replace("\\", "/")
        return {"path": rel_str, "content": content}
    except Exception as e:
        # Binary or otherwise unreadable files are reported as opaque blobs.
        return {"path": path, "content": f"Binary content: {str(e)}"}


@router.post("/jobs/{job_id}/content")
async def save_job_file_content(job_id: str, payload: FileContentPayload):
    """
    Persist edited text content back to a workspace file.

    The path is validated against the workspace root to prevent traversal
    outside of ``files_dir``.
    """
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)
    files_dir.mkdir(parents=True, exist_ok=True)

    decoded = urllib.parse.unquote(payload.path)
    clean_rel = decoded.lstrip("/\\").replace("\\", "/")
    if "files/" in clean_rel:
        clean_rel = clean_rel.split("files/", 1)[1]

    target = (files_dir / clean_rel).resolve()
    try:
        target.relative_to(files_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Access denied: path outside workspace files directory",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload.content, encoding="utf-8")

    # Record the artifact against the job and the chat so downstream lookups
    # (e.g. produced_files) reflect this manual edit.
    actual_job_id = chat.get("job_id") or job_id
    if actual_job_id not in job_scoped_artifacts:
        job_scoped_artifacts[actual_job_id] = []
    if clean_rel not in job_scoped_artifacts[actual_job_id]:
        job_scoped_artifacts[actual_job_id].append(clean_rel)

    if chat_id:
        if chat_id not in job_scoped_artifacts:
            job_scoped_artifacts[chat_id] = []
        if clean_rel not in job_scoped_artifacts[chat_id]:
            job_scoped_artifacts[chat_id].append(clean_rel)

    return {
        "success": True,
        "path": clean_rel,
        "size": target.stat().st_size,
        "message": f"Saved {clean_rel} successfully",
    }
