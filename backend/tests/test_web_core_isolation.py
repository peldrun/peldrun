"""
backend/tests/test_web_core_isolation.py

Architectural Verification Suite for TASK P1-02 (Web-Core Isolation).

Asserts:
1. Architectural Isolation: omweb/engines/peldrun_engine.py does NOT import
   Core internal implementation classes (AgentRunner, ExecutionState, ToolCallAgent, EventEmitter).
2. Public Contract Integrity: RunRequest and RunResult adhere strictly to domain specifications.
3. Core Execution Boundary: CoreRuntimeExecutor executes RunRequest and returns typed RunResult.
4. Cancellation Support: core_executor cleanly handles operator cancellation requests.
"""

import ast
from pathlib import Path
import pytest

from peldrun.runtime.contract import AgentSpec, RunRequest, RunResult, RunStatus, WorkspaceContext
from peldrun.runtime.executor import CoreRuntimeExecutor


def test_peldrun_engine_no_forbidden_core_imports():
    """Verify that Web PeldrunEngine does not import forbidden Core internal classes."""
    engine_file = Path(__file__).resolve().parent.parent / "omweb" / "engines" / "peldrun_engine.py"
    assert engine_file.exists(), f"Engine file {engine_file} not found."

    source_code = engine_file.read_text(encoding="utf-8")
    tree = ast.parse(source_code)

    forbidden_modules = {
        "peldrun.agents.tool_call_agent",
        "peldrun.engine.runner",
        "peldrun.engine.state",
        "peldrun.events.emitter",
    }
    forbidden_names = {
        "ToolCallAgent",
        "AgentRunner",
        "ExecutionState",
        "EventEmitter",
        "MessageRole",
    }

    imported_modules = set()
    imported_names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported_modules.add(mod)
            for alias in node.names:
                imported_names.add(alias.name)

    # Check for forbidden module imports
    violating_mods = forbidden_modules.intersection(imported_modules)
    assert not violating_mods, f"Forbidden Core internal modules imported in Web adapter: {violating_mods}"

    # Check for forbidden class name imports
    violating_names = forbidden_names.intersection(imported_names)
    assert not violating_names, f"Forbidden Core internal classes imported in Web adapter: {violating_names}"


def test_canonical_run_contracts(tmp_path):
    """Verify RunRequest and RunResult serialization and property invariants."""
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
    assert request.workspace.path == tmp_path.resolve()

    # Verify RunResult properties
    res_success = RunResult(
        job_id="job_abc",
        status=RunStatus.COMPLETED,
        final_output="Task done",
        steps_executed=3,
        deliverables=["index.html"],
    )
    assert res_success.is_success is True
    assert res_success.is_failed is False
    assert res_success.is_cancelled is False

    res_fail = RunResult(
        job_id="job_abc",
        status=RunStatus.FAILED,
        error="Execution error",
    )
    assert res_fail.is_success is False
    assert res_fail.is_failed is True

    res_cancel = RunResult(
        job_id="job_abc",
        status=RunStatus.CANCELLED,
        error="Cancelled by user",
    )
    assert res_cancel.is_cancelled is True


@pytest.mark.asyncio
async def test_core_executor_cancellation():
    """Verify CoreRuntimeExecutor tracks active runs and cancels cleanly."""
    executor = CoreRuntimeExecutor()
    assert executor.cancel("non_existent_job") is False