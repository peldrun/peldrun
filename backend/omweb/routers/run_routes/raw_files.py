"""
Raw file delivery endpoints.

Provides direct access to workspace files as HTTP resources and a bulk
download endpoint that returns the whole workspace as a ZIP archive:

* ``GET /jobs/{job_id}/raw/{filepath:path}`` - serve a single file with a
  MIME type inferred from its extension.
* ``GET /jobs/{job_id}/download-zip``         - stream a ZIP archive.
"""

from __future__ import annotations

import mimetypes
import os
from io import BytesIO
from pathlib import Path
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from omweb.project_manager import project_manager

from .utils import resolve_chat, resolve_physical_file


router = APIRouter()


# Explicit content-type overrides for extensions where the platform's
# mimetypes database is unreliable or missing entries.
_EXPLICIT_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
}


@router.get("/jobs/{job_id}/raw/{filepath:path}")
async def get_job_raw_file(job_id: str, filepath: str):
    """Serve a single workspace file with an appropriate content type."""
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    target = resolve_physical_file(files_dir, filepath)
    if not target or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in job deliverables")

    # Determine the media type, preferring the explicit map over guess_type.
    content_type, _ = mimetypes.guess_type(str(target))
    ext = target.suffix.lower()
    if ext in _EXPLICIT_CONTENT_TYPES:
        content_type = _EXPLICIT_CONTENT_TYPES[ext]

    return FileResponse(target, media_type=content_type or "application/octet-stream")


@router.get("/jobs/{job_id}/download-zip")
async def download_job_zip(job_id: str):
    """Return the entire chat workspace as a ZIP archive."""
    _, chat_id = resolve_chat(job_id)
    chat = project_manager.get_chat(chat_id) or {}
    project_id = chat.get("project_id", "default_project")
    files_dir = project_manager.get_chat_files_dir(chat_id, project_id)

    if not files_dir.exists():
        raise HTTPException(status_code=404, detail="No files found for this job")

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, _dirs, files in os.walk(files_dir):
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
