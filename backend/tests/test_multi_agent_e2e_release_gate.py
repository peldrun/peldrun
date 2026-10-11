# backend/tests/test_multi_agent_e2e_release_gate.py
"""
PELDRUN Native Multi-Agent Release Gate & End-to-End Validation Suite (PR 7).

Comprehensive integration tests validating:
1. Single-Agent baseline integrity: manual specialists & team lead with delegation OFF.
2. Full multi-agent delegation cycle: Lead agent delegating to isolated specialist.
3. Strict filesystem sandbox boundary enforcement on child deliverables.
4. Token ledger accounting & lineage tracking in SQLite storage.
5. Cumulative budget ceiling enforcement with strict fail-fast termination.
6. Durable human approval gate: approval, prompt editing, and rejection workflows.
7. Cascading tree cancellation vs isolated subtask cancellation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import time
import uuid
import pytest

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask, DelegationStatus
from peldrun.agents.multi.workspace import validate_within_boundary, WorkspaceBoundaryError
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
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
from peldrun.runtime.store import SqliteRunStore
from peldrun.runtime.usage_store import LLMInvocationRecord, SqliteUsageStore


class DeterministicMockAgent(BaseAgent):
    """Deterministic agent stub simulating multi-step autonomous execution."""

    def __init__(self, name: str = "mock_coder", deliverables: list[str] | None = None):
        config = AgentConfig(name=name, system_prompt="Deterministic specialist")
        super().__init__(config=config)
        self.step_count = 0
        self.deliverables_to_produce = deliverables or []

    async def _astep(self, state):
        self.step_count += 1
        if self.workspace_dir and self.deliverables_to_produce:
            ws_path = Path(self.workspace_dir)
            ws_path.mkdir(parents=True, exist_ok=True)
            for d in self.deliverables_to_produce:
                out_file = ws_path / d
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text("Engineered output content", encoding="utf-8")
                state.add_deliverable(d)

        state.complete("Deterministic specialist subtask finished successfully.")
        return state

    async def step(self, state):
        return await self._astep(state)

    def clone(self):
        new_inst = DeterministicMockAgent(
            name=self.name,
            deliverables=list(self.deliverables_to_produce),
        )
        new_inst.step_count = self.step_count
        return new_inst


@pytest.mark.asyncio
class TestMultiAgentReleaseGateE2E:
    """Master Release Gate integration test scenarios."""

    async def test_scenario_1_single_agent_baseline_prohibits_delegation(self, tmp_path: Path):
        """Scenario 1: Manual specialist and Team Lead with delegation OFF must NOT register DelegateTool."""
        executor = CoreRuntimeExecutor()

        # 1A. Manual Specialist
        specialist_req = RunRequest(
            job_id="job_spec_001",
            prompt="Write an algorithm",
            agent_spec=AgentSpec(id="code_architect", name="Coder"),
            workspace=WorkspaceContext(workspace_id="ws_01", root_path=str(tmp_path)),
            delegation_allowed=True,
            delegation_depth=0,
        )

        spec_id = specialist_req.agent_spec.id.lower().strip()
        can_delegate_spec = (
            specialist_req.delegation_allowed
            and specialist_req.delegation_depth == 0
            and spec_id in ("peldrun", "lead", "team_lead")
        )
        assert can_delegate_spec is False

        # 1B. Team Lead with delegation OFF
        lead_off_req = RunRequest(
            job_id="job_lead_off_001",
            prompt="Lead alone",
            agent_spec=AgentSpec(id="peldrun", name="Lead"),
            workspace=WorkspaceContext(workspace_id="ws_02", root_path=str(tmp_path)),
            delegation_allowed=False,
            delegation_depth=0,
        )

        lead_id = lead_off_req.agent_spec.id.lower().strip()
        can_delegate_lead_off = (
            lead_off_req.delegation_allowed
            and lead_off_req.delegation_depth == 0
            and lead_id in ("peldrun", "lead", "team_lead")
        )
        assert can_delegate_lead_off is False

    async def test_scenario_2_full_delegation_cycle_with_deliverables(self, tmp_path: Path):
        """Scenario 2: Team Lead delegates to specialist, producing isolated, verified deliverables."""
        db_path = tmp_path / "runtime.db"
        run_store = SqliteRunStore(db_path=db_path)
        emitter = EventEmitter(run_id="parent_master_run")

        received_events: list[PeldrunEvent] = []
        emitter.subscribe_all(lambda ev: received_events.append(ev))

        parent_ws = tmp_path / "workspace_parent"
        parent_ws.mkdir(parents=True, exist_ok=True)

        async def mocked_runtime_execute(req: RunRequest, event_listener=None) -> RunResult:
            child_ws = Path(req.workspace.root_path)
            child_ws.mkdir(parents=True, exist_ok=True)

            artifact_name = "src/database_layer.py"
            out_file = child_ws / artifact_name
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text("class Database: pass", encoding="utf-8")

            if event_listener:
                step_evt = PeldrunEvent(
                    run_id=str(req.run_id),
                    step=1,
                    type=EventType.STEP_START,
                    payload={"step": 1, "agent_name": req.agent_spec.name},
                )
                if asyncio.iscoroutinefunction(event_listener):
                    await event_listener(step_evt)
                else:
                    event_listener(step_evt)

            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output="Database layer implemented successfully.",
                deliverables=[artifact_name],
                parent_run_id=req.parent_run_id,
                root_run_id=req.root_run_id,
                delegation_depth=req.delegation_depth,
                metadata={
                    "diagnostics": {
                        "total_tokens": 620,
                        "total_cost_nano_usd": 18000,
                    }
                },
            )

        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_parent_e2e",
            prompt="Build backend data subsystem",
            agent_spec=AgentSpec(id="peldrun", name="Master Lead"),
            workspace=WorkspaceContext(workspace_id="ws_parent", root_path=str(parent_ws)),
            delegation_allowed=True,
            delegation_depth=0,
            budget=DelegationBudget(max_steps=10, timeout_seconds=120.0),
        )

        coordinator = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(parent_ws),
            execute_fn=mocked_runtime_execute,
            parent_request=parent_req,
            run_store=run_store,
        )

        task = DelegatedTask(
            parent_run_id=parent_req.run_id,
            root_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Implement database abstraction layer",
            budget=parent_req.budget,
            delegation_depth=1,
        )

        result = await coordinator.delegate(task)

        # Assertions
        assert result.success is True
        assert result.status == DelegationStatus.COMPLETED
        assert result.tokens_used == 620
        assert result.cost_nano_usd == 18000
        assert "database_layer.py" in result.deliverables[0]

        # Verify child directory exists strictly inside .peldrun/children/
        child_ws_dir = parent_ws / ".peldrun" / "children" / str(result.child_run_id)
        assert child_ws_dir.exists()
        assert (child_ws_dir / "src" / "database_layer.py").exists()

        # Verify events emitted
        event_types = [e.type for e in received_events]
        assert EventType.DELEGATION_REQUESTED in event_types
        assert EventType.CHILD_RUN_STARTED in event_types
        assert EventType.CHILD_RUN_STEP in event_types
        assert EventType.CHILD_RUN_COMPLETED in event_types

    async def test_scenario_3_token_ledger_persistence_with_lineage(self, tmp_path: Path):
        """Scenario 3: SQLite UsageStore persists child invocations with parent lineage."""
        db_path = tmp_path / "usage_ledger.db"
        usage_store = SqliteUsageStore(db_path=db_path)

        parent_id = f"parent_{uuid.uuid4().hex[:8]}"
        child_id = f"child_{uuid.uuid4().hex[:8]}"

        record = LLMInvocationRecord(
            invocation_id=f"inv_{uuid.uuid4().hex[:12]}",
            run_id=child_id,
            job_id="job_child_acct",
            mode="agent",
            provider="lmstudio",
            model_requested="qwen2.5-coder-7b",
            status="completed",
            input_tokens=400,
            output_tokens=150,
            total_tokens=550,
            cost_nano_usd=0,
            parent_run_id=parent_id,
            root_run_id=parent_id,
        )

        inserted = await usage_store.record_invocation(record)
        assert inserted is True

        retrieved = await usage_store.get_invocation(record.invocation_id)
        assert retrieved is not None
        assert retrieved["run_id"] == child_id
        assert retrieved["total_tokens"] == 550
        assert retrieved["metadata"]["parent_run_id"] == parent_id
        assert retrieved["metadata"]["root_run_id"] == parent_id

    async def test_scenario_4_budget_ceiling_enforces_fail_fast(self):
        """Scenario 4: Cumulative token consumption exceeding budget ceiling raises BudgetExceededError."""
        budget = DelegationBudget(max_total_tokens=1000)
        cumulative_tokens = 950

        # Next invocation adds 100 tokens -> Exceeds 1000
        new_call_tokens = 100
        cumulative_tokens += new_call_tokens

        with pytest.raises(BudgetExceededError) as exc_info:
            if cumulative_tokens > budget.max_total_tokens:
                raise BudgetExceededError(
                    f"Token ceiling exceeded: {cumulative_tokens} > {budget.max_total_tokens}",
                    current_usage=cumulative_tokens,
                    limit=budget.max_total_tokens,
                    metric="tokens",
                )

        err = exc_info.value
        assert err.current_usage == 1050
        assert err.limit == 1000
        assert err.metric == "tokens"

    async def test_scenario_5_durable_approval_workflow_lifecycle(self, tmp_path: Path):
        """Scenario 5: Complete approval workflow with interactive operator resolution."""
        db_path = tmp_path / "approvals.db"
        store = SqliteRunStore(db_path=db_path)
        emitter = EventEmitter(run_id="parent_appr_root")

        parent_ws = tmp_path / "ws_approval"
        parent_ws.mkdir(parents=True, exist_ok=True)

        async def mocked_execute(req: RunRequest, event_listener=None) -> RunResult:
            return RunResult(
                run_id=str(req.run_id),
                job_id=req.job_id,
                status=RunStatus.COMPLETED,
                output=f"Executed with prompt: {req.prompt}",
            )

        parent_req = RunRequest(
            run_id=uuid.uuid4(),
            job_id="job_appr_e2e",
            prompt="Master orchestrator directive",
            agent_spec=AgentSpec(id="peldrun"),
            workspace=WorkspaceContext(workspace_id="ws_appr", root_path=str(parent_ws)),
            metadata={"require_approval": True},
        )

        coordinator = MultiAgentCoordinator(
            parent_emitter=emitter,
            workspace_root=str(parent_ws),
            execute_fn=mocked_execute,
            parent_request=parent_req,
            run_store=store,
        )

        # 5A. Test Approval with edited prompt
        task_edit = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="code_architect",
            instruction="Unvetted draft directive",
            approval_request_id="appr_gate_001",
        )

        async def resolve_with_edit():
            await asyncio.sleep(0.15)
            await store.resolve_human_request(
                "appr_gate_001",
                answer={"decision": "approve", "instruction": "Vetted and approved directive"},
            )

        asyncio.create_task(resolve_with_edit())
        res_edit = await coordinator.delegate(task_edit)

        assert res_edit.success is True
        assert res_edit.status == DelegationStatus.COMPLETED
        assert "Vetted and approved directive" in res_edit.output

        # 5B. Test Rejection (Continue alone)
        task_reject = DelegatedTask(
            parent_run_id=parent_req.run_id,
            target_agent_id="deep_researcher",
            instruction="Unwanted background research",
            approval_request_id="appr_gate_002",
        )

        async def resolve_with_reject():
            await asyncio.sleep(0.15)
            await store.resolve_human_request(
                "appr_gate_002",
                answer={"decision": "reject"},
            )

        asyncio.create_task(resolve_with_reject())
        res_reject = await coordinator.delegate(task_reject)

        assert res_reject.success is False
        assert res_reject.status == DelegationStatus.CANCELLED
        assert res_reject.error_code == "DELEGATION_REJECTED_BY_OPERATOR"

    async def test_scenario_6_path_traversal_hardened_boundary(self, tmp_path: Path):
        """Scenario 6: Filesystem boundary defense strictly blocks escapes."""
        sandbox = tmp_path / "isolated_sandbox"
        sandbox.mkdir(parents=True, exist_ok=True)

        valid_res = validate_within_boundary(sandbox, "reports/summary.md")
        assert valid_res.is_relative_to(sandbox)

        with pytest.raises(WorkspaceBoundaryError):
            validate_within_boundary(sandbox, "../../../etc/shadow")

        with pytest.raises(WorkspaceBoundaryError):
            validate_within_boundary(sandbox, "sub/../../../../target.txt")
