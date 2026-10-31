# Path: backend/peldrun/agents/tool_call_agent.py
"""
backend/peldrun/agents/tool_call_agent.py

Autonomous Tool Calling Agent for PELDRUN Core Runtime.
Implements the StepExecutableAgent protocol driving atomic reasoning-action steps.
Hardened under PR 4 (Centralized Tool Retry & LLM Failure Decoupling):
- Decouples LLM retries (transient/timeout/compact) from tool retries.
- Executes tool retry loop centrally governed by ToolExecutionPolicy.
- Emits canonical EventType.TOOL_RETRY with attempt count and delay.
- Restricts non-idempotent/destructive operations to single attempt.
- Records each discrete attempt in ExecutionState.tool_history.
- Propagates dynamic reasoning_effort explicitly in each reasoning step.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Union
import uuid

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.engine.state import ExecutionState, ExecutionStatus, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.llm.client import LLMResponse, ToolCall
from peldrun.tools.base import ToolResult
from peldrun.tools.collection import ToolCollection
from peldrun.tools.contract import ToolExecutionPolicy

logger = logging.getLogger("peldrun.agents.tool_call_agent")


# ---------------------------------------------------------------------------
# Loop detection thresholds
# ---------------------------------------------------------------------------
# Detects when the LLM calls the same tool with identical arguments for
# N consecutive steps. Three is the safe minimum: a legitimate agent
# rarely calls the same tool identically twice in a row, and three
# consecutive identical calls are essentially always a loop.
_LOOP_DETECTION_WINDOW: int = 3


# ---------------------------------------------------------------------------
# OOM detection helper
# ---------------------------------------------------------------------------
def _is_oom_error(exc: BaseException) -> bool:
    """
    Detect provider out-of-memory errors.

    Kept in sync with the helper in ``peldrun.llm.client``. Duplicated
    here rather than imported to keep the coupling between the agent
    loop and the LLM transport minimal — a change in either module
    should not silently break the other.
    """
    if exc is None:
        return False
    err_str = str(exc).lower()
    if not err_str:
        return False
    patterns = (
        "bad allocation",
        "out of memory",
        "cuda out of memory",
        "insufficient memory",
        "memory exhausted",
        "cannot allocate",
        "oom error",
    )
    return any(p in err_str for p in patterns)


# ---------------------------------------------------------------------------
# Observation truncation for tool outputs
# ---------------------------------------------------------------------------
# Tool observations are appended verbatim to ``self.messages`` and to
# ``state.messages`` — both of which are re-sent to the LLM on every
# subsequent step. A single 20 KB observation therefore costs ~5 KB of
# context per step, multiplied by every remaining step in the run.
#
# The per-tool limits below keep the LLM's working context bounded while
# preserving the top results that matter most. The full observation is
# still recorded in ``state.tool_history`` (for audit) and in the SSE
# event stream (for UI) — only the conversation-history copy is capped.
_OBSERVATION_TRUNCATION_LIMITS: Dict[str, int] = {
    "web_search": 3000,        # ~750 tokens — enough for top 3-5 results
    "web_fetch": 5000,
    "http_fetch": 5000,
    "browser_interactive": 4000,
    "bash": 4000,
    "python_execute": 4000,
}
_DEFAULT_OBSERVATION_LIMIT: int = 8000


def _truncate_observation(tool_name: str, output: str) -> str:
    """
    Truncate a tool observation for inclusion in the conversation history.

    Preserves the beginning (first ~70% of the limit) and the tail (last
    ~20%) so that both results lists and trailing error messages survive.
    The middle section — typically redundant results 6..N of a search
    query, or the bulk of a large file listing — is dropped with a clear
    marker so the LLM knows to refine the call if more detail is needed.
    """
    if not output:
        return output
    limit = _OBSERVATION_TRUNCATION_LIMITS.get(
        (tool_name or "").lower(), _DEFAULT_OBSERVATION_LIMIT
    )
    if len(output) <= limit:
        return output
    head_len = int(limit * 0.7)
    tail_len = int(limit * 0.2)
    head = output[:head_len]
    tail = output[-tail_len:] if tail_len > 0 else ""
    return (
        f"{head}\n\n"
        f"... [observation truncated: {len(output)} chars → ~{limit} chars, "
        f"mid-section omitted. If more detail is needed, refine the call.] ...\n\n"
        f"{tail}"
    )


class ToolCallAgent(BaseAgent):
    """
    Autonomous ReAct agent executing real tool invocations on concrete tool instances.
    Maintains full conversational state synchronized with ExecutionState and executes
    centralized retry policies across attempts.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm: Optional[Any] = None,
        tool_collection: Optional[Any] = None,
        emitter: Optional[EventEmitter] = None,
        workspace_dir: Optional[Union[str, Path]] = None,
        tool_registry: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        active_tools = tool_registry if tool_registry is not None else (tool_collection or ToolCollection())

        base_params = inspect.signature(BaseAgent.__init__).parameters
        base_kwargs: Dict[str, Any] = {}

        if "config" in base_params:
            base_kwargs["config"] = config
        if "tool_registry" in base_params:
            base_kwargs["tool_registry"] = active_tools
        elif "tools" in base_params:
            base_kwargs["tools"] = active_tools

        if "emitter" in base_params and emitter is not None:
            base_kwargs["emitter"] = emitter
        elif "event_emitter" in base_params and emitter is not None:
            base_kwargs["event_emitter"] = emitter

        if "workspace_dir" in base_params and workspace_dir is not None:
            base_kwargs["workspace_dir"] = str(workspace_dir)

        for k, v in kwargs.items():
            if k in base_params and k not in base_kwargs and k != "llm":
                base_kwargs[k] = v

        super().__init__(**base_kwargs)

        self._name: str = getattr(config, "name", "ToolCallAgent") if config else "ToolCallAgent"
        if hasattr(self, "config") and self.config and not getattr(self.config, "name", None):
            self.config.name = self._name

        self.llm = llm
        self.tool_registry = tool_registry
        self.tool_collection = active_tools
        self.tools = active_tools
        self.emitter = emitter or getattr(self, "emitter", None) or EventEmitter()
        self.workspace_dir = Path(workspace_dir).resolve() if workspace_dir else Path.cwd()
        self.system_prompt = getattr(config, "system_prompt", "You are an autonomous specialist agent.")
        self.messages: List[Dict[str, Any]] = []
        self.current_step = 1
        self._final_answer: str = ""
        self.state: Optional[ExecutionState] = None
        # Rolling window of recent single-tool-call signatures used to
        # detect when the LLM is stuck calling the same tool with the
        # same arguments repeatedly. Cleared whenever a step contains
        # multiple tool calls (which breaks the "consecutive" pattern).
        self._recent_call_signatures: List[str] = []

    @property
    def name(self) -> str:
        """Return agent identifier name from config or internal attribute."""
        if hasattr(self, "config") and self.config and getattr(self.config, "name", None):
            return self.config.name
        return getattr(self, "_name", self.__class__.__name__)

    @name.setter
    def name(self, value: str) -> None:
        """Set agent identifier name, synchronizing with config if present."""
        self._name = value
        if hasattr(self, "config") and self.config:
            self.config.name = value

    def set_system_prompt(self, prompt: str) -> None:
        """Configure or update the system prompt controlling agent reasoning."""
        self.system_prompt = prompt

    async def _emit(self, event_type: EventType, step: int, payload: Dict[str, Any]) -> None:
        """Dispatch typed events safely to the registered EventEmitter."""
        if not self.emitter:
            return
        try:
            event_obj = PeldrunEvent(type=event_type, step=step, payload=payload)
            res = self.emitter.emit(event_obj)
            if inspect.isawaitable(res):
                await res
        except Exception:
            try:
                res = self.emitter.emit(event_type, step=step, payload=payload)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

    def _get_tools_schema(self) -> List[Dict[str, Any]]:
        """Extract function calling schemas from active tools container."""
        tools_obj = getattr(self, "tool_registry", None) or getattr(self, "tool_collection", None) or getattr(self, "tools", None)
        if tools_obj is None:
            return []

        if hasattr(tools_obj, "get_openai_schemas") and callable(tools_obj.get_openai_schemas):
            return tools_obj.get_openai_schemas()
        elif hasattr(tools_obj, "to_openai_schemas") and callable(tools_obj.to_openai_schemas):
            return tools_obj.to_openai_schemas()
        elif hasattr(tools_obj, "to_params") and callable(tools_obj.to_params):
            return tools_obj.to_params()
        elif hasattr(tools_obj, "tools") and isinstance(tools_obj.tools, dict):
            schemas: List[Dict[str, Any]] = []
            for t in tools_obj.tools.values():
                if hasattr(t, "to_openai_schema") and callable(t.to_openai_schema):
                    schemas.append(t.to_openai_schema())
                elif hasattr(t, "to_param") and callable(t.to_param):
                    schemas.append(t.to_param())
                elif hasattr(t, "parameters"):
                    schemas.append({
                        "type": "function",
                        "function": {
                            "name": getattr(t, "name", str(t)),
                            "description": getattr(t, "description", ""),
                            "parameters": getattr(t, "parameters", {}),
                        },
                    })
            return schemas
        return []

    def _resolve_tool_instance(self, tool_name: str) -> Optional[Any]:
        """Locate concrete tool instance from registry or collection."""
        tools_obj = getattr(self, "tool_registry", None) or getattr(self, "tool_collection", None) or getattr(self, "tools", None)
        if not tools_obj:
            return None

        if hasattr(tools_obj, "get") and callable(tools_obj.get):
            try:
                t = tools_obj.get(tool_name)
                if t:
                    return t
            except Exception:
                pass

        if hasattr(tools_obj, "get_tool") and callable(tools_obj.get_tool):
            t = tools_obj.get_tool(tool_name)
            if t:
                return t

        if hasattr(tools_obj, "tool_map") and isinstance(tools_obj.tool_map, dict):
            if tool_name in tools_obj.tool_map:
                return tools_obj.tool_map[tool_name]

        if hasattr(tools_obj, "tools") and isinstance(tools_obj.tools, dict):
            if tool_name in tools_obj.tools:
                return tools_obj.tools[tool_name]

        if hasattr(tools_obj, "tools") and isinstance(tools_obj.tools, (list, tuple)):
            for t in tools_obj.tools:
                if getattr(t, "name", None) == tool_name:
                    return t

        return None

    def _get_tool_policy(self, tool_name: str) -> ToolExecutionPolicy:
        """Resolve authoritative ToolExecutionPolicy for target tool."""
        tools_obj = getattr(self, "tool_registry", None) or getattr(self, "tool_collection", None)
        if tools_obj and hasattr(tools_obj, "get_tool_policy") and callable(tools_obj.get_tool_policy):
            return tools_obj.get_tool_policy(tool_name)

        tool_inst = self._resolve_tool_instance(tool_name)
        if tool_inst is not None:
            if hasattr(tool_inst, "get_execution_policy") and callable(tool_inst.get_execution_policy):
                return tool_inst.get_execution_policy()
            if hasattr(tool_inst, "execution_policy") and isinstance(tool_inst.execution_policy, ToolExecutionPolicy):
                return tool_inst.execution_policy

        return ToolExecutionPolicy(max_attempts=1, retryable=False, side_effects=True)

    def _sync_messages_from_state(self, state: ExecutionState) -> None:
        """Synchronize in-memory message history from ExecutionState messages."""
        if state.messages:
            self.messages = [msg.to_llm_dict() for msg in state.messages]
        else:
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": state.task_prompt or "Start task."},
            ]
            state.add_message(role=MessageRole.SYSTEM, content=self.system_prompt)
            state.add_message(role=MessageRole.USER, content=state.task_prompt or "Start task.")

    async def think(self, step: int) -> LLMResponse:
        """
        Execute cognitive reasoning phase with decoupled transient LLM retries.
        Explicitly propagates reasoning_effort from provider configuration to execution loop.

        Real token-by-token streaming
        -----------------------------
        The LLM provider (``AsyncLLMClient.chat_completion``) exposes
        ``on_chunk`` and ``on_thought_chunk`` callbacks that fire once per
        generated token. This method wires those callbacks so that each
        delta is emitted as ``EventType.THOUGHT`` with ``is_delta=True``,
        letting the frontend render the reasoning **live** inside
        ``ChatTimeline`` (the same experience as Chat mode's
        ``finalResult`` bubble).

        Buffering strategy
        ------------------
        Raw per-token SSE events would produce ~500 events per turn, each
        persisted to ``run_store`` (SQLite) and forwarded over SSE —
        significant overhead for what is visually indistinguishable from a
        coarser cadence. We therefore batch deltas into a small buffer and
        flush when EITHER:

          * the buffer accumulates ``_STREAM_FLUSH_CHARS`` characters, OR
          * ``_STREAM_FLUSH_INTERVAL`` seconds have elapsed since the last
            flush.

        At typical LLM speeds (~30 tok/s, ~4 chars/token) this yields
        ~20-40 flushes per turn — smooth visually, an order of magnitude
        cheaper than raw token events.

        Non-streaming fallback
        ----------------------
        ``AsyncLLMClient`` may fall back to a non-streaming completion
        (SDK init failure, empty stream). In that case ``on_chunk`` is
        never invoked. We detect this via ``_streamed_any`` and emit the
        full content as a single non-delta thought so the timeline still
        reflects the reasoning.
        """
        tools_schema = self._get_tools_schema()

        if not self.messages or not any(m.get("role") in ("user", "system") for m in self.messages):
            prompt_text = (self.state.task_prompt if self.state else "") or "Execute assigned task."
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt_text},
            ]
            if self.state and not self.state.messages:
                self.state.add_message(role=MessageRole.SYSTEM, content=self.system_prompt)
                self.state.add_message(role=MessageRole.USER, content=prompt_text)

        effort_val = getattr(getattr(self.llm, "config", None), "reasoning_effort", None)
        extra_call_params: Dict[str, Any] = {}
        if effort_val:
            extra_call_params["reasoning_effort"] = effort_val

        # ──────────────────────────────────────────────────────────────
        # Streaming buffer state (local to this think() invocation)
        #
        # [FIX-STREAMING-RUNTIME] Read tuning values from the runtime
        # settings store, NOT from os.environ directly. Otherwise values
        # edited from the Settings UI (`/settings/runtime`) would be
        # silently ignored — the UI persists to runtime_settings.json,
        # but the agent would only honor env vars.
        #
        # Resolution tier (handled inside runtime_settings.resolve):
        #     runtime_settings.json  >  os.environ  >  hardcoded default
        #
        # Read fresh on every think() call, so UI changes apply on the
        # very next LLM step without a server restart.
        #
        # A broken settings service (e.g. during unit tests where omweb
        # is not importable) falls back to os.environ, then to hardcoded
        # defaults, so the agent never loses the streaming optimization.
        # ──────────────────────────────────────────────────────────────
        _STREAM_FLUSH_INTERVAL: float
        _STREAM_FLUSH_CHARS: int

        try:
            # Prefer the runtime settings store (respects UI edits).
            from omweb.services.runtime_settings import (
                resolve as _rt_resolve,
            )

            _rt_interval = _rt_resolve("streaming", "flush_interval_seconds")
            _rt_chars = _rt_resolve("streaming", "flush_chars")
            _STREAM_FLUSH_INTERVAL = float(_rt_interval or 0.05)
            _STREAM_FLUSH_CHARS = int(_rt_chars or 20)
        except Exception:
            # Fallback path — omweb not importable, or settings service
            # is broken. Preserve the historical behavior so no run is
            # ever blocked by a settings-layer issue.
            import os as _os_env

            try:
                _STREAM_FLUSH_INTERVAL = float(
                    _os_env.environ.get(
                        "PELDRUN_STREAM_FLUSH_INTERVAL", "0.05"
                    )
                )
            except (TypeError, ValueError):
                _STREAM_FLUSH_INTERVAL = 0.05
            try:
                _STREAM_FLUSH_CHARS = int(
                    _os_env.environ.get("PELDRUN_STREAM_FLUSH_CHARS", "20")
                )
            except (TypeError, ValueError):
                _STREAM_FLUSH_CHARS = 20

        # Clamp to sane bounds — protects against pathological values
        # (0 would break batching; 10000 would make the stream choppy).
        _STREAM_FLUSH_INTERVAL = max(0.01, min(1.0, _STREAM_FLUSH_INTERVAL))
        _STREAM_FLUSH_CHARS = max(5, min(200, _STREAM_FLUSH_CHARS))

        _stream_buffer: List[str] = []
        _streamed_any = False
        _last_flush = time.monotonic()

        async def _flush_deltas(force: bool = False) -> None:
            nonlocal _last_flush, _streamed_any
            if not _stream_buffer:
                return
            now = time.monotonic()
            pending_len = sum(len(x) for x in _stream_buffer)
            should_flush = (
                force
                or pending_len >= _STREAM_FLUSH_CHARS
                or (now - _last_flush) >= _STREAM_FLUSH_INTERVAL
            )
            if not should_flush:
                return
            payload_text = "".join(_stream_buffer)
            _stream_buffer.clear()
            _last_flush = now
            _streamed_any = True
            try:
                await self._emit(
                    EventType.THOUGHT,
                    step=step,
                    payload={
                        "thought": payload_text,
                        "content": payload_text,
                        "is_delta": True,
                    },
                )
            except Exception as flush_err:
                # A failed flush must never abort the generation loop;
                # the user sees a slightly larger next flush instead.
                logger.debug(
                    "Streaming flush failed for step %d: %s", step, flush_err
                )

        async def _on_chunk(delta_text: str) -> None:
            """Content-token callback from AsyncLLMClient."""
            if not delta_text:
                return
            _stream_buffer.append(delta_text)
            await _flush_deltas()

        async def _on_thought_chunk(delta_text: str) -> None:
            """Reasoning-token callback from AsyncLLMClient (e.g. R1 models)."""
            if not delta_text:
                return
            _stream_buffer.append(delta_text)
            await _flush_deltas()

        async def _call_llm(msgs: List[Dict[str, Any]]) -> LLMResponse:
            if hasattr(self.llm, "generate"):
                return await self.llm.generate(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    temperature=0.2,
                    on_chunk=_on_chunk,
                    on_thought_chunk=_on_thought_chunk,
                    **extra_call_params,
                )
            elif hasattr(self.llm, "chat_completion"):
                return await self.llm.chat_completion(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    temperature=0.2,
                    on_chunk=_on_chunk,
                    on_thought_chunk=_on_thought_chunk,
                    **extra_call_params,
                )
            elif hasattr(self.llm, "chat_complete"):
                return await self.llm.chat_complete(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    temperature=0.2,
                    on_chunk=_on_chunk,
                    on_thought_chunk=_on_thought_chunk,
                    **extra_call_params,
                )
            else:
                raise AttributeError("LLM client does not provide generate or chat_completion interface.")

        max_llm_attempts = 3
        last_llm_exc: Optional[Exception] = None

        for llm_attempt in range(1, max_llm_attempts + 1):
            try:
                response = await _call_llm(self.messages)
                break
            except Exception as exc:
                last_llm_exc = exc
                err_str = str(exc).lower()

                # [FIX-OOM-FAST-FAIL] Out-of-memory is never retryable.
                # A second attempt consumes additional memory before the
                # original allocation is freed, making OOM strictly
                # worse. Propagate immediately with a clear message.
                if _is_oom_error(exc):
                    logger.error(
                        "LLM OOM detected on attempt %d (%s). Raising "
                        "immediately without retry.",
                        llm_attempt,
                        exc,
                    )
                    raise

                if "terminated" in err_str or "context" in err_str or "400" in err_str or "maximum context" in err_str:
                    # [FIX-AGGRESSIVE-COMPACTION] Previous compaction kept
                    # 6 trailing messages (up to 3 tool observations). For
                    # a web_search loop that means 3 × 4 KB = 12 KB of
                    # observations survive — often still too much to fit.
                    # Keep only 3 trailing messages and truncate any tool
                    # observation inside the window to 800 chars.
                    logger.warning(
                        "Provider rejected full context (%s). "
                        "Aggressively compacting on attempt %d.",
                        exc,
                        llm_attempt,
                    )
                    compact_msgs = list(self.messages[:2]) + list(self.messages[-3:])
                    while compact_msgs and len(compact_msgs) > 2 and compact_msgs[2].get("role") == "tool":
                        compact_msgs.pop(2)

                    for cmsg in compact_msgs:
                        if cmsg.get("role") == "tool":
                            c = cmsg.get("content", "")
                            if isinstance(c, str) and len(c) > 800:
                                cmsg["content"] = c[:640] + "... [truncated on compaction]"

                    if not compact_msgs or not any(m.get("role") == "user" for m in compact_msgs):
                        compact_msgs = [
                            {"role": "system", "content": self.system_prompt},
                            {"role": "user", "content": (self.state.task_prompt if self.state else "") or "Proceed with task."},
                        ]
                    try:
                        response = await _call_llm(compact_msgs)
                        break
                    except Exception as compact_exc:
                        last_llm_exc = compact_exc
                        if _is_oom_error(compact_exc):
                            logger.error(
                                "LLM OOM detected during compaction on "
                                "attempt %d (%s). Raising immediately.",
                                llm_attempt,
                                compact_exc,
                            )
                            raise

                is_transient = (
                    "429" in err_str
                    or "rate limit" in err_str
                    or "timeout" in err_str
                    or "502" in err_str
                    or "503" in err_str
                    or "connection" in err_str
                )
                if is_transient and llm_attempt < max_llm_attempts:
                    delay = 1.5 * llm_attempt
                    logger.warning("Transient LLM error on attempt %d (%s). Retrying in %.1fs...", llm_attempt, exc, delay)
                    await asyncio.sleep(delay)
                    continue

                raise last_llm_exc

        # ──────────────────────────────────────────────────────────────
        # Final flush: push any residual buffered text (incomplete last
        # word / sentence) so the timeline reflects the full generation.
        # ──────────────────────────────────────────────────────────────
        await _flush_deltas(force=True)

        thought_val = response.reasoning or response.thought or response.content or ""

        # ──────────────────────────────────────────────────────────────
        # Non-streaming fallback:
        # If the provider never invoked on_chunk (e.g. AsyncLLMClient
        # fell back to stream=False after a transport error), emit the
        # full content as a single non-delta thought so the timeline is
        # not empty.
        # ──────────────────────────────────────────────────────────────
        if thought_val and not _streamed_any:
            await self._emit(
                EventType.THOUGHT,
                step=step,
                payload={
                    "thought": thought_val,
                    "content": thought_val,
                    "is_delta": False,
                },
            )

        return response

    async def act(self, step: int, tool_calls: List[ToolCall]) -> List[str]:
        observations: List[str] = []

        for call in tool_calls:
            tool_name = call.name
            call_id = call.id or f"call_{uuid.uuid4().hex[:12]}"
            call.id = call_id

            args = call.arguments if isinstance(call.arguments, dict) else {}
            if isinstance(call.arguments, str):
                try:
                    args = json.loads(call.arguments)
                except Exception:
                    args = {"raw": call.arguments}

            is_human_tool = tool_name in ("ask_human", "human_input")
            if is_human_tool:
                if not args.get("request_id"):
                    args["request_id"] = f"req_{uuid.uuid4().hex[:12]}"
                if self.state is not None:
                    self.state.mark_waiting_for_input(reason="Agent invoked interactive human tool")

            policy = self._get_tool_policy(tool_name)
            tool_inst = self._resolve_tool_instance(tool_name)

            final_output_str = ""
            final_attempt = 1
            is_success = False

            for attempt in range(1, policy.max_attempts + 1):
                final_attempt = attempt

                await self._emit(
                    EventType.TOOL_CALL,
                    step=step,
                    payload={
                        "tool_name": tool_name,
                        "arguments": args,
                        "tool_call_id": call_id,
                        "attempt": attempt,
                    },
                )

                output_str = ""
                has_error = False
                attempt_exc: Optional[Exception] = None

                if tool_inst is not None:
                    timeout_limit = (
                        float(args.get("timeout_seconds") or 600.0)
                        if is_human_tool
                        else policy.timeout_seconds
                    )

                    try:
                        res: Any

                        async def _execute_underlying() -> Any:
                            if hasattr(tool_inst, "aexecute") and callable(tool_inst.aexecute):
                                return await tool_inst.aexecute(**args)
                            elif hasattr(tool_inst, "_arun") and callable(tool_inst._arun):
                                return await tool_inst._arun(**args)
                            elif hasattr(tool_inst, "execute") and callable(tool_inst.execute):
                                fn = tool_inst.execute
                                if inspect.iscoroutinefunction(fn):
                                    return await fn(**args)
                                else:
                                    r = fn(**args)
                                    return (await r) if inspect.isawaitable(r) else r
                            else:
                                return ToolResult(
                                    output=f"Tool '{tool_name}' has no executable entrypoint.",
                                    exit_code=1,
                                    is_error=True,
                                )

                        if timeout_limit and timeout_limit > 0:
                            res = await asyncio.wait_for(_execute_underlying(), timeout=timeout_limit)
                        else:
                            res = await _execute_underlying()

                        if isinstance(res, ToolResult):
                            if res.is_error:
                                has_error = True
                                output_str = res.error or str(res.output) or f"Error executing tool '{tool_name}'"
                            else:
                                output_str = str(res.output) if res.output is not None else ""
                        elif isinstance(res, dict) and "output" in res:
                            output_str = str(res.get("output", ""))
                            has_error = bool(res.get("is_error", False))
                        else:
                            output_str = str(res)

                    except (asyncio.TimeoutError, TimeoutError) as t_err:
                        attempt_exc = t_err
                        has_error = True
                        output_str = f"Tool '{tool_name}' timed out after {timeout_limit}s."

                    except asyncio.CancelledError as cancel_err:
                        if is_human_tool and self.state is not None:
                            self.state.mark_failed("Human interaction cancelled.", error_code="CANCELLED")
                        raise cancel_err

                    except Exception as ex:
                        attempt_exc = ex
                        has_error = True
                        output_str = f"Error executing tool '{tool_name}': {str(ex)}"
                else:
                    has_error = True
                    output_str = f"Error: Tool '{tool_name}' not found in active collection."

                if is_human_tool and self.state is not None:
                    self.state.mark_running(reason="Human interaction resolved")

                final_output_str = output_str

                if self.state is not None:
                    self.state.record_tool_execution(
                        tool_name=tool_name,
                        arguments=args,
                        output=output_str,
                        exit_code=1 if has_error else 0,
                        is_error=has_error,
                        tool_call_id=call_id,
                        attempt=attempt,
                    )

                if not has_error:
                    is_success = True
                    break

                can_retry = policy.is_retry_permitted(current_attempt=attempt, error=attempt_exc)
                if can_retry:
                    delay = policy.compute_delay(attempt + 1)
                    logger.warning(
                        "Tool '%s' failed on attempt %d/%d (%s). Retrying in %.1fs...",
                        tool_name,
                        attempt,
                        policy.max_attempts,
                        output_str[:80],
                        delay,
                    )

                    await self._emit(
                        EventType.TOOL_RETRY,
                        step=step,
                        payload={
                            "tool_name": tool_name,
                            "tool_call_id": call_id,
                            "attempt": attempt + 1,
                            "max_attempts": policy.max_attempts,
                            "error": output_str,
                            "delay_seconds": delay,
                        },
                    )

                    if self.state is not None and self.state.status == ExecutionStatus.RUNNING:
                        self.state.mark_retrying(reason=f"Retrying tool '{tool_name}' (attempt {attempt + 1})")

                    if delay > 0:
                        await asyncio.sleep(delay)

                    if self.state is not None and self.state.status == ExecutionStatus.RETRYING:
                        self.state.mark_running(reason=f"Resumed execution for tool retry (attempt {attempt + 1})")
                else:
                    logger.info("Tool '%s' failure on attempt %d is non-retryable. Concluding attempts.", tool_name, attempt)
                    break

            await self._emit(
                EventType.OBSERVATION,
                step=step,
                payload={
                    "tool_name": tool_name,
                    "output": final_output_str,
                    "tool_call_id": call_id,
                    "attempt": final_attempt,
                    "is_error": not is_success,
                },
            )

            # [FIX-OBSERVATION-TRUNCATION] Truncate large observations
            # before storing in the conversation history. The full output
            # is already in ``state.tool_history`` (via record_tool_execution
            # above) and in the SSE event above, so nothing is lost from
            # the audit / UI view. Only what the LLM re-sends each step is
            # capped, preventing a single web_search from consuming 5+ KB
            # of context per subsequent step.
            _truncated_observation = _truncate_observation(tool_name, final_output_str)

            self.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": tool_name,
                "content": _truncated_observation,
            })

            if self.state is not None:
                self.state.add_message(
                    role=MessageRole.TOOL,
                    content=_truncated_observation,
                    name=tool_name,
                    tool_call_id=call_id,
                )

            observations.append(final_output_str)

        return observations

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        self.state = state
        self.emitter = emitter
        self.current_step = state.current_step

        self._sync_messages_from_state(state)

        response = await self.think(step=self.current_step)

        # [FIX-LOOP-DETECTION] Detect when the LLM is stuck calling the
        # same tool with identical arguments across consecutive steps.
        # A single-tool step allows an unambiguous comparison. Multi-tool
        # steps reset the tracker because their ordering may legitimately
        # vary without indicating a loop.
        if response.tool_calls:
            if len(response.tool_calls) == 1:
                tc = response.tool_calls[0]
                try:
                    if isinstance(tc.arguments, dict):
                        args_canon = json.dumps(
                            tc.arguments, sort_keys=True, ensure_ascii=False
                        )
                    else:
                        args_canon = str(tc.arguments)
                except Exception:
                    args_canon = str(tc.arguments)
                signature = f"{tc.name}::{args_canon}"

                self._recent_call_signatures.append(signature)
                if len(self._recent_call_signatures) > _LOOP_DETECTION_WINDOW:
                    self._recent_call_signatures = self._recent_call_signatures[
                        -_LOOP_DETECTION_WINDOW:
                    ]

                if (
                    len(self._recent_call_signatures) >= _LOOP_DETECTION_WINDOW
                    and len(set(self._recent_call_signatures)) == 1
                ):
                    loop_msg = (
                        f"Loop detected: tool '{tc.name}' was invoked "
                        f"{_LOOP_DETECTION_WINDOW} times in a row with "
                        f"identical arguments. The agent appears stuck. "
                        f"Forcing termination."
                    )
                    logger.error(loop_msg)
                    raise RuntimeError(loop_msg)
            else:
                # Multi-tool step — break the consecutive-signature chain.
                self._recent_call_signatures.clear()

        if not response.tool_calls:
            self._final_answer = response.content or response.thought or response.reasoning or "Task completed."
            state.final_output = self._final_answer
            return True

        for tc in response.tool_calls:
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:12]}"

        tool_calls_payload = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False) if isinstance(tc.arguments, dict) else str(tc.arguments),
                },
            }
            for tc in response.tool_calls
        ]
        self.messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": tool_calls_payload,
        })
        state.add_message(
            role=MessageRole.ASSISTANT,
            content=response.content or "",
            tool_calls=tool_calls_payload,
        )

        observations = await self.act(step=self.current_step, tool_calls=response.tool_calls)

        has_terminated = any(tc.name.lower() in ("terminate", "done") for tc in response.tool_calls)
        if has_terminated:
            self._final_answer = response.content or (observations[-1] if observations else "Task completed via termination tool.")
            state.final_output = self._final_answer
            return True

        return False

    async def _astep(self) -> bool:
        if self.state is None:
            self.state = ExecutionState(task_prompt="Default task", workspace_root=str(self.workspace_dir))
        return not (await self.step(state=self.state, emitter=self.emitter))

    async def run_task(self, prompt: str, max_steps: int = 30) -> str:
        from peldrun.engine.runner import AgentRunner, RunnerConfig

        runner_config = RunnerConfig(max_steps=max_steps)
        runner = AgentRunner(agent=self, emitter=self.emitter, config=runner_config)
        state = await runner.run(task_prompt=prompt, workspace_root=str(self.workspace_dir))
        return state.final_output or self._final_answer or "Task execution finished."

    async def arun(self, task: str = "", max_steps: Optional[int] = None, **kwargs: Any) -> Any:
        prompt_val = task or kwargs.get("prompt", "")
        limit_val = max_steps or getattr(self.config, "max_steps", 30)
        return await self.run_task(prompt=prompt_val, max_steps=limit_val)


__all__ = ["ToolCallAgent"]
