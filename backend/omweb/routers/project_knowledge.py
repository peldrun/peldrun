# backend/omweb/routers/project_knowledge.py

"""
PELDRUN Project Knowledge Base Router.

Upload / list / delete reference files for a project's knowledge folder,
stored at ``<workspace>/.peldrun/knowledge/``.

The knowledge folder is a *convention*, not a separate storage layer:
files live inside the project workspace, so they follow ``workspace_path``
for external workspaces and ``storage/projects/<id>/workspace`` for
internal ones. This means:

  * When the user changes ``workspace_path``, existing files stay at the
    old location (the folder belongs to the path, not to the project).
  * The agent reads them via its normal file tools
    (``file_ops`` / ``str_replace_editor`` / ``bash``) — no extra
    plumbing is needed to make this work.
  * The engine's artifact scanner (``_scan_workspace_files``) skips any
    path segment that starts with a dot, so knowledge files are never
    reported as agent-produced deliverables.

Design notes
------------

* This module intentionally imports ``_get_project_workspace_path`` from
  ``omweb.routers.projects`` instead of re-implementing workspace
  resolution. The engineering plan forbids adding a fourth resolver:
  the router, the chat dispatcher, and the engine all agree on one path,
  and any new resolver would be a fifth source of truth. Importing the
  helper from its current home is safe because ``projects.py`` does not
  import this module (no cycle).

* File uploads are read fully into memory (``await upload.read()``). The
  per-file cap (20 MB) matches the existing ``workspace/upload`` endpoint
  pattern; if streaming is ever needed for larger files, both endpoints
  should change together so the two paths keep the same limits.

* Uploads are written to a hidden temp file (``.<name>.part``) and
  atomically renamed into place. The listing helper ignores dot-files,
  so a crash mid-write never exposes a partial file to the agent.

* ``_safe_name`` strips the directory part and any leading dots so an
  upload can never escape the knowledge folder or masquerade as a hidden
  file. Non-ASCII characters (Arabic included) are kept on purpose —
  ``str.isalnum()`` returns True for them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from omweb.projects import resolve_safe_path
from omweb.projects.project_prompting import (
    KNOWLEDGE_DIR_REL,
    knowledge_dir,
    list_knowledge_files,
)
# Reuse the existing resolver. Do NOT write a fourth workspace resolver.
from omweb.routers.projects import _get_project_workspace_path

router = APIRouter(
    prefix='/api/projects/{project_id}/knowledge',
    tags=['project-knowledge'],
)


# ---------------------------------------------------------------------------
# Limits
#
# These mirror the pattern used by the existing workspace/upload endpoint.
# They are intentionally code-level constants (not settings): a user who
# needs a larger file can drop it directly into
# <workspace>/.peldrun/knowledge/ and the agent will still read it.
# Exposing them via the API would require a shared /api/config/limits
# endpoint so the frontend has one source of truth, which is out of scope.
# ---------------------------------------------------------------------------
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB per file
MAX_FILES = 200                    # 200 files per project


def _safe_name(raw: str | None) -> str:
    """Normalise a user-supplied filename for storage in the knowledge folder.

    Rules:
      * Drop any directory component (``../../etc/passwd`` -> ``passwd``).
      * Keep letters (Unicode included), digits, and ``. _ - ( ) space``.
      * Replace every other character with ``_``.
      * Strip leading/trailing dots and spaces so the result cannot be
        a hidden file or an empty name.
      * Cap at 150 characters to avoid filesystem limits.

    Returns ``""`` when the input does not yield a usable name.
    """
    name = Path(raw or '').name
    cleaned = ''.join(
        c if (c.isalnum() or c in '._- ()') else '_' for c in name
    )
    return cleaned.strip(' .')[:150]


@router.get('/files')
async def list_files(project_id: str) -> Dict[str, Any]:
    """List every visible file under the knowledge folder.

    Response shape::

        {
            "directory": ".peldrun/knowledge",
            "files": [{"name": str, "path": str, "size": int}, ...],
            "total": int
        }

    An empty or missing folder is not an error: the caller gets an empty
    list, which the UI renders as "no files yet".
    """
    workspace = _get_project_workspace_path(project_id)
    files = list_knowledge_files(workspace)
    return {
        'directory': KNOWLEDGE_DIR_REL,
        'files': files,
        'total': len(files),
    }


@router.post('/files')
async def upload_files(
    project_id: str,
    files: List[UploadFile] = File(...),
    overwrite: bool = Query(False),
) -> Dict[str, Any]:
    """Upload one or more files into the knowledge folder.

    Duplicates are skipped unless ``overwrite=true``. Oversized files,
    invalid names, and the per-project file limit are reported in the
    ``skipped`` list rather than raising, so a batch upload of mixed
    files still succeeds for the valid entries.
    """
    workspace = _get_project_workspace_path(project_id)
    target_dir = knowledge_dir(workspace)
    target_dir.mkdir(parents=True, exist_ok=True)

    count = len(list_knowledge_files(workspace))
    saved: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for upload in files:
        name = _safe_name(upload.filename)
        if not name:
            skipped.append({
                'name': upload.filename or '',
                'reason': 'invalid_name',
            })
            continue
        target = target_dir / name
        exists = target.exists()
        if exists and not overwrite:
            skipped.append({'name': name, 'reason': 'exists'})
            continue
        if not exists and count >= MAX_FILES:
            skipped.append({'name': name, 'reason': 'limit_reached'})
            continue

        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            skipped.append({'name': name, 'reason': 'too_large'})
            continue

        # Hidden temp file + atomic replace: a crash never leaves a
        # half-written file. ``list_knowledge_files`` ignores dot-files,
        # so the temp file is invisible to the agent during the write.
        tmp = target_dir / ('.' + name + '.part')
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                # Best-effort cleanup; the original error is more useful.
                pass
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f'Failed to save {name}: {exc}',
            ) from exc
        if not exists:
            count += 1
        saved.append({
            'name': name,
            'path': f'{KNOWLEDGE_DIR_REL}/{name}',
            'size': len(data),
        })

    return {
        'status': 'success',
        'files': saved,
        'skipped': skipped,
        'total_count': len(saved),
    }


@router.delete('/files')
async def delete_file(
    project_id: str,
    name: str = Query(..., description='File name relative to the knowledge folder'),
) -> Dict[str, Any]:
    """Delete a single file from the knowledge folder.

    ``name`` may include a relative subpath (``docs/spec.md``) so files
    that the user created manually under a sub-folder can also be removed
    from the UI. Path traversal and symlinks that escape the folder are
    rejected by ``resolve_safe_path`` with HTTP 403.
    """
    workspace = _get_project_workspace_path(project_id)
    root = knowledge_dir(workspace)
    if not root.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'File not found: {name}',
        )
    # Raises 403 on path traversal or a symlink that escapes the folder.
    target = resolve_safe_path(str(root), name)
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'File not found: {name}',
        )
    try:
        target.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete {name}: {exc}',
        ) from exc
    return {'status': 'deleted', 'name': name}
