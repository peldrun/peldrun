# test_bug3_async_offload.py
"""
Verifies that `get_workspace_by_chat` and `delete_workspace` no
longer block the event loop. Both used to call `subprocess.run` /
`shutil.rmtree` synchronously inside an async function; the fix
offloads them to worker threads via `asyncio.to_thread`.

Strategy:
  - Patch the blocking primitive to sleep 0.3s.
  - Start the endpoint in one coroutine.
  - Run an independent counter coroutine that yields every 5ms.
  - If offloaded: counter increments ~40+ times during the sleep.
  - If not offloaded: counter is 0-1 (the event loop is frozen).

Run from the project root:
    python test_bug3_async_offload.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path as _Path
from unittest.mock import patch

_BACKEND_ROOT = _Path(__file__).resolve().parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from backend.omweb.services.source_control_facade import SourceControlFacade
from integrations.source_control.domain.enums import ProviderId
from integrations.source_control.domain.models import (
    WorkspaceRef,
    WorkspaceStatus,
)


async def _count_during(coro, *, interval_s: float = 0.005):
    """Run `coro` and a counter loop concurrently. Return (result, count)."""
    counter = [0]
    stop = [False]

    async def count_loop():
        while not stop[0]:
            counter[0] += 1
            await asyncio.sleep(interval_s)

    counter_task = asyncio.create_task(count_loop())
    try:
        result = await coro
    finally:
        stop[0] = True
        await counter_task
    return result, counter[0]


class _NullWSM:
    """Workspace manager that returns None for every lookup."""
    def __init__(self) -> None:
        self._workspaces: dict = {}

    def get_workspace(self, workspace_id):
        return None

    def list_workspaces(self):
        return []

    def delete_workspace(self, workspace_id, force_delete_files=False):
        return None


async def test_get_workspace_by_chat_offloaded() -> None:
    chat_id = "test-bug3-chat"
    backend_dir = _Path(__file__).parent / "backend"
    chat_dir = backend_dir / "storage" / "chats" / chat_id / "files"
    marker_dir = chat_dir / ".peldrun"

    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "workspace.json").write_text(
            json.dumps({
                "workspace_id": "ws-test",
                "repository_id": "test/repo",
                "current_branch": "main",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "is_dirty": False,
                "created_at": "2024-01-01T00:00:00+00:00",
            }),
            encoding="utf-8",
        )

        def slow_run(*args, **kwargs):
            time.sleep(0.3)
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="main\n", stderr="",
            )

        facade = SourceControlFacade(workspace_manager=_NullWSM())

        with patch("subprocess.run", slow_run):
            _, counter = await _count_during(
                facade.get_workspace_by_chat(chat_id)
            )

        assert counter > 10, (
            f"event loop was blocked: only {counter} increments during "
            f"a 300ms subprocess.run call; expected >10"
        )
        print(
            f"[PASS] get_workspace_by_chat: counter={counter} "
            f"(offloaded)"
        )

    finally:
        if chat_dir.exists():
            shutil.rmtree(chat_dir, ignore_errors=True)
        for d in [
            backend_dir / "storage" / "chats" / chat_id,
            backend_dir / "storage" / "chats",
            backend_dir / "storage",
        ]:
            try:
                d.rmdir()
            except OSError:
                break


async def test_delete_workspace_offloaded() -> None:
    tmpdir = tempfile.mkdtemp(prefix="peldrun-bug3-")
    workspace_path = _Path(tmpdir) / "ws"
    workspace_path.mkdir()
    for i in range(50):
        (workspace_path / f"file{i}.txt").write_text("x" * 1000)

    class _StubWSM:
        def __init__(self, path):
            self._path = path
            self._workspaces: dict = {}

        def get_workspace(self, workspace_id):
            return WorkspaceRef(
                workspace_id=workspace_id,
                repository_id="test/repo",
                provider=ProviderId.GITHUB,
                workspace_path=str(self._path),
                current_branch="main",
                base_sha="a" * 40,
                head_sha="b" * 40,
                is_worktree=False,
                is_clean=True,
                status=WorkspaceStatus.READY,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )

        def list_workspaces(self):
            return []

        def delete_workspace(self, workspace_id, force_delete_files=False):
            return None

    facade = SourceControlFacade(
        workspace_manager=_StubWSM(workspace_path),
    )

    original_rmtree = shutil.rmtree

    def slow_rmtree(path, *args, **kwargs):
        time.sleep(0.3)
        return original_rmtree(path, *args, **kwargs)

    try:
        with patch("shutil.rmtree", slow_rmtree):
            _, counter = await _count_during(
                facade.delete_workspace(
                    "ws-test-bug3", force_delete_files=True,
                )
            )

        assert counter > 10, (
            f"event loop was blocked: only {counter} increments during "
            f"a 300ms shutil.rmtree call; expected >10"
        )
        print(
            f"[PASS] delete_workspace: counter={counter} (offloaded)"
        )

    finally:
        original_rmtree(tmpdir, ignore_errors=True)


async def main():
    print("=" * 60)
    print("Bug 3 (async offload) Tests")
    print("=" * 60)
    await test_get_workspace_by_chat_offloaded()
    await test_delete_workspace_offloaded()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        import traceback
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
