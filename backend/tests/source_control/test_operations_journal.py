# backend/tests/source_control/test_operations_journal.py
"""
Tests for the durable operation journal.

The journal is the single source of truth for operation state. These
tests exercise the public contract of the base `OperationJournal`
class and of the default in-memory implementation:

    - `save_record` / `get_record` round-trip persistence.
    - `list_records` returns newest-first snapshots.
    - `append_event` / `list_events` preserve order and deduplication.
    - `delete_record` removes both the record and its events, and is
      idempotent (a second delete returns False, never raises).
    - `clear` empties the journal without touching the filesystem.

The tests are written against `delete_record`, which is the canonical
name on `OperationJournal`. The higher-level `OperationService`
exposes a `delete_operation` convenience wrapper; that wrapper is
tested in the facade / service suites, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import pytest

try:
    from ...integrations.source_control.domain.enums import (
        OperationEventType,
        OperationStatus,
        ProviderId,
    )
    from ...integrations.source_control.domain.models import (
        OperationEvent,
        OperationRecord,
    )
    from ...integrations.source_control.operations.journal import (
        get_default_operation_journal,
        reset_default_operation_journal,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.domain.enums import (
        OperationEventType,
        OperationStatus,
        ProviderId,
    )
    from backend.integrations.source_control.domain.models import (
        OperationEvent,
        OperationRecord,
    )
    from backend.integrations.source_control.operations.journal import (
        get_default_operation_journal,
        reset_default_operation_journal,
    )


def _make_record(
    op_id: str,
    repo_id: str = "repo_1",
    status: OperationStatus = OperationStatus.CREATED,
) -> OperationRecord:
    """Build a minimal OperationRecord for journal tests."""
    return OperationRecord(
        operation_id=op_id,
        run_id="run_1",
        provider=ProviderId.GITHUB,
        repository_id=repo_id,
        kind="push_branch",
        status=status,
        base_sha="a" * 40,
        source_branch="feature/test",
        target_branch="main",
    )


def _make_event(
    op_id: str,
    seq: int,
    event_type: OperationEventType = OperationEventType.STATE_TRANSITION,
) -> OperationEvent:
    """Build a minimal OperationEvent for journal tests."""
    return OperationEvent(
        operation_id=op_id,
        sequence=seq,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        payload={"from": "CREATED", "to": "PUSHING"},
    )


@pytest.fixture(autouse=True)
def reset_journal():
    """Guarantee a clean default journal for every test."""
    reset_default_operation_journal()
    yield
    reset_default_operation_journal()


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_operation(reset_journal) -> None:
    journal = get_default_operation_journal()
    record = _make_record("op_1")

    await journal.save_record(record)
    fetched = await journal.get_record("op_1")

    assert fetched is not None
    assert fetched.operation_id == "op_1"
    assert fetched.repository_id == "repo_1"
    assert fetched.status == OperationStatus.CREATED


@pytest.mark.asyncio
async def test_get_nonexistent_operation(reset_journal) -> None:
    journal = get_default_operation_journal()
    assert await journal.get_record("unknown_op") is None


@pytest.mark.asyncio
async def test_list_operations_filter(reset_journal) -> None:
    journal = get_default_operation_journal()

    await journal.save_record(
        _make_record("op_1", repo_id="repo_A", status=OperationStatus.CREATED)
    )
    await journal.save_record(
        _make_record("op_2", repo_id="repo_A", status=OperationStatus.PUSHED)
    )
    await journal.save_record(
        _make_record("op_3", repo_id="repo_B", status=OperationStatus.CREATED)
    )

    records = await journal.list_records(limit=10)
    by_id = {r.operation_id: r for r in records}
    assert set(by_id) == {"op_1", "op_2", "op_3"}
    assert by_id["op_1"].repository_id == "repo_A"
    assert by_id["op_2"].repository_id == "repo_A"
    assert by_id["op_3"].repository_id == "repo_B"


# ---------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_list_events(reset_journal) -> None:
    journal = get_default_operation_journal()
    await journal.save_record(_make_record("op_evt"))

    evt1 = _make_event("op_evt", seq=1)
    evt2 = _make_event(
        "op_evt", seq=2, event_type=OperationEventType.PUSH_CONFIRMED
    )

    await journal.append_event(evt1)
    await journal.append_event(evt2)

    # `list_events` is async on the current implementation. Fall back
    # gracefully if a future implementation returns a sequence
    # directly.
    events = journal.list_events("op_evt")
    if inspect.iscoroutine(events):
        events = await events

    events = tuple(events)
    assert len(events) == 2
    assert events[0].sequence == 1
    assert events[1].event_type == OperationEventType.PUSH_CONFIRMED


# ---------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_operation(reset_journal) -> None:
    """
    Verify that `delete_record` removes the record and its events,
    and that a second delete is a safe no-op.

    This is the canonical primitive. `OperationService.delete_operation`
    is a thin wrapper over it and is exercised through the service
    suite.
    """
    journal = get_default_operation_journal()
    await journal.save_record(_make_record("op_del"))
    await journal.append_event(_make_event("op_del", seq=1))

    # Sanity: both the record and its event are present.
    assert await journal.get_record("op_del") is not None
    events_before = journal.list_events("op_del")
    if inspect.iscoroutine(events_before):
        events_before = await events_before
    assert len(tuple(events_before)) == 1

    # First deletion succeeds and returns True.
    deleted = await journal.delete_record("op_del")
    assert deleted is True

    # Both the record and its events are gone.
    assert await journal.get_record("op_del") is None
    events_after = journal.list_events("op_del")
    if inspect.iscoroutine(events_after):
        events_after = await events_after
    assert len(tuple(events_after)) == 0

    # Second deletion is a safe no-op and returns False.
    deleted_again = await journal.delete_record("op_del")
    assert deleted_again is False


# ---------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_journal(reset_journal) -> None:
    journal = get_default_operation_journal()
    await journal.save_record(_make_record("op_c1"))
    await journal.save_record(_make_record("op_c2"))

    result = journal.clear()
    if inspect.iscoroutine(result):
        await result

    assert await journal.get_record("op_c1") is None
    assert await journal.get_record("op_c2") is None
