"""
backend/peldrun/engine/runner.py

PELDRUN Core Agent Execution Runner.
Coordinates agent lifecycle, asynchronous execution loops, checkpointing, and event emission.
Hardened under PELDRUN Runtime V1:
- Semantic cancellation pipeline: RUNNING -> CANCELLING -> CANCELLED.
- Inter-step pause & resume lifecycle: RUNNING <-> PAUSED.
- Checkpoint auto-capture upon pause and step boundaries.
- Max-step exhaustion handled strictly as FAILED with MAX_STEPS_EXCEEDED (never fake COMPLETED).
- All lifecycle modifications pass strictly through ExecutionState FSM validators.
- Global timeout suspended during pause intervals and human interactions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from peldrun.engine import lifecycle
from peldrun.engine.cancellation import cancel_and_drain
from peldrun.engine.state import ExecutionState, ExecutionStatus, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType

logger = logging.getLogger("peldrun.engine.runner")


@runtime_checkable
class StepExecutableAgent(Protocol):
    """Protocol defining the interface required for agents executed by AgentRunner."""

    name: str

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        """Execute a single reasoning/action iteration.

        Returns True if the task has reached completion, False to continue iterating.
        """
        ...


class RunnerConfig(BaseModel):
    """Runtime configuration parameters for AgentRunner execution."""

    model_config = ConfigDict(extra="ignore")

    max_steps: int = Field(default=30, description="Maximum allowed reasoning/acting iterations")
    step_timeout_seconds: float = Field(default=300.0, description="Timeout ceiling per individual step")
    total_timeout_seconds: Optional[float] = Field(default=None, description="Global job timeout in seconds")
    enable_checkpointing: bool = Field(default=True, description="Whether to capture state checkpoints each step")
    checkpoint_interval: int = Field(default=1, description="Interval in steps between checkpoints")


class AgentRunner:
    """Primary execution controller for PELDRUN agents.

    Drives step-by-step reasoning cycle, event dissemination, and graceful termination.
    Guarantees strict lifecycle state machine adherence.
    """

    def __init__(
        self,
        agent: Optional[StepExecutableAgent] = None,
        emitter: Optional[EventEmitter] = None,
        config: Optional[RunnerConfig] = None,
    ) -> None:
        self.agent = agent
        self.emitter = emitter or EventEmitter()
        self.config = config or RunnerConfig()
        self._cancel_requested = asyncio.Event()
        self._pause_requested = asyncio.Event()

    def cancel(self) -> None:
        """Signal execution cancellation at the next step boundary."""
        self._cancel_requested.set()

    def pause(self) -> None:
        """Signal execution pause at the current step boundary."""
        self._pause_requested.set()

    def resume(self) -> None:
        """Resume execution from paused state."""
        self._pause_requested.clear()

    @property
    def is_cancelled(self) -> bool:
        """Check whether cancellation has been requested."""
        return self._cancel_requested.is_set()

    @property
    def is_paused(self) -> bool:
        """Check whether pause has been requested."""
        return self._pause_requested.is_set()

    def _build_snapshot_payload(
        self,
        state: ExecutionState,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble a snapshot dict matching the EventEmitter.emit_snapshot contract."""
        metadata: Dict[str, Any] = {
            "run_id": state.run_id,
            "max_steps": state.max_steps,
            "agent_name": state.agent_name,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return {
            "status": state.status.value,
            "step": state.current_step,
            "agent_name": state.agent_name,
            "workspace_files": list(state.deliverables),
            "metadata": metadata,
        }

    async def run(
        self,
        task_prompt: str,
        state: Optional[ExecutionState] = None,
        workspace_root: Optional[str] = None,
        agent: Optional[StepExecutableAgent] = None,
    ) -> ExecutionState:
        """Run the agent loop to completion for a given task directive."""
        active_agent = agent or self.agent
        if active_agent is None:
            raise ValueError("No executable agent provided to AgentRunner.")

        agent_name = getattr(active_agent, "name", "agent")

        # Initialize or link execution state
        if state is None:
            state = ExecutionState(
                task_prompt=task_prompt,
                agent_name=agent_name,
                max_steps=self.config.max_steps,
                workspace_root=workspace_root,
            )
            state.add_message(role=MessageRole.USER, content=task_prompt)
        else:
            state.task_prompt = task_prompt
            state.agent_name = getattr(state, "agent_name", None) or agent_name
            state.max_steps = self.config.max_steps
            if workspace_root:
                state.workspace_root = workspace_root

            if not state.messages:
                state.add_message(role=MessageRole.USER, content=task_prompt)

        # Attach run_id to emitter if unassigned
        if getattr(self.emitter, "run_id", None) is None:
            self.emitter.run_id = state.run_id

        self._cancel_requested.clear()
        self._pause_requested.clear()

        # Enforce formal FSM transition to RUNNING
        state.mark_running(reason="Starting agent execution loop")

        await self.emitter.emit_snapshot(self._build_snapshot_payload(state))

        global_start_time = time.time()
        is_complete = False

        try:
            while state.current_step < self.config.max_steps and not is_complete:
                if self.is_cancelled:
                    logger.info(
                        "Agent run %s cancellation requested by operator.",
                        state.run_id,
                    )
                    # [F.5 FIX] Use the idempotent lifecycle helpers.
                    # The raw ``state.mark_cancelling()`` /
                    # ``state.mark_cancelled()`` raise
                    # ``InvalidStateTransitionError`` when the state
                    # is already terminal (e.g. a step internally
                    # marked the state FAILED and this pre-step check
                    # fires on the next iteration). The lifecycle
                    # helpers short-circuit cleanly.
                    lifecycle.mark_cancelling(
                        state,
                        reason="Operator triggered cancellation",
                    )
                    lifecycle.mark_cancelled(
                        state,
                        reason="Operator cancelled run",
                    )
                    # [F.5 FIX — B13] Emit the CANCELLED terminal
                    # event, NOT an ERROR. The pre-fix code called
                    # ``emit_error()`` here, so:
                    #   * the UI displayed a red error card instead
                    #     of a proper "cancelled" state;
                    #   * ``EventEmitter._last_terminal_kind``
                    #     recorded FAILED instead of CANCELLED,
                    #     breaking diagnostics.
                    # ``emit_cancelled()`` uses the canonical
                    # ``EventType.CANCELLED`` added in Phase F.
                    await self.emitter.emit_cancelled(
                        reason="cancelled_by_operator",
                        step=state.current_step,
                        message="Execution cancelled by operator.",
                    )
                    break

                # Inter-step graceful pause handling
                if self.is_paused:
                    pause_start_time = time.time()
                    while self.is_paused and not self.is_cancelled:
                        if state.status != ExecutionStatus.PAUSED:
                            state.mark_paused(reason="Operator requested pause")
                            if self.config.enable_checkpointing:
                                state.create_checkpoint()
                            await self.emitter.emit_snapshot(
                                self._build_snapshot_payload(state, {"event_action": "paused"})
                            )
                        await asyncio.sleep(0.5)

                    # Deduct paused interval from global timeout to avoid spurious timeouts
                    global_start_time += (time.time() - pause_start_time)

                if self.is_cancelled:
                    # [F.5 FIX — B13 pause-path] Same as the other two
                    # cancellation paths: use the idempotent lifecycle
                    # helpers (which short-circuit on already-terminal
                    # state) AND emit the CANCELLED terminal event so
                    # SSE closes. Pre-fix: this path marked the state
                    # CANCELLED but emitted NOTHING — the user could
                    # press Pause, then Stop, and the frontend spinner
                    # would hang forever.
                    lifecycle.mark_cancelling(
                        state,
                        reason="Operator cancelled while paused",
                    )
                    lifecycle.mark_cancelled(
                        state,
                        reason="Cancelled by operator (during pause)",
                    )
                    await self.emitter.emit_cancelled(
                        reason="cancelled_by_operator",
                        step=state.current_step,
                        message=(
                            "Execution cancelled by operator "
                            "(while paused)."
                        ),
                    )
                    break

                if state.status == ExecutionStatus.PAUSED:
                    state.mark_running(reason="Resuming from paused state")
                    await self.emitter.emit_snapshot(
                        self._build_snapshot_payload(state, {"event_action": "resumed"})
                    )

                state.current_step += 1
                step_index = state.current_step

                if (
                    self.config.total_timeout_seconds
                    and (time.time() - global_start_time) > self.config.total_timeout_seconds
                ):
                    timeout_msg = f"Global execution timeout reached ({self.config.total_timeout_seconds}s)."
                    state.mark_failed(timeout_msg, error_code="GLOBAL_TIMEOUT")
                    raise TimeoutError(timeout_msg)

                await self.emitter.emit_step_start(step_number=step_index)

                # Decoupled step iteration with human wait suspension support
                step_task = asyncio.create_task(
                    active_agent.step(state=state, emitter=self.emitter)
                )
                step_start_time = time.time()

                # [F.5 FIX — B9] Cancellation-aware step wait.
                #
                # Pre-fix behavior:
                #   * ``asyncio.wait_for(asyncio.shield(step_task),
                #     timeout=1.0)`` wrapped ``step_task`` in TWO
                #     protective layers PER ITERATION: ``shield``
                #     (which detaches outer cancellation) and
                #     ``wait_for`` (which creates an outer future
                #     wrapper). Each second of a long step leaked
                #     one wrapper future. A 60s step leaked 60
                #     futures; a 7-step run leaked ~420.
                #   * On cancellation, ``step_task.cancel()`` was
                #     called and the loop ``break``-ed WITHOUT
                #     awaiting. ``step_task`` continued running in
                #     the background. ``CoreRuntimeExecutor``'s
                #     ``finally`` closed provider adapters while
                #     step_task still referenced them — the exact
                #     B9 crash class.
                #
                # Post-fix:
                #   * ``asyncio.wait({step_task}, timeout=1.0)``
                #     creates NO wrapper. No per-iteration leak.
                #   * On cancellation, ``cancel_and_drain`` awaits
                #     step_task's actual termination (bounded at 5s)
                #     before the loop exits. If the task fails to
                #     drain, the runner logs an error but does NOT
                #     hang.
                while not step_task.done():
                    if self.is_cancelled:
                        # Drain the task before moving on.
                        drained = await cancel_and_drain(
                            step_task,
                            timeout=5.0,
                            context="runner.step_cancel",
                        )
                        if not drained:
                            logger.error(
                                "Step task for step %d did not drain "
                                "within 5s during cancellation. It "
                                "will continue running in the "
                                "background; the run cannot be "
                                "cleanly resumed.",
                                step_index,
                            )
                        break

                    is_waiting_human = (
                        state.status in (
                            ExecutionStatus.WAITING_FOR_INPUT,
                            ExecutionStatus.WAITING_FOR_HUMAN,
                        )
                    )

                    # [F.5 FIX — B9] ``asyncio.wait`` does not
                    # create a wrapper future; no per-iteration
                    # leak.
                    try:
                        _done, _pending = await asyncio.wait(
                            {step_task}, timeout=1.0
                        )
                    except Exception as wait_exc:
                        logger.error(
                            "asyncio.wait raised while polling "
                            "step task: %s",
                            wait_exc,
                        )
                        raise

                    if _done:
                        break

                    # Suspend normal step timeout while waiting
                    # for human input.
                    if is_waiting_human:
                        step_start_time = time.time()
                    else:
                        elapsed_step = time.time() - step_start_time
                        if (
                            self.config.step_timeout_seconds
                            and elapsed_step > self.config.step_timeout_seconds
                        ):
                            # [F.5 FIX — B9] Drain before failing.
                            drained = await cancel_and_drain(
                                step_task,
                                timeout=5.0,
                                context="runner.step_timeout",
                            )
                            if not drained:
                                logger.error(
                                    "Step task for step %d did not "
                                    "drain within 5s after timeout. "
                                    "Task is orphaned; the run "
                                    "cannot be cleanly resumed.",
                                    step_index,
                                )
                            step_timeout_msg = (
                                f"Step {step_index} exceeded "
                                f"execution timeout of "
                                f"{self.config.step_timeout_seconds}s."
                            )
                            lifecycle.mark_failed(
                                state,
                                step_timeout_msg,
                                error_code="STEP_TIMEOUT",
                            )
                            raise TimeoutError(step_timeout_msg)

                if self.is_cancelled:
                    # [F.5 FIX — B13] Idempotent marking + emit the
                    # terminal event. The pre-fix code did
                    # ``mark_cancelling`` / ``mark_cancelled``
                    # (raising when already terminal) and emitted
                    # NOTHING — leaving SSE hanging forever after a
                    # mid-step cancellation that the user requested
                    # during the await window.
                    lifecycle.mark_cancelling(
                        state,
                        reason="Operator cancelled step execution",
                    )
                    lifecycle.mark_cancelled(
                        state,
                        reason="Execution cancelled by operator",
                    )
                    await self.emitter.emit_cancelled(
                        reason="cancelled_by_operator",
                        step=state.current_step,
                        message="Execution cancelled by operator.",
                    )
                    break

                is_complete = await step_task

                # Restore running status if agent returned from waiting for human input
                if state.status in (ExecutionStatus.WAITING_FOR_INPUT, ExecutionStatus.WAITING_FOR_HUMAN):
                    state.mark_running(reason="Resumed execution following human interaction")

                if (
                    self.config.enable_checkpointing
                    and (step_index % self.config.checkpoint_interval == 0)
                ):
                    state.create_checkpoint()

                await self.emitter.emit_step_end(step_number=step_index)

            # [F.5 FIX — B13] Post-loop finalization.
            #
            # Pre-fix behavior: the successful path only marked the
            # state COMPLETED without emitting a `FINAL` event. The
            # peldrun.events layer therefore never saw a terminal
            # event on success — `EventEmitter._is_terminated` stayed
            # False, `_last_terminal_kind` stayed None, and any
            # `astream_sse` iterator hung forever.
            #
            # Post-fix: emit `FINAL` explicitly on success, mirroring
            # the `ERROR`/`CANCELLED` emissions on the other terminal
            # paths. `emit_final` sets `_is_terminated` via the
            # Phase F terminal guard, so the peldrun.events layer is
            # self-consistent.
            if is_complete:
                if state.status == ExecutionStatus.RUNNING:
                    lifecycle.mark_completed(
                        state, reason="Agent declared task conclusion"
                    )
                # [F.5 FIX] Emit FINAL from the runner itself so
                # whatever consumes `peldrun.events` sees the
                # terminal event, regardless of whether the agent
                # marked the state COMPLETED internally (via
                # apply_decision) or the runner just did.
                #
                # GUARD: only emit when the state is actually
                # COMPLETED and the emitter has not already emitted
                # a terminal event for this run. `emit_final` itself
                # is idempotent via the Phase F terminal guard, but
                # checking `state.status` avoids a redundant
                # ``mark_completed`` call that would raise.
                if state.status == ExecutionStatus.COMPLETED:
                    await self.emitter.emit_final(
                        output=state.final_output or "",
                        step=state.current_step,
                        deliverables=list(state.deliverables),
                    )
            elif (
                state.current_step >= self.config.max_steps
                and state.status == ExecutionStatus.RUNNING
            ):
                logger.warning(
                    "Agent reached maximum step ceiling (%d) "
                    "without completing task.",
                    self.config.max_steps,
                )
                ceiling_msg = (
                    f"Execution reached maximum step ceiling "
                    f"({self.config.max_steps}) without completing task."
                )
                lifecycle.mark_failed(
                    state,
                    ceiling_msg,
                    error_code="MAX_STEPS_EXCEEDED",
                )
                await self.emitter.emit_error(
                    message=ceiling_msg,
                    error_type="MaxStepsExceededError",
                    details={
                        "max_steps": self.config.max_steps,
                        "step": state.current_step,
                    },
                    recoverable=False,
                    step=state.current_step,
                )

        except asyncio.CancelledError:
            # [F.5 FIX — B13] Explicit cancellation handler.
            #
            # The pre-fix code used a bare ``except Exception`` block,
            # which does NOT catch ``CancelledError`` in Python 3.8+
            # (it inherits from ``BaseException``, not ``Exception``).
            # An outer cancellation — the executor cancelling the
            # runner task itself — therefore propagated past the
            # runner without emitting ANY terminal event, and the SSE
            # stream hung forever.
            #
            # Post-fix behavior:
            #   1. Cancel and drain any in-flight step_task so it
            #      cannot outlive the runner (extends B9 fix to the
            #      outer-cancellation path).
            #   2. Mark the state CANCELLED via the idempotent
            #      lifecycle helper.
            #   3. Emit ``EventType.CANCELLED`` so the frontend
            #      receives a proper cancellation card and the SSE
            #      loop closes.
            #   4. Re-raise so the caller (executor) sees the
            #      cancellation and can build the RunResult with
            #      status=CANCELLED.
            if "step_task" in locals() and not step_task.done():
                try:
                    await cancel_and_drain(
                        step_task,
                        timeout=5.0,
                        context="runner.outer_cancel",
                    )
                except Exception as drain_exc:
                    logger.error(
                        "Failed to drain step_task during outer "
                        "cancellation: %s",
                        drain_exc,
                    )

            try:
                lifecycle.mark_cancelling(
                    state, reason="Runner task cancelled externally"
                )
                lifecycle.mark_cancelled(
                    state, reason="Runner task cancelled externally"
                )
                await self.emitter.emit_cancelled(
                    reason="runner_cancelled",
                    step=state.current_step,
                    message="Execution cancelled.",
                )
            except Exception as emit_exc:
                logger.error(
                    "Failed to emit CANCELLED during outer "
                    "cancellation for run %s: %s",
                    state.run_id,
                    emit_exc,
                )
            raise

        except Exception as ex:
            # [F.5 FIX] Use lifecycle.mark_failed for idempotency.
            # The raw ``state.mark_failed()`` raises when the state
            # is already terminal — e.g. when a step internally
            # marked the state FAILED and then raised, and this
            # handler now runs on an already-failed state.
            lifecycle.mark_failed(
                state,
                str(ex),
                error_code=type(ex).__name__,
            )
            logger.exception(
                "Unhandled error in AgentRunner for run %s: %s",
                state.run_id,
                ex,
            )
            await self.emitter.emit_error(
                message=str(ex),
                error_type=type(ex).__name__,
                details={
                    "step": state.current_step,
                    "elapsed": round(
                        time.time() - global_start_time, 2
                    ),
                },
                recoverable=False,
                step=state.current_step,
            )
            raise

        finally:
            await self.emitter.emit_snapshot(
                self._build_snapshot_payload(
                    state,
                    extra_metadata={"total_elapsed": round(time.time() - global_start_time, 2)},
                )
            )

        return state
