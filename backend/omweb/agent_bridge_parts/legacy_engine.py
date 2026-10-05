"""
OpenManus legacy engine runner (fallback / backward compatibility).

This module provides:
    - scope_legacy_mcp_servers : conditionally disables / wires MCP servers.
    - inject_legacy_runtime_llm: overrides the legacy LLM config at runtime.
    - _run_legacy_openmanus_agent: main runner that instruments the legacy
      agent's step() and execute_tool() to stream SSE events to the UI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event
from omweb.job_manager import job_manager

from .state import job_scoped_artifacts


def scope_legacy_mcp_servers(agent: Any, manifest: Dict[str, Any], project_dir: Optional[Path] = None) -> None:
    """Conditionally disable or wire MCP servers on a legacy OpenManus agent.

    If the agent's manifest does not request any browser/MCP tools, the
    agent's `initialize_mcp_servers` is monkey-patched to a no-op so that
    no external MCP processes are spawned.

    Otherwise, `initialize_mcp_servers` is replaced with a dynamic
    initializer that connects every active MCP server from the extension
    registry (stdio or SSE), injecting the project directory as a
    filesystem root when applicable.

    Args:
        agent:       The instantiated legacy agent object.
        manifest:    The agent manifest containing the "tools" list.
        project_dir: Workspace directory of the current chat (optional).
    """
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
    """Override the legacy agent's LLM configuration at runtime.

    Patches both the global `app.config.config.llm` object (if present)
    and the agent's own `agent.llm` object so that the supplied model,
    base_url and api_key are used for the current run only.

    Args:
        agent:      The instantiated legacy agent object.
        active_llm: Resolved LLM config dict (model, base_url, api_key, ...).
    """
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
    """Execute a job using the legacy OpenManus agent engine.

    Instruments the agent's `step()` and `execute_tool()` methods to
    stream STEP_START / THOUGHT / TOOL_CALL / OBSERVATION / FINAL events
    to the Web UI as SSE, while running the agent inside the project
    directory as its current working directory.

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
    allowed_tools = [t.lower() for t in manifest.get("tools", []) if t.lower() in all_active_tool_ids or t.lower() in ["mcp", "browser", "browser_use"]]
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
        """Wrap the legacy step() to emit STEP_START and THOUGHT SSE events."""
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
        """Wrap execute_tool() to emit TOOL_CALL + OBSERVATION + artifact SSE events."""
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