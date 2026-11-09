# Path: backend/peldrun/engine/cancellation.py
"""
PELDRUN Core Cancellation Contract.

Centralizes the semantics of cancelling an in-flight agent run: how a
request is signalled, how the runner observes it, how child tasks are
drained without leaking, and how the state machine is transitioned
into CANCELLED exactly once.

Design invariants
-----------------

1. **Signal + drain are separate concerns.**
   ``request_cancel()`` only sets an ``asyncio.Event`` — it does not
   touch any ``Task``. Draining a task is an async operation
   performed by ``cancel_and_drain()`` at the call site (typically
   the runner). This separation makes the "signal" side synchronously
   callable from anywhere (HTTP handler, CLI, signal handler) without
   forcing that caller to hold the runner's task handle.

2. **No orphan tasks.**
   ``cancel_and_drain()`` guarantees that, upon return, the target
   task has either terminated or been logged as a leak. It awaits
   the task's actual termination — not just its ``cancel()`` call —
   within an explicit timeout. This is the direct fix for B9
   (``asyncio.shield(step_task)`` + ``break`` leaving a pending task
   that keeps running after the runner exits).

3. **No shielded wrapper leaks.**
   ``await_with_cancellation()`` uses ``asyncio.wait({task},
   timeout=...)`` instead of ``asyncio.wait_for(asyncio.shield(task),
   ...)``. ``wait_for`` on a shielded future leaves a dangling
   ``_GatheringFuture`` when the inner task hasn't finished — one
   per poll iteration. ``asyncio.wait`` creates no wrapper.

4. **Cancellation reasons are typed.**
   ``CancellationReason`` distinguishes operator-initiated from
   timeout- or shutdown-triggered cancellation. The reason is stored
   on the ``CancellationState`` and (in Phase F) is copied into the
   terminal event's payload so the UI can render "Cancelled by
   operator" vs "Step timeout" accurately.

5. **One request per run, ever.**
   ``CancellationRegistry.request()`` is idempotent: the first
   request wins, subsequent requests are silent no-ops. The reason
   recorded is the reason of the FIRST request — if the operator
   clicks Stop at the exact instant a step timeout fires, the UI
   reports "operator" rather than flipping mid-flight.

6. **Terminal state is not this module's job.**
   Cancellation knows how to *request* and *drain*. It does NOT emit
   terminal events, mark the state COMPLETED, or interact with SSE.
   ``lifecycle.mark_cancelled`` and ``terminal.TerminalEvent`` are
   called by the runner. Keeping the boundary crisp prevents the
   "who emits CANCELLED?" ambiguity that produced B13.

7. **Human-wait suspension is handled here.**
   When the agent is blocked in ``WAITING_FOR_INPUT``, the step
   timeout is suspended (the human may take minutes). This is
   currently handled ad-hoc in ``runner.py``'s polling loop; the
   ``human_wait_marker`` parameter of ``await_with_cancellation``
   makes it a first-class concern of the contract.

8. **No external event loop assumptions.**
   Every function that touches ``asyncio.Event`` or tasks is
   ``async`` or is called from within a running loop. The
   sync ``request_cancel()`` / ``is_cancelled()`` helpers are
   intentionally loop-agnostic: ``asyncio.Event.set()`` is safe from
   any thread once the loop is running, and this module uses it
   that way.

See also
--------

    * ``backend/peldrun/engine/state.py``     — the FSM this module
                                                 drives via lifecycle
    * ``backend/peldrun/engine/lifecycle.py`` — terminal-status marking
    * ``backend/peldrun/engine/runner.py``    — the caller that wires
                                                 cancel_event into
                                                 ``await_with_cancellation``
    * ``backend/peldrun/events/terminal.py``  — the CANCELLED event
                                                 that Phase F emits
                                                 after cancellation
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

from peldrun.engine.lifecycle import (
    is_terminal,
    mark_cancelled,
    mark_cancelling,
)
from peldrun.engine.state import ExecutionState

logger = logging.getLogger("peldrun.engine.cancellation")


# ---------------------------------------------------------------------------
# Cancellation reasons
# ---------------------------------------------------------------------------


class CancellationReason(str, Enum):
    """
    Semantic categorization of why a run was cancelled.

    The value is copied verbatim into the terminal event's payload so
    the UI can render an accurate label without re-parsing logs.
    """

    #: The operator pressed Stop (HTTP request, CLI command, UI click).
    OPERATOR = "operator"

    #: A step- or total-timeout expired.
    TIMEOUT = "timeout"

    #: The parent run (in a delegation chain) was cancelled.
    PARENT = "parent"

    #: The application is shutting down; all active runs are cancelled.
    SHUTDOWN = "shutdown"

    #: An internal subsystem decided to abort the run (auth failure,
    #: budget exhaustion, MCP handshake failure, ...).
    SYSTEM = "system"

    #: Token or cost budget exceeded (a specialized SYSTEM reason kept
    #: distinct for diagnostics).
    BUDGET_EXCEEDED = "budget_exceeded"


# ---------------------------------------------------------------------------
# Per-run cancellation state
# ---------------------------------------------------------------------------


@dataclass
class CancellationState:
    """
    Mutable per-run cancellation bookkeeping.

    Created lazily by ``CancellationRegistry.get_or_create()`` the
    first time either the runner or a cancel-issuing caller touches
    the run. Never shared between runs.
    """

    run_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: Optional[CancellationReason] = None
    requested_at: Optional[float] = None
    drained: bool = False

    def is_requested(self) -> bool:
        """Return True once ``request()`` has been called for this run."""
        return self.event.is_set()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CancellationRegistry:
    """
    Per-process map of ``run_id -> CancellationState``.

    One instance is created at module import and exposed via
    ``get_cancellation_registry()``. Callers use the module-level
    convenience functions (``request_cancel``, ``is_cancelled``,
    ``clear_run``) — not the class directly — so that swapping the
    backing implementation later requires no call-site changes.

    Thread-safety: the registry's dict operations are inherently safe
    under CPython's GIL for the operations used (get / set / pop /
    contains). ``asyncio.Event.set()`` is documented as safe from any
    thread once the loop is running. No additional locking.
    """

    def __init__(self) -> None:
        self._states: Dict[str, CancellationState] = {}

    def get_or_create(self, run_id: str) -> CancellationState:
        """Return the state for ``run_id``, creating it on first use."""
        state = self._states.get(run_id)
        if state is None:
            state = CancellationState(run_id=run_id)
            self._states[run_id] = state
        return state

    def request(
        self,
        run_id: str,
        reason: CancellationReason = CancellationReason.OPERATOR,
    ) -> CancellationState:
        """
        Request cancellation for ``run_id``.

        Idempotent: if a cancellation was already requested, this is a
        silent no-op — the original reason and timestamp are kept. The
        returned state is always the current one so callers can
        inspect ``reason`` regardless of which call won.
        """
        state = self.get_or_create(run_id)
        if not state.is_requested():
            state.reason = reason
            state.requested_at = time.time()
            state.event.set()
            logger.info(
                "Cancellation requested for run %r (reason=%s)",
                run_id,
                reason.value,
            )
        return state

    def is_cancelled(self, run_id: str) -> bool:
        """Return True if cancellation has been requested for ``run_id``."""
        state = self._states.get(run_id)
        return state is not None and state.is_requested()

    def get_reason(self, run_id: str) -> Optional[CancellationReason]:
        """Return the recorded reason for ``run_id``, or ``None``."""
        state = self._states.get(run_id)
        return state.reason if state is not None else None

    def get_event(self, run_id: str) -> asyncio.Event:
        """
        Return the ``asyncio.Event`` for ``run_id``, creating the
        state on first use.

        The runner registers this event once at the start of the run
        and polls it from ``await_with_cancellation``. Third-party
        callers can ``await event.wait()`` to be notified of
        cancellation without polling.
        """
        return self.get_or_create(run_id).event

    def mark_drained(self, run_id: str) -> None:
        """
        Record that the run has been drained (all child tasks
        terminated).

        Set by the runner after ``cancel_and_drain`` returns. Used by
        diagnostics to distinguish "cancelled but still winding down"
        from "fully terminated".
        """
        state = self._states.get(run_id)
        if state is not None:
            state.drained = True

    def clear(self, run_id: str) -> None:
        """
        Remove the state for ``run_id``.

        Called by the runner in its ``finally`` block so that a new
        run reusing the same job_id (rare but possible in tests) is
        not immediately cancelled by the previous run's flag.
        """
        self._states.pop(run_id, None)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """
        Return a JSON-safe snapshot of all tracked runs.

        Diagnostics only. Never includes the ``asyncio.Event`` object
        (not JSON-serializable).
        """
        return {
            rid: {
                "run_id": st.run_id,
                "is_requested": st.is_requested(),
                "reason": st.reason.value if st.reason else None,
                "requested_at": st.requested_at,
                "drained": st.drained,
            }
            for rid, st in self._states.items()
        }


_registry = CancellationRegistry()


def get_cancellation_registry() -> CancellationRegistry:
    """Return the process-wide cancellation registry singleton."""
    return _registry


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


def request_cancel(
    run_id: str,
    reason: CancellationReason = CancellationReason.OPERATOR,
) -> CancellationState:
    """Request cancellation for ``run_id``. See ``CancellationRegistry.request``."""
    return _registry.request(run_id, reason)


def is_cancelled(run_id: str) -> bool:
    """Return True if cancellation has been requested for ``run_id``."""
    return _registry.is_cancelled(run_id)


def get_cancel_event(run_id: str) -> asyncio.Event:
    """Return the cancellation event for ``run_id``."""
    return _registry.get_event(run_id)


def get_cancel_reason(run_id: str) -> Optional[CancellationReason]:
    """Return the recorded reason for ``run_id``, or ``None``."""
    return _registry.get_reason(run_id)


def clear_run(run_id: str) -> None:
    """Remove all cancellation bookkeeping for ``run_id``."""
    _registry.clear(run_id)


def request_cancel_for_state(
    state: ExecutionState,
    reason: CancellationReason = CancellationReason.OPERATOR,
) -> CancellationState:
    """
    Request cancellation AND transition the state into CANCELLING.

    Convenience entry point for callers that already hold the
    ``ExecutionState`` (typically an HTTP handler receiving the Stop
    request). The state transition is attempted but never fatal: if
    the state is already terminal, or if the FSM rejects the
    transition, the cancellation request still succeeds — the runner
    will observe the event on its next poll and terminate the loop.

    The runner itself uses ``request_cancel()`` directly (it drives
    the state transitions inside its own ``run`` loop, so it does
    not need this helper). External callers use this one.
    """
    cancel_state = _registry.request(state.run_id, reason)

    if not is_terminal(state):
        try:
            mark_cancelling(
                state,
                reason=f"Cancellation requested: {reason.value}",
            )
        except Exception as exc:
            # A failed state transition is never a reason to reject
            # the cancellation request. Log and let the runner handle
            # the state.
            logger.debug(
                "State transition to CANCELLING failed for run %r: %s",
                state.run_id,
                exc,
            )

    return cancel_state


# ---------------------------------------------------------------------------
# cancel_and_drain — the core primitive
# ---------------------------------------------------------------------------


async def cancel_and_drain(
    task: asyncio.Task,
    *,
    timeout: float = 5.0,
    context: str = "",
    swallow: bool = True,
) -> bool:
    """
    Cancel ``task`` and await its actual termination within ``timeout``.

    Guarantees
    ----------
    * **No orphan tasks.** On return, ``task`` is either done or
      has been logged as leaked. It is NOT left in a "cancelled but
      still running" state (the exact failure mode that
      ``asyncio.shield`` + ``break`` produces in B9).

    * **No exception propagation by default.** When ``swallow=True``
      (default), any exception the task raised during cancellation
      (including the expected ``CancelledError``) is suppressed and
      returned as ``False``. The caller is usually in a cleanup
      path and does not want to escalate a secondary error. Set
      ``swallow=False`` to re-raise non-cancellation exceptions.

    * **Idempotent.** Calling on an already-done task is a no-op
      that returns ``True``.

    Parameters
    ----------
    task
        The task to cancel. Must be an ``asyncio.Task`` (not a bare
        coroutine).
    timeout
        Maximum seconds to wait for the task to actually terminate
        after ``cancel()``. The task's own cancellation handler gets
        this long to run its cleanup.
    context
        Free-form label appended to log lines. Typically the caller
        name (``"runner.step"``) so a leaked-task warning points at
        the exact call site.
    swallow
        When ``True`` (default), non-cancellation exceptions raised
        by the task are logged and swallowed. When ``False``, they
        are re-raised after the task has terminated.

    Returns
    -------
    bool
        ``True`` if the task is confirmed terminated (either it was
        already done, or it finished within ``timeout`` after the
        cancel). ``False`` if the task failed to terminate within
        ``timeout`` — a leak that the caller should surface.

    Notes
    -----
    Python has no way to forcibly kill a task. A task that catches
    ``CancelledError`` and refuses to exit will survive past the
    ``timeout``. In that case we log an error with the context label
    and return ``False``. The caller (typically the runner) treats
    ``False`` as a fatal integrity violation and fails the run.
    """
    label = context or "cancel_and_drain"

    # Case 1 — already done. Consume the exception (if any) so the
    # event loop does not log "Task exception was never retrieved".
    if task.done():
        try:
            _exc = task.exception()
        except (asyncio.CancelledError, Exception):
            _exc = None
        return True

    # Case 2 — issue the cancellation.
    task.cancel()

    # Case 3 — wait for the task to terminate.
    #
    # ``asyncio.wait`` returns ``(done_set, pending_set)`` and never
    # raises on timeout. Crucially, it does NOT wrap the task in a
    # shield / gather future — no wrapper leaks even if the timeout
    # fires.
    try:
        done, pending = await asyncio.wait({task}, timeout=timeout)
    except Exception as wait_exc:
        # Defensive: ``asyncio.wait`` is not documented to raise, but
        # a broken event loop could still surprise us. Log and
        # report failure rather than propagating into a cleanup path.
        logger.error(
            "[%s] asyncio.wait raised while draining task %r: %s",
            label,
            task,
            wait_exc,
        )
        return False

    if pending:
        logger.error(
            "[%s] Task %r failed to terminate within %.1fs after "
            "cancel(). The task caught CancelledError and refused to "
            "exit. This is a task-level bug, not a cancellation "
            "failure; the run cannot be cleanly resumed.",
            label,
            task,
            timeout,
        )
        return False

    # Case 4 — task terminated. Inspect its outcome.
    if task.cancelled():
        # Expected: we just cancelled it.
        return True

    exc = None
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        # ``Task.exception()`` raises CancelledError when the task is
        # cancelled. We already handled that above, but the race
        # window between ``task.cancelled()`` and
        # ``task.exception()`` could theoretically produce this.
        return True
    except Exception as inspect_exc:
        logger.debug(
            "[%s] Task.exception() raised during inspection: %s",
            label,
            inspect_exc,
        )
        return True

    if exc is not None:
        if swallow:
            logger.debug(
                "[%s] Cancelled task raised non-cancellation "
                "exception (swallowed): %s",
                label,
                exc,
            )
        else:
            raise exc

    return True


# ---------------------------------------------------------------------------
# await_with_cancellation — the runner migration helper
# ---------------------------------------------------------------------------


async def await_with_cancellation(
    task: asyncio.Task,
    cancel_event: asyncio.Event,
    *,
    step_timeout: Optional[float] = None,
    poll_interval: float = 0.5,
    human_wait_marker: Optional[Callable[[], bool]] = None,
    drain_timeout: float = 5.0,
) -> Any:
    """
    Await ``task`` to completion while honoring cancellation and
    step-timeout boundaries.

    This is the correct replacement for the historical
    ``wait_for(shield(task), timeout=1.0)`` polling loop in
    ``runner.py``, which:

      * left a dangling ``_GatheringFuture`` per poll iteration
        (one shield wrapper per second, per step, per run);
      * called ``step_task.cancel()`` and then immediately
        ``break``-ed without awaiting — the task kept running after
        the runner exited (B9);
      * did not distinguish cancellation from timeout.

    Contract
    --------
    The function returns when ONE of the following happens:

    1. ``task`` finishes (success or exception). The task's result
       or exception is propagated to the caller via ``task.result()``
       / ``task.exception()`` semantics.

    2. ``cancel_event`` is set. The function cancels ``task`` (via
       ``cancel_and_drain``), then raises ``asyncio.CancelledError``.
       A ``CancelledError`` is used rather than a custom exception
       so that callers already using ``except asyncio.CancelledError``
       do not need a new branch.

    3. ``step_timeout`` expires. The function cancels ``task`` then
       raises ``asyncio.TimeoutError`` with a descriptive message.

    Human-wait suspension
    ---------------------
    When ``human_wait_marker`` returns ``True`` at the start of an
    iteration, the timeout clock is RESET. This preserves the
    pre-existing behavior where a step blocked in
    ``WAITING_FOR_INPUT`` does not hit the step timeout — the human
    operator can take as long as they like.

    Cancellation during human wait is still honored: the
    ``cancel_event`` is checked first, before the timer, so an
    operator Stop always wins.

    Race: task finished before cancel
    ---------------------------------
    If ``cancel_event`` becomes set on the same tick that ``task``
    finishes, ``task.result()`` is returned WITHOUT raising. This is
    the "cancellation was too late" case — the task's side effects
    are already committed, so discarding its result would lose
    information the caller (and the operator) needs. The runner
    still observes ``is_cancelled(run_id)`` and can emit CANCELLED
    even though the step completed; the two flags are independent.

    Parameters
    ----------
    task
        The step task. Must be a real ``asyncio.Task``.
    cancel_event
        Usually ``CancellationRegistry.get_event(run_id)``.
    step_timeout
        Per-step ceiling in seconds. ``None`` disables the ceiling.
    poll_interval
        How often to wake up and re-check cancellation / timeout.
        Defaults to 0.5s — a good balance between responsiveness
        (sub-second Stop) and CPU cost (2 wakeups/sec).
    human_wait_marker
        Optional callable returning True when the agent is
        currently in a ``WAITING_FOR_INPUT`` state. The timer is
        reset whenever this returns True.
    drain_timeout
        Forwarded to ``cancel_and_drain`` — how long the task's
        cancellation handler gets to run.

    Raises
    ------
    asyncio.CancelledError
        When ``cancel_event`` was set before ``task`` finished.
    asyncio.TimeoutError
        When ``step_timeout`` expired before ``task`` finished.
    Exception
        Whatever ``task`` raised, propagated unchanged.
    """
    start_time = time.monotonic()

    while not task.done():
        if cancel_event.is_set():
            # Race: did the task finish before we noticed?
            if task.done():
                # The step committed its side effects. Return the
                # result rather than discarding it.
                return task.result()

            reason = _registry.get_reason(_run_id_for_event(cancel_event)) or CancellationReason.OPERATOR
            drained = await cancel_and_drain(
                task,
                timeout=drain_timeout,
                context="await_with_cancellation.cancel",
            )
            if not drained:
                logger.error(
                    "Step task failed to drain during cancellation; "
                    "the task will continue running in the background "
                    "until it exits on its own."
                )
            raise asyncio.CancelledError(
                f"Step cancelled (reason={reason.value})"
            )

        # Human-wait suspension: reset the timeout clock while the
        # agent is waiting on a human response.
        if human_wait_marker is not None:
            try:
                if human_wait_marker():
                    start_time = time.monotonic()
            except Exception as marker_exc:
                logger.debug(
                    "human_wait_marker raised (ignored): %s", marker_exc
                )

        # Non-blocking wait for the task with a bounded timeout.
        # Unlike ``wait_for(shield(task))``, ``asyncio.wait`` does
        # NOT create a wrapper future, so there is no per-iteration
        # leak.
        try:
            done, _ = await asyncio.wait({task}, timeout=poll_interval)
        except Exception as wait_exc:
            logger.error(
                "asyncio.wait raised while polling step task: %s",
                wait_exc,
            )
            raise

        if done:
            break

        if step_timeout is not None:
            elapsed = time.monotonic() - start_time
            if elapsed >= step_timeout:
                drained = await cancel_and_drain(
                    task,
                    timeout=drain_timeout,
                    context="await_with_cancellation.timeout",
                )
                if not drained:
                    logger.error(
                        "Step task failed to drain during timeout."
                    )
                raise asyncio.TimeoutError(
                    f"Step exceeded timeout of {step_timeout:.1f}s "
                    f"(elapsed {elapsed:.1f}s)."
                )

    # Task finished — propagate its result or exception.
    return task.result()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_id_for_event(event: asyncio.Event) -> str:
    """
    Best-effort lookup of the run_id associated with a cancel event.

    Used only for enriching log messages in ``await_with_cancellation``
    — never for control flow. Returns an empty string when the event
    is not tracked in the registry (defensive: a caller using a
    locally-created ``asyncio.Event`` still works, they just lose
    the reason in the log line).
    """
    for run_id, st in _registry._states.items():
        if st.event is event:
            return run_id
    return ""


__all__ = [
    # Enum + dataclass
    "CancellationReason",
    "CancellationState",
    # Registry
    "CancellationRegistry",
    "get_cancellation_registry",
    # Sync convenience wrappers
    "clear_run",
    "get_cancel_event",
    "get_cancel_reason",
    "is_cancelled",
    "request_cancel",
    "request_cancel_for_state",
    # Core async primitives
    "await_with_cancellation",
    "cancel_and_drain",
]
