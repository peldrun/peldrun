"""
backend/omweb/agent_bridge_parts/dispatcher.py

Authoritative dispatcher entry points for the PELDRUN Universal Agent Bridge.

Dispatches execution to registered engines via the authoritative EngineRegistry abstraction layer.
Hardened under PR 2 (Single Execution Authority):
- Enforces single AgentRunner authority by routing all native core runs to PeldrunEngine.
- Strictly eliminates competing runner initialization or duplicate STEP_START events.
- Guarantees deterministic task registration and cleanup per job.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, Dict, Optional

from omweb.agents.registry import AgentNotFoundError, agent_registry
from omweb.engine_resolver import (
    EngineType,
    get_active_engine_type,
)
from omweb.engines import engine_registry
from omweb.engines.base import EngineRunContext
from omweb.engines.registry import EngineNotFoundError
from omweb.job_manager import job_manager
from omweb.project_manager import project_manager
from omweb.sse_events import SSEEvent, SSEEventType, dispatch_event

from .config_loader import read_active_toml_config
from .errors import format_smart_error
from .lmstudio import check_lmstudio_model_readiness
from .state import (
    cleanup_job_state,
    job_scoped_artifacts,
    register_job_task,
)

logger = logging.getLogger(__name__)


async def run_instrumented(
    job_id: str,
    prompt: str,
    *args: Any,
    agent_id: Any = None,
    llm_override: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> None:
    """
    Dispatch an agent job using the authoritative execution engine registry.
    Ensures single runtime authority delegation.
    """
    # 0. Track current task for safe cancellation
    current_task = asyncio.current_task()
    if current_task is not None:
        register_job_task(job_id, current_task)

    for arg in args:
        if isinstance(arg, dict) and llm_override is None:
            llm_override = arg
        elif isinstance(arg, str) and agent_id is None:
            agent_id = arg

    if not agent_id and isinstance(llm_override, dict):
        agent_id = llm_override.get("agent_id")

    if not agent_id or not isinstance(agent_id, str):
        agent_id = kwargs.get("agent_id") or "peldrun"

    # 1. Resolve target engine identifier
    explicit_engine = kwargs.get("engine") or (
        llm_override.get("engine") if isinstance(llm_override, dict) else None
    )
    if explicit_engine:
        target_engine_str = str(explicit_engine).lower().strip()
    else:
        active_type = get_active_engine_type()
        target_engine_str = (
            active_type.value if isinstance(active_type, EngineType) else str(active_type)
        )

    print(f"\n[BRIDGE] Initializing job {job_id} using engine: '{target_engine_str}'")
    job_scoped_artifacts.setdefault(job_id, [])

    # 2. Strict Fail-Fast Engine Resolution
    try:
        engine = engine_registry.get(target_engine_str)
    except EngineNotFoundError as eng_err:
        err_msg = str(eng_err)
        print(f"[BRIDGE ERROR] {err_msg}")
        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.ERROR,
                step=1,
                data={"message": err_msg, "engine": target_engine_str},
            ),
        )
        cleanup_job_state(job_id)
        return

    # 3. Strict Fail-Fast Agent Resolution
    try:
        manifest = agent_registry.get_agent(agent_id)
    except AgentNotFoundError as agent_err:
        err_msg = str(agent_err)
        print(f"[BRIDGE ERROR] {err_msg}")
        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(
            job_id,
            SSEEvent(type=SSEEventType.ERROR, step=0, data={"message": err_msg}),
        )
        cleanup_job_state(job_id)
        return

    if manifest.get("status") == "disabled":
        err_msg = f"Agent '{manifest.get('name', agent_id)}' is currently disabled in Capability Store. Enable it first."
        job_manager.fail_job(job_id, err_msg)
        await dispatch_event(
            job_id,
            SSEEvent(type=SSEEventType.ERROR, step=0, data={"message": err_msg}),
        )
        cleanup_job_state(job_id)
        return

    # 4. Resolve runtime LLM settings
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
    base_url = b_cand or "http://127.0.0.1:1234/v1"

    print(f"[BRIDGE] Target LLM: [{provider_name}] Model: '{model_name}' | URL: '{base_url}'")

    # 5. Perform LM Studio health check if targeting local endpoint
    if "1234" in base_url or "lmstudio" in provider_name.lower():
        readiness = await check_lmstudio_model_readiness(base_url, model_name, active_llm.get("api_key", ""))
        if readiness.get("unreachable"):
            err_msg = format_smart_error(Exception("Connection refused (Port 1234)"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(
                job_id,
                SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}),
            )
            cleanup_job_state(job_id)
            return

        if readiness.get("found") and not readiness.get("is_loaded"):
            err_msg = format_smart_error(Exception("failed to load model"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(
                job_id,
                SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}),
            )
            cleanup_job_state(job_id)
            return

    # 6. Lifecycle Authority Delegation (Eliminate duplicate STEP_START)
    is_native_core = target_engine_str in ("peldrun", "peldrun-core", "core")

    if not is_native_core:
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
                    "engine": target_engine_str,
                },
            ),
        )
    else:
        status_event_type = getattr(SSEEventType, "STATUS", None) or getattr(SSEEventType, "RUN_ACCEPTED", None)
        if status_event_type is not None:
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=status_event_type,
                    step=0,
                    data={
                        "status": "accepted",
                        "model": model_name,
                        "provider": provider_name,
                        "mode": "agent",
                        "engine": target_engine_str,
                    },
                ),
            )

    # 7. Prepare project workspace directory
    chat = project_manager.get_chat(job_id) or {}
    chat_id = chat.get("id", f"chat_{job_id}")
    project_id = chat.get("project_id", "default_project")
    project_dir = project_manager.get_chat_files_dir(chat_id, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    # 8. Execution through Authoritative Engine Interface
    context = EngineRunContext(
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

    try:
        await engine.run(context)
    except asyncio.CancelledError:
        print(f"[BRIDGE] Job was cancelled/aborted: {job_id}")
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
                data={"message": err_msg, "model": model_name},
            ),
        )
    finally:
        cleanup_job_state(job_id)


async def run_direct_chat(
    job_id: str,
    prompt: str,
    llm_override: Optional[Dict[str, Any]] = None,
) -> None:
    """Dispatch a plain, non-agentic chat completion job."""
    current_task = asyncio.current_task()
    if current_task is not None:
        register_job_task(job_id, current_task)

    print(f"\n[BRIDGE DIRECT CHAT] Initializing direct chat for job: {job_id}")
    job_scoped_artifacts.setdefault(job_id, [])

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
    base_url = b_cand or "http://127.0.0.1:1234/v1"
    api_key = active_llm.get("api_key") or "EMPTY"

    if "1234" in base_url or "lmstudio" in provider_name.lower():
        readiness = await check_lmstudio_model_readiness(base_url, model_name, api_key)
        if readiness.get("unreachable"):
            err_msg = format_smart_error(Exception("Connection refused (Port 1234)"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(
                job_id,
                SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}),
            )
            cleanup_job_state(job_id)
            return

        if readiness.get("found") and not readiness.get("is_loaded"):
            err_msg = format_smart_error(Exception("failed to load model"), model_name, provider_name)
            job_manager.fail_job(job_id, err_msg)
            await dispatch_event(
                job_id,
                SSEEvent(type=SSEEventType.ERROR, step=1, data={"message": err_msg, "model": model_name}),
            )
            cleanup_job_state(job_id)
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
                "mode": "chat",
            },
        ),
    )

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        chat = project_manager.get_chat(job_id) or {}
        turns = chat.get("turns", [])

        messages = [
            {
                "role": "system",
                "content": "You are a helpful, direct, and conversational AI assistant. Respond directly, accurately, and naturally to the user using Markdown.",
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
            stream=False,
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
                    "produced_files": [],
                },
            ),
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
                data={"message": err_msg, "model": model_name},
            ),
        )
    finally:
        cleanup_job_state(job_id)