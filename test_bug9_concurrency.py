# test_bug9_concurrency.py
"""
Standalone concurrency tests for the Bug 9 fix.

Verifies that:
    1. Concurrent `_emit_event` calls produce unique, ordered
       sequence numbers.
    2. Concurrent `transition` calls on the same operation cannot
       lose an update (the second transition must see the state
       left by the first).

Run from the project root:
    python test_bug9_concurrency.py
"""
from __future__ import annotations

import asyncio
import sys

from backend.integrations.source_control.operations.service import (
    OperationService,
)
from backend.integrations.source_control.operations.journal import (
    InMemoryOperationJournal,
)
from backend.integrations.source_control.domain.enums import (
    OperationEventType,
    OperationStatus,
)


async def test_concurrent_events() -> None:
    """Fire 100 concurrent events and verify uniqueness + order."""
    service = OperationService(journal=InMemoryOperationJournal())
    record = await service.create_operation(
        repository_id="test/repo",
        kind="push_branch",
    )
    op_id = record.operation_id

    async def emit_many(tag: int) -> None:
        for _ in range(10):
            await service._emit_event(
                operation_id=op_id,
                event_type=OperationEventType.STATE_TRANSITION,
                payload={"tag": tag},
            )

    # 10 coroutines x 10 events each = 100 concurrent emits.
    await asyncio.gather(*[emit_many(i) for i in range(10)])

    events = await service.list_events(op_id)
    sequences = [e.sequence for e in events]

    # The operation was created with one event, so we expect 101.
    assert len(sequences) == 101, (
        f"Expected 101 events (1 create + 100 emits), got {len(sequences)}"
    )
    assert len(set(sequences)) == len(sequences), (
        f"Duplicate sequences detected: {sequences}"
    )
    assert sequences == sorted(sequences), (
        f"Out-of-order sequences detected: {sequences}"
    )
    print(
        f"[PASS] test_concurrent_events: {len(sequences)} events, "
        f"unique and ordered (max_seq={max(sequences)})"
    )


class SlowJournal(InMemoryOperationJournal):
    """
    In-memory journal with artificial delays in get/save.

    The delay forces an `await` inside the critical section of
    `_transition_internal` / `_update_record`, which is exactly the
    condition under which a `threading.RLock` would fail to
    serialize coroutines (asyncio runs all coroutines in one OS
    thread, so an RLock is effectively re-entrant for every
    coroutine). A real disk-backed journal yields at every I/O,
    which is why this class exists: without it, the fast in-memory
    journal does not actually exercise the lost-update race.
    """

    async def get_record(self, operation_id):  # type: ignore[override]
        await asyncio.sleep(0.001)
        return await super().get_record(operation_id)

    async def save_record(self, record):  # type: ignore[override]
        await asyncio.sleep(0.001)
        return await super().save_record(record)

    async def append_event(self, event):  # type: ignore[override]
        await asyncio.sleep(0.001)
        return await super().append_event(event)


async def _run_lost_update_test(journal_factory, label: str) -> None:
    """Shared body for the lost-update tests."""
    service = OperationService(journal=journal_factory())
    record = await service.create_operation(
        repository_id="test/repo",
        kind="push_branch",
    )
    op_id = record.operation_id

    await service.transition(op_id, OperationStatus.REPOSITORY_RESOLVED)
    await service.transition(op_id, OperationStatus.WORKSPACE_READY)
    await service.transition(op_id, OperationStatus.EDITING)

    # REVIEW_REQUIRED and FAILED_FINAL are BOTH valid targets from
    # EDITING, but neither can be reached from the other in a
    # single step:
    #
    #   REVIEW_REQUIRED -> FAILED_FINAL     : NOT ALLOWED
    #   FAILED_FINAL (terminal) -> anything : NOT ALLOWED
    #
    # So exactly one transition must succeed, regardless of which
    # coroutine wins the race.
    results = await asyncio.gather(
        service.transition(op_id, OperationStatus.REVIEW_REQUIRED),
        service.transition(op_id, OperationStatus.FAILED_FINAL),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]

    # Diagnostic dump: shows the actual transition chain so that a
    # failure tells us exactly what went wrong.
    events = await service.list_events(op_id)
    transitions = [
        (
            e.sequence,
            e.payload.get("from_status"),
            e.payload.get("to_status"),
        )
        for e in events
        if e.event_type == OperationEventType.STATE_TRANSITION
    ]
    final = await service.get_operation(op_id)

    print(f"\n  [{label}] transition chain:")
    for seq, fr, to in transitions:
        print(f"    seq={seq:3d}  from={fr!r:22s} to={to!r}")
    print(f"  [{label}] final state: {final.status.value}")
    print(
        f"  [{label}] successes={len(successes)} "
        f"failures={len(errors)}"
    )

    assert len(successes) == 1, (
        f"[{label}] Expected exactly 1 success, got "
        f"{len(successes)}. A count of 2 means a LOST UPDATE: "
        f"both coroutines read the same EDITING state and both "
        f"succeeded, which means the operation lock did NOT "
        f"serialize them."
    )
    assert len(errors) == 1, (
        f"[{label}] Expected exactly 1 failure, got {len(errors)}. "
        f"Errors: {[type(e).__name__ for e in errors]}"
    )

    # Verify the transition chain: exactly one event must originate
    # from the EDITING state (the winner). More than one means both
    # coroutines wrote from EDITING, which is the definitive
    # signature of a lost update.
    editing_seq = next(
        (s for s, _fr, to in transitions if to == "editing"),
        None,
    )
    assert editing_seq is not None, (
        f"[{label}] EDITING transition not found in chain."
    )
    post_editing = [
        t for t in transitions if t[0] > editing_seq
    ]
    assert len(post_editing) == 1, (
        f"[{label}] Expected 1 transition after EDITING, got "
        f"{len(post_editing)}. Multiple transitions from EDITING "
        f"= LOST UPDATE (both coroutines read the same state)."
    )
    assert post_editing[0][1] == "editing", (
        f"[{label}] Winner's event has from_status="
        f"{post_editing[0][1]!r}, expected 'editing'."
    )

    print(f"[PASS] {label}: 1 success + 1 rejected, "
          f"final={final.status.value}")


async def test_lost_update() -> None:
    """
    Verify that concurrent transitions do not lose updates.

    Runs twice: once with the fast in-memory journal (which does
    NOT yield inside the critical section) and once with a slow
    journal that yields on every read/write. The slow variant is
    the definitive detector for a `threading.RLock`-based lost
    update, because a `threading.RLock` cannot exclude coroutines
    across `await` boundaries.
    """
    await _run_lost_update_test(
        InMemoryOperationJournal, "fast-journal"
    )
    await _run_lost_update_test(
        SlowJournal, "slow-journal"
    )


async def main() -> None:
    print("=" * 60)
    print("Bug 9 Concurrency Tests")
    print("=" * 60)
    await test_concurrent_events()
    await test_lost_update()
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
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
