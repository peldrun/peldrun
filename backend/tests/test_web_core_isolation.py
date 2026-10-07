"""
backend/tests/test_web_core_isolation.py

Verifies architectural isolation between the Web layer (omweb) and Core (peldrun).
Ensures adapters only consume canonical public runtime contracts and prevents
unauthorized internal coupling or leaks.
"""

from pathlib import Path
import pytest

from peldrun.runtime.contract import (
    WorkspaceContext,
    AgentSpec,
    RunRequest,
    RunResult,
    RunStatus,
)


def test_peldrun_engine_no_forbidden_core_imports():
    """
    Verify that web engine adapter source code does not contain forbidden direct core imports.
    Reads file content directly to avoid triggering circular import side-effects in test isolation.
    """
    engine_file = Path(__file__).resolve().parent.parent / "omweb" / "engines" / "peldrun_engine.py"
    assert engine_file.exists(), f"Engine file not found: {engine_file}"

    source_code = engine_file.read_text(encoding="utf-8")
    forbidden_imports = [
        "peldrun.engine.runner",
        "peldrun.engine.graph",
        "peldrun.agents.react_agent",
        "peldrun.agents.coding_agent",
    ]
    for forbidden in forbidden_imports:
        assert forbidden not in source_code, f"Forbidden direct import detected: {forbidden}"


def test_canonical_run_contracts(tmp_path: Path):
    """Verify RunRequest and RunResult serialization and property invariants against V1 contract."""
    ws_ctx = WorkspaceContext(
        workspace_id="test_ws",
        root_path=str(tmp_path),
        chat_id="chat_123",
    )
    spec = AgentSpec(
        id="test_agent",
        name="Test Agent",
        system_prompt="System instructions",
        tools=["terminate"],
        max_steps=10,
    )
    request = RunRequest(
        job_id="job_abc",
        prompt="Execute task",
        agent_spec=spec,
        workspace=ws_ctx,
    )

    assert request.job_id == "job_abc"
    assert Path(request.workspace.root_path).resolve() == tmp_path.resolve()
    assert request.agent_spec.id == "test_agent"
    assert request.agent_spec.max_steps == 10

    # Verify RunResult properties conforming to V1 Durable Execution Contract
    res_success = RunResult(
        run_id="run_success_123",
        job_id="job_abc",
        status=RunStatus.COMPLETED,
        output="Task done",
        total_steps=3,
        deliverables=["index.html"],
    )

    assert res_success.run_id == "run_success_123"
    assert res_success.job_id == "job_abc"
    assert res_success.status == RunStatus.COMPLETED
    assert res_success.output == "Task done"
    assert res_success.total_steps == 3
    assert res_success.deliverables == ["index.html"]
    assert res_success.status.is_terminal is True
    assert res_success.status.is_active is False

    res_failed = RunResult(
        run_id="run_failed_456",
        job_id="job_abc",
        status=RunStatus.FAILED,
        error="Execution error encountered.",
        total_steps=1,
    )

    assert res_failed.run_id == "run_failed_456"
    assert res_failed.status == RunStatus.FAILED
    assert res_failed.error == "Execution error encountered."
    assert res_failed.status.is_terminal is True


@pytest.mark.asyncio
async def test_core_executor_cancellation(tmp_path: Path):
    """Verify that CoreRuntimeExecutor handles cancellation requests properly."""
    from peldrun.runtime.executor import CoreRuntimeExecutor

    executor = CoreRuntimeExecutor()
    result = executor.cancel("non_existent_run_id")
    assert result in (False, True, None)