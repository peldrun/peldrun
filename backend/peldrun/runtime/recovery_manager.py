# Path: backend/peldrun/runtime/recovery_manager.py
"""
PELDRUN Core Recovery Manager.

Centralizes the detection, planning, and execution of run recovery
after a process restart. This is the module that turns a crashed
execution into a resumable one.

Fixes B11 in the PELDRUN execution repair plan: the historical
``omweb/services/recovery.py`` only set ``status="interrupted"`` on
abandoned runs and then stopped. It never read the last checkpoint,
never rehydrated ``ExecutionState``, and never called the executor
again. Every crash permanently lost the run's progress; the user had
to re-submit the prompt from scratch.

Design invariants
-----------------

1. **Detection is caller-supplied, planning is ours.**
   Finding "which runs need recovery?" requires a table scan that
   only the storage layer can do efficiently. The caller (Phase G2's
   startup wiring) queries ``RunStore`` for candidate run IDs and
   hands them to ``detect_orphans()``. This manager performs the
   non-trivial work — checking whether each candidate is actually
   resumable — but does not invent the candidate list.

2. **Plan first, execute second.**
   ``plan_resume(run_id)`` returns a ``RecoveryPlan`` without
   mutating any state or invoking the executor. Callers can inspect
   the plan (in tests, in diagnostics, in a UI preview), request
   confirmation, and only then call ``resume_run()``. The two-step
   flow also lets the runtime distinguish "resumable but awaiting
   approval" from "already resuming".

3. **No global state changes on detection.**
   ``detect_orphans`` and ``plan_resume`` are pure reads. Only
   ``resume_run`` (and explicitly calling ``mark_interrupted``)
   transition state. A startup sweep that finds 50 orphan runs but
   then shuts down does not corrupt anything.

4. **One resume per run, ever.**
   ``resume_run`` acquires an ``asyncio.Lock`` scoped to the run
   before doing any work. A second concurrent call returns a
   ``CONCURRENT_RESUME`` outcome without waiting. This is the direct
   implementation of Phase G2's acceptance criterion: "same run_id
   cannot be resumed twice concurrently".

5. **State restoration goes through checkpoints, not state_json.**
   ``runs.state_json`` (the row overwritten every step) is a
   *diagnostic* snapshot — it may be stale if the process died mid-
   write. ``checkpoints.state_dump_json`` is written to disk and
   DB *atomically before* the corresponding step begins, so it
   always represents a known-good boundary. ``plan_resume`` prefers
   the latest checkpoint; it falls back to ``load_state`` only when
   no checkpoint exists (a first-step crash).

6. **Terminal states are never resumed.**
   A run that already reached COMPLETED / FAILED / CANCELLED is
   left alone — even if its row is stale, resuming it would
   re-execute the final step, double-apply side effects, and
   duplicate the terminal event. The check happens BEFORE the
   checkpoint is loaded (cheaper and safer).

7. **Interrupted states are promoted explicitly.**
   A crashed run is in ``RUNNING`` (or ``RETRYING``) status in the
   DB — the process died before it could transition to FAILED. The
   first action of ``resume_run`` is to promote that status to
   ``FAILED`` with ``error_code="INTERRUPTED_BY_RESTART"`` via
   ``lifecycle.mark_interrupted``, so that:
     * the state's audit trail records the crash;
     * any concurrent observer (UI polling, recovery sweep on
       another node) sees a coherent "was interrupted, now
       resuming" sequence;
     * if the resume itself crashes, the second recovery pass sees
       a well-formed INTERRUPTED state instead of a phantom RUNNING.

8. **No import of executor, runner, or agent.**
   ``resume_run`` takes an ``executor_fn`` callback. The Phase G2
   wiring supplies ``core_executor.execute`` — this module does not
   import it. This keeps ``recovery_manager.py`` loadable in
   isolation (unit tests, CLI tools) and prevents the
   ``executor → recovery_manager → executor`` cycle that would
   otherwise form.

9. **Human input requests are reported, not re-registered.**
   ``plan_resume`` reports the pending ``HumanInputRequest`` objects
   it finds. It does NOT re-register their ``asyncio.Future``
   objects — that is Phase G3's job (``HumanInputPort.register_pending``
   during resume). Phase A guarantees the plan carries the right
   requests; Phase G3 guarantees the coroutine bridging.

See also
--------

    * ``backend/peldrun/runtime/ports.py``           — PersistencePort,
                                                        HumanInputPort

    * ``backend/peldrun/engine/lifecycle.py``        — is_interrupted,
                                                        mark_interrupted,
                                                        try_transition

    * ``backend/peldrun/engine/checkpoint.py``       — StateCheckpoint
                                                        (the restoration
                                                        vehicle)

    * ``backend/peldrun/runtime/executor.py``        — the executor_fn
                                                        Phase G2 wires in

    * ``backend/peldrun/events/terminal.py``         — TerminalKind (the
                                                        post-resume outcome
                                                        classification)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
)

from peldrun.engine.checkpoint import StateCheckpoint
from peldrun.engine.lifecycle import (
    INTERRUPTED_ERROR_CODE,
    is_interrupted,
    is_terminal,
    mark_interrupted,
    try_transition,
)
from peldrun.engine.state import ExecutionState, ExecutionStatus
from peldrun.events.terminal import TerminalKind
from peldrun.runtime.contract import (
    HumanInputRequest,
    HumanInputStatus,
    RunRequest,
    RunResult,
    RunStatus,
)
from peldrun.runtime.ports import HumanInputPort, PersistencePort

logger = logging.getLogger("peldrun.runtime.recovery_manager")


# ---------------------------------------------------------------------------
# Recovery reason taxonomy
# ---------------------------------------------------------------------------


class RecoveryReason(str, Enum):
    """
    Canonical reason a run is (or is not) resumable.

    Every ``RecoveryPlan`` and every non-success ``RunResult``
    returned by ``resume_run`` carries exactly one of these — no
    ad-hoc strings. Phase G2's HTTP layer maps these to user-facing
    messages.
    """

    # --- Resumable ------------------------------------------------
    RESUMABLE_INTERRUPTED = "resumable_interrupted"
    """Process crashed mid-run; a checkpoint exists that captures
    the last known-good step."""

    RESUMABLE_WAITING_INPUT = "resumable_waiting_input"
    """Run was blocked in ``ask_human`` when the process died; the
    pending request can be re-registered."""

    RESUMABLE_NO_CHECKPOINT = "resumable_no_checkpoint"
    """Run was interrupted before the first checkpoint; recovery
    restarts from the initial state, re-running step 1 only."""

    # --- Not resumable --------------------------------------------
    NOT_FOUND = "not_found"
    """No state is persisted for this run_id."""

    ALREADY_TERMINAL = "already_terminal"
    """Run already reached COMPLETED / FAILED / CANCELLED."""

    CANCELLED_BY_OPERATOR = "cancelled_by_operator"
    """A cancellation was requested before the crash; resuming
    would violate operator intent."""

    CANCELLING_STUCK = "cancelling_stuck"
    """Run was mid-cancellation when the process died. Cleanup must
    complete before any resume is attempted."""

    NO_STATE_AND_NO_CHECKPOINT = "no_state_and_no_checkpoint"
    """Neither ``load_state`` nor any checkpoint returned data.
    This indicates a corrupted store, not a recoverable run."""

    CONCURRENT_RESUME = "concurrent_resume"
    """Another ``resume_run`` call is already in progress for this
    run_id. The second caller must retry later or wait on the
    first."""

    REQUEST_BUILD_FAILED = "request_build_failed"
    """The caller's ``request_builder`` callback returned ``None``
    (typically because required fields like ``agent_spec`` or
    ``workspace`` could not be reconstructed from the persisted
    row). The caller must supply a fix before resuming."""

    EXECUTOR_FAILED = "executor_failed"
    """The executor raised an unexpected exception while resuming.
    The exception message is captured in ``error_message``."""


#: Human-readable labels for the log lines and the ``reason_text``
#: field on ``RecoveryPlan``. These are NOT user-facing — the HTTP
#: layer applies its own localization. They exist so that a stack
#: trace or log grep reads coherently.
_REASON_LABELS: Dict[RecoveryReason, str] = {
    RecoveryReason.RESUMABLE_INTERRUPTED: "interrupted run with checkpoint",
    RecoveryReason.RESUMABLE_WAITING_INPUT: "waiting on human input",
    RecoveryReason.RESUMABLE_NO_CHECKPOINT: "interrupted at first step",
    RecoveryReason.NOT_FOUND: "no persisted state",
    RecoveryReason.ALREADY_TERMINAL: "already terminal",
    RecoveryReason.CANCELLED_BY_OPERATOR: "cancelled by operator",
    RecoveryReason.CANCELLING_STUCK: "stuck in cancelling",
    RecoveryReason.NO_STATE_AND_NO_CHECKPOINT: "no state or checkpoint",
    RecoveryReason.CONCURRENT_RESUME: "concurrent resume in progress",
    RecoveryReason.REQUEST_BUILD_FAILED: "request could not be built",
    RecoveryReason.EXECUTOR_FAILED: "executor raised",
}


def describe_reason(reason: RecoveryReason) -> str:
    """Return the log-safe label for ``reason``."""
    return _REASON_LABELS.get(reason, reason.value)


# ---------------------------------------------------------------------------
# RecoveryPlan
# ---------------------------------------------------------------------------


@dataclass
class RecoveryPlan:
    """
    The outcome of planning a resume. Pure data — no side effects.

    Callers MUST inspect ``resumable`` before invoking
    ``resume_run``. When ``resumable`` is False, ``reason`` explains
    why; ``state`` and ``latest_checkpoint`` may still be populated
    (for diagnostics) but the caller must not act on them.

    Attributes
    ----------
    run_id
        The run this plan describes.

    resumable
        True only when every precondition for a safe resume is met.

    reason
        Canonical recovery classification. Set in BOTH resumable and
        non-resumable outcomes.

    reason_text
        Human-readable elaboration. Never used for control flow.

    state
        The persisted state, or the checkpoint-restored state when a
        checkpoint is available. ``None`` when no state was found.

    latest_checkpoint
        The most recent ``StateCheckpoint`` for this run, or
        ``None`` when none was ever written (crash before step 1
        completed).

    pending_human_requests
        Every ``HumanInputRequest`` currently in PENDING status. Only
        non-empty for runs whose state is ``WAITING_FOR_INPUT``. Phase
        G3 uses this list to re-register futures during resume.

    next_step
        The step index the resume will begin from. When a checkpoint
        exists, this is ``checkpoint.step + 1``. When starting from
        scratch, this is 1.
    """

    run_id: str
    resumable: bool = False
    reason: RecoveryReason = RecoveryReason.NOT_FOUND
    reason_text: str = ""
    state: Optional[ExecutionState] = None
    latest_checkpoint: Optional[StateCheckpoint] = None
    pending_human_requests: List[HumanInputRequest] = field(
        default_factory=list
    )
    next_step: int = 1

    def summary(self) -> str:
        """One-line summary for log lines."""
        base = f"run={self.run_id} reason={self.reason.value}"
        if not self.resumable:
            return f"not-resumable ({base})"
        return (
            f"resumable ({base}, "
            f"next_step={self.next_step}, "
            f"checkpoint={'yes' if self.latest_checkpoint else 'no'}, "
            f"pending_hitl={len(self.pending_human_requests)})"
        )


# ---------------------------------------------------------------------------
# ResumeOutcome
# ---------------------------------------------------------------------------


@dataclass
class ResumeOutcome:
    """
    Return value of ``resume_run``.

    Distinct from ``RecoveryPlan`` because it reflects the ACTUAL
    execution, not the intent. A plan can say "resumable" and the
    resume can still fail (executor crash, request build failure) —
    the outcome carries the executor's final ``RunResult`` so the
    caller has full visibility.
    """

    run_id: str
    success: bool
    reason: RecoveryReason
    reason_text: str = ""
    result: Optional[RunResult] = None
    terminal_kind: Optional[TerminalKind] = None
    error_message: Optional[str] = None

    def summary(self) -> str:
        base = f"run={self.run_id} reason={self.reason.value}"
        if self.success:
            kind = self.terminal_kind.value if self.terminal_kind else "?"
            return f"resume-ok ({base}, terminal={kind})"
        return f"resume-failed ({base}): {self.error_message or self.reason_text}"


# ---------------------------------------------------------------------------
# Checkpoint helpers (duck-typed across PersistencePort variants)
# ---------------------------------------------------------------------------


async def _call_list_checkpoints(
    persistence: PersistencePort, run_id: str
) -> List[StateCheckpoint]:
    """
    Return the run's checkpoints, tolerating the two method names in
    use today.

    ``PersistencePort`` declares ``list_checkpoints``. The current
    ``SqliteRunStore`` implements ``get_checkpoints``. Both return
    the same shape (``List[StateCheckpoint]``). We accept either.
    """
    fn = getattr(persistence, "list_checkpoints", None)
    if fn is None:
        fn = getattr(persistence, "get_checkpoints", None)
    if fn is None:
        return []
    try:
        result = fn(run_id)
        if asyncio.iscoroutine(result):
            result = await result
        return list(result or [])
    except Exception as exc:
        logger.warning(
            "list_checkpoints failed for run %r: %s", run_id, exc
        )
        return []


async def _call_latest_checkpoint(
    persistence: PersistencePort, run_id: str
) -> Optional[StateCheckpoint]:
    """
    Return the most recent checkpoint for the run.

    Uses ``get_latest_checkpoint`` when available; otherwise derives
    the latest from the full list by ``step`` (not by wall-clock, so
    a fast retry cannot beat a slower prior checkpoint).
    """
    fn = getattr(persistence, "get_latest_checkpoint", None)
    if fn is not None:
        try:
            result = fn(run_id)
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None:
                return result
        except Exception as exc:
            logger.debug(
                "get_latest_checkpoint fallback triggered for run %r: %s",
                run_id,
                exc,
            )

    checkpoints = await _call_list_checkpoints(persistence, run_id)
    if not checkpoints:
        return None
    return max(checkpoints, key=lambda cp: cp.step)


async def _call_load_state(
    persistence: PersistencePort, run_id: str
) -> Optional[ExecutionState]:
    """Load the persisted state, swallowing storage errors."""
    try:
        result = persistence.load_state(run_id)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except Exception as exc:
        logger.warning("load_state failed for run %r: %s", run_id, exc)
        return None


async def _call_pending_human_requests(
    persistence: PersistencePort, run_id: str
) -> List[HumanInputRequest]:
    """List pending human input requests for the run."""
    try:
        result = persistence.list_pending_human_requests(run_id)
        if asyncio.iscoroutine(result):
            result = await result
        return list(result or [])
    except Exception as exc:
        logger.warning(
            "list_pending_human_requests failed for run %r: %s",
            run_id,
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# Restore state from a checkpoint
# ---------------------------------------------------------------------------


def _restore_from_checkpoint(
    checkpoint: StateCheckpoint,
) -> Optional[ExecutionState]:
    """
    Reconstruct ``ExecutionState`` from a checkpoint's snapshot.

    Returns ``None`` when the snapshot is malformed (a corrupted
    checkpoint is not fatal — ``plan_resume`` falls back to the
    latest loadable snapshot or reports NO_STATE_AND_NO_CHECKPOINT).
    """
    try:
        return ExecutionState.from_snapshot_dict(
            checkpoint.state_data, inflate_images=True
        )
    except Exception as exc:
        logger.warning(
            "Failed to restore state from checkpoint %r: %s",
            checkpoint.id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Concurrency: per-run resume locks
# ---------------------------------------------------------------------------


_resume_locks: Dict[str, asyncio.Lock] = {}
_resume_locks_guard = threading.Lock()


def _get_resume_lock(run_id: str) -> asyncio.Lock:
    """
    Return (creating on first use) the ``asyncio.Lock`` for a run.

    The ``threading.Lock`` protects the dict mutation from concurrent
    ``get_resume_lock`` calls across event loops. Creating an
    ``asyncio.Lock`` outside a running loop is legal in Python 3.10+.
    """
    with _resume_locks_guard:
        lock = _resume_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            _resume_locks[run_id] = lock
        return lock


def _release_resume_lock(run_id: str) -> None:
    """
    Drop the per-run lock entry once the resume finishes.

    Safe even if the lock is still held: ``asyncio.Lock`` objects do
    not require explicit cleanup. Removing the dict entry lets the
    garbage collector reclaim it once the last reference (the
    ``async with`` context) exits.
    """
    with _resume_locks_guard:
        _resume_locks.pop(run_id, None)


# ---------------------------------------------------------------------------
# RecoveryManager
# ---------------------------------------------------------------------------


class RecoveryManager:
    """
    Stateless orchestrator for run recovery.

    Holds only a reference to the injected ports. All state lives in
    the persistence layer and in the process-wide resume lock
    registry (which is intentionally global so that two
    ``RecoveryManager`` instances in the same process coordinate
    correctly).
    """

    def __init__(
        self,
        persistence: PersistencePort,
        human_input: Optional[HumanInputPort] = None,
    ) -> None:
        self._persistence = persistence
        self._human_input = human_input

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    async def detect_orphans(
        self, candidate_run_ids: Iterable[str]
    ) -> List[str]:
        """
        Return the subset of ``candidate_run_ids`` that need recovery.

        A candidate is an orphan when:

          * a persisted state exists (a bare row without state is a
            corruption, not an orphan);
          * the state is NOT terminal (COMPLETED / FAILED / CANCELLED
            are final, never resumed);
          * the state is not stuck in CANCELLING (needs explicit
            cleanup before resume can be considered).

        RUNNING, RETRYING, WAITING_FOR_INPUT, WAITING_FOR_HUMAN,
        PAUSED, STARTING, and INTERRUPTED-tagged FAILED states all
        qualify — that is exactly the set of statuses the process
        could have left behind when it died.

        Parameters
        ----------
        candidate_run_ids
            The IDs of runs the caller believes might need recovery.
            Typically every row from ``runs`` whose ``status`` is in
            a non-terminal set. This manager does NOT query the
            storage layer for candidates — that is a
            storage-schema-specific concern the caller owns.

        Returns
        -------
        List[str]
            The orphan run IDs, in the order the caller supplied
            them (stable, deterministic — tests can assert on it).
        """
        orphans: List[str] = []
        for run_id in candidate_run_ids:
            if not run_id:
                continue
            try:
                state = await _call_load_state(self._persistence, run_id)
            except Exception as exc:
                logger.warning(
                    "detect_orphans: load_state failed for %r: %s",
                    run_id,
                    exc,
                )
                continue

            if state is None:
                continue

            if is_terminal(state) and not is_interrupted(state):
                # Terminal + not interrupted = genuinely finished.
                continue

            if state.status == ExecutionStatus.CANCELLING:
                # Stuck mid-cancellation; surface as an orphan so the
                # caller can run its cleanup path, but ``plan_resume``
                # will report CANCELLING_STUCK and refuse to resume.
                orphans.append(run_id)
                continue

            orphans.append(run_id)

        return orphans

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def plan_resume(self, run_id: str) -> RecoveryPlan:
        """
        Build a ``RecoveryPlan`` for ``run_id``.

        Pure read — no state transitions, no executor invocation, no
        side effects. Safe to call from a preview UI, a diagnostic
        tool, or a startup audit.

        Uses the latest checkpoint as the restoration vehicle
        whenever one exists. Falls back to ``load_state`` only when
        no checkpoint was ever written (crash during step 1).
        """
        if not run_id:
            return RecoveryPlan(
                run_id=run_id,
                resumable=False,
                reason=RecoveryReason.NOT_FOUND,
                reason_text="Empty run_id.",
            )

        # 1. Load state — the cheapest sanity check.
        state = await _call_load_state(self._persistence, run_id)
        if state is None:
            return RecoveryPlan(
                run_id=run_id,
                resumable=False,
                reason=RecoveryReason.NOT_FOUND,
                reason_text=(
                    "No persisted state for this run. "
                    "It may never have started, or its storage "
                    "backend lost the row."
                ),
            )

        # 2. Terminal states are never resumed.
        if is_terminal(state) and not is_interrupted(state):
            return RecoveryPlan(
                run_id=run_id,
                resumable=False,
                reason=RecoveryReason.ALREADY_TERMINAL,
                reason_text=(
                    f"Run is already in terminal status "
                    f"'{state.status.value}'."
                ),
                state=state,
            )

        # 3. Stuck-cancelling needs cleanup, not resume.
        if state.status == ExecutionStatus.CANCELLING:
            return RecoveryPlan(
                run_id=run_id,
                resumable=False,
                reason=RecoveryReason.CANCELLING_STUCK,
                reason_text=(
                    "Run was mid-cancellation when the process died. "
                    "Cancellation cleanup must complete before any "
                    "resume is attempted."
                ),
                state=state,
            )

        # 4. Operator cancellation takes precedence over any
        #    checkpoint — resuming would violate operator intent.
        #    ``state.metadata["error_code"]`` is set by
        #    ``lifecycle.mark_cancelled``/``mark_cancelling``.
        existing_error_code = str(
            state.metadata.get("error_code") or ""
        ).upper()
        if existing_error_code in (
            "CANCELLED_BY_OPERATOR",
            "CANCELLED",
        ):
            return RecoveryPlan(
                run_id=run_id,
                resumable=False,
                reason=RecoveryReason.CANCELLED_BY_OPERATOR,
                reason_text=(
                    "A cancellation was requested before the crash. "
                    "Resuming would violate operator intent."
                ),
                state=state,
            )

        # 5. Load the latest checkpoint (preferred restoration).
        latest_checkpoint = await _call_latest_checkpoint(
            self._persistence, run_id
        )

        # 6. If a checkpoint exists, restore from it. Otherwise the
        #    state we already loaded is the best we have.
        restored_state = state
        next_step = state.current_step + 1

        if latest_checkpoint is not None:
            from_checkpoint = _restore_from_checkpoint(latest_checkpoint)
            if from_checkpoint is not None:
                restored_state = from_checkpoint
                # The checkpoint captures the state AT the end of
                # ``checkpoint.step``. Resume begins at the next one.
                next_step = max(1, int(latest_checkpoint.step) + 1)

        # 7. Load pending HITL requests. Only meaningful for a
        #    waiting_for_input state, but we list them for ANY
        #    non-terminal state so a plan for a crashed run that
        #    was ALSO waiting reports the requests.
        pending_requests: List[HumanInputRequest] = []
        if restored_state.status in (
            ExecutionStatus.WAITING_FOR_INPUT,
            ExecutionStatus.WAITING_FOR_HUMAN,
        ):
            pending_requests = await _call_pending_human_requests(
                self._persistence, run_id
            )

        # 8. Classify the resumable outcome.
        if latest_checkpoint is None:
            return RecoveryPlan(
                run_id=run_id,
                resumable=True,
                reason=RecoveryReason.RESUMABLE_NO_CHECKPOINT,
                reason_text=(
                    "Run was interrupted before the first checkpoint. "
                    "Resume will re-execute step 1 from the initial "
                    "state."
                ),
                state=restored_state,
                latest_checkpoint=None,
                pending_human_requests=pending_requests,
                next_step=1,
            )

        if pending_requests:
            return RecoveryPlan(
                run_id=run_id,
                resumable=True,
                reason=RecoveryReason.RESUMABLE_WAITING_INPUT,
                reason_text=(
                    f"Run is waiting on {len(pending_requests)} "
                    f"human input request(s). Resume will re-register "
                    f"them so the user can still answer."
                ),
                state=restored_state,
                latest_checkpoint=latest_checkpoint,
                pending_human_requests=pending_requests,
                next_step=next_step,
            )

        return RecoveryPlan(
            run_id=run_id,
            resumable=True,
            reason=RecoveryReason.RESUMABLE_INTERRUPTED,
            reason_text=(
                f"Run was interrupted mid-execution. Resume will "
                f"restart from step {next_step}."
            ),
            state=restored_state,
            latest_checkpoint=latest_checkpoint,
            pending_human_requests=pending_requests,
            next_step=next_step,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def resume_run(
        self,
        run_id: str,
        executor_fn: Callable[[RunRequest], Awaitable[RunResult]],
        request_builder: Callable[
            [ExecutionState, RecoveryPlan], Optional[RunRequest]
        ],
        *,
        lock_timeout_seconds: Optional[float] = None,
    ) -> ResumeOutcome:
        """
        Execute a planned resume.

        Parameters
        ----------
        run_id
            The run to resume.

        executor_fn
            An async callable that performs the actual execution
            given a ``RunRequest`` and returns a ``RunResult``. Phase
            G2's HTTP layer supplies ``core_executor.execute``. Tests
            supply a mock.

        request_builder
            A synchronous callable that reconstructs a ``RunRequest``
            from the restored state and the recovery plan. Returns
            ``None`` when the request cannot be built (missing
            agent_spec, missing workspace, ...). Phase G2's wiring
            reconstructs the request from the persisted row.

        lock_timeout_seconds
            When not None, wait up to this many seconds to acquire
            the resume lock before reporting ``CONCURRENT_RESUME``.
            Default (None) reports ``CONCURRENT_RESUME`` immediately
            if the lock is held — matching the plan's acceptance
            criterion "cannot resume twice concurrently".

        Returns
        -------
        ResumeOutcome
            NEVER raises. Executor exceptions are captured in
            ``error_message`` and surfaced as ``EXECUTOR_FAILED``.
        """
        # Fast-path: no run_id.
        if not run_id:
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=RecoveryReason.NOT_FOUND,
                reason_text="Empty run_id.",
                error_message="Empty run_id.",
            )

        lock = _get_resume_lock(run_id)

        if lock_timeout_seconds is None:
            if lock.locked():
                return ResumeOutcome(
                    run_id=run_id,
                    success=False,
                    reason=RecoveryReason.CONCURRENT_RESUME,
                    reason_text=(
                        "Another resume for this run is already in "
                        "progress."
                    ),
                    error_message="Concurrent resume rejected.",
                )
            acquired = await self._acquire_lock(lock, None)
        else:
            acquired = await self._acquire_lock(
                lock, lock_timeout_seconds
            )

        if not acquired:
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=RecoveryReason.CONCURRENT_RESUME,
                reason_text=(
                    f"Timed out waiting for concurrent resume "
                    f"(timeout={lock_timeout_seconds}s)."
                ),
                error_message="Concurrent resume timeout.",
            )

        try:
            return await self._resume_impl(
                run_id=run_id,
                executor_fn=executor_fn,
                request_builder=request_builder,
            )
        finally:
            lock.release()
            _release_resume_lock(run_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    async def _acquire_lock(
        lock: asyncio.Lock, timeout: Optional[float]
    ) -> bool:
        """
        Acquire ``lock``, optionally bounded by ``timeout``.

        ``timeout=None`` means "acquire immediately" (non-blocking
        try). A positive timeout uses ``asyncio.wait_for``. A zero
        timeout is treated as None (immediate).
        """
        if timeout is None or timeout <= 0:
            try:
                # ``acquire`` is instant if the lock is free.
                await lock.acquire()
                return True
            except Exception as exc:
                logger.debug("Immediate lock acquire failed: %s", exc)
                return False
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except Exception as exc:
            logger.debug("Timed lock acquire failed: %s", exc)
            return False

    async def _resume_impl(
        self,
        *,
        run_id: str,
        executor_fn: Callable[[RunRequest], Awaitable[RunResult]],
        request_builder: Callable[
            [ExecutionState, RecoveryPlan], Optional[RunRequest]
        ],
    ) -> ResumeOutcome:
        """The body of ``resume_run`` after the lock is acquired."""
        # 1. Re-plan under the lock. The plan may have been valid a
        #    moment ago; a concurrent activity (user answered a HITL,
        #    external cancel) could have changed the picture.
        plan = await self.plan_resume(run_id)
        if not plan.resumable:
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=plan.reason,
                reason_text=plan.reason_text,
                error_message=plan.reason_text or describe_reason(
                    plan.reason
                ),
            )

        if plan.state is None:
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=RecoveryReason.NO_STATE_AND_NO_CHECKPOINT,
                reason_text=(
                    "Plan is resumable but carries no state. This is "
                    "a RecoveryManager bug, not a user-actionable "
                    "condition."
                ),
                error_message="Recovery plan produced no state.",
            )

        # 2. Promote the state from "was RUNNING when we died" to
        #    "was INTERRUPTED". This runs BEFORE the executor so
        #    that even if the executor crashes again, the audit
        #    trail records the interruption.
        try:
            mark_interrupted(
                plan.state,
                reason=(
                    "Resuming after process restart "
                    f"(recovery reason={plan.reason.value})"
                ),
            )
        except Exception as exc:
            # ``mark_interrupted`` should never raise for a
            # non-terminal state; if it does, we still want to
            # proceed. Log and continue — the executor is the
            # final authority on the run's terminal state.
            logger.warning(
                "mark_interrupted failed for run %r: %s", run_id, exc
            )

        # 3. Build the RunRequest via the caller-supplied callback.
        try:
            request = request_builder(plan.state, plan)
        except Exception as exc:
            logger.error(
                "request_builder raised for run %r: %s", run_id, exc,
                exc_info=True,
            )
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=RecoveryReason.REQUEST_BUILD_FAILED,
                reason_text=(
                    f"request_builder raised: {exc}. The caller must "
                    f"supply a working builder before resuming."
                ),
                error_message=str(exc),
            )

        if request is None:
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=RecoveryReason.REQUEST_BUILD_FAILED,
                reason_text=(
                    "request_builder returned None. Required fields "
                    "(agent_spec, workspace, llm_config) could not be "
                    "reconstructed from the persisted state."
                ),
                error_message="request_builder returned None.",
            )

        # 4. Delegate execution.
        try:
            result = await executor_fn(request)
        except Exception as exc:
            logger.error(
                "Executor raised during resume for run %r: %s",
                run_id,
                exc,
                exc_info=True,
            )
            return ResumeOutcome(
                run_id=run_id,
                success=False,
                reason=RecoveryReason.EXECUTOR_FAILED,
                reason_text=(
                    f"Executor raised: {exc}"
                ),
                error_message=str(exc),
            )

        # 5. Classify the terminal outcome from the executor result.
        terminal_kind = _terminal_kind_from_result(result)

        return ResumeOutcome(
            run_id=run_id,
            success=True,
            reason=plan.reason,
            reason_text=plan.reason_text,
            result=result,
            terminal_kind=terminal_kind,
        )


# ---------------------------------------------------------------------------
# Terminal kind classification from a RunResult
# ---------------------------------------------------------------------------


def _terminal_kind_from_result(
    result: Any,
) -> Optional[TerminalKind]:
    """
    Map a ``RunResult``-shaped object to a ``TerminalKind``.

    Duck-typed: accepts any object with a ``status`` attribute whose
    value matches the PELDRUN vocabulary. Returns ``None`` when
    classification is impossible (which callers treat as "outcome
    unknown" rather than "failed").
    """
    if result is None:
        return None

    status = getattr(result, "status", None)
    if status is None:
        return None

    status_value = (
        status.value if hasattr(status, "value") else str(status)
    ).strip().lower()

    if status_value == RunStatus.COMPLETED.value:
        # Distinguish interrupted-by-restart from a clean completion.
        # ``RunResult`` does not carry an ``interrupted`` flag today;
        # we infer it from the error_code when present.
        error_code = str(getattr(result, "error_code", "") or "").upper()
        if error_code == INTERRUPTED_ERROR_CODE:
            return TerminalKind.INTERRUPTED
        return TerminalKind.COMPLETED

    if status_value == RunStatus.FAILED.value:
        error_code = str(getattr(result, "error_code", "") or "").upper()
        if error_code == INTERRUPTED_ERROR_CODE:
            return TerminalKind.INTERRUPTED
        return TerminalKind.FAILED

    if status_value == RunStatus.CANCELLED.value:
        return TerminalKind.CANCELLED

    return None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def build_recovery_manager(
    persistence: PersistencePort,
    human_input: Optional[HumanInputPort] = None,
) -> RecoveryManager:
    """
    Construct a ``RecoveryManager`` bound to the supplied ports.

    Provided as a named factory so Phase G2's wiring code has a
    single import to reach, and so that tests can build a manager
    against ``NullPersistencePort`` without importing the class
    directly.
    """
    return RecoveryManager(
        persistence=persistence, human_input=human_input
    )


__all__ = [
    # Enums + dataclasses
    "RecoveryManager",
    "RecoveryPlan",
    "RecoveryReason",
    "ResumeOutcome",
    # Helpers
    "build_recovery_manager",
    "describe_reason",
]
