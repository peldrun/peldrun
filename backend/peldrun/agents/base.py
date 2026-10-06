"""
PELDRUN Core Base Agent Architecture.
Defines abstract agent interfaces, execution lifecycle loops, event dispatching,
and coordination across state, LLM providers, tools, memory, and sandboxes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from peldrun.engine.state import ExecutionState
from peldrun.events.emitter import EventEmitter
from peldrun.llm.providers.openai_compat import BaseLLMProvider
from peldrun.memory import MemoryManager
from peldrun.sandbox.base import BaseSandbox
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.agents.base")


class AgentConfig(BaseModel):
    """Configuration governing agent operational boundaries and behavioral defaults."""
    model_config = ConfigDict(extra="allow")

    name: str = Field(default="base_agent", description="Agent identifier string")
    description: str = Field(default="Autonomous base agent", description="Agent functional description")
    max_steps: int = Field(default=30, ge=1, le=200, description="Maximum execution step ceiling")
    system_prompt: Optional[str] = Field(default=None, description="Primary system instruction prompt")
    workspace_root: Optional[str] = Field(default=None, description="Bounded filesystem workspace root")
    timeout_seconds: float = Field(default=600.0, ge=5.0, description="Overall execution timeout in seconds")


class BaseAgent(ABC):
    """
    Abstract base class for all autonomous agents in PELDRUN Core.
    Coordinates cognition cycles, tool dispatching, memory synchronization,
    and event broadcasting.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm_provider: Optional[BaseLLMProvider] = None,
        tool_registry: Optional[ToolRegistry] = None,
        memory_manager: Optional[MemoryManager] = None,
        sandbox: Optional[BaseSandbox] = None,
        emitter: Optional[EventEmitter] = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.llm = llm_provider
        self.tools = tool_registry or ToolRegistry(workspace_root=self.config.workspace_root)
        self.memory = memory_manager or MemoryManager(
            workspace_root=self.config.workspace_root,
            system_prompt=self.config.system_prompt,
        )
        self.sandbox = sandbox
        self.emitter = emitter or EventEmitter()

        self.state: ExecutionState = ExecutionState()
        self._is_running: bool = False
        self._is_paused: bool = False
        self._stop_requested: bool = False
        self._step_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Agent identifier string conforming to StepExecutableAgent."""
        return self.config.name

    @property
    def is_running(self) -> bool:
        """True if the agent execution loop is actively progressing."""
        return self._is_running

    @property
    def is_paused(self) -> bool:
        """True if execution is temporarily suspended."""
        return self._is_paused

    def pause(self) -> None:
        """Signal the agent to pause before initiating the next step."""
        self._is_paused = True
        logger.info("Agent '%s' pause signaled.", self.config.name)

    def resume(self) -> None:
        """Resume execution of a paused agent."""
        self._is_paused = False
        logger.info("Agent '%s' resumed.", self.config.name)

    def stop(self) -> None:
        """Request immediate halt of the agent execution loop."""
        self._stop_requested = True
        logger.info("Agent '%s' stop requested.", self.config.name)

    async def ainitialize(self) -> None:
        """Bootstrap resources across memory, sandbox, and tool bindings."""
        if self.config.workspace_root:
            self.tools.set_workspace_root(self.config.workspace_root)
            if self.sandbox:
                self.sandbox.set_workspace_root(self.config.workspace_root)
                await self.sandbox.ainitialize()

        await self.memory.ainitialize()

    @abstractmethod
    async def _astep(self) -> bool:
        """
        Execute a single cognition-action step.
        Subclasses must implement their specific logic (e.g. ReAct reasoning, plan refinement).
        Returns True if execution should continue to the next step, False if completed or halted.
        """
        ...

    async def step(
        self,
        state: Optional[ExecutionState] = None,
        emitter: Optional[EventEmitter] = None,
    ) -> bool:
        """
        Execute a single reasoning/action iteration conforming to StepExecutableAgent Protocol.
        Synchronizes state, emitter, and memory context, then executes _astep().
        Returns True if the task has reached completion, False to continue iterating.
        """
        if state is not None:
            self.state = state
        if emitter is not None:
            self.emitter = emitter

        # Synchronize short-term memory dialogue from state if needed
        if hasattr(self, "memory") and hasattr(self.memory, "short_term"):
            if not self.memory.short_term.messages and self.state.messages:
                for msg in self.state.messages:
                    self.memory.short_term.add_message(
                        role=msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                        content=msg.content,
                        name=msg.name,
                        tool_calls=msg.tool_calls,
                        tool_call_id=msg.tool_call_id,
                    )

        async with self._step_lock:
            should_continue = await self._astep()

        is_done = (not should_continue) or self.state.is_completed

        # Guarantee final event broadcast upon conclusion
        if is_done and self.state.is_completed:
            await self.emitter.emit_final(
                output=self.state.final_output or "Task finished.",
                step=self.state.current_step,
            )

        return is_done

    async def arun(self, task: str, **kwargs: Any) -> ExecutionState:
        """
        Main asynchronous execution entrypoint.
        Bootstraps context, enters step loop, and guarantees terminal event emission.
        """
        if self._is_running:
            raise RuntimeError(f"Agent '{self.config.name}' is already running an active task.")

        self._is_running = True
        self._stop_requested = False

        # Reset or initialize state
        self.state = ExecutionState()
        self.state.metadata["task"] = task
        self.state.metadata["agent_name"] = self.config.name

        start_time = time.time()
        logger.info("Agent '%s' started task: '%s'", self.config.name, task[:80])

        try:
            await self.ainitialize()

            # Synthesize system prompt enriched with long-term memory
            enriched_system_prompt = await self.memory.compile_system_prompt(query=task)
            self.state.add_message("system", enriched_system_prompt)

            # Record initial user goal
            self.state.add_message("user", task)
            self.memory.short_term.add_message("user", task)

            # Broadcast initial snapshot
            await self.emitter.emit_snapshot(self.state.to_snapshot_dict())

            # Step execution loop
            while self.state.step < self.config.max_steps:
                if self._stop_requested:
                    logger.info("Execution halted per stop request.")
                    break

                # Handle pause state
                while self._is_paused:
                    await asyncio.sleep(0.5)
                    if self._stop_requested:
                        break

                if self._stop_requested:
                    break

                # Check overall timeout ceiling
                elapsed = time.time() - start_time
                if elapsed > self.config.timeout_seconds:
                    timeout_msg = f"Task exceeded maximum timeout limit of {self.config.timeout_seconds}s."
                    logger.warning(timeout_msg)
                    self.state.mark_error(timeout_msg)
                    await self.emitter.emit_error(timeout_msg)
                    break

                self.state.step += 1
                await self.emitter.emit_step_start(step_number=self.state.step)

                # Execute one concrete step under lock
                async with self._step_lock:
                    should_continue = await self._astep()

                await self.emitter.emit_step_end(step_number=self.state.step)
                await self.emitter.emit_snapshot(self.state.to_snapshot_dict())

                if not should_continue or self.state.is_completed:
                    break

            # Handle execution completion
            if not self.state.is_completed and not self.state.is_error:
                if self.state.step >= self.config.max_steps:
                    self.state.mark_completed(output=f"Reached maximum step limit ({self.config.max_steps}).")
                else:
                    self.state.mark_completed(output="Task completed.")

            if self.state.is_completed:
                await self.emitter.emit_final(output=self.state.final_output or "Task finished.")

            # Record final result into long-term memory if successful
            if self.state.is_completed and self.state.final_output:
                await self.memory.long_term.aadd_memory(
                    content=f"Task: {task} | Outcome: {str(self.state.final_output)[:250]}",
                    category="task_summary",
                    tags=[self.config.name, "execution_history"],
                )

        except asyncio.CancelledError:
            logger.info("Agent task was cancelled.")
            self.state.mark_error("Task execution cancelled.")
            await self.emitter.emit_error("Task execution cancelled.")
            raise

        except Exception as ex:
            logger.exception("Agent execution encountered an unhandled exception: %s", ex)
            err_str = f"Execution failure: {str(ex)}"
            self.state.mark_error(err_str)
            await self.emitter.emit_error(err_str)

        finally:
            self._is_running = False
            if self.sandbox:
                await self.sandbox.acleanup()
            logger.info("Agent '%s' execution concluded at step %d.", self.config.name, self.state.step)

        return self.state