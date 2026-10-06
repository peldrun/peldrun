"""
backend/peldrun/events/bus.py

PELDRUN Core Event Bus Architecture.
Provides centralized pub/sub mechanics, monotonic sequence allocation,
durable persistence integration with RunStore, and disconnect recovery replay buffer.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Union
from uuid import UUID

from peldrun.events.schema import EventType, PeldrunEvent

logger = logging.getLogger("peldrun.events.bus")

SubscriberCallback = Callable[[PeldrunEvent], Awaitable[None]]


class EventReplayBuffer:
    """Bounded, thread-safe in-memory buffer storing ordered execution events."""

    def __init__(self, max_capacity: int = 1000):
        self._max_capacity = max_capacity
        self._buffer: Deque[PeldrunEvent] = deque(maxlen=max_capacity)
        self._lock = asyncio.Lock()

    async def append(self, event: PeldrunEvent) -> None:
        async with self._lock:
            self._buffer.append(event)

    async def get_after(self, after_sequence: int) -> List[PeldrunEvent]:
        async with self._lock:
            return [ev for ev in self._buffer if ev.sequence > after_sequence]

    async def clear(self) -> None:
        async with self._lock:
            self._buffer.clear()


class EventBus:
    """
    Central event distribution broker supporting sequence ownership,
    durable RunStore persistence, and replay recovery.
    """

    def __init__(self, run_id: Union[UUID, str], buffer_capacity: int = 1000):
        self.run_id = str(run_id)
        self._sequence_counter: int = 0
        self._subscribers: List[SubscriberCallback] = []
        self._typed_subscribers: Dict[EventType, List[SubscriberCallback]] = {}
        self._replay_buffer = EventReplayBuffer(max_capacity=buffer_capacity)
        self._seq_lock = asyncio.Lock()

    async def allocate_sequence(self) -> int:
        """Monotonically allocate unique event sequence number."""
        async with self._seq_lock:
            self._sequence_counter += 1
            return self._sequence_counter

    def subscribe(self, callback: SubscriberCallback, event_type: Optional[EventType] = None) -> None:
        """Subscribe an asynchronous handler to all events or a specific event type."""
        if event_type is None:
            if callback not in self._subscribers:
                self._subscribers.append(callback)
        else:
            self._typed_subscribers.setdefault(event_type, [])
            if callback not in self._typed_subscribers[event_type]:
                self._typed_subscribers[event_type].append(callback)

    def unsubscribe(self, callback: SubscriberCallback, event_type: Optional[EventType] = None) -> None:
        """Detach an asynchronous handler."""
        if event_type is None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)
        else:
            if event_type in self._typed_subscribers and callback in self._typed_subscribers[event_type]:
                self._typed_subscribers[event_type].remove(callback)

    async def publish(self, event: PeldrunEvent) -> None:
        """
        Store event durably in RunStore, append to replay buffer,
        and dispatch across all matching subscribers.
        """
        # Ensure sequence monotonicity
        if event.sequence <= 0:
            event.sequence = await self.allocate_sequence()
        else:
            async with self._seq_lock:
                if event.sequence > self._sequence_counter:
                    self._sequence_counter = event.sequence

        # Persist event into durable RunStore
        try:
            from peldrun.runtime.store import get_run_store

            store = get_run_store()
            await store.append_event(event)
        except Exception as store_err:
            logger.debug("Failed saving event %s to RunStore: %s", event.event_id, store_err)

        await self._replay_buffer.append(event)

        targets = list(self._subscribers)
        if event.type in self._typed_subscribers:
            targets.extend(self._typed_subscribers[event.type])

        if targets:
            await asyncio.gather(*(cb(event) for cb in targets), return_exceptions=True)

    async def replay_after(self, sequence: int = 0) -> List[PeldrunEvent]:
        """
        Fetch previously published events strictly occurring after sequence number.
        Queries durable RunStore first for crash-resilient replay, falling back to memory.
        """
        try:
            from peldrun.runtime.store import get_run_store

            store = get_run_store()
            raw_events = await store.get_events_after(self.run_id, sequence=sequence)
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
            logger.debug("Failed querying RunStore for bus replay: %s", ex)

        return await self._replay_buffer.get_after(sequence)


__all__ = ["EventBus", "EventReplayBuffer", "SubscriberCallback"]