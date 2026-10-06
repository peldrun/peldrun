"""
PELDRUN Native Embedded Execution Engine (Consolidated Architecture).

Executes agent workflows natively through the consolidated Core lifecycle:
EngineRunContext -> RunRequest -> AgentRunner -> ExecutionState -> StepExecutableAgent.
Pre-seeds conversation turns with SYSTEM and USER messages to prevent empty-context LLM rejections.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event

from .base import EngineRunContext, ExecutionEngine


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
        from peldrun.tools.builtins.terminate import TerminateTool
        from peldrun.tools.collection import ToolCollection
        from peldrun.tools.registry import ToolRegistry as CoreToolRegistry

        from omweb.adapters.core_adapter import resolve_tool_runtime
        from omweb.tools.registry import tool_registry as web_tool_registry

        files_baseline: Set[str] = (
            {p.name for p in project_dir.iterdir() if p.is_file()}
            if project_dir.exists()
            else set()
        )
        latest_meaningful_thought: str = ""

        max_steps = int(manifest.get("max_steps") or 30)
        system_prompt = manifest.get(
            "system_prompt",
            "You are peldrun, an all-around autonomous specialist agent.",
        )
        ws_path = project_dir.resolve()
        ws_path_str = str(ws_path)

        # 1. Establish Core Runtime Invocation Contracts
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

        # 2. Wire Session-Scoped Core ToolRegistry
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

        # 3. Wire Real-Time Event Dispatcher
        emitter = EventEmitter()
        current_core_step = 1
        is_first_step_start = True
        main_loop = asyncio.get_running_loop()

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

        async def _async_on_core_event(event: PeldrunEvent) -> None:
            nonlocal latest_meaningful_thought, current_core_step, is_first_step_start

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
                            data={"thought": thought_text, "content": thought_text, "model": model_name},
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
                tool_call_payload = {
                    "name": tool_name,
                    "toolName": tool_name,
                    "arguments": raw_args,
                    "content": raw_args,
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

                if project_dir.exists():
                    current_files = {p.name for p in project_dir.iterdir() if p.is_file()}
                    new_files = current_files - files_baseline
                    for nf in new_files:
                        if nf not in job_scoped_artifacts[job_id]:
                            job_scoped_artifacts[job_id].append(nf)
                            if chat_id and nf not in job_scoped_artifacts.get(chat_id, []):
                                job_scoped_artifacts.setdefault(chat_id, []).append(nf)
                            await dispatch_event(
                                job_id,
                                SSEEvent(
                                    type=SSEEventType.OBSERVATION,
                                    step=curr_step,
                                    data={
                                        "artifact": nf,
                                        "path": nf,
                                        "chat_id": chat_id,
                                        "event": "artifact_created",
                                        "content": f"Artifact created: {nf}",
                                        "model": model_name,
                                    },
                                ),
                            )

                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.OBSERVATION,
                        step=curr_step,
                        data={"output": obs_str, "content": obs_str, "model": model_name},
                    ),
                )

            elif event.type == EventType.ERROR:
                err_msg = str(event.payload.get("error", "Runtime Error"))
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.ERROR,
                        step=curr_step,
                        data={"message": err_msg, "content": err_msg, "model": model_name},
                    ),
                )

        def _sync_event_handler(event: PeldrunEvent) -> None:
            try:
                main_loop.create_task(_async_on_core_event(event))
            except Exception as e:
                print(f"[ENGINE PELDRUN ERROR] Failed to dispatch core event: {e}")

        emitter.subscribe_all(_sync_event_handler)

        # 4. LLM Configuration
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

        # 5. Agent Instantiation
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
            f"[PROJECT WORKSPACE RULES]\n"
            f"1. Working Directory: Your active directory is: {project_dir.resolve()}\n"
            f"2. File Deliverables: Use available editor tools to create and update files.\n"
            f"3. Execution & Verification: Use available tools adhering strictly to assigned capabilities.\n"
            f"4. Tool Calling Conventions: Always supply required parameters.\n"
            f"5. Task Completion: Call 'terminate' when objectives are accomplished.\n\n"
            f"[USER TASK]\n"
            f"{prompt}"
        )

        # 6. Pre-seed ExecutionState with initial SYSTEM and USER messages
        initial_state = ExecutionState(
            task_prompt=scoped_prompt,
            agent_name=agent.name,
            max_steps=max_steps,
            workspace_root=ws_path_str,
            metadata={"run_request": run_request.model_dump(mode="json")},
        )
        initial_state.add_message(role=MessageRole.SYSTEM, content=system_prompt)
        initial_state.add_message(role=MessageRole.USER, content=scoped_prompt)

        # Initialize agent's in-memory messages directly from initial state
        agent.messages = [msg.to_llm_dict() for msg in initial_state.messages]

        # 7. Consolidated Execution via Core AgentRunner & ExecutionState
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
        except asyncio.CancelledError:
            runner.cancel()
            print(f"[ENGINE PELDRUN] Task cancelled by operator for job: {job_id}")
            raise
        except Exception as exc:
            err_msg = f"Task execution interrupted: {exc}"
            print(f"[ENGINE PELDRUN ERROR] {err_msg}")
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.ERROR,
                    step=current_core_step,
                    data={"message": err_msg, "model": model_name},
                ),
            )
            final_answer = sanitize_final_result_text("", latest_meaningful_thought) or err_msg

        # 8. Finalize Deliverables and Complete Job
        if project_dir.exists():
            current_files = {p.name for p in project_dir.iterdir() if p.is_file()}
            for f_name in (current_files - files_baseline):
                if f_name not in job_scoped_artifacts[job_id]:
                    job_scoped_artifacts[job_id].append(f_name)
                if chat_id and f_name not in job_scoped_artifacts.get(chat_id, []):
                    job_scoped_artifacts.setdefault(chat_id, []).append(f_name)

        if execution_state is not None:
            for deliv in execution_state.deliverables:
                deliv_name = Path(deliv).name
                if deliv_name not in job_scoped_artifacts[job_id]:
                    job_scoped_artifacts[job_id].append(deliv_name)

        new_turn_files = job_scoped_artifacts.get(job_id, [])

        if new_turn_files:
            file_bullets = "\n".join([f"- `{f}`" for f in new_turn_files])
            result_text = (
                f"### Deliverables Created Successfully\n\n"
                f"The requested project files have been built and saved in your workspace:\n\n"
                f"{file_bullets}\n\n"
                f"You can preview and interact with the application live in the **Preview** panel."
            )
        else:
            result_text = sanitize_final_result_text(final_answer, latest_meaningful_thought)

        step_count = execution_state.current_step if execution_state else current_core_step
        print(f"[ENGINE PELDRUN] Job {job_id} completed successfully in step {step_count}. Produced files: {new_turn_files}")
        job_manager.complete_job(job_id, result_text)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.FINAL,
                step=step_count,
                data={
                    "result": result_text,
                    "model": model_name,
                    "engine": self.engine_id,
                    "produced_files": new_turn_files,
                },
            ),
        )