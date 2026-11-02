# Path: backend/omweb/services/recovery.py
"""
Session recovery service.

Reconciles persisted session state with the live run registry.

## The problem this solves

``job_manager._jobs`` and ``ACTIVE_JOB_TASKS`` are in-memory structures.
When the backend process terminates (crash, forced restart, graceful
shutdown during an active run), these structures are lost, but
``session.json`` files on disk retain whatever status they held at
termination time.

A session that was ``running`` when the process died will therefore be
reported as ``running`` on the next startup, but no worker actually owns
it. The downstream consequences are:

  * ``GET /api/run/jobs/{id}`` returns ``status: "running"`` alongside
    ``is_actively_running: false``. A well-behaved client (Phase D) will
    not open an SSE stream, but the sidebar and history views will still
    show a live indicator, misleading the user into waiting for a run
    that will never complete.

  * The session can never transition to a terminal state through normal
    paths, because no worker exists to dispatch a ``final`` or
    ``cancelled`` event.

  * Repeated manual cleanup becomes the only escape, which is not a
    sustainable operator experience.

## The solution

On startup, walk every ``session.json`` on disk and promote any session
whose status is non-terminal but has no live task to ``interrupted``.

``interrupted`` is a **distinct terminal state** — neither ``failed``
nor ``cancelled``. It communicates:

    "The run ended without a user action and without a recoverable
     application error (typically a backend restart or crash)."

The frontend can display it with a neutral indicator and offer a
"Retry" action. The user retains full agency: they can re-run the
prompt without any data loss.

## Idempotency

Safe to call any number of times. The function only touches sessions
whose status is currently non-terminal **and** whose task is not alive.
After one successful pass, every orphaned session is already
``interrupted``; a second pass finds nothing to do and reports
``interrupted: 0``.

## Order of writes

``_atomic_write_json`` guarantees per-file atomicity. The function
writes each ``session.json`` individually, then patches ``index.json``
once at the end. If the process crashes mid-recovery, the next startup
re-runs the same scan and picks up the remaining files.

## Runtime callability

Although the primary use case is startup recovery, the function is
also safe to invoke at runtime (e.g. from an admin endpoint). At
runtime, ``ACTIVE_JOB_TASKS`` may contain live tasks, and those are
correctly preserved via the ``skipped_alive`` path.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from omweb.project_manager import project_manager, _atomic_write_json


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical status classification
#
# See ``docs/state-contracts.md`` §3 for the full taxonomy. Only the
# non-terminal subset matters here: a session in any of these states has
# an obligation to transition to a terminal state eventually. If the
# process has restarted and no task exists to fulfil that obligation,
# the session is orphaned.
#
# Terminal states (``completed``, ``cancelled``, ``failed``,
# ``interrupted``) are skipped: they are already in their final form and
# require no reconciliation.
# ---------------------------------------------------------------------------

_NON_TERMINAL_STATUSES: frozenset[str] = frozenset((
    "pending",
    "queued",
    "starting",
    "running",
    "waiting_for_input",
    "retrying",
    "paused",
    "cancelling",
))


def _is_task_alive(task: Any) -> bool:
    """
    Return True when a registry entry represents a live task.

    ``None`` entries and completed tasks are never alive. Unexpected
    failures in ``done()`` are treated as "alive" so the caller fails
    safe (does not mark a potentially-live run as interrupted).
    """
    if task is None:
        return False
    done = getattr(task, "done", None)
    if callable(done):
        try:
            return not done()
        except Exception:
            return True
    return True


def _collect_session_files() -> List[Path]:
    """
    Return every ``session.json`` path in both supported storage layouts:

        * ``storage/chats/<chat_id>/session.json``
        * ``storage/projects/<project_id>/chats/<chat_id>/session.json``

    Both are valid; a chat may live under either, and the recovery must
    cover both to be complete. The order (standalone first) matches the
    resolution order in ``ProjectManager.get_chat`` so the report reflects
    what the API would return.
    """
    paths: List[Path] = []

    chats_dir = project_manager.chats_dir
    if chats_dir.exists():
        for chat_dir in chats_dir.iterdir():
            if not chat_dir.is_dir():
                continue
            session_file = chat_dir / "session.json"
            if session_file.is_file():
                paths.append(session_file)

    projects_dir = project_manager.projects_dir
    if projects_dir.exists():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            project_chats_dir = proj_dir / "chats"
            if not project_chats_dir.exists():
                continue
            for chat_dir in project_chats_dir.iterdir():
                if not chat_dir.is_dir():
                    continue
                session_file = chat_dir / "session.json"
                if session_file.is_file():
                    paths.append(session_file)

    return paths


def _patch_index_interrupted(chat_ids: List[str]) -> None:
    """
    Update the persisted ``index.json`` entries for the given chat ids
    so the sidebar reflects the recovery without requiring a full
    reindex pass.

    Only the ``status`` field is modified. Every other field is
    preserved verbatim. The write is atomic via
    ``project_manager._write_index``.
    """
    idx = project_manager._read_index()
    chats = idx.get("chats", [])
    if not isinstance(chats, list):
        return

    target = set(chat_ids)
    changed = False
    for c in chats:
        if not isinstance(c, dict):
            continue
        if c.get("id") in target or c.get("job_id") in target:
            if c.get("status") != "interrupted":
                c["status"] = "interrupted"
                c["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                changed = True

    if changed:
        project_manager._write_index(idx)


def recover_orphaned_sessions() -> Dict[str, Any]:
    """
    Scan every persisted ``session.json`` and promote orphaned runs to
    ``interrupted``.

    An "orphaned" run is a session whose ``status`` is non-terminal but
    for which no live task is registered in ``ACTIVE_JOB_TASKS`` under
    any of its known aliases (``chat_id``, ``job_id``).

    Returns a report dictionary:

        {
            "scanned":               int,   # session.json files examined
            "interrupted":           int,   # sessions promoted
            "skipped_alive":         int,   # live task found; left alone
            "skipped_terminal":      int,   # already terminal
            "skipped_unreadable":    int,   # parse errors
            "interrupted_sessions":  [ ... ],   # up to 50 entries
            "errors":                [ ... ],
        }

    Every entry in ``errors`` is non-fatal: the function continues past
    it and reports the rest of the scan. A malformed session.json is
    reported and skipped; a failed write is reported and the function
    moves on. This makes the function safe to call on a partially
    corrupted storage tree.
    """
    # Lazy import of ACTIVE_JOB_TASKS avoids a module-load cycle:
    #   omweb.services.recovery
    #     -> omweb.routers.run_routes.state
    #     -> omweb.routers.run_routes.__init__
    #     -> omweb.routers.run_routes.start_run
    #     -> omweb.agent_bridge
    #     -> omweb.agent_bridge_parts
    #     -> omweb.agent_bridge_parts.dispatcher
    #     -> omweb.chat_storage_engine
    #     -> omweb.project_manager
    # The import cost is negligible (runs once at startup).
    from omweb.routers.run_routes.state import ACTIVE_JOB_TASKS

    report: Dict[str, Any] = {
        "scanned": 0,
        "interrupted": 0,
        "skipped_alive": 0,
        "skipped_terminal": 0,
        "skipped_unreadable": 0,
        "interrupted_sessions": [],
        "errors": [],
    }

    session_files = _collect_session_files()
    report["scanned"] = len(session_files)

    touched_chat_ids: List[str] = []
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    for session_file in session_files:
        try:
            raw = session_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:
            report["skipped_unreadable"] += 1
            report["errors"].append({
                "file": str(session_file),
                "error": f"Failed to read: {exc}",
            })
            continue

        if not isinstance(data, dict):
            report["skipped_unreadable"] += 1
            report["errors"].append({
                "file": str(session_file),
                "error": "session.json is not a JSON object",
            })
            continue

        status = str(data.get("status", "")).strip().lower()
        chat_id = str(data.get("id") or session_file.parent.name)
        job_id = str(data.get("job_id") or "")

        # Terminal sessions are already in their final form.
        if status not in _NON_TERMINAL_STATUSES:
            report["skipped_terminal"] += 1
            continue

        # A live task means the run is genuinely in progress. ``start_run.py``
        # registers the task under both the canonical job id and the chat id,
        # so either key is authoritative.
        live_task = (
            ACTIVE_JOB_TASKS.get(chat_id)
            or (ACTIVE_JOB_TASKS.get(job_id) if job_id else None)
        )
        if _is_task_alive(live_task):
            report["skipped_alive"] += 1
            continue

        # Orphaned. Promote to interrupted.
        data["status"] = "interrupted"
        data["updated_at"] = now_str
        try:
            _atomic_write_json(session_file, data)
        except Exception as exc:
            report["errors"].append({
                "file": str(session_file),
                "error": f"Failed to persist interrupted status: {exc}",
            })
            continue

        report["interrupted"] += 1
        if len(report["interrupted_sessions"]) < 50:
            report["interrupted_sessions"].append({
                "chat_id": chat_id,
                "job_id": job_id,
                "previous_status": status,
            })
        touched_chat_ids.append(chat_id)

    # Patch index.json once at the end.
    if touched_chat_ids:
        try:
            _patch_index_interrupted(touched_chat_ids)
        except Exception as exc:
            report["errors"].append({
                "file": "<index.json>",
                "error": f"Failed to patch index: {exc}",
            })

    logger.info(
        "Session recovery: scanned=%d interrupted=%d alive=%d terminal=%d unreadable=%d errors=%d",
        report["scanned"],
        report["interrupted"],
        report["skipped_alive"],
        report["skipped_terminal"],
        report["skipped_unreadable"],
        len(report["errors"]),
    )

    return report


__all__ = ["recover_orphaned_sessions"]
