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
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Union
import uuid

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.agents.message_guard import rebuild_for_llm
from peldrun.agents.termination import (
    apply_decision,
    evaluate as evaluate_termination,
)
from peldrun.context.projection import (
    ContextProjection,
    ProjectionConfig,
)
from peldrun.engine.state import ExecutionState, ExecutionStatus, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.llm.client import LLMResponse, ToolCall
from peldrun.tools.base import ToolResult
from peldrun.tools.collection import ToolCollection
from peldrun.tools.contract import ToolExecutionPolicy
from peldrun.tools.execution_manager import ToolExecutionContextImpl
from peldrun.tools.execution_result import ToolExecutionResult

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


# ---------------------------------------------------------------------------
# Tool Output Streaming feature flag
# ---------------------------------------------------------------------------
def _is_tool_output_streaming_enabled() -> bool:
    """
    Return True when the Tool Output Streaming feature is enabled.

    Controlled by the ``PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING`` env var.
    Default is False: the feature is opt-in and must be explicitly
    enabled by an operator.

    This is the emergency kill-switch: turning the env var off
    disables ALL streaming at the next agent construction without
    touching code or restarting other subsystems. Only values that
    unambiguously signal intent are accepted as truthy — the exact
    strings "1", "true", "yes", and "on", case-insensitive. Everything
    else (including unset) is False.
    """
    raw = os.environ.get(
        "PELDRUN_ENABLE_TOOL_OUTPUT_STREAMING", ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


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
        # [FIX-NO-TOOL-NOT-DONE] Counts consecutive steps where the model
        # produced text WITHOUT calling any tool. Used by ``step()`` to
        # prevent infinite text-only loops while still allowing reasoning
        # models (Nemotron, R1, QwQ, o1-style) to produce intermediate
        # text mid-task without being misclassified as "task complete".
        self._consecutive_no_tool_steps: int = 0
        # [TOOL-OUTPUT-STREAMING] Feature flag read once at construction
        # time. Reading once (instead of per-call) makes an individual
        # run's behavior consistent even if an operator edits the env
        # var mid-execution. The flag is a deployment concern, NOT
        # per-run state, so ``_reset_run_state`` deliberately does not
        # touch it.
        self._streaming_enabled: bool = _is_tool_output_streaming_enabled()

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
        """
        Synchronize the in-memory message history from ExecutionState.

        [B1 FIX] Every code path now routes through
        ``message_guard.rebuild_for_llm``, which guarantees that the
        system prompt is the FIRST message of the assembled LLM
        payload. The pre-fix branch that simply copied
        ``state.messages`` verbatim silently DROPPED the system
        prompt whenever the state was non-empty, which meant the
        model ran without any operational instructions from step 2
        onward.

        Both branches also produce identical message shapes now —
        ``[system, user, ...]`` — so downstream code that inspects
        ``self.messages`` (diagnostics, replay, projection) never
        has to special-case "did the state have messages or not?".

        The guard is idempotent: calling this method twice in a row
        produces the same message list, and calling it after the
        state has been mutated with new tool observations still
        keeps the system prompt at index 0.
        """
        if state.messages:
            # The state already carries a conversation. Route its
            # messages through the guard so the system prompt is
            # prepended (or reinstated) even when the historical
            # message list does not begin with one.
            self.messages = rebuild_for_llm(
                [msg.to_llm_dict() for msg in state.messages],
                self.system_prompt,
            )
        else:
            # Initial synchronization: seed the state with the
            # task, then route through the same guard so both
            # branches converge on the identical shape.
            state.add_message(role=MessageRole.SYSTEM, content=self.system_prompt)
            state.add_message(role=MessageRole.USER, content=state.task_prompt or "Start task.")
            self.messages = rebuild_for_llm(
                [msg.to_llm_dict() for msg in state.messages],
                self.system_prompt,
            )

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

        # [D FIX — B7] Build the LLM-facing message list from the
        # authoritative ExecutionState via ContextProjection.
        #
        # Pre-Phase-D behavior: ``think()`` used ``self.messages``
        # directly. On a context-overflow error, it built a local
        # ``compact_msgs`` list, retried with it, and then DISCARDED
        # the result. On the next ``think()`` call the full
        # (uncompacted) history was reloaded, and the same overflow
        # recurred. Every step paid the 400-error + compact-retry
        # penalty; long runs never converged on a stable view.
        #
        # Post-fix: ``ContextProjection.build`` reads state.messages
        # (the audit trail — never mutated), estimates token cost,
        # and returns a deterministic, budget-aware view. When the
        # conversation exceeds the budget, the SAME compacted view
        # is produced on every subsequent think() call until the
        # conversation shrinks. Compaction is no longer a one-shot
        # retry artifact.
        #
        # The budget resolves in this order:
        #   1. ``budget_override`` (none here)
        #   2. ``state.metadata["context_budget"]["effective_limit"]``
        #      (set by CoreRuntimeExecutor)
        #   3. ``ProjectionConfig`` fallback:
        #      fallback_max_tokens - fallback_max_output_tokens
        #      = 8192 - 2048 = 6144 tokens (~24 KB of text)
        #
        # The system prompt is enforced at the projection boundary
        # (via message_guard internally), so this call also satisfies
        # the B1 invariant without a separate rebuild_for_llm step
        # for the LLM input path.
        projection = ContextProjection()
        projection_result = projection.build(
            self.state, self.system_prompt
        )
        llm_messages = projection_result.messages

        if projection_result.stats.was_compacted:
            logger.info(
                "Projection compacted before LLM call: %s",
                projection_result.stats.summary(),
            )
        else:
            logger.debug(
                "Projection: %s",
                projection_result.stats.summary(),
            )

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

        # Two independent buffers: content (user-visible output) and
        # thought (internal reasoning). Mixing them into a single
        # THOUGHT stream made the answer appear only at the very end of
        # agent-mode runs, because the frontend renders CHUNK events in
        # the main response bubble and THOUGHT events in the reasoning
        # panel.
        _content_buffer: List[str] = []
        _thought_buffer: List[str] = []
        _streamed_content_any = False
        _streamed_thought_any = False
        _last_flush = time.monotonic()
        # [E.2 FIX — B8] Per-attempt correlation id. Every CHUNK and
        # THOUGHT event carries this so the frontend can distinguish
        # text from different retry attempts. Reassigned at the top
        # of each retry loop iteration; the initial value is only a
        # placeholder for the first attempt.
        _attempt_id: str = uuid.uuid4().hex[:12]

        def _should_flush(buf: List[str], force: bool) -> bool:
            if not buf:
                return False
            now = time.monotonic()
            pending_len = sum(len(x) for x in buf)
            return (
                force
                or pending_len >= _STREAM_FLUSH_CHARS
                or (now - _last_flush) >= _STREAM_FLUSH_INTERVAL
            )

        async def _flush_content(force: bool = False) -> None:
            nonlocal _last_flush, _streamed_content_any, _attempt_id
            if not _should_flush(_content_buffer, force):
                return
            payload_text = "".join(_content_buffer)
            _content_buffer.clear()
            _last_flush = time.monotonic()
            _streamed_content_any = True
            try:
                await self._emit(
                    EventType.CHUNK,
                    step=step,
                    payload={
                        "delta": payload_text,
                        "text": payload_text,
                        "content": payload_text,
                        "is_delta": True,
                        # [E.2] Attempt correlation — the frontend
                        # uses this to discard stale attempt text
                        # when a RESET event arrives for a newer
                        # attempt_id.
                        "attempt_id": _attempt_id,
                    },
                )
            except Exception as flush_err:
                logger.debug(
                    "Content flush failed for step %d: %s", step, flush_err
                )

        async def _flush_thought(force: bool = False) -> None:
            nonlocal _last_flush, _streamed_thought_any, _attempt_id
            if not _should_flush(_thought_buffer, force):
                return
            payload_text = "".join(_thought_buffer)
            _thought_buffer.clear()
            _last_flush = time.monotonic()
            _streamed_thought_any = True
            try:
                await self._emit(
                    EventType.THOUGHT,
                    step=step,
                    payload={
                        "thought": payload_text,
                        "content": payload_text,
                        "is_delta": True,
                        # [E.2] Attempt correlation.
                        "attempt_id": _attempt_id,
                    },
                )
            except Exception as flush_err:
                logger.debug(
                    "Thought flush failed for step %d: %s", step, flush_err
                )

        async def _flush_all(force: bool = False) -> None:
            await _flush_content(force=force)
            await _flush_thought(force=force)

        async def _emit_reset_events(reason: str) -> None:
            """Emit RESET markers on both streaming channels.

            [E.2 FIX — B8] Called before each retry attempt and before
            a final raise. The ``reset: True`` flag tells the frontend
            to discard whatever it accumulated for this channel since
            the last non-reset event. Without this, retrying after a
            mid-stream failure would APPEND the new attempt's tokens
            on top of the failed attempt's visible text — the exact
            duplication B8 documented.

            The ``attempt_id`` field still provides correlation when
            the frontend does not (yet) honor ``reset``: the frontend
            can ignore events whose ``attempt_id`` no longer matches
            the most recent one it has seen. Both mechanisms are
            independent and complementary.

            Emission failures are swallowed. A broken emitter must
            never prevent the retry or the final raise from running.
            """
            try:
                await self._emit(
                    EventType.CHUNK,
                    step=step,
                    payload={
                        "reset": True,
                        "attempt_id": _attempt_id,
                        "reason": reason,
                    },
                )
            except Exception:
                pass
            try:
                await self._emit(
                    EventType.THOUGHT,
                    step=step,
                    payload={
                        "reset": True,
                        "attempt_id": _attempt_id,
                        "reason": reason,
                    },
                )
            except Exception:
                pass

        async def _on_chunk(delta_text: str) -> None:
            """Content-token callback — user-visible output.

            Emits every delta immediately (force=True) so the frontend
            can render word-by-word streaming, matching the pre-M8
            behavior where each delta became a visible event. Rate
            limiting via ``_should_flush`` is bypassed here because the
            upstream provider ALREADY throttles deltas to a natural
            token rate (~30/s), and the SSE channel can easily handle
            that volume. Batching only helped for pathological
            providers that send huge multi-KB chunks; those are rare
            and the user's stated preference is per-token rendering.
            """
            if not delta_text:
                return
            _content_buffer.append(delta_text)
            await _flush_content(force=True)

        async def _on_thought_chunk(delta_text: str) -> None:
            """Reasoning-token callback — internal chain-of-thought.

            Same force=True rationale as ``_on_chunk``: the user wants
            to see reasoning unfold in real time inside the ``🧠 Agent
            Reasoning`` panel of the step timeline.
            """
            if not delta_text:
                return
            _thought_buffer.append(delta_text)
            await _flush_thought(force=True)

        async def _call_llm(msgs: List[Dict[str, Any]]) -> LLMResponse:
            """
            Invoke the underlying LLM client.

            [B2 FIX] The ``temperature`` parameter is deliberately NOT
            passed from this layer. Sampling temperature is part of
            the run's ``GenerationConfig`` (built from
            ``AgentSpec.temperature`` — which itself resolves from
            ``active_llm["temperature"]``, and that in turn comes from
            ``models_budget.json`` / ``config.toml``). Passing a
            hardcoded value here — as the pre-fix code did, with
            ``temperature=0.2`` — silently overrode the user's
            per-model configuration on every step.

            When the bridge (``_ToolCallAgentBridge``) is in use, the
            ``kwargs.get("temperature")`` check inside
            ``_build_request`` correctly sees ``None`` and leaves the
            base request's ``generation.temperature`` untouched.
            When a legacy client is in use, the same behavior applies:
            the client falls back to its own configuration instead of
            receiving a foreign override.

            ``on_chunk`` / ``on_thought_chunk`` remain wired through
            because they are per-call streaming callbacks, not
            model configuration.
            """
            if hasattr(self.llm, "generate"):
                return await self.llm.generate(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    on_chunk=_on_chunk,
                    on_thought_chunk=_on_thought_chunk,
                    **extra_call_params,
                )
            elif hasattr(self.llm, "chat_completion"):
                return await self.llm.chat_completion(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    on_chunk=_on_chunk,
                    on_thought_chunk=_on_thought_chunk,
                    **extra_call_params,
                )
            elif hasattr(self.llm, "chat_complete"):
                return await self.llm.chat_complete(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    on_chunk=_on_chunk,
                    on_thought_chunk=_on_thought_chunk,
                    **extra_call_params,
                )
            else:
                raise AttributeError("LLM client does not provide generate or chat_completion interface.")

        max_llm_attempts = 3
        last_llm_exc: Optional[Exception] = None

        # [D FIX — B7 continued] ``llm_messages`` is the projection
        # output built above. Retries update it by rebuilding the
        # projection with a tighter ProjectionConfig — never by
        # manipulating the message list by hand. This keeps
        # compaction deterministic across retries and across steps.
        for llm_attempt in range(1, max_llm_attempts + 1):
            # [E.2 FIX — B8] Per-attempt streaming state.
            #
            # A fresh ``_attempt_id`` is generated for every retry.
            # Every CHUNK and THOUGHT event emitted during THIS
            # attempt carries that id, so the frontend can
            # distinguish text from this attempt vs. a previous one.
            #
            # The buffers are cleared so the fallback logic at the
            # bottom of think() correctly reflects whether THIS
            # attempt streamed. Without this reset,
            # ``_streamed_content_any`` would remain True from a
            # previous attempt and suppress the non-streaming
            # fallback CHUNK that a compact-retry path needs.
            _attempt_id = uuid.uuid4().hex[:12]
            _is_retry = llm_attempt > 1

            _content_buffer.clear()
            _thought_buffer.clear()
            _streamed_content_any = False
            _streamed_thought_any = False

            # Before a retry, discard whatever partial content the
            # frontend displayed for the failed attempt. Without this,
            # the retry's text would append to the failed attempt's
            # visible text — the duplication B8 documented.
            if _is_retry:
                logger.info(
                    "Retrying LLM call (attempt %d/%d). Emitting "
                    "RESET before new attempt_id=%s.",
                    llm_attempt,
                    max_llm_attempts,
                    _attempt_id,
                )
                await _emit_reset_events(reason="retry")

            try:
                response = await _call_llm(llm_messages)

                # [E.2 FIX — E.1 signal surface]
                #
                # An ``incomplete`` finish_reason means the stream
                # ended without a terminal marker (see E.1 in
                # ``llm/client.py``). The partial content the provider
                # generated is already visible on the UI (via
                # ``_on_chunk`` -> ``_flush_content``). Accepting it
                # as the final response would silently complete the
                # task on truncated output. Convert to a retryable
                # failure so the retry loop discards the partial text
                # and re-streams cleanly.
                if getattr(response, "finish_reason", None) == "incomplete":
                    raise RuntimeError(
                        f"INCOMPLETE_STREAM: LLM returned "
                        f"finish_reason='incomplete' on attempt "
                        f"{llm_attempt}/{max_llm_attempts}. The "
                        f"provider stream ended without a terminal "
                        f"marker; the partial output cannot be "
                        f"trusted as the final response."
                    )

                break
            except Exception as exc:
                last_llm_exc = exc
                err_str = str(exc).lower()

                # [FIX-OOM-FAST-FAIL] Out-of-memory is never
                # retryable. A second attempt consumes additional
                # memory before the original allocation is freed,
                # making OOM strictly worse. Propagate immediately
                # with a clear message.
                if _is_oom_error(exc):
                    logger.error(
                        "LLM OOM detected on attempt %d (%s). Raising "
                        "immediately without retry.",
                        llm_attempt,
                        exc,
                    )
                    # [E.2] Clear the UI before raising so the user
                    # does not see stale partial content from a
                    # failed attempt.
                    await _emit_reset_events(reason="fatal_oom")
                    raise

                # [E.2] INCOMPLETE_STREAM is a distinct signal from
                # the E.1 fix in llm/client.py. It means the provider
                # sent partial content but the connection dropped
                # before the terminal marker. It is retryable — a
                # transient network stall is often the cause — and
                # requires a RESET before the retry so the UI does
                # not accumulate partial output across attempts.
                is_incomplete_stream = (
                    "incomplete_stream" in err_str
                    or "incomplete stream" in err_str
                )
                if is_incomplete_stream:
                    logger.warning(
                        "INCOMPLETE_STREAM detected on attempt "
                        "%d/%d. Partial content will be discarded "
                        "and the call retried with a fresh "
                        "attempt_id.",
                        llm_attempt,
                        max_llm_attempts,
                    )

                if (
                    "terminated" in err_str
                    or "context" in err_str
                    or "400" in err_str
                    or "maximum context" in err_str
                ):
                    # [D FIX — B7] Context compaction path.
                    # Unchanged from Phase D except for the
                    # incomplete-stream check after the retry, which
                    # ensures a successful-but-truncated compact
                    # response is also treated as a failure.
                    logger.warning(
                        "Provider rejected full context (%s). "
                        "Rebuilding projection with tighter config "
                        "on attempt %d.",
                        exc,
                        llm_attempt,
                    )
                    tighter_config = ProjectionConfig(
                        keep_recent_units=3,
                        keep_first_user=True,
                        keep_last_user=True,
                        compaction_threshold_ratio=0.85,
                    )
                    projection_result = projection.build(
                        self.state,
                        self.system_prompt,
                        config_override=tighter_config,
                    )
                    llm_messages = projection_result.messages
                    logger.info(
                        "Aggressive projection retry: %s",
                        projection_result.stats.summary(),
                    )
                    try:
                        response = await _call_llm(llm_messages)

                        # [E.2] Same incomplete check as above — a
                        # compact retry that ends mid-stream is not a
                        # valid response either.
                        if getattr(response, "finish_reason", None) == "incomplete":
                            raise RuntimeError(
                                f"INCOMPLETE_STREAM: LLM returned "
                                f"finish_reason='incomplete' after "
                                f"compaction on attempt "
                                f"{llm_attempt}/{max_llm_attempts}."
                            )

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
                            await _emit_reset_events(
                                reason="fatal_oom_compaction"
                            )
                            raise

                        # A more aggressive projection still failed.
                        # If the error is transient, fall through to
                        # the transient-retry branch below; otherwise
                        # raise the compact attempt's exception.
                        compact_err_str = str(compact_exc).lower()
                        compact_is_incomplete = (
                            "incomplete_stream" in compact_err_str
                            or "incomplete stream" in compact_err_str
                        )
                        if not (
                            compact_is_incomplete
                            or "429" in compact_err_str
                            or "rate limit" in compact_err_str
                            or "timeout" in compact_err_str
                            or "502" in compact_err_str
                            or "503" in compact_err_str
                            or "connection" in compact_err_str
                        ):
                            await _emit_reset_events(
                                reason="final_failure_compaction"
                            )
                            raise last_llm_exc

                is_transient = (
                    is_incomplete_stream
                    or "429" in err_str
                    or "rate limit" in err_str
                    or "timeout" in err_str
                    or "502" in err_str
                    or "503" in err_str
                    or "connection" in err_str
                )
                if is_transient and llm_attempt < max_llm_attempts:
                    delay = 1.5 * llm_attempt
                    logger.warning(
                        "Transient LLM error on attempt %d (%s). "
                        "Retrying in %.1fs...",
                        llm_attempt,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                # [E.2] Final failure — clear the UI before raising
                # so no partial content remains visible from a
                # failed attempt.
                await _emit_reset_events(reason="final_failure")
                raise last_llm_exc

        # ──────────────────────────────────────────────────────────────
        # Final flush: push any residual buffered text (incomplete last
        # word / sentence) so the timeline reflects the full generation.
        # ──────────────────────────────────────────────────────────────
        await _flush_all(force=True)

        # Fallback for the reasoning channel: if the provider never
        # streamed any reasoning, emit the final reasoning text as a
        # single non-delta THOUGHT so the reasoning panel is not empty.
        thought_val = response.reasoning or response.thought or ""
        if thought_val and not _streamed_thought_any:
            await self._emit(
                EventType.THOUGHT,
                step=step,
                payload={
                    "thought": thought_val,
                    "content": thought_val,
                    "is_delta": False,
                    # [E.2] The fallback lands in the same attempt as
                    # the final response — carry the id so the
                    # frontend can keep it.
                    "attempt_id": _attempt_id,
                },
            )

        # Fallback for the content channel: if the provider never
        # streamed any content, surface the final answer as a single
        # non-delta CHUNK so it appears in the main response bubble
        # even on the non-streaming path.
        if response.content and not _streamed_content_any:
            await self._emit(
                EventType.CHUNK,
                step=step,
                payload={
                    "delta": response.content,
                    "text": response.content,
                    "content": response.content,
                    "is_delta": False,
                    # [E.2] Same correlation rationale as above.
                    "attempt_id": _attempt_id,
                },
            )

        return response

    async def _drain_output_queue(
        self,
        queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
        *,
        tool_call_id: str,
        tool_name: str,
        step: int,
    ) -> None:
        """
        Consume streamed chunks from a tool's output queue and emit them
        as SSE events.

        Terminates when the sentinel (``None``) is received. The caller
        is responsible for enqueueing the sentinel in a ``finally``
        block, which guarantees the drain task exits even when the
        tool raised.

        Event mapping
        -------------
        Chunks whose ``stream == "progress"`` are emitted as
        ``TOOL_PROGRESS`` (structured progress update rendered as a
        progress bar in the frontend). Everything else is emitted as
        ``TOOL_OUTPUT`` (raw stdout/stderr chunks appended to the tool
        card's live output buffer).

        Error handling
        --------------
        Any failure during event emission is logged and swallowed. A
        broken emitter must never prevent the tool from completing;
        the tool's own result (returned via ``aexecute_with_result``)
        is already authoritative. Losing a mid-flight progress event
        is acceptable; failing the tool is not.
        """
        while True:
            item = await queue.get()
            if item is None:
                return
            try:
                stream = item.get("stream", "output")
                event_type = (
                    EventType.TOOL_PROGRESS
                    if stream == "progress"
                    else EventType.TOOL_OUTPUT
                )
                await self._emit(event_type, step=step, payload=item)
            except Exception as exc:
                logger.warning(
                    "Failed to emit streamed tool output for '%s' "
                    "(call=%s): %s",
                    tool_name,
                    tool_call_id,
                    exc,
                )

    async def act(
        self, step: int, tool_calls: List[ToolCall]
    ) -> List[ToolExecutionResult]:
        """
        Execute all tool calls emitted by the LLM and return their
        canonical results.

        [C.3 FIX — B5 + B6] The historical return type was ``List[str]``:
        a plain-text observation per tool call. ``step()`` had no
        structured way to know whether a tool succeeded or failed, so
        it fell back to inspecting tool NAMES — which produced the B3
        bug (a failed ``terminate`` still ended the run). The new
        return type is ``List[ToolExecutionResult]``, whose elements
        carry ``status``, ``outcome``, ``exit_code``, ``error``, and
        ``tool_call_id``. The termination evaluator (C.4) consumes
        these results directly.

        Execution path
        --------------
        Every tool invocation flows through
        ``ToolCollection.aexecute_with_result`` -> ``ExecutionManager``:

          * synchronous tools are offloaded to a worker thread (B6);
          * timeouts become ``status="timeout"`` with ``exit_code=124``;
          * tool-level exceptions become ``status="failure"``;
          * run-level cancellation propagates as ``CancelledError``.

        Retry semantics
        ---------------
        The agent (this method) is the ONLY retry layer. For each tool
        call, up to ``policy.max_attempts`` attempts are made. Between
        attempts, ``policy.is_retry_permitted`` decides whether to
        continue, ``policy.compute_delay`` computes the backoff, and a
        ``TOOL_RETRY`` event is emitted with the attempt number. Non-
        retriable failures break the loop immediately.

        Human tools (``ask_human``, ``human_input``) get a 600-second
        default timeout so the user has ample time to respond.
        """
        results: List[ToolExecutionResult] = []

        # Resolve the collection that carries ``aexecute_with_result``.
        # ``self.tool_collection`` may be a ``ToolCollection`` (which
        # implements the method directly) or a ``ToolRegistry`` (which
        # wraps one as ``._collection``).
        tools_obj = (
            getattr(self, "tool_registry", None)
            or getattr(self, "tool_collection", None)
            or getattr(self, "tools", None)
        )
        collection = (
            getattr(tools_obj, "_collection", tools_obj)
            if tools_obj is not None
            else None
        )
        if collection is None or not hasattr(collection, "aexecute_with_result"):
            logger.error(
                "act(): no tool collection with aexecute_with_result "
                "is available; returning empty results."
            )
            return []

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
                    self.state.mark_waiting_for_input(
                        reason="Agent invoked interactive human tool"
                    )

            policy = self._get_tool_policy(tool_name)

            final_result: Optional[ToolExecutionResult] = None
            attempts_made = 0

            for attempt in range(1, policy.max_attempts + 1):
                attempts_made = attempt

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

                # Human tools get a long default timeout; other tools
                # use the policy's configured value.
                if is_human_tool:
                    call_timeout: Optional[float] = float(
                        args.get("timeout_seconds") or 600.0
                    )
                else:
                    call_timeout = policy.timeout_seconds

                # ----------------------------------------------------------
                # Tool Output Streaming (opt-in via feature flag)
                #
                # When enabled AND the tool is not interactive, a
                # per-(call, attempt) ToolExecutionContextImpl is
                # created and backed by an asyncio.Queue. A dedicated
                # drain task consumes the queue and translates each
                # item into a TOOL_OUTPUT or TOOL_PROGRESS event.
                #
                # When the flag is off (the default) OR the tool is
                # interactive, ``context=None`` is passed and behavior
                # is byte-for-byte identical to the pre-feature
                # baseline. This is the "do no harm" guarantee.
                #
                # Interactive tools (ask_human / human_input) are
                # excluded unconditionally: they block waiting for
                # user input, produce no meaningful mid-flight
                # output, and wrapping them in a queue+drain task
                # would waste overhead on every HITL round-trip.
                # ----------------------------------------------------------
                streaming_ctx: Optional[ToolExecutionContextImpl] = None
                output_queue: Optional[
                    "asyncio.Queue[Optional[Dict[str, Any]]]"
                ] = None
                drain_task: Optional[asyncio.Task] = None

                if self._streaming_enabled and not is_human_tool:
                    try:
                        output_queue = asyncio.Queue()
                        streaming_ctx = ToolExecutionContextImpl(
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            attempt=attempt,
                            queue=output_queue,
                        )
                        drain_task = asyncio.create_task(
                            self._drain_output_queue(
                                output_queue,
                                tool_call_id=call_id,
                                tool_name=tool_name,
                                step=step,
                            )
                        )
                    except Exception as ctx_err:
                        # Streaming initialization failure must NOT
                        # prevent the tool from running. Degrade to
                        # the non-streaming path and log the cause.
                        logger.warning(
                            "act(): failed to initialize tool output "
                            "streaming for '%s' (call=%s): %s. "
                            "Falling back to non-streaming execution.",
                            tool_name,
                            call_id,
                            ctx_err,
                        )
                        streaming_ctx = None
                        output_queue = None
                        drain_task = None

                try:
                    result = await collection.aexecute_with_result(
                        tool_name,
                        args,
                        tool_call_id=call_id,
                        timeout_seconds=call_timeout,
                        context=streaming_ctx,
                    )
                except asyncio.CancelledError:
                    if is_human_tool and self.state is not None:
                        self.state.mark_failed(
                            "Human interaction cancelled.",
                            error_code="CANCELLED",
                        )
                    raise
                except Exception as exc:
                    # ``aexecute_with_result`` is documented to never
                    # raise, but a custom collection could violate that
                    # contract. Degrade to a structured failure so the
                    # agent continues instead of crashing the run.
                    logger.exception(
                        "act(): aexecute_with_result raised for '%s'.",
                        tool_name,
                    )
                    result = ToolExecutionResult.failure(
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        error=f"Tool execution raised: {exc}",
                        exit_code=1,
                    )
                finally:
                    # Signal the drain task to stop and wait (bounded)
                    # for its natural exit BEFORE the OBSERVATION event
                    # for this attempt is dispatched later in the loop.
                    # This guarantees the frontend sees the ordering:
                    #
                    #     TOOL_CALL -> TOOL_OUTPUT... -> OBSERVATION
                    #
                    # Failure of the drain cleanup is non-fatal: the
                    # tool result has already been computed above and
                    # nothing in this block may alter it. The
                    # ``except BaseException`` deliberately swallows
                    # CancelledError as well — the original in-flight
                    # exception (if any) from the try block continues
                    # to propagate once the finally block exits.
                    if output_queue is not None:
                        try:
                            output_queue.put_nowait(None)
                        except Exception:
                            pass
                    if drain_task is not None and not drain_task.done():
                        try:
                            # 2s is comfortably larger than the drain
                            # task's natural work (it just reads a
                            # queue and emits events), but tight enough
                            # that a pathological drain cannot hang
                            # the agent.
                            await asyncio.wait_for(
                                drain_task, timeout=2.0
                            )
                        except BaseException:
                            try:
                                drain_task.cancel()
                            except BaseException:
                                pass

                final_result = result

                if is_human_tool and self.state is not None:
                    self.state.mark_running(
                        reason="Human interaction resolved"
                    )

                # Compose the observation text used for state history
                # and retry error messages.
                if result.is_success:
                    observation_text = (
                        str(result.output)
                        if result.output is not None
                        else ""
                    )
                else:
                    observation_text = (
                        result.error
                        or (
                            str(result.output)
                            if result.output is not None
                            else ""
                        )
                        or f"Tool '{tool_name}' failed."
                    )

                if self.state is not None:
                    self.state.record_tool_execution(
                        tool_name=result.tool_name or tool_name,
                        arguments=args,
                        output=observation_text,
                        exit_code=result.exit_code,
                        is_error=not result.is_success,
                        tool_call_id=result.tool_call_id or call_id,
                        attempt=attempt,
                    )

                if result.is_success:
                    break

                # Tool-level cancellation is not retriable. Run-level
                # cancellation was already handled by the except block
                # above; this branch covers a tool that returned a
                # "cancelled" result of its own volition.
                if result.status == "cancelled":
                    logger.warning(
                        "Tool '%s' returned status='cancelled'; "
                        "not retrying.",
                        tool_name,
                    )
                    break

                # Synthesize a representative exception so the retry
                # policy's transient-error heuristics (timeout, ...)
                # continue to fire. For non-timeout failures we pass
                # ``None`` — matching the pre-C.3 fallback behavior
                # where attempt_exc was left unset for non-exception
                # failures.
                retry_error: Optional[BaseException] = None
                if result.status == "timeout":
                    retry_error = asyncio.TimeoutError(
                        result.error or f"Tool '{tool_name}' timed out."
                    )

                can_retry = policy.is_retry_permitted(
                    current_attempt=attempt, error=retry_error
                )
                if not can_retry or attempt >= policy.max_attempts:
                    logger.info(
                        "Tool '%s' failure on attempt %d is "
                        "non-retryable or attempts exhausted.",
                        tool_name,
                        attempt,
                    )
                    break

                delay = policy.compute_delay(attempt + 1)
                logger.warning(
                    "Tool '%s' failed on attempt %d/%d (%s). "
                    "Retrying in %.1fs...",
                    tool_name,
                    attempt,
                    policy.max_attempts,
                    observation_text[:80],
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
                        "error": observation_text,
                        "delay_seconds": delay,
                    },
                )

                if (
                    self.state is not None
                    and self.state.status == ExecutionStatus.RUNNING
                ):
                    self.state.mark_retrying(
                        reason=(
                            f"Retrying tool '{tool_name}' "
                            f"(attempt {attempt + 1})"
                        )
                    )

                if delay > 0:
                    await asyncio.sleep(delay)

                if (
                    self.state is not None
                    and self.state.status == ExecutionStatus.RETRYING
                ):
                    self.state.mark_running(
                        reason=(
                            f"Resumed execution for tool retry "
                            f"(attempt {attempt + 1})"
                        )
                    )

            # Every tool_call iteration always sets final_result at
            # least once (policy.max_attempts >= 1 is invariant).
            assert final_result is not None, (
                f"act(): final_result not set for tool '{tool_name}'"
            )

            # Compose the observation payload for SSE and history.
            if final_result.is_success:
                final_observation_text = (
                    str(final_result.output)
                    if final_result.output is not None
                    else ""
                )
            else:
                final_observation_text = (
                    final_result.error
                    or (
                        str(final_result.output)
                        if final_result.output is not None
                        else ""
                    )
                    or f"Tool '{tool_name}' failed."
                )

            # Emit the OBSERVATION event. The full structured result
            # is embedded as ``result`` so downstream consumers (UI,
            # audit, replay) can render status/exit_code/artifacts.
            await self._emit(
                EventType.OBSERVATION,
                step=step,
                payload={
                    "tool_name": final_result.tool_name or tool_name,
                    "output": final_observation_text,
                    "tool_call_id": final_result.tool_call_id or call_id,
                    "attempt": attempts_made,
                    "status": final_result.status,
                    "outcome": final_result.outcome,
                    "exit_code": final_result.exit_code,
                    "is_error": not final_result.is_success,
                    "duration_ms": final_result.duration_ms,
                    "result": final_result.to_dict(),
                },
            )

            # Conversation history stores the TRUNCATED observation
            # string. The full result stays in ``state.tool_history``
            # (via record_tool_execution) and in the SSE event above.
            _truncated_observation = _truncate_observation(
                tool_name, final_observation_text
            )

            self.messages.append({
                "role": "tool",
                "tool_call_id": final_result.tool_call_id or call_id,
                "name": tool_name,
                "content": _truncated_observation,
            })

            if self.state is not None:
                self.state.add_message(
                    role=MessageRole.TOOL,
                    content=_truncated_observation,
                    name=tool_name,
                    tool_call_id=final_result.tool_call_id or call_id,
                )

            results.append(final_result)

        return results

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
            # [FIX-NO-TOOL-NOT-DONE] The prior behavior treated ANY model
            # response without tool_calls as "task complete". That
            # assumption is WRONG for agentic workloads:
            #
            #   * Reasoning-class models (Nemotron, DeepSeek-R1, QwQ,
            #     o1/o3, "thinking" models) frequently emit intermediate
            #     text ("Let me search for...") WITHOUT a tool call,
            #     then call the tool on the next step. Ending the loop
            #     here silently truncated the task — the model never
            #     finished what the user asked for, but the engine
            #     reported "completed successfully".
            #
            #   * The PELDRUN operational contract explicitly states
            #     that the task is ONLY complete when the model calls
            #     the ``terminate`` tool (see [OPERATIONAL DIRECTIVE]
            #     §3 in peldrun_engine.py). Any other response must be
            #     treated as intermediate.
            #
            # The correct behavior: send an explicit reminder asking the
            # model to call ``terminate`` (if done) or continue with
            # tools (if not done), then CONTINUE the loop.
            #
            # Safety rails (to bound the loop and prevent runaway text):
            #   1. Max steps: reaching the ceiling without terminate
            #      raises a clear failure.
            #   2. Two consecutive no-tool responses: the model is
            #      genuinely stuck → fail fast rather than loop forever.
            self._final_answer = (
                response.content
                or response.thought
                or response.reasoning
                or ""
            )
            state.final_output = self._final_answer

            # [FIX-EMPTY-RESPONSE] A truly empty response (no content,
            # no reasoning, no tool_calls) is NOT an "agent forgot to
            # call terminate" situation — it is a broken provider
            # response. Retrying with a reminder will produce another
            # empty response because the underlying cause is
            # deterministic: wrong model name, wrong URL, missing
            # permissions, or a safety filter that blocked everything.
            #
            # Fail immediately with a diagnostic message that surfaces:
            #   * finish_reason (reveals safety_filter, length, etc.)
            #   * provider + model (to identify which adapter ran)
            #   * usage (to diagnose quota / billing issues)
            # instead of waiting two steps and then reporting the
            # misleading "no tool calls for 2 consecutive steps".
            if not self._final_answer:
                _provider = (
                    getattr(self.llm, "provider_id", None) or "unknown"
                )
                _model = (
                    getattr(self.llm, "model", None)
                    or getattr(getattr(self.llm, "config", None), "model", None)
                    or getattr(
                        getattr(self.llm, "_base_request", None),
                        "model_id", None,
                    )
                    or "unknown"
                )
                _finish_reason = response.finish_reason or "unknown"
                _usage = getattr(response, "usage", None)
                _usage_summary = (
                    _usage.model_dump() if _usage is not None else None
                )
                # [FIX-EMPTY-DIAGNOSTIC] Enrich the diagnostic with the
                # reasoning / output token split so the operator can
                # distinguish "model crashed" from "model burned its
                # budget on thinking". Gemini 2.5+ thinking models
                # frequently hit finish_reason='length' with 3000+
                # reasoning tokens and zero visible output — that is
                # NOT a crash, it is a token-budget misconfiguration.
                _hint = ""
                if _usage is not None:
                    _reasoning_tok = getattr(
                        _usage, "reasoning_output_tokens", None
                    )
                    _output_tok = getattr(_usage, "output_tokens", None)
                    if (
                        isinstance(_reasoning_tok, int)
                        and _reasoning_tok > 0
                        and _finish_reason == "length"
                    ):
                        _hint = (
                            f" HINT: the model consumed {_reasoning_tok} "
                            f"reasoning tokens and hit "
                            f"max_output_tokens before producing visible "
                            f"text. Increase max_output_tokens (e.g. to "
                            f"{max(8192, (_reasoning_tok or 0) + 2048)}) "
                            f"or reduce the model's thinking_budget."
                        )
                raise RuntimeError(
                    f"Provider returned an EMPTY response "
                    f"(no content, no reasoning, no tool calls). "
                    f"provider={_provider!r}, model={_model!r}, "
                    f"finish_reason={_finish_reason!r}, "
                    f"step={state.current_step}, "
                    f"usage={_usage_summary}.{_hint} "
                    f"Common causes: "
                    f"(1) invalid or unsupported model name, "
                    f"(2) wrong API URL / route (e.g. OpenAI shim URL "
                    f"sent to native adapter), "
                    f"(3) API key lacks access to the model, "
                    f"(4) safety filter blocked the entire response. "
                    f"Check provider logs and configuration before "
                    f"retrying."
                )

            # [FIX-MULTIMODAL-NO-TOOLS] Multimodal turns where the
            # provider disables tools (notably GigaChat v2, which
            # discards tool definitions when images are present) can
            # never emit a `terminate` call. Without this branch, the
            # loop guard "2 consecutive no-tool steps" fires even
            # though the user's request WAS fulfilled — the image was
            # analyzed and the descriptive text is complete.
            #
            # Detect the case as: current step is the FIRST step AND
            # at least one message in the conversation carries an
            # `image_url` part. Multi-step continuations after the
            # first step are excluded so Gemini (which DOES support
            # tools + images) can still complete multi-tool workflows
            # when it chooses to.
            # [FIX-MULTIMODAL-NO-TOOLS-STEP2] Removed the `state.current_step <= 1`
            # restriction. Some providers (GigaChat) discard tools when images
            # are present, which forces the model to answer in plain text.
            # If the conversation contains images and the model produced
            # text, we accept it as the final answer regardless of the step.
            _has_images = False
            for _msg in self.messages:
                if not isinstance(_msg, dict):
                    continue
                _mc = _msg.get("content")
                if isinstance(_mc, list):
                    for _p in _mc:
                        if (
                            isinstance(_p, dict)
                            and _p.get("type") == "image_url"
                        ):
                            _has_images = True
                            break
                if _has_images:
                    break

            if _has_images and self._final_answer.strip():
                logger.info(
                    "Multimodal turn produced text without tool "
                    "calls — accepting as final answer "
                    "(provider disabled tools for image inputs)."
                )
                state.mark_completed(output=self._final_answer)
                return True

            # [SOFT-COMPLETION] Conversational turn: no tool has been
            # invoked during this ENTIRE run. The model's text
            # response IS the final answer (e.g. the user said
            # "Hello" and the model replied "Hello!" with no tools
            # involved). This is the most common path for simple
            # chat-style interactions routed through agent mode and
            # must produce a clean COMPLETED, never an error card.
            #
            # Distinguishing this from "model forgot to terminate
            # after using tools" is exactly why we check
            # ``state.tool_history`` — a non-empty tool history
            # proves tools were used and the reminder path applies;
            # an empty history means the model was never expected to
            # use tools in the first place.
            _any_tool_executed = bool(
                getattr(state, "tool_history", None)
            )
            if not _any_tool_executed:
                logger.info(
                    "Conversational completion on step %d "
                    "(no prior tool usage). Accepting text-only "
                    "response as the final answer.",
                    state.current_step,
                )
                state.metadata["completion_kind"] = "soft_text"
                state.metadata["completion_reason"] = (
                    "no_tool_used_conversational_turn"
                )
                state.mark_completed(output=self._final_answer)
                return True

            # Safety rail 1 — step ceiling.
            max_steps_ceiling = int(
                getattr(state, "max_steps", None)
                or getattr(self.config, "max_steps", None)
                or 30
            )
            if state.current_step >= max_steps_ceiling:
                # [SOFT-COMPLETION] Step ceiling reached without a
                # successful ``terminate``. The model produced text
                # but did not close the task. Instead of surfacing a
                # raw RuntimeError to the user (which looks like an
                # app crash), soft-complete with the accumulated
                # text. The user sees the model's last output and
                # can decide whether to retry with a stronger model
                # or clearer instruction.
                logger.warning(
                    "Reached step ceiling %d without calling "
                    "'terminate' after prior tool usage. "
                    "Soft-completing with the accumulated text.",
                    max_steps_ceiling,
                )
                state.metadata["completion_kind"] = "soft_text"
                state.metadata["completion_reason"] = (
                    "step_ceiling_reached"
                )
                state.mark_completed(output=self._final_answer)
                return True

            # Safety rail 2 — consecutive no-tool responses.
            self._consecutive_no_tool_steps += 1
            if self._consecutive_no_tool_steps >= 2:
                # [SOFT-COMPLETION] Two consecutive text-only steps
                # after tool usage. This is either (a) the model
                # describing the outcome in prose without calling
                # terminate, or (b) a genuine text loop. In both
                # cases, the user benefits from seeing the text
                # rather than an error card. Soft-complete.
                logger.warning(
                    "Model produced text without tool calls for %d "
                    "consecutive steps after prior tool usage. "
                    "Soft-completing with the accumulated text.",
                    self._consecutive_no_tool_steps,
                )
                state.metadata["completion_kind"] = "soft_text"
                state.metadata["completion_reason"] = (
                    "consecutive_text_only_steps"
                )
                state.mark_completed(output=self._final_answer)
                return True

            # Reminder to the model. This becomes part of the
            # conversation history (state.messages + self.messages),
            # so on the next step the model sees it and can react:
            #   * If the task is genuinely done → call 'terminate'.
            #   * If more work is needed → call the next tool.
            _reminder = (
                "[SYSTEM] Your last response contained text but did "
                "NOT call any tool. This does NOT end the task. "
                "If the task is complete, you MUST call the "
                "'terminate' tool to signal completion. If not "
                "complete, use the available tools to make progress. "
                "Do not reply with text alone."
            )
            state.add_message(role=MessageRole.USER, content=_reminder)
            # Also reflect into the in-memory history so the next
            # ``think()`` call (which uses ``self.messages``) sees it
            # immediately even before ``_sync_messages_from_state``
            # runs at the start of the next step.
            self.messages.append({"role": "user", "content": _reminder})
            return False

        # Model produced tool calls — reset the no-tool counter.
        self._consecutive_no_tool_steps = 0

        for tc in response.tool_calls:
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:12]}"

        tool_calls_payload = []
        for tc in response.tool_calls:
            entry = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False) if isinstance(tc.arguments, dict) else str(tc.arguments),
                },
            }
            # Gemini 2.5+ thinking models attach an opaque
            # ``thought_signature`` to each function call. It MUST be
            # echoed back verbatim when the message is replayed on the
            # next turn, or the API returns 400 INVALID_ARGUMENT.
            # Providers without this concept leave the field ``None``,
            # so nothing extra is serialized for them.
            _sig = getattr(tc, "thought_signature", None)
            if _sig:
                entry["thought_signature"] = _sig
            tool_calls_payload.append(entry)
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

        results = await self.act(step=self.current_step, tool_calls=response.tool_calls)

        # [C.4 FIX — B3] The termination decision is now based on the
        # STRUCTURED results returned by ``act()``, not on the tool
        # names the LLM emitted. A tool named "terminate" that failed
        # to execute (unknown registration, invalid arguments, tool-
        # level exception, timeout) is no longer accepted as a
        # successful completion. The evaluator enforces the full
        # metadata contract:
        #
        #   * ``metadata.terminated`` must be True
        #   * ``metadata.status`` must be "success" or "failure"
        #   * for status="success", the tool call itself must have
        #     succeeded (is_success == True)
        #
        # When the terminate call is malformed, the evaluator returns
        # ``should_terminate=False`` with a ``rejection_reason``. The
        # task continues, the model sees the rejection in the
        # observation history, and can retry with a valid call.
        decision = evaluate_termination(results, state)

        if decision.should_terminate:
            # Determine the displayable final answer. Priority:
            #   1. The terminate tool's own output (authoritative —
            #      the model explicitly designated it as the closing
            #      message).
            #   2. The model's response.content (context around the
            #      terminate call, often useful for the user).
            #   3. The failure reason (for terminate:failure with no
            #      output).
            #   4. A generic closing message.
            if decision.final_output:
                self._final_answer = decision.final_output
            elif response.content:
                self._final_answer = response.content
            elif decision.failure_reason:
                self._final_answer = decision.failure_reason
            else:
                self._final_answer = "Task execution finished."

            # Pre-set final_output so the failure path (which does
            # not set it inside mark_failed) still reports a
            # meaningful message to the user. apply_decision overrides
            # it on the success path with the same value.
            state.final_output = self._final_answer

            # The lifecycle module performs the FSM transition
            # (COMPLETED for success, FAILED for terminate:failure).
            apply_decision(
                state, decision, default_success_output=self._final_answer
            )
            return True

        if decision.was_rejected:
            logger.warning(
                "[C.4] Terminate call rejected: %s. Continuing the "
                "loop so the model can correct the call or make more "
                "progress.",
                decision.rejection_reason,
            )

        return False

    async def _astep(self) -> bool:
        if self.state is None:
            self.state = ExecutionState(task_prompt="Default task", workspace_root=str(self.workspace_dir))
        return not (await self.step(state=self.state, emitter=self.emitter))

    def _reset_run_state(self) -> None:
        """
        Reset all per-run agent state to its construction-time defaults.

        [B15 FIX] Prior to this reset, ``ToolCallAgent`` instances
        accumulated state across consecutive ``run_task()`` calls,
        producing the exact failure modes documented in session 7 of
        the PELDRUN execution repair plan:

          * ``_recent_call_signatures`` retained the previous run's
            tool signatures. The loop detector interpreted them as
            "the current run is repeating the same tool call" and
            raised a false positive on the very first step of the
            new run.

          * ``_consecutive_no_tool_steps`` was not zeroed. A fresh
            run whose first step produced a text-only response was
            immediately classified as "the model is stuck thinking
            out loud" and aborted before any real work could
            happen.

          * ``_final_answer`` and ``self.messages`` carried the
            previous run's data. When the next run's first step
            produced no content, the fallback path returned the
            PREVIOUS task's answer as if it were the new task's
            result.

          * ``self.state`` and ``self.current_step`` were left
            dangling, so any diagnostic, event, or retry classifier
            that read them before the first ``step()`` saw stale
            step indices.

        Called at the start of every ``run_task()`` invocation.
        ``arun()`` delegates to ``run_task()``, so both public entry
        points inherit the reset.

        Configuration is NOT reset
        --------------------------
        ``self.system_prompt``, ``self.config``, ``self.llm``,
        ``self.tool_registry``, ``self.tool_collection``,
        ``self.emitter``, and ``self.workspace_dir`` are all config,
        not per-run state. Reusing an agent object across tasks is
        expected to preserve configuration. Callers who want full
        per-run isolation of the emitter (fresh terminal-event
        registry, fresh SSE queue) must construct a new agent with a
        new ``EventEmitter`` — that is a caller concern, not this
        method's.

        Not covered here
        ----------------
        ``self.emitter._is_terminated`` is not reset. The emitter's
        terminated flag is set once a FINAL / ERROR / CANCELLED event
        has been emitted; a second run on the same emitter will
        silently drop its own terminal event because the flag is
        already True. This is a known limitation of the current
        emitter, tracked in the plan under B13 and Phase F. The
        proper fix belongs to ``EventEmitter.astream_sse`` (Phase F)
        and is orthogonal to B15's counter-reset scope.
        """
        self.messages = []
        self.current_step = 1
        self._final_answer = ""
        self._recent_call_signatures = []
        self._consecutive_no_tool_steps = 0
        self.state = None

        # [B15 REMNANT — DIAGNOSTIC ONLY] Warn when this reset is
        # happening on an agent whose emitter has already terminated
        # a previous run. Phase F added ``EventEmitter.reset_for_new_run()``,
        # but the agent does NOT call it automatically — doing so would
        # silently change the emitter's run_id and break callers that
        # rely on the executor-managed run_id (see ``executor.py``,
        # which creates a fresh emitter per run and never reuses one).
        #
        # The correct policy depends on the calling context:
        #
        #   * Executor path (``CoreRuntimeExecutor.execute``):
        #     a fresh emitter is created per run → no reset needed.
        #
        #   * Direct path (``agent.run_task()`` called twice on the
        #     same agent without an external emitter manager):
        #     the caller MUST invoke
        #     ``self.emitter.reset_for_new_run(new_run_id)`` BEFORE
        #     the second ``run_task()`` call. This warning surfaces
        #     the omission so users do not silently lose the second
        #     run's terminal event (B13 symptom).
        #
        # The warning is intentionally non-fatal: an emitter with
        # ``_is_terminated=True`` still emits non-terminal events
        # (STEP_START, THOUGHT, CHUNK, ...), so a run would proceed
        # up to the terminal event and then appear to hang. Logging
        # the risk is the least-invasive way to surface it.
        if getattr(self.emitter, "_is_terminated", False):
            logger.warning(
                "Agent reset with an EventEmitter that is already "
                "terminated. The next run's terminal event (FINAL / "
                "ERROR / CANCELLED) will be SILENTLY DROPPED unless "
                "the caller invokes "
                "``emitter.reset_for_new_run(new_run_id)`` first. "
                "This warning exists because B13 manifests here: "
                "agent reuse with a shared emitter loses the second "
                "run's completion."
            )

    async def run_task(self, prompt: str, max_steps: int = 30) -> str:
        from peldrun.engine.runner import AgentRunner, RunnerConfig

        # [B15 FIX] Reset per-run state before each invocation. The
        # reset covers messages, counters, in-memory step index, and
        # cached final answer — everything that must not leak from
        # one task to the next on the same agent object. This makes
        # ``agent.run_task(task1)`` followed by
        # ``agent.run_task(task2)`` behave as if ``task2`` had been
        # executed by a freshly constructed agent, except that
        # configuration (system prompt, LLM binding, tool registry,
        # workspace, emitter) is preserved.
        self._reset_run_state()

        runner_config = RunnerConfig(max_steps=max_steps)
        runner = AgentRunner(agent=self, emitter=self.emitter, config=runner_config)
        state = await runner.run(task_prompt=prompt, workspace_root=str(self.workspace_dir))
        return state.final_output or self._final_answer or "Task execution finished."

    async def arun(self, task: str = "", max_steps: Optional[int] = None, **kwargs: Any) -> Any:
        prompt_val = task or kwargs.get("prompt", "")
        limit_val = max_steps or getattr(self.config, "max_steps", 30)
        return await self.run_task(prompt=prompt_val, max_steps=limit_val)


__all__ = ["ToolCallAgent"]
