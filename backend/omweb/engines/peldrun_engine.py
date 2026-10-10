"""
backend/omweb/engines/peldrun_engine.py

PELDRUN Native Embedded Execution Engine (Hardened Context Architecture).
Executes autonomous workflows by delegating to peldrun-core via RunRequest.
Integrates ContextManager to enforce strict isolation between persistent history
and LLM context, guaranteeing zero execution telemetry leakage across turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set
import uuid

from omweb.agent_bridge_parts.state import job_scoped_artifacts
from omweb.agent_bridge_parts.text_utils import sanitize_final_result_text
from omweb.chat_storage_engine import chat_storage_engine
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager
from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event

from .base import EngineRunContext, ExecutionEngine

# Core Public Boundary Imports
from peldrun.context.builder import ContextManager
from peldrun.context.sidecar import build_context_sidecar, load_context_sidecar
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.runtime.contract import AgentSpec, RunRequest, RunStatus, WorkspaceContext
from peldrun.runtime.executor import core_executor
from peldrun.runtime.pricing_store import NANO_USD_PER_USD, get_pricing_store
from peldrun.runtime.usage_store import get_usage_store
from peldrun.tools.builtins.human_input import HumanInputRegistry

from omweb.adapters.core_adapter import resolve_tool_runtime
from omweb.tools.registry import tool_registry as web_tool_registry

logger = logging.getLogger(__name__)


class PeldrunEngine(ExecutionEngine):
    """Isolated Web execution adapter powered by local PELDRUN Core."""

    engine_id: str = "peldrun"

    async def health(self) -> Dict[str, Any]:
        """Perform health and version diagnostics for the embedded PELDRUN runtime."""
        try:
            import peldrun

            return {
                "engine_id": self.engine_id,
                "available": True,
                "embedded": getattr(peldrun, "__is_embedded__", False),
                "version": getattr(peldrun, "__version__", "unknown"),
                "upstream_commit": getattr(peldrun, "__upstream_commit__", "unknown"),
                "package_path": str(Path(peldrun.__file__).resolve().parent),
            }
        except ImportError as err:
            return {
                "engine_id": self.engine_id,
                "available": False,
                "embedded": False,
                "error": f"Failed to import embedded peldrun package: {err}",
            }

    async def run(self, context: EngineRunContext) -> None:
        """Execute autonomous agent workflow through canonical Core boundaries."""
        job_id = context.job_id
        prompt = context.prompt
        agent_id = context.agent_id
        active_llm = context.active_llm
        model_name = context.model_name
        provider_name = context.provider_name
        project_dir = context.project_dir
        chat_id = context.chat_id or f"chat_{job_id}"
        manifest = context.manifest

        print(f"\n[ENGINE PELDRUN] >>> Starting Task Execution for Job: {job_id} (Chat: {chat_id}) <<<")

        job_scoped_artifacts.setdefault(job_id, [])
        if chat_id:
            job_scoped_artifacts.setdefault(chat_id, [])

        ws_path = project_dir.resolve()
        ws_path_str = str(ws_path)

        def _scan_workspace_files() -> Dict[str, Path]:
            scanned: Dict[str, Path] = {}
            if not project_dir.exists():
                return scanned
            for file_path in project_dir.rglob("*"):
                if file_path.is_file():
                    rel_parts = file_path.relative_to(project_dir).parts
                    if any(part.startswith(".") for part in rel_parts):
                        continue
                    canonical_rel = str(file_path.relative_to(project_dir)).replace("\\", "/")
                    scanned[canonical_rel] = file_path
            return scanned

        files_baseline: Dict[str, Path] = _scan_workspace_files()
        known_artifact_paths: Set[str] = set(files_baseline.keys())
        latest_meaningful_thought: str = ""

        max_steps = int(manifest.get("max_steps") or 30)
        base_system_prompt = manifest.get(
            "system_prompt",
            "You are peldrun, an all-around autonomous specialist agent.",
        )

        system_prompt = (
            f"{base_system_prompt}\n\n"
            f"[CRITICAL OPERATIONAL DIRECTIVE]\n"
            f"1. Autonomous Loop Contract: You run inside an automated agent loop. If you need to ask the user questions, gather answers, or wait for input, you MUST invoke the 'ask_human' tool for each individual interaction. DO NOT print questions as plain text, as plain text without tool calls terminates execution.\n"
            f"2. Task Scope Sovereignty: Satisfy the user's overarching objective faithfully. If the user asks for a conversation, questionnaire, or reasoning task, do NOT invent an artificial need to create workspace files or write code unless explicitly commanded.\n"
            f"3. Strict Counting & Non-Premature Termination: When asked to perform a specific number of interactions, you must execute all of them completely and wait for each answer before concluding. Never call 'terminate' until all requested questions have been answered."
        )

        project_rules = (
            "1. User Directive Sovereignty: Follow USER TASK instructions strictly.\n"
            "2. Multi-Question Counting Protocol: Execute all sequential interactions completely before concluding.\n"
            "3. Workspace Operations: Current working directory is already workspace root. Use relative paths.\n"
            "4. Task Conclusion: Ensure all parts of the user request are satisfied completely before calling 'terminate'."
        )

        # 1. Resolve & Refresh Context Sidecar from disk
        session_dir = project_manager.chats_dir / chat_id
        sidecar = chat_storage_engine.get_sidecar_object(chat_id)
        if not sidecar:
            chat_data = project_manager.get_chat(chat_id) or project_manager.get_chat(job_id) or {}
            sidecar = build_context_sidecar(
                session_id=chat_id,
                session_data=chat_data,
                model_id=model_name,
                storage_root=project_manager.storage_dir,
            )

        # 2. Assemble Sanitized Prompt Context via ContextManager
        context_mgr = ContextManager(default_system_prompt=system_prompt)
        context_messages, context_usage = context_mgr.build_messages(
            sidecar=sidecar,
            current_user_message=prompt,
            system_prompt=system_prompt,
            project_rules=project_rules,
            include_execution=False,  # Clean initial state for new task turn
        )

        logger.info(
            "[CONTEXT HARDENING] Assembled %d context messages for job %s (Tokens: %s)",
            len(context_messages),
            job_id,
            context_usage.get("total", 0),
        )

        # 3. Resolve custom Web tools to adapt into Core tools
        requested_tools = list(manifest.get("tools", []))
        available_web_tools = {
            t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)
        }

        adapted_core_tools: List[Any] = []
        for req_tool in requested_tools:
            if req_tool in ("terminate", "file_saver"):
                continue
            if req_tool in available_web_tools:
                t_meta = available_web_tools[req_tool]
                adapter = resolve_tool_runtime(
                    tool_id=req_tool,
                    tool_meta=t_meta,
                    workspace_root=ws_path,
                    registry=web_tool_registry,
                )
                adapted_core_tools.append(adapter)

        # 4. Build Canonical RunRequest with contextual message payload
        workspace_ctx = WorkspaceContext(
            workspace_id=chat_id,
            root_path=ws_path_str,
            chat_id=chat_id,
            project_id=manifest.get("project_id", "default_project"),
            read_only=False,
        )
        agent_spec = AgentSpec(
            id=agent_id,
            name=manifest.get("name") or agent_id,
            system_prompt=system_prompt,
            tools=requested_tools,
            max_steps=max_steps,
            temperature=float(active_llm.get("temperature", 0.2)),
        )
        run_request = RunRequest(
            run_id=str(uuid.uuid4()),
            job_id=job_id,
            prompt=prompt,
            agent_spec=agent_spec,
            workspace=workspace_ctx,
            llm_config=active_llm,
            metadata={
                "model_name": model_name,
                "chat_id": chat_id,
                "provider": provider_name,
                "session_dir": str(session_dir),
                "project_rules": project_rules,
                "sidecar": sidecar,
                "context_messages": context_messages,
                "context_usage": context_usage,
            },
            context_sidecar=sidecar,
            project_rules=project_rules,
            step_timeout_seconds=float(active_llm.get("timeout") or 120.0),
        )

        # 5. Setup Async Event Processing Bridge
        event_queue: asyncio.Queue[Optional[PeldrunEvent]] = asyncio.Queue()
        event_seq: int = 0
        current_core_step: int = 1
        is_first_step_start: bool = True
        terminal_dispatched: bool = False

        def _extract_event_step(evt: PeldrunEvent) -> Optional[int]:
            if hasattr(evt, "step") and isinstance(evt.step, int) and evt.step > 0:
                return evt.step
            if hasattr(evt, "payload") and isinstance(evt.payload, dict):
                p_step = (
                    evt.payload.get("step")
                    or evt.payload.get("step_num")
                    or evt.payload.get("current_step")
                )
                if isinstance(p_step, int) and p_step > 0:
                    return p_step
                if isinstance(p_step, str) and p_step.isdigit() and int(p_step) > 0:
                    return int(p_step)
            return None

        async def _sync_and_dispatch_artifacts(step_num: int) -> List[str]:
            nonlocal event_seq
            current_files = _scan_workspace_files()
            discovered_paths: List[str] = []

            for rel_path, file_path in current_files.items():
                if rel_path not in known_artifact_paths:
                    known_artifact_paths.add(rel_path)
                    discovered_paths.append(rel_path)
                    file_name = file_path.name
                    file_size = file_path.stat().st_size

                    if rel_path not in job_scoped_artifacts[job_id]:
                        job_scoped_artifacts[job_id].append(rel_path)

                    if chat_id and rel_path not in job_scoped_artifacts[chat_id]:
                        job_scoped_artifacts[chat_id].append(rel_path)

                    event_seq += 1
                    print(f"[ENGINE PELDRUN] Detected Artifact Created on Disk: {rel_path} ({file_size} bytes)")

                    await dispatch_event(
                        job_id,
                        SSEEvent(
                            type=SSEEventType.ARTIFACT_CREATED,
                            step=step_num,
                            data={
                                "artifact": rel_path,
                                "path": rel_path,
                                "relative_path": rel_path,
                                "name": file_name,
                                "size_bytes": file_size,
                                "chat_id": chat_id,
                                "job_id": job_id,
                                "seq": event_seq,
                                "model": model_name,
                            },
                        ),
                    )

            return discovered_paths

        async def _process_single_core_event(event: PeldrunEvent) -> None:
            nonlocal latest_meaningful_thought, current_core_step, is_first_step_start, event_seq

            event_seq += 1
            explicit_step = _extract_event_step(event)
            if explicit_step is not None:
                current_core_step = explicit_step
                is_first_step_start = False
            elif event.type == EventType.STEP_START:
                if is_first_step_start:
                    current_core_step = 1
                    is_first_step_start = False
                else:
                    current_core_step += 1

            curr_step = current_core_step

            if event.type == EventType.STEP_START:
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.STEP_START,
                        step=curr_step,
                        data={
                            "step": curr_step,
                            "seq": event_seq,
                            "model": model_name,
                            "engine": self.engine_id,
                            "status": "running",
                        },
                    ),
                )

            elif event.type == EventType.THOUGHT or getattr(event.type, "name", "") == "STEP_PROGRESS":
                thought_text = str(event.payload.get("thought", "")).strip()
                if thought_text:
                    latest_meaningful_thought = thought_text
                    await dispatch_event(
                        job_id,
                        SSEEvent(
                            type=SSEEventType.THOUGHT,
                            step=curr_step,
                            data={
                                "thought": thought_text,
                                "content": thought_text,
                                "seq": event_seq,
                                "model": model_name,
                            },
                        ),
                    )

            elif (
                event.type == getattr(EventType, "TOOL_CALL", None)
                or event.type == getattr(EventType, "TOOL_CALLED", None)
            ):
                tool_name = str(event.payload.get("tool_name") or event.payload.get("tool") or "tool")
                arguments = event.payload.get("arguments", {})
                raw_args = (
                    json.dumps(arguments, ensure_ascii=False)
                    if isinstance(arguments, dict)
                    else str(arguments)
                )

                is_human_ask = tool_name in ("ask_human", "human_input")
                tool_call_payload: Dict[str, Any] = {
                    "name": tool_name,
                    "toolName": tool_name,
                    "arguments": raw_args,
                    "content": raw_args,
                    "seq": event_seq,
                    "model": model_name,
                    "requires_input": is_human_ask,
                }
                if is_human_ask and isinstance(arguments, dict):
                    tool_call_payload["prompt"] = (
                        arguments.get("prompt")
                        or arguments.get("query")
                        or arguments.get("question")
                        or ""
                    )
                    tool_call_payload["input_type"] = arguments.get("input_type", "text")
                    tool_call_payload["options"] = arguments.get("options", [])
                    req_id = arguments.get("request_id")
                    if not req_id and hasattr(HumanInputRegistry, "_pending_requests") and HumanInputRegistry._pending_requests:
                        req_id = list(HumanInputRegistry._pending_requests.keys())[-1]
                    if req_id:
                        tool_call_payload["request_id"] = req_id

                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.TOOL_CALL,
                        step=curr_step,
                        data=tool_call_payload,
                    ),
                )

            elif event.type == EventType.ASK_HUMAN:
                payload = event.payload if isinstance(event.payload, dict) else {}
                prompt_text = payload.get("prompt") or payload.get("question") or ""
                req_id = payload.get("request_id") or ""
                tool_call_payload = {
                    "name": "ask_human",
                    "toolName": "ask_human",
                    "prompt": prompt_text,
                    "question": prompt_text,
                    "input_type": payload.get("input_type", "text"),
                    "options": payload.get("options", []),
                    "request_id": req_id,
                    "requires_input": True,
                    "seq": event_seq,
                    "model": model_name,
                }
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.TOOL_CALL,
                        step=curr_step,
                        data=tool_call_payload,
                    ),
                )

            elif (
                event.type == EventType.OBSERVATION
                or event.type == getattr(EventType, "TOOL_COMPLETED", None)
            ):
                obs_out = event.payload.get("output", "") or event.payload.get("error", "")
                obs_str = str(obs_out)

                await _sync_and_dispatch_artifacts(curr_step)

                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.OBSERVATION,
                        step=curr_step,
                        data={
                            "output": obs_str,
                            "content": obs_str,
                            "seq": event_seq,
                            "model": model_name,
                        },
                    ),
                )

            elif event.type == EventType.ERROR:
                err_msg = str(event.payload.get("error", "Runtime Error"))
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=curr_step,
                        data={
                            "message": err_msg,
                            "content": err_msg,
                            "seq": event_seq,
                            "model": model_name,
                        },
                    ),
                )

        async def _ordered_event_consumer() -> None:
            while True:
                event = await event_queue.get()
                if event is None:
                    event_queue.task_done()
                    break
                try:
                    await _process_single_core_event(event)
                except Exception as ex:
                    logger.error(f"[ENGINE PELDRUN ERROR] Consumer failed for job {job_id}: {ex}", exc_info=True)
                finally:
                    event_queue.task_done()

        consumer_task = asyncio.create_task(_ordered_event_consumer())

        async def _on_core_event(evt: PeldrunEvent) -> None:
            await event_queue.put(evt)

        # 6. Delegate Execution to Core Authority
        try:
            run_result = await core_executor.execute(
                request=run_request,
                event_listener=_on_core_event,
                custom_tools=adapted_core_tools,
            )

            await _sync_and_dispatch_artifacts(run_result.total_steps or current_core_step)
            await event_queue.put(None)
            await consumer_task

        except asyncio.CancelledError:
            core_executor.cancel(job_id)
            try:
                await event_queue.put(None)
                await asyncio.wait_for(consumer_task, timeout=1.0)
            except Exception:
                consumer_task.cancel()
            print(f"[ENGINE PELDRUN] Task cancelled by operator for job: {job_id}")
            raise

        except Exception as exc:
            try:
                await event_queue.put(None)
                await asyncio.wait_for(consumer_task, timeout=1.0)
            except Exception:
                consumer_task.cancel()

            err_msg = f"Task execution interrupted: {exc}"
            print(f"[ENGINE PELDRUN ERROR] {err_msg}")
            job_manager.fail_job(job_id, err_msg)
            if not terminal_dispatched:
                terminal_dispatched = True
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=current_core_step,
                        data={
                            "message": err_msg,
                            "model": model_name,
                            "seq": event_seq + 1,
                        },
                    ),
                )
            return

        # 7. Extract Telemetry & Usage Metrics
        diag_data = run_result.metadata.get("diagnostics", {})
        inp_tok = int(diag_data.get("total_input_tokens") or 0)
        out_tok = int(diag_data.get("total_output_tokens") or 0)
        tot_tok = int(diag_data.get("total_tokens") or (inp_tok + out_tok))
        cost_nano = int(diag_data.get("total_cost_nano_usd") or 0)
        cost_usd = round(cost_nano / NANO_USD_PER_USD, 6)

        turn_id = f"turn_{int(time.time())}"
        turn_usage_dict = {
            "turn_id": turn_id,
            "prompt_tokens": inp_tok,
            "completion_tokens": out_tok,
            "total_tokens": tot_tok,
            "cached_input_tokens": diag_data.get("total_cached_tokens"),
            "reasoning_output_tokens": diag_data.get("total_reasoning_tokens"),
            "cost_usd": cost_usd,
            "cost_nano_usd": cost_nano,
            "latency_ms": diag_data.get("total_duration_ms"),
            "tokens_per_second": diag_data.get("overall_tokens_per_second"),
            "model": model_name,
            "provider": provider_name,
        }

        session_usage_summary = project_manager.record_turn_usage(
            chat_id=chat_id,
            turn_id=turn_id,
            turn_usage=turn_usage_dict,
            project_id=workspace_ctx.project_id or "default_project",
        )

        all_created = sorted([p for p in known_artifact_paths if p not in files_baseline])
        canonical_deliverables = all_created
        step_count = run_result.total_steps if run_result.total_steps > 0 else current_core_step

        if run_result.status == RunStatus.FAILED:
            err_msg = run_result.error or "Execution failed before reaching completion."
            print(f"[ENGINE PELDRUN] Job {job_id} terminated as FAILED in step {step_count}: {err_msg}")
            job_manager.fail_job(job_id, err_msg)
            if not terminal_dispatched:
                terminal_dispatched = True
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=step_count,
                        data={
                            "message": err_msg,
                            "model": model_name,
                            "engine": self.engine_id,
                            "produced_files": canonical_deliverables,
                            "usage": turn_usage_dict,
                            "usage_summary": session_usage_summary,
                            "seq": event_seq + 1,
                        },
                    ),
                )
            return

        if run_result.status == RunStatus.CANCELLED:
            cancel_msg = run_result.error or "Execution was cancelled by operator."
            print(f"[ENGINE PELDRUN] Job {job_id} terminated as CANCELLED in step {step_count}")
            job_manager.fail_job(job_id, cancel_msg)
            if not terminal_dispatched:
                terminal_dispatched = True
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=step_count,
                        data={
                            "message": cancel_msg,
                            "model": model_name,
                            "engine": self.engine_id,
                            "status": "cancelled",
                            "seq": event_seq + 1,
                        },
                    ),
                )
            return

        if canonical_deliverables:
            file_bullets = "\n".join([f"- `{f}`" for f in canonical_deliverables])
            result_text = (
                f"### Deliverables Created Successfully\n\n"
                f"The requested project files have been built and saved in your workspace:\n\n"
                f"{file_bullets}\n\n"
                f"You can preview and interact with the application live in the **Preview** panel."
            )
        else:
            result_text = sanitize_final_result_text(run_result.output, latest_meaningful_thought)

        print(
            f"[ENGINE PELDRUN] Job {job_id} completed successfully in step {step_count}. "
            f"Tokens: {tot_tok} | Cost: ${cost_usd}"
        )

        job_manager.complete_job(job_id, result_text)

        # 8. Atomically Synchronize Context Sidecar after task conclusion
        try:
            chat_storage_engine.sync_sidecar(chat_id, model_id=model_name)
        except Exception as sidecar_err:
            logger.debug(f"[ENGINE PELDRUN] Sidecar synchronization notice: {sidecar_err}")

        if not terminal_dispatched:
            terminal_dispatched = True
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.FINAL,
                    step=step_count,
                    data={
                        "result": result_text,
                        "model": model_name,
                        "engine": self.engine_id,
                        "produced_files": canonical_deliverables,
                        "usage": turn_usage_dict,
                        "usage_summary": session_usage_summary,
                        "cost_usd": cost_usd,
                        "seq": event_seq + 1,
                    },
                ),
            )
