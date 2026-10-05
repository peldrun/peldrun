"""
PELDRUN Core native engine runner.

Executes an autonomous agent workflow via the ``peldrun-core`` package
with real-time SSE event streaming to the Web UI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event
from omweb.job_manager import job_manager

from .state import job_scoped_artifacts
from .text_utils import sanitize_final_result_text


async def _run_peldrun_core_agent(
    job_id: str,
    prompt: str,
    agent_id: str,
    active_llm: Dict[str, Any],
    model_name: str,
    provider_name: str,
    project_dir: Path,
    chat_id: str,
    manifest: Dict[str, Any],
) -> None:
    """Execute the agent workflow using the PELDRUN Core engine.

    Wires real tools, subscribes to the core EventEmitter, streams
    STEP_START / THOUGHT / TOOL_CALL / OBSERVATION / ERROR / FINAL
    events to the web UI as SSE, and completes the job with the result.

    Args:
        job_id:        Unique job identifier.
        prompt:        User task prompt.
        agent_id:      Agent registry identifier.
        active_llm:    Resolved LLM config (model, base_url, api_key, ...).
        model_name:    Display name of the active model.
        provider_name: Display name of the active provider.
        project_dir:   Workspace directory for the current chat.
        chat_id:       Chat session identifier.
        manifest:      Agent manifest (system prompt, tools, max_steps, ...).
    """
    print(f"\n[BRIDGE CORE] >>> Starting Live Run for Job: {job_id} using PELDRUN Core Engine <<<")

    # Local imports so that the core engine is only required when in use.
    from peldrun.events.emitter import EventEmitter
    from peldrun.events.schema import EventType, PeldrunEvent
    from peldrun.tools.collection import ToolCollection
    from peldrun.tools.builtins.terminate import TerminateTool
    from peldrun.llm.client import LLMConfig
    from peldrun.llm.providers.openai_compat import OpenAICompatProvider
    from peldrun.agents.tool_call_agent import ToolCallAgent
    from peldrun.agents.base import AgentConfig

    from omweb.adapters.core_adapter import resolve_tool_runtime
    from omweb.tools.registry import tool_registry as web_tool_registry

    files_baseline: Set[str] = {p.name for p in project_dir.iterdir() if p.is_file()} if project_dir.exists() else set()
    latest_meaningful_thought: str = ""

    max_steps = int(manifest.get("max_steps") or 30)
    system_prompt = manifest.get("system_prompt", "You are peldrun, an all-around autonomous specialist agent.")
    ws_path = project_dir.resolve()
    ws_path_str = str(ws_path)

    # 1. Wire Real Executable Tool Collection
    collection = ToolCollection()
    collection.add_tool(TerminateTool(workspace_root=ws_path_str))

    available_web_tools = {t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)}

    # Always ensure essential execution tools are present
    requested_tools = list(manifest.get("tools", []))
    for essential in ("str_replace_editor", "bash", "python_execute", "web_search", "browser_use", "ask_human"):
        if essential not in requested_tools and essential in available_web_tools:
            requested_tools.append(essential)

    for req_tool in requested_tools:
        if req_tool in ("terminate",):
            continue

        if req_tool in available_web_tools:
            t_meta = available_web_tools[req_tool]
            real_adapter = resolve_tool_runtime(
                tool_id=req_tool,
                tool_meta=t_meta,
                workspace_root=ws_path,
                registry=web_tool_registry
            )
            collection.add_tool(real_adapter)

    # 2. Setup Real-Time Event Dispatcher to Web UI safely wrapping async loops
    emitter = EventEmitter()
    current_core_step = 1
    is_first_step_start = True
    main_loop = asyncio.get_running_loop()

    def _extract_event_step(evt: PeldrunEvent) -> Optional[int]:
        """Extract explicit step integer from event object or event payload."""
        if hasattr(evt, "step") and isinstance(evt.step, int) and evt.step > 0:
            return evt.step
        if hasattr(evt, "payload") and isinstance(evt.payload, dict):
            p_step = evt.payload.get("step") or evt.payload.get("step_num") or evt.payload.get("current_step")
            if isinstance(p_step, int) and p_step > 0:
                return p_step
            if isinstance(p_step, str) and p_step.isdigit() and int(p_step) > 0:
                return int(p_step)
        return None

    async def _async_on_core_event(event: PeldrunEvent) -> None:
        """Async handler that maps every core engine event to SSE events."""
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
                    data={"step": curr_step, "model": model_name, "engine": "peldrun-core", "status": "running"}
                )
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
                            "model": model_name
                        }
                    )
                )

        elif event.type == getattr(EventType, "TOOL_CALL", None) or event.type == getattr(EventType, "TOOL_CALLED", None):
            tool_name = str(event.payload.get("tool_name") or event.payload.get("tool") or "tool")
            arguments = event.payload.get("arguments", {})
            raw_args = json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else str(arguments)

            # Special SSE payload decoration for interactive human inquiry
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
                tool_call_payload["prompt"] = arguments.get("prompt") or arguments.get("query") or ""
                tool_call_payload["input_type"] = arguments.get("input_type", "text")
                tool_call_payload["options"] = arguments.get("options", [])

            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.TOOL_CALL,
                    step=curr_step,
                    data=tool_call_payload
                )
            )

        elif event.type == EventType.OBSERVATION or event.type == getattr(EventType, "TOOL_COMPLETED", None):
            obs_out = event.payload.get("output", "") or event.payload.get("error", "")
            obs_str = str(obs_out)

            # Real-time artifact detection on disk per step
            if project_dir.exists():
                current_files = {p.name for p in project_dir.iterdir() if p.is_file()}
                new_files = current_files - files_baseline
                for nf in new_files:
                    if nf not in job_scoped_artifacts[job_id]:
                        job_scoped_artifacts[job_id].append(nf)
                        if chat_id and nf not in job_scoped_artifacts.get(chat_id, []):
                            job_scoped_artifacts.setdefault(chat_id, []).append(nf)
                        print(f"[BRIDGE CORE] Detected Artifact Created on Disk in Step {curr_step}: {nf}")
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
                                    "model": model_name
                                }
                            )
                        )

            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.OBSERVATION,
                    step=curr_step,
                    data={
                        "output": obs_str,
                        "content": obs_str,
                        "model": model_name
                    }
                )
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
                        "model": model_name
                    }
                )
            )

    def _sync_event_handler(event: PeldrunEvent) -> None:
        """Synchronous wrapper ensuring async events execute correctly on main loop."""
        try:
            main_loop.create_task(_async_on_core_event(event))
        except Exception as e:
            print(f"[BRIDGE ERROR] Failed to dispatch core event async: {e}")

    emitter.subscribe_all(_sync_event_handler)

    # 3. Universal LLM Client & Provider Configuration
    base_url = active_llm.get("base_url") or "http://127.0.0.1:1234/v1"
    api_key = active_llm.get("api_key") or "EMPTY"
    safe_max_tokens = min(int(active_llm.get("max_tokens") or 4096), 4096)

    llm_cfg = LLMConfig(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        temperature=float(active_llm.get("temperature", 0.2)),
        max_tokens=safe_max_tokens,
    )

    llm_provider = OpenAICompatProvider(config=llm_cfg)

    # 4. Instantiate ToolCallAgent
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
        tool_collection=collection,
        emitter=emitter,
        workspace_dir=ws_path_str,
    )
    agent.set_system_prompt(system_prompt)

    scoped_prompt = (
        f"[PROJECT WORKSPACE RULES]\n"
        f"1. Working Directory: Your active directory is: {project_dir.resolve()}\n"
        f"2. File Deliverables: Use 'python_execute' or 'str_replace_editor' to create, edit, and verify files on disk.\n"
        f"3. Tool Calling Conventions: Always supply required parameters (e.g. 'query' for web_search, 'url' for browser_use).\n"
        f"4. Search & Fallback Strategy: First use 'web_search'. If search engines return no snippets, use 'browser_use' to visit authoritative websites directly (e.g. TechCrunch, Reuters, BBC).\n"
        f"5. Mandatory Human Consultation (ask_human): If you cannot find live articles or need guidance, DO NOT generate outdated answers from memory. You MUST immediately invoke 'ask_human' with a clear prompt, appropriate 'input_type' ('text' | 'confirm' | 'select'), and actionable 'options' (e.g. ['Provide Custom URL', 'Try Different Topic', 'Answer From Knowledge']). The task will pause and wait for the human operator.\n"
        f"6. Citations & Attribution: When reporting news or technical data, ALWAYS cite direct sources using Markdown links [Title](URL).\n"
        f"7. Task Completion: Do NOT call 'terminate' until the deliverables are created and verified or the user inquiry is fully satisfied.\n\n"
        f"[USER TASK]\n"
        f"{prompt}"
    )

    final_answer = await agent.run_task(prompt=scoped_prompt, max_steps=max_steps)

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

    print(f"[BRIDGE CORE] Job {job_id} completed successfully. Produced files: {new_turn_files}")
    job_manager.complete_job(job_id, result_text)
    await dispatch_event(
        job_id,
        SSEEvent(
            type=SSEEventType.FINAL,
            step=current_core_step,
            data={
                "result": result_text,
                "model": model_name,
                "engine": "peldrun-core",
                "produced_files": new_turn_files,
            }
        )
    )