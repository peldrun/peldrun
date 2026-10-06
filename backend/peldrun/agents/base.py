"""
backend/peldrun/agents/base.py

PELDRUN Core Base Agent Architecture.
Defines abstract agent interfaces conforming to the StepExecutableAgent protocol.
Hardened under PR 2 (Single Execution Authority):
- Removes duplicate while loops and competing runner logic from BaseAgent.
- arun() delegates strictly to AgentRunner.
- Retains step() coordinating cognitive cycles with state and memory synchronization.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional
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
    Acts as the cognitive delegate executed by AgentRunner under the StepExecutableAgent protocol.
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
        self._step_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Agent identifier string conforming to StepExecutableAgent."""
        return self.config.name

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

        # Synchronize short-term memory dialogue from state if present
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
        return is_done

    async def arun(self, task: str, **kwargs: Any) -> ExecutionState:
        """
        Authoritative asynchronous execution entrypoint.
        Delegates entirely to AgentRunner, enforcing single runtime authority.
        """
        from peldrun.engine.runner import AgentRunner, RunnerConfig

        await self.ainitialize()

        runner_config = RunnerConfig(
            max_steps=self.config.max_steps,
            total_timeout_seconds=self.config.timeout_seconds,
        )
        runner = AgentRunner(
            agent=self,
            emitter=self.emitter,
            config=runner_config,
        )

        try:
            return await runner.run(
                task_prompt=task,
                workspace_root=self.config.workspace_root,
                agent=self,
            )
        finally:
            if self.sandbox:
                await self.sandbox.acleanup()