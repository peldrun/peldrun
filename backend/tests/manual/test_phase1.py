# backend/tests/manual/test_phase1.py
"""
Manual acceptance test for Phase 1.

Exercises the critical path that was broken before Phase 1:
    1. Path policy accepts chat-scoped workspaces under `storage/`.
    2. `create_workspace` with `target_path` keeps the repository in
       the chat's `files/` directory.
    3. `_materialize` bootstraps a born HEAD when the repository is
       created from scratch (no `"0"*40` placeholder).
    4. `commit_manager.create_commit` succeeds on that workspace.
    5. `diff_engine.compute_diff` returns a non-empty diff after a
       file is added.

Run with:
    PYTHONPATH=backend python backend/tests/manual/test_phase1.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from integrations.source_control.domain.enums import ProviderId
from integrations.source_control.domain.models import RepositoryRef
from integrations.source_control.git.commit import (
    CommitOptions,
    get_default_commit_manager,
)
from integrations.source_control.git.diff import get_default_diff_engine
from integrations.source_control.git.path_policy import (
    get_default_path_policy,
    reset_default_path_policy,
)
from integrations.source_control.git.workspace import (
    get_default_workspace_manager,
)


FAILURES: list[str] = []


def _fail(reason: str) -> None:
    FAILURES.append(reason)
    print(f"[FAIL] {reason}")


def _ok(reason: str) -> None:
    print(f"[ OK ] {reason}")


async def main() -> int:
    # --- 1. Path policy picks up extra_roots -----------------------------
    reset_default_path_policy()
    policy = get_default_path_policy()
    print(f"[INFO] primary root:  {policy.root}")
    print(f"[INFO] extra roots:   {policy.extra_roots}")

    if not policy.extra_roots:
        _fail("path policy has no extra_roots; File 5 did not take effect")
        return 1
    _ok("path policy exposes extra_roots")

    # --- 2. Resolve a chat-scoped project_dir ----------------------------
    storage_root = policy.extra_roots[0]
    chat_id = f"test_{uuid.uuid4().hex[:8]}"
    project_dir = storage_root / "chats" / chat_id / "files"
    project_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] project_dir:  {project_dir}")

    try:
        resolved = policy.resolve_within_root(project_dir, must_exist=True)
    except Exception as exc:
        _fail(f"policy rejected project_dir: {type(exc).__name__}: {exc}")
        return 1
    if resolved != project_dir.resolve():
        _fail(f"resolved path mismatch: {resolved} != {project_dir.resolve()}")
        return 1
    _ok("policy accepts chat-scoped project_dir")

    # --- 3. Create a workspace with target_path = project_dir -----------
    manager = get_default_workspace_manager()
    fake_repo = RepositoryRef(
        provider=ProviderId.GITHUB,
        account_id="test",
        repository_id="test/repo",
        namespace="test",
        name="repo",
        clone_url="",             # forces `_init_empty_repo`
        web_url="",
        default_branch="main",
        is_private=False,
    )

    try:
        ws = await manager.create_workspace(
            repo=fake_repo,
            provider=ProviderId.GITHUB,
            branch_name="phase1-test-branch",
            target_path=project_dir,
            use_worktree=True,
        )
    except Exception as exc:
        _fail(f"create_workspace raised: {type(exc).__name__}: {exc}")
        return 1

    print(f"[INFO] workspace_id: {ws.workspace_id}")
    print(f"[INFO] workspace_path: {ws.workspace_path}")
    print(f"[INFO] base_sha: {ws.base_sha}")
    print(f"[INFO] head_sha: {ws.head_sha}")

    if Path(ws.workspace_path) != project_dir.resolve():
        _fail(
            f"workspace_path {ws.workspace_path} != project_dir "
            f"{project_dir.resolve()}"
        )
    else:
        _ok("workspace lives in project_dir")

    if not ws.base_sha or ws.base_sha == "0" * 40:
        _fail(f"base_sha is placeholder or empty: {ws.base_sha!r}")
    else:
        _ok(f"base_sha is a real commit SHA ({len(ws.base_sha)} chars)")

    if len(ws.base_sha) != 40:
        _fail(f"base_sha length is {len(ws.base_sha)}, expected 40 (SHA-1)")
    else:
        _ok("base_sha is SHA-1 (40 chars)")

    # --- 4. Add a file and commit ---------------------------------------
    (project_dir / "README.md").write_text("# Phase 1 test\n")
    commit_mgr = get_default_commit_manager()
    try:
        commit = await commit_mgr.create_commit(
            workspace_id=ws.workspace_id,
            message="Test: add README",
            options=CommitOptions(stage_all=True, allow_empty=False),
            workspace_path=ws.workspace_path,
        )
    except Exception as exc:
        _fail(f"create_commit raised: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        for attr in ("details", "code", "message", "args",
                     "command", "returncode", "stderr", "stdout"):
            if hasattr(exc, attr):
                print(f"[DEBUG] {attr}: {getattr(exc, attr)!r}")
        print(f"[DEBUG] workspace kept at: {project_dir}")
        return 1

    print(f"[INFO] commit sha: {commit.sha}")
    _ok("create_commit succeeded")

    # --- 5. Compute diff against the original base_sha ------------------
    diff_mgr = get_default_diff_engine()
    try:
        diff = await diff_mgr.compute_diff(
            workspace_id=ws.workspace_id,
            base_sha=ws.base_sha,
            include_patch=True,
            workspace_path=ws.workspace_path,
        )
    except Exception as exc:
        _fail(f"compute_diff raised: {type(exc).__name__}: {exc}")
        return 1

    print(f"[INFO] diff file count: {len(diff.files)}")
    print(f"[INFO] diff sha256:     {diff.sha256()}")
    for f in diff.files:
        print(f"[INFO]   {f.change_type.value:10s} {f.path}")

    if not diff.files:
        _fail("diff is empty after adding a file")
    else:
        _ok(f"diff contains {len(diff.files)} file(s)")

    # --- 6. Cleanup -----------------------------------------------------
    try:
        await manager.delete_workspace(ws.workspace_id)
    except Exception as exc:
        print(f"[WARN] cleanup raised: {type(exc).__name__}: {exc}")
    else:
        _ok("workspace deleted")

    # --- Summary --------------------------------------------------------
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} FAILURE(S):")
        for reason in FAILURES:
            print(f"   - {reason}")
        return 1

    print("✅ PHASE 1 ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
