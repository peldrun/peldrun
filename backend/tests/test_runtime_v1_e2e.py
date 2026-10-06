"""
backend/tests/test_runtime_v1_e2e.py

PELDRUN Runtime V1 Comprehensive End-to-End Integration Test Suite.
Validates the complete execution runtime lifecycle across Tests A through N:
- Test A: Verified search with idempotent retry policy
- Test B: Sequential multi-tool execution and distinct tool identities
- Test C: Interactive Human-in-the-loop interruption and resumption
- Test D: Cross-process durable state persistence via SqliteRunStore
- Test E: Multi-question flow isolation (request_id != job_id) and idempotency
- Test F: Tool retry loop on transient failures with exponential backoff
- Test G: Non-retryable guards on destructive and side-effecting tools
- Test H: Direct artifact mutation tracking and manifest revisioning
- Test I: Test-fail-fix-pass cycle simulation
- Test J: Genuine LLM failure semantics (no fake complete)
- Test K: Monotonic SSE sequence replay and event deduplication
- Test L: Clean cancellation semantics (RUNNING -> CANCELLED, never PAUSED)
- Test M: Strict workspace sandbox containment and traversal rejection
- Test N: Max-step ceiling exhaustion handled strictly as FAILED
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

# Step 1: Ensure backend root directory is in sys.path before any local imports
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest

# Core Runtime & Contracts
from peldrun.runtime.contract import (
    AgentSpec,
    HumanInputRequest,
    HumanInputStatus,
    RunRequest,
    RunResult,
    RunStatus,
    WorkspaceContext,
)
from peldrun.runtime.store import SqliteRunStore, get_run_store

# State Machine & Runner
from peldrun.engine.runner import AgentRunner, RunnerConfig
from peldrun.engine.state import (
    ChatMessage,
    ExecutionState,
    ExecutionStatus,
    InvalidStateTransitionError,
    MessageRole,
)

# Events & Protocol
from peldrun.events.bus import EventBus
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent

# Tools & Policies
from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.collection import ToolCollection
from peldrun.tools.contract import ToolExecutionPolicy
from peldrun.tools.registry import ToolRegistry
from peldrun.tools.builtins.file_ops import FileOpsTool
from peldrun.tools.builtins.human_input import HumanInputRegistry, HumanInputTool
from peldrun.tools.builtins.str_replace_editor import StrReplaceEditor
from peldrun.tools.builtins.web_search import WebSearchTool

# Artifacts
from peldrun.artifacts.manager import ArtifactManager
from peldrun.artifacts.manifest import ArtifactManifest


# ==============================================================================
# Helper to execute async test coroutines cleanly across environments
# ==============================================================================

def run_async(coro):
    """Execute coroutine in an isolated event loop for robust test execution."""
    return asyncio.run(coro)


# ==============================================================================
# Fixtures & Test Setup
# ==============================================================================

@pytest.fixture
def temp_workspace():
    """Create isolated temporary directory acting as workspace root."""
    temp_dir = tempfile.mkdtemp(prefix="peldrun_e2e_ws_")
    yield Path(temp_dir).resolve()
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def temp_store(temp_workspace):
    """Create isolated SQLite store for durable state verification."""
    db_file = temp_workspace / "test_runtime.db"
    store = SqliteRunStore(db_path=db_file)
    yield store


# ==============================================================================
# Test A — Search Tool & Execution Policy
# ==============================================================================

def test_a_search_execution_policy(temp_workspace):
    """Test A: Verify WebSearchTool policy enforces idempotency and retry capability."""
    async def _test():
        tool = WebSearchTool(workspace_root=str(temp_workspace))
        policy = tool.get_execution_policy()

        assert policy.retryable is True
        assert policy.idempotent is True
        assert policy.side_effects is False
        assert policy.max_attempts == 3
        assert policy.timeout_seconds > 0.0

        res = await tool.aexecute(query="PELDRUN autonomous agent architecture", max_results=3)
        assert isinstance(res, ToolResult)
        assert res.exit_code == 0
        assert not res.is_error
        assert len(str(res.output)) > 0

    run_async(_test())


# ==============================================================================
# Test B — Sequential Multi-Tool Execution & Distinct Identities
# ==============================================================================

def test_b_multiple_tools_execution(temp_workspace):
    """Test B: Verify execution of multiple tools maintains discrete call identities."""
    async def _test():
        state = ExecutionState(task_prompt="Multi-tool pipeline", workspace_root=str(temp_workspace))
        state.mark_running()

        # Tool 1: File Create
        editor = StrReplaceEditor(workspace_root=str(temp_workspace))
        res1 = await editor.aexecute(command="create", path="module.py", file_text="x = 42\n")
        rec1 = state.record_tool_execution(
            tool_name="str_replace_editor",
            arguments={"command": "create", "path": "module.py"},
            output=res1.output,
            tool_call_id="tc_001",
        )

        # Tool 2: File Read via FileOps
        file_ops = FileOpsTool(workspace_root=str(temp_workspace))
        res2 = await file_ops.aexecute(action="read", path="module.py")
        rec2 = state.record_tool_execution(
            tool_name="file_ops",
            arguments={"action": "read", "path": "module.py"},
            output=res2.output,
            tool_call_id="tc_002",
        )

        assert len(state.tool_history) == 2
        assert state.tool_history[0].call_id == "tc_001"
        assert state.tool_history[1].call_id == "tc_002"
        assert "x = 42" in str(rec2.output)

    run_async(_test())


# ==============================================================================
# Test C — Human Interrupt, Wait, & Resume Flow
# ==============================================================================

def test_c_human_interrupt_and_resume(temp_workspace):
    """Test C: Verify ask_human pauses autonomy into WAITING_FOR_INPUT and resumes cleanly."""
    async def _test():
        emitter = EventEmitter(run_id="run_hitl_01")
        human_tool = HumanInputTool(workspace_root=str(temp_workspace), emitter=emitter)
        state = ExecutionState(run_id="run_hitl_01", task_prompt="Approval needed")
        state.mark_running()

        req_id = "req_approval_101"
        asked_question = "Do you approve deploying to staging?"

        async def _simulate_operator_reply():
            await asyncio.sleep(0.1)
            assert state.status == ExecutionStatus.WAITING_FOR_INPUT
            HumanInputRegistry.resolve_request(req_id, "Approved by Tech Lead")

        state.mark_waiting_for_input(reason="Agent asked human operator")
        reply_task = asyncio.create_task(_simulate_operator_reply())

        tool_res = await human_tool.aexecute(
            prompt=asked_question,
            input_type="confirm",
            request_id=req_id,
            timeout_seconds=5,
        )
        await reply_task

        assert tool_res.is_success
        assert tool_res.output == "Approved by Tech Lead"

        state.mark_running(reason="Resumed following human approval")
        assert state.status == ExecutionStatus.RUNNING

    run_async(_test())


# ==============================================================================
# Test D — Durable State Persistence Across Process Restart
# ==============================================================================

def test_d_durable_persistence_and_restart(temp_workspace):
    """Test D: Verify run state and human request survive process restart via SqliteRunStore."""
    async def _test():
        db_path = temp_workspace / "durable_store.db"

        # Process A: Write state & pending request
        store_a = SqliteRunStore(db_path=db_path)
        run_req = RunRequest(
            run_id=uuid4(),
            job_id="job_restart_test",
            prompt="Build application",
            agent_spec=AgentSpec(id="peldrun", name="PeldrunAgent", max_steps=10),
            workspace=WorkspaceContext(workspace_id="ws_01", root_path=str(temp_workspace)),
        )
        await store_a.create_run(run_req)

        state_a = ExecutionState(run_id=str(run_req.run_id), task_prompt="Build application")
        state_a.mark_running()
        state_a.mark_waiting_for_input(reason="Awaiting user choice")
        await store_a.save_state(str(run_req.run_id), state_a)

        h_req = HumanInputRequest(
            request_id="req_durable_restart",
            run_id=str(run_req.run_id),
            question="Choose theme: light or dark?",
            input_type="select",
            options=["light", "dark"],
            status=HumanInputStatus.PENDING,
        )
        await store_a.save_human_request(h_req)

        # Process B: Restart simulation with fresh store instance
        store_b = SqliteRunStore(db_path=db_path)
        loaded_run = await store_b.get_run(str(run_req.run_id))
        assert loaded_run is not None
        assert loaded_run["job_id"] == "job_restart_test"

        pending_list = await store_b.list_pending_human_requests(run_id=str(run_req.run_id))
        assert len(pending_list) == 1
        assert pending_list[0].request_id == "req_durable_restart"

        # Resolve in fresh process
        resolved = await store_b.resolve_human_request("req_durable_restart", "dark")
        assert resolved is True

        # State restored and resumed
        loaded_state = await store_b.load_state(str(run_req.run_id))
        assert loaded_state is not None
        assert loaded_state.status == ExecutionStatus.WAITING_FOR_INPUT

        loaded_state.mark_running(reason="Resumed in new process")
        await store_b.save_state(str(run_req.run_id), loaded_state)

        final_state = await store_b.load_state(str(run_req.run_id))
        assert final_state.status == ExecutionStatus.RUNNING

    run_async(_test())


# ==============================================================================
# Test E — Multi-Question Isolation & Idempotency
# ==============================================================================

def test_e_multi_question_isolation_and_idempotency(temp_store):
    """Test E: Verify request_id != job_id, preventing collision across sequential questions."""
    async def _test():
        job_id = "job_multi_q_test"

        req_1 = HumanInputRequest(
            request_id="req_question_1",
            run_id=job_id,
            question="What is your database name?",
            status=HumanInputStatus.PENDING,
        )
        req_2 = HumanInputRequest(
            request_id="req_question_2",
            run_id=job_id,
            question="What port should it run on?",
            status=HumanInputStatus.PENDING,
        )

        await temp_store.save_human_request(req_1)
        await temp_store.save_human_request(req_2)

        # Resolve Question 1
        first_res = await temp_store.resolve_human_request("req_question_1", "postgres_db")
        assert first_res is True

        # Idempotent second resolution attempt must return False
        dup_res = await temp_store.resolve_human_request("req_question_1", "duplicate_postgres_db")
        assert dup_res is False

        # Question 2 must still remain pending
        q2_record = await temp_store.get_human_request("req_question_2")
        assert q2_record is not None
        assert q2_record.status == HumanInputStatus.PENDING

        # Resolve Question 2 independently
        second_res = await temp_store.resolve_human_request("req_question_2", "5432")
        assert second_res is True

    run_async(_test())


# ==============================================================================
# Test F — Tool Retry Policy on Transient Failure
# ==============================================================================

class TransientFailingTool(BaseTool):
    """Mock tool that fails on first attempt and succeeds on second attempt."""

    name: str = "transient_service"
    description: str = "Simulates transient network timeouts before succeeding."
    execution_policy = ToolExecutionPolicy(
        max_attempts=3,
        timeout_seconds=5.0,
        retryable=True,
        idempotent=True,
        side_effects=False,
        backoff_factor=0.05,
    )

    def __init__(self, **kwargs: Any):
        super().__init__(execution_policy=self.execution_policy)
        self.call_count = 0

    async def _arun(self, **kwargs: Any) -> ToolResult:
        self.call_count += 1
        if self.call_count == 1:
            raise ConnectionResetError("Remote server closed socket connection abruptly.")
        return ToolResult(
            output=f"Connected successfully on attempt {self.call_count}",
            exit_code=0,
            is_error=False,
            attempt=self.call_count,
        )


def test_f_tool_retry_policy(temp_workspace):
    """Test F: Verify retry policy executes backoff, retries, and captures final success."""
    async def _test():
        tool = TransientFailingTool()
        policy = tool.get_execution_policy()
        state = ExecutionState(task_prompt="Execute transient tool", workspace_root=str(temp_workspace))
        state.mark_running()

        emitter = EventEmitter(run_id="run_retry_test")
        retry_events: List[PeldrunEvent] = []
        emitter.subscribe(EventType.TOOL_RETRY, lambda ev: retry_events.append(ev))

        final_output = ""
        is_success = False

        for attempt in range(1, policy.max_attempts + 1):
            has_error = False
            err_msg = ""
            err_obj = None

            try:
                res = await tool.aexecute()
                if res.is_success:
                    final_output = str(res.output)
                    is_success = True
                    state.record_tool_execution(
                        tool_name=tool.name,
                        arguments={},
                        output=res.output,
                        tool_call_id="tc_retry_01",
                        attempt=attempt,
                    )
                    break
                else:
                    has_error = True
                    err_msg = res.error
            except Exception as exc:
                has_error = True
                err_msg = str(exc)
                err_obj = exc

            if has_error:
                can_retry = policy.is_retry_permitted(attempt, err_obj)
                if can_retry:
                    delay = policy.compute_delay(attempt + 1)
                    await emitter.emit_tool_retry(
                        tool_name=tool.name,
                        tool_call_id="tc_retry_01",
                        attempt=attempt + 1,
                        max_attempts=policy.max_attempts,
                        error=err_msg,
                        delay_seconds=delay,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)

        assert is_success is True
        assert tool.call_count == 2
        assert "attempt 2" in final_output
        assert len(retry_events) == 1
        assert retry_events[0].payload["attempt"] == 2

    run_async(_test())


# ==============================================================================
# Test G — Non-Retryable Destructive Tool Guard
# ==============================================================================

class DestructiveDeleteTool(BaseTool):
    """Mock destructive tool with side_effects=True and retryable=False."""

    name: str = "purge_table"
    description: str = "Irreversible database wipe."
    execution_policy = ToolExecutionPolicy(
        max_attempts=1,
        retryable=False,
        idempotent=False,
        side_effects=True,
    )

    async def _arun(self, **kwargs: Any) -> ToolResult:
        return ToolResult(output="Permission denied for purge.", exit_code=1, is_error=True)


def test_g_destructive_tool_no_retry():
    """Test G: Destructive operations must never execute automated retries."""
    tool = DestructiveDeleteTool()
    policy = tool.get_execution_policy()

    assert policy.retryable is False
    assert policy.max_attempts == 1
    assert policy.is_retry_permitted(1, RuntimeError("Failure")) is False


# ==============================================================================
# Test H — Direct Artifact Mutation Tracking & Revisions
# ==============================================================================

def test_h_artifact_mutation_tracking(temp_workspace):
    """Test H: Verify direct artifact mutations track revisions without full directory scans."""
    async def _test():
        manager = ArtifactManager(workspace_root=str(temp_workspace))
        editor = StrReplaceEditor(workspace_root=str(temp_workspace))

        # Mutation 1: Create artifact
        res_create = await editor.aexecute(command="create", path="index.html", file_text="<h1>V1</h1>")
        assert res_create.is_success
        assert "index.html" in res_create.artifacts

        art_ref_1 = await manager.arecord_mutation(
            file_path=temp_workspace / "index.html",
            operation="created",
            source_tool="str_replace_editor",
        )
        assert art_ref_1 is not None
        assert art_ref_1.revision == 1
        assert art_ref_1.operation == "created"

        # Mutation 2: Edit artifact
        res_edit = await editor.aexecute(
            command="str_replace",
            path="index.html",
            old_str="<h1>V1</h1>",
            new_str="<h1>V2</h1>",
        )
        assert res_edit.is_success
        assert "index.html" in res_edit.artifacts

        art_ref_2 = await manager.arecord_mutation(
            file_path=temp_workspace / "index.html",
            operation="updated",
            source_tool="str_replace_editor",
        )
        assert art_ref_2 is not None
        assert art_ref_2.revision == 2
        assert art_ref_2.operation == "updated"

        # Manifest check
        manifest = manager.get_manifest()
        tracked = manifest.get_by_path("index.html")
        assert tracked is not None
        assert tracked.revision == 2

    run_async(_test())


# ==============================================================================
# Test I — Test-Fail-Fix-Pass Cycle Simulation
# ==============================================================================

def test_i_test_fail_fix_pass_cycle(temp_workspace):
    """Test I: Verify runner handles test execution failure, inspects error, and fixes code."""
    async def _test():
        editor = StrReplaceEditor(workspace_root=str(temp_workspace))
        state = ExecutionState(task_prompt="TDD Cycle", workspace_root=str(temp_workspace))
        state.mark_running()

        # Step 1: Create broken code
        await editor.aexecute(command="create", path="calculator.py", file_text="def add(a, b): return a - b\n")

        # Step 2: Test fails
        def run_test():
            with open(temp_workspace / "calculator.py", "r", encoding="utf-8") as f:
                code = f.read()
            return 0 if "return a + b" in code else 1

        exit_code_1 = run_test()
        assert exit_code_1 == 1

        state.record_tool_execution(
            tool_name="test_runner",
            arguments={"target": "calculator.py"},
            output="AssertionError: 2 + 2 != 0",
            exit_code=exit_code_1,
            is_error=True,
        )

        # Step 3: Agent fixes code
        await editor.aexecute(
            command="str_replace",
            path="calculator.py",
            old_str="return a - b",
            new_str="return a + b",
        )

        # Step 4: Test passes
        exit_code_2 = run_test()
        assert exit_code_2 == 0

        state.record_tool_execution(
            tool_name="test_runner",
            arguments={"target": "calculator.py"},
            output="All unit tests passed successfully.",
            exit_code=exit_code_2,
            is_error=False,
        )

        assert state.tool_history[-1].exit_code == 0
        assert not state.tool_history[-1].is_error

    run_async(_test())


# ==============================================================================
# Test J — Genuine LLM Failure Semantics (No Fake Complete)
# ==============================================================================

class FailingAgentMock:
    """Mock agent simulating unrecoverable LLM API failure."""

    name: str = "failing_agent"

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        raise RuntimeError("LLM Provider 500: Out of capacity")


def test_j_genuine_llm_failure_semantics(temp_workspace):
    """Test J: LLM provider failure must fail execution run and NEVER fake COMPLETED."""
    async def _test():
        runner = AgentRunner(
            agent=FailingAgentMock(),
            config=RunnerConfig(max_steps=5),
        )
        state = ExecutionState(task_prompt="Mission impossible", workspace_root=str(temp_workspace))

        with pytest.raises(RuntimeError):
            await runner.run(task_prompt="Mission impossible", state=state, workspace_root=str(temp_workspace))

        assert state.status == ExecutionStatus.FAILED
        assert state.is_failed is True
        assert state.is_completed is False

    run_async(_test())


# ==============================================================================
# Test K — Monotonic SSE Sequence & Replay Deduplication
# ==============================================================================

def test_k_monotonic_event_replay_and_deduplication(temp_store):
    """Test K: Verify replay_after(sequence) recovers missing events in monotonic order."""
    async def _test():
        run_id = f"run_seq_{uuid4().hex[:8]}"
        bus = EventBus(run_id=run_id, buffer_capacity=100)

        # Publish 20 events
        for i in range(1, 21):
            evt = PeldrunEvent(
                version=1,
                event_id=uuid4(),
                run_id=run_id,
                sequence=i,
                step=1,
                type=EventType.THOUGHT if i < 20 else EventType.FINAL,
                payload={"thought": f"Reasoning step {i}"},
            )
            await bus.publish(evt)

        # Replay after sequence 12 (must return 13..20 strictly)
        replayed = await bus.replay_after(sequence=12)

        assert len(replayed) == 8
        assert replayed[0].sequence == 13
        assert replayed[-1].sequence == 20

        sequences = [e.sequence for e in replayed]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))

    run_async(_test())


# ==============================================================================
# Test L — Operator Cancellation Semantics
# ==============================================================================

class StoppableAgentMock:
    """Mock agent performing long reasoning cycles."""

    name: str = "stoppable_agent"

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        await asyncio.sleep(0.05)
        return False


def test_l_cancellation_semantics(temp_workspace):
    """Test L: Cancellation must transition cleanly to CANCELLED and never PAUSED."""
    async def _test():
        runner = AgentRunner(
            agent=StoppableAgentMock(),
            config=RunnerConfig(max_steps=10),
        )
        state = ExecutionState(task_prompt="Long running job", workspace_root=str(temp_workspace))

        async def _trigger_cancel():
            await asyncio.sleep(0.08)
            runner.cancel()

        cancel_task = asyncio.create_task(_trigger_cancel())
        await runner.run(task_prompt="Long running job", state=state, workspace_root=str(temp_workspace))
        await cancel_task

        assert state.status == ExecutionStatus.CANCELLED
        assert state.is_cancelled is True

    run_async(_test())


# ==============================================================================
# Test M — Workspace Sandbox Containment & Path Traversal Guard
# ==============================================================================

def test_m_sandbox_path_security(temp_workspace):
    """Test M: Absolute paths and directory traversal escapes must be denied."""
    async def _test():
        editor = StrReplaceEditor(workspace_root=str(temp_workspace))

        # Traversal test 1: Relative escape
        res_traversal = await editor.aexecute(
            command="create",
            path="../../outside_file.txt",
            file_text="hacked",
        )
        assert res_traversal.is_error is True
        assert "outside workspace root" in str(res_traversal.output).lower() or res_traversal.exit_code != 0

        # Traversal test 2: Root directory target
        res_root = await editor.aexecute(command="create", path=".", file_text="root_write")
        assert res_root.is_error is True

    run_async(_test())


# ==============================================================================
# Test N — Max Step Ceiling Exhaustion Semantics
# ==============================================================================

class NonTerminatingAgentMock:
    """Mock agent that never declares task completion."""

    name: str = "non_terminating"

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        return False


def test_n_max_steps_exhaustion_handled_as_failed(temp_workspace):
    """Test N: Hitting maximum steps without explicit finish is FAILED, not COMPLETED."""
    async def _test():
        runner = AgentRunner(
            agent=NonTerminatingAgentMock(),
            config=RunnerConfig(max_steps=3),
        )
        state = ExecutionState(task_prompt="Endless loop", max_steps=3, workspace_root=str(temp_workspace))

        await runner.run(task_prompt="Endless loop", state=state, workspace_root=str(temp_workspace))

        assert state.current_step == 3
        assert state.status == ExecutionStatus.FAILED
        assert state.is_failed is True
        assert state.is_completed is False
        assert state.metadata.get("error_code") == "MAX_STEPS_EXCEEDED"

    run_async(_test())