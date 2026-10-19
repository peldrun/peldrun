# backend/tests/manual/test_phase4_status.py
"""Acceptance test for File 15 — status reader after bytes->str fix."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from integrations.source_control.domain.enums import ProviderId
from integrations.source_control.domain.models import RepositoryRef
from integrations.source_control.git.path_policy import (
    get_default_path_policy,
    reset_default_path_policy,
)
from integrations.source_control.git.status import (
    GitStatusReader,
    get_default_status_reader,
)
from integrations.source_control.git.workspace import (
    get_default_workspace_manager,
)


def _ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


async def main() -> int:
    reset_default_path_policy()
    policy = get_default_path_policy()
    storage_root = policy.extra_roots[0]
    chat_id = f"test_{uuid.uuid4().hex[:8]}"
    project_dir = storage_root / "chats" / chat_id / "files"
    project_dir.mkdir(parents=True, exist_ok=True)

    manager = get_default_workspace_manager()
    fake_repo = RepositoryRef(
        provider=ProviderId.GITHUB,
        account_id="test",
        repository_id="test/status",
        namespace="test",
        name="status",
        clone_url="",
        web_url="",
        default_branch="main",
        is_private=False,
    )

    ws = await manager.create_workspace(
        repo=fake_repo,
        provider=ProviderId.GITHUB,
        branch_name="status-test",
        target_path=project_dir,
        use_worktree=True,
    )
    _ok(f"workspace: {ws.workspace_id}")

    reader = get_default_status_reader()

    # --- 1. Fresh workspace must be clean ---------------------------
    clean = await reader.is_clean(ws.workspace_path)
    assert clean is True, f"fresh workspace reported dirty: {clean}"
    _ok("is_clean: fresh workspace is clean")

    # --- 2. Add a file: must become dirty ---------------------------
    (project_dir / "README.md").write_text("# status test\n")
    clean = await reader.is_clean(ws.workspace_path)
    assert clean is False, f"workspace with new file reported clean"
    _ok("is_clean: workspace with new file is dirty")

    # --- 3. Full read_status returns untracked file -----------------
    status = await reader.read_status(ws.workspace_path)
    print(f"[INFO] status.is_clean={status.is_clean}, "
          f"changed_paths={status.changed_paths}")
    assert status.is_clean is False
    assert "README.md" in status.changed_paths, (
        f"README.md not in {status.changed_paths}"
    )
    _ok("read_status: README.md reported as changed")

    # --- 4. read_untracked ------------------------------------------
    untracked = await reader.read_untracked(ws.workspace_path)
    assert "README.md" in untracked, f"untracked={untracked}"
    _ok(f"read_untracked: {list(untracked)}")

    # --- 5. read_conflicts: no conflicts on healthy workspace -------
    conflicts = await reader.read_conflicts(ws.workspace_path)
    assert conflicts == (), f"unexpected conflicts: {conflicts}"
    _ok("read_conflicts: empty (healthy workspace)")

    # --- 6. read_modified: nothing tracked yet ----------------------
    modified = await reader.read_modified(ws.workspace_path)
    assert modified == (), f"unexpected modified files: {modified}"
    _ok("read_modified: empty (file is untracked, not modified)")

    # --- 7. read_porcelain_entries ----------------------------------
    entries = await reader.read_porcelain_entries(ws.workspace_path)
    assert len(entries) >= 1, f"no porcelain entries: {entries}"
    assert any(e.path == "README.md" for e in entries)
    _ok(f"read_porcelain_entries: {len(entries)} entries, README.md found")

    # --- Cleanup ----------------------------------------------------
    await manager.delete_workspace(ws.workspace_id)
    _ok("workspace deleted")

    print()
    print("FILE 15 ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
