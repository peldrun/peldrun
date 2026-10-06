"""
PELDRUN Native Embedded Execution Engine.

Executes agent workflows using the embedded peldrun runtime package
with streaming SSE event dispatching, platform tool resolution, and deliverable tracking.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from omweb.agent_bridge_parts.state import job_scoped_artifacts
from omweb.agent_bridge_parts.text_utils import sanitize_final_result_text
from omweb.job_manager import job_manager
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
        """Execute autonomous agent workflow using the native embedded Core runtime."""
        job_id = context.job_id
        prompt = context.prompt
        agent_id = context.agent_id
        active_llm = context.active_llm
        model_name = context.model_name
        project_dir = context.project_dir
        chat_id = context.chat_id
        manifest = context.manifest

        print(f"\n[ENGINE PELDRUN] >>> Starting Task Execution for Job: {job_id} <<<")

        # Native embedded package imports
        from peldrun.agents.base import AgentConfig
        from peldrun.agents.tool_call_agent import ToolCallAgent
        from peldrun.events.emitter import EventEmitter
        from peldrun.events.schema import EventType, PeldrunEvent
        from peldrun.llm.client import LLMConfig
        from peldrun.llm.providers.openai_compat import OpenAICompatProvider
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
            "You are peldrun, an all-around autonomous specialist agent."
        )
        ws_path = project_dir.resolve()
        ws_path_str = str(ws_path)

        # 1. Wire Session-Scoped Core ToolRegistry
        core_registry = CoreToolRegistry(workspace_root=ws_path_str)
        core_registry.register(TerminateTool(workspace_root=ws_path_str))

        available_web_tools = {
            t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)
        }

        requested_tools = list(manifest.get("tools", []))
        essential_tool_ids = (
            "str_replace_editor",
            "bash",
            "python_execute",
            "web_search",
            "browser_use",
            "ask_human",
        )
        for essential in essential_tool_ids:
            if essential not in requested_tools and essential in available_web_tools:
                requested_tools.append(essential)

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
            ToolCollection(core_registry.list_tools())
        )

        # 2. Wire Real-Time Event Dispatcher
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

        # 3. LLM Configuration
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

        # 4. Agent Instantiation
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

        scoped_prompt = (
            f"[PROJECT WORKSPACE RULES]\n"
            f"1. Working Directory: Your active directory is: {project_dir.resolve()}\n"
            f"2. File Deliverables: ALWAYS use 'str_replace_editor' with command='create' to write and save project files directly to disk.\n"
            f"3. Execution & Verification: Use 'python_execute' only when you need to run calculations, test execution, or process data.\n"
            f"4. Tool Calling Conventions: Always supply required parameters.\n"
            f"5. Search & Web Fallback: Use 'web_search' for search queries and 'browser_use' to visit authoritative websites.\n"
            f"6. Mandatory Human Consultation (ask_human): When you need operator input or answers, invoke 'ask_human'.\n"
            f"7. Task Completion: Call 'terminate' when project files are created and verified.\n\n"
            f"[USER TASK]\n"
            f"{prompt}"
        )

        try:
            final_answer = await agent.run_task(prompt=scoped_prompt, max_steps=max_steps)
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

        # 5. Finalize Deliverables and Complete Job
        if project_dir.exists():
            current_files = {p.name for p in project_dir.iterdir() if p.is_file()}
            for f_name in (current_files - files_baseline):
                if f_name not in job_scoped_artifacts[job_id]:
                    job_scoped_artifacts[job_id].append(f_name)
                if chat_id and f_name not in job_scoped_artifacts.get(chat_id, []):
                    job_scoped_artifacts.setdefault(chat_id, []).append(f_name)

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

        print(f"[ENGINE PELDRUN] Job {job_id} completed successfully. Produced files: {new_turn_files}")
        job_manager.complete_job(job_id, result_text)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.FINAL,
                step=current_core_step,
                data={
                    "result": result_text,
                    "model": model_name,
                    "engine": self.engine_id,
                    "produced_files": new_turn_files,
                },
            ),
        )