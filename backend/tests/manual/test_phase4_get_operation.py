# backend/tests/manual/test_phase4_get_operation.py
"""
Acceptance test for the new `get_operation` and `list_operations` tools.

The tools are tested standalone (no dispatcher, no LLM). Integration
with `SourceControlTool._ACTION_MAP` is tested separately.
"""

from __future__ import annotations

import asyncio
import sys

from integrations.source_control.domain.enums import OperationStatus
from integrations.source_control.operations.service import (
    reset_default_operation_service,
    get_default_operation_service,
)
from integrations.source_control.tools.base import ToolContext
from integrations.source_control.tools.get_operation import (
    GetOperationTool,
    ListOperationsTool,
)


def _ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def _fail(msg: str, failures: list[str]) -> None:
    failures.append(msg)
    print(f"[FAIL] {msg}")


async def main() -> int:
    failures: list[str] = []

    reset_default_operation_service()
    svc = get_default_operation_service()
    ctx = ToolContext(run_id="test-get-op")

    # --- 1. Create two operations for testing ------------------------
    op_push = await svc.create_operation(
        repository_id="owner/repo",
        kind="push_branch",
        source_branch="feature/x",
        target_branch="feature/x",
        workspace_id="ws-1",
    )
    op_pr = await svc.create_operation(
        repository_id="owner/repo",
        kind="create_change_request",
        source_branch="feature/x",
        target_branch="main",
        workspace_id="ws-1",
    )

    # --- 2. get_operation finds the push operation -------------------
    tool = GetOperationTool()
    r = await tool.run(ctx, operation_id=op_push.operation_id)
    if not r.success:
        _fail(f"get_operation(push) failed: {r.error_message}", failures)
    else:
        d = r.data
        if d["operation_id"] != op_push.operation_id:
            _fail(f"wrong operation_id: {d['operation_id']}", failures)
        elif d["kind"] != "push_branch":
            _fail(f"wrong kind: {d['kind']}", failures)
        elif d["status"] != "created":
            _fail(f"wrong status: {d['status']}", failures)
        elif d["source_branch"] != "feature/x":
            _fail(f"wrong source_branch: {d['source_branch']}", failures)
        elif d["target_branch"] != "feature/x":
            _fail(f"wrong target_branch: {d['target_branch']}", failures)
        else:
            _ok("get_operation(push): correct record")

    # --- 3. get_operation includes events ---------------------------
    if r.success:
        events = r.data.get("events", [])
        if not events:
            _fail("get_operation: no events returned", failures)
        elif events[0].get("event_type") != "operation_created":
            _fail(
                f"first event should be operation_created, got "
                f"{events[0].get('event_type')}",
                failures,
            )
        else:
            _ok(f"get_operation: {len(events)} events, first is operation_created")

    # --- 4. get_operation with include_events=False ----------------
    r_no_events = await tool.run(
        ctx, operation_id=op_push.operation_id, include_events=False
    )
    if r_no_events.success and r_no_events.data.get("events"):
        _fail("include_events=False should omit events", failures)
    elif r_no_events.success:
        _ok("get_operation: include_events=False omits events")

    # --- 5. get_operation on missing ID ----------------------------
    r_missing = await tool.run(ctx, operation_id="op_does_not_exist")
    if r_missing.success:
        _fail("get_operation accepted a missing ID", failures)
    elif r_missing.error_code != "NOT_FOUND":
        _fail(
            f"missing ID should be NOT_FOUND, got {r_missing.error_code}",
            failures,
        )
    else:
        _ok("get_operation(missing): NOT_FOUND")

    # --- 6. Transition push to review_required --------------------
    for status in (
        OperationStatus.REPOSITORY_RESOLVED,
        OperationStatus.WORKSPACE_READY,
        OperationStatus.EDITING,
        OperationStatus.REVIEW_REQUIRED,
    ):
        await svc.transition(
            op_push.operation_id, status, actor="test"
        )

    r_review = await tool.run(ctx, operation_id=op_push.operation_id)
    if r_review.success and r_review.data["status"] == "review_required":
        _ok("get_operation: reflects review_required status")
    else:
        _fail(
            f"expected review_required, got "
            f"{r_review.data.get('status') if r_review.success else 'error'}",
            failures,
        )

    # --- 7. list_operations returns both ---------------------------
    list_tool = ListOperationsTool()
    r_list = await list_tool.run(ctx)
    if not r_list.success:
        _fail(f"list_operations failed: {r_list.error_message}", failures)
    elif r_list.data["count"] < 2:
        _fail(
            f"list_operations returned {r_list.data['count']}, expected >=2",
            failures,
        )
    else:
        _ok(f"list_operations: {r_list.data['count']} operations")

    # --- 8. list_operations with status filter --------------------
    r_filtered = await list_tool.run(ctx, status="review_required")
    if not r_filtered.success:
        _fail(f"filtered list failed: {r_filtered.error_message}", failures)
    elif r_filtered.data["count"] != 1:
        _fail(
            f"status filter returned {r_filtered.data['count']}, expected 1",
            failures,
        )
    elif r_filtered.data["operations"][0]["operation_id"] != op_push.operation_id:
        _fail("status filter wrong operation", failures)
    else:
        _ok("list_operations: status filter works")

    # --- 9. list_operations with kind filter ----------------------
    r_kind = await list_tool.run(ctx, kind="create_change_request")
    if not r_kind.success:
        _fail(f"kind filter failed: {r_kind.error_message}", failures)
    elif r_kind.data["count"] != 1:
        _fail(
            f"kind filter returned {r_kind.data['count']}, expected 1",
            failures,
        )
    elif r_kind.data["operations"][0]["operation_id"] != op_pr.operation_id:
        _fail("kind filter wrong operation", failures)
    else:
        _ok("list_operations: kind filter works")

    # --- 10. list_operations with workspace filter -----------------
    r_ws = await list_tool.run(ctx, workspace_id="ws-1")
    if not r_ws.success:
        _fail(f"workspace filter failed: {r_ws.error_message}", failures)
    elif r_ws.data["count"] != 2:
        _fail(
            f"workspace filter returned {r_ws.data['count']}, expected 2",
            failures,
        )
    else:
        _ok("list_operations: workspace filter works")

    # --- 11. list_operations with bad limit -----------------------
    r_bad = await list_tool.run(ctx, limit=0)
    if r_bad.success or r_bad.error_code != "VALIDATION_ERROR":
        _fail(
            f"limit=0 should be VALIDATION_ERROR, got {r_bad.error_code}",
            failures,
        )
    else:
        _ok("list_operations: rejects invalid limit")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print("PHASE 4 (GET_OPERATION) ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
