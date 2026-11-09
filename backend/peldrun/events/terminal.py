# Path: backend/peldrun/events/terminal.py
"""
PELDRUN Core Terminal Event Authority.

Centralizes the semantic definition of a "terminal event" — the single
event that marks the immutable end of a run — and provides the
primitives that every runtime layer uses to guarantee the "exactly one
terminal event per run" contract.

Design invariants
-----------------

1. **Four terminal kinds, one authority.**
   Every run ends in exactly one of:

       COMPLETED   — the task finished successfully.
       FAILED      — the task aborted due to an error.
       CANCELLED   — the operator cancelled the run.
       INTERRUPTED — the process died mid-run and was recovered.

   There is no fifth kind. ``describe_terminal_kind()`` is the single
   source of the human-readable label for each, so UI layers never
   invent their own translations.

2. **Forward and backward compatible.**
   ``EventType`` today defines ``FINAL``, ``ERROR``, and
   ``RUN_CANCELLED`` — it does NOT yet define ``CANCELLED`` or
   ``INTERRUPTED``. Rather than requiring Phase F to edit this module
   when those statuses are added, the ``_EVENT_VALUE_TO_KIND`` mapping
   ALREADY includes the future canonical strings (``"cancelled"``,
   ``"interrupted"``) alongside their current aliases
   (``"run_cancelled"``, ``"run_completed"``). Phase F adds the enum
   members and the mapping is picked up automatically.

3. **``event_type_for_kind()`` is preferred-canonical.**
   When a kind has a dedicated ``EventType`` member (Phase F+), it is
   returned. Until then, a shim maps the kind to the closest available
   event type (``TerminalKind.CANCELLED`` → ``EventType.RUN_CANCELLED``,
   ``TerminalKind.INTERRUPTED`` → ``EventType.ERROR``). Callers never
   hardcode this mapping — they call the helper.

4. **String values, not enum members, drive the terminal check.**
   ``is_terminal_event_value()`` accepts ``str`` or ``EventType``.
   Using strings internally means a future EventType addition is a
   no-op for every consumer; the emitter's SSE-closure logic
   (Phase F) simply reads ``TERMINAL_EVENT_VALUES``.

5. **One claim per run, ever.**
   ``TerminalGuard`` models the "terminal slot" as a per-run token.
   ``try_claim()`` succeeds exactly once; subsequent calls return
   False. The guard is intentionally sync-only — the caller
   (EventEmitter.emit) already holds an ``asyncio.Lock``, so the guard
   needs no internal synchronization. The guard is a semantic helper;
   the emitter's own terminal flag remains the enforcement point.

6. **The bridge to lifecycle.**
   ``terminal_kind_for_state()`` maps an ``ExecutionState`` to its
   ``TerminalKind`` (or None if not terminal). This is the ONE place
   that knows about ``state.metadata["interrupted"]``; every consumer
   that needs to decide "how should this run's terminal event look?"
   calls this helper rather than inspecting metadata directly.

See also
--------

    * ``backend/peldrun/events/schema.py``   — EventType, PeldrunEvent
    * ``backend/peldrun/events/emitter.py``  — the sole event dispatcher
                                                (Phase F will wire
                                                its terminal check to
                                                this module)
    * ``backend/peldrun/engine/lifecycle.py`` — the state-side authority
                                                for terminal *status*;
                                                this module is the
                                                event-side counterpart
    * ``backend/peldrun/engine/state.py``     — ExecutionState / Status
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Union

# [CYCLE FIX] ``ExecutionState`` is used ONLY as a type hint and
# ``ExecutionStatus`` is used ONLY inside ``terminal_kind_for_state()``.
# Importing either at module load triggers:
#
#   terminal.py
#     → engine.state
#       → engine/__init__.py
#         → engine.graph
#           → events.emitter       ← half-loaded
#             → events.terminal    ← CYCLE
#
# Breaking this cycle:
#   * ``ExecutionState`` moves under ``TYPE_CHECKING`` (type hints only,
#     no runtime dependency).
#   * ``ExecutionStatus`` moves into ``terminal_kind_for_state()`` as a
#     lazy import — evaluated on first call, by which time ``engine.state``
#     is guaranteed to be fully initialized.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from peldrun.engine.state import ExecutionState

from peldrun.events.schema import EventType, PeldrunEvent

logger = logging.getLogger("peldrun.events.terminal")


# ---------------------------------------------------------------------------
# Canonical terminal kinds
# ---------------------------------------------------------------------------


class TerminalKind(str, Enum):
    """
    Semantic kind of a terminal event.

    There are exactly four. This enum IS the authority; no other layer
    may define its own terminal-status taxonomy.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


#: The set of all legal terminal kinds. Useful for validation and
#: exhaustiveness checks.
TERMINAL_KINDS: FrozenSet[TerminalKind] = frozenset(TerminalKind)


# ---------------------------------------------------------------------------
# Event-value mapping
# ---------------------------------------------------------------------------
#
# The keys are ``EventType.<member>.value`` strings. The values are the
# canonical kind. The mapping includes values that do NOT yet exist in
# EventType (``"cancelled"``, ``"interrupted"``) so that when Phase F
# adds those members, this module needs NO edit.
#
# Some values are aliases:
#
#   ``"run_completed"``  → COMPLETED   (existing EventType.RUN_COMPLETED)
#   ``"run_cancelled"``  → CANCELLED   (existing EventType.RUN_CANCELLED)
#
# These aliases are not removed when the canonical values are added —
# historical events stored in ``run_store`` still use them, and the
# terminal detector MUST recognize them on replay.

_EVENT_VALUE_TO_KIND: Dict[str, TerminalKind] = {
    # Completed
    "final": TerminalKind.COMPLETED,
    "run_completed": TerminalKind.COMPLETED,  # alias — EventType.RUN_COMPLETED
    # Failed
    "error": TerminalKind.FAILED,
    # Cancelled
    "cancelled": TerminalKind.CANCELLED,      # future canonical (Phase F)
    "run_cancelled": TerminalKind.CANCELLED,  # existing EventType.RUN_CANCELLED
    # Interrupted
    "interrupted": TerminalKind.INTERRUPTED,  # future (Phase G2)
}


#: The complete set of event-type VALUES that count as terminal.
#:
#: EventEmitter.astream_sse() uses this to decide when to close the
#: stream. The historical check was hardcoded to ``{"final", "error"}``
#: and therefore left SSE sockets open forever after a CANCELLED event
#: — the exact B13 bug the PELDRUN execution repair plan describes.
TERMINAL_EVENT_VALUES: FrozenSet[str] = frozenset(_EVENT_VALUE_TO_KIND.keys())


# ---------------------------------------------------------------------------
# String / enum predicates
# ---------------------------------------------------------------------------


def is_terminal_event_value(value: Union[EventType, str, None]) -> bool:
    """
    Return True if ``value`` (an ``EventType``, an ``EventType``'s
    ``.value`` string, or ``None``) represents a terminal event type.

    ``None`` is explicitly handled so that callers pattern-matching on
    optional event types do not need a redundant guard.
    """
    if value is None:
        return False
    if isinstance(value, EventType):
        return value.value in TERMINAL_EVENT_VALUES
    return str(value) in TERMINAL_EVENT_VALUES


def is_terminal_event(event: Optional[PeldrunEvent]) -> bool:
    """
    Return True if ``event`` is a terminal event.

    Safe with ``None`` — a missing event is, by definition, not
    terminal (it never happened).
    """
    if event is None:
        return False
    event_type = getattr(event, "type", None)
    if event_type is None:
        return False
    return is_terminal_event_value(event_type)


def extract_terminal_kind(
    event: Optional[PeldrunEvent],
) -> Optional[TerminalKind]:
    """
    Return the ``TerminalKind`` for ``event``, or ``None`` when the
    event is not terminal.

    Handles the four terminal event types plus their aliases. Unknown
    event types return ``None`` — never raise.
    """
    if event is None:
        return None
    event_type = getattr(event, "type", None)
    if event_type is None:
        return None
    key = event_type.value if isinstance(event_type, EventType) else str(event_type)
    return _EVENT_VALUE_TO_KIND.get(key)


# ---------------------------------------------------------------------------
# Kind → EventType resolution
# ---------------------------------------------------------------------------


def event_type_for_kind(kind: TerminalKind) -> EventType:
    """
    Return the ``EventType`` that represents ``kind`` in the CURRENT
    schema version.

    Preference order:

      1. If a dedicated ``EventType`` member exists (Phase F+), return
         it. E.g., ``TerminalKind.CANCELLED`` prefers
         ``EventType.CANCELLED`` when it exists.
      2. Otherwise, return the historical shim:
         ``TerminalKind.CANCELLED`` → ``EventType.RUN_CANCELLED``,
         ``TerminalKind.INTERRUPTED`` → ``EventType.ERROR``.

    Consumers that emit a terminal event MUST call this helper instead
    of hardcoding ``EventType.FINAL`` (which would silently skip
    cancellation and interruption semantics).
    """
    if kind == TerminalKind.COMPLETED:
        return EventType.FINAL

    if kind == TerminalKind.FAILED:
        return EventType.ERROR

    if kind == TerminalKind.CANCELLED:
        # Prefer the canonical EventType.CANCELLED once Phase F adds it.
        canonical = getattr(EventType, "CANCELLED", None)
        if canonical is not None:
            return canonical
        # Fall back to the historical alias.
        return EventType.RUN_CANCELLED

    if kind == TerminalKind.INTERRUPTED:
        canonical = getattr(EventType, "INTERRUPTED", None)
        if canonical is not None:
            return canonical
        # Shim: until Phase G2 introduces INTERRUPTED as a first-class
        # EventType, an interrupted run surfaces as ERROR at the event
        # layer. The state layer already records the more precise
        # ``error_code="INTERRUPTED_BY_RESTART"`` so downstream
        # consumers can still distinguish the two.
        return EventType.ERROR

    raise ValueError(f"Unknown TerminalKind: {kind!r}")


# ---------------------------------------------------------------------------
# State → kind bridge
# ---------------------------------------------------------------------------


def terminal_kind_for_state(
    state: Optional["ExecutionState"],
) -> Optional[TerminalKind]:
    """
    Return the ``TerminalKind`` that corresponds to the run's current
    state, or ``None`` if the run has not reached a terminal status.

    This is the ONE function that interprets
    ``state.metadata["interrupted"]``. Every consumer that needs to
    decide "which terminal event should this run emit?" calls this
    helper rather than inspecting metadata directly.

    Mapping:

      ExecutionStatus.COMPLETED           → COMPLETED
      ExecutionStatus.FAILED              → FAILED
                                            (unless ``interrupted`` is
                                            set on metadata, in which
                                            case → INTERRUPTED)
      ExecutionStatus.CANCELLED           → CANCELLED
      anything else                       → None

    Note: ``interrupted`` short-circuits the FAILED branch because the
    Phase A shim in ``lifecycle.mark_interrupted()`` records interrupted
    runs as FAILED with ``error_code="INTERRUPTED_BY_RESTART"``. That
    keeps the FSM valid while preserving the semantic distinction for
    the UI. Phase G2 will add a real ``INTERRUPTED`` status; the mapping
    below continues to work unchanged.
    """
    if state is None:
        return None

    # [CYCLE FIX] Lazy import — ``engine.state`` is fully loaded by the
    # time any caller invokes this function (see module-level comment).
    from peldrun.engine.state import ExecutionStatus

    status = state.status
    if status == ExecutionStatus.COMPLETED:
        return TerminalKind.COMPLETED
    if status == ExecutionStatus.CANCELLED:
        return TerminalKind.CANCELLED
    if status == ExecutionStatus.FAILED:
        if state.metadata.get("interrupted"):
            return TerminalKind.INTERRUPTED
        return TerminalKind.FAILED
    return None


# ---------------------------------------------------------------------------
# Human-readable labels
# ---------------------------------------------------------------------------


_KIND_LABELS: Dict[TerminalKind, str] = {
    TerminalKind.COMPLETED: "Completed",
    TerminalKind.FAILED: "Failed",
    TerminalKind.CANCELLED: "Cancelled",
    TerminalKind.INTERRUPTED: "Interrupted",
}


def describe_terminal_kind(kind: TerminalKind) -> str:
    """
    Return the human-readable label for ``kind``.

    The single source of these labels — UI layers, log formatters, and
    error messages all read from here so users never see two different
    spellings of the same outcome.
    """
    return _KIND_LABELS.get(kind, kind.value.title())


# ---------------------------------------------------------------------------
# TerminalEvent — the pre-emission intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalEvent:
    """
    A terminal event intent, before emission.

    Emitters that want to reason about "which terminal event will I
    emit?" construct a ``TerminalEvent`` first, then hand it to the
    dispatch layer. This gives a typed handle for the "the run is
    over" concept without forcing callers to know the specific
    ``EventType`` value.

    Immutable: frozen dataclass. Two ``TerminalEvent`` instances with
    the same fields are equal, which lets tests assert on the shape of
    the terminal event without re-implementing the mapping logic.
    """

    kind: TerminalKind
    payload: Dict[str, Any] = field(default_factory=dict)
    step: int = 0

    def event_type(self) -> EventType:
        """Return the ``EventType`` that this intent will become."""
        return event_type_for_kind(self.kind)

    def label(self) -> str:
        """Return the human-readable label for this intent's kind."""
        return describe_terminal_kind(self.kind)


# ---------------------------------------------------------------------------
# TerminalGuard — one claim per run
# ---------------------------------------------------------------------------


class TerminalGuard:
    """
    Enforces the "exactly one terminal event per run" contract.

    A single ``TerminalGuard`` instance may track MANY runs — the map
    is keyed by ``run_id``. Callers who need a per-run guard should
    scope their guard to the run's lifetime (e.g. inside the emitter
    or the runner), not share one across unrelated runs.

    Not internally synchronized: the caller (usually EventEmitter,
    which already holds ``self._lock``) provides the mutual exclusion.
    Adding a second lock here would be redundant at best and
    deadlock-prone at worst if the two locks were ever taken in
    different orders.

    Usage::

        guard = TerminalGuard()
        if guard.try_claim(run_id, TerminalKind.CANCELLED):
            await emitter.emit(terminal_event)
        else:
            # A terminal event was already emitted; the run is over.
            pass
    """

    def __init__(self) -> None:
        self._terminated: Dict[str, TerminalKind] = {}

    def try_claim(self, run_id: str, kind: TerminalKind) -> bool:
        """
        Attempt to claim the terminal slot for ``run_id``.

        Returns ``True`` on the FIRST call for a given run, ``False``
        on every subsequent call. The kind recorded on the first claim
        is authoritative; a later ``try_claim`` with a different kind
        does NOT override it (the caller can retrieve the recorded kind
        via ``get_kind``).
        """
        if run_id in self._terminated:
            return False
        self._terminated[run_id] = kind
        return True

    def get_kind(self, run_id: str) -> Optional[TerminalKind]:
        """Return the claimed kind for ``run_id``, or ``None`` if unclaimed."""
        return self._terminated.get(run_id)

    def has_terminated(self, run_id: str) -> bool:
        """Return True if the terminal slot for ``run_id`` was claimed."""
        return run_id in self._terminated

    def release(self, run_id: str) -> None:
        """
        Release the claim for ``run_id``.

        Reserved for testing and recovery tooling. Production code
        must NOT call this — the "one terminal per run" invariant is
        meant to hold for the full lifetime of the process.
        """
        self._terminated.pop(run_id, None)

    def snapshot(self) -> Dict[str, TerminalKind]:
        """
        Return a shallow copy of the internal claim map.

        Used by diagnostics and test assertions; production code does
        not need this.
        """
        return dict(self._terminated)


__all__ = [
    # TerminalKind enum + set
    "TerminalKind",
    "TERMINAL_KINDS",
    # Event-value mapping
    "TERMINAL_EVENT_VALUES",
    # String / enum predicates
    "is_terminal_event_value",
    "is_terminal_event",
    "extract_terminal_kind",
    # Kind → EventType resolution
    "event_type_for_kind",
    # State → kind bridge
    "terminal_kind_for_state",
    # Human-readable label
    "describe_terminal_kind",
    # Pre-emission intent
    "TerminalEvent",
    # One-claim-per-run helper
    "TerminalGuard",
]
