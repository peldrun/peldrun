# backend/peldrun/agents/multi/coordinator.py
"""
PELDRUN Core Multi-Agent Coordinator.
Orchestrates isolated child execution runs, bridges event streams with
explicit lineage tracking, and aggregates deliverables via authoritative Runtime.

Hardened under Phase M3 (PR 5 & PR 7):
- Durable Approval Gate: persists delegation proposals into RunStore; strictly blocks child execution until operator approval.
- Rich Lineage Events: emits explicit DELEGATION_REQUESTED, APPROVAL_*, and CHILD_RUN_* lifecycle events.
- Branching and Concurrency Regulation: enforces max_child_runs and max_parallel_children semaphore ceilings.
- Complete Runtime Authority: routes child execution via execute_fn to enforce ledger and token ceilings.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from peldrun.agents.base import BaseAgent
from peldrun.agents.multi.context_bridge import ContextBridge
from peldrun.agents.multi.protocol import DelegatedTask, DelegationResult, DelegationStatus
from peldrun.agents.multi.workspace import (
    WorkspaceBoundaryError,
    resolve_child_workspace,
    validate_within_boundary,
)
from peldrun.artifacts.manifest import ArtifactRef
from peldrun.engine.runner import AgentRunner, RunnerConfig, StepExecutableAgent
from peldrun.engine.state import ExecutionState, ExecutionStatus, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.runtime.contract import (
    AgentSpec,
    HumanInputRequest,
    HumanInputStatus,
    RunRequest,
    RunResult,
    WorkspaceContext,
)
from peldrun.runtime.store import RunStore, get_run_store

logger = logging.getLogger("peldrun.agents.multi.coordinator")

ExecuteCallable = Callable[..., Awaitable[RunResult]]


class MultiAgentCoordinator:
    """Orchestrates parent-child agent workflows, task delegation, and artifact propagation."""

    def __init__(
        self,
        parent_emitter: EventEmitter,
        workspace_root: Optional[str] = None,
        step_timeout_seconds: float = 120.0,
        default_max_steps: int = 20,
        agent_factory: Optional[Callable[[str], Optional[StepExecutableAgent]]] = None,
        execute_fn: Optional[ExecuteCallable] = None,
        parent_request: Optional[RunRequest] = None,
        run_store: Optional[RunStore] = None,
    ) -> None:
        self.parent_emitter = parent_emitter
        self.workspace_root = workspace_root
        self.step_timeout_seconds = step_timeout_seconds
        self.default_max_steps = default_max_steps
        self._agent_factory = agent_factory
        self._execute_fn = execute_fn
        self._parent_request = parent_request
        self._run_store = run_store or get_run_store()
        self._specialists: Dict[str, Union[StepExecutableAgent, BaseAgent]] = {}
        self._factories: Dict[str, Callable[[], StepExecutableAgent]] = {}
        self._lock = asyncio.Lock()

        # Branching and concurrency tracking
        self._child_run_count: int = 0
        parallel_cap = (
            parent_request.budget.max_parallel_children
            if parent_request and parent_request.budget
            else 2
        )
        self._concurrency_semaphore = asyncio.Semaphore(parallel_cap)

    def register_specialist(
        self,
        agent_id: str,
        agent: Union[StepExecutableAgent, BaseAgent],
    ) -> None:
        """Register a specialist agent template instance."""
        self._specialists[agent_id.lower().strip()] = agent
        logger.debug("Registered specialist agent instance template: '%s'", agent_id)

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
        """Resolve or spawn a strictly isolated specialist agent instance."""
        if specialist_key in self._factories:
            return self._factories[specialist_key]()

        if self._agent_factory is not None:
            spawned = self._agent_factory(specialist_key)
            if spawned is not None:
                return spawned

        if specialist_key in self._specialists:
            candidate = self._specialists[specialist_key]
            if callable(candidate) and not hasattr(candidate, "step"):
                return candidate()
            if hasattr(candidate, "clone") and callable(candidate.clone):
                return candidate.clone()

            logger.error(
                "Specialist '%s' cannot be safely isolated. "
                "No factory or clone() method found. Refusing execution.",
                specialist_key,
            )
            return None

        return None

    @staticmethod
    def _extract_result_text(state: ExecutionState) -> str:
        """Extract final assistant message text from execution state."""
        for msg in reversed(state.messages):
            role_val = msg.get("role")
            if role_val in (MessageRole.ASSISTANT.value, "assistant"):
                content = str(msg.get("content", "")).strip()
                if content:
                    return content
        return "Subtask execution concluded successfully."

    async def _handle_approval_gate(
        self,
        task: DelegatedTask,
        timeout_seconds: float = 300.0,
    ) -> tuple[bool, str]:
        """
        Durable Human Approval Gate.
        Returns: (is_approved: bool, effective_instruction: str)
        """
        approval_id = task.approval_request_id or f"appr_{uuid4().hex[:12]}"
        parent_id_str = str(task.parent_run_id)

        # 1. Create and persist durable approval record
        approval_req = HumanInputRequest(
            request_id=approval_id,
            run_id=parent_id_str,
            question=f"Authorize delegation to specialist [{task.target_agent_id}]?",
            input_type="delegation_approval",
            options=["approve", "reject", "continue_alone"],
            status=HumanInputStatus.PENDING,
            expires_at=time.time() + timeout_seconds,
            metadata={
                "task_id": str(task.task_id),
                "target_agent_id": task.target_agent_id,
                "instruction": task.instruction,
                "budget": task.budget.model_dump(mode="json") if task.budget else {},
            },
        )
        await self._run_store.save_human_request(approval_req)

        # 2. Emit explicit APPROVAL_REQUIRED event
        await self.parent_emitter.emit(
            PeldrunEvent(
                run_id=parent_id_str,
                type=EventType.APPROVAL_REQUIRED,
                payload={
                    "approval_request_id": approval_id,
                    "task_id": str(task.task_id),
                    "parent_run_id": parent_id_str,
                    "target_agent_id": task.target_agent_id,
                    "instruction": task.instruction,
                    "status": "pending",
                    "timeout_seconds": timeout_seconds,
                },
                metadata={
                    "parent_run_id": parent_id_str,
                    "task_id": str(task.task_id),
                    "target_agent": task.target_agent_id,
                },
            )
        )

        # 3. Poll durable store for resolution with low-latency polling
        poll_interval = 0.05
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            stored = await self._run_store.get_human_request(approval_id)
            if stored and stored.status == HumanInputStatus.ANSWERED:
                ans = stored.answer
                decision = "approve"
                edited_inst = task.instruction

                if isinstance(ans, dict):
                    decision = str(ans.get("decision", "approve")).lower()
                    edited_inst = str(ans.get("instruction") or task.instruction)
                elif isinstance(ans, str):
                    decision = ans.lower().strip()

                is_ok = decision in ("approve", "approved", "yes")

                # Emit explicit APPROVAL_RESOLVED event
                await self.parent_emitter.emit(
                    PeldrunEvent(
                        run_id=parent_id_str,
                        type=EventType.APPROVAL_RESOLVED,
                        payload={
                            "approval_request_id": approval_id,
                            "task_id": str(task.task_id),
                            "parent_run_id": parent_id_str,
                            "target_agent_id": task.target_agent_id,
                            "instruction": edited_inst,
                            "status": "resolved",
                            "decision": decision,
                            "edited_instruction": edited_inst if edited_inst != task.instruction else None,
                        },
                        metadata={
                            "parent_run_id": parent_id_str,
                            "task_id": str(task.task_id),
                            "approved": is_ok,
                        },
                    )
                )

                return is_ok, edited_inst

            await asyncio.sleep(poll_interval)

        logger.warning("Approval request %s expired after %s seconds.", approval_id, timeout_seconds)
        return False, task.instruction

    async def delegate(self, task: DelegatedTask) -> DelegationResult:
        """
        Execute a delegated task subject to approval gates, branching limits, and concurrency caps.
        """
        start_time = time.time()
        child_run_id = uuid4()
        specialist_key = task.target_agent_id.lower().strip()
        budget = task.budget
        parent_id_str = str(task.parent_run_id)
        root_id_str = str(task.root_run_id or task.parent_run_id)

        # 1. Emit explicit DELEGATION_REQUESTED event
        await self.parent_emitter.emit(
            PeldrunEvent(
                run_id=parent_id_str,
                type=EventType.DELEGATION_REQUESTED,
                payload={
                    "task_id": str(task.task_id),
                    "parent_run_id": parent_id_str,
                    "target_agent_id": task.target_agent_id,
                    "child_run_id": str(child_run_id),
                    "instruction": task.instruction,
                    "status": "pending",
                },
                metadata={
                    "parent_run_id": parent_id_str,
                    "root_run_id": root_id_str,
                    "target_agent": task.target_agent_id,
                    "task_id": str(task.task_id),
                },
            )
        )

        # 2. Check if Durable Approval Gate is Required
        require_approval = False
        if isinstance(task.context_data, dict) and task.context_data.get("require_approval"):
            require_approval = True
        elif self._parent_request and isinstance(self._parent_request.metadata, dict):
            require_approval = bool(self._parent_request.metadata.get("require_approval"))

        effective_instruction = task.instruction
        if require_approval:
            is_approved, effective_instruction = await self._handle_approval_gate(
                task=task,
                timeout_seconds=budget.timeout_seconds if budget else 300.0,
            )
            if not is_approved:
                err_msg = f"Delegation to [{task.target_agent_id}] was rejected or cancelled by operator."
                logger.info(err_msg)
                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=False,
                    status=DelegationStatus.CANCELLED,
                    error=err_msg,
                    error_code="DELEGATION_REJECTED_BY_OPERATOR",
                    duration_seconds=round(time.time() - start_time, 3),
                )

        # 3. Enforce Branching Ceiling (max_child_runs)
        max_allowed_children = budget.max_child_runs if budget else 3
        async with self._lock:
            if self._child_run_count >= max_allowed_children:
                err_msg = (
                    f"Delegation rejected: Spawning limit exceeded. "
                    f"Maximum allowed child runs is {max_allowed_children}."
                )
                logger.warning(err_msg)
                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=False,
                    status=DelegationStatus.FAILED,
                    error=err_msg,
                    error_code="CHILD_RUN_LIMIT_EXCEEDED",
                    duration_seconds=round(time.time() - start_time, 3),
                )
            self._child_run_count += 1

        # 4. Acquire Concurrency Semaphore
        async with self._concurrency_semaphore:
            child_agent = self._resolve_specialist_instance(specialist_key)
            if not child_agent and self._execute_fn is None:
                err_msg = f"Target specialist agent '{task.target_agent_id}' could not be cleanly initialized."
                logger.error(err_msg)
                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=False,
                    status=DelegationStatus.FAILED,
                    error=err_msg,
                    error_code="SPECIALIST_INITIALIZATION_FAILED",
                    duration_seconds=round(time.time() - start_time, 3),
                )

            # Establish isolated child filesystem sandbox
            parent_ws = task.workspace_root or self.workspace_root or "."
            try:
                child_ws_path = resolve_child_workspace(
                    parent_root=parent_ws,
                    child_run_id=child_run_id,
                )
                effective_workspace = str(child_ws_path)
            except WorkspaceBoundaryError as wb_err:
                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=False,
                    status=DelegationStatus.FAILED,
                    error=str(wb_err),
                    error_code="WORKSPACE_BOUNDARY_VIOLATION",
                    duration_seconds=round(time.time() - start_time, 3),
                )

            # 5. Emit explicit CHILD_RUN_STARTED event
            await self.parent_emitter.emit(
                PeldrunEvent(
                    run_id=parent_id_str,
                    type=EventType.CHILD_RUN_STARTED,
                    payload={
                        "task_id": str(task.task_id),
                        "parent_run_id": parent_id_str,
                        "target_agent_id": task.target_agent_id,
                        "child_run_id": str(child_run_id),
                        "instruction": effective_instruction,
                        "status": "running",
                    },
                    metadata={
                        "parent_run_id": parent_id_str,
                        "child_run_id": str(child_run_id),
                        "task_id": str(task.task_id),
                        "target_agent": task.target_agent_id,
                        "workspace": effective_workspace,
                    },
                )
            )

            # Format prompt via ContextBridge
            try:
                formatted_prompt = ContextBridge.format_child_prompt(
                    instruction=effective_instruction,
                    context_data=task.context_data,
                    delegation_depth=task.delegation_depth,
                )
            except Exception as bridge_err:
                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=False,
                    status=DelegationStatus.FAILED,
                    error=f"Context assembly failed: {str(bridge_err)}",
                    error_code="CONTEXT_ASSEMBLY_FAILED",
                    duration_seconds=round(time.time() - start_time, 3),
                )

            max_steps = budget.max_steps if budget and budget.max_steps else (task.max_steps or self.default_max_steps)
            timeout_seconds = budget.timeout_seconds if budget and budget.timeout_seconds else self.step_timeout_seconds

            async def bridge_child_event(ev: PeldrunEvent) -> None:
                ev_copy = ev.model_copy(deep=True)
                ev_copy.metadata["parent_run_id"] = parent_id_str
                ev_copy.metadata["child_run_id"] = str(child_run_id)
                ev_copy.metadata["delegated_agent"] = task.target_agent_id
                ev_copy.metadata["task_id"] = str(task.task_id)

                if ev.type == EventType.STEP_START:
                    ev_copy.type = EventType.CHILD_RUN_STEP
                await self.parent_emitter.emit(ev_copy)

            # Authoritative Path: Execute via Core Runtime (execute_fn)
            if self._execute_fn is not None:
                child_job_id = f"{parent_id_str}_child_{child_run_id.hex[:8]}"
                child_agent_name = getattr(child_agent, "name", task.target_agent_id) if child_agent else task.target_agent_id
                agent_prompt = getattr(child_agent, "system_prompt", "") if child_agent else ""

                child_spec = AgentSpec(
                    id=task.target_agent_id,
                    name=child_agent_name,
                    system_prompt=agent_prompt,
                    max_steps=max_steps,
                )
                chat_id = getattr(self._parent_request.workspace, "chat_id", None) if self._parent_request else None
                project_id = getattr(self._parent_request.workspace, "project_id", None) if self._parent_request else None

                child_ws_ctx = WorkspaceContext(
                    workspace_id=str(child_run_id),
                    root_path=effective_workspace,
                    chat_id=chat_id,
                    project_id=project_id,
                )

                child_req = RunRequest(
                    run_id=child_run_id,
                    job_id=child_job_id,
                    prompt=formatted_prompt,
                    agent_spec=child_spec,
                    workspace=child_ws_ctx,
                    llm_config=self._parent_request.llm_config if self._parent_request else {},
                    step_timeout_seconds=self.step_timeout_seconds,
                    total_timeout_seconds=timeout_seconds,
                    parent_run_id=parent_id_str,
                    root_run_id=root_id_str,
                    delegator_agent_id=getattr(self._parent_request.agent_spec, "id", "peldrun") if self._parent_request else "peldrun",
                    delegation_depth=task.delegation_depth,
                    delegation_allowed=False,
                    budget=budget,
                )

                try:
                    child_run_res: RunResult = await self._execute_fn(
                        child_req,
                        event_listener=bridge_child_event,
                    )

                    duration = round(time.time() - start_time, 3)
                    is_success = (
                        child_run_res.status == ExecutionStatus.COMPLETED
                        or str(child_run_res.status).lower() == "completed"
                    )
                    is_cancelled = str(child_run_res.status).lower() == "cancelled"

                    outcome_status = DelegationStatus.COMPLETED if is_success else (
                        DelegationStatus.CANCELLED if is_cancelled else DelegationStatus.FAILED
                    )

                    validated_deliverables: List[str] = []
                    child_artifacts: List[ArtifactRef] = []
                    for d in child_run_res.deliverables:
                        try:
                            resolved_file = validate_within_boundary(child_ws_path, d)
                            if resolved_file.exists() and resolved_file.is_file():
                                rel_p = resolved_file.relative_to(child_ws_path).as_posix()
                                validated_deliverables.append(rel_p)
                                child_artifacts.append(ArtifactRef.from_path(resolved_file, child_ws_path))
                        except WorkspaceBoundaryError as esc_err:
                            logger.warning("Deliverable dropped due to boundary violation: %s", esc_err)

                    diag_meta = child_run_res.metadata.get("diagnostics") if isinstance(child_run_res.metadata, dict) else {}
                    tokens_used = diag_meta.get("total_tokens", 0) if isinstance(diag_meta, dict) else 0
                    cost_nano = diag_meta.get("total_cost_nano_usd", 0) if isinstance(diag_meta, dict) else 0

                    terminal_event_type = EventType.CHILD_RUN_COMPLETED if is_success else (
                        EventType.CHILD_RUN_CANCELLED if is_cancelled else EventType.CHILD_RUN_FAILED
                    )

                    await self.parent_emitter.emit(
                        PeldrunEvent(
                            run_id=parent_id_str,
                            type=terminal_event_type,
                            payload={
                                "task_id": str(task.task_id),
                                "parent_run_id": parent_id_str,
                                "target_agent_id": task.target_agent_id,
                                "child_run_id": str(child_run_id),
                                "status": outcome_status.value,
                                "duration_seconds": duration,
                                "deliverables": validated_deliverables,
                                "tokens_used": tokens_used,
                                "cost_nano_usd": cost_nano,
                                "error": child_run_res.error,
                            },
                            metadata={
                                "parent_run_id": parent_id_str,
                                "child_run_id": str(child_run_id),
                                "task_id": str(task.task_id),
                                "target_agent": task.target_agent_id,
                            },
                        )
                    )

                    return DelegationResult(
                        task_id=task.task_id,
                        child_run_id=child_run_id,
                        parent_run_id=task.parent_run_id,
                        root_run_id=task.root_run_id or task.parent_run_id,
                        success=is_success,
                        status=outcome_status,
                        output=child_run_res.output or "",
                        error=child_run_res.error,
                        error_code=child_run_res.error_code,
                        artifacts=child_artifacts,
                        deliverables=validated_deliverables,
                        tokens_used=tokens_used,
                        cost_nano_usd=cost_nano,
                        duration_seconds=duration,
                    )
                except Exception as core_exc:
                    duration = round(time.time() - start_time, 3)
                    err_msg = f"Delegation through Core Runtime failed: {str(core_exc)}"
                    logger.exception(err_msg)

                    await self.parent_emitter.emit(
                        PeldrunEvent(
                            run_id=parent_id_str,
                            type=EventType.CHILD_RUN_FAILED,
                            payload={
                                "task_id": str(task.task_id),
                                "parent_run_id": parent_id_str,
                                "target_agent_id": task.target_agent_id,
                                "child_run_id": str(child_run_id),
                                "status": "failed",
                                "duration_seconds": duration,
                                "error": err_msg,
                            },
                            metadata={
                                "parent_run_id": parent_id_str,
                                "child_run_id": str(child_run_id),
                                "task_id": str(task.task_id),
                            },
                        )
                    )

                    return DelegationResult(
                        task_id=task.task_id,
                        child_run_id=child_run_id,
                        parent_run_id=task.parent_run_id,
                        root_run_id=task.root_run_id or task.parent_run_id,
                        success=False,
                        status=DelegationStatus.FAILED,
                        error=err_msg,
                        error_code="RUNTIME_EXECUTION_ERROR",
                        duration_seconds=duration,
                    )

            # Standalone Runner Fallback (isolated unit testing)
            child_emitter = EventEmitter(run_id=str(child_run_id))
            child_emitter.subscribe_all(bridge_child_event)

            agent_name = getattr(child_agent, "name", task.target_agent_id)
            child_state = ExecutionState(
                run_id=str(child_run_id),
                task_prompt=formatted_prompt,
                agent_name=agent_name,
                max_steps=max_steps,
                workspace_root=effective_workspace,
            )
            child_state.metadata["parent_run_id"] = parent_id_str
            child_state.metadata["root_run_id"] = root_id_str
            child_state.metadata["task_id"] = str(task.task_id)
            child_state.metadata["delegation_depth"] = task.delegation_depth
            child_state.add_message(role=MessageRole.USER, content=formatted_prompt)

            if hasattr(child_agent, "workspace_dir"):
                child_agent.workspace_dir = effective_workspace
            if hasattr(child_agent, "emitter"):
                child_agent.emitter = child_emitter

            runner_config = RunnerConfig(
                max_steps=max_steps,
                step_timeout_seconds=timeout_seconds,
                total_timeout_seconds=timeout_seconds,
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
                    agent=child_agent,
                )

                duration = round(time.time() - start_time, 3)
                output_text = self._extract_result_text(final_state)

                validated_deliverables = []
                child_artifacts = []
                for d in final_state.deliverables:
                    try:
                        resolved_file = validate_within_boundary(child_ws_path, d)
                        if resolved_file.exists() and resolved_file.is_file():
                            rel_path = resolved_file.relative_to(child_ws_path).as_posix()
                            validated_deliverables.append(rel_path)
                            child_artifacts.append(ArtifactRef.from_path(resolved_file, child_ws_path))
                    except WorkspaceBoundaryError as escape_err:
                        logger.warning("Child deliverable dropped due to boundary violation: %s", escape_err)

                is_success = (final_state.status == ExecutionStatus.COMPLETED)
                outcome_status = DelegationStatus.COMPLETED if is_success else DelegationStatus.FAILED

                await self.parent_emitter.emit(
                    PeldrunEvent(
                        run_id=parent_id_str,
                        type=EventType.CHILD_RUN_COMPLETED if is_success else EventType.CHILD_RUN_FAILED,
                        payload={
                            "task_id": str(task.task_id),
                            "parent_run_id": parent_id_str,
                            "target_agent_id": task.target_agent_id,
                            "child_run_id": str(child_run_id),
                            "status": outcome_status.value,
                            "duration_seconds": duration,
                            "deliverables": validated_deliverables,
                        },
                        metadata={
                            "parent_run_id": parent_id_str,
                            "child_run_id": str(child_run_id),
                            "task_id": str(task.task_id),
                        },
                    )
                )

                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=is_success,
                    status=outcome_status,
                    output=output_text,
                    artifacts=child_artifacts,
                    deliverables=validated_deliverables,
                    duration_seconds=duration,
                )

            except Exception as exc:
                duration = round(time.time() - start_time, 3)
                err_msg = f"Delegation to [{task.target_agent_id}] failed: {str(exc)}"
                logger.exception(err_msg)

                await self.parent_emitter.emit(
                    PeldrunEvent(
                        run_id=parent_id_str,
                        type=EventType.CHILD_RUN_FAILED,
                        payload={
                            "task_id": str(task.task_id),
                            "parent_run_id": parent_id_str,
                            "target_agent_id": task.target_agent_id,
                            "child_run_id": str(child_run_id),
                            "status": "failed",
                            "duration_seconds": duration,
                            "error": err_msg,
                        },
                        metadata={
                            "parent_run_id": parent_id_str,
                            "child_run_id": str(child_run_id),
                            "task_id": str(task.task_id),
                        },
                    )
                )

                return DelegationResult(
                    task_id=task.task_id,
                    child_run_id=child_run_id,
                    parent_run_id=task.parent_run_id,
                    root_run_id=task.root_run_id or task.parent_run_id,
                    success=False,
                    status=DelegationStatus.FAILED,
                    error=err_msg,
                    error_code="RUNNER_EXECUTION_ERROR",
                    duration_seconds=duration,
                )
