"""
backend/peldrun/events/emitter.py

PELDRUN Core Asynchronous Event Emitter.
Dispatches strictly typed lifecycle events to registered subscribers and SSE streams
with thread-safe and coroutine-safe monotonic sequence numbering, durable RunStore persistence,
and bounded historical replay recovery.
Hardened under PR 5:
- Integrates with RunStore for persistent event logging.
- Supports replay_after querying durable database records.
- Guarantees strict monotonic sequence allocation.
- Enforces single terminal outcome per run.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, List, Optional, Set, Union
from uuid import UUID, uuid4

from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.events.terminal import (
    TerminalKind,
    extract_terminal_kind,
    is_terminal_event_value,
)

logger = logging.getLogger("peldrun.events.emitter")

EventListener = Callable[[PeldrunEvent], Coroutine[Any, Any, None]]


class EventEmitter:
    """Thread-safe and coroutine-safe event broadcaster supporting sequence tracking, replay buffer, and SSE queues."""

    def __init__(self, run_id: Optional[Union[UUID, str]] = None, buffer_capacity: int = 1000) -> None:
        self._run_id: Union[UUID, str] = self._coerce_run_id(run_id) if run_id is not None else uuid4()
        self._sequence_counter: int = 0
        self._last_emitted_sequence: int = 0
        self._seq_lock = threading.Lock()
        self._lock = asyncio.Lock()
        self._listeners: Dict[str, List[EventListener]] = defaultdict(list)
        self._global_listeners: List[EventListener] = []
        self._sse_queues: Set[asyncio.Queue[PeldrunEvent]] = set()
        self._replay_buffer: deque[PeldrunEvent] = deque(maxlen=buffer_capacity)
        self._is_terminated: bool = False
        # [B13 FIX] Diagnostic record of WHICH terminal kind was emitted.
        # ``_is_terminated`` alone loses the semantic distinction
        # between COMPLETED, FAILED, CANCELLED, and INTERRUPTED. Code
        # that needs "how did this run end?" (diagnostics, replay
        # tooling, post-mortem) reads this field rather than re-parsing
        # the last event.
        self._last_terminal_kind: Optional[TerminalKind] = None

    @staticmethod
    def _coerce_run_id(value: Optional[Union[UUID, str]]) -> Union[UUID, str]:
        """Convert string or UUID instance to UUID or canonical string identifier, generating a UUID if None."""
        if value is None:
            return uuid4()
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            trimmed = value.strip()
            try:
                return UUID(trimmed)
            except ValueError:
                return trimmed
        raise ValueError(f"Value must be a valid UUID or run_id string, got {type(value).__name__}")

    @staticmethod
    def _coerce_uuid(value: Optional[Union[UUID, str]]) -> Union[UUID, str]:
        """Backward-compatible alias for _coerce_run_id."""
        return EventEmitter._coerce_run_id(value)

    @property
    def run_id(self) -> Union[UUID, str]:
        """Active execution or session run identifier."""
        return self._run_id

    @run_id.setter
    def run_id(self, val: Optional[Union[UUID, str]]) -> None:
        self._run_id = self._coerce_run_id(val)

    @property
    def current_sequence(self) -> int:
        """Current monotonic sequence index."""
        with self._seq_lock:
            return self._sequence_counter

    @property
    def is_terminated(self) -> bool:
        """Indicates if a terminal lifecycle event has been emitted."""
        return self._is_terminated

    def reset_sequence(self, start: int = 0) -> None:
        """Reset the sequence counter to a designated baseline."""
        with self._seq_lock:
            self._sequence_counter = start
            self._last_emitted_sequence = start

    def create_event(
        self,
        event_type: Union[EventType, str],
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        step: int = 0,
        run_id: Optional[Union[UUID, str]] = None,
        sequence: Optional[int] = None,
        replay: bool = False,
    ) -> PeldrunEvent:
        """
        Factory method producing a strictly valid PeldrunEvent.
        Explicit sequence override is forbidden unless replay=True and sequence is strictly monotonic.
        """
        with self._seq_lock:
            if sequence is not None:
                if not replay:
                    raise ValueError(
                        "Explicit sequence assignment is forbidden outside replay mode. Pass replay=True to import historical events."
                    )
                if sequence <= self._sequence_counter:
                    raise ValueError(
                        f"Replay sequence {sequence} violates monotonicity; current sequence counter is {self._sequence_counter}."
                    )
                self._sequence_counter = sequence
                assigned_sequence = sequence
            else:
                self._sequence_counter += 1
                assigned_sequence = self._sequence_counter

        resolved_type = EventType(event_type) if not isinstance(event_type, EventType) else event_type
        target_run_id = self._coerce_run_id(run_id) if run_id is not None else self._run_id

        return PeldrunEvent(
            version=1,
            event_id=uuid4(),
            sequence=assigned_sequence,
            run_id=target_run_id,
            timestamp=time.time(),
            step=step,
            type=resolved_type,
            payload=payload or {},
            metadata=metadata or {},
        )

    def subscribe(self, event_type: Union[EventType, str], listener: EventListener) -> None:
        """Register an async callback for a specific event type."""
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if listener not in self._listeners[key]:
            self._listeners[key].append(listener)

    def subscribe_all(self, listener: EventListener) -> None:
        """Register an async callback to receive all dispatched events."""
        if listener not in self._global_listeners:
            self._global_listeners.append(listener)

    def unsubscribe(self, event_type: Union[EventType, str], listener: EventListener) -> None:
        """Unregister a listener from a specific type and global list."""
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if listener in self._listeners[key]:
            self._listeners[key].remove(listener)
        if listener in self._global_listeners:
            self._global_listeners.remove(listener)

    async def emit(self, event: PeldrunEvent, allow_external_sequence: bool = False) -> None:
        """
        Publish an event across matching listeners, historical replay buffer, and active SSE queues.
        Guarantees thread-safe and coroutine-safe strictly monotonic sequence allocation.
        Persists event durably into RunStore and enforces a single canonical terminal outcome per run.
        """
        async with self._lock:
            key = event.type.value if hasattr(event.type, "value") else str(event.type)

            # [B13 FIX] Terminal guard driven by the SINGLE source of
            # truth in ``events/terminal.py``.
            #
            # Pre-fix behavior: the check was hardcoded to
            # ``EventType.FINAL.value`` and ``EventType.ERROR.value``
            # plus a ``getattr(EventType, "CANCELLED", None)`` that
            # returned ``None`` (EventType had no CANCELLED member).
            # Therefore RUN_CANCELLED (value ``"run_cancelled"``) was
            # NOT protected: a cancellation event followed by a FINAL
            # or ERROR would produce TWO terminal events on the same
            # run. That is the B13 lifecycle bug.
            #
            # Post-fix: ``is_terminal_event_value`` consults
            # ``TERMINAL_EVENT_VALUES`` which includes every terminal
            # spelling — final, error, run_completed, cancelled,
            # run_cancelled, interrupted. Whatever name an emitter
            # chooses, the guard fires exactly once.
            if is_terminal_event_value(key):
                if self._is_terminated:
                    logger.warning(
                        "Ignoring duplicate terminal event '%s' for run '%s' "
                        "(already terminated as '%s').",
                        key,
                        self._run_id,
                        self._last_terminal_kind.value
                        if self._last_terminal_kind is not None
                        else "unknown",
                    )
                    return
                self._is_terminated = True
                self._last_terminal_kind = extract_terminal_kind(event)

            with self._seq_lock:
                if event.sequence <= 0:
                    self._sequence_counter += 1
                    event.sequence = self._sequence_counter
                elif not allow_external_sequence:
                    if event.sequence <= self._last_emitted_sequence:
                        self._sequence_counter = max(self._sequence_counter, self._last_emitted_sequence) + 1
                        event.sequence = self._sequence_counter
                    elif event.sequence > self._sequence_counter:
                        self._sequence_counter = event.sequence
                else:
                    if event.sequence > self._sequence_counter:
                        self._sequence_counter = event.sequence

                self._last_emitted_sequence = max(self._last_emitted_sequence, event.sequence)

            targets = list(self._listeners.get(key, [])) + list(self._global_listeners)
            queues = list(self._sse_queues)

            # Store in historical replay buffer
            self._replay_buffer.append(event)

            # Persist event into durable RunStore
            try:
                from peldrun.runtime.store import get_run_store

                store = get_run_store()
                store._sync_append_event(event)
            except Exception as store_err:
                logger.debug("Failed persisting event %s to RunStore: %s", event.event_id, store_err)

        for listener in targets:
            try:
                res = listener(event)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as listener_err:
                # [B13 DIAGNOSTIC] Log the listener failure with full
                # correlation context so operators can trace lost
                # events back to the specific subscriber. Pre-fix
                # behavior was ``except Exception: pass`` — a broken
                # listener silently dropped events and the run
                # appeared to proceed without any indication of the
                # dropped dispatch.
                #
                # CRITICAL INVARIANT: the listener failure MUST NOT
                # break the emitter. The event was already persisted
                # to RunStore (above) and appended to the replay
                # buffer, so subsequent reads recover it. Logging is
                # the only addition — the exception is still
                # swallowed from the dispatch loop.
                logger.warning(
                    "Event listener failed for run=%s event=%s type=%s: %s",
                    self._run_id,
                    event.event_id,
                    key,
                    listener_err,
                    exc_info=True,
                )

        for q in queues:
            await q.put(event)

    async def replay_after(self, after_sequence: int = 0) -> List[PeldrunEvent]:
        """
        Fetch previously published events occurring strictly after sequence number.
        Queries durable RunStore first for crash-resilient replay, falling back to memory.
        """
        try:
            from peldrun.runtime.store import get_run_store

            store = get_run_store()
            raw_events = await store.get_events_after(str(self._run_id), sequence=after_sequence)
            if raw_events:
                events: List[PeldrunEvent] = []
                for item in raw_events:
                    events.append(
                        PeldrunEvent(
                            version=1,
                            event_id=item["event_id"],
                            run_id=item["run_id"],
                            sequence=item["sequence"],
                            step=item["step"],
                            type=item["type"],
                            payload=item["payload"],
                            metadata=item["metadata"],
                            timestamp=item["timestamp"],
                        )
                    )
                return events
        except Exception as ex:
            logger.debug("Failed querying RunStore for emitter replay: %s", ex)

        async with self._lock:
            return [ev for ev in self._replay_buffer if ev.sequence > after_sequence]

    async def ensure_terminated(self, default_error: Optional[str] = None) -> None:
        """Enforce terminal event guarantee if execution halts unexpectedly without final status."""
        if not self._is_terminated:
            err_msg = default_error or "Execution terminated unexpectedly without final status."
            await self.emit_error(error=err_msg)

    async def astream_sse(self) -> AsyncIterator[str]:
        """Asynchronously yield formatted SSE string frames as events occur.

        [B13 FIX] The loop closes on ANY terminal event, not just
        FINAL and ERROR. Pre-fix behavior: a cancelled run emitted
        ``RUN_CANCELLED`` and left the SSE socket open forever — the
        frontend spinner never resolved, and the browser kept the
        connection for minutes until an idle timeout on the proxy
        side closed it. The user's experience was "the run hung".

        Post-fix: ``is_terminal_event_value`` recognizes every
        terminal spelling (final, error, run_completed, cancelled,
        run_cancelled, interrupted), so the SSE loop closes
        deterministically for every outcome.
        """
        q: asyncio.Queue[PeldrunEvent] = asyncio.Queue()
        self._sse_queues.add(q)
        try:
            while True:
                event = await q.get()
                yield event.to_sse()
                # ``is_terminal_event_value`` accepts either an
                # ``EventType`` or its ``.value`` string. Passing the
                # event's ``type`` directly is cleaner and avoids the
                # ``hasattr(..., "value")`` dance.
                if is_terminal_event_value(event.type):
                    break
        finally:
            self._sse_queues.discard(q)

    # Convenience domain helpers with automatic envelope packaging
    async def emit_snapshot(self, snapshot: Dict[str, Any], step: int = 0, **kwargs: Any) -> None:
        payload = {"snapshot": snapshot, **kwargs}
        event = self.create_event(EventType.SNAPSHOT, payload=payload, step=step)
        await self.emit(event)

    async def emit_step_start(self, step_number: int, **kwargs: Any) -> None:
        payload = {"step_number": step_number, **kwargs}
        event = self.create_event(EventType.STEP_START, payload=payload, step=step_number)
        await self.emit(event)

    async def emit_thought(self, thought: str = "", step: int = 0, **kwargs: Any) -> None:
        payload = {"thought": thought, **kwargs}
        event = self.create_event(EventType.THOUGHT, payload=payload, step=step)
        await self.emit(event)

    async def emit_agent_activity(self, message: str, phase: str = "general", step: int = 0, **kwargs: Any) -> None:
        payload = {"message": message, "phase": phase, **kwargs}
        event = self.create_event(EventType.AGENT_ACTIVITY, payload=payload, step=step)
        await self.emit(event)

    async def emit_tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        step: int = 0,
        **kwargs: Any,
    ) -> None:
        payload = {"tool_name": tool_name, "arguments": arguments, "tool_call_id": tool_call_id, **kwargs}
        event = self.create_event(EventType.TOOL_CALL, payload=payload, step=step)
        await self.emit(event)

    async def emit_tool_retry(
        self,
        tool_name: str,
        tool_call_id: str = "",
        attempt: int = 1,
        max_attempts: int = 3,
        error: str = "",
        delay_seconds: float = 0.0,
        step: int = 0,
        **kwargs: Any,
    ) -> None:
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error": error,
            "delay_seconds": delay_seconds,
            **kwargs,
        }
        event = self.create_event(EventType.TOOL_RETRY, payload=payload, step=step)
        await self.emit(event)

    async def emit_observation(
        self,
        output: Any,
        tool_name: str = "",
        tool_call_id: str = "",
        exit_code: int = 0,
        is_error: bool = False,
        step: int = 0,
        **kwargs: Any,
    ) -> None:
        payload = {
            "output": output,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "exit_code": exit_code,
            "is_error": is_error,
            **kwargs,
        }
        event = self.create_event(EventType.OBSERVATION, payload=payload, step=step)
        await self.emit(event)

    async def emit_step_end(self, step_number: int, elapsed_seconds: float = 0.0, **kwargs: Any) -> None:
        payload = {"step_number": step_number, "elapsed_seconds": elapsed_seconds, **kwargs}
        event = self.create_event(EventType.STEP_END, payload=payload, step=step_number)
        await self.emit(event)

    async def emit_ask_human(self, question: str, options: Optional[List[str]] = None, step: int = 0, **kwargs: Any) -> None:
        payload = {"question": question, "options": options or [], **kwargs}
        event = self.create_event(EventType.ASK_HUMAN, payload=payload, step=step)
        await self.emit(event)

    async def emit_error(self, error: str = "", step: int = 0, **kwargs: Any) -> None:
        payload = {"error": error, **kwargs}
        event = self.create_event(EventType.ERROR, payload=payload, step=step)
        await self.emit(event)

    async def emit_final(self, output: str = "", step: int = 0, **kwargs: Any) -> None:
        payload = {"output": output, **kwargs}
        event = self.create_event(EventType.FINAL, payload=payload, step=step)
        await self.emit(event)

    # ------------------------------------------------------------------
    # [B13 FIX] Canonical terminal emitters
    # ------------------------------------------------------------------

    async def emit_cancelled(
        self,
        reason: str = "cancelled_by_operator",
        step: int = 0,
        **kwargs: Any,
    ) -> None:
        """Emit the canonical CANCELLED terminal event.

        Uses ``EventType.CANCELLED`` (the canonical spelling) rather
        than ``EventType.RUN_CANCELLED`` (the historical alias). Both
        are recognized by ``is_terminal_event_value`` and both close
        the SSE stream, but new code SHOULD prefer the canonical name
        so that historical and current events are distinguishable in
        the store.

        The ``reason`` field is exposed as ``payload["reason"]`` so
        the UI can display WHY the run was cancelled (operator,
        timeout, budget exceeded, ...).
        """
        payload = {"status": "cancelled", "reason": reason, **kwargs}
        event = self.create_event(EventType.CANCELLED, payload=payload, step=step)
        await self.emit(event)

    async def emit_interrupted(
        self,
        reason: str = "interrupted_by_restart",
        step: int = 0,
        **kwargs: Any,
    ) -> None:
        """Emit the canonical INTERRUPTED terminal event.

        Used by the Phase G2 ``RecoveryManager`` when a crash was
        detected and the run will NOT be resumed (or when resume
        itself failed). Until Phase G2 ships, ``runner.py`` may still
        emit ``ERROR`` with ``error_code="INTERRUPTED_BY_RESTART"``
        — both close the SSE stream and both are recognized as
        terminal.
        """
        payload = {"status": "interrupted", "reason": reason, **kwargs}
        event = self.create_event(EventType.INTERRUPTED, payload=payload, step=step)
        await self.emit(event)

    # ------------------------------------------------------------------
    # [B15 REMNANT FIX] Per-run reset for agent reuse
    # ------------------------------------------------------------------

    def reset_for_new_run(
        self,
        run_id: Optional[Union[UUID, str]] = None,
    ) -> Union[UUID, str]:
        """Reset all per-run state so the emitter can serve a new run.

        Context
        -------
        ``ToolCallAgent._reset_run_state()`` (see
        ``agents/tool_call_agent.py``) resets agent-local state before
        each ``run_task()`` call, but it does NOT reset the emitter's
        terminal flag. The pre-Phase-F emitter had no way to clear
        ``_is_terminated``, so calling ``agent.run_task()`` twice on
        the SAME agent (reusing the SAME emitter) would silently drop
        the second run's FINAL/ERROR/CANCELLED event — the first
        terminal event from run #1 was still "winning". This was the
        last remnant of B15.

        Post-Phase-F: this method resets terminal state and, when a
        new ``run_id`` is provided, also resets the sequence counter
        (safe because the durable store keys sequence by ``run_id``,
        so a new run_id starts a fresh monotonic scope).

        Parameters
        ----------
        run_id
            Optional new run identifier. When provided, the emitter
            adopts it and resets ``_sequence_counter`` to 0 (a new
            run is a new sequence scope). When omitted, a fresh UUID
            is generated — the emitter MUST NOT silently reuse the
            old run_id with a cleared sequence counter, because that
            would corrupt the store's monotonicity invariant.

        Returns
        -------
        Union[UUID, str]
            The new ``run_id`` in effect after the reset. Callers
            that persist the emitter's events (``RunStore``,
            ``CheckpointManager``) MUST use this value, not the
            pre-reset one.

        Idempotency
        -----------
        Calling twice generates a new UUID each time (if no explicit
        run_id is passed). The emitter's state is always clean after
        the call. There is no "already reset" error state — every
        call is a fresh reset.

        Not covered
        -----------
        Active SSE subscribers are NOT closed by this method. The
        caller (usually ``AgentRunner``) is responsible for
        cancelling any in-flight SSE streams BEFORE calling reset,
        or the streams will receive events from BOTH the old and new
        runs (interleaved, never end).

        The ``_listeners`` and ``_global_listeners`` collections are
        ALSO preserved — listeners are configured once for the
        emitter's lifetime and should survive run boundaries. A
        listener that needs per-run state should key on
        ``event.run_id``.
        """
        resolved_run_id: Union[UUID, str]
        if run_id is not None:
            resolved_run_id = self._coerce_run_id(run_id)
        else:
            resolved_run_id = uuid4()

        self._run_id = resolved_run_id
        self._is_terminated = False
        self._last_terminal_kind = None
        self._replay_buffer.clear()
        with self._seq_lock:
            self._sequence_counter = 0
            self._last_emitted_sequence = 0

        logger.debug(
            "EventEmitter reset for new run_id=%s",
            resolved_run_id,
        )
        return resolved_run_id


__all__ = ["EventEmitter", "EventListener"]
