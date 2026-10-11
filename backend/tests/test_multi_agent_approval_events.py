# backend/tests/test_multi_agent_approval_events.py
"""
Unit and integration tests for PELDRUN Native Multi-Agent PR 5:
1. Durable Approval Gate blocks child execution until human decision.
2. Operator rejection terminates subtask cleanly without running child.
3. Operator edit updates child instruction prompt.
4. Lineage events (DELEGATION_REQUESTED, APPROVAL_*, CHILD_RUN_*) are emitted.
5. Async event queue draining prevents event loss on completion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import uuid
import pytest

from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask, DelegationStatus
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.runtime.contract import (
    AgentSpec,
    DelegationBudget,
    RunRequest,
    RunResult,
    RunStatus,
    WorkspaceContext,
)
from peldrun.runtime.store import SqliteRunStore


@pytest.mark.asyncio
class TestDurableApprovalGate:
    """Verifies durable human approval workflow before delegation."""

    async def test_approval_rejection_blocks_child_execution(self, tmp_path: Path):
        db_path = tmp_path / "test_store.db"
        store = SqliteRunStore(db_path=db_path)
        emitter = EventEmitter(run_id="test_parent_appr")

        emitted_events: list[PeldrunEvent] = []
        emitter.subscribe_all(lambda ev: emitted_events.append(ev))

        child_executed = False

        async def fake_execute(req: RunRequest, event_listener=None) -> RunResult:
            nonlocal child_executed
            child_executed = True
            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Done",
            )

        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_p_appr",
            prompt="High level plan",
            agent_spec=AgentSpec(id="peldrun"),
            workspace=WorkspaceContext(workspace_id="ws_appr", root_path=str(tmp_path)),
            metadata={"require_approval": True},
        )

        coord = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(tmp_path),
            execute_fn=fake_execute,
            parent_request=parent_req,
            run_store=store,
        )

        task = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Deploy microservice cluster",
            approval_request_id="appr_reject_test",
        )

        # Simulate operator rejection in background task after 0.2s
        async def background_rejector():
            await asyncio.sleep(0.2)
            await store.resolve_human_request("appr_reject_test", answer={"decision": "reject"})

        asyncio.create_task(background_rejector())

        res = await coord.delegate(task)

        # Verify child never executed
        assert child_executed is False
        assert res.success is False
        assert res.status == DelegationStatus.CANCELLED
        assert res.error_code == "DELEGATION_REJECTED_BY_OPERATOR"

        # Verify events emitted
        event_types = [e.type for e in emitted_events]
        assert EventType.DELEGATION_REQUESTED in event_types
        assert EventType.APPROVAL_REQUIRED in event_types
        assert EventType.APPROVAL_RESOLVED in event_types
        assert EventType.CHILD_RUN_STARTED not in event_types

    async def test_approval_acceptance_with_edited_prompt(self, tmp_path: Path):
        db_path = tmp_path / "test_store_edit.db"
        store = SqliteRunStore(db_path=db_path)
        emitter = EventEmitter(run_id="test_parent_edit")

        executed_requests: list[RunRequest] = []

        async def fake_execute(req: RunRequest, event_listener=None) -> RunResult:
            executed_requests.append(req)
            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Created unit tests",
                deliverables=["test_out.py"],
            )

        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_p_edit",
            prompt="Supervise QA",
            agent_spec=AgentSpec(id="peldrun"),
            workspace=WorkspaceContext(workspace_id="ws_edit", root_path=str(tmp_path)),
            metadata={"require_approval": True},
        )

        coord = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(tmp_path),
            execute_fn=fake_execute,
            parent_request=parent_req,
            run_store=store,
        )

        task = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Original unvetted instruction",
            approval_request_id="appr_edit_test",
        )

        # Simulate operator editing the instruction and approving
        async def background_editor():
            await asyncio.sleep(0.2)
            await store.resolve_human_request(
                "appr_edit_test",
                answer={
                    "decision": "approve",
                    "instruction": "Strictly audited secure test suite",
                },
            )

        asyncio.create_task(background_editor())

        res = await coord.delegate(task)

        assert res.success is True
        assert res.status == DelegationStatus.COMPLETED
        assert len(executed_requests) == 1

        # The child request must carry the edited instruction
        assert "Strictly audited secure test suite" in executed_requests[0].prompt
        assert "Original unvetted instruction" not in executed_requests[0].prompt


@pytest.mark.asyncio
class TestMultiAgentLineageEvents:
    """Verifies that complete lineage events are emitted on parent emitter."""

    async def test_coordinator_emits_full_lifecycle_events(self, tmp_path: Path):
        emitter = EventEmitter(run_id="test_parent_events")
        received_events: list[PeldrunEvent] = []
        emitter.subscribe_all(lambda ev: received_events.append(ev))

        async def mock_execute(req: RunRequest, event_listener=None) -> RunResult:
            if event_listener:
                # Simulate a step event from child
                child_step_evt = PeldrunEvent(
                    run_id=str(req.run_id),
                    step=1,
                    type=EventType.STEP_START,
                    payload={"step": 1, "agent_name": "code_architect"},
                )
                if asyncio.iscoroutinefunction(event_listener):
                    await event_listener(child_step_evt)
                else:
                    event_listener(child_step_evt)

            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Success result",
            )

        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_events_root",
            prompt="Root task",
            agent_spec=AgentSpec(id="peldrun"),
            workspace=WorkspaceContext(workspace_id="ws_ev", root_path=str(tmp_path)),
        )

        coord = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(tmp_path),
            execute_fn=mock_execute,
            parent_request=parent_req,
        )

        task = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Execute task step",
        )

        res = await coord.delegate(task)
        assert res.success is True

        event_types = [e.type for e in received_events]
        assert EventType.DELEGATION_REQUESTED in event_types
        assert EventType.CHILD_RUN_STARTED in event_types
        assert EventType.CHILD_RUN_STEP in event_types
        assert EventType.CHILD_RUN_COMPLETED in event_types

        # Verify lineage headers in bridged events
        step_events = [e for e in received_events if e.type == EventType.CHILD_RUN_STEP]
        assert len(step_events) > 0
        assert step_events[0].metadata.get("parent_run_id") == str(parent_req.run_id)
        assert step_events[0].metadata.get("child_run_id") == str(res.child_run_id)
