"""
backend/omweb/agent_bridge_parts/dispatcher.py

Authoritative dispatcher entry points for the PELDRUN Universal Agent Bridge.

Dispatches execution to registered engines via the authoritative EngineRegistry abstraction layer.
Hardened under PR 2 (Single Execution Authority), Milestone P1 (Usage Accounting),
and Hybrid Reasoning Control (2026 Industry Spec):
- Enforces single AgentRunner authority by routing all native core runs to PeldrunEngine.
- Strictly unifies the canonical token accounting path between Agent and Direct Chat modes.
- Direct Chat invocations are fully metered in SQLite token_ledger and projected to session.json.
- Dynamically resolves model reasoning capabilities from caller payload and persistent metadata database.
- Strictly normalizes 'off' to 'none' for OpenAI-compatible schema compliance without mutating graduated levels.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
import traceback
from typing import Any, Dict, List, Optional
import uuid

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


# Public Core LLM & Telemetry Boundary Imports (P1-02 Conformance)
from peldrun.llm.client import AsyncLLMClient
from peldrun.runtime.pricing_store import NANO_USD_PER_USD, get_pricing_store
from peldrun.runtime.usage_store import LLMInvocationRecord, get_usage_store

# Context Management & Sidecar Integration
from omweb.chat_storage_engine import chat_storage_engine
from peldrun.context.builder import ContextManager
from peldrun.context.sidecar import build_context_sidecar, load_context_sidecar

from .config_loader import read_active_toml_config
from .errors import format_smart_error
from .lmstudio import check_lmstudio_model_readiness
from .state import (
    cleanup_job_state,
    job_scoped_artifacts,
    register_job_task,
)

logger = logging.getLogger(__name__)


def _check_model_metadata_reasoning(model_name: str) -> Optional[bool]:
    """
    Check persisted models_metadata.json for explicit reasoning capability.
    Allows user settings and model discovery metadata to act as source of truth.
    Safely resolves across working directories and relative paths.
    """
    try:
        module_dir = Path(__file__).resolve().parent
        candidates = [
            module_dir.parent.parent / "storage" / "models_metadata.json",
            Path("storage/models_metadata.json").resolve(),
            Path("backend/storage/models_metadata.json").resolve(),
        ]
        for p in candidates:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                meta = data.get(model_name)
                if isinstance(meta, dict):
                    caps = meta.get("capabilities", {})
                    if isinstance(caps, dict) and "reasoning" in caps:
                        return bool(caps["reasoning"])
                    if "is_reasoning" in meta:
                        return bool(meta["is_reasoning"])
                    if "reasoning" in meta:
                        return bool(meta["reasoning"])
    except Exception as e:
        logger.debug(f"Could not load reasoning metadata for {model_name}: {e}")
    return None


def _is_reasoning_model(model_name: str, active_llm: Optional[Dict[str, Any]] = None) -> bool:
    """
    Deterministically identify whether the target model is a reasoning-centric model:
    1. Highest priority: explicit flag passed from caller / frontend metadata.
    2. Second priority: database record in models_metadata.json.
    3. Fallback: heuristic pattern matching for known reasoning architectures.
    """
    if active_llm:
        if "is_reasoning_model" in active_llm and active_llm["is_reasoning_model"] is not None:
            return bool(active_llm["is_reasoning_model"])
        if "is_reasoning" in active_llm and active_llm["is_reasoning"] is not None:
            return bool(active_llm["is_reasoning"])

    db_flag = _check_model_metadata_reasoning(model_name)
    if db_flag is not None:
        return db_flag

    norm = (model_name or "").lower()
    return any(
        sub in norm
        for sub in (
            "nemotron",
            "deepseek-r1",
            "reasoner",
            "qwq",
            "o1-",
            "o3-",
            "thought",
            "thinking",
        )
    )


def _build_optimized_chat_messages(
    turns: List[Dict[str, Any]],
    current_prompt: str,
    model_name: str,
    reasoning_effort: str,
    active_llm: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """
    Construct chat completion messages obeying 2026 reasoning model guidelines:
    - If model is a reasoning architecture or reasoning effort is off/none, avoid long personas
      that cause semantic distractions and infinite thought loops.
    - If reasoning is explicitly requested or model is standard, provide clean concise instructions.
    """
    messages: List[Dict[str, str]] = []
    is_reasoner = _is_reasoning_model(model_name, active_llm=active_llm)
    norm_effort = (reasoning_effort or "none").lower().strip()

    if is_reasoner and norm_effort in ("none", "off", "0"):
        pass
    elif is_reasoner:
        messages.append({
            "role": "system",
            "content": "You are a helpful and concise assistant. Respond naturally and directly.",
        })
    else:
        messages.append({
            "role": "system",
            "content": "You are a helpful, direct, and conversational AI assistant. Respond directly and accurately using Markdown.",
        })

    for t in turns[-6:]:
        p = t.get("prompt")
        r = t.get("result")
        if p:
            messages.append({"role": "user", "content": p})
        if r:
            messages.append({"role": "assistant", "content": r})

    messages.append({"role": "user", "content": current_prompt})
    return messages


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
    Ensures single runtime authority delegation and preserves reasoning_effort.
    """
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

    toml_cfg = read_active_toml_config()
    active_llm = dict(toml_cfg.get("llm", {}))
    if llm_override:
        for k, v in llm_override.items():
            if v is not None:
                active_llm[k] = v

        if llm_override.get("provider_name"):
            active_llm["provider_name"] = llm_override["provider_name"]
        elif llm_override.get("provider"):
            active_llm["provider_name"] = llm_override["provider"]
        elif llm_override.get("model") and llm_override["model"] != active_llm.get("model"):
            if "1234" not in str(active_llm.get("base_url", "")):
                active_llm["provider_name"] = active_llm.get("provider") or active_llm.get("model")
        active_llm.update(llm_override)

    # Normalize reasoning_effort strictly: 'off' -> 'none'
    if "reasoning_effort" in active_llm and active_llm["reasoning_effort"]:
        raw_eff = str(active_llm["reasoning_effort"]).lower().strip()
        if raw_eff in ("none", "off", "0"):
            active_llm["reasoning_effort"] = "none"

    p_cand = active_llm.get("provider_name") or active_llm.get("provider") or ""
    b_cand = str(active_llm.get("base_url") or "")
    if "1234" not in b_cand and "lmstudio" not in str(active_llm.get("provider") or "").lower():
        if "lm studio" in p_cand.lower():
            p_cand = active_llm.get("provider") or active_llm.get("model") or "Custom Engine"
    provider_name = p_cand or "Active Primary"
    model_name = active_llm.get("model") or "default"
    base_url = b_cand or "http://127.0.0.1:1234/v1"

    print(f"[BRIDGE] Target LLM: [{provider_name}] Model: '{model_name}' | URL: '{base_url}' | Reasoning: '{active_llm.get('reasoning_effort')}'")

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

    chat = project_manager.get_chat(job_id) or {}
    chat_id = chat.get("id", f"chat_{job_id}")
    project_id = chat.get("project_id", "default_project")
    project_dir = project_manager.get_chat_files_dir(chat_id, project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

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
    """
    Dispatch a direct, conversational chat completion job.
    Fully integrated into the canonical token accounting and dynamic pricing subsystem.
    Enforces dynamic reasoning effort control, optimized prompts, and progressive token telemetry.
    """
    current_task = asyncio.current_task()
    if current_task is not None:
        register_job_task(job_id, current_task)

    print(f"\n[BRIDGE DIRECT CHAT] Initializing metered direct chat for job: {job_id}")
    job_scoped_artifacts.setdefault(job_id, [])

    toml_cfg = read_active_toml_config()
    active_llm = dict(toml_cfg.get("llm", {}))
    if llm_override:
        for k, v in llm_override.items():
            if v is not None:
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

    is_reasoner = _is_reasoning_model(model_name, active_llm=active_llm)
    raw_effort = str(active_llm.get("reasoning_effort") or "none").lower().strip() if is_reasoner else "none"
    reasoning_effort = "none" if raw_effort in ("none", "off", "0") else raw_effort

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
                "is_reasoning_model": is_reasoner,
                "reasoning_effort": reasoning_effort,
            },
        ),
    )

    start_time = time.time()
    try:
        chat = project_manager.get_chat(job_id) or {}
        chat_id = chat.get("id", f"chat_{job_id}")
        project_id = chat.get("project_id", "default_project")
        turns = chat.get("turns", [])

        # Resolve Context Sidecar and apply dynamic user token budget
        session_dir = project_manager.chats_dir / chat_id
        sidecar = load_context_sidecar(session_dir)
        if not sidecar:
            sidecar = build_context_sidecar(
                session_id=chat_id,
                session_data=chat,
                model_id=model_name,
                storage_root=project_manager.storage_dir,
            )

        context_mgr = ContextManager()
        messages, budget_usage = context_mgr.build_messages(
            sidecar=sidecar,
            current_user_message=prompt,
        )
        print(
            f"[CONTEXT BUDGET] Enforced limits for {model_name} | "
            f"Active: {budget_usage.get('total_tokens', 0)} / {budget_usage.get('effective_limit', 0)} tokens"
        )

        max_tokens = active_llm.get("max_tokens") or 8192
        if isinstance(max_tokens, str):
            try:
                max_tokens = int(max_tokens)
            except Exception:
                max_tokens = 8192

        client = AsyncLLMClient(
            base_url=base_url,
            api_key=api_key,
            timeout=float(active_llm.get("timeout") or 300.0),
        )

        completion_kwargs: Dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": float(active_llm.get("temperature", 0.7)),
            "model": model_name,
        }

        if is_reasoner and reasoning_effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
            completion_kwargs["reasoning_effort"] = reasoning_effort

        response = await client.chat_completion(**completion_kwargs)
        completed_time = time.time()

        result_text = response.content or ""
        if not result_text:
            result_text = "I received your message, but no content was returned by the model."

        reasoning_thought = getattr(response, "reasoning_content", None)
        if reasoning_thought and str(reasoning_thought).strip():
            await dispatch_event(
                job_id,
                SSEEvent(
                    type=SSEEventType.THOUGHT,
                    step=1,
                    data={"thought": str(reasoning_thought).strip(), "model": model_name},
                ),
            )

        pricing_store = get_pricing_store()
        usage_store = get_usage_store()
        usage = response.usage

        cost_nano_usd = 0
        pricing_rule_id = None
        if usage:
            cost_nano_usd, pricing_rule_id = await pricing_store.calculate_cost(
                usage=usage,
                provider=provider_name,
                model=model_name,
                base_url=base_url,
            )

        cost_usd = round(cost_nano_usd / NANO_USD_PER_USD, 6)

        inv_id = f"inv_chat_{uuid.uuid4().hex[:10]}"
        turn_id = f"turn_{len(turns) + 1}"

        if usage:
            record = LLMInvocationRecord(
                invocation_id=inv_id,
                job_id=job_id,
                chat_id=chat_id,
                project_id=project_id,
                turn_id=turn_id,
                mode="chat",
                provider=provider_name,
                model_requested=model_name,
                model_returned=response.model or model_name,
                status="completed",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                usage_source=usage.source.value if hasattr(usage.source, "value") else str(usage.source),
                estimated=usage.estimated,
                tokenizer_id=usage.tokenizer_id,
                tokenizer_version=usage.tokenizer_version,
                estimation_method=usage.estimation_method,
                started_at=start_time,
                first_token_at=getattr(response, "first_token_at", None),
                completed_at=completed_time,
                latency_ms=round((completed_time - start_time) * 1000.0, 2),
                ttft_ms=round(response.ttft_ms, 2) if response.ttft_ms is not None else None,
                finish_reason=response.finish_reason,
                pricing_version_id=pricing_rule_id,
                cost_nano_usd=cost_nano_usd,
            )
            await usage_store.record_invocation(record)

            turn_usage_dict = {
                "turn_id": turn_id,
                "prompt_tokens": usage.input_tokens or 0,
                "completion_tokens": usage.output_tokens or 0,
                "total_tokens": usage.total_tokens or 0,
                "cost_usd": cost_usd,
                "cost_nano_usd": cost_nano_usd,
                "estimated": usage.estimated,
                "latency_ms": response.latency_ms,
            }
            project_manager.record_turn_usage(
                chat_id=chat_id,
                turn_id=turn_id,
                turn_usage=turn_usage_dict,
                project_id=project_id,
            )

        print(
            f"[BRIDGE DIRECT CHAT] Completed for job {job_id} | "
            f"Tokens: {usage.total_tokens if usage else 0} | Cost: ${cost_usd}"
        )
        job_manager.complete_job(job_id, result_text)

        # Automatically update session.context.json with the new conversation turn
        try:
            chat_storage_engine.sync_sidecar(chat_id, model_id=model_name)
        except Exception as sidecar_err:
            logger.debug(f"[BRIDGE DIRECT CHAT] Sidecar sync notice: {sidecar_err}")

        await dispatch_event(
            job_id,
            SSEEvent(
                type=SSEEventType.FINAL,
                step=1,
                data={
                    "result": result_text,
                    "model": model_name,
                    "provider": provider_name,
                    "mode": "chat",
                    "produced_files": [],
                    "usage": usage.to_dict() if usage else None,
                    "cost_usd": cost_usd,
                    "latency_ms": response.latency_ms,
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


__all__ = [
    "run_instrumented",
    "run_direct_chat",
]