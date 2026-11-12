# backend/omweb/memory/monitoring/token_accounting.py
"""
PELDRUN Memory Platform - Token Accounting Bridge.

Bridges the memory subsystem's per-run usage tracking (see
``memory_usage.py``) with the LLM invocation ledger
(``peldrun.runtime.usage_store``). Provides the two missing
pieces the plan's task 1.9.3 requires:

    1. **Registration.** When the dispatcher retrieves memory
       for a run, it registers the ``MemoryContext`` here.
       This simultaneously (a) writes a row to
       ``memory_usage_ledger`` via the tracker, and (b) records
       the ``used_tokens`` for ONE-SHOT attachment to the next
       LLM invocation.

    2. **Consumption.** When the executor builds the FIRST
       ``LLMInvocationRecord`` for the run, it pops the pending
       value. Subsequent invocations return ``None`` -- the
       memory block is injected ONCE per turn, not re-injected
       on every reasoning step. This matches the injection
       contract in P1.5's ``ContextManager.build_messages``.

Why a bridge
------------

The plan SEPARATES the memory subsystem (which knows what was
retrieved) from the LLM accounting subsystem (which knows what
was spent). The two are independent services:

    * MemoryService produces ``MemoryContext``s.
    * CoreRuntimeExecutor produces ``LLMInvocationRecord``s.

Neither imports the other. The bridge is the ONE integration
point that knows both -- it imports
``peldrun.runtime.usage_store`` (to shape records) and
``memory_usage`` (to persist usage). If either subsystem needs
to evolve independently, only the bridge changes.

Design -- one-shot consumption
------------------------------

The bridge maintains an in-memory dict::

    _pending: Dict[run_id, memory_tokens_used]

Operations:

    record_retrieval
        Dispatcher calls this after a successful retrieval.
        Writes the ledger row AND registers the pending
        value.

    take_memory_tokens
        Executor calls this when building the invocation
        record. First call returns the value and REMOVES the
        dict entry; subsequent calls return None.

    peek_memory_tokens
        Non-destructive read. Used by diagnostics and tests.

    clear_run
        Cleanup. Called by the executor's ``finally`` block so
        a run's pending entry never outlives it.

The dict is process-local (matches the singleton pattern of
``MemoryService`` and the tracker). It is NOT persisted -- if
the process crashes after a retrieval but before the first
LLM call, the ``MemoryUsage`` ledger row is still there, so
the data is not lost; only the field-level attribution to
that specific invocation is missing. Acceptable -- the
ledger is the source of truth, and the field-level
attribution is a convenience for per-invocation drill-downs.

Why the ledger row AND the field-level attribution
--------------------------------------------------

Both are useful; they are NOT redundant:

    * **Ledger row** (``memory_usage_ledger``) answers "run
      123 used 320 memory tokens total, split 200 user / 120
      project / 0 chat / 0 agent". Used by the aggregated
      dashboard.

    * **Per-invocation field** (``memory_tokens_used`` in
      ``token_ledger.metadata_json``) answers "the first LLM
      call of run 123 spent 320 of its 1500 input tokens on
      memory; step 2 spent 0; step 3 spent 0". Used by
      per-step drill-downs.

The bridge ensures the two are consistent: the SAME
``MemoryContext.used_tokens`` is both written to the ledger
and carried into the first invocation record.

Thread safety
-------------

The bridge uses ``threading.Lock`` around every dict
operation. Although the dispatcher and executor are typically
single-threaded per run, tests and future multi-worker
deployments can produce concurrent access. ``dict.pop(key)``
in CPython is atomic, but the surrounding control flow
(check-then-act) is not -- the lock closes that window.

The lock is NOT an asyncio.Lock. None of the bridge
operations are ``async`` except ``record_retrieval`` (which
needs to await the tracker). Adding ``async`` to the
synchronous methods would force every caller to await a
microsecond-scale operation for no benefit.

Failure isolation
-----------------

``record_retrieval`` is BEST-EFFORT at every layer:

    * Tracker failures log at WARNING and return False.
    * Pending registration cannot fail (pure dict write).
    * The run is NOT aborted by any accounting failure --
      memory is an enhancement, not a critical-path resource.

The dispatcher's call site (P1.5) already wraps this in a
try/except for defense in depth, matching the same
philosophy at the dispatcher level.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from peldrun.memory.domain import MemoryContext

from omweb.memory.monitoring.memory_usage import (
    MemoryUsageTracker,
    get_memory_usage_tracker,
)


logger = logging.getLogger(
    "omweb.memory.monitoring.token_accounting"
)


# ---------------------------------------------------------------------------
# MemoryAccountingBridge
# ---------------------------------------------------------------------------


class MemoryAccountingBridge:
    """Bridges ``MemoryContext`` retrieval with ``LLMInvocationRecord``.

    See the module docstring for the full design rationale.

    Lifecycle
    ---------
    Instantiate ONCE per process (use
    ``get_memory_accounting_bridge``). Callers use the singleton
    accessor, not the constructor, so that every subsystem
    (dispatcher, executor, tests) sees the same pending map.

    Direct construction is supported for isolated tests:

        tracker = MemoryUsageTracker(store=temp_store)
        bridge = MemoryAccountingBridge(tracker=tracker)
    """

    def __init__(self, tracker: MemoryUsageTracker) -> None:
        if tracker is None:
            raise TypeError(
                "tracker must be a MemoryUsageTracker instance, "
                "got None"
            )
        if not isinstance(tracker, MemoryUsageTracker):
            raise TypeError(
                f"tracker must be MemoryUsageTracker, got "
                f"{type(tracker).__name__}"
            )
        self._tracker = tracker
        self._pending: Dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration (dispatcher path)
    # ------------------------------------------------------------------

    async def record_retrieval(
        self,
        *,
        run_id: str,
        context: MemoryContext,
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
        retrieval_tokens: int = 0,
        overhead_tokens: int = 0,
    ) -> bool:
        """Record the retrieval AND register pending memory tokens.

        Called by the dispatcher (P1.5) exactly ONCE per run,
        immediately after ``MemoryService.retrieve`` returns
        a non-empty context.

        The ledger write and the pending registration are
        INDEPENDENT operations, executed sequentially:

          1. Ledger write via the tracker. A failure here
             logs and returns ``False``, but does NOT prevent
             the pending registration -- the executor's
             ``memory_tokens_used`` field should still
             reflect what the model actually received, even
             if the monitoring ledger is broken.

          2. Pending registration. Only fires when the
             context is non-empty AND carries a nonzero
             ``used_tokens``. The checks avoid registering a
             "0 memory tokens" pending value that would
             pollute the invocation record with a spurious
             field.

        Parameters
        ----------
        run_id
            The run identifier. Must match the value the
            executor will pass to ``take_memory_tokens``.
            Trimmed; empty raises no exception but returns
            ``False`` (a warning is logged).

        context
            The ``MemoryContext`` returned by
            ``MemoryService.retrieve``.

        chat_id, project_id
            Optional attribution -- forwarded to the ledger
            row so the aggregated dashboard can filter by
            chat or project without a join.

        retrieval_tokens, overhead_tokens
            Additional ledger fields. See
            ``MemoryUsageTracker.record``'s docstring for
            what each represents.

        Returns
        -------
        bool
            True when the ledger row was written; False
            otherwise. Does NOT indicate whether the pending
            registration succeeded (that is silent and always
            best-effort).
        """
        if not isinstance(run_id, str) or not run_id.strip():
            logger.warning(
                "MemoryAccountingBridge.record_retrieval: empty "
                "run_id; skipping."
            )
            return False
        if not isinstance(context, MemoryContext):
            logger.warning(
                "MemoryAccountingBridge.record_retrieval: context "
                "is not a MemoryContext (got %s); skipping.",
                type(context).__name__,
            )
            return False

        clean_run_id = run_id.strip()

        # ---- 1. Ledger write (best-effort) ----
        ledger_written = False
        try:
            ledger_written = await self._tracker.record(
                run_id=clean_run_id,
                context=context,
                chat_id=chat_id,
                project_id=project_id,
                retrieval_tokens=retrieval_tokens,
                overhead_tokens=overhead_tokens,
            )
        except Exception as exc:
            logger.warning(
                "MemoryAccountingBridge.record_retrieval: tracker "
                "raised for run %r: %s",
                clean_run_id,
                exc,
            )

        # ---- 2. Pending registration (silent) ----
        if not context.is_empty and context.used_tokens > 0:
            with self._lock:
                self._pending[clean_run_id] = int(
                    context.used_tokens
                )
            logger.debug(
                "MemoryAccountingBridge: registered %d memory "
                "tokens as pending for run %r.",
                context.used_tokens,
                clean_run_id,
            )
        else:
            logger.debug(
                "MemoryAccountingBridge: no pending memory for "
                "run %r (is_empty=%s, used_tokens=%d).",
                clean_run_id,
                context.is_empty,
                context.used_tokens,
            )

        return ledger_written

    # ------------------------------------------------------------------
    # Consumption (executor path)
    # ------------------------------------------------------------------

    def take_memory_tokens(self, run_id: str) -> Optional[int]:
        """Pop and return the pending memory tokens for ``run_id``.

        Called by the executor when it builds the FIRST
        ``LLMInvocationRecord`` for the run. The call is
        ONE-SHOT: the value is removed from the pending map
        and subsequent calls return ``None``.

        Returns ``None`` when:

          * no retrieval happened for this run,
          * the retrieval produced an empty context,
          * the value was already consumed (a later LLM
            invocation).

        All three cases are indistinguishable to the caller
        -- which is CORRECT: the invocation did not carry a
        memory block, so its ``memory_tokens_used`` should be
        ``None`` (not ``0``, which would look like "0 tokens
        of memory were injected").
        """
        if not isinstance(run_id, str) or not run_id.strip():
            return None
        with self._lock:
            return self._pending.pop(run_id.strip(), None)

    def peek_memory_tokens(self, run_id: str) -> Optional[int]:
        """Return the pending memory tokens WITHOUT consuming.

        Used by diagnostics and tests. Production code paths
        use ``take_memory_tokens`` -- the value must be
        consumed exactly once so that subsequent invocations
        correctly see ``None``.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            return None
        with self._lock:
            return self._pending.get(run_id.strip())

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def clear_run(self, run_id: str) -> None:
        """Remove any pending state for ``run_id``.

        Called by the executor in its ``finally`` block so
        that a run's pending entry (if it was never consumed)
        does not linger for the process lifetime. Idempotent.
        """
        if not isinstance(run_id, str):
            return
        with self._lock:
            self._pending.pop(run_id.strip(), None)

    def pending_count(self) -> int:
        """Return the number of runs with un-consumed pending tokens.

        Diagnostic. A growing value indicates runs that
        registered memory but whose executor never called
        ``take_memory_tokens`` -- typically a shutdown or
        exception path that skipped the normal finally block.
        """
        with self._lock:
            return len(self._pending)

    def pending_run_ids(self) -> List[str]:
        """Return a snapshot of pending run IDs (diagnostic).

        Ordering is not guaranteed. Callers use this only for
        logging and tests, never for control flow.
        """
        with self._lock:
            return list(self._pending.keys())


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_bridge: Optional[MemoryAccountingBridge] = None
_bridge_lock = threading.Lock()


def get_memory_accounting_bridge() -> MemoryAccountingBridge:
    """Return the process-wide ``MemoryAccountingBridge``.

    Lazily initializes on first call with the singleton tracker.
    Every subsystem (dispatcher, executor, tests) uses this
    accessor so they all share the same pending map.

    Raises
    ------
    RuntimeError
        Only when the singleton tracker itself is broken (a
        storage initialization failure during lazy init). This
        is a genuine startup error -- the caller should have
        initialized the memory subsystem first.
    """
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = MemoryAccountingBridge(
                tracker=get_memory_usage_tracker()
            )
        return _bridge


def reset_accounting_bridge_for_tests() -> None:
    """Reset the singleton. Test-only helper.

    Not exported via ``__all__`` -- production code should never
    reset the bridge mid-process. Tests that want an isolated
    bridge construct ``MemoryAccountingBridge`` directly with an
    isolated tracker.
    """
    global _bridge
    with _bridge_lock:
        _bridge = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryAccountingBridge",
    "get_memory_accounting_bridge",
]
