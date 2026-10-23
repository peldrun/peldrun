# test_cleanup_orphaned_operations.py
"""
Verifies that `SourceControlFacade.cleanup_orphaned_operations`
inspects the FULL journal, not just the newest 100 operations.

The pre-fix behavior was capped at 100 because the method called
`OperationService.list_operations()` without a `limit` argument,
which falls back to `limit=100`. Any orphaned operation older than
the 100 newest was silently skipped.

Run from the project root:
    python test_cleanup_orphaned_operations.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path as _Path

_BACKEND_ROOT = _Path(__file__).resolve().parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from backend.omweb.services.source_control_facade import SourceControlFacade
from integrations.source_control.errors import ValidationError
from integrations.source_control.operations.journal import (
    InMemoryOperationJournal,
)
from integrations.source_control.operations.service import OperationService


class OrphanWorkspaceManager:
    """Every workspace lookup returns None; every delete is a no-op."""

    def __init__(self) -> None:
        self._workspaces: dict = {}

    def get_workspace(self, workspace_id):
        return None

    def list_workspaces(self):
        return []

    def delete_workspace(self, workspace_id, force_delete_files=False):
        return None


async def _build_orphaned_ops(count: int):
    journal = InMemoryOperationJournal()
    op_service = OperationService(journal=journal)
    for i in range(count):
        await op_service.create_operation(
            repository_id=f"test/repo{i:03d}",
            kind="push_branch",
            workspace_id=f"ws-{i:03d}",
        )
    return journal, op_service


async def test_cleanup_scans_more_than_100() -> None:
    TOTAL = 150
    journal, op_service = await _build_orphaned_ops(TOTAL)

    records = await journal.list_records(limit=10_000)
    assert len(records) == TOTAL, (
        f"setup error: expected {TOTAL} records, got {len(records)}"
    )

    facade = SourceControlFacade(
        operation_service=op_service,
        workspace_manager=OrphanWorkspaceManager(),
    )

    # dry_run must see ALL 150 candidates (pre-fix saw only 100).
    dry = await facade.cleanup_orphaned_operations(dry_run=True)
    assert dry["removed_count"] == TOTAL, (
        f"dry_run removed_count={dry['removed_count']} (expected {TOTAL}); "
        f"the 100-record cap is back"
    )
    assert dry["dry_run"] is True

    # Real sweep must remove all 150.
    real = await facade.cleanup_orphaned_operations(dry_run=False)
    assert real["removed_count"] == TOTAL, (
        f"real removed_count={real['removed_count']} (expected {TOTAL})"
    )
    assert real["failures"] == [], f"deletion failures: {real['failures']}"

    remaining = await journal.list_records(limit=10_000)
    assert len(remaining) == 0, (
        f"journal still has {len(remaining)} records after cleanup"
    )
    print(
        f"[PASS] scanned+removed {TOTAL} orphaned operations "
        f"(the pre-fix cap was 100)"
    )


async def test_limit_parameter_honored() -> None:
    journal, op_service = await _build_orphaned_ops(20)
    facade = SourceControlFacade(
        operation_service=op_service,
        workspace_manager=OrphanWorkspaceManager(),
    )
    result = await facade.cleanup_orphaned_operations(dry_run=True, limit=5)
    assert result["removed_count"] == 5, (
        f"expected 5 with limit=5, got {result['removed_count']}"
    )
    print("[PASS] limit=5 honored: 5 candidates scanned")


async def test_dry_run_does_not_mutate() -> None:
    journal, op_service = await _build_orphaned_ops(3)
    facade = SourceControlFacade(
        operation_service=op_service,
        workspace_manager=OrphanWorkspaceManager(),
    )
    result = await facade.cleanup_orphaned_operations(dry_run=True)
    assert result["removed_count"] == 3
    remaining = await journal.list_records(limit=100)
    assert len(remaining) == 3, (
        f"dry_run mutated the journal: {len(remaining)} records left"
    )
    print("[PASS] dry_run does not mutate the journal")


async def test_invalid_limit_raises() -> None:
    _, op_service = await _build_orphaned_ops(0)
    facade = SourceControlFacade(
        operation_service=op_service,
        workspace_manager=OrphanWorkspaceManager(),
    )
    try:
        await facade.cleanup_orphaned_operations(dry_run=True, limit=0)
    except ValidationError as exc:
        print(f"[PASS] invalid limit rejected: {type(exc).__name__}")
        return
    raise AssertionError("limit=0 did not raise ValidationError")


async def main():
    print("=" * 60)
    print("cleanup_orphaned_operations Tests (100-cap fix)")
    print("=" * 60)
    await test_cleanup_scans_more_than_100()
    await test_limit_parameter_honored()
    await test_dry_run_does_not_mutate()
    await test_invalid_limit_raises()
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
