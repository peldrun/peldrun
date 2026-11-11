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


def _patch_index_statuses(status_by_id: Dict[str, str]) -> None:
    """
    Update the persisted ``index.json`` entries for the given
    identifiers, mapping each to its new status string.

    Generalisation of the historical ``_patch_index_interrupted``
    helper. Recovery may now resolve a session to ``interrupted``,
    ``cancelled``, or the actual terminal status reported by
    RecoveryManager (``completed`` / ``failed`` / ``cancelled``), so
    the patch helper accepts a per-id status instead of hard-coding
    ``"interrupted"``.

    Keys in ``status_by_id`` may be either chat ids or job ids; a
    matching chat row is updated by either. Only the ``status`` and
    ``updated_at`` fields are touched. The write is atomic via
    ``project_manager._write_index``.
    """
    if not status_by_id:
        return

    idx = project_manager._read_index()
    chats = idx.get("chats", [])
    if not isinstance(chats, list):
        return

    changed = False
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    for c in chats:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        jid = c.get("job_id")
        new_status = status_by_id.get(cid) or status_by_id.get(jid)
        if new_status and c.get("status") != new_status:
            c["status"] = new_status
            c["updated_at"] = now_str
            changed = True

    if changed:
        project_manager._write_index(idx)


async def _enrich_recovery_plans(
    orphan_job_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Ask RecoveryManager whether each orphan can actually be resumed.

    For each job_id, calls ``RecoveryManager.plan_resume(job_id)`` and
    returns a compact dictionary that captures the essential facts
    needed by the write-back pass:

        {
            "resumable":      bool,
            "reason":         str,   # RecoveryReason.value
            "reason_text":    str,   # human-readable elaboration
            "next_step":      int,   # step to resume from
            "has_checkpoint": bool,
            "actual_status":  Optional[str],  # state.status.value
        }

    Returns an empty dict when RecoveryManager is unavailable (import
    error, DB construction error, or an unexpectedly empty registry).
    Every per-run failure is caught, logged at WARNING, and reported
    as ``resumable=False`` so the caller keeps the historical
    "interrupted" behavior for that run.

    This helper is deliberately isolated from the write-back pass:
    a bug in RecoveryManager never prevents us from promoting orphaned
    sessions to ``interrupted``, which is the primary safety net.
    """
    if not orphan_job_ids:
        return {}

    try:
        from peldrun.runtime.recovery_manager import RecoveryManager
        from peldrun.runtime.store import get_run_store
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "RecoveryManager unavailable for startup sweep: %s", exc
        )
        return {}

    try:
        store = get_run_store()
        manager = RecoveryManager(persistence=store)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not construct RecoveryManager: %s", exc
        )
        return {}

    results: Dict[str, Dict[str, Any]] = {}
    for job_id in orphan_job_ids:
        if not job_id:
            continue
        try:
            plan = await manager.plan_resume(job_id)
            actual_status: Optional[str] = None
            if plan.state is not None:
                state_status = plan.state.status
                actual_status = (
                    state_status.value
                    if hasattr(state_status, "value")
                    else str(state_status)
                )
            results[job_id] = {
                "resumable": bool(plan.resumable),
                "reason": (
                    plan.reason.value
                    if plan.reason is not None
                    else "unknown"
                ),
                "reason_text": plan.reason_text or "",
                "next_step": int(plan.next_step or 0),
                "has_checkpoint": plan.latest_checkpoint is not None,
                "actual_status": actual_status,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "plan_resume failed for run %r: %s", job_id, exc
            )
            results[job_id] = {
                "resumable": False,
                "reason": "plan_error",
                "reason_text": f"plan_resume raised: {exc}",
                "next_step": 0,
                "has_checkpoint": False,
                "actual_status": None,
            }

    return results


async def recover_orphaned_sessions() -> Dict[str, Any]:
    """
    Scan every persisted ``session.json`` and reconcile orphaned runs.

    An "orphaned" run is a session whose ``status`` is non-terminal but
    for which no live task is registered in ``ACTIVE_JOB_TASKS`` under
    any of its known aliases (``chat_id``, ``job_id``).

    For each orphan the function now does two things:

      1. **Promotes it to a terminal status** (same as before, with a
         smarter mapping — see below).
      2. **Enriches it with a RecoveryManager plan** so the UI can
         offer a Resume button when the run is actually resumable.
         A ``resumable`` boolean and (when True) a ``recovery_info``
         block are persisted to ``session.json``.

    Final status mapping
    --------------------
    The status written to ``session.json`` is derived from the plan
    reason, defaulting to the historical ``"interrupted"``:

      * ``already_terminal`` + a known ``actual_status``
          -> use the state's own terminal status (``completed`` /
             ``failed`` / ``cancelled``). The session was already
             finished; the previous ``"interrupted"`` label was
             inaccurate.
      * ``cancelled_by_operator``
          -> ``"cancelled"``. A cancellation was requested before
             the crash; honoring operator intent is safer than
             marking the run as interrupted.
      * anything else (including ``not_found``, ``plan_error``,
        ``resumable_*``)
          -> ``"interrupted"``. Matches the pre-existing behavior.

    **Breaking change**: this function is now ``async def``. Callers
    MUST ``await`` it. A caller that invokes it without ``await``
    receives a coroutine object that never runs — Python emits a
    ``RuntimeWarning: coroutine was never awaited`` at GC time, which
    is the only signal of the missing ``await``. The startup wiring
    in ``omweb/main.py`` (lifespan) must be updated accordingly.

    Returns a report dictionary:

        {
            "scanned":               int,   # session.json files examined
            "interrupted":           int,   # sessions promoted (any terminal status)
            "resumable_count":       int,   # subset that can be resumed
            "skipped_alive":         int,   # live task found; left alone
            "skipped_terminal":      int,   # already terminal
            "skipped_unreadable":    int,   # parse errors
            "interrupted_sessions":  [ ... ],   # up to 50 entries
            "resumable_sessions":    [ ... ],   # up to 50 entries
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
        "resumable_count": 0,
        "skipped_alive": 0,
        "skipped_terminal": 0,
        "skipped_unreadable": 0,
        "interrupted_sessions": [],
        "resumable_sessions": [],
        "errors": [],
    }

    session_files = _collect_session_files()
    report["scanned"] = len(session_files)

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------
    # Pass 1 — collect orphans.
    # ------------------------------------------------------------------
    orphans: List[Dict[str, Any]] = []
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

        if status not in _NON_TERMINAL_STATUSES:
            report["skipped_terminal"] += 1
            continue

        live_task = (
            ACTIVE_JOB_TASKS.get(chat_id)
            or (ACTIVE_JOB_TASKS.get(job_id) if job_id else None)
        )
        if _is_task_alive(live_task):
            report["skipped_alive"] += 1
            continue

        orphans.append({
            "session_file": session_file,
            "data": data,
            "chat_id": chat_id,
            "job_id": job_id,
            "previous_status": status,
        })

    # ------------------------------------------------------------------
    # Pass 2 — enrich with RecoveryManager plans.
    #
    # A failure here is non-fatal: plan_map stays empty and Pass 3
    # falls back to the historical "interrupted" default for every
    # orphan, preserving the pre-P0-Item-6 behavior end-to-end.
    # ------------------------------------------------------------------
    plan_map: Dict[str, Dict[str, Any]] = {}
    if orphans:
        try:
            plan_map = await _enrich_recovery_plans(
                [o["job_id"] for o in orphans if o["job_id"]]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RecoveryManager enrichment failed; falling back to "
                "interrupted-only sweep: %s",
                exc,
            )
            plan_map = {}

    # ------------------------------------------------------------------
    # Pass 3 — write back.
    # ------------------------------------------------------------------
    touched_statuses: Dict[str, str] = {}

    for orphan in orphans:
        session_file = orphan["session_file"]
        data = orphan["data"]
        chat_id = orphan["chat_id"]
        job_id = orphan["job_id"]
        previous_status = orphan["previous_status"]
        plan_info = plan_map.get(job_id, {}) if job_id else {}

        reason_value = plan_info.get("reason", "")
        final_status = "interrupted"
        is_resumable = False

        if (
            reason_value == "already_terminal"
            and plan_info.get("actual_status")
        ):
            # The state was already in a terminal status before the
            # crash. Reporting "interrupted" would contradict the
            # persisted audit trail.
            final_status = str(plan_info["actual_status"])
        elif reason_value == "cancelled_by_operator":
            # Operator intent takes precedence.
            final_status = "cancelled"
        elif plan_info.get("resumable"):
            is_resumable = True

        data["status"] = final_status
        data["updated_at"] = now_str
        data["resumable"] = is_resumable
        if is_resumable:
            data["recovery_info"] = {
                "reason": reason_value,
                "next_step": int(plan_info.get("next_step") or 0),
                "has_checkpoint": bool(
                    plan_info.get("has_checkpoint", False)
                ),
            }
        else:
            # Clear any stale recovery_info from a previous sweep.
            data.pop("recovery_info", None)

        try:
            _atomic_write_json(session_file, data)
        except Exception as exc:
            report["errors"].append({
                "file": str(session_file),
                "error": f"Failed to persist recovery status: {exc}",
            })
            continue

        report["interrupted"] += 1
        if is_resumable:
            report["resumable_count"] += 1
            if len(report["resumable_sessions"]) < 50:
                report["resumable_sessions"].append({
                    "chat_id": chat_id,
                    "job_id": job_id,
                    "previous_status": previous_status,
                    "recovery_reason": reason_value,
                    "next_step": int(plan_info.get("next_step") or 0),
                })

        if len(report["interrupted_sessions"]) < 50:
            report["interrupted_sessions"].append({
                "chat_id": chat_id,
                "job_id": job_id,
                "previous_status": previous_status,
                "final_status": final_status,
                "resumable": is_resumable,
            })

        touched_statuses[chat_id] = final_status
        if job_id:
            touched_statuses[job_id] = final_status

    # ------------------------------------------------------------------
    # Pass 4 — patch index.json once at the end.
    # ------------------------------------------------------------------
    if touched_statuses:
        try:
            _patch_index_statuses(touched_statuses)
        except Exception as exc:
            report["errors"].append({
                "file": "<index.json>",
                "error": f"Failed to patch index: {exc}",
            })

    logger.info(
        "Session recovery: scanned=%d interrupted=%d resumable=%d "
        "alive=%d terminal=%d unreadable=%d errors=%d",
        report["scanned"],
        report["interrupted"],
        report["resumable_count"],
        report["skipped_alive"],
        report["skipped_terminal"],
        report["skipped_unreadable"],
        len(report["errors"]),
    )

    return report


__all__ = ["recover_orphaned_sessions"]
