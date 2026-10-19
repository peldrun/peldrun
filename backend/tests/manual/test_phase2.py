# backend/tests/manual/test_phase2.py
"""
Manual acceptance test for Phase 2 — request_approval.

Exercises the critical path that Phase 2 fixes:
    1. `request_approval` creates an operation whose source_branch
       matches the workspace's current branch.
    2. The operation's base_sha is the actual Git SHA-1 (40 chars),
       not a transformed SHA-256 (64 chars).
    3. The operation's diff_sha256 is a 64-char SHA-256 of the diff.
    4. The approval ticket's target_branch matches the input.
    5. `ApprovalGate.grant` + `ApprovalGate.consume` succeed, with
       every bound field matching the record.
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
from integrations.source_control.git.path_policy import (
    get_default_path_policy,
    reset_default_path_policy,
)
from integrations.source_control.git.workspace import (
    get_default_workspace_manager,
)
from integrations.source_control.tools.base import ToolContext
from integrations.source_control.tools.request_approval import (
    RequestApprovalTool,
)
from integrations.source_control.security.approval import (
    get_default_approval_gate,
)
from integrations.source_control.operations.service import (
    get_default_operation_service,
)


FAILURES: list[str] = []


def _fail(reason: str) -> None:
    FAILURES.append(reason)
    print(f"[FAIL] {reason}")


def _ok(reason: str) -> None:
    print(f"[ OK ] {reason}")


async def main() -> int:
    # --- 1. Path policy + workspace ------------------------------------
    reset_default_path_policy()
    policy = get_default_path_policy()
    if not policy.extra_roots:
        _fail("path policy has no extra_roots")
        return 1
    storage_root = policy.extra_roots[0]
    chat_id = f"test_{uuid.uuid4().hex[:8]}"
    project_dir = storage_root / "chats" / chat_id / "files"
    project_dir.mkdir(parents=True, exist_ok=True)

    manager = get_default_workspace_manager()
    fake_repo = RepositoryRef(
        provider=ProviderId.GITHUB,
        account_id="test",
        repository_id="test/repo",
        namespace="test",
        name="repo",
        clone_url="",
        web_url="",
        default_branch="main",
        is_private=False,
    )

    ws = await manager.create_workspace(
        repo=fake_repo,
        provider=ProviderId.GITHUB,
        branch_name="phase2-test-branch",
        target_path=project_dir,
        use_worktree=True,
    )
    _ok(f"workspace created: {ws.workspace_id} (branch={ws.current_branch})")

    # --- 2. Add a file + commit ----------------------------------------
    (project_dir / "README.md").write_text("# Phase 2 test\n")
    commit_mgr = get_default_commit_manager()
    await commit_mgr.create_commit(
        workspace_id=ws.workspace_id,
        message="Test: add README",
        options=CommitOptions(stage_all=True, allow_empty=False),
        workspace_path=ws.workspace_path,
    )
    _ok("commit succeeded")

    # --- 3. request_approval -------------------------------------------
    ctx = ToolContext(run_id=str(uuid.uuid4()))
    tool = RequestApprovalTool()
    target_branch = "feature/test-phase2"

    result = await tool.run(
        ctx,
        workspace_id=ws.workspace_id,
        operation="push_branch",
        target_branch=target_branch,
    )

    if not result.success:
        _fail(
            f"request_approval failed: "
            f"{result.error_code}: {result.error_message}"
        )
        return 1
    _ok("request_approval succeeded")

    data = result.data
    operation_id = data["operation_id"]
    approval_id = data["approval_id"]

    # --- 4. Verify the operation record --------------------------------
    op_service = get_default_operation_service()
    record = await op_service.get_operation(operation_id)

    print(f"[INFO] status:        {record.status.value}")
    print(f"[INFO] source_branch: {record.source_branch}")
    print(f"[INFO] target_branch: {record.target_branch}")
    print(f"[INFO] base_sha:      {record.base_sha} ({len(record.base_sha)} chars)")
    print(f"[INFO] diff_sha256:   {record.diff_sha256} ({len(record.diff_sha256)} chars)")

    if record.status.value != "review_required":
        _fail(f"status is {record.status.value!r}, expected review_required")
    else:
        _ok("status is review_required")

    if record.source_branch != ws.current_branch:
        _fail(
            f"source_branch {record.source_branch!r} != "
            f"ws.current_branch {ws.current_branch!r}"
        )
    else:
        _ok(f"source_branch matches workspace branch ({ws.current_branch})")

    if record.target_branch != target_branch:
        _fail(f"target_branch {record.target_branch!r} != {target_branch!r}")
    else:
        _ok(f"target_branch matches input ({target_branch})")

    if not record.base_sha or len(record.base_sha) != 40:
        _fail(f"base_sha is not SHA-1 (40 chars): {record.base_sha!r}")
    else:
        _ok("base_sha is SHA-1 (40 chars)")

    if not record.diff_sha256 or len(record.diff_sha256) != 64:
        _fail(f"diff_sha256 is not 64 chars: {record.diff_sha256!r}")
    else:
        _ok("diff_sha256 is SHA-256 (64 chars)")

    # --- 5. Verify the ticket ------------------------------------------
    gate = get_default_approval_gate()
    ticket = gate.get_ticket(approval_id)
    if ticket is None:
        _fail(f"ticket {approval_id} not found in gate")
        return 1
    _ok("ticket exists in gate")

    if ticket.target_branch != target_branch:
        _fail(
            f"ticket.target_branch {ticket.target_branch!r} != "
            f"{target_branch!r}"
        )
    else:
        _ok("ticket.target_branch matches input")

    # --- 6. grant + consume --------------------------------------------
    try:
        gate.grant(approval_id, actor="test")
        _ok("ticket granted")
    except Exception as exc:
        _fail(f"grant raised: {type(exc).__name__}: {exc}")
        return 1

    try:
        gate.consume(
            approval_id,
            expected_operation_id=record.operation_id,
            expected_repository_id=record.repository_id,
            expected_operation=record.kind,
            expected_base_sha=record.base_sha,
            expected_diff_sha256=record.diff_sha256,
            expected_target_branch=record.target_branch,
            actor="test",
        )
        _ok("ticket consumed with matching bound fields")
    except Exception as exc:
        _fail(f"consume raised: {type(exc).__name__}: {exc}")
        return 1

    # --- 7. Cleanup ----------------------------------------------------
    try:
        await manager.delete_workspace(ws.workspace_id)
    except Exception as exc:
        print(f"[WARN] cleanup raised: {type(exc).__name__}: {exc}")

    # --- Summary -------------------------------------------------------
    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} FAILURE(S):")
        for reason in FAILURES:
            print(f"   - {reason}")
        return 1
    print("✅ PHASE 2 ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
