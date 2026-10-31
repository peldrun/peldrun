# Path: backend/omweb/routers/files.py
"""
Workspace Files Router.
Provides file tree scanning, upload, download, deletion, and direct content editing persistence.
Hardened under Phase M2 & Release Bug Fixes:
- Added canonical multi-file upload endpoint supporting dual-tier persistence (workspace & chat session).
- Extended multi-tier lookup (root, workspace, storage/chats) to prevent 404 errors on deliverables.
"""

import base64
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
STORAGE_DIR = PROJECT_ROOT / "storage"
CHATS_DIR = STORAGE_DIR / "chats"
PROJECTS_DIR = STORAGE_DIR / "projects"

for d in [WORKSPACE_DIR, STORAGE_DIR, CHATS_DIR, PROJECTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

SYSTEM_FILES = {"events.json", "session.json", "project.json", ".gitkeep", ".ds_store", "thumbs.db"}

# ---------------------------------------------------------------------------
# Temporary artifact store (multimodal image uploads)
# ---------------------------------------------------------------------------
# User-pasted screenshots and tool-captured screenshots are stored here as
# opaque ``<file_id>`` references and embedded into provider payloads
# on-demand. This avoids the ~33% size penalty of inlining base64 in the
# run request, and keeps the JSON session files small enough to remain
# human-inspectable.
TEMP_ARTIFACTS_DIR = STORAGE_DIR / "artifacts" / "temp"
TEMP_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Only image formats that both major vision providers accept are allowed.
# SVG is excluded because it can carry script payloads and is rejected
# as a vision input by OpenAI/Anthropic.
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# Extension <-> MIME mappings used to normalize uploads and to serve
# temporary artifacts back over HTTP with the correct Content-Type.
_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_EXT_TO_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _probe_image_dimensions(
    path: Path,
) -> Tuple[Optional[int], Optional[int]]:
    """
    Best-effort image dimension probe using Pillow.

    Returns ``(width, height)`` when Pillow is available and the file
    parses; ``(None, None)`` otherwise. The upload endpoint surfaces
    these dimensions so the frontend can render accurate previews
    without a second round-trip, and so downstream consumers can pick
    the correct ``detail`` hint for provider payloads.
    """
    try:
        from PIL import Image  # noqa: PLC0415 — optional dependency

        with Image.open(path) as img:
            width, height = img.size
            return int(width), int(height)
    except Exception:
        return None, None


def _resolve_temp_artifact_path(file_id: str) -> Optional[Path]:
    """
    Resolve a ``file_id`` to a physical path inside the temp store.

    Returns ``None`` when the identifier is malformed (contains path
    separators or traversal) or the file does not exist. The returned
    path is guaranteed to be inside ``TEMP_ARTIFACTS_DIR``.
    """
    if not file_id:
        return None
    # Reject any path-like input. file_ids are opaque tokens of the shape
    # ``img_<hex>.<ext>``; anything else is a caller bug.
    if "/" in file_id or "\\" in file_id or ".." in file_id:
        return None
    candidate = (TEMP_ARTIFACTS_DIR / file_id).resolve()
    try:
        candidate.relative_to(TEMP_ARTIFACTS_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def resolve_temp_artifact_to_data_uri(file_id: str) -> Optional[str]:
    """
    Convert a temporary artifact ``file_id`` into a ``data:`` URI
    suitable for the ``image_url.url`` field of a multimodal provider
    payload.

    Returns ``None`` when the artifact does not exist or cannot be read.
    This function performs NO downscaling — the caller (typically the
    ``VisionContextOptimizer`` in ``peldrun.context.builder``) is
    responsible for resizing before embedding. This separation keeps
    the router free of an eager Pillow dependency and lets the context
    layer decide the appropriate maximum dimension for the target
    provider.

    The returned URI is a plain ``data:<mime>;base64,<payload>`` string
    ready to be passed to ``ImageUrlPayload(url=...)``.
    """
    path = _resolve_temp_artifact_path(file_id)
    if path is None:
        return None
    ext = path.suffix.lower().lstrip(".")
    mime = _EXT_TO_MIME.get(ext)
    if mime is None:
        return None
    try:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return None


class FileContentPayload(BaseModel):
    path: str
    content: str


def safe_resolve(target_path: str) -> Path:
    target = (PROJECT_ROOT / target_path).resolve()
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside project root")
    return target


def find_file_in_storage(filename_or_rel: str) -> Optional[Path]:
    """Search for deliverable file in workspace or storage hierarchy if not found in root."""
    clean = filename_or_rel.lstrip("/\\").replace("\\", "/")

    # 1. Direct workspace check
    candidate = (WORKSPACE_DIR / clean).resolve()
    if candidate.is_file():
        return candidate

    # 2. Direct storage path check
    candidate = (STORAGE_DIR / clean).resolve()
    if candidate.is_file():
        return candidate

    # 3. Recursive lookup inside chats files directories
    target_name = Path(clean).name
    if CHATS_DIR.exists():
        for chat_dir in CHATS_DIR.iterdir():
            if chat_dir.is_dir():
                files_sub = chat_dir / "files"
                if files_sub.exists():
                    target_match = (files_sub / clean).resolve()
                    if target_match.is_file():
                        return target_match
                    for sub_file in files_sub.rglob(target_name):
                        if sub_file.is_file() and sub_file.stat().st_size > 0:
                            return sub_file

    return None


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in [".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico"]:
        return "image"
    if ext in [".html", ".htm"]:
        return "html"
    if ext in [".md", ".markdown"]:
        return "markdown"
    if ext in [".json", ".csv", ".tsv"]:
        return "data"
    if ext in [".pdf"]:
        return "pdf"
    if ext in [".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".java", ".cpp", ".c", ".rs", ".go", ".sql", ".sh", ".bat", ".ps1"]:
        return "code"
    return "file"


def is_valid_artifact_path(entry: Path, origin: str) -> bool:
    fname = entry.name.lower()
    if fname in SYSTEM_FILES:
        return False
    parts = [p.lower() for p in entry.parts]
    if origin == "workspace":
        return True
    if origin in ["chats", "projects"]:
        return "files" in parts or "shared_files" in parts
    return False


@router.get("")
@router.get("/")
async def list_workspace_files():
    artifacts = []
    dirs_to_scan = [
        ("workspace", WORKSPACE_DIR),
        ("chats", CHATS_DIR),
        ("projects", PROJECTS_DIR)
    ]

    for origin, base_path in dirs_to_scan:
        if base_path.exists():
            for entry in base_path.rglob("*"):
                if entry.is_file() and is_valid_artifact_path(entry, origin):
                    try:
                        rel_root = str(entry.relative_to(PROJECT_ROOT)).replace("\\", "/")
                        stat = entry.stat()
                        artifacts.append({
                            "name": entry.name,
                            "path": rel_root,
                            "rel_root": rel_root,
                            "origin": origin,
                            "is_dir": False,
                            "size": stat.st_size,
                            "modified": int(stat.st_mtime),
                            "type": detect_file_type(entry.name),
                        })
                    except Exception:
                        continue

    sorted_artifacts = sorted(artifacts, key=lambda x: x["modified"], reverse=True)
    return {
        "workspace": str(WORKSPACE_DIR),
        "storage": str(STORAGE_DIR),
        "files": sorted_artifacts,
        "total_count": len(sorted_artifacts)
    }


# [FIX-5.2] When True, files are also mirrored to WORKSPACE_DIR for
# backward compatibility with tooling that still references it.
# Set to False once the transition to per-chat storage is complete.
MIRROR_UPLOADS_TO_WORKSPACE = False


@router.post("/upload")
async def upload_workspace_files(
    files: List[UploadFile] = File(...),
    chat_id: Optional[str] = Form(None),
    job_id: Optional[str] = Form(None),
):
    """
    Upload and persist user attachments into the agent's per-chat workspace.

    [FIX-5.2] Single source of truth:
        Files are written ONLY into `storage/chats/<chat_id>/files/` (or the
        matching project-scoped path when chat_id belongs to a project).
        This eliminates state divergence between a mirror copy in
        `workspace/` and the canonical copy consumed by the agent.

    Backward compatibility:
        While MIRROR_UPLOADS_TO_WORKSPACE is True, a copy is additionally
        written to `workspace/`. Once all legacy consumers have migrated,
        flip the flag to False to make the migration complete.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided for upload")

    # Per-chat storage is the canonical target. Fall back to legacy workspace
    # only when the client did not provide a chat_id (e.g. ad-hoc uploads).
    if chat_id:
        primary_dir = CHATS_DIR / chat_id / "files"
    else:
        primary_dir = WORKSPACE_DIR

    primary_dir.mkdir(parents=True, exist_ok=True)

    uploaded_records: List[Dict[str, Any]] = []

    for file in files:
        raw_name = file.filename or "uploaded_attachment"
        clean_filename = Path(raw_name).name

        if not clean_filename:
            continue

        try:
            content = await file.read()

            # 1. Canonical write (single source of truth).
            primary_target = primary_dir / clean_filename
            primary_target.write_bytes(content)

            # 2. Optional mirror for legacy consumers during transition.
            if (
                MIRROR_UPLOADS_TO_WORKSPACE
                and chat_id
                and primary_dir != WORKSPACE_DIR
            ):
                mirror_target = WORKSPACE_DIR / clean_filename
                mirror_target.write_bytes(content)

            rel_primary_path = str(
                primary_target.relative_to(PROJECT_ROOT)
            ).replace("\\", "/")

            uploaded_records.append({
                "name": clean_filename,
                "path": rel_primary_path,
                "size": len(content),
                "type": detect_file_type(clean_filename),
            })
        except Exception as upload_err:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save uploaded file '{clean_filename}': {upload_err}"
            )

    return {
        "status": "success",
        "message": f"Successfully uploaded {len(uploaded_records)} file(s)",
        "files": uploaded_records,
        "total_count": len(uploaded_records),
    }


@router.post("/upload-temp")
async def upload_temp_image(file: UploadFile = File(...)):
    """
    Upload a single image into the temporary artifact store.

    Returns a lightweight reference describing the stored file so the
    frontend composer can include it in the subsequent ``start_run``
    payload as an opaque ``file_id``. The image bytes never travel
    through the run request, which keeps the request payload under
    a few KB regardless of image size.

    Response shape::

        {
            "file_id":     "img_ab12cd34ef56.png",
            "filename":    "Screenshot 2026-10-31.png",
            "mime_type":   "image/png",
            "size_bytes":  245891,
            "width":       1280,   # None if Pillow is unavailable
            "height":      720,    # None if Pillow is unavailable
            "url":         "/api/files/temp/img_ab12cd34ef56.png"
        }
    """
    # Validate MIME type. When the caller's Content-Type is missing or
    # generic, fall back to the extension before rejecting.
    declared_mime = (file.content_type or "").strip().lower()
    if declared_mime == "image/jpg":
        # Some browsers and legacy clients emit this non-standard value.
        declared_mime = "image/jpeg"

    original_name = file.filename or "uploaded_image"
    ext_from_name = (
        original_name.rsplit(".", 1)[-1].lower()
        if "." in original_name
        else ""
    )
    if ext_from_name == "jpeg":
        ext_from_name = "jpg"
    inferred_mime = _EXT_TO_MIME.get(ext_from_name)
    effective_mime = (
        declared_mime
        if declared_mime in ALLOWED_IMAGE_TYPES
        else inferred_mime
    )

    if effective_mime not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid image format. Allowed types: "
                + ", ".join(sorted(ALLOWED_IMAGE_TYPES))
            ),
        )

    file_id = f"img_{uuid.uuid4().hex[:12]}.{_MIME_TO_EXT[effective_mime]}"
    target_path = TEMP_ARTIFACTS_DIR / file_id

    try:
        # Stream chunks to disk to avoid loading the entire payload in RAM.
        with open(target_path, "wb") as out_file:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                out_file.write(chunk)
    except Exception as exc:
        # Best-effort cleanup on partial write.
        try:
            target_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist uploaded image: {exc}",
        )

    try:
        size_bytes = target_path.stat().st_size
    except Exception:
        size_bytes = 0

    width, height = _probe_image_dimensions(target_path)

    return {
        "file_id": file_id,
        "filename": original_name,
        "mime_type": effective_mime,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "url": f"/api/files/temp/{file_id}",
    }


@router.get("/temp/{file_id}")
async def get_temp_artifact(file_id: str):
    """
    Serve a temporary artifact image back to the browser.

    Used by the composer to render inline previews during the brief
    window between upload and the run request (e.g. after a page
    reload that lost the ``URL.createObjectURL`` handle). The returned
    content is served with the correct ``Content-Type`` so the browser
    can render it as an ``<img>`` element directly.
    """
    path = _resolve_temp_artifact_path(file_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    ext = path.suffix.lower().lstrip(".")
    media_type = _EXT_TO_MIME.get(ext, "application/octet-stream")
    return FileResponse(path=str(path), media_type=media_type)


@router.delete("/temp/{file_id}")
async def delete_temp_artifact(file_id: str):
    """
    Delete a temporary artifact.

    Called by the frontend when the user removes an image from the
    composer before submission, or when an upload is aborted. Idempotent:
    deleting a non-existent artifact returns ``deleted=False`` rather
    than raising.
    """
    path = _resolve_temp_artifact_path(file_id)
    if path is None:
        return {"status": "not_found", "file_id": file_id, "deleted": False}
    try:
        path.unlink()
        return {"status": "success", "file_id": file_id, "deleted": True}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete artifact {file_id}: {exc}",
        )


@router.get("/download")
async def download_file(path: str = Query(...)):
    target = safe_resolve(path)
    if not target.exists() or not target.is_file():
        target = find_file_in_storage(path)
        if not target or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(target), filename=target.name, media_type="application/octet-stream")


@router.get("/content/{filepath:path}")
@router.get("/content")
async def get_file_content(filepath: Optional[str] = None, path: Optional[str] = Query(None)):
    target_rel = filepath or path
    if not target_rel:
        raise HTTPException(status_code=400, detail="Missing file path")
    target = safe_resolve(target_rel)
    if not target.exists() or not target.is_file():
        fallback = find_file_in_storage(target_rel)
        if fallback and fallback.is_file():
            target = fallback
        else:
            raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "size": target.stat().st_size, "name": target.name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/content")
async def save_file_content(payload: FileContentPayload):
    """Save or update file content in the project workspace."""
    target = safe_resolve(payload.path)
    if not target.parent.exists():
        fallback = find_file_in_storage(payload.path)
        if fallback:
            target = fallback
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(payload.content, encoding="utf-8")
        rel_path = str(target.relative_to(PROJECT_ROOT)).replace("\\", "/")
        return {
            "status": "success",
            "message": f"Saved {target.name}",
            "path": rel_path,
            "size": target.stat().st_size,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")


@router.get("/raw/{filepath:path}")
@router.get("/raw")
async def get_raw_file(filepath: Optional[str] = None, path: Optional[str] = Query(None)):
    target_rel = filepath or path
    if not target_rel:
        raise HTTPException(status_code=400, detail="Missing file path")
    target = safe_resolve(target_rel)
    if not target.exists() or not target.is_file():
        fallback = find_file_in_storage(target_rel)
        if fallback and fallback.is_file():
            target = fallback
        else:
            raise HTTPException(status_code=404, detail="File not found")

    ext = target.suffix.lower()
    media_type = "text/plain"
    if ext in [".png"]:
        media_type = "image/png"
    elif ext in [".jpg", ".jpeg"]:
        media_type = "image/jpeg"
    elif ext in [".svg"]:
        media_type = "image/svg+xml"
    elif ext in [".webp"]:
        media_type = "image/webp"
    elif ext in [".gif"]:
        media_type = "image/gif"
    elif ext in [".html", ".htm"]:
        media_type = "text/html"
    elif ext in [".css"]:
        media_type = "text/css"
    elif ext in [".js"]:
        media_type = "application/javascript"
    elif ext in [".json"]:
        media_type = "application/json"
    elif ext in [".pdf"]:
        media_type = "application/pdf"

    return FileResponse(path=str(target), media_type=media_type)


@router.delete("")
@router.delete("/")
async def delete_file(path: str = Query(...)):
    target = safe_resolve(path)
    if not target.exists():
        fallback = find_file_in_storage(path)
        if fallback:
            target = fallback
        else:
            raise HTTPException(status_code=404, detail="File or folder not found")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"status": "success", "message": f"Deleted {target.name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
