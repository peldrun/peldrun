"""
backend/omweb/routers/files.py

Workspace Files Router.
Provides file tree scanning, download, deletion, and direct content editing persistence.
Hardened under Phase M2 & Stabilization:
- Extended multi-tier lookup (root, workspace, storage/chats) to prevent 404 errors on deliverables.
"""

import os
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Query
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