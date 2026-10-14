# backend/peldrun/agents/multi/coordinator.py
"""
PELDRUN Core Multi-Agent Coordinator.
Orchestrates isolated child execution runs, bridges event streams with
explicit lineage tracking, and aggregates deliverables via authoritative Runtime.

Hardened under Phase M3 (PR 5 & PR 7) and Search Results Plan Phase 7 (SR-701..SR-704):
- Durable Approval Gate: persists delegation proposals into RunStore; strictly blocks child execution until operator approval.
- Rich Lineage Events: emits explicit DELEGATION_REQUESTED, APPROVAL_*, and CHILD_RUN_* lifecycle events.
- Branching and Concurrency Regulation: enforces max_child_runs and max_parallel_children semaphore ceilings.
- Complete Runtime Authority: routes child execution via execute_fn to enforce ledger and token ceilings.
- Explicit Child Tool Authorization & Workspace Isolation: resolves an explicit allowlist of authorized
  tool IDs for each delegated child run, constructs fresh tool instances bound to the child workspace
  root and authoritative search configuration, and passes only permitted tools to CoreRuntimeExecutor.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Union
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
ToolResolverCallable = Callable[..., List[Any]]

_PROHIBITED_CHILD_TOOLS: Set[str] = {"delegate_task", "delegate", "terminate"}
_WRITE_OR_EXEC_TOOLS: Set[str] = {
    "bash",
    "shell_exec",
    "isolated_shell",
    "python_execute",
    "file_saver",
    "str_replace_editor",
}


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
        tool_resolver: Optional[ToolResolverCallable] = None,
    ) -> None:
        self.parent_emitter = parent_emitter
        self.workspace_root = workspace_root
        self.step_timeout_seconds = step_timeout_seconds
        self.default_max_steps = default_max_steps
        self._agent_factory = agent_factory
        self._execute_fn = execute_fn
        self._parent_request = parent_request
        self._run_store = run_store or get_run_store()
        self._tool_resolver = tool_resolver
        self._specialists: Dict[str, Union[StepExecutableAgent, BaseAgent]] = {}
        self._factories: Dict[str, Callable[[], StepExecutableAgent]] = {}
        self._specialist_tools: Dict[str, List[str]] = {}
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
        tools: Optional[Iterable[str]] = None,
    ) -> None:
        """Register a specialist agent template instance and optional authorized tool allowlist."""
        clean_id = agent_id.lower().strip()
        self._specialists[clean_id] = agent
        if tools is not None:
            self._specialist_tools[clean_id] = [str(t).strip() for t in tools if str(t).strip()]
        logger.debug("Registered specialist agent instance template: '%s'", agent_id)

    def register_factory(
        self,
        agent_id: str,
        factory: Callable[[], StepExecutableAgent],
        tools: Optional[Iterable[str]] = None,
    ) -> None:
        """Register an agent factory to spawn clean, isolated instances per delegation run."""
        clean_id = agent_id.lower().strip()
        self._factories[clean_id] = factory
        if tools is not None:
            self._specialist_tools[clean_id] = [str(t).strip() for t in tools if str(t).strip()]
        logger.debug("Registered specialist agent factory: '%s'", agent_id)

    def register_specialist_tools(
        self,
        agent_id: str,
        tools: Iterable[str],
    ) -> None:
        """Explicitly set the authorized tool IDs for a target specialist agent."""
        clean_id = agent_id.lower().strip()
        self._specialist_tools[clean_id] = [str(t).strip() for t in tools if str(t).strip()]

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

    def _resolve_authorized_child_tool_ids(
        self,
        specialist_key: str,
        task: DelegatedTask,
        child_agent: Optional[Any] = None,
    ) -> List[str]:
        """
        Determine the explicit authorized tool-ID list for a delegated child run (SR-701).

        Precedence order:
        1. Explicit per-task tool allowlist (`task.tools` or `task.context_data['tools' | 'allowed_tools' | 'authorized_tools']`).
        2. Explicit coordinator registration (`self._specialist_tools[specialist_key]`).
        3. Parent request metadata mapping (`parent_request.metadata['specialist_tools'][specialist_key]` or `child_tools`).
        4. Specialist agent instance/config `tools` attribute.
        5. Platform agent registry manifest (`omweb.agents.registry.agent_registry`).
        6. Parent `agent_spec.tools` fallback when present.

        Privilege and security constraints:
        - Strips delegation and termination control tools (`delegate_task`, `delegate`, `terminate`).
        - Honors `disallowed_tools` / `blocked_tools` in `task.context_data` or parent metadata.
        - Enforces parent tool ceiling intersection when `enforce_parent_tool_ceiling` is active.
        - Strips write/exec tools if the workspace is marked `read_only`.
        """
        raw_candidates: Optional[List[str]] = None
        ctx_data = task.context_data if isinstance(task.context_data, dict) else {}
        parent_meta = (
            self._parent_request.metadata
            if self._parent_request and isinstance(self._parent_request.metadata, dict)
            else {}
        )

        # 1. Explicit task-level allowlist
        task_tools_attr = getattr(task, "tools", None)
        if isinstance(task_tools_attr, (list, tuple, set)):
            raw_candidates = [str(t).strip() for t in task_tools_attr]
        else:
            for key in ("tools", "allowed_tools", "authorized_tools"):
                if key in ctx_data and isinstance(ctx_data[key], (list, tuple, set)):
                    raw_candidates = [str(t).strip() for t in ctx_data[key]]
                    break

        # 2. Coordinator-registered specialist tools
        if raw_candidates is None and specialist_key in self._specialist_tools:
            raw_candidates = list(self._specialist_tools[specialist_key])

        # 3. Parent request metadata specialist tool mapping
        if raw_candidates is None:
            spec_map = parent_meta.get("specialist_tools")
            if isinstance(spec_map, dict):
                for map_k, map_v in spec_map.items():
                    if str(map_k).lower().strip() == specialist_key and isinstance(map_v, (list, tuple, set)):
                        raw_candidates = [str(t).strip() for t in map_v]
                        break
            if raw_candidates is None and isinstance(parent_meta.get("child_tools"), (list, tuple, set)):
                raw_candidates = [str(t).strip() for t in parent_meta["child_tools"]]

        # 4. Specialist agent object attributes
        if raw_candidates is None and child_agent is not None:
            agent_tools = getattr(child_agent, "tools", None)
            if not isinstance(agent_tools, (list, tuple, set)):
                agent_cfg = getattr(child_agent, "config", None)
                agent_tools = getattr(agent_cfg, "tools", None) if agent_cfg else None
            if isinstance(agent_tools, (list, tuple, set)):
                raw_candidates = [
                    str(getattr(t, "name", t)).strip() for t in agent_tools
                ]

        # 5. Platform Agent Registry lookup for known specialists (e.g. deep_researcher)
        if raw_candidates is None:
            try:
                from omweb.agents.registry import agent_registry  # type: ignore[import-not-found]

                for getter_name in ("get_agent", "get"):
                    getter = getattr(agent_registry, getter_name, None)
                    if callable(getter):
                        manifest = getter(specialist_key) or getter(task.target_agent_id)
                        if isinstance(manifest, dict) and isinstance(manifest.get("tools"), list):
                            raw_candidates = [str(t).strip() for t in manifest["tools"]]
                            break
            except Exception:
                pass

        # 6. Fallback to parent AgentSpec.tools if defined
        if raw_candidates is None and self._parent_request and self._parent_request.agent_spec:
            parent_tools = getattr(self._parent_request.agent_spec, "tools", None)
            if isinstance(parent_tools, (list, tuple, set)):
                raw_candidates = [str(t).strip() for t in parent_tools]

        if not raw_candidates:
            return []

        # Collect blocked/disallowed tools
        blocked: Set[str] = set(_PROHIBITED_CHILD_TOOLS)
        for src in (ctx_data, parent_meta):
            for b_key in ("disallowed_tools", "blocked_tools", "excluded_tools"):
                b_val = src.get(b_key)
                if isinstance(b_val, (list, tuple, set)):
                    blocked.update(str(item).strip() for item in b_val if str(item).strip())

        # Optional parent tool ceiling enforcement
        enforce_ceiling = bool(
            ctx_data.get(
                "enforce_parent_tool_ceiling",
                parent_meta.get("enforce_parent_tool_ceiling", False),
            )
        )
        parent_allowed_set: Optional[Set[str]] = None
        if (
            enforce_ceiling
            and self._parent_request
            and self._parent_request.agent_spec
            and self._parent_request.agent_spec.tools
        ):
            parent_allowed_set = {
                str(t).strip() for t in self._parent_request.agent_spec.tools if str(t).strip()
            }

        read_only_ws = bool(
            ctx_data.get("read_only")
            or (
                self._parent_request
                and self._parent_request.workspace
                and getattr(self._parent_request.workspace, "read_only", False)
            )
        )

        authorized: List[str] = []
        seen: Set[str] = set()
        for raw_tid in raw_candidates:
            tid = str(raw_tid or "").strip()
            if not tid or tid in seen or tid in blocked:
                continue
            if read_only_ws and tid in _WRITE_OR_EXEC_TOOLS:
                continue
            if parent_allowed_set is not None and tid not in parent_allowed_set:
                continue
            seen.add(tid)
            authorized.append(tid)

        return authorized

    def _extract_effective_search_config(self, task: DelegatedTask) -> Optional[Dict[str, Any]]:
        """Extract authoritative search configuration override from task or parent request metadata."""
        if isinstance(task.context_data, dict) and isinstance(task.context_data.get("search_config"), dict):
            return dict(task.context_data["search_config"])

        if self._parent_request and isinstance(self._parent_request.metadata, dict):
            parent_sc = self._parent_request.metadata.get("search_config")
            if isinstance(parent_sc, dict):
                return dict(parent_sc)

        return None

    def _build_child_custom_tools(
        self,
        authorized_tool_ids: List[str],
        child_ws_path: Path,
        *,
        child_run_id: str,
        child_job_id: str,
        chat_id: Optional[str],
        project_id: Optional[str],
        search_config: Optional[Dict[str, Any]],
    ) -> List[Any]:
        """
        Construct fresh, workspace-isolated tool instances for a child run (SR-702, SR-703).
        Never reuses parent-bound mutable tool objects.
        """
        if not authorized_tool_ids:
            return []

        # 1. Custom injected resolver (e.g. from PeldrunEngine or test harness)
        if callable(self._tool_resolver):
            try:
                return list(
                    self._tool_resolver(
                        authorized_tool_ids=authorized_tool_ids,
                        workspace_root=child_ws_path,
                        run_id=child_run_id,
                        job_id=child_job_id,
                        chat_id=chat_id,
                        project_id=project_id,
                        search_config=search_config,
                    )
                )
            except TypeError:
                return list(self._tool_resolver(authorized_tool_ids, child_ws_path))

        # 2. Canonical Web-to-Core adapter resolver
        try:
            from omweb.adapters.core_adapter_parts.resolve_tool_runtime import (  # type: ignore[import-not-found]
                resolve_authorized_tools,
            )

            resolved = resolve_authorized_tools(
                authorized_tool_ids=authorized_tool_ids,
                workspace_root=child_ws_path,
                run_id=child_run_id,
                job_id=child_job_id,
                chat_id=chat_id,
                project_id=project_id,
                search_config=search_config,
            )
            if resolved:
                return resolved
        except Exception as adapter_exc:
            logger.debug("Web adapter child tool resolution fallback notice: %s", adapter_exc)

        # 3. Pure peldrun-core builtin fallback (for standalone Core unit/integration tests)
        core_tools: List[Any] = []
        ws_str = str(child_ws_path)
        for tid in authorized_tool_ids:
            try:
                if tid == "web_search":
                    from peldrun.tools.builtins.web_search import WebSearchTool

                    core_tools.append(
                        WebSearchTool(
                            workspace_root=ws_str,
                            search_config=search_config,
                            run_id=child_run_id,
                            job_id=child_job_id,
                            chat_id=chat_id,
                            project_id=project_id,
                        )
                    )
                elif tid == "python_execute":
                    from peldrun.tools.builtins.isolated_shell import IsolatedPythonTool

                    core_tools.append(IsolatedPythonTool(workspace_root=ws_str))
                elif tid in ("bash", "shell_exec", "isolated_shell"):
                    from peldrun.tools.builtins.shell_exec import ShellExecTool

                    core_tools.append(ShellExecTool(workspace_root=ws_str))
                elif tid == "str_replace_editor":
                    from peldrun.tools.builtins.str_replace_editor import StrReplaceEditorTool

                    core_tools.append(StrReplaceEditorTool(workspace_root=ws_str))
                elif tid == "file_saver":
                    from peldrun.tools.builtins.file_ops import FileWriteTool

                    core_tools.append(FileWriteTool(workspace_root=ws_str))
                elif tid == "file_reader":
                    from peldrun.tools.builtins.file_ops import FileReadTool

                    core_tools.append(FileReadTool(workspace_root=ws_str))
            except Exception as tool_err:
                logger.warning("Failed to build Core tool %r for child run %s: %s", tid, child_run_id, tool_err)

        return core_tools

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
        Execute a delegated task subject to approval gates, branching limits,
        concurrency caps, and explicit child tool authorization.
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

            # Resolve explicit authorized tools for this child run (SR-701)
            authorized_tool_ids = self._resolve_authorized_child_tool_ids(
                specialist_key=specialist_key,
                task=task,
                child_agent=child_agent,
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
                        "authorized_tools": authorized_tool_ids,
                        "status": "running",
                    },
                    metadata={
                        "parent_run_id": parent_id_str,
                        "child_run_id": str(child_run_id),
                        "task_id": str(task.task_id),
                        "target_agent": task.target_agent_id,
                        "workspace": effective_workspace,
                        "authorized_tools": authorized_tool_ids,
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
                    tools=authorized_tool_ids,
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

                effective_search_cfg = self._extract_effective_search_config(task)
                child_metadata: Dict[str, Any] = {
                    "authorized_tools": list(authorized_tool_ids),
                    "parent_run_id": parent_id_str,
                    "root_run_id": root_id_str,
                }
                if effective_search_cfg is not None:
                    child_metadata["search_config"] = effective_search_cfg

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
                    metadata=child_metadata,
                )

                # Build workspace-bound tool instances strictly from authorized_tool_ids (SR-702, SR-703)
                child_custom_tools = self._build_child_custom_tools(
                    authorized_tool_ids=authorized_tool_ids,
                    child_ws_path=child_ws_path,
                    child_run_id=str(child_run_id),
                    child_job_id=child_job_id,
                    chat_id=chat_id,
                    project_id=project_id,
                    search_config=effective_search_cfg,
                )

                try:
                    exec_kwargs: Dict[str, Any] = {"event_listener": bridge_child_event}
                    if child_custom_tools:
                        try:
                            sig = inspect.signature(self._execute_fn)
                            if "custom_tools" in sig.parameters or any(
                                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                            ):
                                exec_kwargs["custom_tools"] = child_custom_tools
                        except (TypeError, ValueError):
                            exec_kwargs["custom_tools"] = child_custom_tools

                    child_run_res: RunResult = await self._execute_fn(
                        child_req,
                        **exec_kwargs,
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
            child_state.metadata["authorized_tools"] = list(authorized_tool_ids)
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
