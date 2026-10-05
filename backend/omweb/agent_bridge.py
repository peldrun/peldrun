"""
PELDRUN Universal Agent Bridge.
Dual-Engine runtime router dispatching execution to:
- PELDRUN Core: Native event-driven execution using EventEmitter and typed lifecycle contracts.
- OpenManus (Legacy): Backward-compatible execution using runtime memory instrumentation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

import httpx

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["MPLBACKEND"] = "Agg"
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager
from omweb.engine_resolver import (
    EngineType,
    get_active_engine_type,
    inject_engine_to_syspath,
    is_core_engine_available,
    is_legacy_engine_available,
    resolve_active_engine_path,
)

inject_engine_to_syspath()

human_answers: Dict[str, asyncio.Event] = {}
human_data: Dict[str, str] = {}
active_tasks: Dict[str, asyncio.Task] = {}
current_active_job_id: Dict[str, str] = {}
job_scoped_artifacts: Dict[str, List[str]] = {}


def read_active_toml_config() -> Dict[str, Any]:
    """Read active config.toml from engine path directly from disk."""
    engine_path = resolve_active_engine_path()
    candidates = [
        engine_path / "config" / "config.toml",
        engine_path / "config.toml",
        Path(__file__).resolve().parent.parent / "config.toml",
        Path(r"D:\AI\peldrun\config\config.toml"),
        Path(r"D:\AI\peldrun-core\config.toml"),
    ]

    target_file = None
    for cand in candidates:
        if cand.is_file():
            target_file = cand
            break

    if not target_file:
        return {}

    try:
        try:
            import tomllib
            with open(target_file, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            try:
                import tomli
                with open(target_file, "rb") as f:
                    return tomli.load(f)
            except ImportError:
                import toml
                with open(target_file, "r", encoding="utf-8") as f:
                    return toml.load(f)
    except Exception as e:
        print(f"[BRIDGE WARNING] Could not parse config.toml: {e}")
        return {}


def _err_card(title: str, description: str, hint: str = "", code: str = "") -> str:
    parts = [f"### ⚠️ {title}", "", description]
    if hint:
        parts += ["", f"> 💡 **Hint:** {hint}"]
    if code:
        parts += ["", "```", code, "```"]
    return "\n".join(parts)


def format_smart_error(err: Exception, model_name: str, provider_name: str) -> str:
    err_str = str(err)
    lower_err = err_str.lower()

    if (
        "failed to load model" in lower_err
        or "failed to load" in lower_err
        or "engine protocol predict request failed" in lower_err
        or "fetch failed" in lower_err
    ):
        return _err_card(
            title=f"Model `{model_name}` engine is not responding in LM Studio",
            description=(
                "LM Studio internal engine either crashed (GPU Out-Of-Memory) "
                "or the model is currently not loaded into memory."
            ),
            hint=(
                "Open LM Studio, navigate to the Local Server tab, and click 'Reload' "
                "(or eject and re-load the model) to refresh the engine memory."
            ),
            code=err_str if "fetch failed" in lower_err else ""
        )

    if "connection refused" in lower_err or "connecterror" in lower_err or "10061" in lower_err:
        if "1234" in err_str or "lmstudio" in provider_name.lower():
            return _err_card(
                title="Could not connect to the local LM Studio server",
                description="The LM Studio local server on **port 1234** is unreachable.",
                hint="Make sure the LM Studio application is running and the Local Server is enabled.",
            )
        if "11434" in err_str or "ollama" in provider_name.lower():
            return _err_card(
                title="Could not connect to the local Ollama server",
                description="The Ollama service on **port 11434** is unreachable.",
                hint="Make sure the Ollama service is running on your machine.",
            )

    if "context_length_exceeded" in lower_err or "maximum context length" in lower_err:
        return _err_card(
            title=f"Context length exceeded for model `{model_name}`",
            description="The conversation has exceeded the maximum context length of this model.",
            hint="Start a new conversation or reduce the size of the input.",
        )

    if "invalid_api_key" in lower_err or "incorrect api key" in lower_err or "401" in lower_err:
        return _err_card(
            title=f"API key authentication failed for provider `{provider_name}`",
            description="The API key was rejected by the provider.",
            hint="Verify the key is correct in Settings > Cloud Providers.",
        )

    return _err_card(
        title="Chat execution error",
        description=f"**Provider:** `{provider_name}` — **Model:** `{model_name}`",
        code=err_str,
    )


async def check_lmstudio_model_readiness(base_url: str, model_name: str, api_key: str = "") -> Dict[str, Any]:
    clean_base = base_url.replace("/v1", "").rstrip("/")
    headers = {"Accept": "application/json"}
    if api_key and "••••" not in api_key and api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {api_key}"

    endpoints = [
        f"{clean_base}/api/v1/models",
        f"{clean_base}/api/v0/models",
    ]

    for ep in endpoints:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(ep, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("models") or data.get("data") or []
                    for m in models:
                        mid = str(m.get("key") or m.get("id") or "")
                        if mid.lower() == model_name.lower() or model_name.lower() in mid.lower():
                            loaded_instances = m.get("loaded_instances") or []
                            is_loaded = len(loaded_instances) > 0
                            ctx_len = loaded_instances[0].get("config", {}).get("context_length") if is_loaded else None
                            return {
                                "found": True,
                                "is_loaded": is_loaded,
                                "context_length": ctx_len,
                                "unreachable": False,
                            }
                    return {"found": False, "is_loaded": False, "unreachable": False}
        except Exception:
            continue

    return {"found": False, "is_loaded": False, "unreachable": True}


def sanitize_final_result_text(raw_text: Any, latest_thought: str = "") -> str:
    """Sanitizes raw final results preventing internal ToolResult dumps from leaking to user."""
    if not raw_text:
        return latest_thought or "Task completed successfully."

    text = str(raw_text).strip()
    if text.startswith("output=") or "ToolResult" in text or "'terminated': True" in text:
        m = re.search(r"output=['\"](.*?)['\"]", text)
        if m:
            extracted = m.group(1).strip()
            if extracted and extracted != "Task execution finished with status: success":
                return extracted
        if latest_thought:
            return latest_thought
        return "Task completed successfully."

    return text


# ============================================================================
# PRIMARY ENGINE: PELDRUN Core Runtime Execution (Native EventEmitter & Contracts)
# ============================================================================

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
    """Execute autonomous agent workflow natively via peldrun-core package with real-time SSE streaming."""
    print(f"\n[BRIDGE CORE] >>> Starting Live Run for Job: {job_id} using PELDRUN Core Engine <<<")

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
    for essential in ("str_replace_editor", "bash", "python_execute", "web_search", "ask_human"):
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
        nonlocal latest_meaningful_thought, current_core_step, is_first_step_start
        
        # Rigorous step resolution starting at Step 1 and incrementing strictly upon subsequent steps
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
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.TOOL_CALL,
                    step=curr_step,
                    data={
                        "name": tool_name,
                        "toolName": tool_name,
                        "arguments": raw_args,
                        "content": raw_args,
                        "model": model_name
                    }
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
        f"2. File Deliverables: Use 'python_execute' to reliably create, write, and verify files on this machine.\n"
        f"3. Tool Invocation Rules: Always provide required parameters (e.g. 'code' for python_execute, 'command' for bash).\n"
        f"4. Do NOT call 'terminate' until the required files are actually written and inspected.\n\n"
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


# ============================================================================
# SECONDARY ENGINE: OpenManus Legacy Execution (Fallback / Compatibility)
# ============================================================================

def scope_legacy_mcp_servers(agent: Any, manifest: Dict[str, Any], project_dir: Optional[Path] = None) -> None:
    import shutil
    from omweb.extensions.registry import extension_registry

    allowed = [t.lower() for t in manifest.get("tools", [])]
    needs_mcp = any("browser" in t or "mcp" in t for t in allowed)

    if not needs_mcp and hasattr(agent, "initialize_mcp_servers"):
        async def dummy_init_mcp():
            return None
        agent.initialize_mcp_servers = dummy_init_mcp
        return

    if hasattr(agent, "connect_mcp_server"):
        async def dynamic_init_mcp():
            active_mcp = extension_registry.get_active_mcp_servers()
            for s in active_mcp:
                sid = s.get("id")
                cmd = s.get("command", "")
                transport = s.get("transport", "stdio")
                if transport == "stdio" and cmd:
                    parts = cmd.split()
                    exe = shutil.which(parts[0]) or parts[0]
                    args = list(parts[1:])
                    if "filesystem" in sid and project_dir:
                        args.append(str(project_dir.resolve()))
                    try:
                        await asyncio.wait_for(
                            agent.connect_mcp_server(exe, server_id=sid, use_stdio=True, stdio_args=args, tool_name_prefix=False),
                            timeout=10.0
                        )
                    except Exception as mcp_err:
                        print(f"[BRIDGE WARNING] Could not connect legacy MCP server '{sid}': {mcp_err}")
                elif transport == "sse" and s.get("url"):
                    try:
                        await asyncio.wait_for(
                            agent.connect_mcp_server(s["url"], server_id=sid, use_stdio=False),
                            timeout=10.0
                        )
                    except Exception as mcp_err:
                        print(f"[BRIDGE WARNING] Could not connect legacy MCP SSE server '{sid}': {mcp_err}")

        agent.initialize_mcp_servers = dynamic_init_mcp


def inject_legacy_runtime_llm(agent: Any, active_llm: Dict[str, Any]) -> None:
    model = active_llm.get("model")
    base_url = active_llm.get("base_url")
    api_key = active_llm.get("api_key") or "EMPTY"

    try:
        import app.config as app_config_mod
        if hasattr(app_config_mod, "config"):
            cfg = getattr(app_config_mod, "config")
            if hasattr(cfg, "llm"):
                llm_attr = getattr(cfg, "llm")
                if isinstance(llm_attr, dict):
                    llm_attr.update(active_llm)
                else:
                    for k, v in active_llm.items():
                        if hasattr(llm_attr, k):
                            setattr(llm_attr, k, v)
    except Exception:
        pass

    if hasattr(agent, "llm"):
        if model and hasattr(agent.llm, "model"):
            agent.llm.model = model
        if base_url:
            try:
                from openai import AsyncOpenAI
                agent.llm.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            except Exception as client_err:
                print(f"[BRIDGE WARNING] Could not rebuild legacy AsyncOpenAI client: {client_err}")


async def _run_legacy_openmanus_agent(
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
    print(f"[BRIDGE LEGACY] Executing job {job_id} using OpenManus legacy engine.")

    try:
        from app.agent.peldrun import peldrun as legacy_peldrun_agent
    except ImportError:
        try:
            from app.agent.manus import Manus as legacy_peldrun_agent
        except ImportError as e:
            err_msg = (
                f"OpenManus legacy engine files are not found on disk: {e}. "
                "Switch to 'peldrun-core' engine in Settings or restore the legacy engine files."
            )
            print(f"[BRIDGE ERROR] {err_msg}")
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(job_id, SSEEvent(type=SSEEventType.ERROR, step=0, data={"message": err_msg, "model": model_name}))
            return

    from omweb.tools.registry import tool_registry

    all_active_tool_ids = {t["id"].lower() for t in tool_registry.list_tools() if t.get("is_enabled", True)}
    allowed_tools = [t.lower() for t in manifest.get("tools", []) if t.lower() in all_active_tool_ids or t.lower() in ["mcp", "browser"]]
    allowed_tools.extend(["terminate", "ask_human"])

    agent = legacy_peldrun_agent()
    scope_legacy_mcp_servers(agent, manifest, project_dir)

    if manifest.get("max_steps"):
        agent.max_steps = manifest["max_steps"]

    sys_prompt = manifest.get("system_prompt", "").strip()
    if sys_prompt and hasattr(agent, "system_prompt"):
        try:
            agent.system_prompt = f"{sys_prompt}\n\n{agent.system_prompt}"
        except Exception:
            pass

    inject_legacy_runtime_llm(agent, active_llm)

    files_baseline: Set[str] = {p.name for p in project_dir.iterdir() if p.is_file()} if project_dir.exists() else set()
    latest_meaningful_thought: str = ""

    original_step = agent.step

    async def instrumented_step():
        nonlocal latest_meaningful_thought
        curr_step = getattr(agent, "current_step", 1)
        await dispatch_event(job_id, SSEEvent(type=SSEEventType.STEP_START, step=curr_step, data={"step": curr_step, "model": model_name, "engine": "openmanus"}))
        result = await original_step()

        if hasattr(agent, "memory") and hasattr(agent.memory, "messages"):
            for m in reversed(agent.memory.messages[-3:]):
                role = getattr(m, "role", "")
                content = getattr(m, "content", "")
                if role == "assistant" and content:
                    clean_c = content.strip()
                    if clean_c and not clean_c.startswith("terminate(") and not clean_c.startswith("```"):
                        latest_meaningful_thought = clean_c
                    await dispatch_event(job_id, SSEEvent(type=SSEEventType.THOUGHT, step=curr_step, data={"thought": content, "model": model_name}))
                    break
        return result

    original_execute_tool = agent.execute_tool

    async def instrumented_execute_tool(command):
        tool_name = getattr(command.function, "name", "unknown") if hasattr(command, "function") else "unknown"
        raw_args = getattr(command.function, "arguments", "{}") if hasattr(command, "function") else "{}"
        curr_step = getattr(agent, "current_step", 1)

        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.TOOL_CALL,
                step=curr_step,
                data={"name": tool_name, "arguments": raw_args, "model": model_name}
            )
        )

        files_before: Set[str] = {p.name for p in project_dir.iterdir() if p.is_file()} if project_dir.exists() else set()

        try:
            obs_output = await original_execute_tool(command)
        except Exception as tool_err:
            obs_output = f"Tool Execution Error ({tool_name}): {str(tool_err)}"

        if project_dir.exists():
            files_after = {p.name for p in project_dir.iterdir() if p.is_file()}
            for nf in (files_after - files_before):
                if nf not in job_scoped_artifacts[job_id]:
                    job_scoped_artifacts[job_id].append(nf)
                await dispatch_event(
                    job_id,
                    SSEEvent(
                        type=SSEEventType.OBSERVATION,
                        step=curr_step,
                        data={"artifact": nf, "path": nf, "chat_id": chat_id, "event": "artifact_created", "model": model_name}
                    )
                )

        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.OBSERVATION,
                step=curr_step,
                data={"output": obs_output, "model": model_name}
            )
        )
        return obs_output

    agent.execute_tool = instrumented_execute_tool
    agent.step = instrumented_step

    scoped_prompt = (
        f"[PROJECT WORKSPACE RULES]\n"
        f"1. Working Directory: Your active directory is set to: {project_dir.resolve()}\n"
        f"2. File Deliverables: Save generated files using clean relative names.\n\n"
        f"[USER PROMPT]\n"
        f"{prompt}"
    )

    orig_cwd = os.getcwd()
    try:
        os.chdir(str(project_dir.resolve()))
        final_out = await agent.run(scoped_prompt)
    finally:
        try:
            os.chdir(orig_cwd)
        except Exception:
            pass

    if project_dir.exists():
        current_files = {p.name for p in project_dir.iterdir() if p.is_file()}
        for f_name in (current_files - files_baseline):
            if f_name not in job_scoped_artifacts[job_id]:
                job_scoped_artifacts[job_id].append(f_name)

    new_turn_files = job_scoped_artifacts.get(job_id, [])

    if new_turn_files:
        file_bullets = "\n".join([f"- `{f}`" for f in new_turn_files])
        result_text = (
            f"### Deliverables Created Successfully\n\n"
            f"The requested project files have been built and saved in your workspace:\n\n"
            f"{file_bullets}\n\n"
            f"You can preview and interact with the application live in the **Preview** panel."
        )
    elif latest_meaningful_thought:
        result_text = latest_meaningful_thought
    else:
        raw_final = str(final_out) if final_out else ""
        for stop_tag in ["terminate(status=\"success\")", "terminate(status='success')", "</tool_call>"]:
            raw_final = raw_final.replace(stop_tag, "").strip()
        result_text = raw_final if raw_final else "Task completed successfully."

    job_manager.complete_job(job_id, result_text)
    await dispatch_event(
        job_id,
        SSEEvent(
            type=SSEEventType.FINAL,
            step=getattr(agent, "current_step", 1),
            data={
                "result": result_text,
                "model": model_name,
                "engine": "openmanus",
                "produced_files": new_turn_files
            }
        )
    )


# ============================================================================
# UNIVERSAL PUBLIC DISPATCHER ENTRY POINTS
# ============================================================================

async def run_instrumented(
    job_id: str,
    prompt: str,
    *args: Any,
    agent_id: Any = None,
    llm_override: Optional[Dict[str, Any]] = None,
    **kwargs: Any
) -> None:
    for arg in args:
        if isinstance(arg, dict) and llm_override is None:
            llm_override = arg
        elif isinstance(arg, str) and agent_id is None:
            agent_id = arg

    if not agent_id and isinstance(llm_override, dict):
        agent_id = llm_override.get("agent_id")

    if not agent_id or not isinstance(agent_id, str):
        agent_id = kwargs.get("agent_id") or "peldrun"

    explicit_engine = kwargs.get("engine") or (llm_override.get("engine") if isinstance(llm_override, dict) else None)
    if explicit_engine:
        eng_str = str(explicit_engine).lower().strip()
        target_engine = EngineType.LEGACY if eng_str in ["openmanus", "legacy", "manus"] else EngineType.CORE
    else:
        target_engine = get_active_engine_type()

    print(f"\n[BRIDGE] Initializing job {job_id} using engine: '{target_engine.value}'")
    current_active_job_id["current"] = job_id
    job_scoped_artifacts[job_id] = []

    toml_cfg = read_active_toml_config()
    active_llm = dict(toml_cfg.get("llm", {}))
    if llm_override:
        for k, v in llm_override.items():
            if v:
                active_llm[k] = v

        if llm_override.get("provider_name"):
            active_llm["provider_name"] = llm_override["provider_name"]
        elif llm_override.get("provider"):
            active_llm["provider_name"] = llm_override["provider"]
        elif llm_override.get("model") and llm_override["model"] != active_llm.get("model"):
            if "1234" not in str(active_llm.get("base_url", "")):
                active_llm["provider_name"] = active_llm.get("provider") or active_llm.get("model")
        active_llm.update(llm_override)

    p_cand = active_llm.get("provider_name") or active_llm.get("provider") or ""
    b_cand = str(active_llm.get("base_url") or "")
    if "1234" not in b_cand and "lmstudio" not in str(active_llm.get("provider") or "").lower():
        if "lm studio" in p_cand.lower():
            p_cand = active_llm.get("provider") or active_llm.get("model") or "Custom Engine"
    provider_name = p_cand or "Active Primary"
    model_name = active_llm.get("model") or "default"
    base_url = b_cand or "[http://127.0.0.1:1234/v1](http://127.0.0.1:1234/v1)"

    print(f"[BRIDGE] Target LLM: [{provider_name}] Model: '{model_name}' | URL: '{base_url}'")

    if "1234" in base_url or "lmstudio" in provider_name.lower():
        readiness = await check_lmstudio_model_readiness(base_url, model_name, active_llm.get("api_key", ""))
        if readiness.get("unreachable"):
            err_msg = format_smart_error(Exception("Connection refused (Port 1234)"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(job_id, SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}))
            return

        if readiness.get("found") and not readiness.get("is_loaded"):
            err_msg = format_smart_error(Exception("failed to load model"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(job_id, SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}))
            return

    await asyncio.sleep(0.05)
    await dispatch_event(
        job_id,
        SSEEvent(
            type=SSEEventType.STEP_START,
            step=1,
            data={
                "status": "running",
                "model": model_name,
                "provider": provider_name,
                "mode": "agent",
                "engine": target_engine.value
            }
        )
    )

    from omweb.agents.registry import agent_registry
    manifest = agent_registry.get_agent(agent_id)
    if manifest.get("status") == "disabled":
        err_msg = f"Agent '{manifest.get('name')}' is currently disabled in the Capability Store. Enable it first to run tasks."
        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(job_id, SSEEvent(type=SSEEventType.ERROR, step=0, data={"message": err_msg, "model": model_name}))
        return

    chat = project_manager.get_chat(job_id) or {}
    chat_id = chat.get("id", f"chat_{job_id}")
    project_id = chat.get("project_id", "default_project")
    project_dir = project_manager.get_chat_files_dir(chat_id, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    try:
        if target_engine == EngineType.CORE and is_core_engine_available():
            await _run_peldrun_core_agent(
                job_id=job_id,
                prompt=prompt,
                agent_id=agent_id,
                active_llm=active_llm,
                model_name=model_name,
                provider_name=provider_name,
                project_dir=project_dir,
                chat_id=chat_id,
                manifest=manifest,
            )
        else:
            await _run_legacy_openmanus_agent(
                job_id=job_id,
                prompt=prompt,
                agent_id=agent_id,
                active_llm=active_llm,
                model_name=model_name,
                provider_name=provider_name,
                project_dir=project_dir,
                chat_id=chat_id,
                manifest=manifest,
            )
    except asyncio.CancelledError:
        print(f"[BRIDGE] Job was aborted: {job_id}")
    except Exception as err:
        tb = traceback.format_exc()
        print(f"[BRIDGE ERROR] {err}\n{tb}")
        err_msg = format_smart_error(err, model_name, provider_name)
        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.ERROR,
                step=1,
                data={"message": err_msg, "model": model_name}
            )
        )
    finally:
        human_answers.pop(job_id, None)
        human_data.pop(job_id, None)
        active_tasks.pop(job_id, None)
        if current_active_job_id.get("current") == job_id:
            current_active_job_id.pop("current", None)


async def run_direct_chat(
    job_id: str,
    prompt: str,
    llm_override: Optional[Dict[str, Any]] = None
) -> None:
    print(f"\n[BRIDGE DIRECT CHAT] Initializing direct chat for job: {job_id}")
    current_active_job_id["current"] = job_id
    job_scoped_artifacts[job_id] = []

    toml_cfg = read_active_toml_config()
    active_llm = dict(toml_cfg.get("llm", {}))
    if llm_override:
        for k, v in llm_override.items():
            if v:
                active_llm[k] = v

        if llm_override.get("provider_name"):
            active_llm["provider_name"] = llm_override["provider_name"]
        elif llm_override.get("provider"):
            active_llm["provider_name"] = llm_override["provider"]
        elif llm_override.get("model") and llm_override["model"] != active_llm.get("model"):
            if "1234" not in str(active_llm.get("base_url", "")):
                active_llm["provider_name"] = active_llm.get("provider") or active_llm.get("model")
        active_llm.update(llm_override)

    p_cand = active_llm.get("provider_name") or active_llm.get("provider") or ""
    b_cand = str(active_llm.get("base_url") or "")
    if "1234" not in b_cand and "lmstudio" not in str(active_llm.get("provider") or "").lower():
        if "lm studio" in p_cand.lower():
            p_cand = active_llm.get("provider") or active_llm.get("model") or "Custom Engine"
    provider_name = p_cand or "Active Primary"
    model_name = active_llm.get("model") or "default"
    base_url = b_cand or "[http://127.0.0.1:1234/v1](http://127.0.0.1:1234/v1)"
    api_key = active_llm.get("api_key") or "EMPTY"

    if "1234" in base_url or "lmstudio" in provider_name.lower():
        readiness = await check_lmstudio_model_readiness(base_url, model_name, api_key)
        if readiness.get("unreachable"):
            err_msg = format_smart_error(Exception("Connection refused (Port 1234)"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(job_id, SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}))
            return

        if readiness.get("found") and not readiness.get("is_loaded"):
            err_msg = format_smart_error(Exception("failed to load model"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(job_id, SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}))
            return

    await asyncio.sleep(0.05)
    await dispatch_event(
        job_id,
        SSEEvent(
            type=SSEEventType.STEP_START,
            step=1,
            data={
                "status": "running",
                "model": model_name,
                "provider": provider_name,
                "mode": "chat"
            }
        )
    )

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        chat = project_manager.get_chat(job_id) or {}
        turns = chat.get("turns", [])

        messages = [
            {
                "role": "system",
                "content": "You are a helpful, direct, and conversational AI assistant. Respond directly, accurately, and naturally to the user using Markdown."
            }
        ]

        for t in turns[-6:]:
            p = t.get("prompt")
            r = t.get("result")
            if p:
                messages.append({"role": "user", "content": p})
            if r:
                messages.append({"role": "assistant", "content": r})

        messages.append({"role": "user", "content": prompt})

        max_tokens = active_llm.get("max_tokens") or 8192
        if isinstance(max_tokens, str):
            try:
                max_tokens = int(max_tokens)
            except Exception:
                max_tokens = 8192

        response = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=float(active_llm.get("temperature", 0.7)),
            stream=False
        )

        result_text = ""
        if response.choices and len(response.choices) > 0:
            msg = response.choices[0].message
            result_text = getattr(msg, "content", "") or ""

        if not result_text:
            result_text = "I received your message, but no content was returned by the model."

        print(f"[BRIDGE DIRECT CHAT] Completed successfully for job: {job_id}")
        job_manager.complete_job(job_id, result_text)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.FINAL,
                step=1,
                data={
                    "result": result_text,
                    "model": model_name,
                    "mode": "chat",
                    "produced_files": []
                }
            )
        )

    except asyncio.CancelledError:
        print(f"[BRIDGE DIRECT CHAT] Job was cancelled: {job_id}")
    except Exception as err:
        tb = traceback.format_exc()
        print(f"[BRIDGE DIRECT CHAT ERROR] {err}\n{tb}")
        err_msg = format_smart_error(err, model_name, provider_name)
        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.ERROR,
                step=1,
                data={"message": err_msg, "model": model_name}
            )
        )
    finally:
        human_answers.pop(job_id, None)
        human_data.pop(job_id, None)
        active_tasks.pop(job_id, None)
        if current_active_job_id.get("current") == job_id:
            current_active_job_id.pop("current", None)