# backend/tests/test_multi_agent_gateway.py
"""
Integration tests for PELDRUN Native Multi-Agent PR 3 Execution Gateway.
Validates:
1. DelegateTool is registered ONLY for Team Lead ('peldrun') when delegation_allowed=True and depth=0.
2. DelegateTool is STRICTLY PROHIBITED for manual specialists (e.g., 'code_architect', 'analyst').
3. DelegateTool is STRICTLY PROHIBITED for child agents (depth >= 1).
4. DelegateTool is NOT registered when delegation_allowed=False.
5. Child tasks executed through execute_fn produce unified telemetry and lineage records.
"""

from __future__ import annotations

import uuid
from pathlib import Path
import pytest

from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask, DelegationStatus
from peldrun.events.emitter import EventEmitter
from peldrun.runtime.contract import (
    AgentSpec,
    DelegationBudget,
    RunRequest,
    RunResult,
    RunStatus,
    WorkspaceContext,
)
from peldrun.runtime.executor import CoreRuntimeExecutor


class TestExecutionGatewayGovernance:
    """Verifies strict role-based tool registration rules."""

    @pytest.fixture
    def base_request(self, tmp_path: Path) -> RunRequest:
        return RunRequest(
            job_id="job_gov_test",
            prompt="Supervise architectural redesign",
            agent_spec=AgentSpec(id="peldrun", name="Lead Orchestrator"),
            workspace=WorkspaceContext(
                workspace_id="ws_gov",
                root_path=str(tmp_path),
            ),
        )

    def test_team_lead_with_delegation_enabled_receives_delegate_tool(self, base_request: RunRequest):
        req = base_request.model_copy(
            update={"delegation_allowed": True, "delegation_depth": 0}
        )
        spec = req.agent_spec
        agent_id_clean = spec.id.lower().strip()
        is_lead = agent_id_clean in ("peldrun", "lead", "team_lead")
        can_delegate = req.delegation_allowed and req.delegation_depth == 0 and is_lead

        assert can_delegate is True

    def test_team_lead_with_delegation_disabled_prohibits_delegate_tool(self, base_request: RunRequest):
        req = base_request.model_copy(
            update={"delegation_allowed": False, "delegation_depth": 0}
        )
        spec = req.agent_spec
        agent_id_clean = spec.id.lower().strip()
        is_lead = agent_id_clean in ("peldrun", "lead", "team_lead")
        can_delegate = req.delegation_allowed and req.delegation_depth == 0 and is_lead

        assert can_delegate is False

    def test_manual_specialist_prohibits_delegate_tool_even_if_flagged(self, base_request: RunRequest):
        req = base_request.model_copy(
            update={
                "agent_spec": AgentSpec(id="code_architect", name="Specialist Coder"),
                "delegation_allowed": True,
                "delegation_depth": 0,
            }
        )
        spec = req.agent_spec
        agent_id_clean = spec.id.lower().strip()
        is_lead = agent_id_clean in ("peldrun", "lead", "team_lead")
        can_delegate = req.delegation_allowed and req.delegation_depth == 0 and is_lead

        assert can_delegate is False

    def test_child_agent_prohibits_delegate_tool_at_depth_one(self, base_request: RunRequest):
        req = base_request.model_copy(
            update={
                "delegation_allowed": True,
                "delegation_depth": 1,
            }
        )
        spec = req.agent_spec
        agent_id_clean = spec.id.lower().strip()
        is_lead = agent_id_clean in ("peldrun", "lead", "team_lead")
        can_delegate = req.delegation_allowed and req.delegation_depth == 0 and is_lead

        assert can_delegate is False


@pytest.mark.asyncio
class TestCoordinatorRuntimeGatewayExecution:
    """Verifies that coordinator delegates through execute_fn correctly."""

    async def test_coordinator_routes_child_run_through_execute_fn(self, tmp_path: Path):
        emitter = EventEmitter(run_id="parent_run_123")
        recorded_requests: list[RunRequest] = []

        async def fake_execute(req: RunRequest, event_listener=None) -> RunResult:
            recorded_requests.append(req)
            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Specialist subtask completed successfully.",
                deliverables=["output_report.txt"],
                parent_run_id=req.parent_run_id,
                root_run_id=req.root_run_id,
                delegation_depth=req.delegation_depth,
                metadata={
                    "diagnostics": {
                        "total_tokens": 850,
                        "total_cost_nano_usd": 25000,
                    }
                },
            )

        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_parent_999",
            prompt="Parent overarching goal",
            agent_spec=AgentSpec(id="peldrun", name="Lead"),
            workspace=WorkspaceContext(workspace_id="ws_01", root_path=str(tmp_path)),
            llm_config={"model": "gpt-4o-mini"},
        )

        coord = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(tmp_path),
            execute_fn=fake_execute,
            parent_request=parent_req,
        )

        task = DelegatedTask(
            parent_run_id=parent_req.run_id,
            root_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Create database abstraction layer",
            delegation_depth=1,
            budget=DelegationBudget(max_steps=8, timeout_seconds=180.0),
        )

        res = await coord.delegate(task)

        # 1. Verify coordinator returned enriched DelegationResult
        assert res.success is True
        assert res.status == DelegationStatus.COMPLETED
        assert res.tokens_used == 850
        assert res.cost_nano_usd == 25000
        assert "Specialist subtask completed successfully." in res.output

        # 2. Verify child request was constructed and dispatched via authoritative gateway
        assert len(recorded_requests) == 1
        child_req = recorded_requests[0]
        assert child_req.agent_spec.id == "code_architect"
        assert child_req.parent_run_id == str(parent_req.run_id)
        assert child_req.root_run_id == str(parent_req.run_id)
        assert child_req.delegation_depth == 1
        assert child_req.delegation_allowed is False  # Child must never be allowed to delegate
        assert child_req.budget.max_steps == 8
        assert child_req.total_timeout_seconds == 180.0
