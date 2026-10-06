"""
backend/omweb/agent_bridge_parts/core_engine.py

PELDRUN Core native execution runtime adapter.
Hardened under Phases M0, M1, and M2:
- Strict FIFO event ordering with sequence numbering.
- Push streaming of Canonical Artifact events (created/updated).
- Scope-isolated memory management and terminal contract enforcement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event
from omweb.job_manager import job_manager

from .state import cleanup_job_state, job_scoped_artifacts
from .text_utils import sanitize_final_result_text

logger = logging.getLogger(__name__)


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
    """Execute an autonomous agent workflow using the PELDRUN Core engine."""
    print(f"\n[BRIDGE CORE] >>> Starting Live Run for Job: {job_id} using PELDRUN Core Engine <<<")

    from peldrun.events.emitter import EventEmitter
    from peldrun.events.schema import EventType, PeldrunEvent
    from peldrun.tools.registry import ToolRegistry as CoreToolRegistry
    from peldrun.tools.collection import ToolCollection
    from peldrun.tools.builtins.terminate import TerminateTool
    from peldrun.llm.client import LLMConfig
    from peldrun.llm.providers.openai_compat import OpenAICompatProvider
    from peldrun.agents.tool_call_agent import ToolCallAgent
    from peldrun.agents.base import AgentConfig
    from peldrun.artifacts.manager import ArtifactManager

    from omweb.adapters.core_adapter import resolve_tool_runtime
    from omweb.tools.registry import tool_registry as web_tool_registry

    # 1. Initialize Job Workspace and Artifact Infrastructure
    ws_path = project_dir.resolve()
    ws_path_str = str(ws_path)
    job_scoped_artifacts.setdefault(job_id, [])
    if chat_id:
        job_scoped_artifacts.setdefault(chat_id, [])

    artifact_mgr = ArtifactManager(ws_path_str)
    artifact_mgr.snapshot_baseline()

    max_steps = int(manifest.get("max_steps") or 30)
    system_prompt = manifest.get(
        "system_prompt",
        "You are peldrun, an all-around autonomous specialist agent."
    )

    # 2. Wire Session-Scoped Core ToolRegistry (Store Sovereignty)
    core_registry = CoreToolRegistry(workspace_root=ws_path_str)
    core_registry.register(TerminateTool(workspace_root=ws_path_str))

    available_web_tools = {
        t["id"]: t
        for t in web_tool_registry.list_tools()
        if t.get("is_enabled", True)
    }

    requested_tools = list(manifest.get("tools", []))
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

    # 3. Setup Ordered FIFO Event Bridge
    emitter = EventEmitter()
    main_loop = asyncio.get_running_loop()

    event_queue: asyncio.Queue[Optional[PeldrunEvent]] = asyncio.Queue()
    event_seq: int = 0
    current_core_step: int = 1
    is_first_step_start: bool = True
    latest_meaningful_thought: str = ""

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

    async def _emit_artifact_mutations() -> None:
        """Scan workspace for changes and push canonical artifact events via SSE."""
        nonlocal event_seq
        mutations = await artifact_mgr.scan_mutations()
        for op, art_ref in mutations:
            rel = art_ref.relative_path
            if rel not in job_scoped_artifacts[job_id]:
                job_scoped_artifacts[job_id].append(rel)
            if chat_id and rel not in job_scoped_artifacts[chat_id]:
                job_scoped_artifacts[chat_id].append(rel)

            event_seq += 1
            sse_type = (
                SSEEventType.ARTIFACT_CREATED
                if op == "created"
                else SSEEventType.ARTIFACT_UPDATED
            )
            payload_data = {
                "artifact_id": art_ref.artifact_id,
                "name": art_ref.name,
                "relative_path": rel,
                "path": rel,
                "artifact": rel,
                "operation": op,
                "mime_type": art_ref.mime_type,
                "size_bytes": art_ref.size_bytes,
                "revision": art_ref.revision,
                "seq": event_seq,
                "job_id": job_id,
                "chat_id": chat_id,
                "model": model_name,
            }
            print(f"[BRIDGE CORE M2] Pushing SSE Artifact Event: {op.upper()} -> {rel} (rev {art_ref.revision})")
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=sse_type,
                    step=current_core_step,
                    data=payload_data,
                ),
            )

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
                        "engine": "peldrun-core",
                        "status": "running",
                    },
                ),
            )

        elif (
            event.type == EventType.THOUGHT
            or getattr(event.type, "name", "") == "STEP_PROGRESS"
        ):
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
            tool_name = str(
                event.payload.get("tool_name")
                or event.payload.get("tool")
                or "tool"
            )
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

            # Reconcile disk artifacts via push streaming on each observation
            await _emit_artifact_mutations()

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

        elif event.type in (
            EventType.ARTIFACT_CREATED,
            EventType.ARTIFACT_UPDATED,
            EventType.ARTIFACT_DELETED,
        ):
            await _emit_artifact_mutations()

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
        """Dedicated consumer worker guaranteeing strictly ordered event dispatch."""
        while True:
            event = await event_queue.get()
            if event is None:
                event_queue.task_done()
                break
            try:
                await _process_single_core_event(event)
            except Exception as ex:
                logger.error(
                    f"[BRIDGE ERROR] Exception in ordered event consumer for job {job_id}: {ex}",
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

    # 5. Instantiate Agent
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
        f"1. Working Directory: Your active directory is: {ws_path}\n"
        f"2. File Deliverables: ALWAYS use 'str_replace_editor' with command='create' to write and save project files directly to disk (e.g. index.html, style.css, app.js). DO NOT run Python scripts via 'python_execute' merely to save or create files.\n"
        f"3. Execution & Verification: Use 'python_execute' only when you need to run calculations, test execution, or process data. Use 'bash' for terminal environment commands.\n"
        f"4. Tool Calling Conventions: Always supply required parameters (e.g. 'command' and 'path' for str_replace_editor, 'query' for web_search, 'url' for browser_use).\n"
        f"5. Search & Web Fallback: Use 'web_search' for search queries and 'browser_use' to visit authoritative websites directly.\n"
        f"6. Mandatory Human Consultation (ask_human): When you need operator input, preferences, or sequential answers, invoke 'ask_human' with a clear prompt.\n"
        f"7. Citations & Attribution: Cite direct sources using Markdown links [Title](URL).\n"
        f"8. Task Completion: Call 'terminate' when project files are created and verified OR when conversational goals/questions are completely finished and you provide the final response.\n\n"
        f"[USER TASK]\n"
        f"{prompt}"
    )

    # 6. Primary Execution and Terminal Contract Enforcement
    try:
        final_answer = await agent.run_task(prompt=scoped_prompt, max_steps=max_steps)

        # Final mutation check before terminal state
        await _emit_artifact_mutations()

        # Gracefully drain and await all pending FIFO events before terminal state
        await event_queue.put(None)
        await consumer_task

    except Exception as exc:
        try:
            await event_queue.put(None)
            await asyncio.wait_for(consumer_task, timeout=2.0)
        except Exception:
            consumer_task.cancel()

        err_msg = f"Task execution interrupted: {exc}"
        print(f"[BRIDGE CORE ERROR] {err_msg}")

        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.ERROR,
                step=current_core_step,
                data={
                    "message": err_msg,
                    "seq": event_seq + 1,
                    "model": model_name,
                },
            ),
        )
        cleanup_job_state(job_id)
        return

    # 7. Finalize Deliverables and Complete Job
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
                "seq": event_seq + 1,
            },
        ),
    )

    cleanup_job_state(job_id)