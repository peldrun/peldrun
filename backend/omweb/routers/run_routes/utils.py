"""
Shared helper utilities for the run router submodules.

These helpers were extracted from the original monolithic router to keep the
endpoint modules focused on request handling. They cover three concerns:

* Resolving a chat record from an ambiguous identifier (chat id, job id, or
  a fallback value).
* Computing the list of files created or modified during a specific job,
  based on a pre-execution snapshot.
* Resolving a physical file on disk from a user-supplied relative path,
  with a safe fallback for bare filenames.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

from omweb.job_manager import job_manager
from omweb.project_manager import project_manager

from .state import JOB_PRE_SNAPSHOTS


def resolve_chat(identifier: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Resolve a chat record and its canonical id from a loose identifier.

    The identifier may be:
        * a chat id (direct hit),
        * a job id that was previously linked to a chat,
        * a job id stored in the chat metadata or in one of its turns,
        * an arbitrary string that will be coerced into a ``chat_*`` id.

    Returns:
        A tuple ``(chat_or_none, resolved_chat_id)``. The chat dictionary is
        ``None`` when no stored chat could be located.
    """
    if not identifier:
        return None, ""

    direct_chat = project_manager.get_chat(identifier)
    if direct_chat:
        return direct_chat, direct_chat.get("id", identifier)

    # Local in-memory mapping populated during job creation.
    from .state import JOB_TO_CHAT_ID

    if identifier in JOB_TO_CHAT_ID:
        cid = JOB_TO_CHAT_ID[identifier]
        return project_manager.get_chat(cid), cid

    # Fallback: the job object may carry a chat_id attribute.
    job = job_manager.get_job(identifier)
    if job and hasattr(job, "chat_id") and getattr(job, "chat_id"):
        cid = getattr(job, "chat_id")
        return project_manager.get_chat(cid), cid

    # Last resort: scan every stored chat looking for a matching job id.
    for c_item in project_manager.list_chats():
        cid = c_item.get("id")
        if not cid:
            continue
        fc = project_manager.get_chat(cid)
        if not fc:
            continue
        if fc.get("job_id") == identifier:
            return fc, cid
        for turn in fc.get("turns", []):
            if turn.get("job_id") == identifier:
                return fc, cid

    fallback_id = identifier if identifier.startswith("chat_") else f"chat_{identifier}"
    return None, fallback_id


def compute_job_delta_files(job_id: str, files_dir: Path) -> List[Dict[str, Any]]:
    """
    Compute the files created or modified specifically during a given job.

    The comparison is done against the pre-execution snapshot captured by the
    run-creation endpoint (``JOB_PRE_SNAPSHOTS``). Files not present in the
    snapshot are reported as ``created``; files whose ``(mtime, size)`` tuple
    changed are reported as ``modified``.

    Returns:
        A list of dictionaries sorted so that non-empty files come first,
        then by relative path.
    """
    pre_snap = JOB_PRE_SNAPSHOTS.get(job_id, {})
    delta_list: List[Dict[str, Any]] = []

    if not files_dir.exists():
        return delta_list

    for root, _dirs, f_names in os.walk(files_dir):
        for f in f_names:
            p = Path(root) / f
            try:
                rel = str(p.relative_to(files_dir)).replace("\\", "/")
                st = p.stat()
                curr_stat = (st.st_mtime, st.st_size)

                if rel not in pre_snap:
                    delta_list.append({
                        "name": f,
                        "path": rel,
                        "size": st.st_size,
                        "action": "created",
                        "status": "created",
                    })
                elif curr_stat != pre_snap[rel]:
                    delta_list.append({
                        "name": f,
                        "path": rel,
                        "size": st.st_size,
                        "action": "modified",
                        "status": "modified",
                    })
            except Exception:
                # Skip files that disappear or become unreadable mid-walk.
                pass

    # Non-empty files first, then alphabetical for stable ordering.
    delta_list.sort(key=lambda x: (0 if x["size"] > 0 else 1, x["path"]))
    return delta_list


def resolve_physical_file(files_dir: Path, requested_path: str) -> Optional[Path]:
    """
    Resolve a physical file from a user-supplied relative path.

    The resolver defends against path traversal by requiring the resolved
    target to remain inside ``files_dir``. When the request contains only a
    bare filename (no directory component) and no direct match exists, the
    resolver performs a recursive lookup for a non-empty file with that name.

    Returns:
        The resolved :class:`Path` if a safe, existing file was found,
        otherwise ``None``.
    """
    decoded = urllib.parse.unquote(requested_path)
    clean_rel = decoded.lstrip("/\\").replace("\\", "/")

    # Normalize paths that were provided with a leading "files/" segment.
    if "files/" in clean_rel:
        clean_rel = clean_rel.split("files/", 1)[1]

    direct_target = (files_dir / clean_rel).resolve()
    try:
        direct_target.relative_to(files_dir.resolve())
        if direct_target.is_file():
            return direct_target
    except ValueError:
        # Path escaped the workspace; ignore and fall through to the search.
        pass

    # Fallback: search by filename ONLY when no directory component was given.
    target_name = Path(clean_rel).name
    if "/" not in clean_rel:
        for sub_file in files_dir.rglob(target_name):
            if sub_file.is_file() and sub_file.stat().st_size > 0:
                return sub_file

    return None
