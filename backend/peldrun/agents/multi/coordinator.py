"""
PELDRUN Core Multi-Agent Coordinator.
Manages registry of specialized sub-agents, delegates child tasks,
attaches child event streams to parent execution contexts, and aggregates deliverables.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from peldrun.agents.base import BaseAgent
from peldrun.agents.multi.protocol import DelegatedTask, DelegationResult
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent

logger = logging.getLogger("peldrun.agents.multi.coordinator")


class MultiAgentCoordinator:
    """Orchestrates parent-child agent workflows and task delegation."""

    def __init__(self, parent_emitter: EventEmitter):
        self.parent_emitter = parent_emitter
        self._specialists: Dict[str, BaseAgent] = {}
        self._lock = asyncio.Lock()

    def register_specialist(self, agent_id: str, agent: BaseAgent) -> None:
        """Register a specialist agent ready for delegation."""
        self._specialists[agent_id.lower().strip()] = agent
        logger.debug("Registered specialist agent: '%s'", agent_id)

    def list_specialists(self) -> List[str]:
        """Return list of all registered specialist identifiers."""
        return list(self._specialists.keys())

    async def delegate(self, task: DelegatedTask) -> DelegationResult:
        """Execute a delegated task using a dedicated child execution context."""
        start_time = time.time()
        child_run_id = uuid4()
        specialist_key = task.target_agent_id.lower().strip()
        agent = self._specialists.get(specialist_key)

        if not agent:
            err_msg = f"Target specialist agent '{task.target_agent_id}' is not registered."
            logger.error(err_msg)
            return DelegationResult(
                task_id=task.task_id,
                child_run_id=child_run_id,
                success=False,
                error=err_msg,
                duration_seconds=round(time.time() - start_time, 3),
            )

        # Broadcast delegation start on parent stream
        await self.parent_emitter.emit_agent_activity(
            phase="delegating",
            message=f"Delegating task to [{task.target_agent_id}]: {task.instruction[:80]}",
            step=1,
            metadata={"child_run_id": str(child_run_id), "task_id": str(task.task_id)},
        )

        # Bridge child events to parent emitter with lineage metadata
        child_emitter = EventEmitter(run_id=child_run_id)

        async def bridge_child_event(ev: PeldrunEvent) -> None:
            # Re-emit on parent with child tracking metadata
            ev_copy = ev.model_copy(deep=True)
            ev_copy.metadata["parent_run_id"] = str(task.parent_run_id)
            ev_copy.metadata["delegated_agent"] = task.target_agent_id
            await self.parent_emitter.emit(ev_copy)

        child_emitter.subscribe_all(bridge_child_event)

        # Assign child emitter to the agent temporarily
        original_emitter = getattr(agent, "emitter", None)
        setattr(agent, "emitter", child_emitter)

        try:
            # Execute delegated task
            output = await agent.run_task(prompt=task.instruction)
            duration = round(time.time() - start_time, 3)

            await self.parent_emitter.emit_agent_activity(
                phase="delegation_completed",
                message=f"Specialist [{task.target_agent_id}] completed task successfully.",
                step=1,
                metadata={"child_run_id": str(child_run_id), "task_id": str(task.task_id)},
            )

            return DelegationResult(
                task_id=task.task_id,
                child_run_id=child_run_id,
                success=True,
                output=output,
                duration_seconds=duration,
            )

        except Exception as exc:
            duration = round(time.time() - start_time, 3)
            err_msg = f"Delegation to [{task.target_agent_id}] failed: {str(exc)}"
            logger.exception(err_msg)

            await self.parent_emitter.emit_error(
                error=err_msg,
                step=1,
                metadata={"child_run_id": str(child_run_id), "task_id": str(task.task_id)},
            )

            return DelegationResult(
                task_id=task.task_id,
                child_run_id=child_run_id,
                success=False,
                error=err_msg,
                duration_seconds=duration,
            )
        finally:
            setattr(agent, "emitter", original_emitter)