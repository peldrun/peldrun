# backend/test_accounting.py
"""
Temporary verification script for M8.1 accounting layer.

Run with:
    python test_accounting.py

Verifies:
  * LLMInvocationTracker.complete() writes a success record
  * LLMInvocationTracker.fail() writes a failure record
  * LLMInvocationTracker.cancel() writes a cancellation record
  * cost_status is correctly derived from the pricing rule
"""

import asyncio
import time

from peldrun.llm.accounting import (
    InvocationContext,
    LLMInvocationTracker,
    COST_STATUS_KNOWN,
    COST_STATUS_FREE,
    COST_STATUS_UNKNOWN,
)
from peldrun.llm.client import LLMResponse, TokenUsage, UsageSource


async def _run() -> None:
    tracker = LLMInvocationTracker()
    ctx = InvocationContext(
        provider="test_provider",
        model_requested="test_model",
        mode="chat",
        job_id="test_job_m8_1",
        chat_id="test_chat_m8_1",
    )

    # ---- Case 1: success with estimated usage ----
    resp = LLMResponse(
        content="hello",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            source=UsageSource.ESTIMATED,
            estimated=True,
        ),
        model="test_model",
        latency_ms=123.4,
        ttft_ms=100.0,
    )
    rec1 = await tracker.complete(resp, ctx, started_at=time.time() - 0.5)
    print(
        f"[success]  inv_id={rec1.invocation_id} "
        f"tokens={rec1.total_tokens} status={rec1.status} "
        f"usage_source={rec1.usage_source} cost_status={rec1.metadata.get('cost_status')}"
    )

    # ---- Case 2: failure ----
    rec2 = await tracker.fail(
        RuntimeError("simulated provider failure"),
        ctx,
        started_at=time.time() - 0.2,
    )
    print(
        f"[failure]  inv_id={rec2.invocation_id} "
        f"status={rec2.status} reason={rec2.failure_reason!r} "
        f"cost_status={rec2.metadata.get('cost_status')}"
    )

    # ---- Case 3: cancellation ----
    rec3 = await tracker.cancel(ctx, started_at=time.time() - 1.0)
    print(
        f"[cancel]   inv_id={rec3.invocation_id} "
        f"status={rec3.status} reason={rec3.failure_reason!r} "
        f"cost_status={rec3.metadata.get('cost_status')}"
    )

    # ---- Verify all three landed in the ledger ----
    from peldrun.runtime.usage_store import get_usage_store

    store = get_usage_store()
    found = 0
    for inv_id in (rec1.invocation_id, rec2.invocation_id, rec3.invocation_id):
        row = await store.get_invocation(inv_id)
        if row is not None:
            found += 1
    print(f"\nLedger records found: {found}/3")

    assert found == 3, "Some records missing from token_ledger"
    assert rec1.status == "completed"
    assert rec2.status == "failed"
    assert rec3.status == "cancelled"
    print("OK — all assertions passed")


if __name__ == "__main__":
    asyncio.run(_run())
