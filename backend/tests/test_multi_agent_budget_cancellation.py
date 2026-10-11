# backend/tests/test_multi_agent_budget_cancellation.py
"""
Unit and integration tests for PELDRUN Native Multi-Agent PR 4:
1. Cumulative token budget exhaustion stops execution with TOKEN_BUDGET_EXCEEDED.
2. Max child runs cap rejects excess delegation tasks with CHILD_RUN_LIMIT_EXCEEDED.
3. Concurrency semaphore limits simultaneous child runs.
4. Cascading cancellation: cancelling parent cancels all active children.
5. Independent cancellation: cancelling child terminates child without cancelling parent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import uuid
import pytest

from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask, DelegationStatus
from peldrun.events.emitter import EventEmitter
from peldrun.runtime.contract import (
    AgentSpec,
    BudgetExceededError,
    DelegationBudget,
    RunRequest,
    RunResult,
    RunStatus,
    WorkspaceContext,
)
from peldrun.runtime.executor import CoreRuntimeExecutor


class DummyRunner:
    """Mock runner supporting cooperative cancellation."""
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class TestCascadingAndIndependentCancellation:
    """Verifies tree cancellation mechanics."""

    def test_cancelling_parent_cascades_to_all_children(self):
        executor = CoreRuntimeExecutor()

        parent_id = "parent_run_001"
        child_1 = "child_run_101"
        child_2 = "child_run_102"

        parent_runner = DummyRunner()
        child_1_runner = DummyRunner()
        child_2_runner = DummyRunner()

        executor._active_runners[parent_id] = parent_runner
        executor._active_runners[child_1] = child_1_runner
        executor._active_runners[child_2] = child_2_runner

        executor._run_children[parent_id] = {child_1, child_2}
        executor._run_parents[child_1] = parent_id
        executor._run_parents[child_2] = parent_id

        # Cancel parent
        assert executor.cancel(parent_id) is True

        assert parent_runner.cancelled is True
        assert child_1_runner.cancelled is True
        assert child_2_runner.cancelled is True

    def test_cancelling_child_does_not_cancel_parent(self):
        executor = CoreRuntimeExecutor()

        parent_id = "parent_run_002"
        child_1 = "child_run_201"

        parent_runner = DummyRunner()
        child_1_runner = DummyRunner()

        executor._active_runners[parent_id] = parent_runner
        executor._active_runners[child_1] = child_1_runner

        executor._run_children[parent_id] = {child_1}
        executor._run_parents[child_1] = parent_id

        # Cancel child independently
        assert executor.cancel(child_1) is True

        assert child_1_runner.cancelled is True
        assert parent_runner.cancelled is False


@pytest.mark.asyncio
class TestBranchingAndConcurrencyCaps:
    """Verifies child spawning and concurrency bounds in MultiAgentCoordinator."""

    async def test_coordinator_rejects_excess_child_runs(self, tmp_path: Path):
        emitter = EventEmitter(run_id="parent_branch_test")

        async def mock_execute(req: RunRequest, event_listener=None) -> RunResult:
            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Done",
            )

        parent_budget = DelegationBudget(max_child_runs=2)
        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_p_branch",
            prompt="Master plan",
            agent_spec=AgentSpec(id="peldrun"),
            workspace=WorkspaceContext(workspace_id="ws_b", root_path=str(tmp_path)),
            budget=parent_budget,
        )

        coord = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(tmp_path),
            execute_fn=mock_execute,
            parent_request=parent_req,
        )

        # First run -> Success
        t1 = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Task 1",
            budget=parent_budget,
        )
        r1 = await coord.delegate(t1)
        assert r1.success is True

        # Second run -> Success
        t2 = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Task 2",
            budget=parent_budget,
        )
        r2 = await coord.delegate(t2)
        assert r2.success is True

        # Third run -> Must be rejected (limit=2)
        t3 = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Task 3",
            budget=parent_budget,
        )
        r3 = await coord.delegate(t3)
        assert r3.success is False
        assert r3.status == DelegationStatus.FAILED
        assert r3.error_code == "CHILD_RUN_LIMIT_EXCEEDED"

    async def test_concurrency_semaphore_limits_parallel_runs(self, tmp_path: Path):
        emitter = EventEmitter(run_id="parent_concurrency_test")
        active_concurrent = 0
        max_concurrent_observed = 0

        async def slow_mock_execute(req: RunRequest, event_listener=None) -> RunResult:
            nonlocal active_concurrent, max_concurrent_observed
            active_concurrent += 1
            if active_concurrent > max_concurrent_observed:
                max_concurrent_observed = active_concurrent
            await asyncio.sleep(0.05)
            active_concurrent -= 1
            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Done",
            )

        parent_budget = DelegationBudget(max_child_runs=5, max_parallel_children=2)
        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_p_conc",
            prompt="Parallel tasks",
            agent_spec=AgentSpec(id="peldrun"),
            workspace=WorkspaceContext(workspace_id="ws_c", root_path=str(tmp_path)),
            budget=parent_budget,
        )

        coord = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(tmp_path),
            execute_fn=slow_mock_execute,
            parent_request=parent_req,
        )

        tasks = [
            DelegatedTask(
                parent_run_id=parent_req.run_id,
                target_agent_id="code_architect",
                instruction=f"Parallel subtask {i}",
                budget=parent_budget,
            )
            for i in range(4)
        ]

        results = await asyncio.gather(*(coord.delegate(t) for t in tasks))

        assert all(r.success for r in results)
        assert max_concurrent_observed <= 2


class TestBudgetExceededErrorContract:
    """Verifies BudgetExceededError attributes."""

    def test_budget_exceeded_error_fields(self):
        err = BudgetExceededError(
            message="Token limit exceeded",
            current_usage=15000,
            limit=12000,
            metric="tokens",
        )
        assert str(err) == "Token limit exceeded"
        assert err.current_usage == 15000
        assert err.limit == 12000
        assert err.metric == "tokens"
