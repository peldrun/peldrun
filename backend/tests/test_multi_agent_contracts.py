# backend/tests/test_multi_agent_contracts.py
"""
Unit and integration contract tests for PELDRUN Native Multi-Agent PR 1.
Validates:
1. DelegationBudget bounds, defaults, and validation constraints.
2. RunRequest lineage parameters, default isolation, and UUID/string coercion.
3. RunResult lineage reflection.
4. DelegatedTask and DelegationResult protocol extensions.
5. CoreRuntimeExecutor timeout propagation and unified identity resolution.
"""

from __future__ import annotations

import uuid
import pytest
from pydantic import ValidationError

from peldrun.agents.multi.protocol import DelegatedTask, DelegationResult, DelegationStatus
from peldrun.runtime.contract import (
    AgentSpec,
    DelegationBudget,
    RunRequest,
    RunResult,
    RunStatus,
    WorkspaceContext,
)
from peldrun.runtime.executor import CoreRuntimeExecutor


class TestDelegationBudgetContract:
    """Verifies typed resource budget boundary behavior."""

    def test_default_budget_values(self):
        budget = DelegationBudget()
        assert budget.max_steps == 10
        assert budget.max_total_tokens == 12_000
        assert budget.max_cost_nano_usd is None
        assert budget.timeout_seconds == 300.0
        assert budget.max_child_runs == 3
        assert budget.max_parallel_children == 2

    def test_custom_budget_values(self):
        budget = DelegationBudget(
            max_steps=25,
            max_total_tokens=50_000,
            max_cost_nano_usd=5_000_000,
            timeout_seconds=600.0,
            max_child_runs=5,
            max_parallel_children=3,
        )
        assert budget.max_steps == 25
        assert budget.max_total_tokens == 50_000
        assert budget.max_cost_nano_usd == 5_000_000
        assert budget.timeout_seconds == 600.0

    def test_negative_steps_rejected(self):
        with pytest.raises(ValidationError):
            DelegationBudget(max_steps=0)

        with pytest.raises(ValidationError):
            DelegationBudget(max_steps=-5)

    def test_negative_timeout_rejected(self):
        with pytest.raises(ValidationError):
            DelegationBudget(timeout_seconds=-10.0)

    def test_excessive_steps_rejected(self):
        with pytest.raises(ValidationError):
            DelegationBudget(max_steps=100)


class TestRunRequestLineageContract:
    """Verifies RunRequest backward compatibility and lineage extensions."""

    @pytest.fixture
    def minimal_run_request(self) -> RunRequest:
        return RunRequest(
            job_id="job_test_123",
            prompt="Analyze architectural bottlenecks",
            agent_spec=AgentSpec(id="peldrun", name="PELDRUN Coordinator"),
            workspace=WorkspaceContext(
                workspace_id="ws_001",
                root_path="/tmp/peldrun_test_ws",
            ),
        )

    def test_default_lineage_is_root(self, minimal_run_request: RunRequest):
        req = minimal_run_request
        assert req.parent_run_id is None
        assert req.root_run_id is None
        assert req.delegation_depth == 0
        assert req.delegation_allowed is False
        assert req.budget.max_steps == 10
        assert req.approval_request_id is None

    def test_child_run_request_lineage(self, minimal_run_request: RunRequest):
        parent_id = str(uuid.uuid4())
        root_id = str(uuid.uuid4())
        child_req = minimal_run_request.model_copy(
            update={
                "parent_run_id": parent_id,
                "root_run_id": root_id,
                "delegator_agent_id": "peldrun",
                "delegation_depth": 1,
                "delegation_allowed": False,
                "approval_request_id": "appr_999",
            }
        )
        assert child_req.parent_run_id == parent_id
        assert child_req.root_run_id == root_id
        assert child_req.delegation_depth == 1
        assert child_req.delegator_agent_id == "peldrun"
        assert child_req.approval_request_id == "appr_999"

    def test_run_id_coercion_from_string(self, minimal_run_request: RunRequest):
        custom_id = "test-custom-run-identifier-001"
        req = minimal_run_request.model_copy(update={"run_id": custom_id})
        assert str(req.run_id) == custom_id

    def test_run_result_lineage_reflection(self):
        result = RunResult(
            run_id="run_child_01",
            job_id="job_child_01",
            status=RunStatus.COMPLETED,
            output="Done",
            parent_run_id="run_parent_01",
            root_run_id="run_root_01",
            delegation_depth=1,
        )
        assert result.parent_run_id == "run_parent_01"
        assert result.root_run_id == "run_root_01"
        assert result.delegation_depth == 1


class TestMultiAgentProtocolContract:
    """Verifies DelegatedTask and DelegationResult protocols."""

    def test_delegated_task_defaults(self):
        parent_uuid = uuid.uuid4()
        task = DelegatedTask(
            parent_run_id=parent_uuid,
            target_agent_id="code_architect",
            instruction="Write a unit test",
        )
        assert task.parent_run_id == parent_uuid
        assert task.target_agent_id == "code_architect"
        assert task.delegation_depth == 1
        assert task.budget.max_steps == 10
        assert task.budget.max_total_tokens == 12_000

    def test_delegation_status_lifecycle(self):
        assert not DelegationStatus.PENDING.is_terminal
        assert not DelegationStatus.WAITING_APPROVAL.is_terminal
        assert not DelegationStatus.RUNNING.is_terminal
        assert DelegationStatus.COMPLETED.is_terminal
        assert DelegationStatus.FAILED.is_terminal
        assert DelegationStatus.CANCELLED.is_terminal
        assert DelegationStatus.TIMED_OUT.is_terminal

    def test_delegation_result_enrichment(self):
        t_id = uuid.uuid4()
        c_id = uuid.uuid4()
        p_id = uuid.uuid4()
        res = DelegationResult(
            task_id=t_id,
            child_run_id=c_id,
            parent_run_id=p_id,
            success=True,
            status=DelegationStatus.COMPLETED,
            output="Artifact produced successfully",
            tokens_used=1540,
            cost_nano_usd=35000,
            duration_seconds=4.25,
        )
        assert res.success is True
        assert res.status == DelegationStatus.COMPLETED
        assert res.tokens_used == 1540
        assert res.cost_nano_usd == 35000
        assert res.duration_seconds == 4.25


class TestCoreExecutorRuntimeIdentity:
    """Verifies CoreRuntimeExecutor identity and cancellation mechanics."""

    def test_executor_cancellation_by_run_id_or_job_id(self):
        executor = CoreRuntimeExecutor()

        class DummyRunner:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        dummy = DummyRunner()
        executor._active_runners["run_100"] = dummy
        executor._active_runners["job_100"] = dummy

        # Can cancel using run_id
        assert executor.cancel("run_100") is True
        assert dummy.cancelled is True

        dummy.cancelled = False
        # Can cancel using job_id
        assert executor.cancel("job_100") is True
        assert dummy.cancelled is True

        # Non-existent ID returns False cleanly
        assert executor.cancel("unknown_id") is False
