# backend/test_p2_coordinator_fixes.py
"""P2 verification: child_run_count release + approval polling backoff."""

import asyncio
import inspect
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))


async def main() -> None:
    all_ok = True

    # ------------------------------------------------------------------
    # Case 1: _delegate_inner exists (refactor proof).
    # ------------------------------------------------------------------
    from peldrun.agents.multi.coordinator import MultiAgentCoordinator
    ok = hasattr(MultiAgentCoordinator, "_delegate_inner")
    print(f"[{'PASS' if ok else 'FAIL'}] _delegate_inner method exists")
    all_ok = all_ok and ok

    # ------------------------------------------------------------------
    # Case 2: _child_run_count decremented after failed delegation.
    # ------------------------------------------------------------------
    from peldrun.events.emitter import EventEmitter
    from peldrun.agents.multi.protocol import DelegatedTask

    coord = MultiAgentCoordinator(
        parent_emitter=EventEmitter(run_id="t"),
        workspace_root=".",
    )

    # Pre-register NO specialists so _resolve_specialist_instance returns
    # None, causing the delegate to fail EARLY (after counter increment).
    # Then verify the counter is back to 0.
    initial = coord._child_run_count
    task = DelegatedTask(
        parent_run_id="parent",
        target_agent_id="nonexistent",
        instruction="never runs",
    )

    try:
        _ = await coord.delegate(task)
    except Exception:
        pass

    after = coord._child_run_count
    ok = initial == 0 and after == 0
    print(f"[{'PASS' if ok else 'FAIL'}] _child_run_count released after failure (initial={initial}, after={after})")
    all_ok = all_ok and ok

    # ------------------------------------------------------------------
    # Case 3: multiple failed delegations don't saturate the counter.
    # ------------------------------------------------------------------
    coord2 = MultiAgentCoordinator(
        parent_emitter=EventEmitter(run_id="t"),
        workspace_root=".",
    )

    # Budget with max_child_runs=3 (default), fire 5 failures.
    tasks = [
        DelegatedTask(
            parent_run_id="parent",
            target_agent_id="nonexistent",
            instruction=f"attempt {i}",
        )
        for i in range(5)
    ]
    for t in tasks:
        try:
            await coord2.delegate(t)
        except Exception:
            pass

    after = coord2._child_run_count
    ok = after == 0
    print(f"[{'PASS' if ok else 'FAIL'}] 5 failed delegations → counter={after} (expect 0)")
    all_ok = all_ok and ok

    # ------------------------------------------------------------------
    # Case 4: approval polling uses backoff (source scan).
    # ------------------------------------------------------------------
    src = Path("peldrun/agents/multi/coordinator.py").read_text(
        encoding="utf-8"
    )
    has_backoff = "_POLL_INTERVALS_S" in src
    has_marker = "P2 FIX — Approval polling backoff" in src
    ok = has_backoff and has_marker
    print(f"[{'PASS' if ok else 'FAIL'}] approval polling uses exponential backoff")
    all_ok = all_ok and ok

    # ------------------------------------------------------------------
    # Case 5: verify _handle_approval_gate polls fewer than 15 times in
    #         ~2 seconds against a mock store that never resolves.
    # ------------------------------------------------------------------
    from peldrun.agents.multi.coordinator import MultiAgentCoordinator as C
    from peldrun.agents.multi.protocol import DelegatedTask as DT

    poll_count = [0]

    class SlowStore:
        async def save_human_request(self, req):
            pass

        async def get_human_request(self, rid):
            poll_count[0] += 1
            class R:
                status = type("S", (), {"value": "pending"})()
                answer = None
            return R()

    coord3 = C(
        parent_emitter=EventEmitter(run_id="t"),
        workspace_root=".",
        run_store=SlowStore(),
    )

    task3 = DT(
        parent_run_id="parent",
        target_agent_id="specialist",
        instruction="test",
    )

    start = time.time()
    # 2-second timeout — old code would poll ~40 times, new ~4-5.
    _ = await coord3._handle_approval_gate(task3, timeout_seconds=2.0)
    elapsed = time.time() - start
    # With backoff: 0.05+0.1+0.2+0.4+0.8+1.0 ≈ 2.55s → around 4-5 polls
    # With old polling: 40 polls in 2s.
    ok = poll_count[0] <= 15 and elapsed >= 1.8
    print(f"[{'PASS' if ok else 'FAIL'}] approval polling backoff: {poll_count[0]} polls in {elapsed:.2f}s (expect <=15)")
    all_ok = all_ok and ok

    print("\nRESULT:", "ALL PASS" if all_ok else "SOME FAILED")


if __name__ == "__main__":
    asyncio.run(main())
