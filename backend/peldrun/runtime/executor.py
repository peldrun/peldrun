"""
backend/peldrun/runtime/executor.py

PELDRUN Core Runtime Executor.

Authoritative execution gateway inside peldrun-core.
Encapsulates AgentRunner, ToolCallAgent, ExecutionState, and EventEmitter.
Fully instrumented with P1-05 Structured Diagnostics & Latency Telemetry.
Persists immutable invocation records into token_ledger via usage_store and pricing_store.
Adheres strictly to the Runtime V1 contract in peldrun.runtime.contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
import uuid

from peldrun.agents.base import AgentConfig
from peldrun.agents.tool_call_agent import ToolCallAgent
from peldrun.engine.runner import AgentRunner, RunnerConfig
from peldrun.engine.state import ExecutionState, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.llm.client import LLMConfig
from peldrun.llm.providers.openai_compat import OpenAICompatProvider
from peldrun.runtime.contract import RunRequest, RunResult, RunStatus
from peldrun.runtime.diagnostics import DiagnosticsCollector
from peldrun.runtime.pricing_store import get_pricing_store
from peldrun.runtime.usage_store import LLMInvocationRecord, get_usage_store
from peldrun.tools.builtins.terminate import TerminateTool
from peldrun.tools.collection import ToolCollection
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.runtime.executor")

EventListener = Callable[[PeldrunEvent], Awaitable[None]]


class CoreRuntimeExecutor:
    """Public execution engine encapsulating Core agent lifecycle with telemetry instrumentation."""

    def __init__(self) -> None:
        self._active_runners: Dict[str, AgentRunner] = {}

    def cancel(self, job_id: str) -> bool:
        """Cancel an active agent execution by job ID."""
        runner = self._active_runners.get(job_id)
        if runner:
            runner.cancel()
            logger.info("Cancellation signal dispatched for active job: %s", job_id)
            return True
        return False

    async def execute(
        self,
        request: RunRequest,
        event_listener: Optional[EventListener] = None,
        custom_tools: Optional[List[Any]] = None,
    ) -> RunResult:
        """
        Execute an autonomous agent task adhering strictly to canonical RunRequest.
        Orchestrates LLM client, tool execution, telemetry tracking, and durable token persistence.
        """
        job_id = request.job_id
        run_id_str = str(request.run_id)
        start_time = time.time()
        ws_root = str(request.workspace.root_path)
        chat_id = request.workspace.chat_id
        project_id = request.workspace.project_id or "default_project"
        spec = request.agent_spec
        llm_cfg_dict = request.llm_config

        # 1. Initialize Diagnostics Collector
        diagnostics = DiagnosticsCollector(
            run_id=run_id_str,
            job_id=job_id,
            chat_id=chat_id,
        )

        # 2. Initialize EventEmitter and bind event listener & telemetry hooks
        emitter = EventEmitter(run_id=job_id)
        main_loop = asyncio.get_running_loop()

        active_tool_calls: Dict[str, str] = {}

        def _telemetry_event_interceptor(evt: PeldrunEvent) -> None:
            # Update step progression
            if hasattr(evt, "step") and isinstance(evt.step, int) and evt.step > 0:
                diagnostics.set_step(evt.step)

            # Track tool execution start
            if evt.type in (EventType.TOOL_CALL, getattr(EventType, "TOOL_CALLED", None)):
                t_name = str(evt.payload.get("tool_name") or evt.payload.get("tool") or "tool")
                call_key = diagnostics.start_tool(tool_name=t_name, step_num=evt.step)
                active_tool_calls[t_name] = call_key

            # Track tool execution completion
            elif evt.type in (EventType.OBSERVATION, getattr(EventType, "TOOL_COMPLETED", None)):
                t_name = str(evt.payload.get("tool_name") or evt.payload.get("tool") or "")
                call_key = active_tool_calls.pop(t_name, None)
                if not call_key and active_tool_calls:
                    call_key = active_tool_calls.pop(list(active_tool_calls.keys())[-1])

                is_err = bool(evt.payload.get("error"))
                err_msg = str(evt.payload.get("error")) if is_err else None
                if call_key:
                    diagnostics.complete_tool(call_key=call_key, success=not is_err, error_message=err_msg)

        emitter.subscribe_all(_telemetry_event_interceptor)

        if event_listener:
            def _sync_listener_bridge(evt: PeldrunEvent) -> None:
                if asyncio.iscoroutinefunction(event_listener):
                    asyncio.run_coroutine_threadsafe(event_listener(evt), main_loop)
                else:
                    event_listener(evt)  # type: ignore

            emitter.subscribe_all(_sync_listener_bridge)

        # 3. Build Tool Registry
        registry = ToolRegistry(workspace_root=ws_root)
        registry.register(TerminateTool(workspace_root=ws_root))

        if custom_tools:
            for tool in custom_tools:
                registry.register(tool)

        tool_collection: ToolCollection = getattr(
            registry,
            "_collection",
            ToolCollection(registry.list_tools()),
        )

        # 4. Configure LLM Provider & Instrument Invocations
        base_url = llm_cfg_dict.get("base_url") or "http://127.0.0.1:1234/v1"
        api_key = llm_cfg_dict.get("api_key") or "EMPTY"
        max_tokens = min(int(llm_cfg_dict.get("max_tokens") or 4096), 4096)
        step_timeout = float(request.step_timeout_seconds or 120.0)
        model_name = llm_cfg_dict.get("model", "default")
        provider_name = llm_cfg_dict.get("provider", "openai")

        llm_cfg = LLMConfig(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=float(spec.temperature),
            max_tokens=max_tokens,
            timeout=step_timeout,
        )
        llm_provider = OpenAICompatProvider(config=llm_cfg)

        # Wrap LLM generate to record telemetry and persist accounting fact atomically
        original_generate = llm_provider.generate
        usage_store = get_usage_store()
        pricing_store = get_pricing_store()

        async def _instrumented_generate(*args: Any, **kwargs: Any):
            inv_start = time.time()
            resp = await original_generate(*args, **kwargs)
            inv_end = time.time()

            u = resp.usage
            inv_id = f"inv_{uuid.uuid4().hex[:12]}"

            # Calculate monetary cost in integer Nano-USD
            cost_nano, rule_id = 0, None
            if u:
                cost_nano, rule_id = await pricing_store.calculate_cost(
                    usage=u,
                    provider=provider_name,
                    model=model_name,
                    base_url=base_url,
                )

            # Record in Diagnostics Collector
            diagnostics.record_llm_invocation(
                invocation_id=inv_id,
                provider=provider_name,
                model=model_name,
                started_at=inv_start,
                completed_at=inv_end,
                first_token_at=getattr(resp, "first_token_at", None),
                input_tokens=u.input_tokens if u else None,
                output_tokens=u.output_tokens if u else None,
                total_tokens=u.total_tokens if u else None,
                cached_tokens=u.cached_input_tokens if u else None,
                reasoning_tokens=u.reasoning_output_tokens if u else None,
                cost_nano_usd=cost_nano,
                finish_reason=resp.finish_reason,
            )

            # Persist immutable fact into SQLite token_ledger
            if u:
                record = LLMInvocationRecord(
                    invocation_id=inv_id,
                    run_id=run_id_str,
                    job_id=job_id,
                    project_id=project_id,
                    chat_id=chat_id,
                    step_id=str(diagnostics._current_step),
                    mode="agent",
                    provider=provider_name,
                    model_requested=model_name,
                    model_returned=resp.model or model_name,
                    status="completed",
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    total_tokens=u.total_tokens,
                    cached_input_tokens=u.cached_input_tokens,
                    reasoning_output_tokens=u.reasoning_output_tokens,
                    usage_source=u.source.value if hasattr(u.source, "value") else str(u.source),
                    estimated=u.estimated,
                    tokenizer_id=u.tokenizer_id,
                    tokenizer_version=u.tokenizer_version,
                    estimation_method=u.estimation_method,
                    started_at=inv_start,
                    first_token_at=getattr(resp, "first_token_at", None),
                    completed_at=inv_end,
                    latency_ms=round((inv_end - inv_start) * 1000.0, 2),
                    ttft_ms=round(resp.ttft_ms, 2) if resp.ttft_ms is not None else None,
                    finish_reason=resp.finish_reason,
                    pricing_version_id=rule_id,
                    cost_nano_usd=cost_nano,
                )
                await usage_store.record_invocation(record)

            return resp

        llm_provider.generate = _instrumented_generate

        # 5. Instantiate Autonomous Agent
        agent_config = AgentConfig(
            name=spec.name or spec.id,
            system_prompt=spec.system_prompt,
            max_steps=spec.max_steps,
        )
        if hasattr(agent_config, "workspace_root"):
            agent_config.workspace_root = ws_root

        agent = ToolCallAgent(
            config=agent_config,
            llm=llm_provider,
            tool_registry=registry,
            tool_collection=tool_collection,
            emitter=emitter,
            workspace_dir=ws_root,
        )
        agent.set_system_prompt(spec.system_prompt)
        agent.name = agent_config.name

        # 6. Initialize Execution State
        initial_state = ExecutionState(
            run_id=job_id,
            task_prompt=request.prompt,
            agent_name=agent.name,
            max_steps=spec.max_steps,
            workspace_root=ws_root,
            metadata={
                "run_id": run_id_str,
                "job_id": job_id,
                "request_metadata": request.metadata,
            },
        )
        initial_state.add_message(role=MessageRole.SYSTEM, content=spec.system_prompt)
        initial_state.add_message(role=MessageRole.USER, content=request.prompt)
        agent.messages = [msg.to_llm_dict() for msg in initial_state.messages]

        # 7. Configure Runner Lifecycle
        runner_config = RunnerConfig(
            max_steps=spec.max_steps,
            step_timeout_seconds=step_timeout,
            enable_checkpointing=True,
            checkpoint_interval=1,
        )
        runner = AgentRunner(
            agent=agent,
            emitter=emitter,
            config=runner_config,
        )
        self._active_runners[job_id] = runner

        # 8. Execute Autonomous Loop with Diagnostic Catching
        execution_state: Optional[ExecutionState] = None
        try:
            execution_state = await runner.run(
                task_prompt=request.prompt,
                state=initial_state,
                workspace_root=ws_root,
                agent=agent,
            )
        except asyncio.CancelledError:
            runner.cancel()
            logger.warning("Execution cancelled for job: %s", job_id)
            diag = diagnostics.finalize(status=RunStatus.CANCELLED, failure_reason="Cancelled by operator.")
            return RunResult(
                run_id=run_id_str,
                job_id=job_id,
                status=RunStatus.CANCELLED,
                output=None,
                deliverables=[],
                total_steps=diagnostics._current_step,
                error="Execution was cancelled by operator.",
                error_code="CANCELLED_BY_OPERATOR",
                metadata={"diagnostics": diag.model_dump(mode="json")},
                created_at=start_time,
                completed_at=time.time(),
            )
        except Exception as exc:
            logger.error("Core execution failure for job %s: %s", job_id, exc, exc_info=True)
            diag = diagnostics.finalize(status=RunStatus.FAILED, failure_reason=str(exc))
            return RunResult(
                run_id=run_id_str,
                job_id=job_id,
                status=RunStatus.FAILED,
                output=None,
                deliverables=[],
                total_steps=diagnostics._current_step,
                error=str(exc),
                error_code="EXECUTION_EXCEPTION",
                metadata={"diagnostics": diag.model_dump(mode="json")},
                created_at=start_time,
                completed_at=time.time(),
            )
        finally:
            self._active_runners.pop(job_id, None)

        completed_time = time.time()

        # 9. Assemble Terminal RunResult with Diagnostic Report
        if execution_state is None:
            diag = diagnostics.finalize(status=RunStatus.FAILED, failure_reason="No state produced.")
            return RunResult(
                run_id=run_id_str,
                job_id=job_id,
                status=RunStatus.FAILED,
                output=None,
                deliverables=[],
                total_steps=0,
                error="Runner exited without producing an execution state.",
                error_code="NO_STATE_PRODUCED",
                metadata={"diagnostics": diag.model_dump(mode="json")},
                created_at=start_time,
                completed_at=completed_time,
            )

        if execution_state.is_failed:
            err = execution_state.metadata.get("error") or "Execution failed during task execution."
            diag = diagnostics.finalize(status=RunStatus.FAILED, failure_reason=err)
            meta = dict(execution_state.metadata)
            meta["diagnostics"] = diag.model_dump(mode="json")
            return RunResult(
                run_id=run_id_str,
                job_id=job_id,
                status=RunStatus.FAILED,
                output=execution_state.final_output,
                deliverables=list(execution_state.deliverables),
                total_steps=execution_state.current_step,
                error=err,
                error_code="AGENT_EXECUTION_FAILED",
                metadata=meta,
                created_at=start_time,
                completed_at=completed_time,
            )

        if execution_state.is_cancelled:
            diag = diagnostics.finalize(status=RunStatus.CANCELLED, failure_reason="Cancelled by operator.")
            meta = dict(execution_state.metadata)
            meta["diagnostics"] = diag.model_dump(mode="json")
            return RunResult(
                run_id=run_id_str,
                job_id=job_id,
                status=RunStatus.CANCELLED,
                output=execution_state.final_output,
                deliverables=list(execution_state.deliverables),
                total_steps=execution_state.current_step,
                error="Execution was cancelled by operator.",
                error_code="CANCELLED_BY_OPERATOR",
                metadata=meta,
                created_at=start_time,
                completed_at=completed_time,
            )

        diag = diagnostics.finalize(status=RunStatus.COMPLETED)
        meta = dict(execution_state.metadata)
        meta["diagnostics"] = diag.model_dump(mode="json")

        return RunResult(
            run_id=run_id_str,
            job_id=job_id,
            status=RunStatus.COMPLETED,
            output=execution_state.final_output,
            deliverables=list(execution_state.deliverables),
            total_steps=execution_state.current_step,
            error=None,
            error_code=None,
            metadata=meta,
            created_at=start_time,
            completed_at=completed_time,
        )


core_executor = CoreRuntimeExecutor()

__all__ = ["CoreRuntimeExecutor", "core_executor"]