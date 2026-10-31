# backend/peldrun/runtime/executor.py
"""
PELDRUN Core Runtime Executor.

Authoritative execution gateway inside peldrun-core.
Encapsulates AgentRunner, ToolCallAgent, ExecutionState, ContextManager, and EventEmitter.
Fully instrumented with P1-05 Structured Diagnostics & Latency Telemetry.
Persists immutable invocation records into token_ledger via usage_store and pricing_store.
Adheres strictly to the Runtime V1 contract in peldrun.runtime.contract.
Faithfully extracts and propagates reasoning_effort control to the agent's LLM provider.

Hardened under Phase M3 (PR 7 Refinement):
- Scoped Budget Enforcement: enforces child delegation budget ceilings strictly on child subtasks.
  Allows root primary agent runs to operate smoothly across their configured max_steps.
- Complete Event Queue Draining: guarantees all bridged async events drain prior to terminal result emission.
- Cascading and Independent Cancellation: parent cancellation terminates active children; child cancellation is isolated.
- Complete Lineage Accounting: persists parent_run_id and root_run_id in ledger invocation records.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
import uuid

from peldrun.agents.base import AgentConfig
from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.tool_call_agent import ToolCallAgent
from peldrun.context.builder import ContextManager
from peldrun.context.sidecar import load_context_sidecar
from peldrun.engine.runner import AgentRunner, RunnerConfig
from peldrun.engine.state import ExecutionState, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.llm.client import LLMConfig
from peldrun.llm.providers.openai_compat import OpenAICompatProvider
from peldrun.runtime.contract import BudgetExceededError, RunRequest, RunResult, RunStatus
from peldrun.runtime.diagnostics import DiagnosticsCollector
from peldrun.runtime.pricing_store import get_pricing_store
from peldrun.runtime.usage_store import LLMInvocationRecord, get_usage_store
from peldrun.tools.builtins.delegate_tool import DelegateTool
from peldrun.tools.builtins.terminate import TerminateTool
from peldrun.tools.collection import ToolCollection
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.runtime.executor")

EventListener = Callable[[PeldrunEvent], Awaitable[None]]


class CoreRuntimeExecutor:
    """Public execution engine encapsulating Core agent lifecycle with telemetry instrumentation."""

    def __init__(self) -> None:
        self._active_runners: Dict[str, AgentRunner] = {}
        self._run_children: Dict[str, Set[str]] = {}
        self._run_parents: Dict[str, str] = {}
        self._job_to_run: Dict[str, str] = {}

    def cancel(self, run_or_job_id: str) -> bool:
        """Cancel an active execution supporting independent and cascading cancellation."""
        canonical_id = self._job_to_run.get(run_or_job_id, run_or_job_id)
        cancelled_any = False

        runner = self._active_runners.get(canonical_id) or self._active_runners.get(run_or_job_id)
        if runner:
            runner.cancel()
            logger.info("Cancellation signal dispatched for active execution handle: %s", canonical_id)
            cancelled_any = True

        children = list(self._run_children.get(canonical_id, set()))
        for child_id in children:
            child_runner = self._active_runners.get(child_id)
            if child_runner:
                child_runner.cancel()
                logger.info("Cascaded cancellation to child execution handle: %s", child_id)
                cancelled_any = True

        return cancelled_any

    async def execute(
        self,
        request: RunRequest,
        event_listener: Optional[EventListener] = None,
        custom_tools: Optional[List[Any]] = None,
    ) -> RunResult:
        """
        Execute an autonomous agent task adhering strictly to canonical RunRequest.
        Orchestrates ContextManager assembly, LLM client, tool execution, telemetry, and durable token persistence.
        """
        job_id = request.job_id
        run_id_str = str(request.run_id)
        parent_run_id = request.parent_run_id
        root_run_id = request.root_run_id or run_id_str
        delegation_depth = request.delegation_depth

        start_time = time.time()
        ws_root = str(request.workspace.root_path)
        chat_id = request.workspace.chat_id
        project_id = request.workspace.project_id or "default_project"
        spec = request.agent_spec
        llm_cfg_dict = request.llm_config

        self._job_to_run[job_id] = run_id_str
        if parent_run_id:
            self._run_children.setdefault(parent_run_id, set()).add(run_id_str)
            self._run_parents[run_id_str] = parent_run_id

        # 1. Initialize Diagnostics Collector
        diagnostics = DiagnosticsCollector(
            run_id=run_id_str,
            job_id=job_id,
            chat_id=chat_id,
        )

        # 2. Initialize EventEmitter and bind event listener with durable queue tracking
        emitter = EventEmitter(run_id=run_id_str)
        main_loop = asyncio.get_running_loop()

        active_tool_calls: Dict[str, str] = {}
        active_span_ids: Dict[str, str] = {}
        pending_listener_futures: Set[Any] = set()

        def _telemetry_event_interceptor(evt: PeldrunEvent) -> None:
            if hasattr(evt, "step") and isinstance(evt.step, int) and evt.step > 0:
                diagnostics.set_step(evt.step)

            # --- Resource Monitor Span Tracking (best-effort, decoupled) ---
            try:
                from omweb.services.resource_monitor_service import get_resource_monitor_service
                from omweb.resource_monitor.models import SpanType, SpanStatus

                monitor = get_resource_monitor_service()

                if evt.type in (EventType.TOOL_CALL, getattr(EventType, "TOOL_CALLED", None)):
                    t_name = str(evt.payload.get("tool_name") or evt.payload.get("tool") or "tool")
                    span_id = f"span_{uuid.uuid4().hex[:12]}"
                    monitor.start_span(
                        span_id=span_id,
                        run_id=job_id,
                        target_name=t_name,
                        span_type=SpanType.TOOL_CALL,
                    )
                    active_span_ids[t_name] = span_id

                elif evt.type in (EventType.OBSERVATION, getattr(EventType, "TOOL_COMPLETED", None)):
                    t_name = str(evt.payload.get("tool_name") or evt.payload.get("tool") or "")
                    span_id = active_span_ids.pop(t_name, None)
                    if not span_id and active_span_ids:
                        span_id = active_span_ids.pop(list(active_span_ids.keys())[-1])

                    if span_id:
                        is_err = bool(evt.payload.get("error"))
                        status = SpanStatus.FAILED if is_err else SpanStatus.COMPLETED
                        monitor.end_span(span_id=span_id, status=status)
            except Exception:
                # Never let monitoring break execution
                pass

            # --- Diagnostics Collector Tracking (existing) ---
            if evt.type in (EventType.TOOL_CALL, getattr(EventType, "TOOL_CALLED", None)):
                t_name = str(evt.payload.get("tool_name") or evt.payload.get("tool") or "tool")
                call_key = diagnostics.start_tool(tool_name=t_name, step_num=evt.step)
                active_tool_calls[t_name] = call_key

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
                    fut = asyncio.run_coroutine_threadsafe(event_listener(evt), main_loop)
                    pending_listener_futures.add(fut)
                    fut.add_done_callback(pending_listener_futures.discard)
                else:
                    event_listener(evt)

            emitter.subscribe_all(_sync_listener_bridge)

        # 3. Build Tool Registry and configure Native Delegation
        registry = ToolRegistry(workspace_root=ws_root)
        registry.register(TerminateTool(workspace_root=ws_root))

        agent_id_clean = (spec.id or "").lower().strip()
        is_lead_agent = agent_id_clean in ("peldrun", "lead", "team_lead")
        can_delegate = (
            request.delegation_allowed
            and request.delegation_depth == 0
            and is_lead_agent
        )

        if can_delegate:
            coordinator = MultiAgentCoordinator(
                parent_emitter=emitter,
                workspace_root=ws_root,
                step_timeout_seconds=float(request.step_timeout_seconds or 120.0),
                default_max_steps=int(spec.max_steps),
                execute_fn=self.execute,
                parent_request=request,
            )
            delegate_tool = DelegateTool(
                coordinator=coordinator,
                parent_run_id=run_id_str,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
                budget=request.budget,
                workspace_root=ws_root,
            )
            registry.register(delegate_tool)
            logger.info("Native DelegateTool wired for Team Lead run %s", run_id_str)
        else:
            logger.debug(
                "Delegation prohibited for run %s (agent=%s, allowed=%s, depth=%s)",
                run_id_str,
                agent_id_clean,
                request.delegation_allowed,
                delegation_depth,
            )

        if custom_tools:
            for tool in custom_tools:
                registry.register(tool)

        tool_collection: ToolCollection = getattr(
            registry,
            "_collection",
            ToolCollection(registry.list_tools()),
        )

        # 4. Configure LLM Provider with Scoped Budget Enforcement
        base_url = llm_cfg_dict.get("base_url") or "http://127.0.0.1:1234/v1"
        api_key = llm_cfg_dict.get("api_key") or "EMPTY"
        max_tokens = min(int(llm_cfg_dict.get("max_tokens") or 4096), 4096)
        step_timeout = float(request.step_timeout_seconds or 120.0)
        model_name = llm_cfg_dict.get("model", "default")
        provider_name = llm_cfg_dict.get("provider", "openai")

        # [FIX-LLM-REQUEST-TIMEOUT] Distinguish between the per-step
        # ceiling (``step_timeout``, enforced by AgentRunner) and the
        # per-request HTTP timeout (``llm_request_timeout``, enforced
        # by AsyncLLMClient here). Previously this function hardwired
        # ``LLMConfig.timeout = step_timeout``, which made the user's
        # dedicated ``llm_request_timeout_seconds`` setting from
        # /settings/runtime have zero effect in agent mode.
        #
        # Resolution:
        #   1. ``llm_cfg_dict["timeout"]`` — set by dispatcher from
        #      runtime_settings.json (or by an explicit per-request
        #      override, which wins at the dispatcher level).
        #   2. Fall back to ``step_timeout`` when no separate LLM
        #      timeout is configured — preserves the historical
        #      behavior for deployments that never set one.
        #
        # The outer step_timeout will still cap this value in practice
        # (a request that exceeds the step ceiling is cancelled by the
        # runner regardless of what the client's HTTP timeout says).
        # Users who want llm_request_timeout to actually bite must set
        # it <= step_timeout — otherwise the step ceiling fires first.
        _configured_llm_timeout = llm_cfg_dict.get("timeout")
        try:
            llm_request_timeout = float(
                _configured_llm_timeout
                if _configured_llm_timeout is not None
                else step_timeout
            )
        except (TypeError, ValueError):
            llm_request_timeout = step_timeout

        raw_effort = llm_cfg_dict.get("reasoning_effort")
        resolved_effort: Optional[str] = None
        if raw_effort:
            effort_str = str(raw_effort).lower().strip()
            if effort_str in ("none", "off", "0"):
                resolved_effort = "none"
            else:
                resolved_effort = effort_str

        llm_cfg = LLMConfig(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=float(spec.temperature),
            max_tokens=max_tokens,
            timeout=llm_request_timeout,
            reasoning_effort=resolved_effort,
        )
        llm_provider = OpenAICompatProvider(config=llm_cfg)

        original_generate = llm_provider.generate
        usage_store = get_usage_store()
        pricing_store = get_pricing_store()

        cumulative_tokens = 0
        cumulative_cost_nano = 0
        budget = request.budget

        # Scope budget enforcement: apply subtask token/cost limits strictly to delegated children.
        # Root primary agent runs execute across their configured max_steps without premature token choking,
        # unless an explicit token/cost limit is requested via metadata.
        is_delegated_subrun = bool(parent_run_id) or (delegation_depth > 0)
        token_ceiling: Optional[int] = None
        cost_ceiling: Optional[int] = None

        if is_delegated_subrun and budget:
            token_ceiling = budget.max_total_tokens
            cost_ceiling = budget.max_cost_nano_usd
        elif not is_delegated_subrun and isinstance(request.metadata, dict):
            token_ceiling = request.metadata.get("max_total_tokens") or request.metadata.get("root_token_budget")
            cost_ceiling = request.metadata.get("max_cost_nano_usd") or request.metadata.get("root_cost_budget")

        async def _instrumented_generate(*args: Any, **kwargs: Any):
            nonlocal cumulative_tokens, cumulative_cost_nano

            if token_ceiling is not None and cumulative_tokens >= token_ceiling:
                raise BudgetExceededError(
                    f"Cumulative token budget exhausted: {cumulative_tokens} >= {token_ceiling}",
                    current_usage=cumulative_tokens,
                    limit=token_ceiling,
                    metric="tokens",
                )

            if cost_ceiling is not None and cumulative_cost_nano >= cost_ceiling:
                raise BudgetExceededError(
                    f"Cumulative cost budget exhausted: {cumulative_cost_nano} >= {cost_ceiling}",
                    current_usage=cumulative_cost_nano,
                    limit=cost_ceiling,
                    metric="cost_nano",
                )

            inv_start = time.time()
            resp = await original_generate(*args, **kwargs)
            inv_end = time.time()

            u = resp.usage
            inv_id = f"inv_{uuid.uuid4().hex[:12]}"

            cost_nano, rule_id = 0, None
            if u:
                cost_nano, rule_id = await pricing_store.calculate_cost(
                    usage=u,
                    provider=provider_name,
                    model=model_name,
                    base_url=base_url,
                )
                if u.total_tokens:
                    cumulative_tokens += int(u.total_tokens)
                if cost_nano:
                    cumulative_cost_nano += int(cost_nano)

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
                    parent_run_id=parent_run_id,
                    root_run_id=root_run_id,
                    metadata={
                        "parent_run_id": parent_run_id,
                        "root_run_id": root_run_id,
                        "delegation_depth": delegation_depth,
                    },
                )
                await usage_store.record_invocation(record)

            if token_ceiling is not None and cumulative_tokens > token_ceiling:
                raise BudgetExceededError(
                    f"Cumulative token budget exceeded: {cumulative_tokens} > {token_ceiling}",
                    current_usage=cumulative_tokens,
                    limit=token_ceiling,
                    metric="tokens",
                )

            if cost_ceiling is not None and cumulative_cost_nano > cost_ceiling:
                raise BudgetExceededError(
                    f"Cumulative cost budget exceeded: {cumulative_cost_nano} > {cost_ceiling}",
                    current_usage=cumulative_cost_nano,
                    limit=cost_ceiling,
                    metric="cost_nano",
                )

            return resp

        llm_provider.generate = _instrumented_generate

        # 5. Instantiate Autonomous Agent with explicit LLM provider binding
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

        # 6. Assemble Context & Initialize Execution State via ContextManager
        sidecar = getattr(request, "context_sidecar", None)
        if sidecar is None and isinstance(request.metadata, dict):
            sidecar = request.metadata.get("context_sidecar") or request.metadata.get("sidecar")

        if sidecar is None and chat_id:
            try:
                session_dir_cand = (
                    request.metadata.get("session_dir")
                    if isinstance(request.metadata, dict)
                    else None
                )
                if session_dir_cand and Path(session_dir_cand).exists():
                    sidecar = load_context_sidecar(Path(session_dir_cand))
            except Exception as sidecar_err:
                logger.debug("Core executor sidecar auto-resolution notice: %s", sidecar_err)

        project_rules = getattr(request, "project_rules", None)
        if not project_rules and isinstance(request.metadata, dict):
            project_rules = request.metadata.get("project_rules")

        context_mgr = ContextManager()
        assembled_messages, budget_usage = context_mgr.build_messages(
            sidecar=sidecar,
            current_user_message=request.prompt,
            system_prompt=spec.system_prompt,
            project_rules=project_rules,
            include_execution=True,
        )

        logger.info(
            "Context assembled for agent job %s (run %s) | model: %s | active tokens: %s / %s",
            job_id,
            run_id_str,
            model_name,
            budget_usage.get("total_tokens", 0),
            budget_usage.get("effective_limit", 0),
        )

        # ─────────────────────────────────────────────────────────────
        # [FIX-MULTIMODAL] Inject multimodal attachment references into
        # the last user message before the agent sees the payload.
        #
        # The context manager builds messages from a plain-text prompt
        # only. Multimodal attachments (uploaded via /api/files/upload-temp)
        # are carried as lightweight ``fileId`` references and must be
        # resolved to inline ``image_url`` parts HERE, at the last
        # responsible boundary, so that every provider path (streaming,
        # non-streaming, tool loops, retries) sees the same multimodal
        # content without duplicating the resolution logic.
        #
        # Source precedence for the attachments list:
        #   1. request.llm_config["attachments"]  — set by dispatcher
        #      when the caller supplied attachments in llm_override.
        #      This is the historical path; retained for compatibility.
        #   2. request.metadata["attachments"]    — forwarded by
        #      peldrun_engine from context.attachments after File 26.
        #   3. Nothing.
        #
        # Filesystem resolution targets the canonical temp artifact
        # store under <storage_root>/artifacts/temp/<fileId>.
        # Rejections (bad id, missing file, unreadable bytes) are logged
        # and skipped — never fatal.
        # ─────────────────────────────────────────────────────────────
        _attachments_raw = None
        if isinstance(request.llm_config, dict):
            _attachments_raw = request.llm_config.get("attachments")
        if not _attachments_raw and isinstance(request.metadata, dict):
            _attachments_raw = request.metadata.get("attachments")

        if _attachments_raw and isinstance(assembled_messages, list) and assembled_messages:
            try:
                import base64 as _b64
                from peldrun.storage_paths import get_storage_root as _get_root

                _MIME_BY_EXT = {
                    "png": "image/png",
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "webp": "image/webp",
                    "gif": "image/gif",
                }
                _temp_dir = _get_root() / "artifacts" / "temp"

                # [FIX-WORKSPACE-FIRST] Resolve attachments from the
                # workspace copy first, temp as fallback.
                #
                # Why this ordering matters:
                #   * ``start_run.py`` copies every attachment to
                #     ``<chat_files>/.peldrun/user_uploads/<fileId>`` and
                #     (after the companion change in ``start_run.py``)
                #     deletes the temp original as soon as the copy
                #     succeeds. Reading temp first would therefore fail
                #     for any attachment that has already been persisted
                #     once — including regeneration runs, follow-up turns
                #     that reuse the same fileId, and any page reload
                #     past 24h when the periodic sweeper has run.
                #   * Reading workspace first is also faster (no need to
                #     fall back), and more resilient (temp may be
                #     cleaned up mid-run by the background cleaner).
                #
                # Both directories are content-scoped, and fileIds are
                # opaque UUIDs, so the same fileId in a different chat's
                # workspace is either the same image (safe) or a hash
                # collision (astronomically improbable).
                _workspace_upload_dir: Optional[Path] = None
                try:
                    # ws_root already points at <chat_files>.
                    _workspace_upload_dir = (
                        Path(ws_root) / ".peldrun" / "user_uploads"
                    )
                except Exception:
                    _workspace_upload_dir = None

                _image_parts: List[Dict[str, Any]] = []
                for _att in _attachments_raw:
                    if not isinstance(_att, dict):
                        continue
                    _fid = _att.get("fileId") or _att.get("file_id")
                    if not isinstance(_fid, str) or not _fid:
                        continue
                    # Reject traversal attempts. fileId is an opaque token
                    # like ``img_<hex>.<ext>``; anything else is a caller
                    # bug or attack.
                    if "/" in _fid or "\\" in _fid or ".." in _fid:
                        logger.warning(
                            "Skipping attachment with unsafe fileId %r", _fid
                        )
                        continue

                    # Resolve: workspace first, temp fallback.
                    _path: Optional[Path] = None
                    if _workspace_upload_dir is not None:
                        _ws_candidate = _workspace_upload_dir / _fid
                        if _ws_candidate.is_file():
                            _path = _ws_candidate
                    if _path is None:
                        _temp_candidate = _temp_dir / _fid
                        if _temp_candidate.is_file():
                            _path = _temp_candidate

                    if _path is None:
                        logger.warning(
                            "Attachment fileId %r not found in workspace "
                            "or temp store",
                            _fid,
                        )
                        continue
                    try:
                        _raw = _path.read_bytes()
                    except Exception as _read_exc:
                        logger.warning(
                            "Failed to read attachment %r: %s", _fid, _read_exc
                        )
                        continue
                    _ext = _path.suffix.lower().lstrip(".")
                    _mime = _MIME_BY_EXT.get(_ext, "image/png")
                    _encoded = _b64.b64encode(_raw).decode("ascii")
                    _image_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{_mime};base64,{_encoded}",
                            "detail": "auto",
                        },
                    })

                if _image_parts:
                    # The last user message is the current turn's prompt
                    # (assembled_messages ends with the current user turn).
                    # Convert its content from a plain string into a
                    # multimodal parts list.
                    for _i in range(len(assembled_messages) - 1, -1, -1):
                        _msg = assembled_messages[_i]
                        if (
                            isinstance(_msg, dict)
                            and _msg.get("role") == "user"
                            and isinstance(_msg.get("content"), str)
                        ):
                            _text = _msg["content"]
                            _msg["content"] = (
                                [{"type": "text", "text": _text}]
                                + _image_parts
                            )
                            logger.info(
                                "Injected %d multimodal image(s) into "
                                "context for job %s (fileIds: %s)",
                                len(_image_parts),
                                job_id,
                                ", ".join(
                                    str(_att.get("fileId"))
                                    for _att in _attachments_raw
                                    if isinstance(_att, dict)
                                ),
                            )
                            break
                else:
                    logger.info(
                        "Multimodal attachments present for job %s but "
                        "none resolved to readable files. Continuing with "
                        "text-only context.",
                        job_id,
                    )
            except Exception as _mm_exc:
                # Multimodal injection is best-effort: if it fails for any
                # reason, fall back to the text-only context so the run
                # still proceeds. The user sees no image analysis but the
                # agent works normally.
                logger.warning(
                    "Multimodal attachment injection failed for job %s: %s",
                    job_id,
                    _mm_exc,
                )

        initial_state = ExecutionState(
            run_id=run_id_str,
            task_prompt=request.prompt,
            agent_name=agent.name,
            max_steps=spec.max_steps,
            workspace_root=ws_root,
            metadata={
                "run_id": run_id_str,
                "job_id": job_id,
                "parent_run_id": parent_run_id,
                "root_run_id": root_run_id,
                "delegation_depth": delegation_depth,
                "request_metadata": request.metadata,
                "reasoning_effort": resolved_effort,
                "context_budget": budget_usage,
            },
        )

        for msg in assembled_messages:
            role_str = str(msg.get("role", "user")).lower()
            if role_str == "system":
                role_enum = MessageRole.SYSTEM
            elif role_str == "assistant":
                role_enum = MessageRole.ASSISTANT
            else:
                role_enum = MessageRole.USER

            _raw_content = msg.get("content", "")

            # [FIX-MULTIMODAL-STATE] Preserve the full multimodal payload
            # in ``ExecutionState.messages`` too — NOT just in
            # ``agent.messages``. The AgentRunner uses
            # ``initial_state.messages`` as the source of truth for each
            # LLM call: it rehydrates ``agent.messages`` from
            # ``state.messages`` at the start of every step. If we
            # flatten here, the model loses the ``image_url`` parts
            # entirely and reports that it cannot see any image, while
            # chat mode (which bypasses ExecutionState) works correctly.
            #
            # Trade-off:
            #   * Checkpoints for image turns become larger (base64 image
            #     data is serialized into the checkpoint JSON). This is
            #     bounded — checkpoints are overwritten each step, and
            #     turns with images are typically single-LLM-call runs.
            #   * If ``add_message`` type-checks content as ``str`` and
            #     rejects the multimodal list, we fall back to the
            #     flattened summary so the run still proceeds. A warning
            #     is logged so the failure is diagnosable.
            if isinstance(_raw_content, list):
                try:
                    initial_state.add_message(
                        role=role_enum, content=_raw_content
                    )
                    logger.info(
                        "[FIX-MULTIMODAL-STATE] Preserved %d multimodal "
                        "part(s) in ExecutionState for role=%s (job=%s)",
                        len(_raw_content),
                        role_enum.value,
                        job_id,
                    )
                    continue
                except (TypeError, ValueError, AttributeError) as state_add_err:
                    logger.warning(
                        "[FIX-MULTIMODAL-STATE] ExecutionState rejected "
                        "multimodal content for job %s: %s — falling "
                        "back to flattened summary (model will NOT "
                        "receive image parts this turn).",
                        job_id,
                        state_add_err,
                    )

                # Fallback: flatten with a clear marker.
                _text_fragments = [
                    str(_p.get("text", ""))
                    for _p in _raw_content
                    if isinstance(_p, dict)
                    and _p.get("type") == "text"
                    and _p.get("text")
                ]
                _img_count = sum(
                    1
                    for _p in _raw_content
                    if isinstance(_p, dict) and _p.get("type") == "image_url"
                )
                _flattened = " ".join(_text_fragments)
                if _img_count > 0:
                    _flattened += f" [{_img_count} image(s) attached]"
                initial_state.add_message(role=role_enum, content=_flattened)
            else:
                initial_state.add_message(role=role_enum, content=str(_raw_content))

        agent.messages = [dict(m) for m in assembled_messages]

        # 7. Configure Runner Lifecycle with explicit total_timeout_seconds propagation
        runner_config = RunnerConfig(
            max_steps=spec.max_steps,
            step_timeout_seconds=step_timeout,
            total_timeout_seconds=request.total_timeout_seconds,
            enable_checkpointing=True,
            checkpoint_interval=1,
        )
        runner = AgentRunner(
            agent=agent,
            emitter=emitter,
            config=runner_config,
        )

        self._active_runners[run_id_str] = runner
        if job_id != run_id_str:
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
            logger.warning("Execution cancelled for run %s (job %s)", run_id_str, job_id)
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
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
            )
        except BudgetExceededError as b_err:
            logger.warning("Execution halted due to budget overrun for run %s: %s", run_id_str, b_err)
            diag = diagnostics.finalize(status=RunStatus.FAILED, failure_reason=str(b_err))
            code = "TOKEN_BUDGET_EXCEEDED" if b_err.metric == "tokens" else "COST_BUDGET_EXCEEDED"
            return RunResult(
                run_id=run_id_str,
                job_id=job_id,
                status=RunStatus.FAILED,
                output=None,
                deliverables=[],
                total_steps=diagnostics._current_step,
                error=str(b_err),
                error_code=code,
                metadata={"diagnostics": diag.model_dump(mode="json")},
                created_at=start_time,
                completed_at=time.time(),
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
            )
        except Exception as exc:
            logger.error("Core execution failure for run %s (job %s): %s", run_id_str, job_id, exc, exc_info=True)
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
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
            )
        finally:
            self._active_runners.pop(run_id_str, None)
            self._active_runners.pop(job_id, None)
            self._job_to_run.pop(job_id, None)
            if parent_run_id and parent_run_id in self._run_children:
                self._run_children[parent_run_id].discard(run_id_str)
            self._run_parents.pop(run_id_str, None)

            if pending_listener_futures:
                try:
                    await asyncio.wait(
                        [asyncio.wrap_future(f) for f in list(pending_listener_futures)],
                        timeout=2.0,
                    )
                except Exception as drain_err:
                    logger.debug("Event drain cleanup notice for run %s: %s", run_id_str, drain_err)

        completed_time = time.time()

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
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
            )

        if execution_state.is_failed:
            err = execution_state.metadata.get("error") or "Execution failed during task execution."
            err_code = "AGENT_EXECUTION_FAILED"
            if "token budget" in err.lower():
                err_code = "TOKEN_BUDGET_EXCEEDED"
            elif "cost budget" in err.lower():
                err_code = "COST_BUDGET_EXCEEDED"

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
                error_code=err_code,
                metadata=meta,
                created_at=start_time,
                completed_at=completed_time,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
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
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                delegation_depth=delegation_depth,
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
            parent_run_id=parent_run_id,
            root_run_id=root_run_id,
            delegation_depth=delegation_depth,
        )


core_executor = CoreRuntimeExecutor()

__all__ = ["CoreRuntimeExecutor", "core_executor"]
