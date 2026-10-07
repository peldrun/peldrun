"""
Sovereign Chat Storage & Disk Governance Engine.

Synchronizes with root storage/index.json, purges backend/jobs.json, governs sessions,
and maintains the non-destructive session.context.json Sidecar for LLM context optimization.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from omweb.project_manager import project_manager

# Ensure backend root is accessible for peldrun package imports
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    from peldrun.context.sidecar import (
        build_context_sidecar,
        load_context_sidecar,
        save_context_sidecar,
        get_sidecar_path,
    )
except ImportError:
    # Graceful fallback if peldrun is located under core or sibling structure
    build_context_sidecar = None
    load_context_sidecar = None
    save_context_sidecar = None
    get_sidecar_path = None


class ChatStorageEngine:
    def __init__(self):
        # Bound directly to the real storage directory used by project_manager
        self.storage_dir = project_manager.storage_dir
        self.root_dir = self.storage_dir.parent
        self.chats_dir = project_manager.chats_dir
        self.projects_dir = project_manager.projects_dir
        self.index_file = project_manager.index_file
        self.backend_dir = self.root_dir / "backend"
        self.workspace_dir = self.root_dir / "workspace"
        self.jobs_json_path = self.backend_dir / "jobs.json"

        self.chats_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _parse_created_timestamp(self, val: Any) -> float:
        if isinstance(val, (int, float)):
            return float(val) if val < 1e11 else float(val) / 1000.0
        if isinstance(val, str):
            val = val.strip()
            try:
                return float(val)
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(val[:19], fmt)
                    return dt.timestamp()
                except Exception:
                    pass
        return 0.0

    def _purge_from_jobs_json(self, target_ids: Set[str]) -> int:
        """Purge entries matching chat_id or job_id from backend/jobs.json."""
        if not target_ids or not self.jobs_json_path.exists():
            return 0

        purged_count = 0
        try:
            raw = self.jobs_json_path.read_text(encoding="utf-8")
            jobs_data = json.loads(raw)
            if isinstance(jobs_data, list):
                original_len = len(jobs_data)
                cleaned = [
                    j for j in jobs_data
                    if str(j.get("job_id", "")) not in target_ids
                    and str(j.get("id", "")) not in target_ids
                ]
                purged_count = original_len - len(cleaned)
                if purged_count > 0:
                    self.jobs_json_path.write_text(
                        json.dumps(cleaned, indent=2, ensure_ascii=False),
                        encoding="utf-8"
                    )
        except Exception as e:
            print(f"[STORAGE ENGINE] Failed to purge from jobs.json: {e}")

        # In-memory cleanup
        try:
            from omweb.job_manager import job_manager
            for tid in target_ids:
                if tid in job_manager._jobs:
                    del job_manager._jobs[tid]
        except Exception:
            pass

        return purged_count

    # =========================================================================
    # Context Sidecar Management & Synchronization Hooks (Stage 1)
    # =========================================================================

    def sync_sidecar(
        self,
        chat_id: str,
        model_id: Optional[str] = None,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Path]:
        """
        Build or refresh 'session.context.json' alongside 'session.json'.

        Extracts clean conversational dialogue, isolates active execution telemetry,
        and applies dynamic per-model token budget allocations.
        Guarantees zero side-effects on existing frontend storage.
        """
        if not build_context_sidecar or not save_context_sidecar:
            return None

        session_dir = self.chats_dir / chat_id
        session_file = session_dir / "session.json"
        if not session_file.exists():
            return None

        try:
            session_raw = session_file.read_text(encoding="utf-8")
            session_data = json.loads(session_raw)
            if not isinstance(session_data, dict):
                return None

            # Look for existing events file if events list not provided directly
            events_data = events
            if events_data is None:
                events_file = session_dir / "events.json"
                if events_file.exists():
                    try:
                        raw_ev = events_file.read_text(encoding="utf-8")
                        events_data = json.loads(raw_ev)
                    except Exception:
                        events_data = None

            existing_sidecar = None
            if load_context_sidecar:
                existing_sidecar = load_context_sidecar(session_dir)

            sidecar = build_context_sidecar(
                session_id=chat_id,
                session_data=session_data,
                events_data=events_data,
                existing_sidecar=existing_sidecar,
                model_id=model_id or session_data.get("model"),
                storage_root=self.storage_dir,
            )

            saved_path = save_context_sidecar(session_dir, sidecar)
            return saved_path
        except Exception as err:
            print(f"[STORAGE ENGINE] Warning: Failed to synchronize context sidecar for {chat_id}: {err}")
            return None

    def get_sidecar_data(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve active context sidecar dictionary for a chat session."""
        session_dir = self.chats_dir / chat_id
        if load_context_sidecar:
            sidecar_obj = load_context_sidecar(session_dir)
            if sidecar_obj:
                return sidecar_obj.model_dump() if hasattr(sidecar_obj, "model_dump") else sidecar_obj.dict()

        # Fallback raw read
        sidecar_file = session_dir / "session.context.json"
        if sidecar_file.exists():
            try:
                return json.loads(sidecar_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def record_turn_and_sync(
        self,
        chat_id: str,
        user_message: str,
        assistant_response: str,
        model_id: Optional[str] = None,
        steps: Optional[List[Dict[str, Any]]] = None,
        artifacts: Optional[List[str]] = None,
    ) -> Optional[Path]:
        """
        Record a completed conversation turn to session.json and automatically
        synchronize the session.context.json Sidecar file.
        """
        session_dir = self.chats_dir / chat_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "session.json"

        session_data: Dict[str, Any] = {
            "id": chat_id,
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "turns": [],
            "messages": [],
        }

        if session_file.exists():
            try:
                session_data = json.loads(session_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        turn_index = len(session_data.get("turns", [])) + 1
        turn_id = f"turn_{turn_index}_{int(datetime.now().timestamp())}"

        turn_entry = {
            "id": turn_id,
            "user": user_message,
            "assistant": assistant_response,
            "timestamp": self._now_iso(),
            "steps": steps or [],
            "artifacts": artifacts or [],
        }

        turns = session_data.setdefault("turns", [])
        turns.append(turn_entry)

        # Mirror in flat messages array for legacy backward compatibility
        messages = session_data.setdefault("messages", [])
        messages.append({"role": "user", "content": user_message, "timestamp": self._now_iso()})
        messages.append({"role": "assistant", "content": assistant_response, "timestamp": self._now_iso()})

        session_data["updated_at"] = self._now_iso()
        if model_id:
            session_data["model"] = model_id

        # Write presentation session.json atomically
        tmp_session = session_file.with_suffix(".tmp")
        tmp_session.write_text(json.dumps(session_data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_session.replace(session_file)

        # Synchronize sidecar alongside session.json
        return self.sync_sidecar(chat_id, model_id=model_id)

    # =========================================================================
    # Standard Chat Storage Management
    # =========================================================================

    def list_chats(
        self,
        project_id: Optional[str] = None,
        include_archived: bool = False,
        pinned_only: bool = False,
        search: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        raw_chats = project_manager.list_chats()
        results = []

        for c in raw_chats:
            if not isinstance(c, dict):
                continue

            c_pid = c.get("project_id", "default_project")
            if project_id and project_id != "default_project":
                if c_pid != project_id:
                    continue

            is_archived = c.get("is_archived", False)
            is_pinned = c.get("is_pinned", c.get("pinned", False))

            if not include_archived and is_archived:
                continue
            if pinned_only and not is_pinned:
                continue

            if search:
                q = search.lower().strip()
                title_match = q in (c.get("title") or "").lower()
                prompt_match = q in (c.get("prompt") or "").lower()
                id_match = q in (c.get("id") or "").lower()
                job_match = q in (c.get("job_id") or "").lower()
                if not (title_match or prompt_match or id_match or job_match):
                    continue

            results.append(c)

        # Sort: pinned first, then chronological descending
        results.sort(
            key=lambda x: (
                1 if (x.get("is_pinned") or x.get("pinned")) else 0,
                self._parse_created_timestamp(x.get("created_at"))
            ),
            reverse=True
        )
        return results

    def toggle_pin(self, chat_id: str) -> Optional[Dict[str, Any]]:
        idx = project_manager._read_index()
        target_chat = None

        for c in idx.get("chats", []):
            if isinstance(c, dict) and (c.get("id") == chat_id or c.get("job_id") == chat_id):
                target_chat = c
                break

        if not target_chat:
            return None

        current = target_chat.get("is_pinned", target_chat.get("pinned", False))
        new_state = not current
        target_chat["is_pinned"] = new_state
        target_chat["pinned"] = new_state
        target_chat["pinned_at"] = self._now_iso() if new_state else None
        target_chat["updated_at"] = self._now_iso()

        project_manager._write_index(idx)

        actual_id = target_chat.get("id", chat_id)
        s_path = self.chats_dir / actual_id / "session.json"
        if s_path.exists():
            try:
                s_data = json.loads(s_path.read_text(encoding="utf-8"))
                s_data["is_pinned"] = new_state
                s_data["pinned"] = new_state
                s_path.write_text(json.dumps(s_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return target_chat

    def toggle_archive(self, chat_id: str) -> Optional[Dict[str, Any]]:
        idx = project_manager._read_index()
        target_chat = None

        for c in idx.get("chats", []):
            if isinstance(c, dict) and (c.get("id") == chat_id or c.get("job_id") == chat_id):
                target_chat = c
                break

        if not target_chat:
            return None

        current = target_chat.get("is_archived", False)
        new_state = not current
        target_chat["is_archived"] = new_state
        target_chat["archived_at"] = self._now_iso() if new_state else None
        target_chat["updated_at"] = self._now_iso()

        project_manager._write_index(idx)

        actual_id = target_chat.get("id", chat_id)
        s_path = self.chats_dir / actual_id / "session.json"
        if s_path.exists():
            try:
                s_data = json.loads(s_path.read_text(encoding="utf-8"))
                s_data["is_archived"] = new_state
                s_path.write_text(json.dumps(s_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return target_chat

    def rename_chat(self, chat_id: str, new_title: str) -> Optional[Dict[str, Any]]:
        clean_title = new_title.strip()
        if not clean_title:
            return None

        idx = project_manager._read_index()
        target_chat = None

        for c in idx.get("chats", []):
            if isinstance(c, dict) and (c.get("id") == chat_id or c.get("job_id") == chat_id):
                target_chat = c
                break

        if not target_chat:
            return None

        target_chat["title"] = clean_title
        target_chat["updated_at"] = self._now_iso()

        project_manager._write_index(idx)

        actual_id = target_chat.get("id", chat_id)
        s_path = self.chats_dir / actual_id / "session.json"
        if s_path.exists():
            try:
                s_data = json.loads(s_path.read_text(encoding="utf-8"))
                s_data["title"] = clean_title
                s_path.write_text(json.dumps(s_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return target_chat

    def deep_delete_chat(self, chat_id: str) -> Dict[str, Any]:
        """Deep delete: wipes chat directory (including session.json and session.context.json)."""
        idx = project_manager._read_index()
        target_chat = None

        for c in idx.get("chats", []):
            if isinstance(c, dict) and (c.get("id") == chat_id or c.get("job_id") == chat_id):
                target_chat = c
                break

        job_id = target_chat.get("job_id", "") if target_chat else ""
        project_id = target_chat.get("project_id", "default_project") if target_chat else "default_project"
        target_ids: Set[str] = {t for t in [chat_id, job_id] if t}

        deleted_paths: List[str] = []
        freed_bytes: int = 0

        candidate_chat_folders = [
            self.chats_dir / chat_id,
            self.chats_dir / job_id,
            self.projects_dir / project_id / "chats" / chat_id,
            self.projects_dir / project_id / "chats" / job_id
        ]
        for cf in candidate_chat_folders:
            if cf.exists():
                if cf.is_dir():
                    for root, _, files in os.walk(cf):
                        for f in files:
                            try:
                                freed_bytes += (Path(root) / f).stat().st_size
                            except Exception:
                                pass
                    shutil.rmtree(cf, ignore_errors=True)
                else:
                    freed_bytes += cf.stat().st_size
                    cf.unlink(missing_ok=True)
                deleted_paths.append(str(cf))

        candidate_ws_folders = [
            self.workspace_dir / chat_id,
            self.workspace_dir / job_id,
            self.backend_dir / "workspace" / chat_id,
            self.backend_dir / "workspace" / job_id
        ]
        for ws in candidate_ws_folders:
            if ws.exists() and ws.is_dir():
                for root, _, files in os.walk(ws):
                    for f in files:
                        try:
                            freed_bytes += (Path(root) / f).stat().st_size
                        except Exception:
                            pass
                shutil.rmtree(ws, ignore_errors=True)
                deleted_paths.append(str(ws))

        idx["chats"] = [
            c for c in idx.get("chats", [])
            if (c.get("id") if isinstance(c, dict) else str(c)) not in target_ids
            and (c.get("job_id") if isinstance(c, dict) else "") not in target_ids
        ]
        project_manager._write_index(idx)

        purged_jobs_count = self._purge_from_jobs_json(target_ids)

        return {
            "status": "ok",
            "chat_id": chat_id,
            "job_id": job_id,
            "purged_jobs_records": purged_jobs_count,
            "freed_bytes": freed_bytes,
            "deleted_paths": deleted_paths
        }

    def delete_all_chats(self) -> Dict[str, Any]:
        """Deep purge: deletes all chats, project chat references, workspace artifacts,
        and completely empties backend/jobs.json."""
        idx = project_manager._read_index()
        all_chats = idx.get("chats", [])
        target_ids: Set[str] = set()

        for c in all_chats:
            if isinstance(c, dict):
                if c.get("id"): target_ids.add(c["id"])
                if c.get("job_id"): target_ids.add(c["job_id"])
            elif isinstance(c, str):
                target_ids.add(c)

        freed_bytes = 0

        # 1. Clear chats folder
        if self.chats_dir.exists():
            for item in self.chats_dir.iterdir():
                try:
                    if item.is_dir():
                        for root, _, files in os.walk(item):
                            for f in files:
                                try: freed_bytes += (Path(root) / f).stat().st_size
                                except Exception: pass
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        freed_bytes += item.stat().st_size
                        item.unlink(missing_ok=True)
                except Exception:
                    pass

        # 2. Clear all workspace folders
        for ws_root in [self.workspace_dir, self.backend_dir / "workspace"]:
            if ws_root.exists():
                for item in ws_root.iterdir():
                    try:
                        if item.is_dir():
                            for root, _, files in os.walk(item):
                                for f in files:
                                    try: freed_bytes += (Path(root) / f).stat().st_size
                                    except Exception: pass
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            freed_bytes += item.stat().st_size
                            item.unlink(missing_ok=True)
                    except Exception:
                        pass

        # 3. Clear chats inside project folders
        if self.projects_dir.exists():
            for p_dir in self.projects_dir.iterdir():
                if p_dir.is_dir():
                    p_chats = p_dir / "chats"
                    if p_chats.exists():
                        shutil.rmtree(p_chats, ignore_errors=True)
                        p_chats.mkdir(parents=True, exist_ok=True)

        # 4. Reset index.json chats array
        idx["chats"] = []
        for p in idx.get("projects", []):
            if isinstance(p, dict) and "chat_ids" in p:
                p["chat_ids"] = []
        project_manager._write_index(idx)

        # 5. Empty backend/jobs.json
        purged_jobs_count = self._purge_from_jobs_json(target_ids)
        if self.jobs_json_path.exists():
            try:
                self.jobs_json_path.write_text("[]", encoding="utf-8")
            except Exception:
                pass

        # 6. Clear in-memory jobs
        try:
            from omweb.job_manager import job_manager
            job_manager._jobs.clear()
        except Exception:
            pass

        # 7. Sweep any residual orphans
        sweep_rep = self.sweep_orphaned_storage()

        return {
            "status": "ok",
            "message": "All chats and workspace files permanently cleared",
            "deleted_sessions_count": len(all_chats),
            "purged_jobs_count": purged_jobs_count,
            "freed_mb": round((freed_bytes + sweep_rep.get("freed_bytes", 0)) / (1024 * 1024), 2)
        }

    def sweep_orphaned_storage(self) -> Dict[str, Any]:
        idx = project_manager._read_index()
        valid_ids: Set[str] = set()

        for c in idx.get("chats", []):
            if isinstance(c, dict):
                if c.get("id"): valid_ids.add(c["id"])
                if c.get("job_id"): valid_ids.add(c["job_id"])

        if self.jobs_json_path.exists():
            try:
                jdata = json.loads(self.jobs_json_path.read_text(encoding="utf-8"))
                if isinstance(jdata, list):
                    for j in jdata:
                        if j.get("job_id"): valid_ids.add(j["job_id"])
                        if j.get("id"): valid_ids.add(j["id"])
            except Exception:
                pass

        removed_folders: List[str] = []
        freed_bytes: int = 0

        for ws_root in [self.workspace_dir, self.backend_dir / "workspace"]:
            if ws_root.exists():
                for entry in ws_root.iterdir():
                    if entry.is_dir() and (entry.name.startswith("chat_") or entry.name.startswith("job_")):
                        if entry.name not in valid_ids:
                            for root, _, files in os.walk(entry):
                                for f in files:
                                    try:
                                        freed_bytes += (Path(root) / f).stat().st_size
                                    except Exception:
                                        pass
                            shutil.rmtree(entry, ignore_errors=True)
                            removed_folders.append(entry.name)

        return {
            "status": "ok",
            "valid_chats_count": len(valid_ids),
            "removed_orphans_count": len(removed_folders),
            "removed_folders": removed_folders,
            "freed_bytes": freed_bytes,
            "freed_mb": round(freed_bytes / (1024 * 1024), 2)
        }

    def get_storage_metrics(self) -> Dict[str, Any]:
        raw_chats = project_manager.list_chats()
        chats = [c for c in raw_chats if isinstance(c, dict)]

        total_chats = len(chats)
        pinned_chats = sum(1 for c in chats if c.get("is_pinned", c.get("pinned", False)))
        archived_chats = sum(1 for c in chats if c.get("is_archived", False))
        active_chats = total_chats - archived_chats

        total_size_bytes = 0
        if self.storage_dir.exists():
            for root, _, files in os.walk(self.storage_dir):
                for f in files:
                    try:
                        total_size_bytes += (Path(root) / f).stat().st_size
                    except Exception:
                        pass

        return {
            "total_chats": total_chats,
            "active_chats": active_chats,
            "pinned_chats": pinned_chats,
            "archived_chats": archived_chats,
            "disk_size_bytes": total_size_bytes,
            "disk_size_mb": round(total_size_bytes / (1024 * 1024), 2)
        }


chat_storage_engine = ChatStorageEngine()