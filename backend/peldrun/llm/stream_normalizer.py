# backend/peldrun/llm/stream_normalizer.py
"""
PELDRUN Stream Normalizer.

Bridges the new native adapter streaming protocol (``LLMStreamEvent``)
to the legacy runtime protocol (``StreamChunk`` + ``on_chunk`` /
``on_thought_chunk`` callbacks).

Without this module, migrating ``dispatcher.py`` and ``executor.py``
to use the new native adapters would require rewriting every consumer
of ``AsyncLLMClient.stream()`` and ``AsyncLLMClient.chat_completion()``.
With it, the migration is a single import change.

Design invariants
-----------------

1. **Single contract per direction.**
   * Adapter → normalizer: ``AsyncIterator[LLMStreamEvent]``
   * Normalizer → consumer: ``AsyncIterator[StreamChunk]`` (streaming)
   * Normalizer → consumer: ``LLMResponse`` (buffered)

   The normalizer owns every translation decision. Callers do not
   branch on event types.

2. **Both APIs share the same accumulation logic.**
   ``collect()`` internally calls ``normalize()`` and accumulates.
   Any bug fixed in one path is fixed in the other.

3. **Callbacks fire per-token.**
   ``on_chunk`` fires once per ``CONTENT_DELTA``; ``on_thought_chunk``
   fires once per ``REASONING_DELTA``. The exact call signature matches
   ``AsyncLLMClient.chat_completion()`` so that existing callers keep
   working unchanged.

4. **Tool call reconstruction is stateful but well-defined.**
   The normalizer tracks tool-call state by index, so that:
   * ``TOOL_CALL_START``        → opens an entry, emits an empty delta
   * ``TOOL_CALL_DELTA``        → appends argument fragment
   * ``TOOL_CALL_COMPLETE``     → emits the finalized ``ToolCall``
                                    only at MESSAGE_COMPLETE time
   This mirrors how ``AsyncLLMClient.chat_completion`` reconstructs
   tool calls from ``DeltaToolCall`` sequences today.

5. **Cancellation is propagated unwrapped.**
   ``asyncio.CancelledError`` from the adapter is re-raised immediately.
   The normalizer never wraps it.

6. **Diagnostics survive.**
   ``provider_metadata`` from the final ``MESSAGE_COMPLETE`` event
   (LM Studio's ``stats``, for example) is preserved in
   ``StreamChunk.raw`` and in the final ``LLMResponse.raw``.

See also
--------

    * ``backend/peldrun/llm/contracts.py`` — LLMStreamEvent, StreamEventType
    * ``backend/peldrun/llm/client.py``    — StreamChunk, LLMResponse, ToolCall
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from peldrun.llm.client import (
    DeltaToolCall,
    LLMResponse,
    StreamChunk,
    TokenUsage,
    ToolCall,
    UsageSource,
)
from peldrun.llm.contracts import LLMStreamEvent, StreamEventType

logger = logging.getLogger("peldrun.llm.stream_normalizer")


# ---------------------------------------------------------------------------
# Watchdog timeout constants
# ---------------------------------------------------------------------------
#
# Match the pre-M7 AsyncLLMClient behavior exactly. Same env vars, same
# defaults. The migration to adapter-based streaming must not change the
# timeouts users see when a provider hangs.
#
#   PELDRUN_LLM_FIRST_CHUNK_TIMEOUT  — max wait for the FIRST event (90s)
#   PELDRUN_LLM_INTER_CHUNK_TIMEOUT  — max silence between events (120s)
import os as _os

_FIRST_CHUNK_TIMEOUT_SECONDS: float = float(
    _os.environ.get("PELDRUN_LLM_FIRST_CHUNK_TIMEOUT", "90.0")
)
_INTER_CHUNK_TIMEOUT_SECONDS: float = float(
    _os.environ.get("PELDRUN_LLM_INTER_CHUNK_TIMEOUT", "120.0")
)


# ---------------------------------------------------------------------------
# Callback type aliases
# ---------------------------------------------------------------------------


ContentCallback = Callable[[str], Any]
ThoughtCallback = Callable[[str], Any]


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class StreamNormalizer:
    """
    Consume ``LLMStreamEvent`` and produce ``StreamChunk`` or
    ``LLMResponse``.

    Stateless across calls: each ``normalize()`` / ``collect()`` call
    creates its own accumulator. Safe to reuse the instance across
    requests and threads.
    """

    # ------------------------------------------------------------------
    # Streaming API (legacy-compatible)
    # ------------------------------------------------------------------

    async def normalize(
        self,
        events: AsyncIterator[LLMStreamEvent],
        *,
        on_chunk: Optional[ContentCallback] = None,
        on_thought_chunk: Optional[ThoughtCallback] = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Convert an event stream to ``StreamChunk`` and fire the
        legacy callbacks per-token.

        The emitted ``StreamChunk`` sequence is IDENTICAL in shape to
        what ``AsyncLLMClient.stream()`` yields today:

        * Content deltas arrive as ``StreamChunk.content_delta``
        * Reasoning deltas arrive as ``StreamChunk.reasoning_delta``
        * Tool call deltas arrive as ``StreamChunk.tool_call_deltas``
          (incremental — one ``DeltaToolCall`` per fragment)
        * The final chunk carries ``finish_reason`` and ``usage``

        Parameters
        ----------
        events
            Async iterator produced by ``adapter.stream(request)``.
        on_chunk
            Optional per-token content callback. Fires before the
            corresponding ``StreamChunk`` is yielded. May be sync or
            async.
        on_thought_chunk
            Optional per-token reasoning callback. Same semantics as
            ``on_chunk``.
        """
        async for chunk in self._normalize_impl(
            events, on_chunk=on_chunk, on_thought_chunk=on_thought_chunk
        ):
            yield chunk

    async def _normalize_impl(
        self,
        events: AsyncIterator[LLMStreamEvent],
        *,
        on_chunk: Optional[ContentCallback],
        on_thought_chunk: Optional[ThoughtCallback],
    ) -> AsyncIterator[StreamChunk]:
        # State accumulated across events.
        pending_finish_reason: Optional[str] = None
        pending_usage: Optional[TokenUsage] = None
        pending_provider_metadata: Optional[Dict[str, Any]] = None
        last_tool_signature: Dict[int, Dict[str, Any]] = {}
        # [FIX-DOUBLE-ARGS] Track which tool-call indexes have already
        # emitted incremental TOOL_CALL_DELTA fragments during this
        # stream. If deltas were emitted, the accumulator already has
        # the full args string; re-emitting the COMPLETE args as
        # another delta would concatenate two JSON objects
        # (``{...}{...}``) and produce invalid JSON. That invalid JSON
        # is what the accumulator falls back to wrapping in
        # ``{"raw": "..."}`` — the exact bug that produced
        # "Search Error: Query cannot be empty" and
        # "Parameter 'path' cannot be empty" on LM Studio / Nemotron
        # Nano 4B runs.
        _tool_call_deltas_emitted: set = set()

        # ------------------------------------------------------------------
        # Watchdog state.
        #
        # The pre-M7 ``AsyncLLMClient.chat_completion`` had two timeouts:
        # ``STREAM_FIRST_CHUNK_TIMEOUT_SECONDS`` (max wait for the first
        # event) and ``STREAM_INTER_CHUNK_TIMEOUT_SECONDS`` (max silence
        # between events). Those timeouts are what prevent the backend
        # from freezing forever when a provider accepts the connection
        # but never sends data (ejected model, crashed server, network
        # black hole).
        #
        # We replicate that behavior here — once, for every adapter
        # (legacy via _LegacyAdapterBridge AND native adapters). The
        # watchdog lives in the normalizer so a stalled adapter cannot
        # bypass it.
        # ------------------------------------------------------------------
        first_chunk_timeout = _FIRST_CHUNK_TIMEOUT_SECONDS
        inter_chunk_timeout = _INTER_CHUNK_TIMEOUT_SECONDS
        first_event_received = False
        event_count = 0

        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        events.__anext__(),
                        timeout=(
                            first_chunk_timeout
                            if not first_event_received
                            else inter_chunk_timeout
                        ),
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    waited = (
                        first_chunk_timeout
                        if not first_event_received
                        else inter_chunk_timeout
                    )
                    if event_count == 0:
                        raise RuntimeError(
                            f"The provider did not produce any streaming "
                            f"event within {waited:.0f}s. The provider may "
                            f"have ejected the model, crashed, or lost "
                            f"connectivity. Check that the model is loaded "
                            f"and the provider is running, then retry."
                        )
                    raise RuntimeError(
                        f"The provider stopped responding after "
                        f"{event_count} event(s). No new data arrived for "
                        f"{waited:.0f}s. The provider likely crashed or the "
                        f"model was ejected mid-generation."
                    )

                first_event_received = True
                event_count += 1

                event_type = event.type

                # ---------------------------------------------------
                # CONTENT_DELTA — text token.
                # ---------------------------------------------------
                if event_type == StreamEventType.CONTENT_DELTA:
                    piece = event.content_delta
                    if isinstance(piece, str) and piece:
                        if on_chunk is not None:
                            result = on_chunk(piece)
                            if asyncio.iscoroutine(result):
                                await result
                        yield StreamChunk(content_delta=piece)
                    continue

                # ---------------------------------------------------
                # REASONING_DELTA — thinking token.
                # ---------------------------------------------------
                if event_type == StreamEventType.REASONING_DELTA:
                    piece = event.reasoning_delta
                    if isinstance(piece, str) and piece:
                        if on_thought_chunk is not None:
                            result = on_thought_chunk(piece)
                            if asyncio.iscoroutine(result):
                                await result
                        yield StreamChunk(reasoning_delta=piece)
                    continue

                # ---------------------------------------------------
                # TOOL_CALL_START — emit an empty delta to open the
                # tool call slot on the consumer side.
                # ---------------------------------------------------
                if event_type == StreamEventType.TOOL_CALL_START:
                    index = (
                        event.tool_index
                        if event.tool_index is not None
                        else 0
                    )
                    tool_id = event.tool_call_id or f"call_{uuid.uuid4().hex[:8]}"
                    tool_name = event.tool_name or ""
                    last_tool_signature[index] = {
                        "id": tool_id,
                        "name": tool_name,
                    }
                    yield StreamChunk(
                        tool_call_deltas=[
                            DeltaToolCall(
                                index=index,
                                id=tool_id,
                                name=tool_name,
                                arguments="",
                                type="function",
                            )
                        ]
                    )
                    continue

                # ---------------------------------------------------
                # TOOL_CALL_DELTA — incremental argument fragment.
                #
                # CRITICAL: OpenAI's streaming protocol sends the tool's
                # ``id`` and ``name`` on the FIRST delta of each tool
                # call, with ``arguments=""``. Subsequent deltas carry
                # only argument bytes (``id`` and ``name`` are null).
                #
                # The pre-fix version of this block SKIPPED the first
                # delta entirely (because ``""`` is falsy), so the
                # tool name was never captured, and the accumulator
                # produced ``ToolCall(name="")``. That in turn triggered
                # the runtime's loop detector on the second step.
                #
                # The fix: capture id/name unconditionally on arrival,
                # and ALWAYS emit a DeltaToolCall so the downstream
                # accumulator can record them even when this fragment
                # has no argument bytes yet.
                # ---------------------------------------------------
                if event_type == StreamEventType.TOOL_CALL_DELTA:
                    index = (
                        event.tool_index
                        if event.tool_index is not None
                        else 0
                    )

                    # Capture id/name from this event — first arrival
                    # wins (later deltas carry None for both).
                    if index not in last_tool_signature:
                        last_tool_signature[index] = {
                            "id": event.tool_call_id
                            or f"call_{uuid.uuid4().hex[:8]}",
                            "name": event.tool_name or "",
                        }
                    else:
                        sig = last_tool_signature[index]
                        if event.tool_call_id and not sig.get("id"):
                            sig["id"] = event.tool_call_id
                        if event.tool_name and not sig.get("name"):
                            sig["name"] = event.tool_name

                    sig = last_tool_signature[index]
                    piece = event.tool_arguments_delta

                    # [FIX-DOUBLE-ARGS] Record that this index has
                    # received at least one incremental delta. Later,
                    # TOOL_CALL_COMPLETE will check this set to decide
                    # whether to emit the complete args as a delta
                    # (needed for Ollama / Google) or skip them
                    # (correct for LM Studio / OpenAI which already
                    # streamed the full JSON via deltas).
                    _tool_call_deltas_emitted.add(index)

                    # Emit even when ``piece`` is empty — this carries
                    # the tool name to the accumulator on the very
                    # first delta.
                    yield StreamChunk(
                        tool_call_deltas=[
                            DeltaToolCall(
                                index=index,
                                id=sig["id"],
                                name=sig["name"],
                                arguments=(
                                    piece
                                    if isinstance(piece, str) and piece
                                    else None
                                ),
                                type="function",
                            )
                        ]
                    )
                    continue

                # ---------------------------------------------------
                # TOOL_CALL_COMPLETE — emit the complete arguments
                # as a single DeltaToolCall fragment, which the
                # consumer will parse into a final ToolCall once
                # ``finish_reason`` arrives. This preserves the
                # incremental contract while still delivering the
                # full arguments for adapters that only emit them
                # once (Ollama, Google).
                # ---------------------------------------------------
                if event_type == StreamEventType.TOOL_CALL_COMPLETE:
                    index = (
                        event.tool_index
                        if event.tool_index is not None
                        else 0
                    )
                    # [FIX-DOUBLE-ARGS] Skip when the adapter has
                    # already streamed incremental deltas for this
                    # index. Emitting the complete args as ANOTHER
                    # delta would concatenate two JSON objects —
                    # ``{...}{...}`` — which is invalid JSON. The
                    # accumulator then falls back to wrapping it in
                    # ``{"raw": "..."}``, breaking every downstream
                    # tool call. This was the root cause of
                    # "Search Error: Query cannot be empty" and
                    # "Parameter 'path' cannot be empty" on LM Studio
                    # / Nemotron Nano 4B runs.
                    #
                    # For adapters that ONLY emit TOOL_CALL_COMPLETE
                    # without prior deltas (Ollama, Google), this
                    # set is empty for the index, so we fall through
                    # and emit the full args as a single delta —
                    # preserving the original behavior for those
                    # providers.
                    if index in _tool_call_deltas_emitted:
                        continue
                    args_complete = event.tool_arguments_complete
                    if args_complete is None:
                        continue
                    # Serialize as JSON so the consumer's existing
                    # DeltaToolCall accumulation logic (which joins
                    # string fragments) reconstructs the correct
                    # final JSON.
                    try:
                        args_str = json.dumps(
                            args_complete, ensure_ascii=False
                        )
                    except Exception:
                        args_str = str(args_complete)
                    if not args_str:
                        continue
                    sig = last_tool_signature.get(
                        index,
                        {
                            "id": event.tool_call_id
                            or f"call_{uuid.uuid4().hex[:8]}",
                            "name": event.tool_name or "",
                        },
                    )
                    # Native adapters (Google Gemini thinking models,
                    # notably) attach an opaque per-call token in
                    # ``provider_metadata["thought_signature"]``. It MUST
                    # be forwarded to the accumulator so it reaches the
                    # final ToolCall and is echoed back on the next turn.
                    # Missing this was the root cause of
                    # "Function call is missing a thought_signature".
                    thought_sig: Optional[str] = None
                    if event.provider_metadata:
                        raw_sig = event.provider_metadata.get(
                            "thought_signature"
                        )
                        if isinstance(raw_sig, str) and raw_sig:
                            thought_sig = raw_sig
                    yield StreamChunk(
                        tool_call_deltas=[
                            DeltaToolCall(
                                index=index,
                                id=sig["id"],
                                name=sig["name"],
                                arguments=args_str,
                                type="function",
                                thought_signature=thought_sig,
                            )
                        ]
                    )
                    continue

                # ---------------------------------------------------
                # USAGE_UPDATE — stash until MESSAGE_COMPLETE so the
                # legacy consumer's "single usage at end" assumption
                # is preserved.
                # ---------------------------------------------------
                if event_type == StreamEventType.USAGE_UPDATE:
                    if event.usage is not None:
                        pending_usage = event.usage
                    continue

                # ---------------------------------------------------
                # MESSAGE_COMPLETE — final chunk carries finish_reason
                # and the accumulated usage.
                # ---------------------------------------------------
                if event_type == StreamEventType.MESSAGE_COMPLETE:
                    pending_finish_reason = event.finish_reason or "stop"
                    if event.usage is not None:
                        pending_usage = event.usage
                    if event.provider_metadata:
                        pending_provider_metadata = dict(
                            event.provider_metadata
                        )
                    yield StreamChunk(
                        finish_reason=pending_finish_reason,
                        usage=pending_usage,
                        raw=pending_provider_metadata,
                    )
                    return

                # ---------------------------------------------------
                # PROVIDER_ERROR — surface as a final chunk with an
                # error marker. Consumers react by raising.
                # ---------------------------------------------------
                if event_type == StreamEventType.PROVIDER_ERROR:
                    error_payload = dict(event.error or {})
                    error_message = str(
                        error_payload.get("message")
                        or "Provider reported an error during streaming."
                    )
                    yield StreamChunk(
                        finish_reason="error",
                        raw={
                            "error": error_payload,
                            "sequence": event.sequence,
                        },
                    )
                    raise RuntimeError(error_message)

        except asyncio.CancelledError:
            # Propagate cancellation unwrapped.
            raise


# ---------------------------------------------------------------------------
# Accumulator (buffered response builder)
# ---------------------------------------------------------------------------


class _ResponseAccumulator:
    """
    Accumulates ``StreamChunk`` sequence into a single ``LLMResponse``.

    Mirrors the accumulation logic inside
    ``AsyncLLMClient.chat_completion()`` so migration from the old
    client to the normalizer is behavior-preserving.
    """

    def __init__(self) -> None:
        self.content: List[str] = []
        self.reasoning: List[str] = []
        self.tool_call_deltas: Dict[int, Dict[str, Any]] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Optional[TokenUsage] = None
        self.provider_metadata: Optional[Dict[str, Any]] = None
        self.started_at: float = time.time()
        self.first_token_at: Optional[float] = None

    # ------------------------------------------------------------------

    def consume(self, chunk: StreamChunk) -> None:
        """Absorb one StreamChunk into the accumulator."""
        now = time.time()

        # Content delta.
        if chunk.content_delta:
            if self.first_token_at is None:
                self.first_token_at = now
            self.content.append(chunk.content_delta)

        # Reasoning delta.
        if chunk.reasoning_delta:
            if self.first_token_at is None:
                self.first_token_at = now
            self.reasoning.append(chunk.reasoning_delta)

        # Tool call deltas — merge by index.
        for delta in chunk.tool_call_deltas or []:
            index = getattr(delta, "index", 0) or 0
            if index not in self.tool_call_deltas:
                self.tool_call_deltas[index] = {
                    "id": getattr(delta, "id", None)
                    or f"call_{uuid.uuid4().hex[:8]}",
                    "name": getattr(delta, "name", None) or "",
                    "arguments": getattr(delta, "arguments", None) or "",
                    "thought_signature": getattr(
                        delta, "thought_signature", None
                    ),
                }
            else:
                slot = self.tool_call_deltas[index]
                incoming_id = getattr(delta, "id", None)
                if incoming_id:
                    slot["id"] = incoming_id
                incoming_name = getattr(delta, "name", None)
                if incoming_name:
                    slot["name"] = incoming_name
                incoming_args = getattr(delta, "arguments", None)
                if incoming_args:
                    slot["arguments"] += incoming_args
                # Late-arriving thought_signature (some adapters emit
                # it only on the COMPLETE event, not on incremental
                # deltas). Any non-None value wins.
                incoming_sig = getattr(delta, "thought_signature", None)
                if incoming_sig:
                    slot["thought_signature"] = incoming_sig

        # Finish reason / usage / provider metadata.
        if chunk.finish_reason is not None:
            self.finish_reason = chunk.finish_reason
        if chunk.usage is not None:
            self.usage = chunk.usage
        if isinstance(chunk.raw, dict):
            self.provider_metadata = dict(chunk.raw)

    # ------------------------------------------------------------------

    def finalize(
        self,
        model: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        Build the final ``LLMResponse`` from the accumulated state.

        When the provider did not return usage during streaming
        (common with LM Studio, older Ollama, and some cloud
        providers that omit the usage chunk), we fall back to local
        estimation via ``peldrun.llm.tokenizer``. This matches the
        pre-M7.1 behavior of ``AsyncLLMClient.chat_completion``.
        """
        completed_at = time.time()
        latency_ms = round((completed_at - self.started_at) * 1000.0, 2)
        ttft_ms: Optional[float] = None
        if self.first_token_at is not None:
            ttft_ms = round(
                (self.first_token_at - self.started_at) * 1000.0, 2
            )

        # Reconstruct tool calls from accumulated deltas.
        tool_calls: List[ToolCall] = []
        for index in sorted(self.tool_call_deltas.keys()):
            slot = self.tool_call_deltas[index]
            raw_args = slot.get("arguments", "")
            parsed_args: Any
            if isinstance(raw_args, str) and raw_args:
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            tool_calls.append(
                ToolCall(
                    id=slot.get("id") or f"call_{index}",
                    name=slot.get("name") or "",
                    arguments=parsed_args,
                    type="function",
                    thought_signature=slot.get("thought_signature"),
                )
            )

        reasoning_text = "".join(self.reasoning) or None
        content_text = "".join(self.content)

        # Fallback usage estimation when the provider did not report
        # usage. The estimated usage is marked with
        # ``source=ESTIMATED`` so downstream accounting can distinguish
        # it from provider-reported values.
        usage = self.usage
        if usage is None:
            usage = _estimate_usage_from_accumulator(
                messages=messages,
                content_text=content_text,
                reasoning_text=reasoning_text,
                tool_calls=tool_calls,
                model=model,
            )

        return LLMResponse(
            content=content_text,
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=self.finish_reason or "stop",
            usage=usage,
            model=model,
            raw=self.provider_metadata,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
        )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


_default_normalizer = StreamNormalizer()


def _estimate_usage_from_accumulator(
    messages: Optional[List[Dict[str, Any]]],
    content_text: str,
    reasoning_text: Optional[str],
    tool_calls: List[ToolCall],
    model: str,
) -> Optional[TokenUsage]:
    """
    Estimate token usage locally when the provider did not send any.

    Mirrors ``AsyncLLMClient._fallback_usage_estimation``. Failures are
    non-fatal — the caller (accounting) will record
    ``usage_source="unknown"`` in that case.
    """
    try:
        from peldrun.llm.tokenizer import estimate_invocation_usage

        est = estimate_invocation_usage(
            messages=messages or [],
            output_text=content_text or None,
            reasoning_text=reasoning_text,
            tool_calls=tool_calls or None,
            model=model,
        )
        return TokenUsage(**est)
    except Exception as exc:
        logger.warning(
            "StreamNormalizer: tokenizer fallback failed for %r: %s",
            model,
            exc,
        )
        return TokenUsage(
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            source=UsageSource.UNKNOWN,
            estimated=True,
            estimation_method="failed_estimation",
        )


async def collect_response(
    events: AsyncIterator[LLMStreamEvent],
    *,
    model: str = "default",
    messages: Optional[List[Dict[str, Any]]] = None,
    on_chunk: Optional[ContentCallback] = None,
    on_thought_chunk: Optional[ThoughtCallback] = None,
) -> LLMResponse:
    """
    Collect an event stream into a full ``LLMResponse`` while also
    firing the per-token callbacks.

    Drop-in replacement for
    ``AsyncLLMClient.chat_completion(stream=True, on_chunk=..., ...)``.

    Parameters
    ----------
    events
        Async iterator produced by ``adapter.stream(request)``.
    model
        The model ID to store in ``LLMResponse.model`` when the
        adapter did not include one in its events.
    messages
        The request messages. Used ONLY by the fallback tokenizer
        when the provider did not return usage. When ``None``, the
        fallback estimation runs with an empty message list — output
        tokens are still estimated correctly, only input tokens are
        underestimated. Pass ``messages`` whenever available.
    on_chunk / on_thought_chunk
        Legacy per-token callbacks. Fired exactly once per delta.
    """
    accumulator = _ResponseAccumulator()
    async for chunk in _default_normalizer.normalize(
        events, on_chunk=on_chunk, on_thought_chunk=on_thought_chunk
    ):
        accumulator.consume(chunk)
    return accumulator.finalize(model=model, messages=messages)


__all__ = [
    "StreamNormalizer",
    "collect_response",
]
