"""
Pytest Integration Suite for Consolidated AgentRunner Runtime Lifecycle.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from omweb.engines import EngineRunContext, PeldrunEngine
from omweb.job_manager import job_manager
from peldrun.agents.tool_call_agent import ToolCallAgent
from peldrun.engine.runner import AgentRunner, RunnerConfig, StepExecutableAgent
from peldrun.engine.state import ExecutionState, ExecutionStatus
from peldrun.events.emitter import EventEmitter
from peldrun.runtime.contract import AgentSpec, RunRequest, WorkspaceContext


def test_core_runtime_contracts(tmp_path):
    """Verify initialization and serialization of Core runtime contracts."""
    ws = WorkspaceContext(workspace_id="ws_01", root_path=str(tmp_path))
    spec = AgentSpec(id="agent_01", name="Spec Agent", tools=["terminate"])
    req = RunRequest(job_id="job_01", prompt="run prompt", agent_spec=spec, workspace=ws)

    assert req.job_id == "job_01"
    assert req.workspace.root_path == str(tmp_path)
    assert req.agent_spec.tools == ["terminate"]


@pytest.mark.asyncio
async def test_agent_runner_lifecycle(tmp_path):
    """Verify that AgentRunner drives step cycle, checkpoints, and returns ExecutionState."""
    class DeterministicAgent:
        name = "TestRunnerAgent"

        async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
            state.final_output = "Task completed in test"
            return True

    agent = DeterministicAgent()
    assert isinstance(agent, StepExecutableAgent)

    runner = AgentRunner(
        agent=agent,
        config=RunnerConfig(max_steps=5, enable_checkpointing=True),
    )

    state = await runner.run(task_prompt="Run lifecycle test", workspace_root=str(tmp_path))

    assert state.status == ExecutionStatus.COMPLETED
    assert state.current_step == 1
    assert state.final_output == "Task completed in test"
    assert len(state.checkpoints) >= 1


@pytest.mark.asyncio
async def test_peldrun_engine_consolidated_run(tmp_path):
    """Verify PeldrunEngine dispatches jobs through consolidated AgentRunner pipeline."""
    job_id = "pytest-consolidated-job"
    job_manager.create_job(job_id)

    ctx = EngineRunContext(
        job_id=job_id,
        prompt="Consolidated run prompt",
        agent_id="peldrun",
        active_llm={"base_url": "http://127.0.0.1:1234/v1", "model": "mock", "temperature": 0.2},
        model_name="mock",
        provider_name="MockProvider",
        project_dir=tmp_path,
        chat_id="chat_01",
        manifest={"name": "peldrun", "tools": ["terminate"], "max_steps": 5},
    )

    engine = PeldrunEngine()

    with patch.object(ToolCallAgent, "step", new_callable=AsyncMock) as mock_step:
        async def _mock_step(state, emitter):
            state.final_output = "Pytest verified consolidated execution"
            return True

        mock_step.side_effect = _mock_step

        await engine.run(ctx)

        job = job_manager.get_job(job_id)
        raw_status = getattr(job, "status", "")
        status_val = (raw_status.value if hasattr(raw_status, "value") else str(raw_status)).lower()
        assert status_val == "completed"
        assert "Pytest verified consolidated execution" in getattr(job, "result", "")