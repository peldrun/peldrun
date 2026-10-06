"""
backend/peldrun/engine/runner.py

PELDRUN Core Agent Execution Runner.
Coordinates agent lifecycle, asynchronous execution loops, checkpointing, and event emission.
Hardened under PELDRUN Runtime V1:
- Semantic cancellation pipeline: RUNNING -> CANCELLING -> CANCELLED (never PAUSED).
- Max-step exhaustion handled strictly as FAILED with MAX_STEPS_EXCEEDED (never fake COMPLETED).
- All lifecycle modifications pass strictly through ExecutionState FSM validators.
- Timeout suspension preserved during human interactions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

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
    step_timeout_seconds: float = Field(default=120.0, description="Timeout ceiling per individual step")
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
        """Signal execution pause."""
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
                    logger.info("Agent run %s cancellation requested by operator.", state.run_id)
                    state.mark_cancelling(reason="Operator triggered cancellation")
                    state.mark_cancelled(reason="Operator cancelled run")
                    await self.emitter.emit_error(
                        message="Execution cancelled by operator.",
                        error_type="CancelledError",
                        recoverable=False,
                        step=state.current_step,
                    )
                    break

                while self.is_paused and not self.is_cancelled:
                    if state.status != ExecutionStatus.PAUSED:
                        state.mark_paused(reason="Operator requested pause")
                    await asyncio.sleep(0.5)

                if self.is_cancelled:
                    state.mark_cancelling(reason="Operator cancelled run during pause")
                    state.mark_cancelled(reason="Cancelled by operator")
                    break

                if state.status == ExecutionStatus.PAUSED:
                    state.mark_running(reason="Resuming from paused state")

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

                while not step_task.done():
                    if self.is_cancelled:
                        step_task.cancel()
                        break

                    is_waiting_human = (
                        state.status in (ExecutionStatus.WAITING_FOR_INPUT, ExecutionStatus.WAITING_FOR_HUMAN)
                    )

                    try:
                        await asyncio.wait_for(asyncio.shield(step_task), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

                    if step_task.done():
                        break

                    # Suspend normal step timeout while waiting for human input
                    if is_waiting_human:
                        step_start_time = time.time()
                    else:
                        elapsed_step = time.time() - step_start_time
                        if (
                            self.config.step_timeout_seconds
                            and elapsed_step > self.config.step_timeout_seconds
                        ):
                            step_task.cancel()
                            step_timeout_msg = (
                                f"Step {step_index} exceeded execution timeout of {self.config.step_timeout_seconds}s."
                            )
                            state.mark_failed(step_timeout_msg, error_code="STEP_TIMEOUT")
                            raise TimeoutError(step_timeout_msg)

                if self.is_cancelled:
                    if not state.is_terminal:
                        state.mark_cancelling(reason="Operator cancelled step execution")
                        state.mark_cancelled(reason="Execution cancelled by operator")
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

            # Post-loop status finalization adhering strictly to Runtime V1 semantics
            if is_complete:
                if state.status == ExecutionStatus.RUNNING:
                    state.mark_completed(reason="Agent declared task conclusion")
            elif (
                state.current_step >= self.config.max_steps
                and state.status == ExecutionStatus.RUNNING
            ):
                logger.warning("Agent reached maximum step ceiling (%d) without completing task.", self.config.max_steps)
                ceiling_msg = f"Execution reached maximum step ceiling ({self.config.max_steps}) without completing task."
                state.mark_failed(ceiling_msg, error_code="MAX_STEPS_EXCEEDED")
                await self.emitter.emit_error(
                    message=ceiling_msg,
                    error_type="MaxStepsExceededError",
                    details={"max_steps": self.config.max_steps, "step": state.current_step},
                    recoverable=False,
                    step=state.current_step,
                )

        except Exception as ex:
            if not state.is_terminal:
                state.mark_failed(str(ex), error_code=type(ex).__name__)
            logger.exception("Unhandled error in AgentRunner for run %s: %s", state.run_id, ex)
            await self.emitter.emit_error(
                message=str(ex),
                error_type=type(ex).__name__,
                details={"step": state.current_step, "elapsed": round(time.time() - global_start_time, 2)},
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