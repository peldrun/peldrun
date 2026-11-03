# backend/peldrun/llm/plugin_host/supervisor.py
"""
Main-process manager for a single Provider Package worker.

The supervisor is the ONLY component that spawns workers, sends
requests to them, and interprets their replies. Higher layers
(the ``custom_plugin`` adapter wrapper, the provider factory, and
ultimately ``dispatcher.py`` / ``executor.py``) never touch the raw
transport — they see a normal ``ProviderAdapter``-shaped object and
call methods on it. All IPC complexity is contained here.

Lifecycle
---------

    create  → start  → (ready)  → load_adapter  → (adapter_loaded)
                                                        │
                                                        ▼
                            ┌───────────────────────────────────────┐
                            │  serve requests                        │
                            │  generate / stream / discover / ...    │
                            └───────────────────────────────────────┘
                                                        │
                                                        ▼
                                          shutdown  → (ack)  → exit

``start()`` blocks until the worker announces ``READY`` and its
``LOAD_ADAPTER`` request has been acknowledged. After that, the worker
is available for requests.

The supervisor is meant to be used as an async context manager:

    async with WorkerSupervisor(...) as sup:
        response = await sup.generate(request)

The ``__aexit__`` path calls ``shutdown()``, which sends ``SHUTDOWN``,
waits for the ack, and escalates to SIGTERM + SIGKILL if the worker
does not exit.

Correlation model
-----------------

Every request is assigned a fresh ``request_id`` (a UUID hex). The
supervisor maintains ``_pending_replies: Dict[str, asyncio.Future]``
keyed by that ID. A single background task — ``_read_loop`` — reads
envelopes from the transport and dispatches:

    * ``reply`` messages → resolve the corresponding future.
    * ``stream_event`` / ``stream_end`` messages → push into the
      stream's queue (keyed by ``stream_id``).
    * ``log`` messages → forwarded to the main process's logger.
    * ``ready`` / unsolicited → logged and dropped.

This design allows any number of concurrent requests against a single
worker: each has its own future, and the read loop does not
serialize them beyond the transport's own write lock (which only
protects a single write call).

Failure modes
-------------

1. **Worker crashes.** The read loop sees ``TransportClosedError``,
   fails all pending futures with ``WorkerCrashedError``, and marks
   the supervisor as dead. Subsequent ``generate()`` calls raise
   immediately (no silent hang).

2. **Request times out.** The supervisor cancels the future, raises
   ``REQUEST_TIMEOUT``. The worker may still be executing the
   request — this is intentional; forcing a cancel via a separate
   mechanism would require the worker to support cooperative
   cancellation of every adapter method, which not every SDK allows.

3. **Stream stalls.** Each ``stream_event`` is delivered via a
   ``Queue`` with a timeout on ``Queue.get()``. A stalled worker
   (e.g. an SDK that buffers indefinitely) surfaces as
   ``STREAM_STALLED`` and cancels the stream — the supervisor never
   waits forever.

4. **Shutdown hangs.** ``shutdown()`` sends the request, waits
   ``SHUTDOWN_TIMEOUT_SECONDS`` for the ack, then escalates:
   SIGTERM, wait ``SIGTERM_GRACE_SECONDS``, then SIGKILL. The
   supervisor never leaves an orphaned process behind.

Error translation
-----------------

Every failure the supervisor produces is a subclass of
``ProviderError`` — the same hierarchy the in-process adapters use.
Downstream code cannot tell whether an error originated in-process
or over IPC. The translation table is documented in
``_map_transport_error()``.

Streaming
---------

The supervisor does NOT buffer stream events. ``stream()`` is an
async generator that yields each event as the read loop receives it.
The per-stream queue has a bounded size (``STREAM_QUEUE_MAX``); when
it fills, the read loop blocks on ``queue.put()`` — this backpressures
the worker (which blocks on its next ``write_envelope`` once the OS
pipe buffer fills). The alternative — dropping events — would corrupt
the response.

See also
--------

    * ``backend/peldrun/llm/plugin_host/protocol.py`` — message schema
    * ``backend/peldrun/llm/plugin_host/transport.py`` — I/O
    * ``backend/peldrun/llm/plugin_host/worker.py`` — the subprocess
    * ``backend/peldrun/llm/providers/custom_plugin.py`` — adapter
      that wraps this supervisor
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from peldrun.llm.contracts import (
    ConnectionContext,
    LLMStreamEvent,
)
from peldrun.llm.errors import (
    ProviderConnectionError,
    ProviderContractIncompatibleError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from peldrun.llm.plugin_host.protocol import (
    REQUEST_TIMEOUT_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    SIGTERM_GRACE_SECONDS,
    WORKER_STARTUP_TIMEOUT_SECONDS,
    AdapterLoadedPayload,
    ConnectionResultPayload,
    DiscoverModelsPayload,
    Envelope,
    ErrorPayload,
    GeneratePayload,
    GenerateResultPayload,
    GetModelMetadataPayload,
    LoadAdapterPayload,
    MessageType,
    MetadataResultPayload,
    ModelsResultPayload,
    ProtocolErrorCode,
    ReadyPayload,
    ShutdownPayload,
    StreamCancelPayload,
    StreamEndPayload,
    StreamEventPayload,
    StreamStartPayload,
    StreamStartedPayload,
    ValidateConnectionPayload,
    make_request,
    new_request_id,
)
from peldrun.llm.plugin_host.transport import (
    SubprocessTransport,
    TransportClosedError,
    TransportError,
    TransportMessageTooLargeError,
    TransportProtocolError,
    TransportReadError,
    TransportWriteError,
)
from peldrun.llm.request import LLMRequest


logger = logging.getLogger("peldrun.llm.plugin_host.supervisor")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Maximum number of buffered stream events per stream before the read
#: loop blocks on ``queue.put()``. Backpressures the worker instead of
#: growing memory unbounded. 64 events is large enough to smooth over
#: normal bursty token streams (a language model emits one token per
#: ``CONTENT_DELTA``, typically 20-80 per second) while remaining
#: small enough that a misbehaving worker cannot exhaust memory.
STREAM_QUEUE_MAX: int = 64


#: How long a single ``Queue.get()`` waits for the next stream event
#: before treating the stream as stalled. 5 minutes is generous — a
#: legitimate slow model (long thinking pause, huge context) can go
#: this long between tokens. A truly hung stream surfaces here.
STREAM_STALL_TIMEOUT_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SupervisorError(ProviderError):
    """
    Base class for supervisor-level failures.

    Subclasses of ``ProviderError`` so downstream code catches them
    with the same ``except`` clauses it uses for in-process adapters.
    """

    code = "plugin_supervisor_error"


class WorkerCrashedError(ProviderUnavailableError):
    """
    The worker exited unexpectedly (EOF on stdout, uncaught crash, or
    SIGKILL by the OS). Retryable — the supervisor can spawn a fresh
    worker and try again.
    """

    code = "plugin_worker_crashed"


class WorkerStartupError(SupervisorError):
    """
    The worker failed to reach ``READY`` within
    ``WORKER_STARTUP_TIMEOUT_SECONDS``, or ``LOAD_ADAPTER`` failed.
    NOT retryable without operator intervention — the same
    configuration will fail identically.
    """

    code = "plugin_worker_startup_failed"
    retryable = False


class StreamStalledError(ProviderTimeoutError):
    """
    A stream stopped emitting events for longer than
    ``STREAM_STALL_TIMEOUT_SECONDS``. Treat like a normal timeout —
    retryable.
    """

    code = "plugin_stream_stalled"


# ---------------------------------------------------------------------------
# State per active stream
# ---------------------------------------------------------------------------


class _StreamState:
    """
    Per-stream correlation state.

    ``queue`` receives ``STREAM_EVENT`` payloads and a single terminal
    marker (either the ``StreamEndPayload`` for normal completion or
    an ``ErrorPayload`` for failure). The consumer's async generator
    pops items until it sees the terminal marker.

    ``cancelled`` is set by ``cancel()`` so the consumer knows the
    terminal marker is a user-initiated cancel, not an error.
    """

    __slots__ = ("queue", "cancelled", "closed")

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=STREAM_QUEUE_MAX)
        self.cancelled: bool = False
        self.closed: bool = False


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class WorkerSupervisor:
    """
    Owns ONE worker subprocess for its entire lifetime.

    Constructed by ``custom_plugin.PluginProviderAdapter`` when the
    factory needs a provider package. The worker is spawned lazily by
    ``start()`` and lives until ``shutdown()``.

    The class is a context manager:

        async with WorkerSupervisor(...) as sup:
            result = await sup.generate(request)

    Thread safety
    -------------
    Not thread-safe. All methods MUST be called from the same asyncio
    event loop the supervisor was started on. This matches the
    runtime's design — everything in ``peldrun.llm`` runs on the
    caller's loop.

    Reentrancy
    ----------
    Not reentrant. A single supervisor instance corresponds to a
    single (provider, connection) pair. The factory creates a fresh
    supervisor per connection — see ``custom_plugin.py``.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        version: str,
        connection: ConnectionContext,
        venv_python: Path,
        package_dir: Path,
        entry_point: str,
        adapter_profile_dict: Dict[str, Any],
        permissions: List[str],
    ) -> None:
        """
        Parameters
        ----------
        provider_id
            The package's canonical ID (from the manifest). Used for
            logging and error messages.
        version
            The installed version. Included in error messages so the
            operator knows which version failed.
        connection
            The per-connection context (base_url, api_key, headers,
            extra_params). This is the ONLY connection the worker
            receives — it can never see another connection's secrets.
        venv_python
            The path to the venv's Python interpreter. The supervisor
            spawns this interpreter, NOT ``sys.executable``, so the
            worker sees the package's isolated dependencies.
        package_dir
            The path to the extracted package files. Passed to the
            worker as ``LoadAdapterPayload.package_dir`` so it can
            insert it into ``sys.path`` before importing.
        entry_point
            ``module.path:ClassName`` — the adapter to instantiate.
        adapter_profile_dict
            The ``AdapterProfile`` serialized to a dict. Passed to
            the worker as ``LoadAdapterPayload.profile``; the worker
            constructs the real ``AdapterProfile`` from it so the
            adapter's constructor signature is satisfied.
        permissions
            The manifest's declared permission strings. Passed to the
            worker as ``LoadAdapterPayload.permissions``; the worker
            builds the runtime enforcer from them.
        """
        self._provider_id = provider_id
        self._version = version
        self._connection = connection
        self._venv_python = Path(venv_python)
        self._package_dir = Path(package_dir)
        self._entry_point = entry_point
        self._adapter_profile_dict = dict(adapter_profile_dict)
        self._permissions = list(permissions)

        # Populated by ``start()``.
        self._process: Optional[asyncio.subprocess.Process] = None
        self._transport: Optional[SubprocessTransport] = None
        self._read_task: Optional[asyncio.Task] = None
        self._pending_replies: Dict[str, asyncio.Future] = {}
        self._active_streams: Dict[str, _StreamState] = {}
        self._ready_event: Optional[asyncio.Event] = None
        self._adapter_loaded_event: Optional[asyncio.Event] = None
        self._started: bool = False
        self._dead: bool = False
        self._worker_pid: Optional[int] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "WorkerSupervisor":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Spawn the worker and complete the handshake.

        Steps
        -----
        1. Launch ``venv_python -m peldrun.llm.plugin_host.worker``
           as a subprocess with stdin/stdout pipes and stderr forwarded
           to the main process's stderr.
        2. Wait for ``READY`` (bounded by
           ``WORKER_STARTUP_TIMEOUT_SECONDS``).
        3. Send ``LOAD_ADAPTER`` and wait for ``ADAPTER_LOADED``.

        Raises
        ------
        WorkerStartupError
            On any failure before the adapter is ready. The worker
            subprocess is terminated and reaped before the exception
            propagates, so the caller never has to clean up.
        """
        if self._started:
            raise RuntimeError(
                "WorkerSupervisor.start() called twice. Construct a "
                "fresh supervisor instead of restarting."
            )

        if not self._venv_python.is_file():
            raise WorkerStartupError(
                f"The worker's venv interpreter does not exist: "
                f"{self._venv_python}. The package may need to be "
                f"reinstalled.",
                provider=self._provider_id,
                model=None,
                details={
                    "venv_python": str(self._venv_python),
                    "version": self._version,
                },
            )

        # --------------------------------------------------------------
        # Spawn the subprocess.
        # --------------------------------------------------------------
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._venv_python),
                "-m",
                "peldrun.llm.plugin_host.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr inherits from the main process by default,
                # so the worker's own print/logging output lands in
                # the backend's log stream. This is what makes the
                # `capture_stdout_for_protocol()` redirect useful —
                # the redirect sends text stdout to stderr, which is
                # visible to the operator without corrupting the
                # protocol channel.
                stderr=None,
            )
        except FileNotFoundError as exc:
            raise WorkerStartupError(
                f"Could not launch the worker interpreter "
                f"{self._venv_python}: {exc}",
                provider=self._provider_id,
                details={"venv_python": str(self._venv_python)},
            ) from exc
        except Exception as exc:
            raise WorkerStartupError(
                f"Could not spawn the worker subprocess: {exc}",
                provider=self._provider_id,
                details={"exception_type": type(exc).__name__},
            ) from exc

        self._process = process
        self._worker_pid = process.pid
        self._transport = SubprocessTransport(process)
        self._ready_event = asyncio.Event()
        self._adapter_loaded_event = asyncio.Event()

        # --------------------------------------------------------------
        # Start the read loop BEFORE waiting for READY. The loop is
        # what resolves the ready future.
        # --------------------------------------------------------------
        self._read_task = asyncio.create_task(self._read_loop())

        # --------------------------------------------------------------
        # Wait for READY.
        # --------------------------------------------------------------
        try:
            await asyncio.wait_for(
                self._ready_event.wait(),
                timeout=WORKER_STARTUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._kill_and_reap()
            raise WorkerStartupError(
                f"Worker did not become ready within "
                f"{WORKER_STARTUP_TIMEOUT_SECONDS:.0f}s "
                f"(provider={self._provider_id}, "
                f"version={self._version}, pid={process.pid}). The "
                f"worker may have crashed during import, or the venv "
                f"may be corrupted.",
                provider=self._provider_id,
                details={
                    "pid": process.pid,
                    "timeout_seconds": WORKER_STARTUP_TIMEOUT_SECONDS,
                },
            )

        # --------------------------------------------------------------
        # Send LOAD_ADAPTER.
        # --------------------------------------------------------------
        try:
            await self._send_load_adapter()
        except Exception:
            await self._kill_and_reap()
            raise

        # --------------------------------------------------------------
        # Wait for ADAPTER_LOADED.
        # --------------------------------------------------------------
        try:
            await asyncio.wait_for(
                self._adapter_loaded_event.wait(),
                timeout=WORKER_STARTUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._kill_and_reap()
            raise WorkerStartupError(
                f"Worker did not acknowledge LOAD_ADAPTER within "
                f"{WORKER_STARTUP_TIMEOUT_SECONDS:.0f}s "
                f"(provider={self._provider_id}). The adapter import "
                f"may be hanging on a network call — adapters MUST "
                f"NOT perform network I/O at import time.",
                provider=self._provider_id,
                details={
                    "pid": process.pid,
                    "timeout_seconds": WORKER_STARTUP_TIMEOUT_SECONDS,
                },
            )

        self._started = True
        logger.info(
            "WorkerSupervisor started: provider=%r version=%r pid=%d.",
            self._provider_id,
            self._version,
            process.pid,
        )

    async def shutdown(self) -> None:
        """
        Graceful shutdown, with escalation.

        Idempotent. Safe to call even if ``start()`` never succeeded
        or the worker already crashed.

        Sequence
        --------
        1. If the worker is alive, send ``SHUTDOWN``.
        2. Wait ``SHUTDOWN_TIMEOUT_SECONDS`` for the ack.
        3. If no ack, send SIGTERM.
        4. Wait ``SIGTERM_GRACE_SECONDS``.
        5. If still alive, send SIGKILL.
        6. Reap the process, cancel the read task, and close the
           transport.

        Every step is best-effort. A hung shutdown is worse than a
        missed ack, so the escalation is unconditional — the
        supervisor never leaves an orphaned subprocess.
        """
        if self._dead and self._process is None:
            # Already fully torn down.
            return

        # --------------------------------------------------------------
        # Try graceful shutdown if the worker is still reachable.
        # --------------------------------------------------------------
        worker_reachable = (
            self._transport is not None
            and self._process is not None
            and self._process.returncode is None
        )

        if worker_reachable:
            try:
                await asyncio.wait_for(
                    self._send_shutdown_and_wait(),
                    timeout=SHUTDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.debug(
                    "SHUTDOWN ack timed out for provider=%r; "
                    "escalating to SIGTERM.",
                    self._provider_id,
                )
            except Exception as exc:
                logger.debug(
                    "SHUTDOWN request failed for provider=%r: %s; "
                    "escalating to SIGTERM.",
                    self._provider_id,
                    exc,
                )

        # --------------------------------------------------------------
        # Escalate: SIGTERM → SIGKILL.
        # --------------------------------------------------------------
        await self._kill_and_reap()

    async def _send_shutdown_and_wait(self) -> None:
        """
        Send ``SHUTDOWN`` and wait for the ack.

        Uses a future registered by ``_read_loop`` when the ack
        arrives. The future is failed by the read loop on EOF; the
        caller's outer ``wait_for`` handles the timeout.
        """
        request_id = new_request_id()
        reply_future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_replies[request_id] = reply_future
        try:
            envelope = make_request(
                MessageType.SHUTDOWN,
                ShutdownPayload(reason="supervisor_shutdown"),
                request_id=request_id,
            )
            await self._transport.write_envelope(envelope)
            await reply_future
        finally:
            self._pending_replies.pop(request_id, None)

    async def _kill_and_reap(self) -> None:
        """
        Terminate the worker subprocess and clean up all supervisor
        state. Idempotent and never raises.
        """
        if self._dead:
            return
        self._dead = True

        # --------------------------------------------------------------
        # Cancel the read task first, so it does not try to read from
        # a dying pipe.
        # --------------------------------------------------------------
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
        self._read_task = None

        # --------------------------------------------------------------
        # Fail every pending reply so blocked requests unblock.
        # --------------------------------------------------------------
        for request_id, future in list(self._pending_replies.items()):
            if not future.done():
                future.set_exception(
                    WorkerCrashedError(
                        f"Worker for provider '{self._provider_id}' "
                        f"exited during shutdown.",
                        provider=self._provider_id,
                    )
                )
        self._pending_replies.clear()

        # --------------------------------------------------------------
        # Push terminal markers to every active stream.
        # --------------------------------------------------------------
        for stream_state in list(self._active_streams.values()):
            if not stream_state.closed:
                stream_state.closed = True
                try:
                    stream_state.queue.put_nowait(
                        ErrorPayload(
                            code=WorkerCrashedError.code,
                            message=(
                                f"Worker for provider "
                                f"'{self._provider_id}' exited."
                            ),
                        )
                    )
                except asyncio.QueueFull:
                    pass
        self._active_streams.clear()

        # --------------------------------------------------------------
        # Terminate the subprocess.
        # --------------------------------------------------------------
        process = self._process
        if process is not None and process.returncode is None:
            # Close stdin so the worker sees EOF on its next read.
            try:
                if self._transport is not None:
                    await self._transport.close()
            except Exception:
                pass

            # SIGTERM.
            try:
                process.terminate()
            except ProcessLookupError:
                pass

            # Wait for exit.
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=SIGTERM_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                # SIGKILL.
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Worker pid=%d did not exit even after SIGKILL; "
                        "it may be in an uninterruptible kernel state.",
                        process.pid,
                    )
            except Exception:
                pass

        self._process = None
        self._transport = None
        self._started = False

    # ------------------------------------------------------------------
    # Read loop
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """
        Background task: read envelopes from the worker and dispatch.

        Runs until EOF or cancellation. On EOF, fails every pending
        future with ``WorkerCrashedError`` and terminates every active
        stream with an error marker.

        Never raises — any exception inside the loop is logged and
        treated as "worker died".
        """
        assert self._transport is not None

        try:
            while True:
                try:
                    envelope = await self._transport.read_envelope()
                except TransportClosedError:
                    logger.debug(
                        "Worker for provider=%r closed its output "
                        "channel.",
                        self._provider_id,
                    )
                    break
                except TransportError as exc:
                    logger.warning(
                        "Transport error reading from worker "
                        "provider=%r: %s",
                        self._provider_id,
                        exc,
                    )
                    break

                try:
                    await self._dispatch_envelope(envelope)
                except Exception as exc:
                    # An exception while dispatching one envelope must
                    # never kill the read loop — the channel is still
                    # valid, subsequent messages must still be read.
                    logger.exception(
                        "Error dispatching envelope %r from worker: %s",
                        envelope.type,
                        exc,
                    )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            logger.exception(
                "Unhandled exception in read loop for provider=%r: %s",
                self._provider_id,
                exc,
            )

        finally:
            # Worker is gone. Fail everything.
            self._handle_worker_death()

    def _handle_worker_death(self) -> None:
        """
        Fail every pending future and terminate every active stream.

        Called from the read loop's ``finally`` block. Idempotent —
        a second call is a no-op.
        """
        if self._dead:
            return

        # Mark dead BEFORE failing futures so any handler that tries
        # to send a new request fails fast.
        self._dead = True

        for request_id, future in list(self._pending_replies.items()):
            if not future.done():
                future.set_exception(
                    WorkerCrashedError(
                        f"Worker for provider '{self._provider_id}' "
                        f"unexpectedly closed its channel.",
                        provider=self._provider_id,
                    )
                )
        self._pending_replies.clear()

        for stream_state in list(self._active_streams.values()):
            if not stream_state.closed:
                stream_state.closed = True
                try:
                    stream_state.queue.put_nowait(
                        ErrorPayload(
                            code=WorkerCrashedError.code,
                            message=(
                                f"Worker for provider "
                                f"'{self._provider_id}' closed "
                                f"its channel mid-stream."
                            ),
                        )
                    )
                except asyncio.QueueFull:
                    pass
        self._active_streams.clear()

    # ------------------------------------------------------------------
    # Envelope dispatch
    # ------------------------------------------------------------------

    async def _dispatch_envelope(self, envelope: Envelope) -> None:
        """
        Route an incoming envelope to the correct destination.

        Routing table:

            READY           → set ``_ready_event``
            ADAPTER_LOADED  → set ``_adapter_loaded_event`` + reply
            GENERATE_RESULT → resolve pending future
            STREAM_STARTED  → resolve pending future
            STREAM_EVENT    → push to stream queue
            STREAM_END      → push terminal marker to stream queue
            MODELS_RESULT   → resolve pending future
            CONNECTION_RESULT → resolve pending future
            METADATA_RESULT → resolve pending future
            SHUTDOWN_ACK    → resolve pending future
            ERROR           → fail pending future or push error
                              to stream queue
            LOG             → forward to logger
        """
        msg_type = envelope.type

        # ==============================================================
        # Startup handshake.
        # ==============================================================
        if msg_type == MessageType.READY:
            try:
                ready = ReadyPayload.model_validate(envelope.payload)
                if ready.protocol_version != _get_protocol_version():
                    self._handle_protocol_mismatch(
                        ready.protocol_version
                    )
                    return
                logger.debug(
                    "Worker for provider=%r is ready (pid=%d, "
                    "python=%s, protocol=%d).",
                    self._provider_id,
                    ready.worker_pid,
                    ready.python_version,
                    ready.protocol_version,
                )
            except Exception as exc:
                logger.warning(
                    "Malformed READY from worker: %s", exc
                )
            if self._ready_event is not None:
                self._ready_event.set()
            return

        if msg_type == MessageType.ADAPTER_LOADED:
            try:
                loaded = AdapterLoadedPayload.model_validate(
                    envelope.payload
                )
                logger.debug(
                    "Worker for provider=%r loaded adapter profile.",
                    self._provider_id,
                )
                # The profile was pre-computed by the supervisor, so
                # we do not overwrite it — this is just a confirmation
                # the adapter accepted the config.
                _ = loaded  # silence linters
            except Exception as exc:
                logger.warning(
                    "Malformed ADAPTER_LOADED from worker: %s", exc
                )
            self._resolve_pending(envelope.id, envelope.payload)
            if self._adapter_loaded_event is not None:
                self._adapter_loaded_event.set()
            return

        # ==============================================================
        # Request replies — resolve the matching future.
        # ==============================================================
        if msg_type in (
            MessageType.GENERATE_RESULT,
            MessageType.STREAM_STARTED,
            MessageType.MODELS_RESULT,
            MessageType.CONNECTION_RESULT,
            MessageType.METADATA_RESULT,
            MessageType.SHUTDOWN_ACK,
        ):
            self._resolve_pending(envelope.id, envelope.payload)
            return

        # ==============================================================
        # Stream events — push to the correct stream's queue.
        # ==============================================================
        if msg_type == MessageType.STREAM_EVENT:
            try:
                payload = StreamEventPayload.model_validate(
                    envelope.payload
                )
            except Exception as exc:
                logger.warning(
                    "Malformed STREAM_EVENT from worker: %s", exc
                )
                return
            stream_state = self._active_streams.get(payload.stream_id)
            if stream_state is None or stream_state.closed:
                logger.debug(
                    "Discarding event for unknown/closed stream %r.",
                    payload.stream_id,
                )
                return
            try:
                await stream_state.queue.put(payload.event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to enqueue stream event: %s", exc
                )
            return

        if msg_type == MessageType.STREAM_END:
            try:
                payload = StreamEndPayload.model_validate(
                    envelope.payload
                )
            except Exception as exc:
                logger.warning(
                    "Malformed STREAM_END from worker: %s", exc
                )
                return
            stream_state = self._active_streams.get(payload.stream_id)
            if stream_state is None or stream_state.closed:
                logger.debug(
                    "Discarding STREAM_END for unknown/closed "
                    "stream %r.",
                    payload.stream_id,
                )
                return
            stream_state.closed = True
            # Determine the terminal marker:
            #   * cancelled=True → cancellation, not an error
            #   * error payload present → error
            #   * otherwise → normal completion
            if payload.cancelled:
                marker: Any = {"__cancelled__": True}
            elif payload.error is not None:
                marker = payload.error
            else:
                marker = {"__done__": True, "usage": payload.usage}
            try:
                stream_state.queue.put_nowait(marker)
            except asyncio.QueueFull:
                # Should not happen — the consumer is reading. Log
                # and drop the marker; the stream will be cleaned
                # up when the consumer's generator is cancelled.
                logger.warning(
                    "Stream queue full when pushing terminal marker "
                    "for stream %r; marker dropped.",
                    payload.stream_id,
                )
            return

        # ==============================================================
        # Error — resolve or fail pending future, or fail the stream.
        # ==============================================================
        if msg_type == MessageType.ERROR:
            try:
                error_payload = ErrorPayload.model_validate(
                    envelope.payload
                )
            except Exception:
                # Malformed error — treat as a generic protocol error.
                error_payload = ErrorPayload(
                    code="unknown_worker_error",
                    message=(
                        f"Malformed ERROR payload: "
                        f"{envelope.payload!r}"
                    ),
                )

            # Is this an error for a specific request?
            if envelope.id and envelope.id in self._pending_replies:
                self._fail_pending(envelope.id, error_payload)
                return

            # No specific request — could be a stream error. Push to
            # every active stream that has a matching pattern... but
            # we cannot know which stream without context. The safest
            # behavior is: log the error and continue, because a
            # worker SHOULD send stream-scoped errors as STREAM_END
            # with an error payload, not as ERROR without context.
            logger.warning(
                "Worker for provider=%r sent unscoped ERROR: "
                "code=%s message=%s",
                self._provider_id,
                error_payload.code,
                error_payload.message,
            )
            return

        # ==============================================================
        # Log — forward to the main process's logger.
        # ==============================================================
        if msg_type == MessageType.LOG:
            try:
                log_payload = _parse_log_payload(envelope.payload)
                logger.log(
                    log_payload["level"],
                    "[worker %s] %s",
                    log_payload["logger_name"] or "adapter",
                    log_payload["message"],
                )
            except Exception:
                pass
            return

        # ==============================================================
        # Unknown — log and drop.
        # ==============================================================
        logger.debug(
            "Unhandled message type from worker: %s",
            msg_type,
        )

    # ------------------------------------------------------------------
    # Pending future management
    # ------------------------------------------------------------------

    def _resolve_pending(
        self, request_id: Optional[str], payload: Dict[str, Any]
    ) -> None:
        """Resolve the future for ``request_id`` with ``payload``."""
        if request_id is None:
            return
        future = self._pending_replies.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(payload)

    def _fail_pending(
        self, request_id: str, error_payload: ErrorPayload
    ) -> None:
        """Fail the future for ``request_id`` with a mapped exception."""
        future = self._pending_replies.pop(request_id, None)
        if future is None or future.done():
            return
        future.set_exception(
            _payload_to_exception(error_payload, self._provider_id)
        )

    def _handle_protocol_mismatch(self, worker_version: int) -> None:
        """
        Fail every pending future and mark dead if the worker speaks a
        different protocol version.
        """
        from peldrun.llm.plugin_host.protocol import PROTOCOL_VERSION

        message = (
            f"Worker for provider '{self._provider_id}' speaks "
            f"protocol version {worker_version}, but this supervisor "
            f"speaks {PROTOCOL_VERSION}. Reinstall the package."
        )
        logger.error(message)
        self._dead = True

        for request_id, future in list(self._pending_replies.items()):
            if not future.done():
                future.set_exception(
                    ProviderContractIncompatibleError(
                        message,
                        provider=self._provider_id,
                        details={
                            "worker_protocol": worker_version,
                            "supervisor_protocol": PROTOCOL_VERSION,
                        },
                    )
                )
        self._pending_replies.clear()

    # ------------------------------------------------------------------
    # Request senders
    # ------------------------------------------------------------------

    async def _send_and_wait(
        self,
        msg_type: MessageType,
        payload: Any,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Send a request and wait for its reply. Returns the reply payload
        as a raw dict.

        Parameters
        ----------
        msg_type
            The message type to send.
        payload
            A Pydantic payload model instance.
        timeout
            Maximum wait for the reply. Defaults to
            ``REQUEST_TIMEOUT_SECONDS``.

        Raises
        ------
        WorkerCrashedError
            When the worker died before replying.
        WorkerStartupError
            When ``start()`` was never called, or the worker is dead.
        ProviderError
            Any error mapped from the worker's ``ErrorPayload``.
        ProviderTimeoutError
            When ``timeout`` expires before the reply arrives.
        """
        if not self._started or self._transport is None:
            raise WorkerStartupError(
                f"Worker for provider '{self._provider_id}' is not "
                f"running. Call start() first.",
                provider=self._provider_id,
            )
        if self._dead:
            raise WorkerCrashedError(
                f"Worker for provider '{self._provider_id}' has "
                f"already exited; the supervisor cannot be reused.",
                provider=self._provider_id,
            )

        request_id = new_request_id()
        loop = asyncio.get_running_loop()
        reply_future: asyncio.Future = loop.create_future()
        self._pending_replies[request_id] = reply_future

        effective_timeout = timeout or REQUEST_TIMEOUT_SECONDS

        try:
            envelope = make_request(msg_type, payload, request_id=request_id)
            try:
                await self._transport.write_envelope(envelope)
            except TransportError as exc:
                raise _map_transport_error(
                    exc, provider_id=self._provider_id
                ) from exc

            try:
                result = await asyncio.wait_for(
                    reply_future, timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                # Cancel the future so the read loop's late reply is
                # discarded cleanly.
                if not reply_future.done():
                    reply_future.cancel()
                raise ProviderTimeoutError(
                    f"Worker for provider '{self._provider_id}' did "
                    f"not reply to {msg_type.value!r} within "
                    f"{effective_timeout:.0f}s.",
                    provider=self._provider_id,
                    phase="plugin_request",
                )
            return result
        finally:
            self._pending_replies.pop(request_id, None)

    async def _send_load_adapter(self) -> None:
        """
        Send ``LOAD_ADAPTER`` (fire-and-await-ack via
        ``_adapter_loaded_event``, not via a pending future).

        Handled separately because ``start()`` has already set up the
        events that the read loop uses to signal readiness.
        """
        payload = LoadAdapterPayload(
            provider_id=self._provider_id,
            entry_point=self._entry_point,
            connection=self._connection.model_dump(),
            profile=self._adapter_profile_dict,
            permissions=self._permissions,
            package_dir=str(self._package_dir),
        )
        request_id = new_request_id()

        # We still register a pending future so the ADAPTER_LOADED
        # reply can also be correlated with an explicit request_id in
        # logs. The actual readiness signal is the event, set by the
        # read loop on receipt.
        loop = asyncio.get_running_loop()
        reply_future: asyncio.Future = loop.create_future()
        self._pending_replies[request_id] = reply_future
        try:
            envelope = make_request(
                MessageType.LOAD_ADAPTER, payload, request_id=request_id
            )
            await self._transport.write_envelope(envelope)
        except TransportError as exc:
            self._pending_replies.pop(request_id, None)
            raise WorkerStartupError(
                f"Failed to send LOAD_ADAPTER to worker: {exc.message}",
                provider=self._provider_id,
            ) from exc

    # ------------------------------------------------------------------
    # Public request methods
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> Dict[str, Any]:
        """
        Send a non-streaming generation request.

        Returns the serialized ``LLMResponse`` dict. The
        ``custom_plugin`` adapter rehydrates it back to a real
        ``LLMResponse`` instance for the caller.
        """
        payload = GeneratePayload(request=request.model_dump())
        reply = await self._send_and_wait(
            MessageType.GENERATE, payload
        )
        try:
            return GenerateResultPayload.model_validate(reply).response
        except Exception as exc:
            raise SupervisorError(
                f"Malformed GENERATE_RESULT from worker: {exc}",
                provider=self._provider_id,
                details={"payload_keys": list(reply.keys())},
            ) from exc

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Send a streaming generation request and yield events as they
        arrive.

        The generator emits every event the worker produces, ending
        when the worker sends ``STREAM_END``. On cancellation (the
        caller cancels the generator), the supervisor sends
        ``STREAM_CANCEL`` and waits briefly for the worker's
        ``STREAM_END`` before returning.

        Raises
        ------
        WorkerCrashedError
            If the worker dies mid-stream.
        ProviderError
            If the worker sends an error as the terminal event.
        """
        if not self._started or self._transport is None:
            raise WorkerStartupError(
                f"Worker for provider '{self._provider_id}' is not "
                f"running.",
                provider=self._provider_id,
            )
        if self._dead:
            raise WorkerCrashedError(
                f"Worker for provider '{self._provider_id}' has "
                f"already exited.",
                provider=self._provider_id,
            )

        # Send STREAM_START and wait for STREAM_STARTED with the id.
        payload = StreamStartPayload(request=request.model_dump())
        start_reply = await self._send_and_wait(
            MessageType.STREAM_START, payload
        )
        try:
            stream_id = StreamStartedPayload.model_validate(
                start_reply
            ).stream_id
        except Exception as exc:
            raise SupervisorError(
                f"Malformed STREAM_STARTED from worker: {exc}",
                provider=self._provider_id,
            ) from exc

        stream_state = _StreamState()
        self._active_streams[stream_id] = stream_state

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        stream_state.queue.get(),
                        timeout=STREAM_STALL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await self._cancel_stream(stream_id)
                    raise StreamStalledError(
                        f"Stream for provider "
                        f"'{self._provider_id}' stalled — no events "
                        f"for {STREAM_STALL_TIMEOUT_SECONDS:.0f}s.",
                        provider=self._provider_id,
                        phase="stream",
                    )

                # Terminal markers.
                if isinstance(item, dict):
                    if item.get("__cancelled__"):
                        return
                    if item.get("__done__"):
                        return

                if isinstance(item, ErrorPayload):
                    raise _payload_to_exception(
                        item, self._provider_id
                    )

                # Regular event — rehydrate the dict into LLMStreamEvent.
                try:
                    event = LLMStreamEvent.model_validate(item)
                except Exception as exc:
                    logger.warning(
                        "Malformed stream event from worker for "
                        "provider=%r: %s",
                        self._provider_id,
                        exc,
                    )
                    continue
                yield event

        except asyncio.CancelledError:
            # Caller cancelled the generator. Send a cancel request so
            # the worker can stop generating server-side.
            await self._cancel_stream(stream_id)
            raise

        finally:
            self._active_streams.pop(stream_id, None)

    async def _cancel_stream(self, stream_id: str) -> None:
        """
        Send STREAM_CANCEL and wait briefly for the worker to
        acknowledge (via STREAM_END). Best-effort — no exception on
        failure.
        """
        if self._transport is None or self._dead:
            return
        try:
            cancel_payload = StreamCancelPayload(stream_id=stream_id)
            envelope = make_request(
                MessageType.STREAM_CANCEL, cancel_payload
            )
            await self._transport.write_envelope(envelope)
            # Wait for the read loop to mark the stream as closed.
            # The stream_state was already set to closed by
            # _dispatch_envelope when STREAM_END arrived — but the
            # consumer stopped reading the queue when it caught
            # CancelledError. That's fine; the state is cleaned up by
            # the `finally` in `stream()`.
            await asyncio.sleep(0)
        except Exception as exc:
            logger.debug(
                "STREAM_CANCEL send failed for provider=%r: %s",
                self._provider_id,
                exc,
            )

    async def discover_models(
        self,
    ) -> List[Dict[str, Any]]:
        """Request model discovery. Returns the raw adapter list."""
        payload = DiscoverModelsPayload(
            connection=self._connection.model_dump()
        )
        reply = await self._send_and_wait(
            MessageType.DISCOVER_MODELS, payload
        )
        try:
            return list(
                ModelsResultPayload.model_validate(reply).models
            )
        except Exception as exc:
            raise SupervisorError(
                f"Malformed MODELS_RESULT from worker: {exc}",
                provider=self._provider_id,
            ) from exc

    async def validate_connection(
        self,
    ) -> Dict[str, Any]:
        """Request a connection probe. Returns the raw result dict."""
        payload = ValidateConnectionPayload(
            connection=self._connection.model_dump()
        )
        reply = await self._send_and_wait(
            MessageType.VALIDATE_CONNECTION, payload
        )
        try:
            validated = ConnectionResultPayload.model_validate(reply)
        except Exception as exc:
            raise SupervisorError(
                f"Malformed CONNECTION_RESULT from worker: {exc}",
                provider=self._provider_id,
            ) from exc
        return {
            "ok": validated.ok,
            "message": validated.message,
            "latency_ms": validated.latency_ms,
            "code": validated.code,
            "status_code": validated.status_code,
        }

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """Request per-model metadata. Returns ``None`` when absent."""
        payload = GetModelMetadataPayload(model_id=model_id)
        reply = await self._send_and_wait(
            MessageType.GET_MODEL_METADATA, payload
        )
        try:
            result = MetadataResultPayload.model_validate(reply)
        except Exception as exc:
            raise SupervisorError(
                f"Malformed METADATA_RESULT from worker: {exc}",
                provider=self._provider_id,
            ) from exc
        return result.metadata


# ---------------------------------------------------------------------------
# Error mapping helpers
# ---------------------------------------------------------------------------


def _get_protocol_version() -> int:
    """Lazy import to avoid a circular dependency at module load."""
    from peldrun.llm.plugin_host.protocol import PROTOCOL_VERSION

    return PROTOCOL_VERSION


def _parse_log_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert the worker's ``LogPayload`` into a dict ready for
    ``logging.Logger.log(level, ...)``.

    Malformed payloads fall back to DEBUG level.
    """
    from peldrun.llm.plugin_host.protocol import LogPayload

    try:
        lp = LogPayload.model_validate(payload)
    except Exception:
        return {
            "level": logging.DEBUG,
            "message": str(payload)[:500],
            "logger_name": "",
        }

    # Normalize the level string to a numeric one.
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    level = level_map.get(
        str(lp.level).upper(), logging.DEBUG
    )

    return {
        "level": level,
        "message": lp.message,
        "logger_name": lp.logger_name,
    }


def _map_transport_error(
    exc: TransportError, *, provider_id: str
) -> ProviderError:
    """
    Map a ``TransportError`` to a canonical ``ProviderError``.

    Translation table
    -----------------
        TransportClosedError             → WorkerCrashedError
        TransportWriteError              → ProviderConnectionError
        TransportReadError               → ProviderUnavailableError
        TransportMessageTooLargeError    → ProviderInvalidRequestError
        TransportProtocolError           → ProviderResponseParseError
        TransportError (base)            → ProviderError

    The mapping preserves retryability — transport failures are almost
    always retryable (a fresh worker or retry may succeed); protocol
    failures are not (the message shape will not change).
    """
    from peldrun.llm.errors import (
        ProviderInvalidRequestError,
        ProviderResponseParseError,
    )

    if isinstance(exc, TransportClosedError):
        return WorkerCrashedError(
            f"Worker for provider '{provider_id}' closed the "
            f"transport: {exc.message}",
            provider=provider_id,
        )
    if isinstance(exc, TransportMessageTooLargeError):
        return ProviderInvalidRequestError(
            f"IPC message exceeded the size limit: {exc.message}",
            provider=provider_id,
        )
    if isinstance(exc, TransportProtocolError):
        return ProviderResponseParseError(
            f"Worker sent a malformed protocol message: {exc.message}",
            provider=provider_id,
        )
    if isinstance(exc, TransportWriteError):
        return ProviderConnectionError(
            f"Could not write to worker: {exc.message}",
            provider=provider_id,
        )
    if isinstance(exc, TransportReadError):
        return ProviderUnavailableError(
            f"Could not read from worker: {exc.message}",
            provider=provider_id,
        )
    return ProviderError(
        f"Transport error: {exc.message}",
        provider=provider_id,
    )


def _payload_to_exception(
    payload: ErrorPayload, provider_id: str
) -> ProviderError:
    """
    Reconstruct a ``ProviderError`` (or appropriate subclass) from an
    ``ErrorPayload``.

    This is the inverse of the worker's ``_exception_to_error_payload``.
    The mapping is best-effort — the exact ``ProviderError`` subclass
    is inferred from ``payload.code``. Codes that do not match a known
    pattern become a generic ``ProviderError`` so the caller still
    sees a structured exception.
    """
    from peldrun.llm.errors import (
        ProviderAuthError,
        ProviderConnectionError,
        ProviderContentPolicyError,
        ProviderContextLengthError,
        ProviderInvalidRequestError,
        ProviderQuotaExceededError,
        ProviderRateLimitError,
        ProviderResponseParseError,
        ProviderTimeoutError,
        ProviderUnsupportedFeatureError,
    )

    code = (payload.code or "").lower()

    common = {
        "provider": provider_id,
        "status_code": payload.status_code,
        "details": payload.details,
    }

    # Auth.
    if code in ("auth_failed", "permission_denied"):
        return ProviderAuthError(payload.message, **common)

    # Rate limit / quota.
    if code == "rate_limited":
        return ProviderRateLimitError(payload.message, **common)
    if code == "quota_exceeded":
        return ProviderQuotaExceededError(payload.message, **common)

    # Timeout / availability.
    if code == "timeout":
        return ProviderTimeoutError(
            payload.message, phase="plugin", **common
        )
    if code in ("connection_failed",):
        return ProviderConnectionError(payload.message, **common)
    if code in ("unavailable",):
        return ProviderUnavailableError(payload.message, **common)

    # Request validation.
    if code in ("invalid_request",):
        return ProviderInvalidRequestError(payload.message, **common)
    if code in ("context_length_exceeded",):
        return ProviderContextLengthError(payload.message, **common)
    if code in ("unsupported_feature",):
        return ProviderUnsupportedFeatureError(payload.message, **common)

    # Content policy.
    if code in ("content_policy",):
        return ProviderContentPolicyError(payload.message, **common)

    # Parsing.
    if code in ("response_parse_error",):
        return ProviderResponseParseError(payload.message, **common)

    # Plugin-specific codes (from ``protocol.ProtocolErrorCode``).
    if code in (
        "plugin_worker_crashed",
        "worker_crashed",
    ):
        return WorkerCrashedError(payload.message, **common)
    if code in (
        "plugin_worker_startup_failed",
        "startup_timeout",
        "adapter_load_failed",
    ):
        return WorkerStartupError(payload.message, **common)
    if code in ("plugin_stream_stalled",):
        return StreamStalledError(
            payload.message, phase="stream", **common
        )

    # Fallback.
    return ProviderError(payload.message, **common)


__all__ = [
    "STREAM_QUEUE_MAX",
    "STREAM_STALL_TIMEOUT_SECONDS",
    "SupervisorError",
    "WorkerCrashedError",
    "WorkerStartupError",
    "StreamStalledError",
    "WorkerSupervisor",
]
