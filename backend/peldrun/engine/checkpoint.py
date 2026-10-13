"""
backend/peldrun/engine/checkpoint.py

PELDRUN Core State Checkpoint Management.
Provides snapshots, persistence, and resumption capabilities for agent execution runs.
Hardened under PR 3 & Runtime V1:
- Automatic disk index synchronization on initialization.
- Integrates with RunStore to persist checkpoints into the durable runtime database.
- Preserves local filesystem JSON snapshots for inspectability and quick recovery.
- Supports single-call latest state reconstruction for resume-on-interruption workflows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from peldrun.engine.state import ExecutionState


class StateCheckpoint(BaseModel):
    """Model representing an immutable checkpoint record."""
    model_config = ConfigDict(extra="allow")

    id: str
    step: int
    label: str = ""
    file_path: str = ""
    state_data: Dict[str, Any] = Field(default_factory=dict)


class CheckpointManager:
    """Manages asynchronous checkpoint creation, disk snapshots, and RunStore synchronization."""

    def __init__(self, workspace_root: Optional[str] = None, storage_dir: Optional[str] = None) -> None:
        target_root = workspace_root or storage_dir or "."
        self.workspace_root = Path(target_root)
        self.checkpoint_dir = self.workspace_root / ".checkpoints"
        self._checkpoints: List[StateCheckpoint] = []

    async def ainitialize(self) -> None:
        """Create checkpoints persistence directory and re-index existing checkpoints from disk."""
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        if not self._checkpoints and self.checkpoint_dir.exists():
            for f in sorted(self.checkpoint_dir.glob("cp_*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self._checkpoints.append(StateCheckpoint.model_validate(data))
                except Exception:
                    pass

    async def asave_checkpoint(self, state: ExecutionState, label: str = "") -> StateCheckpoint:
        """
        Asynchronously serialize state as a checkpoint and sync with durable RunStore.
        """
        await self.ainitialize()
        cp_id = f"cp_{state.current_step}_{int(state.updated_at)}_{len(self._checkpoints) + 1}"
        file_path = self.checkpoint_dir / f"{cp_id}.json"

        cp = StateCheckpoint(
            id=cp_id,
            step=state.current_step,
            label=label,
            file_path=str(file_path),
            state_data=state.to_snapshot_dict(),
        )

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(cp.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

        self._checkpoints.append(cp)

        # Sync checkpoint to durable database
        try:
            from peldrun.runtime.store import get_run_store

            store = get_run_store()
            await store.save_checkpoint(cp, run_id=state.run_id)
        except Exception:
            pass

        return cp

    async def alist_checkpoints(self) -> List[StateCheckpoint]:
        """Return all persisted checkpoints in chronological order."""
        await self.ainitialize()
        return list(self._checkpoints)

    async def aload_checkpoint(self, checkpoint_id: str) -> Optional[StateCheckpoint]:
        """Fetch a specific checkpoint by id."""
        await self.ainitialize()
        for cp in self._checkpoints:
            if cp.id == checkpoint_id:
                return cp
        return None

    async def aload_latest_checkpoint(self) -> Optional[StateCheckpoint]:
        """Fetch the most recently created checkpoint."""
        await self.ainitialize()
        return self._checkpoints[-1] if self._checkpoints else None

    async def arestore_state(self, checkpoint_id: str) -> ExecutionState:
        """Reconstruct an ExecutionState from a saved checkpoint."""
        await self.ainitialize()
        for cp in self._checkpoints:
            if cp.id == checkpoint_id:
                return ExecutionState.from_snapshot_dict(cp.state_data)

        # Fallback to durable RunStore search
        try:
            from peldrun.runtime.store import get_run_store

            store = get_run_store()
            conn = store._get_connection()
            cursor = conn.execute(
                "SELECT state_dump_json FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            )
            row = cursor.fetchone()
            if row:
                return ExecutionState.from_snapshot_dict(json.loads(row["state_dump_json"]))
        except Exception:
            pass

        raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found.")

    async def arestore_latest_state(self) -> Optional[ExecutionState]:
        """Reconstruct an ExecutionState from the latest available checkpoint."""
        await self.ainitialize()
        latest = await self.aload_latest_checkpoint()
        if latest:
            return await self.arestore_state(latest.id)
        return None


__all__ = ["StateCheckpoint", "CheckpointManager"]
