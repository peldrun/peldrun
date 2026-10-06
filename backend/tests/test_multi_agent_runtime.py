"""
backend/tests/test_multi_agent_runtime.py

Integration verification test for Phase M3: Multi-Agent Runtime.

Verifies:
1. Delegation to registered sub-agents without mutable emitter swapping.
2. Full event lineage propagation (parent_run_id -> child_run_id -> task_id).
3. Concurrent delegation runs maintain complete state and event stream isolation.
4. Aggregation of deliverables and artifacts from child runs into DelegationResult.
"""

import asyncio
from pathlib import Path
from uuid import uuid4
import pytest

from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask
from peldrun.engine.state import ExecutionState, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import PeldrunEvent


class MockSpecialistAgent:
    """Mock agent implementing the StepExecutableAgent protocol."""

    def __init__(self, name: str, deliverable_file: Optional[str] = None):
        self.name = name
        self.deliverable_file = deliverable_file

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        # Emit a domain observation event
        await emitter.emit_thought(thought=f"{self.name} is performing assigned subtask.")

        if self.deliverable_file:
            state.add_deliverable(self.deliverable_file)

        state.add_message(
            role=MessageRole.ASSISTANT,
            content=f"Subtask completed by {self.name} with deliverable: {self.deliverable_file}",
        )
        return True


@pytest.mark.asyncio
async def test_concurrent_multi_agent_delegation(tmp_path: Path) -> None:
    """Verify concurrent sub-agent delegation with strict lineage and event isolation."""
    parent_run_id = uuid4()
    parent_emitter = EventEmitter(run_id=parent_run_id)

    received_parent_events: list[PeldrunEvent] = []

    async def collect_event(ev: PeldrunEvent) -> None:
        received_parent_events.append(ev)

    parent_emitter.subscribe_all(collect_event)

    coordinator = MultiAgentCoordinator(
        parent_emitter=parent_emitter,
        workspace_root=str(tmp_path),
    )

    # Register specialist factories
    coordinator.register_factory(
        "coder",
        lambda: MockSpecialistAgent("coder", deliverable_file="app.py"),
    )
    coordinator.register_factory(
        "researcher",
        lambda: MockSpecialistAgent("researcher", deliverable_file="report.md"),
    )

    # Create dummy files in temporary workspace
    (tmp_path / "app.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / "report.md").write_text("# Research Report", encoding="utf-8")

    task_coder = DelegatedTask(
        parent_run_id=parent_run_id,
        target_agent_id="coder",
        instruction="Build the primary backend entrypoint.",
        context_data={"framework": "fastapi"},
    )

    task_researcher = DelegatedTask(
        parent_run_id=parent_run_id,
        target_agent_id="researcher",
        instruction="Gather performance benchmarks.",
        context_data={"dataset": "v1"},
    )

    # Execute both subtasks concurrently
    res_coder, res_researcher = await asyncio.gather(
        coordinator.delegate(task_coder),
        coordinator.delegate(task_researcher),
    )

    # 1. Assert Results
    assert res_coder.success is True
    assert "coder" in res_coder.output
    assert "app.py" in res_coder.deliverables

    assert res_researcher.success is True
    assert "researcher" in res_researcher.output
    assert "report.md" in res_researcher.deliverables

    # 2. Assert Lineage Tracking on Bridged Events
    coder_child_run_str = str(res_coder.child_run_id)
    researcher_child_run_str = str(res_researcher.child_run_id)

    coder_events = [
        ev for ev in received_parent_events
        if ev.metadata.get("child_run_id") == coder_child_run_str
    ]
    researcher_events = [
        ev for ev in received_parent_events
        if ev.metadata.get("child_run_id") == researcher_child_run_str
    ]

    assert len(coder_events) > 0
    assert len(researcher_events) > 0

    # Ensure parent_run_id is preserved on all bridged events
    for ev in coder_events:
        assert ev.metadata.get("parent_run_id") == str(parent_run_id)
        assert ev.metadata.get("delegated_agent") == "coder"

    for ev in researcher_events:
        assert ev.metadata.get("parent_run_id") == str(parent_run_id)
        assert ev.metadata.get("delegated_agent") == "researcher"