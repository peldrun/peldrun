from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
import pytest

# Resilient imports for operations subsystem and domain errors
try:
    from ...integrations.source_control.errors import (
        ConflictError,
        NotFoundError,
        SourceControlError,
        ValidationError,
    )
    from ...integrations.source_control.operations.journal import OperationJournal
    from ...integrations.source_control.operations.models import (
        OperationEvent,
        OperationRecord,
        OperationState,
    )
    from ...integrations.source_control.operations.service import OperationService
except (ImportError, ValueError):
    from backend.integrations.source_control.errors import (
        ConflictError,
        NotFoundError,
        SourceControlError,
        ValidationError,
    )
    from backend.integrations.source_control.operations.journal import OperationJournal
    from backend.integrations.source_control.operations.models import (
        OperationEvent,
        OperationRecord,
        OperationState,
    )
    from backend.integrations.source_control.operations.service import OperationService


# ---------------------------------------------------------------------------
# 1. OperationJournal File Persistence & JSONL Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_saves_and_retrieves_record(tmp_path: Path) -> None:
    """Verifies that an OperationRecord is persisted to disk and retrieved intact."""
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = OperationJournal(journal_dir=journal_dir)

    record = OperationRecord(
        operation_id="op_persistence_001",
        operation_type="push_branch",
        provider="github",
        repository_id="repo_123",
        target_branch="feature/persist-test",
        current_state=OperationState.CREATED,
        workspace_id="ws_001",
    )

    await journal.save_record(record)

    # Retrieve from the same journal instance
    retrieved = await journal.get_record("op_persistence_001")
    assert retrieved is not None
    assert retrieved.operation_id == "op_persistence_001"
    assert retrieved.operation_type == "push_branch"
    assert retrieved.current_state == OperationState.CREATED
    assert retrieved.workspace_id == "ws_001"


@pytest.mark.asyncio
async def test_journal_persists_events_as_jsonl(tmp_path: Path) -> None:
    """Verifies that operation transition events are appended to readable JSONL files."""
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = OperationJournal(journal_dir=journal_dir)

    event = OperationEvent(
        event_id="evt_001",
        from_state=OperationState.CREATED,
        to_state=OperationState.WORKSPACE_READY,
        timestamp=datetime.utcnow(),
        metadata={"step": "workspace_setup", "agent": "coder"},
    )

    await journal.append_event("op_jsonl_test", event)

    # Inspect the raw JSONL file on filesystem
    jsonl_files = list(journal_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1

    target_file = None
    for f in jsonl_files:
        if "op_jsonl_test" in f.name or f.name == "operations.jsonl" or f.name == "events.jsonl":
            target_file = f
            break

    assert target_file is not None
    lines = target_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1

    last_entry = json.loads(lines[-1])
    assert "evt_001" in json.dumps(last_entry)
    assert last_entry.get("to_state") == OperationState.WORKSPACE_READY.value or last_entry.get("state") == OperationState.WORKSPACE_READY.value


# ---------------------------------------------------------------------------
# 2. State Machine Transitions Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operation_service_lifecycle_transitions(
    operation_service: OperationService,
) -> None:
    """Ensures valid progression through the entire operation state machine lifecycle."""
    op = await operation_service.create_operation(
        operation_type="push_branch",
        provider="github",
        repository_id="repo_999",
        target_branch="feature/full-lifecycle",
        workspace_id="ws_101",
    )
    assert op.current_state == OperationState.CREATED

    # 1. Transition to WORKSPACE_READY
    op = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.WORKSPACE_READY,
        metadata={"workspace_path": "/tmp/ws_101"},
    )
    assert op.current_state == OperationState.WORKSPACE_READY

    # 2. Transition to REVIEW_REQUIRED
    op = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.REVIEW_REQUIRED,
        metadata={"diff_sha256": "0" * 64, "base_sha": "a" * 40},
    )
    assert op.current_state == OperationState.REVIEW_REQUIRED

    # 3. Transition to APPROVED
    op = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.APPROVED,
        metadata={"approved_by": "user_admin"},
    )
    assert op.current_state == OperationState.APPROVED

    # 4. Transition to PUSHED
    op = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.PUSHED,
        metadata={"pushed_sha": "b" * 40},
    )
    assert op.current_state == OperationState.PUSHED

    # 5. Transition to CHANGE_REQUEST_CREATED
    op = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.CHANGE_REQUEST_CREATED,
        metadata={"cr_url": "https://github.com/org/repo/pull/1"},
    )
    assert op.current_state == OperationState.CHANGE_REQUEST_CREATED
    assert len(op.events) >= 5


@pytest.mark.asyncio
async def test_invalid_state_transition_prevented(
    operation_service: OperationService,
) -> None:
    """Verifies that illegal jumps in the state machine are strictly rejected."""
    op = await operation_service.create_operation(
        operation_type="push_branch",
        provider="github",
        repository_id="repo_guard",
        target_branch="feature/guard",
    )
    assert op.current_state == OperationState.CREATED

    # Attempting to jump directly from CREATED to PUSHED must be disallowed
    with pytest.raises((ValidationError, ConflictError, SourceControlError)):
        await operation_service.transition_state(
            operation_id=op.operation_id,
            target_state=OperationState.PUSHED,
        )


# ---------------------------------------------------------------------------
# 3. Crash Recovery & Resumption Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_recovery_after_restart(tmp_path: Path) -> None:
    """Simulates abrupt process restart and verifies that pending review resumes from disk."""
    shared_journal_dir = tmp_path / "durable_journal"
    shared_journal_dir.mkdir(parents=True, exist_ok=True)

    # 1. Run initial session using first service instance
    journal_1 = OperationJournal(journal_dir=shared_journal_dir)
    service_1 = OperationService(journal=journal_1)

    initial_op = await service_1.create_operation(
        operation_type="push_branch",
        provider="github",
        repository_id="repo_crash_recovery",
        target_branch="feature/crash-resume",
        workspace_id="ws_crash_1",
    )
    await service_1.transition_state(
        operation_id=initial_op.operation_id,
        target_state=OperationState.WORKSPACE_READY,
    )
    await service_1.transition_state(
        operation_id=initial_op.operation_id,
        target_state=OperationState.REVIEW_REQUIRED,
        metadata={"diff_sha256": "f" * 64, "base_sha": "e" * 40},
    )

    # 2. Simulate process termination by dropping initial session references
    del service_1
    del journal_1

    # 3. Spin up fresh service instance pointing to the exact same storage directory
    journal_2 = OperationJournal(journal_dir=shared_journal_dir)
    service_2 = OperationService(journal=journal_2)

    recovered_op = await service_2.get_operation(initial_op.operation_id)
    assert recovered_op is not None
    assert recovered_op.operation_id == initial_op.operation_id
    assert recovered_op.current_state == OperationState.REVIEW_REQUIRED
    assert len(recovered_op.events) >= 2

    # 4. Resume workflow in the new session and progress to APPROVED
    resumed_op = await service_2.transition_state(
        operation_id=initial_op.operation_id,
        target_state=OperationState.APPROVED,
        metadata={"user": "reviewer"},
    )
    assert resumed_op.current_state == OperationState.APPROVED


# ---------------------------------------------------------------------------
# 4. Idempotency & Chronological Event Ordering Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_transition_behavior(
    operation_service: OperationService,
) -> None:
    """Verifies that re-applying the active state with identical metadata does not corrupt state."""
    op = await operation_service.create_operation(
        operation_type="push_branch",
        provider="github",
        repository_id="repo_idempotent",
        target_branch="feature/idempotent",
    )

    # Transition to WORKSPACE_READY
    op1 = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.WORKSPACE_READY,
        metadata={"key": "val"},
    )
    assert op1.current_state == OperationState.WORKSPACE_READY

    # Re-transition to the same state
    op2 = await operation_service.transition_state(
        operation_id=op.operation_id,
        target_state=OperationState.WORKSPACE_READY,
        metadata={"key": "val"},
    )
    assert op2.current_state == OperationState.WORKSPACE_READY


@pytest.mark.asyncio
async def test_chronological_event_ordering(
    operation_service: OperationService,
) -> None:
    """Ensures historical events maintain sequential chronological order."""
    op = await operation_service.create_operation(
        operation_type="push_branch",
        provider="github",
        repository_id="repo_events_order",
        target_branch="feature/order",
    )

    states = [
        OperationState.WORKSPACE_READY,
        OperationState.REVIEW_REQUIRED,
        OperationState.CANCELLED,
    ]
    for s in states:
        await operation_service.transition_state(
            operation_id=op.operation_id,
            target_state=s,
        )

    final_op = await operation_service.get_operation(op.operation_id)
    assert final_op is not None

    timestamps = [e.timestamp for e in final_op.events]
    for i in range(len(timestamps) - 1):
        assert timestamps[i] <= timestamps[i + 1]


@pytest.mark.asyncio
async def test_get_non_existent_operation_returns_none(
    operation_service: OperationService,
) -> None:
    """Verifies that querying an unknown operation ID returns None or raises NotFoundError cleanly."""
    res = await operation_service.get_operation("non_existent_op_99999")
    assert res is None
