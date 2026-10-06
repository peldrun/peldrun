"""
backend/omweb/engines/peldrun_engine.py

PELDRUN Native Embedded Execution Engine (Consolidated Architecture).

Executes agent workflows natively through the consolidated Core lifecycle:
EngineRunContext -> RunRequest -> AgentRunner -> ExecutionState -> StepExecutableAgent.

Hardened under Phase M2 & Stabilization:
- Decouples workspace execution: Deliverables are created ONLY when explicitly requested by user directive.
- Relative CWD Sovereignty: Working directory '.' is already workspace root; absolute Windows paths are forbidden.
- Strict Question Counting Protocol: Prevents off-by-one step index confusion (e.g. asking all 5 questions).
- Human interaction via ask_human is strictly enforced without jumping prematurely to task completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event

from .base import EngineRunContext, ExecutionEngine

logger = logging.getLogger(__name__)


class PeldrunEngine(ExecutionEngine):
    """First-class embedded execution engine powered by local PELDRUN Core."""

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
        """Execute autonomous agent workflow through the consolidated Core lifecycle."""
        job_id = context.job_id
        prompt = context.prompt
        agent_id = context.agent_id
        active_llm = context.active_llm
        model_name = context.model_name
        project_dir = context.project_dir
        chat_id = context.chat_id
        manifest = context.manifest

        print(f"\n[ENGINE PELDRUN] >>> Starting Consolidated Task Execution for Job: {job_id} <<<")

        from omweb.agent_bridge_parts.state import job_scoped_artifacts
        from omweb.agent_bridge_parts.text_utils import sanitize_final_result_text
        from omweb.job_manager import job_manager

        from peldrun.agents.base import AgentConfig
        from peldrun.agents.tool_call_agent import ToolCallAgent
        from peldrun.engine.runner import AgentRunner, RunnerConfig
        from peldrun.engine.state import ExecutionState, MessageRole
        from peldrun.events.emitter import EventEmitter
        from peldrun.events.schema import EventType, PeldrunEvent
        from peldrun.llm.client import LLMConfig
        from peldrun.llm.providers.openai_compat import OpenAICompatProvider
        from peldrun.runtime.contract import AgentSpec, RunRequest, WorkspaceContext
        from peldrun.tools.builtins.human_input import HumanInputRegistry
        from peldrun.tools.builtins.terminate import TerminateTool
        from peldrun.tools.collection import ToolCollection
        from peldrun.tools.registry import ToolRegistry as CoreToolRegistry

        from omweb.adapters.core_adapter import resolve_tool_runtime
        from omweb.tools.registry import tool_registry as web_tool_registry

        job_scoped_artifacts.setdefault(job_id, [])
        if chat_id:
            job_scoped_artifacts.setdefault(chat_id, [])

        ws_path = project_dir.resolve()
        ws_path_str = str(ws_path)

        def _scan_workspace_files() -> Dict[str, Path]:
            """Recursively scan workspace directory for deliverable files."""
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
            f"3. Strict Counting & Non-Premature Termination: When asked to perform a specific number of interactions (e.g. ask 5 questions sequentially), you must execute all of them completely (Question 1, Question 2, Question 3, Question 4, Question 5) and wait for each answer before concluding. Do not confuse step numbers with question count. Never call 'terminate' until all requested questions have been answered."
        )

        workspace_ctx = WorkspaceContext(
            workspace_id=chat_id or job_id,
            root_path=ws_path_str,
            chat_id=chat_id,
            project_id=manifest.get("project_id"),
        )
        requested_tools = list(manifest.get("tools", []))
        agent_spec = AgentSpec(
            id=agent_id,
            name=manifest.get("name") or agent_id,
            system_prompt=system_prompt,
            tools=requested_tools,
            max_steps=max_steps,
            temperature=float(active_llm.get("temperature", 0.2)),
        )
        run_request = RunRequest(
            job_id=job_id,
            prompt=prompt,
            agent_spec=agent_spec,
            workspace=workspace_ctx,
            llm_config=active_llm,
        )

        core_registry = CoreToolRegistry(workspace_root=ws_path_str)
        core_registry.register(TerminateTool(workspace_root=ws_path_str))

        available_web_tools = {
            t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)
        }

        for req_tool in requested_tools:
            if req_tool in ("terminate", "file_saver"):
                continue

            if req_tool in available_web_tools:
                t_meta = available_web_tools[req_tool]
                real_adapter = resolve_tool_runtime(
                    tool_id=req_tool,
                    tool_meta=t_meta,
                    workspace_root=ws_path,
                    registry=web_tool_registry,
                )
                core_registry.register(real_adapter)

        fallback_collection: ToolCollection = getattr(
            core_registry,
            "_collection",
            ToolCollection(core_registry.list_tools()),
        )

        emitter = EventEmitter(run_id=job_id)
        main_loop = asyncio.get_running_loop()

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

                    await dispatch_event(
                        job_id,
                        SSEEvent(
                            type=SSEEventType.OBSERVATION,
                            step=step_num,
                            data={
                                "artifact": rel_path,
                                "path": rel_path,
                                "chat_id": chat_id,
                                "event": "artifact_created",
                                "content": f"Artifact created: {rel_path}",
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
                    logger.error(
                        f"[ENGINE PELDRUN ERROR] Exception in ordered event consumer for job {job_id}: {ex}",
                        exc_info=True,
                    )
                finally:
                    event_queue.task_done()

        consumer_task = asyncio.create_task(_ordered_event_consumer())

        def _sync_event_handler(event: PeldrunEvent) -> None:
            try:
                event_queue.put_nowait(event)
            except Exception:
                main_loop.call_soon_threadsafe(event_queue.put_nowait, event)

        emitter.subscribe_all(_sync_event_handler)

        base_url = active_llm.get("base_url") or "http://127.0.0.1:1234/v1"
        api_key = active_llm.get("api_key") or "EMPTY"
        safe_max_tokens = min(int(active_llm.get("max_tokens") or 4096), 4096)
        llm_timeout = float(active_llm.get("timeout") or 300.0)

        llm_cfg = LLMConfig(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=float(active_llm.get("temperature", 0.2)),
            max_tokens=safe_max_tokens,
            timeout=llm_timeout,
        )
        llm_provider = OpenAICompatProvider(config=llm_cfg)

        agent_config = AgentConfig(
            name=manifest.get("name") or agent_id,
            system_prompt=system_prompt,
            max_steps=max_steps,
        )
        if hasattr(agent_config, "workspace_root"):
            agent_config.workspace_root = ws_path_str

        agent = ToolCallAgent(
            config=agent_config,
            llm=llm_provider,
            tool_registry=core_registry,
            tool_collection=fallback_collection,
            emitter=emitter,
            workspace_dir=ws_path_str,
        )
        agent.set_system_prompt(system_prompt)
        agent.name = agent_config.name

        scoped_prompt = (
            f"[PROJECT EXECUTION RULES]\n"
            f"1. User Directive Sovereignty: Follow the USER TASK instructions strictly. If the user task asks you to ask questions, interview, or reason, DO NOT create files, folders, or code deliverables unless explicitly requested.\n"
            f"2. Multi-Question Counting Protocol: When the user asks for N questions (e.g. 5 questions sequentially one at a time):\n"
            f"   - Question 1: call ask_human -> receive Answer 1\n"
            f"   - Question 2: call ask_human -> receive Answer 2\n"
            f"   - Question 3: call ask_human -> receive Answer 3\n"
            f"   - Question 4: call ask_human -> receive Answer 4\n"
            f"   - Question 5: call ask_human -> receive Answer 5\n"
            f"   - AFTER all 5 answers are collected: summarize the answers and call 'terminate'.\n"
            f"   * CRITICAL: Do NOT stop after Question 4! Step index is NOT questions answered. You must ask all 5 distinct questions.\n"
            f"3. Workspace Operations (When Relevant): If and ONLY IF the user explicitly requests building files, projects, or apps:\n"
            f"   - Your current working directory ('.') is ALREADY the isolated workspace root for this task.\n"
            f"   - ALWAYS use relative paths (e.g., './mini_app/index.html' or 'mini_app/index.html').\n"
            f"   - NEVER write absolute Windows paths (e.g., 'D:\\...' or 'C:\\...') in shell commands because backslashes will be stripped by bash and corrupt file names.\n"
            f"   - Web deliverables (HTML/CSS/JS) are rendered live in the UI Preview panel. NEVER execute blocking foreground servers like 'python -m http.server'.\n"
            f"4. Task Conclusion: Ensure all parts of the user request are satisfied completely before calling 'terminate'.\n\n"
            f"5. Anti-Premature Termination: If your thoughts state that you need to create or write another file (e.g. app.js), "
            f"YOU MUST execute the tool call to create that file first. NEVER invoke 'terminate' while uncompleted deliverables remain."
            f"[USER TASK]\n"
            f"{prompt}"
        )

        initial_state = ExecutionState(
            run_id=job_id,
            task_prompt=scoped_prompt,
            agent_name=agent.name,
            max_steps=max_steps,
            workspace_root=ws_path_str,
            metadata={"run_request": run_request.model_dump(mode="json")},
        )
        initial_state.add_message(role=MessageRole.SYSTEM, content=system_prompt)
        initial_state.add_message(role=MessageRole.USER, content=scoped_prompt)

        agent.messages = [msg.to_llm_dict() for msg in initial_state.messages]

        runner_config = RunnerConfig(
            max_steps=max_steps,
            step_timeout_seconds=llm_timeout,
            enable_checkpointing=True,
            checkpoint_interval=1,
        )
        runner = AgentRunner(
            agent=agent,
            emitter=emitter,
            config=runner_config,
        )

        execution_state: Optional[ExecutionState] = None
        try:
            execution_state = await runner.run(
                task_prompt=scoped_prompt,
                state=initial_state,
                workspace_root=ws_path_str,
                agent=agent,
            )
            final_answer = execution_state.final_output or ""

            await _sync_and_dispatch_artifacts(execution_state.current_step)

            await event_queue.put(None)
            await consumer_task

        except asyncio.CancelledError:
            runner.cancel()
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

        all_created = sorted([p for p in known_artifact_paths if p not in files_baseline])
        canonical_deliverables = all_created

        if execution_state is not None:
            for deliv in canonical_deliverables:
                execution_state.add_deliverable(deliv)

        if canonical_deliverables:
            file_bullets = "\n".join([f"- `{f}`" for f in canonical_deliverables])
            result_text = (
                f"### Deliverables Created Successfully\n\n"
                f"The requested project files have been built and saved in your workspace:\n\n"
                f"{file_bullets}\n\n"
                f"You can preview and interact with the application live in the **Preview** panel."
            )
        else:
            result_text = sanitize_final_result_text(final_answer, latest_meaningful_thought)

        step_count = execution_state.current_step if execution_state else current_core_step
        print(f"[ENGINE PELDRUN] Job {job_id} completed successfully in step {step_count}. Produced files: {canonical_deliverables}")

        job_manager.complete_job(job_id, result_text)
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
                        "seq": event_seq + 1,
                    },
                ),
            )