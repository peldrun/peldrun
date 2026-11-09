# Path: backend/peldrun/engine/lifecycle.py
"""
PELDRUN Core Execution Lifecycle Authority.

Centralizes the legal-transition rules, status predicates, and
semantic marking helpers that the entire runtime (AgentRunner,
CoreRuntimeExecutor, RecoveryManager, SSE bridge) uses to drive an
``ExecutionState`` through its lifecycle.

Design invariants
-----------------

1. **Single mutation entry point.** All callers transition state via
   ``lifecycle.transition(state, target, reason)``. Direct calls to
   ``state.transition_to(...)`` remain technically possible (the
   ``ExecutionState.__setattr__`` FSM guard still fires), but new
   code MUST use this module so that semantic guards added in later
   phases (F, G2) are enforced uniformly across every layer.

2. **No FSM duplication.** The actual transition matrix lives in
   ``peldrun.engine.state.VALID_TRANSITIONS`` — one definition, one
   source of truth. This module wraps it, exposes it via
   ``can_transition()``, and layers semantic predicates on top. It
   never re-derives the allowed transitions from scratch.

3. **Semantic status sets are canonical.** ``TERMINAL_STATUSES``,
   ``ACTIVE_STATUSES``, ``WAITING_STATUSES``, ``PENDING_STATUSES``,
   ``RESUMABLE_STATUSES`` are defined ONCE here and imported
   everywhere else. No layer may re-derive "is this terminal?" with
   ad-hoc ``status == COMPLETED or status == FAILED or ...`` checks —
   those drift silently when a new status is added.

4. **INTERRUPTED is a forward-compatible shim.** The canonical
   contract (per the PELDRUN execution repair plan) requires a
   distinct ``INTERRUPTED`` state for crash recovery. The current
   ``ExecutionStatus`` enum does not define one; Phase G2 will add
   it. Until then, ``mark_interrupted()`` sets a canonical
   ``metadata["interrupted"]`` marker AND transitions to FAILED (or
   CANCELLED when the current status has no direct path to FAILED).
   Callers written against ``is_interrupted(state)`` and
   ``mark_interrupted(state)`` keep working unchanged after Phase G2
   replaces the shim — both observe the metadata marker, which is
   preserved across the upgrade.

5. **Terminal states are truly terminal.** ``transition()`` refuses
   to re-enter a terminal state (raising
   ``InvalidStateTransitionError``), and the ``mark_*`` helpers
   short-circuit when the state is already terminal — the FIRST
   terminal status wins. This is the belt-and-suspenders fix for the
   "COMPLETED then CANCELLED" double-terminal bug (B13 in the plan),
   independent of whatever guards state.py adds in the future.

6. **Idempotent semantics.** Calling ``mark_cancelled()`` twice,
   or ``mark_failed()`` twice, is a no-op after the first. The
   original terminal status and error message are preserved. This
   makes race conditions (user clicks Stop + step timeout fires at
   the same instant) safe without every caller needing its own
   ``if is_terminal(state): return`` guard.

See also
--------

    * ``backend/peldrun/engine/state.py``     — FSM matrix + ExecutionState
    * ``backend/peldrun/engine/runner.py``    — the sole caller that drives
                                                 the run loop
    * ``backend/peldrun/runtime/executor.py`` — consumes ``is_terminal`` to
                                                 decide RunResult status
    * ``backend/peldrun/engine/cancellation.py`` (Phase A, next) — the
                                                 cancellation contract that
                                                 calls ``mark_cancelled``
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, FrozenSet, Optional, Union

from peldrun.engine.state import (
    VALID_TRANSITIONS,
    ExecutionState,
    ExecutionStatus,
    InvalidStateTransitionError,
)

logger = logging.getLogger("peldrun.engine.lifecycle")


# ---------------------------------------------------------------------------
# Canonical status sets
# ---------------------------------------------------------------------------
#
# These are the ONLY definitions of "terminal", "active", "waiting",
# "pending", and "resumable" in the entire runtime. Every other layer
# MUST import from here. If a new status is added in Phase G2, exactly
# one place changes and every consumer picks it up automatically.

#: Runs that have reached an immutable end. No further transitions are
#: legal: ``transition()`` raises, ``mark_*`` helpers short-circuit.
TERMINAL_STATUSES: FrozenSet[ExecutionStatus] = frozenset({
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
})

#: Runs that represent "the engine is doing work right now". A UI
#: polling a run in one of these states should keep the spinner on.
ACTIVE_STATUSES: FrozenSet[ExecutionStatus] = frozenset({
    ExecutionStatus.STARTING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.RETRYING,
    ExecutionStatus.CANCELLING,
})

#: Runs that are alive but not actively progressing. Typically waiting
#: on external input (human or an explicit resume).
WAITING_STATUSES: FrozenSet[ExecutionStatus] = frozenset({
    ExecutionStatus.WAITING_FOR_INPUT,
    ExecutionStatus.WAITING_FOR_HUMAN,  # legacy alias from state.py
    ExecutionStatus.PAUSED,
})

#: Runs that were never started, or are being prepared. The execution
#: repair plan calls this "PENDING". state.py splits it into QUEUED /
#: IDLE / STARTING; all three are semantically equivalent here.
PENDING_STATUSES: FrozenSet[ExecutionStatus] = frozenset({
    ExecutionStatus.QUEUED,
    ExecutionStatus.IDLE,
    ExecutionStatus.STARTING,
})

#: Runs that can be safely handed to ``RecoveryManager.resume()``
#: without losing user-visible progress. Waiting-for-input is
#: explicitly included: if the process crashed while the agent was
#: waiting for a human response, resuming must restore the pending
#: request rather than abandoning it.
RESUMABLE_STATUSES: FrozenSet[ExecutionStatus] = frozenset({
    ExecutionStatus.WAITING_FOR_INPUT,
    ExecutionStatus.WAITING_FOR_HUMAN,
})

#: Marker written into ``state.metadata["error_code"]`` when a run is
#: interrupted by a process restart. Kept here (not in state.py) so
#: Phase G2 can replace the shim with a real ``INTERRUPTED`` status
#: without touching this constant — callers continue to observe it
#: through ``is_interrupted()``.
INTERRUPTED_ERROR_CODE: str = "INTERRUPTED_BY_RESTART"

#: Marker written into ``state.metadata["interrupted"]`` when
#: ``mark_interrupted()`` runs. Remains True even after Phase G2 so
#: existing persisted checkpoints stay interpretable, and so that
#: ``is_interrupted()`` produces stable results across the upgrade.
_INTERRUPTED_METADATA_KEY: str = "interrupted"


# ---------------------------------------------------------------------------
# Status predicates (accept ExecutionStatus | str)
# ---------------------------------------------------------------------------


def _coerce_status(value: Union[ExecutionStatus, str]) -> ExecutionStatus:
    """
    Coerce ``value`` into an ``ExecutionStatus`` enum member.

    Raises ``ValueError`` when ``value`` is neither an
    ``ExecutionStatus`` instance nor a valid status string. Callers
    that cannot tolerate the exception should use the corresponding
    ``try_*`` helper or the ``can_transition`` guard.
    """
    if isinstance(value, ExecutionStatus):
        return value
    return ExecutionStatus(value)


def is_terminal_status(status: Union[ExecutionStatus, str]) -> bool:
    """Return True if ``status`` is one of the terminal states."""
    return _coerce_status(status) in TERMINAL_STATUSES


def is_active_status(status: Union[ExecutionStatus, str]) -> bool:
    """Return True if ``status`` represents active progress."""
    return _coerce_status(status) in ACTIVE_STATUSES


def is_waiting_status(status: Union[ExecutionStatus, str]) -> bool:
    """Return True if ``status`` is a non-terminal waiting state."""
    return _coerce_status(status) in WAITING_STATUSES


def is_pending_status(status: Union[ExecutionStatus, str]) -> bool:
    """Return True if ``status`` is a pre-run (pending) state."""
    return _coerce_status(status) in PENDING_STATUSES


def is_resumable_status(status: Union[ExecutionStatus, str]) -> bool:
    """Return True if ``status`` can be handed to ``RecoveryManager``."""
    return _coerce_status(status) in RESUMABLE_STATUSES


# ---------------------------------------------------------------------------
# Lifecycle state predicates (accept ExecutionState, not raw status)
# ---------------------------------------------------------------------------


def is_terminal(state: ExecutionState) -> bool:
    """Return True if the state has reached an immutable end."""
    return state.status in TERMINAL_STATUSES


def is_active(state: ExecutionState) -> bool:
    """Return True if the state is actively progressing."""
    return state.status in ACTIVE_STATUSES


def is_waiting(state: ExecutionState) -> bool:
    """Return True if the state is waiting on external input."""
    return state.status in WAITING_STATUSES


def is_pending(state: ExecutionState) -> bool:
    """Return True if the state has not yet begun execution."""
    return state.status in PENDING_STATUSES


def is_interrupted(state: ExecutionState) -> bool:
    """
    Return True if ``state`` represents a run interrupted by a restart.

    Phase A shim: reads ``state.metadata["interrupted"]``, which
    ``mark_interrupted()`` always sets. Phase G2 will additionally
    consult ``ExecutionStatus.INTERRUPTED`` once the status exists,
    but the metadata marker will remain set for backward compatibility
    with checkpoints persisted before Phase G2.
    """
    return bool(state.metadata.get(_INTERRUPTED_METADATA_KEY))


def is_resumable(state: ExecutionState) -> bool:
    """Return True if the state can be resumed without loss of progress."""
    return state.status in RESUMABLE_STATUSES or is_interrupted(state)


# ---------------------------------------------------------------------------
# Transition API
# ---------------------------------------------------------------------------


def can_transition(
    state: ExecutionState, target: Union[ExecutionStatus, str]
) -> bool:
    """
    Return True if ``state`` can legally transition to ``target``.

    Pure check — never mutates state. Used by RecoveryManager to
    decide whether a resume path is available, and by UI layers to
    enable / disable action buttons.

    Rules (in order):
      1. No-op transitions (``target == current``) are always True —
         idempotent marking is legal everywhere.
      2. Any transition FROM a terminal state is False, regardless of
         whether state.py's matrix would allow it. Defense in depth.
      3. Otherwise, defer to ``VALID_TRANSITIONS``.
    """
    try:
        target_status = _coerce_status(target)
    except ValueError:
        return False

    if state.status == target_status:
        return True

    if state.status in TERMINAL_STATUSES:
        return False

    return target_status in VALID_TRANSITIONS.get(state.status, set())


def transition(
    state: ExecutionState,
    target: Union[ExecutionStatus, str],
    reason: str = "",
) -> None:
    """
    Canonical lifecycle transition.

    Validates against the FSM matrix, appends the transition to the
    state's audit ledger, and updates ``updated_at``. Raises
    ``InvalidStateTransitionError`` on any illegal move.

    Callers MUST use this function — not ``state.transition_to()``
    directly — so that future semantic guards (Phase F) apply without
    a separate migration.
    """
    target_status = _coerce_status(target)

    # No-op transitions are legal and idempotent. Return early WITHOUT
    # appending a redundant history entry.
    if state.status == target_status:
        return

    # Explicit terminal guard. state.py's matrix already enforces this
    # (terminal statuses have empty transition sets), but making the
    # rule explicit here means it survives any future edit to the
    # matrix that might accidentally add a terminal-to-terminal edge.
    if state.status in TERMINAL_STATUSES:
        raise InvalidStateTransitionError(
            f"Cannot transition from terminal status "
            f"'{state.status.value}' to '{target_status.value}'. "
            f"Reason: {reason or 'unspecified'}"
        )

    state.transition_to(target_status, reason=reason)


def try_transition(
    state: ExecutionState,
    target: Union[ExecutionStatus, str],
    reason: str = "",
) -> bool:
    """
    Attempt a transition, returning ``True`` on success and ``False``
    on failure instead of raising.

    Intended for recovery paths and "best-effort" transitions where a
    failed transition must not abort the caller (e.g., promoting an
    interrupted run to CANCELLED when the user later clicks Stop, or
    promoting a stale RUNNING state to FAILED during orphan detection).

    Swallows both ``InvalidStateTransitionError`` (illegal move) and
    ``ValueError`` (invalid target status string).
    """
    try:
        transition(state, target, reason=reason)
        return True
    except (InvalidStateTransitionError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Semantic marking helpers
# ---------------------------------------------------------------------------
#
# These wrap ``transition()`` with the exact reason strings and
# metadata bookkeeping the runtime expects. Callers SHOULD use these
# instead of hand-rolling ``state.mark_*()`` calls, so that when
# Phase F tightens the guards (e.g., "mark_completed() requires
# proof of successful terminate") every layer inherits the stricter
# behavior automatically.


def mark_running(state: ExecutionState, reason: str = "") -> None:
    """Transition a pending run into RUNNING."""
    transition(state, ExecutionStatus.RUNNING, reason=reason)


def mark_waiting_for_input(
    state: ExecutionState,
    reason: str = "",
    request_id: Optional[str] = None,
) -> None:
    """
    Transition into WAITING_FOR_INPUT and record the pending request
    identifier on the state so that a restart can restore the exact
    future binding (Phase G3).
    """
    transition(state, ExecutionStatus.WAITING_FOR_INPUT, reason=reason)
    if request_id:
        state.metadata["pending_human_request_id"] = request_id


def mark_resumed_from_input(
    state: ExecutionState, reason: str = ""
) -> None:
    """
    Transition from WAITING_FOR_INPUT back into RUNNING after the
    human operator provided an answer. Clears the pending-request
    marker so subsequent checkpoints do not advertise a stale id.
    """
    transition(
        state,
        ExecutionStatus.RUNNING,
        reason=reason or "Human input resolved",
    )
    state.metadata.pop("pending_human_request_id", None)


def mark_paused(state: ExecutionState, reason: str = "") -> None:
    """Transition into PAUSED (operator requested an inter-step pause)."""
    transition(state, ExecutionStatus.PAUSED, reason=reason)


def mark_cancelling(state: ExecutionState, reason: str = "") -> None:
    """
    Transition into CANCELLING. Used when the cancellation signal has
    been received but the runner has not yet confirmed that all
    sub-tasks and pending tool calls have been drained.

    [F.6 FIX — B13 follow-up] Idempotent on terminal state.
    The historical implementation called ``transition()`` directly,
    which raises ``InvalidStateTransitionError`` on any transition
    FROM a terminal state. Real-world impact: if an outer cancellation
    arrived after the state had already become FAILED (e.g. the user
    clicked Stop just as an internal error fired), ``mark_cancelling``
    raised, ``mark_cancelled`` and ``emit_cancelled`` were never
    reached, and the frontend received CANCELLED at the executor
    layer despite the state being FAILED — an inconsistency that
    confused operators reading the logs.

    New behavior: when the state is already terminal, this is a
    silent no-op. The previously-terminal status (COMPLETED, FAILED,
    or CANCELLED) is preserved. Callers can safely invoke this
    unconditionally; the subsequent ``mark_cancelled`` /
    ``mark_failed`` / ``mark_completed`` calls will also short-
    circuit on the already-terminal state via their own idempotent
    guards, and the emitter's terminal guard ensures only one
    terminal event reaches the wire.
    """
    if state.status in TERMINAL_STATUSES:
        logger.debug(
            "mark_cancelling() no-op: run '%s' is already terminal "
            "('%s'). Preserving existing status.",
            state.run_id,
            state.status.value,
        )
        return
    transition(state, ExecutionStatus.CANCELLING, reason=reason)


def mark_cancelled(state: ExecutionState, reason: str = "") -> None:
    """
    Transition into the CANCELLED terminal state.

    Idempotent: calling twice is a no-op. This makes the
    "double cancellation" scenario (user clicks Stop twice, or the
    Stop request races with a step timeout) safe without every caller
    re-checking ``is_terminal(state)`` first.

    Non-destructive: if the state has already reached COMPLETED or
    FAILED, the existing terminal status is preserved and a warning
    is logged. This is the belt-and-suspenders fix for B13.
    """
    if state.status == ExecutionStatus.CANCELLED:
        return  # idempotent
    if state.status in TERMINAL_STATUSES:
        logger.warning(
            "mark_cancelled() ignored: run '%s' is already in terminal "
            "status '%s'.",
            state.run_id,
            state.status.value,
        )
        return
    transition(
        state,
        ExecutionStatus.CANCELLED,
        reason=reason or "Cancelled by operator",
    )


def mark_completed(
    state: ExecutionState,
    output: Optional[str] = None,
    reason: str = "",
) -> None:
    """
    Transition into the COMPLETED terminal state.

    Phase A behavior: ``output`` is optional. Phase F will add a
    strict guard requiring proof of successful ``terminate``
    invocation before COMPLETED is allowed — that guard is the direct
    fix for the "false success" bugs B3/B4.

    Idempotent: calling twice with COMPLETED already set is a no-op.
    Non-destructive: if already FAILED or CANCELLED, the existing
    terminal status is preserved.
    """
    if state.status == ExecutionStatus.COMPLETED:
        return  # idempotent
    if state.status in TERMINAL_STATUSES:
        logger.warning(
            "mark_completed() ignored: run '%s' is already in terminal "
            "status '%s'.",
            state.run_id,
            state.status.value,
        )
        return
    if output is not None:
        state.final_output = output
    transition(
        state,
        ExecutionStatus.COMPLETED,
        reason=reason or "Task completed successfully",
    )


def mark_failed(
    state: ExecutionState,
    message: str,
    error_code: Optional[str] = None,
    reason: str = "",
) -> None:
    """
    Transition into the FAILED terminal state, recording the error.

    Idempotent: once FAILED, further calls are no-ops. The error
    message from the FIRST failure is preserved — subsequent calls'
    messages are discarded so the operator sees the original root
    cause, not a cascade of secondary errors.

    Non-destructive: if already COMPLETED or CANCELLED, the existing
    terminal status is preserved.
    """
    if state.status == ExecutionStatus.FAILED:
        return  # idempotent
    if state.status in TERMINAL_STATUSES:
        logger.warning(
            "mark_failed() ignored: run '%s' is already in terminal "
            "status '%s'.",
            state.run_id,
            state.status.value,
        )
        return

    # Record metadata FIRST, then transition. If the transition
    # somehow raises (should not, per VALID_TRANSITIONS), the
    # metadata at least carries the intent for post-mortem analysis.
    state.metadata["error"] = message
    state.metadata["error_code"] = error_code or "RUNTIME_ERROR"
    state.metadata["error_ts"] = time.time()

    transition(
        state,
        ExecutionStatus.FAILED,
        reason=reason or message,
    )


def mark_interrupted(
    state: ExecutionState,
    reason: str = "",
) -> None:
    """
    Mark a run as INTERRUPTED by a process restart.

    Phase A shim (forward-compatible): state.py's ``ExecutionStatus``
    does not yet define an ``INTERRUPTED`` member. We therefore:

      1. Set ``metadata["interrupted"] = True`` unconditionally —
         this is the marker ``is_interrupted()`` reads, and it stays
         valid after Phase G2 upgrades the shim to a real status.
      2. Record ``metadata["error_code"] = "INTERRUPTED_BY_RESTART"``
         so post-mortem tooling can distinguish a crash from a
         genuine execution failure.
      3. Transition to FAILED via ``transition()``. Most statuses
         (RUNNING, WAITING_FOR_INPUT, RETRYING, STARTING, CANCELLING)
         support this directly.
      4. Fall back to CANCELLED for the legacy statuses (IDLE,
         PAUSED) that have no direct path to FAILED. The semantics
         are equivalent here: no work was in flight, and
         ``is_interrupted()`` still returns True.

    Non-destructive: if the run is already in a terminal state,
    only the metadata markers are refreshed — the status is preserved.
    This prevents a crash-detection sweep on server startup from
    overwriting a run that completed successfully between the crash
    and the sweep.
    """
    now = time.time()

    # Preserve existing terminal status; only refresh metadata.
    if state.status in TERMINAL_STATUSES:
        state.metadata[_INTERRUPTED_METADATA_KEY] = True
        state.metadata.setdefault("interrupted_at", now)
        return

    # Record intent before the transition so the intent survives even
    # if the transition raises for an unexpected reason.
    state.metadata["error"] = (
        reason or "Execution was interrupted by a process restart."
    )
    state.metadata["error_code"] = INTERRUPTED_ERROR_CODE
    state.metadata["error_ts"] = now
    state.metadata[_INTERRUPTED_METADATA_KEY] = True
    state.metadata["interrupted_at"] = now

    # Try the direct FAILED transition first (works for the common
    # crash-during-execution case).
    if try_transition(
        state,
        ExecutionStatus.FAILED,
        reason=reason or "Process restart detected",
    ):
        return

    # Fallback for legacy statuses that cannot reach FAILED directly
    # (IDLE, PAUSED). CANCELLED is reachable from both and carries
    # the same semantic meaning in this context: the run is dead,
    # no work was in flight, and no progress was lost.
    logger.info(
        "mark_interrupted() falling back to CANCELLED for run '%s' "
        "(current status '%s' has no direct path to FAILED).",
        state.run_id,
        state.status.value,
    )
    try_transition(
        state,
        ExecutionStatus.CANCELLED,
        reason=reason or "Process restart detected",
    )


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def get_allowed_transitions(
    state: ExecutionState,
) -> FrozenSet[ExecutionStatus]:
    """
    Return the set of statuses ``state`` can legally transition to.

    Empty for terminal runs. Used by UI layers to decide which action
    buttons to show (Stop / Resume / Retry) and by RecoveryManager to
    decide whether a resume path exists.
    """
    if state.status in TERMINAL_STATUSES:
        return frozenset()
    return frozenset(VALID_TRANSITIONS.get(state.status, set()))


def describe_state(state: ExecutionState) -> Dict[str, Any]:
    """
    Return a compact, JSON-safe summary of the state's lifecycle.

    Safe to embed in SSE payloads, diagnostics, and log lines — never
    includes message content, tool outputs, or workspace file paths.
    """
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "is_terminal": is_terminal(state),
        "is_active": is_active(state),
        "is_waiting": is_waiting(state),
        "is_pending": is_pending(state),
        "is_interrupted": is_interrupted(state),
        "is_resumable": is_resumable(state),
        "current_step": state.current_step,
        "max_steps": state.max_steps,
        "allowed_transitions": sorted(
            s.value for s in get_allowed_transitions(state)
        ),
    }


__all__ = [
    # Status sets (frozensets — safe to import anywhere)
    "ACTIVE_STATUSES",
    "INTERRUPTED_ERROR_CODE",
    "PENDING_STATUSES",
    "RESUMABLE_STATUSES",
    "TERMINAL_STATUSES",
    "WAITING_STATUSES",
    # Status predicates (accept ExecutionStatus or str)
    "is_active_status",
    "is_pending_status",
    "is_resumable_status",
    "is_terminal_status",
    "is_waiting_status",
    # Lifecycle predicates (accept ExecutionState)
    "is_active",
    "is_interrupted",
    "is_pending",
    "is_resumable",
    "is_terminal",
    "is_waiting",
    # Transition API
    "can_transition",
    "transition",
    "try_transition",
    # Semantic marking helpers
    "mark_cancelled",
    "mark_cancelling",
    "mark_completed",
    "mark_failed",
    "mark_interrupted",
    "mark_paused",
    "mark_resumed_from_input",
    "mark_running",
    "mark_waiting_for_input",
    # Introspection
    "describe_state",
    "get_allowed_transitions",
    # Re-exports for convenience (callers import from ONE place)
    "ExecutionState",
    "ExecutionStatus",
    "InvalidStateTransitionError",
    "VALID_TRANSITIONS",
]
