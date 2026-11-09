# Path: backend/peldrun/runtime/ports.py
"""
PELDRUN Core Runtime Ports.

Defines the four Protocol interfaces that decouple PELDRUN's runtime
from its concrete persistence, tool-resolution, human-input, and
workspace implementations. These protocols are the contracts that
Phase G (Recovery, HITL durability) and Phase H (Multi-Agent, MCP,
Web boundary) depend on.

The ports exist so that no runtime file needs to import
``peldrun.runtime.store``, ``omweb.adapters``, or any other concrete
implementation. The runner, the recovery manager, the executor, and
every tool that persists durable state hold a reference to the port
interface and never see the underlying backend.

Design invariants
-----------------

1. **Zero external dependencies.**
   This module imports only stdlib. The four ``TYPE_CHECKING``
   imports are PELDRUN-internal modules that do NOT import ``ports``
   themselves, so there is no cycle. ``ports.py`` never imports
   ``peldrun.runtime.store``, ``omweb.*``, or any concrete backend.

2. **Async for I/O, sync for in-memory.**
   Every method that touches the filesystem, a database, or the
   network is ``async def``. Methods that are pure in-memory lookups
   (``has_tool``, ``resolve_path``, ``list_available_tools``) are
   synchronous. This split maximizes flexibility: a sync
   implementation is trivially wrapped in ``asyncio.to_thread`` by
   the caller, but a sync method that secretly blocks inside a
   supposedly-async interface is impossible to detect and impossible
   to fix from the caller's side.

3. **Null implementations are first-class.**
   ``NullPersistencePort``, ``NullHumanInputPort``, and
   ``NullWorkspacePort`` provide safe, well-defined behavior for
   contexts where the real backend is deliberately absent (stateless
   sub-agents, isolated tests, HITL-free delegated runs). They
   satisfy the Protocol structurally — ``isinstance(null, Port)``
   returns True — so callers accept them without special-casing.
   The null implementations document the contract through their
   no-op / raise-in-the-right-place behavior.

4. **No global state.**
   Ports carry no shared instances. The caller constructs the
   concrete implementation at the appropriate scope (per-app for
   persistence, per-run for workspace, per-Job for human input) and
   passes it into the runtime component that needs it.

5. **Forward compatible.**
   The four ports cover everything Phase A currently needs. Phase
   G2's ``RecoveryManager``, Phase G3's durable HITL, and Phase H's
   MCP / delegation layers all consume these SAME interfaces —
   adding a new port is a deliberate act (a new Protocol), not an
   accretion of ad-hoc methods on an existing one. Protocol
   evolution follows the standard rule: additive methods only, with
   default implementations on null / test backends.

6. **The four ports of the PELDRUN execution repair plan.**
   These are the exact ports named in the Phase A acceptance
   criteria:

       PersistencePort     — state, checkpoint, event, and human
                             request persistence
       ToolResolverPort    — tool name → live tool object
       HumanInputPort      — durable HITL request lifecycle
       WorkspacePort       — bounded filesystem access

See also
--------

    * ``backend/peldrun/engine/checkpoint.py``   — the current
                                                    disk+DB checkpoint
                                                    implementation
                                                    (Phase G1 wires
                                                    it behind
                                                    PersistencePort)

    * ``backend/peldrun/runtime/store.py``        — the current
                                                    ``RunStore``
                                                    (Phase G1's
                                                    concrete backend)

    * ``backend/peldrun/tools/builtins/human_input.py``
                                                    Phase G3 wires the
                                                    current
                                                    ``HumanInputRegistry``
                                                    behind
                                                    HumanInputPort

    * ``backend/peldrun/runtime/recovery_manager.py``
                                                    The first Phase A
                                                    consumer of these
                                                    ports.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from peldrun.engine.checkpoint import StateCheckpoint
    from peldrun.engine.state import ExecutionState
    from peldrun.events.schema import PeldrunEvent
    from peldrun.runtime.contract import HumanInputRequest

logger = logging.getLogger("peldrun.runtime.ports")


#: The type of a human's answer to an ``ask_human`` request. Providers
#: accept strings, booleans, integers, and structured JSON; the port
#: preserves the answer verbatim so the agent's tool result can carry
#: whatever type the model expects.
HumanAnswer = Any


# ---------------------------------------------------------------------------
# Port 1: PersistencePort
# ---------------------------------------------------------------------------


@runtime_checkable
class PersistencePort(Protocol):
    """
    Abstract contract for durable runtime persistence.

    Groups four concerns that must always share a backing store so
    that a crash-recovery pass sees a consistent view:

      * **Execution state** — the current ``ExecutionState`` per run.
        Used by Phase G2's resume path.

      * **Checkpoints** — the incremental step-by-step state snapshots.
        Used by Phase G1's checkpoint writer and Phase G2's
        ``RecoveryManager.plan_resume``.

      * **Events** — the append-only event log. Used by the emitter's
        replay path (Phase F) and by diagnostics / audit.

      * **Human input requests** — the durable HITL lifecycle. Used
        by Phase G3 to survive process restarts during a pending
        ``ask_human`` call.

    Contract notes
    --------------
    * All methods are ``async`` because a real implementation
      (SQLite, Postgres, S3-backed file store) does I/O.
    * Missing data must return ``None`` or an empty list — never
      raise. Callers check ``is None`` / ``not in``.
    * ``save_*`` and ``resolve_*`` methods are idempotent: writing
      the same state twice produces the same end state, resolving
      the same request twice returns the result of the first
      resolution.
    * ``list_*`` methods return results in chronological order
      (oldest first) unless their docstring explicitly states
      otherwise.
    """

    # ------------------------------------------------------------------
    # Execution state
    # ------------------------------------------------------------------

    async def save_state(self, run_id: str, state: "ExecutionState") -> None:
        """
        Persist the current execution state for ``run_id``.

        Overwrites any previous snapshot for the same run. Callers
        invoke this after every step, after every human interaction,
        and on terminal transitions.
        """
        ...

    async def load_state(self, run_id: str) -> Optional["ExecutionState"]:
        """
        Load the persisted execution state for ``run_id``.

        Returns ``None`` when no state has been saved. Never raises
        on missing data; only raises on a genuine I/O failure
        (corrupted file, DB connection loss, ...).
        """
        ...

    async def delete_state(self, run_id: str) -> bool:
        """
        Delete the persisted state for ``run_id``.

        Returns True if a state existed and was deleted, False if
        there was nothing to delete. Idempotent.
        """
        ...

    async def has_state(self, run_id: str) -> bool:
        """Return True if a state exists for ``run_id``."""
        ...

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    async def save_checkpoint(
        self, checkpoint: "StateCheckpoint", *, run_id: str
    ) -> None:
        """
        Persist a single checkpoint.

        ``run_id`` is passed separately from the checkpoint object
        because ``StateCheckpoint`` (from ``peldrun.engine.checkpoint``)
        does not carry the run_id — the caller (AgentRunner) owns the
        run-to-checkpoint mapping.
        """
        ...

    async def load_checkpoint(
        self, checkpoint_id: str
    ) -> Optional["StateCheckpoint"]:
        """
        Load a specific checkpoint by ID. Returns ``None`` when it
        does not exist.
        """
        ...

    async def get_latest_checkpoint(
        self, run_id: str
    ) -> Optional["StateCheckpoint"]:
        """
        Return the most recently created checkpoint for ``run_id``, or
        ``None`` if the run has no checkpoints.

        "Most recent" means highest ``step`` (not highest wall-clock
        timestamp), so a fast retry cannot beat a slower prior
        checkpoint.
        """
        ...

    async def list_checkpoints(self, run_id: str) -> List["StateCheckpoint"]:
        """Return all checkpoints for ``run_id``, oldest first."""
        ...

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def append_event(self, event: "PeldrunEvent") -> None:
        """
        Append an event to the durable log.

        Events are strictly append-only; duplicates are the caller's
        responsibility (the emitter's ``TerminalGuard`` prevents
        terminal-event duplicates at a higher level, but non-terminal
        events are not deduplicated here).
        """
        ...

    async def get_events_after(
        self, run_id: str, sequence: int = 0
    ) -> List["PeldrunEvent"]:
        """
        Return all events for ``run_id`` with ``event.sequence >
        sequence``, ordered by sequence ascending.

        Used by the emitter's ``replay_after`` path and by
        ``RecoveryManager`` to reconstruct the state of an interrupted
        run.
        """
        ...

    async def list_events(self, run_id: str) -> List["PeldrunEvent"]:
        """Return every event for ``run_id``, ordered by sequence."""
        ...

    # ------------------------------------------------------------------
    # Human input requests
    # ------------------------------------------------------------------

    async def save_human_request(
        self, request: "HumanInputRequest"
    ) -> None:
        """
        Persist (or update) a human input request.

        Called at request registration AND at resolution/cancellation
        so that a recovery pass can distinguish PENDING from
        ANSWERED / EXPIRED / CANCELLED requests.
        """
        ...

    async def get_human_request(
        self, request_id: str
    ) -> Optional["HumanInputRequest"]:
        """Load a human input request by ID. Returns ``None`` when absent."""
        ...

    async def resolve_human_request(
        self, request_id: str, answer: HumanAnswer
    ) -> bool:
        """
        Atomically transition the request to ANSWERED and store the
        answer.

        Returns True on the first resolution, False on any subsequent
        attempt (including attempts on an already-EXPIRED or
        already-CANCELLED request). Idempotent — the first answer
        wins.

        The atomicity requirement is important: two concurrent
        ``POST /human/answer`` requests from the same user (double
        click) must produce exactly one ANSWERED state and one
        payload. Implementations that cannot guarantee atomicity at
        the DB level must still serialize at the application level.
        """
        ...

    async def list_pending_human_requests(
        self, run_id: Optional[str] = None
    ) -> List["HumanInputRequest"]:
        """
        Return all requests in ``HumanInputStatus.PENDING``.

        When ``run_id`` is provided, restrict to that run. When
        ``None``, return all pending requests across all runs — used
        by the startup recovery sweep to detect orphans.
        """
        ...


# ---------------------------------------------------------------------------
# Port 2: ToolResolverPort
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolResolverPort(Protocol):
    """
    Abstract contract for tool name → live tool object resolution.

    ``ToolCallAgent.act()`` (Phase C), the multi-agent coordinator
    (Phase H), and the MCP session pool (Phase H) all resolve tool
    names through this port instead of importing concrete adapters.

    Contract notes
    --------------
    * ``resolve_tool`` is async because MCP tool resolution may
      spawn a subprocess, perform a network handshake, or lazily
      download a tool schema.
    * ``has_tool`` and ``list_available_tools`` are synchronous —
      they read an in-memory registry and must be callable from
      message-assembly code paths where no ``await`` is available.
    * ``resolve_tool`` returning ``None`` is NOT an error — it means
      "this tool name is not available in this run's context". The
      caller (usually ``ExecutionManager.execute_by_name``) turns
      ``None`` into a well-formed ``ToolExecutionResult.failure()``.
    """

    async def resolve_tool(
        self,
        tool_id: str,
        *,
        run_id: Optional[str] = None,
        workspace_root: Optional[str] = None,
        **context: Any,
    ) -> Optional[Any]:
        """
        Resolve ``tool_id`` to a live tool object, or return ``None``
        when the tool is not available.

        Keyword arguments carry the run-scoped context that some
        resolvers need:

          * ``run_id`` — for MCP session pooling, per-run auth, and
                         diagnostics correlation.
          * ``workspace_root`` — for tools that read/write files and
                                 need to be bound to the sandbox.

        Extra ``**context`` accepts forward-compatible keys (e.g.
        ``chat_id``, ``project_id``) without requiring a signature
        change. Resolvers MUST ignore keys they do not recognize.
        """
        ...

    def has_tool(self, tool_id: str) -> bool:
        """
        Return True if ``tool_id`` is registered / available.

        Synchronous — callers use this from message-assembly code
        without a surrounding async context.
        """
        ...

    def list_available_tools(self) -> List[str]:
        """
        Return the identifiers of every tool the resolver knows about.

        Synchronous, in-memory lookup. Used by diagnostics and by
        ``ExecutionManager.execute_by_name`` to compose the "available
        tools" list included in a not-found failure.
        """
        ...


# ---------------------------------------------------------------------------
# Port 3: HumanInputPort
# ---------------------------------------------------------------------------


@runtime_checkable
class HumanInputPort(Protocol):
    """
    Abstract contract for durable HITL (human-in-the-loop) requests.

    Extends the persistence port with the in-memory coroutine
    bridging that ``ask_human`` requires: a ``Future`` that the
    agent awaits while the UI renders a modal, and that resolves
    when the user submits an answer OR when the request expires.

    Design invariants
    -----------------
    * ``register_pending`` returns an ``asyncio.Future`` that the
      caller awaits. The Future is resolved by ``resolve`` (user
      answer) or by ``cancel`` (timeout / run cancellation).
    * Persistence of the request happens INSIDE ``register_pending``
      before the Future is returned. If persistence fails,
      ``register_pending`` raises and the caller never awaits a
      phantom Future.
    * ``resolve`` is the only sanctioned resolution path. It
      transitions both the durable record AND the in-memory Future
      atomically (from the caller's perspective — the Future is
      resolved after the durable write succeeds).
    * Phase G3 makes the Future survive a process restart by
      re-registering it during ``RecoveryManager.resume()``.
    """

    async def register_pending(
        self, request: "HumanInputRequest"
    ) -> "asyncio.Future[Any]":
        """
        Persist ``request`` and return a Future that resolves when
        the request is answered, cancelled, or times out.

        The Future's result type is ``HumanAnswer`` on success, and
        raises ``asyncio.CancelledError`` when the request was
        cancelled (timeout, run cancellation, server shutdown).
        """
        ...

    async def resolve(self, request_id: str, answer: HumanAnswer) -> bool:
        """
        Resolve a pending request with the user's answer.

        Returns True if this call transitioned the request from
        PENDING → ANSWERED (the future receives ``answer``). Returns
        False if the request was not PENDING — already answered,
        expired, cancelled, or nonexistent.

        Idempotent — double-click on the "Submit" button results in
        one resolution and one log entry.
        """
        ...

    async def cancel(self, request_id: str) -> bool:
        """
        Cancel a pending request.

        Used by timeout expiry, run cancellation, and server
        shutdown. The Future receives ``asyncio.CancelledError``.
        Returns False if the request was not PENDING.

        Idempotent.
        """
        ...

    async def get(
        self, request_id: str
    ) -> Optional["HumanInputRequest"]:
        """
        Load a request by ID. Used by the HTTP router to validate
        inbound answers against the exact stored request (Phase G3's
        security fix — rejecting "answered request A from run B").
        """
        ...

    async def list_pending(
        self, run_id: Optional[str] = None
    ) -> List["HumanInputRequest"]:
        """
        Return all requests currently PENDING.

        With ``run_id``, restrict to that run. Used by
        ``RecoveryManager`` on startup to detect and re-register
        orphaned requests after a crash.
        """
        ...


# ---------------------------------------------------------------------------
# Port 4: WorkspacePort
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkspacePort(Protocol):
    """
    Abstract contract for bounded filesystem access.

    Every tool that reads or writes files, every artifact detector,
    and every delegation child receives a ``WorkspacePort`` scoped to
    its allowed root. The port guarantees that path traversal outside
    the root is rejected before any I/O happens.

    Design invariants
    -----------------
    * ``read_file``, ``write_file``, ``delete_file``, ``exists``, and
      ``list_files`` are async: real filesystem I/O on a network
      mount or a slow SSD can block for tens of milliseconds, and
      hiding that in a synchronous method silently stalls the event
      loop.
    * ``resolve_path`` is sync because it only does path arithmetic
      (``Path(root, rel_path).resolve()``) with a boundary check — no
      I/O. Callers use it to inspect the boundary without
      round-tripping through the event loop.
    * ``root_path`` is a property, not a method, because every caller
      reads it multiple times and a property documents "this is
      cheap to access".
    * Path traversal (``..``, absolute paths, symlinks that escape
      the root) is rejected by raising ``ValueError`` — not by
      silently clamping to the root, which would produce data
      corruption.
    """

    @property
    def root_path(self) -> Path:
        """The absolute path of the bounded workspace root."""
        ...

    async def list_files(
        self, *, include_hidden: bool = False
    ) -> List[str]:
        """
        Return every file inside the workspace, as paths RELATIVE to
        ``root_path`` (forward-slash separated, POSIX-style, for
        cross-platform consistency).

        ``include_hidden`` defaults to False — dotted files
        (``.git/``, ``.peldrun/``, ...) are excluded from the default
        listing. Artifact detection uses ``include_hidden=False`` to
        avoid surfacing internal bookkeeping as deliverables.
        """
        ...

    async def read_file(self, rel_path: str) -> bytes:
        """
        Read the raw bytes of ``rel_path``.

        Raises ``FileNotFoundError`` when the file does not exist,
        ``ValueError`` when ``rel_path`` escapes ``root_path``,
        ``IsADirectoryError`` when it points at a directory.
        """
        ...

    async def write_file(self, rel_path: str, data: bytes) -> None:
        """
        Write ``data`` to ``rel_path``, creating parent directories
        as needed.

        Overwrites any existing file atomically (temp + fsync +
        rename) so a crash cannot leave a half-written artifact
        behind. Raises ``ValueError`` on path traversal.
        """
        ...

    async def delete_file(self, rel_path: str) -> bool:
        """
        Delete ``rel_path``. Returns True if it existed and was
        deleted, False if there was nothing to delete.
        """
        ...

    async def exists(self, rel_path: str) -> bool:
        """
        Return True if ``rel_path`` exists inside the workspace.

        Returns False (instead of raising) for path-traversal
        attempts so callers can safely probe untrusted paths without
        a try/except. This is the ONE exception to the
        "traversal raises ValueError" rule, justified by the caller
        pattern: ``if not exists(p): create(p)`` must not double-raise.
        """
        ...

    def resolve_path(self, rel_path: str) -> Path:
        """
        Return the absolute ``Path`` for ``rel_path``, validating
        that it stays inside ``root_path``.

        Raises ``ValueError`` on traversal attempts. Never performs
        I/O — the returned path may or may not exist.
        """
        ...


# ---------------------------------------------------------------------------
# Null implementations
# ---------------------------------------------------------------------------
#
# The Null* classes below are safe, well-defined implementations for
# contexts where the real backend is deliberately absent:
#
#   * Stateless sub-agents in a delegation chain — they read the
#     parent's workspace but do not persist their own state.
#   * Isolated unit tests that exercise agent logic without spinning
#     up SQLite.
#   * Chat-mode runs where no durable run state is needed.
#
# They satisfy the Protocol structurally: ``isinstance(Null*, Port)``
# returns True. Callers never special-case the nulls.


class NullPersistencePort:
    """
    A ``PersistencePort`` that stores nothing.

    ``save_*`` methods are silent no-ops; ``load_*`` methods return
    ``None`` / empty lists. This is the correct backend for a run
    whose state is managed entirely in-memory (chat-mode, tests,
    short-lived CLI invocations).
    """

    async def save_state(self, run_id: str, state: Any) -> None:
        return None

    async def load_state(self, run_id: str) -> Optional[Any]:
        return None

    async def delete_state(self, run_id: str) -> bool:
        return False

    async def has_state(self, run_id: str) -> bool:
        return False

    async def save_checkpoint(self, checkpoint: Any, *, run_id: str) -> None:
        return None

    async def load_checkpoint(self, checkpoint_id: str) -> Optional[Any]:
        return None

    async def get_latest_checkpoint(self, run_id: str) -> Optional[Any]:
        return None

    async def list_checkpoints(self, run_id: str) -> List[Any]:
        return []

    async def append_event(self, event: Any) -> None:
        return None

    async def get_events_after(
        self, run_id: str, sequence: int = 0
    ) -> List[Any]:
        return []

    async def list_events(self, run_id: str) -> List[Any]:
        return []

    async def save_human_request(self, request: Any) -> None:
        return None

    async def get_human_request(self, request_id: str) -> Optional[Any]:
        return None

    async def resolve_human_request(
        self, request_id: str, answer: Any
    ) -> bool:
        return False

    async def list_pending_human_requests(
        self, run_id: Optional[str] = None
    ) -> List[Any]:
        return []


class NullHumanInputPort:
    """
    A ``HumanInputPort`` that immediately resolves every request with
    an empty answer.

    This is the correct backend for contexts that explicitly do NOT
    support human interaction (headless runs, delegated child agents,
    batch processing). Rather than hanging forever on a Future that
    will never be resolved, the port returns a Future that is
    already resolved with ``None``.

    The caller (``ask_human``) interprets the empty answer as "no
    response available". The agent's tool result carries
    ``metadata.reason="no_human_available"``, matching the semantics
    of ``on_timeout="return_empty"``.
    """

    async def register_pending(self, request: Any) -> "asyncio.Future[Any]":
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        future.set_result(None)
        return future

    async def resolve(self, request_id: str, answer: Any) -> bool:
        return False

    async def cancel(self, request_id: str) -> bool:
        return False

    async def get(self, request_id: str) -> Optional[Any]:
        return None

    async def list_pending(self, run_id: Optional[str] = None) -> List[Any]:
        return []


class NullWorkspacePort:
    """
    A ``WorkspacePort`` whose ``root_path`` points at a real but empty
    temporary directory.

    ``read_file`` / ``resolve_path`` raise ``FileNotFoundError`` for
    every path; ``write_file`` raises ``PermissionError``; ``exists``
    always returns ``False``; ``list_files`` returns ``[]``.

    This is the correct backend for contexts where file operations
    must be explicitly rejected (an isolated sub-agent that is not
    allowed to read or write files). Silent no-ops would be
    dangerous — a tool that "successfully wrote a file" but has no
    filesystem is a bug the caller must know about.
    """

    def __init__(self) -> None:
        # A real path is required so ``resolve_path`` can compare
        # against a boundary. The path does not need to exist.
        import tempfile

        self._root = Path(tempfile.gettempdir()) / ".peldrun_null_workspace"

    @property
    def root_path(self) -> Path:
        return self._root

    async def list_files(
        self, *, include_hidden: bool = False
    ) -> List[str]:
        return []

    async def read_file(self, rel_path: str) -> bytes:
        raise FileNotFoundError(
            f"NullWorkspacePort: cannot read {rel_path!r} — no real "
            f"workspace is attached to this context."
        )

    async def write_file(self, rel_path: str, data: bytes) -> None:
        raise PermissionError(
            f"NullWorkspacePort: cannot write {rel_path!r} — no real "
            f"workspace is attached to this context."
        )

    async def delete_file(self, rel_path: str) -> bool:
        return False

    async def exists(self, rel_path: str) -> bool:
        return False

    def resolve_path(self, rel_path: str) -> Path:
        raise FileNotFoundError(
            f"NullWorkspacePort: cannot resolve {rel_path!r} — no "
            f"real workspace is attached to this context."
        )


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def null_persistence() -> NullPersistencePort:
    """Return a fresh ``NullPersistencePort``. Cheap to instantiate."""
    return NullPersistencePort()


def null_human_input() -> NullHumanInputPort:
    """Return a fresh ``NullHumanInputPort``. Cheap to instantiate."""
    return NullHumanInputPort()


def null_workspace() -> NullWorkspacePort:
    """Return a fresh ``NullWorkspacePort``. Cheap to instantiate."""
    return NullWorkspacePort()


__all__ = [
    # Answer type alias
    "HumanAnswer",
    # The four ports
    "PersistencePort",
    "ToolResolverPort",
    "HumanInputPort",
    "WorkspacePort",
    # Null implementations
    "NullPersistencePort",
    "NullHumanInputPort",
    "NullWorkspacePort",
    # Convenience constructors
    "null_persistence",
    "null_human_input",
    "null_workspace",
]
