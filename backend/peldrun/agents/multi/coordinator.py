"""
backend/peldrun/agents/multi/coordinator.py

PELDRUN Core Multi-Agent Coordinator.
Orchestrates isolated child execution runs, bridges event streams with
explicit lineage tracking, and aggregates deliverables via AgentRunner.

Hardened under Phase M3:
- Completely eliminates mutable emitter swapping on shared agent instances.
- Drives child agent execution exclusively via AgentRunner lifecycle authority.
- Enforces per-child ExecutionState and isolated event streams.
- Aggregates child deliverables and artifacts into parent execution contexts.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Union
from uuid import UUID, uuid4

from peldrun.agents.base import BaseAgent
from peldrun.agents.multi.protocol import DelegatedTask, DelegationResult
from peldrun.artifacts.manifest import ArtifactRef
from peldrun.engine.runner import AgentRunner, RunnerConfig, StepExecutableAgent
from peldrun.engine.state import ExecutionState, ExecutionStatus, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent

logger = logging.getLogger("peldrun.agents.multi.coordinator")


class MultiAgentCoordinator:
    """Orchestrates parent-child agent workflows, task delegation, and artifact propagation."""

    def __init__(
        self,
        parent_emitter: EventEmitter,
        workspace_root: Optional[str] = None,
        step_timeout_seconds: float = 120.0,
        default_max_steps: int = 20,
    ) -> None:
        self.parent_emitter = parent_emitter
        self.workspace_root = workspace_root
        self.step_timeout_seconds = step_timeout_seconds
        self.default_max_steps = default_max_steps
        self._specialists: Dict[str, Union[StepExecutableAgent, BaseAgent]] = {}
        self._factories: Dict[str, Callable[[], StepExecutableAgent]] = {}
        self._lock = asyncio.Lock()

    def register_specialist(
        self,
        agent_id: str,
        agent: Union[StepExecutableAgent, BaseAgent],
    ) -> None:
        """Register a specialist agent instance ready for delegation."""
        self._specialists[agent_id.lower().strip()] = agent
        logger.debug("Registered specialist agent instance: '%s'", agent_id)

    def register_factory(
        self,
        agent_id: str,
        factory: Callable[[], StepExecutableAgent],
    ) -> None:
        """Register an agent factory to spawn clean, isolated instances per delegation run."""
        self._factories[agent_id.lower().strip()] = factory
        logger.debug("Registered specialist agent factory: '%s'", agent_id)

    def list_specialists(self) -> List[str]:
        """Return a sorted list of all available specialist identifiers."""
        keys = set(self._specialists.keys()) | set(self._factories.keys())
        return sorted(list(keys))

    def _resolve_specialist_instance(self, specialist_key: str) -> Optional[StepExecutableAgent]:
        """Resolve or spawn an isolated specialist agent instance for execution."""
        # 1. Prefer factory instantiation for true concurrency isolation
        if specialist_key in self._factories:
            return self._factories[specialist_key]()

        # 2. Resolve registered template or instance
        if specialist_key in self._specialists:
            candidate = self._specialists[specialist_key]
            if callable(candidate) and not hasattr(candidate, "step"):
                return candidate()
            if hasattr(candidate, "clone") and callable(candidate.clone):
                return candidate.clone()
            # Shallow-copy instance attributes to isolate execution state references
            try:
                return copy.copy(candidate)
            except Exception:
                return candidate

        return None

    @staticmethod
    def _extract_result_text(state: ExecutionState) -> str:
        """Extract the final assistant response from execution state messages."""
        for msg in reversed(state.messages):
            role_val = msg.get("role")
            if role_val in (MessageRole.ASSISTANT.value, "assistant"):
                content = str(msg.get("content", "")).strip()
                if content:
                    return content
        return "Subtask execution concluded successfully."

    async def delegate(self, task: DelegatedTask) -> DelegationResult:
        """
        Execute a delegated task using an isolated child execution context.

        Guarantees:
        - Independent child run_id and dedicated EventEmitter.
        - Hierarchical event bridging with lineage tracking metadata.
        - Concurrency-safe execution driven by AgentRunner.
        - Aggregation of produced child deliverables.
        """
        start_time = time.time()
        child_run_id = uuid4()
        specialist_key = task.target_agent_id.lower().strip()

        child_agent = self._resolve_specialist_instance(specialist_key)
        if not child_agent:
            err_msg = f"Target specialist agent '{task.target_agent_id}' is not registered."
            logger.error(err_msg)
            return DelegationResult(
                task_id=task.task_id,
                child_run_id=child_run_id,
                success=False,
                error=err_msg,
                duration_seconds=round(time.time() - start_time, 3),
            )

        # 1. Broadcast delegation initiation on parent stream
        await self.parent_emitter.emit_agent_activity(
            phase="delegating",
            message=f"Delegating subtask to [{task.target_agent_id}]: {task.instruction[:80]}",
            step=1,
            metadata={
                "child_run_id": str(child_run_id),
                "parent_run_id": str(task.parent_run_id),
                "task_id": str(task.task_id),
                "target_agent": task.target_agent_id,
            },
        )

        # 2. Wire child emitter and bridge events to parent with strict lineage metadata
        child_emitter = EventEmitter(run_id=child_run_id)

        async def bridge_child_event(ev: PeldrunEvent) -> None:
            ev_copy = ev.model_copy(deep=True)
            ev_copy.metadata["parent_run_id"] = str(task.parent_run_id)
            ev_copy.metadata["child_run_id"] = str(child_run_id)
            ev_copy.metadata["delegated_agent"] = task.target_agent_id
            ev_copy.metadata["task_id"] = str(task.task_id)
            await self.parent_emitter.emit(ev_copy)

        child_emitter.subscribe_all(bridge_child_event)

        # 3. Format prompt and inject context parameters
        formatted_prompt = task.instruction
        if task.context_data:
            formatted_prompt = (
                f"[DELEGATED CONTEXT]\n"
                f"{json.dumps(task.context_data, ensure_ascii=False, indent=2)}\n\n"
                f"[TASK DIRECTIVE]\n"
                f"{task.instruction}"
            )

        # 4. Initialize isolated ExecutionState
        effective_workspace = task.workspace_root or self.workspace_root
        max_steps = task.max_steps or self.default_max_steps
        agent_name = getattr(child_agent, "name", task.target_agent_id)

        child_state = ExecutionState(
            task_prompt=formatted_prompt,
            agent_name=agent_name,
            max_steps=max_steps,
            workspace_root=effective_workspace,
        )
        child_state.run_id = child_run_id
        child_state.metadata["parent_run_id"] = str(task.parent_run_id)
        child_state.metadata["task_id"] = str(task.task_id)
        child_state.metadata["context_data"] = task.context_data
        child_state.add_message(role=MessageRole.USER, content=formatted_prompt)

        # 5. Execute via authoritative AgentRunner
        runner_config = RunnerConfig(
            max_steps=max_steps,
            step_timeout_seconds=self.step_timeout_seconds,
        )
        runner = AgentRunner(
            agent=child_agent,
            emitter=child_emitter,
            config=runner_config,
        )

        try:
            final_state = await runner.run(
                task_prompt=formatted_prompt,
                state=child_state,
                workspace_root=effective_workspace,
            )

            duration = round(time.time() - start_time, 3)
            output_text = self._extract_result_text(final_state)

            # 6. Reconcile deliverables and artifacts generated during the child run
            deliverables = list(final_state.deliverables)
            child_artifacts: List[ArtifactRef] = []
            if effective_workspace and Path(effective_workspace).exists():
                ws_path = Path(effective_workspace)
                for d in deliverables:
                    fpath = ws_path / d
                    if fpath.exists() and fpath.is_file():
                        child_artifacts.append(ArtifactRef.from_path(fpath, ws_path))

            # 7. Broadcast delegation completion on parent stream
            await self.parent_emitter.emit_agent_activity(
                phase="delegation_completed",
                message=f"Specialist [{task.target_agent_id}] completed task successfully.",
                step=1,
                metadata={
                    "child_run_id": str(child_run_id),
                    "parent_run_id": str(task.parent_run_id),
                    "task_id": str(task.task_id),
                    "duration_seconds": duration,
                    "deliverables": deliverables,
                },
            )

            return DelegationResult(
                task_id=task.task_id,
                child_run_id=child_run_id,
                success=(final_state.status == ExecutionStatus.COMPLETED),
                output=output_text,
                artifacts=child_artifacts,
                deliverables=deliverables,
                duration_seconds=duration,
            )

        except Exception as exc:
            duration = round(time.time() - start_time, 3)
            err_msg = f"Delegation to [{task.target_agent_id}] failed: {str(exc)}"
            logger.exception(err_msg)

            await self.parent_emitter.emit_error(
                message=err_msg,
                step=1,
                metadata={
                    "child_run_id": str(child_run_id),
                    "parent_run_id": str(task.parent_run_id),
                    "task_id": str(task.task_id),
                },
            )

            return DelegationResult(
                task_id=task.task_id,
                child_run_id=child_run_id,
                success=False,
                error=err_msg,
                duration_seconds=duration,
            )