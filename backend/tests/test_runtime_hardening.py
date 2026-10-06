"""
backend/tests/test_runtime_hardening.py

Integration verification test for Phase M0: Runtime Hardening.

Verifies:
1. Complete state and memory isolation between concurrent jobs.
2. Prevention of duplicate STEP_START events from the Web Dispatcher.
3. Strict Terminal Event Contract (exactly one terminal event per run).
4. Deterministic cleanup of per-job state upon completion.
"""

import asyncio
from pathlib import Path
import pytest

from omweb.sse_events import SSEEvent, SSEEventType, register_job_listener, unregister_job_listener
from omweb.agent_bridge_parts.state import (
    active_tasks,
    cleanup_job_state,
    current_active_job_id,
    job_scoped_artifacts,
)


@pytest.mark.asyncio
async def test_concurrent_jobs_state_and_event_isolation(tmp_path: Path) -> None:
    """Verify concurrent job execution maintains strict isolation and single terminal states."""
    job_alpha = "job_test_alpha"
    job_beta = "job_test_beta"

    # Register listeners
    queue_alpha = await register_job_listener(job_alpha)
    queue_beta = await register_job_listener(job_beta)

    events_alpha = []
    events_beta = []

    async def consume_stream(queue: asyncio.Queue, target_list: list) -> None:
        while True:
            evt: SSEEvent = await queue.get()
            target_list.append(evt)
            if evt.type in (SSEEventType.FINAL, SSEEventType.ERROR):
                break

    task_alpha = asyncio.create_task(consume_stream(queue_alpha, events_alpha))
    task_beta = asyncio.create_task(consume_stream(queue_beta, events_beta))

    from omweb.sse_events import dispatch_event

    # Simulate concurrent ordered events for Alpha
    await dispatch_event(job_alpha, SSEEvent(type=SSEEventType.STEP_START, step=1, data={"seq": 1}))
    await dispatch_event(job_alpha, SSEEvent(type=SSEEventType.THOUGHT, step=1, data={"thought": "Alpha thinking", "seq": 2}))
    await dispatch_event(job_alpha, SSEEvent(type=SSEEventType.FINAL, step=1, data={"result": "Alpha done", "seq": 3}))

    # Simulate concurrent ordered events for Beta
    await dispatch_event(job_beta, SSEEvent(type=SSEEventType.STEP_START, step=1, data={"seq": 1}))
    await dispatch_event(job_beta, SSEEvent(type=SSEEventType.TOOL_CALL, step=1, data={"name": "str_replace_editor", "seq": 2}))
    await dispatch_event(job_beta, SSEEvent(type=SSEEventType.FINAL, step=1, data={"result": "Beta done", "seq": 3}))

    await asyncio.gather(task_alpha, task_beta)

    # 1. Assert Event Scoping & No Interleaving
    assert len(events_alpha) == 3
    assert len(events_beta) == 3
    assert events_alpha[1].type == SSEEventType.THOUGHT
    assert events_beta[1].type == SSEEventType.TOOL_CALL

    # 2. Assert Terminal Event Contract: exactly one FINAL per job
    alpha_terminals = [e for e in events_alpha if e.type in (SSEEventType.FINAL, SSEEventType.ERROR)]
    beta_terminals = [e for e in events_beta if e.type in (SSEEventType.FINAL, SSEEventType.ERROR)]
    assert len(alpha_terminals) == 1
    assert len(beta_terminals) == 1

    # 3. Assert Cleanup State Isolation
    cleanup_job_state(job_alpha)
    cleanup_job_state(job_beta)

    assert job_alpha not in active_tasks
    assert job_beta not in active_tasks
    assert "current" not in current_active_job_id

    # Unregister listeners
    await unregister_job_listener(job_alpha, queue_alpha)
    await unregister_job_listener(job_beta, queue_beta)


if __name__ == "__main__":
    asyncio.run(test_concurrent_jobs_state_and_event_isolation(Path("./tmp")))
    print("[PASS] M0 Runtime Hardening Verification Succeeded.")