"""
backend/peldrun/engine/checkpoint.py

PELDRUN Core State Checkpoint Management.
Provides snapshots, persistence, and resumption capabilities for agent execution runs.

Hardened under P0 Item 1 (Durability Foundation):
- Atomic disk writes: every checkpoint is written to a sibling
  ``*.tmp`` file, flushed, fsync'd, then renamed via ``os.replace``.
  A crash mid-write therefore leaves either the previous good
  checkpoint or the new one -- never a truncated file.
- Errors are surfaced, not swallowed:
    * Disk-write failures raise. The caller must not act on a
      phantom checkpoint whose disk copy is truncated or missing.
    * Durable-store failures are logged at ERROR level with the full
      traceback. The disk copy remains intact, so the runner
      proceeds, but the failure is visible in logs -- and
      RecoveryManager will not see the checkpoint until a later
      DB write succeeds.
    * Corrupt checkpoint files during startup re-index are logged
      at WARNING instead of silently discarded.
    * The DB fallback in ``arestore_state`` logs a WARNING on
      failure before raising ``FileNotFoundError``, so operators
      can distinguish "not found" from "DB unreachable".
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from peldrun.engine.state import ExecutionState

logger = logging.getLogger("peldrun.engine.checkpoint")


class StateCheckpoint(BaseModel):
    """Model representing an immutable checkpoint record."""
    model_config = ConfigDict(extra="allow")

    id: str
    step: int
    label: str = ""
    file_path: str = ""
    state_data: Dict[str, Any] = Field(default_factory=dict)


def _fsync_directory(dir_path: Path) -> None:
    """
    Best-effort fsync of a directory so that the rename is durable.

    On POSIX this guarantees that the directory entry produced by
    ``os.replace`` survives a power loss. On Windows, ``os.open`` on a
    directory does not behave the same way and there is no equivalent
    API -- the call is a silent no-op.
    """
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        # Some filesystems (tmpfs, overlayfs, some network mounts)
        # refuse directory fsync. Not fatal -- the file itself was
        # already fsync'd before the rename.
        pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


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
                except Exception as exc:
                    # [P0-1 FIX] Do not silently discard a corrupt
                    # checkpoint. A corrupt file usually signals a
                    # crashed writer from before the atomic-write fix
                    # was applied; surfacing it at WARNING makes the
                    # historical failure visible without aborting the
                    # re-index.
                    logger.warning(
                        "Skipping unreadable checkpoint file %s during re-index: %s",
                        f.name,
                        exc,
                    )

    async def asave_checkpoint(self, state: ExecutionState, label: str = "") -> StateCheckpoint:
        """
        Asynchronously serialize state as a checkpoint and sync with durable RunStore.

        Persistence contract
        --------------------
        1. The checkpoint is written to disk ATOMICALLY: a sibling
           ``*.tmp`` file is written, flushed, fsync'd, then renamed
           via ``os.replace``. On failure, the temp file is removed
           and the exception is raised -- the caller never receives
           a checkpoint whose disk copy is truncated or missing.
        2. The disk write is the authoritative persistence for the
           local CheckpointManager and for the runner. The durable
           RunStore write is a mirror used by RecoveryManager on
           process restart.
        3. Durable-store failures are logged at ERROR level; the
           disk copy stays intact and the method still returns the
           checkpoint, so the runner can proceed. RecoveryManager
           will not see the checkpoint until the DB write succeeds
           on a future attempt -- but that gap is visible in logs,
           not silent.
        """
        await self.ainitialize()
        cp_id = f"cp_{state.current_step}_{int(state.updated_at)}_{len(self._checkpoints) + 1}"
        file_path = self.checkpoint_dir / f"{cp_id}.json"
        # Unique temp name: pid + short uuid prevent collisions when
        # two runners share the same workspace root (rare, but the
        # cost of a unique suffix is negligible).
        tmp_path = file_path.with_name(
            f"{file_path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        )

        cp = StateCheckpoint(
            id=cp_id,
            step=state.current_step,
            label=label,
            file_path=str(file_path),
            # [FIX-CHECKPOINT-OPT] strip_images=True: inline base64 image
            # payloads are replaced with artifact-cache://<hash> references
            # and the raw bytes are written to the content-addressed cache.
            # Without this, each checkpoint would be megabytes on disk and
            # the run's checkpoint directory would grow to hundreds of
            # megabytes for any multimodal turn.
            state_data=state.to_snapshot_dict(strip_images=True),
        )

        # ------------------------------------------------------------------
        # 1. Atomic disk write.
        #
        # Crash-safety: a partially-written file is never visible as a
        # valid checkpoint because the final name is only produced by
        # ``os.replace`` after ``fsync`` returns. ``os.replace`` is
        # atomic on both POSIX and Windows for same-filesystem renames.
        # ------------------------------------------------------------------
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cp.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
            _fsync_directory(self.checkpoint_dir)
        except Exception as disk_err:
            # Best-effort cleanup of the temp file. The final file was
            # never produced, so no reader can pick it up.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            logger.error(
                "Failed to write checkpoint %s to disk (%s). The checkpoint is NOT persisted.",
                cp_id,
                disk_err,
            )
            # [P0-1 FIX] Do NOT return a phantom checkpoint. The
            # caller must treat this as a failure and abort or retry.
            raise

        # Register only AFTER the disk write succeeded.
        self._checkpoints.append(cp)

        # ------------------------------------------------------------------
        # 2. Mirror into the durable RunStore.
        #
        # [P0-1 FIX] Removed the historical ``except Exception: pass``.
        # A failure here leaves the disk copy intact but makes the
        # checkpoint invisible to RecoveryManager on the next process
        # restart (RecoveryManager reads from DB). Log at ERROR with
        # the full traceback so the failure is diagnosable, then
        # continue -- aborting the run would be worse than a single
        # invisible checkpoint, especially since the runner already
        # has the disk copy in memory.
        # ------------------------------------------------------------------
        try:
            from peldrun.runtime.store import get_run_store

            store = get_run_store()
            await store.save_checkpoint(cp, run_id=state.run_id)
        except Exception as db_err:
            logger.exception(
                "Checkpoint %s was written to disk (%s) but FAILED to sync "
                "to the durable RunStore for run %s. RecoveryManager will "
                "NOT see this checkpoint until the DB write succeeds. "
                "Root cause: %s",
                cp_id,
                file_path,
                state.run_id,
                db_err,
            )

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
                # [FIX-CHECKPOINT-OPT] inflate_images=True: every
                # artifact-cache://<hash> reference is resolved back to a
                # full data URI from the content-addressed cache. Cache
                # misses become text placeholders so the resumed run
                # continues with the image elided rather than aborting.
                return ExecutionState.from_snapshot_dict(
                    cp.state_data, inflate_images=True
                )

        # Fallback to durable RunStore search.
        # [P0-1 FIX] Log the failure instead of swallowing it silently;
        # the caller needs to distinguish "checkpoint not found" from
        # "DB unreachable" -- the former raises FileNotFoundError, the
        # latter is logged at WARNING before the same exception is
        # raised so operators can trace the root cause in logs.
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
                return ExecutionState.from_snapshot_dict(
                    json.loads(row["state_dump_json"]),
                    inflate_images=True,
                )
        except Exception as db_err:
            logger.warning(
                "Durable-store fallback failed while loading checkpoint %s: %s",
                checkpoint_id,
                db_err,
            )

        raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found.")

    async def arestore_latest_state(self) -> Optional[ExecutionState]:
        """Reconstruct an ExecutionState from the latest available checkpoint."""
        await self.ainitialize()
        latest = await self.aload_latest_checkpoint()
        if latest:
            return await self.arestore_state(latest.id)
        return None


__all__ = ["StateCheckpoint", "CheckpointManager"]
