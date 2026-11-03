# backend/peldrun/llm/plugin_host/transport.py
"""
JSON Lines transport for the Plugin Host IPC channel.

Two concrete transports, one shared contract:

    * ``SubprocessTransport`` — used by the SUPERVISOR (main process).
      Wraps the ``asyncio.subprocess.Process`` returned by
      ``create_subprocess_exec`` and reads/writes over its
      ``stdout``/``stdin`` pipes.

    * ``StdioTransport`` — used by the WORKER (subprocess).
      Reads from ``sys.stdin.buffer`` and writes to a captured
      reference to ``sys.stdout.buffer`` (captured before any code
      writes to ``sys.stdout``).

Both implement the same three async methods:

    * ``read_envelope() -> Envelope``   — read and validate one message.
    * ``write_envelope(env) -> None``   — serialize and write one message.
    * ``close() -> None``               — release resources.

Why two classes and not one
---------------------------

A ``StreamReader``/``StreamWriter`` pair (supervisor) and raw
``sys.stdin.buffer``/``sys.stdout.buffer`` (worker) have very different
APIs. Unifying them behind a single class with ``if isinstance(...)``
branches would obscure both code paths without saving meaningful lines.
Two thin adapters with the same contract are easier to reason about and
to test in isolation.

Design invariants
-----------------

1. **Line-oriented framing only.** A message is one UTF-8 line, with
   a trailing ``\\n``. The reader enforces ``MAX_MESSAGE_BYTES`` so a
   malicious or buggy peer cannot exhaust memory by sending a single
   enormous line.

2. **Pydantic-validated on read.** ``Envelope.model_validate_json``
   rejects malformed JSON, missing required fields, unknown message
   types, and extra top-level keys (``extra="forbid"`` in the model).
   A bad message raises ``TransportProtocolError`` — never a partially
   populated ``Envelope``.

3. **Write is serialized by an ``asyncio.Lock``.** Two concurrent
   ``write_envelope`` calls cannot interleave bytes on the wire. This
   matters because the supervisor may send a cancel request while a
   stream-event writer is active.

4. **EOF raises a distinct exception.** ``TransportClosedError`` is
   raised when the peer closes the pipe. This is EXPECTED on graceful
   shutdown and MUST NOT be logged at warning level by callers. Other
   I/O failures raise ``TransportReadError`` / ``TransportWriteError``
   and are genuinely alarming.

5. **stdout is hijacked by the worker.** The worker redirects
   ``sys.stdout`` to ``sys.stderr`` at startup so accidental
   ``print()`` calls from the adapter do not corrupt the protocol
   channel. The ORIGINAL binary stdout is captured for protocol use
   only. ``capture_stdout_for_protocol()`` performs the hijack.

6. **No logging on the hot path.** ``logging.debug`` is called at most
   once per message; everything else is silent to keep latency low
   for streaming.

See also
--------

    * ``backend/peldrun/llm/plugin_host/protocol.py`` — message schema
    * ``backend/peldrun/llm/plugin_host/supervisor.py`` — uses
      ``SubprocessTransport``
    * ``backend/peldrun/llm/plugin_host/worker.py`` — uses
      ``StdioTransport``
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from typing import BinaryIO, Optional

from pydantic import ValidationError as PydanticValidationError

from peldrun.llm.plugin_host.protocol import (
    MAX_MESSAGE_BYTES,
    Envelope,
)

logger = logging.getLogger("peldrun.llm.plugin_host.transport")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """
    Base class for every transport-level failure.

    ``code`` is a stable string so the supervisor can map each failure
    to an appropriate ``ProviderError`` subclass (see
    ``supervisor._map_transport_error``).
    """

    code: str = "TRANSPORT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class TransportClosedError(TransportError):
    """
    The peer closed its end of the pipe (EOF).

    EXPECTED on graceful shutdown. Callers MUST catch this and treat
    it as a normal "worker exited" signal — never log at WARNING.
    """

    code = "TRANSPORT_CLOSED"


class TransportReadError(TransportError):
    """
    A non-EOF read failure occurred (I/O error, broken pipe, invalid
    UTF-8 on a line that was not a clean close).

    The supervisor escalates this to
    ``ProviderUnavailableError``; a read failure usually means the
    worker crashed mid-response.
    """

    code = "TRANSPORT_READ_ERROR"


class TransportWriteError(TransportError):
    """
    A write to the peer failed (broken pipe, closed stdin).

    When the worker sees this, it should exit cleanly — the supervisor
    is gone. When the supervisor sees this, it should treat the worker
    as dead.
    """

    code = "TRANSPORT_WRITE_ERROR"


class TransportMessageTooLargeError(TransportError):
    """
    A single message exceeded ``MAX_MESSAGE_BYTES``.

    The reader aborts the connection rather than truncating — a
    truncated JSON line cannot be parsed, and silently dropping the
    peer's subsequent bytes would desynchronize the channel.
    """

    code = "TRANSPORT_MESSAGE_TOO_LARGE"


class TransportProtocolError(TransportError):
    """
    A message was received but did not parse as a valid ``Envelope``.

    Possible causes:
        * Invalid JSON.
        * Missing required field (``v``, ``type``).
        * Unknown ``type`` value.
        * Extra top-level field (``extra="forbid"``).

    The offending line is logged at DEBUG level only — never at INFO —
    because a malformed message may contain adapter-generated content
    that should not end up in the main log.
    """

    code = "TRANSPORT_PROTOCOL_ERROR"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_for_log(line: bytes, limit: int = 200) -> str:
    """
    Return a short, safe-to-log representation of a message line.

    Never includes more than ``limit`` characters. Uses ``repr`` so
    embedded control characters do not break the log output.
    """
    try:
        text = line[:limit].decode("utf-8", errors="replace")
        suffix = "..." if len(line) > limit else ""
        return repr(text) + suffix
    except Exception:
        return "<unparseable>"


def _parse_envelope(line: bytes) -> Envelope:
    """
    Parse and validate a single JSON line into an ``Envelope``.

    Raises ``TransportProtocolError`` on any parse or validation
    failure. On success, returns a fully validated envelope.
    """
    if not line:
        raise TransportClosedError("EOF while reading message line.")

    # Strip the trailing newline; pydantic's model_validate_json
    # accepts bytes and ignores surrounding whitespace, but stripping
    # makes error messages cleaner.
    stripped = line.rstrip(b"\r\n")
    if not stripped:
        raise TransportProtocolError("Empty line received (expected JSON).")

    try:
        return Envelope.model_validate_json(stripped)
    except PydanticValidationError as exc:
        logger.debug(
            "Invalid envelope received: %s (line=%s)",
            exc,
            _truncate_for_log(stripped),
        )
        raise TransportProtocolError(
            f"Invalid envelope: {exc.errors()[0].get('msg', 'unknown error')}"
        ) from exc
    except Exception as exc:
        logger.debug(
            "Unparseable message line: %s (line=%s)",
            exc,
            _truncate_for_log(stripped),
        )
        raise TransportProtocolError(
            f"Unparseable message line: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class Transport:
    """
    Shared contract for both transport implementations.

    The base class only declares the interface and the write-lock
    helper. Concrete subclasses implement I/O.
    """

    def __init__(self) -> None:
        # ``asyncio.Lock`` is created lazily on first use — creating
        # one in ``__init__`` requires a running event loop on some
        # Python versions.
        self._write_lock: Optional[asyncio.Lock] = None

    def _get_write_lock(self) -> asyncio.Lock:
        """Return the write lock, creating it on first use."""
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    # ------------------------------------------------------------------
    # To be implemented by subclasses
    # ------------------------------------------------------------------

    async def read_envelope(self) -> Envelope:
        raise NotImplementedError

    async def write_envelope(self, envelope: Envelope) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Supervisor-side transport
# ---------------------------------------------------------------------------


class SubprocessTransport(Transport):
    """
    Transport used by the SUPERVISOR to communicate with a worker.

    Wraps an ``asyncio.subprocess.Process``. Reads from
    ``process.stdout`` (the worker's writes) and writes to
    ``process.stdin`` (the worker's reads).

    The reader enforces ``MAX_MESSAGE_BYTES`` by accumulating chunks
    manually — ``StreamReader.readline()`` would happily buffer an
    unbounded amount of memory if the peer never sends a newline.

    Lifecycle
    ---------
    The supervisor OWNS the process. Calling ``close()`` closes the
    stdin pipe (signaling EOF to the worker), then waits for the
    process to exit with a short grace period before returning.
    Forceful termination is the caller's responsibility — the
    transport does not send SIGKILL, because it cannot know whether
    the worker is mid-cleanup.
    """

    def __init__(self, process: "asyncio.subprocess.Process") -> None:
        super().__init__()
        if process.stdin is None or process.stdout is None:
            raise TransportError(
                "SubprocessTransport requires a process created with "
                "stdin=PIPE and stdout=PIPE."
            )
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._closed = False

    # ------------------------------------------------------------------

    async def read_envelope(self) -> Envelope:
        """
        Read one JSON-line message from the worker.

        Raises
        ------
        TransportClosedError
            When the worker closed its stdout (process exited or
            closed the pipe).
        TransportMessageTooLargeError
            When the accumulated line exceeds ``MAX_MESSAGE_BYTES``.
        TransportReadError
            When an underlying I/O failure occurs.
        TransportProtocolError
            When the line is not a valid ``Envelope``.
        """
        if self._closed:
            raise TransportClosedError(
                "Transport is closed; cannot read."
            )

        buffer = bytearray()

        try:
            while True:
                # Read a small chunk. Using a fixed chunk size (rather
                # than readline) lets us enforce the size cap BEFORE
                # the StreamReader buffers the whole line.
                chunk = await self._stdout.read(8192)

                if not chunk:
                    # EOF. If we had partial data, the peer sent a
                    # truncated line — treat as protocol error.
                    if buffer:
                        raise TransportReadError(
                            "Worker closed stdout mid-message "
                            f"(received {len(buffer)} bytes without "
                            "a terminating newline)."
                        )
                    raise TransportClosedError(
                        "Worker closed stdout (clean EOF)."
                    )

                buffer.extend(chunk)

                if len(buffer) > MAX_MESSAGE_BYTES:
                    raise TransportMessageTooLargeError(
                        f"Message exceeded {MAX_MESSAGE_BYTES} bytes; "
                        f"aborting connection."
                    )

                # A complete line is terminated by \n. The chunk may
                # contain MORE than one line; the caller's next
                # read_envelope() will consume the remainder. Because
                # StreamReader is a buffered stream, that remainder is
                # already in the reader's internal buffer — no data is
                # lost.
                if b"\n" in chunk:
                    # Trim to the first newline; discard the tail.
                    newline_index = buffer.find(b"\n")
                    line = bytes(buffer[: newline_index + 1])
                    return _parse_envelope(line)

        except TransportError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportReadError(
                f"Unexpected read failure: {exc}"
            ) from exc

    # ------------------------------------------------------------------

    async def write_envelope(self, envelope: Envelope) -> None:
        """
        Write one JSON-line message to the worker.

        Raises
        ------
        TransportClosedError
            When the worker has closed its stdin (process exited).
        TransportWriteError
            On any underlying I/O failure.
        """
        if self._closed:
            raise TransportClosedError(
                "Transport is closed; cannot write."
            )

        line = envelope.to_json_line()
        if len(line) > MAX_MESSAGE_BYTES:
            raise TransportMessageTooLargeError(
                f"Attempted to send a {len(line)}-byte message, which "
                f"exceeds {MAX_MESSAGE_BYTES}. Check the payload for "
                f"unexpectedly large content."
            )

        async with self._get_write_lock():
            try:
                self._stdin.write(line)
                await self._stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise TransportClosedError(
                    f"Worker closed stdin: {exc}"
                ) from exc
            except Exception as exc:
                raise TransportWriteError(
                    f"Unexpected write failure: {exc}"
                ) from exc

    # ------------------------------------------------------------------

    async def close(self) -> None:
        """
        Close the stdin pipe and wait briefly for the process to exit.

        Idempotent. Does NOT send signals — the caller (supervisor) is
        responsible for escalating to SIGTERM/SIGKILL when the worker
        does not exit within its own timeout.
        """
        if self._closed:
            return
        self._closed = True

        try:
            self._stdin.close()
        except Exception:
            pass

        # Give the worker up to 2 seconds to exit after stdin closes.
        # Most well-behaved workers exit immediately.
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Worker-side transport
# ---------------------------------------------------------------------------


def capture_stdout_for_protocol() -> BinaryIO:
    """
    Redirect ``sys.stdout`` to ``sys.stderr`` and return the ORIGINAL
    binary stdout for protocol use.

    Called by the worker at startup, BEFORE any adapter code runs, so
    that:
        * ``print()`` calls from adapter code go to stderr (visible in
          the supervisor's logs, harmless to the protocol).
        * The protocol writer has exclusive use of the original binary
          stdout pipe.

    Returns the original ``sys.stdout.buffer`` — a ``BufferedWriter``
    wrapping the real pipe. The caller MUST keep this reference and
    pass it to ``StdioTransport``.

    Raises ``RuntimeError`` if called when ``sys.stdout`` has already
    been redirected (double-capture is a programming error).
    """
    if not hasattr(sys.stdout, "buffer"):
        raise RuntimeError(
            "sys.stdout has no binary buffer; cannot capture for "
            "protocol use. This usually means stdout was already "
            "redirected or replaced by a StringIO."
        )

    original = sys.stdout.buffer

    # Redirect text-level stdout to stderr. Adapter code that does
    # ``print("hello")`` now writes to the supervisor's log stream
    # rather than corrupting the protocol channel.
    sys.stdout = sys.stderr
    return original


class StdioTransport(Transport):
    """
    Transport used by the WORKER to communicate with the supervisor.

    Reads from ``sys.stdin.buffer`` and writes to a captured binary
    stdout (see ``capture_stdout_for_protocol``).

    The reader uses ``sys.stdin.buffer.readline(limit)`` in a thread
    executor so the async event loop remains responsive even though
    stdin is a blocking file descriptor. The ``limit`` argument
    enforces ``MAX_MESSAGE_BYTES`` at the OS read level, so we never
    allocate more than the limit + a small margin.

    The writer is synchronous at the OS level (a single ``write`` +
    ``flush``), which is fast enough not to justify a thread hop.
    The ``asyncio.Lock`` still serializes concurrent calls so that
    interleaved writes from a stream task and a periodic log flush
    do not corrupt the line.
    """

    def __init__(self, stdout_binary: Optional[BinaryIO] = None) -> None:
        super().__init__()
        if stdout_binary is None:
            # Auto-capture if the caller forgot to. This is the
            # correct default in the common case, but the worker
            # SHOULD call capture_stdout_for_protocol() explicitly
            # at the very top of its main() so that redirecting
            # stdout earlier is guaranteed.
            stdout_binary = capture_stdout_for_protocol()
        self._stdin = sys.stdin.buffer
        self._stdout = stdout_binary
        self._closed = False

    # ------------------------------------------------------------------

    async def read_envelope(self) -> Envelope:
        """
        Read one JSON-line message from the supervisor.

        Uses ``asyncio.to_thread`` so the blocking ``readline()`` does
        not stall the event loop. The read is bounded by
        ``MAX_MESSAGE_BYTES + 1`` — a message that would exceed the
        cap returns exactly ``MAX_MESSAGE_BYTES + 1`` bytes, which
        we detect and reject.

        Raises
        ------
        TransportClosedError
            When the supervisor closes the channel (EOF).
        TransportMessageTooLargeError
            When the line exceeds ``MAX_MESSAGE_BYTES``.
        TransportReadError
            On any underlying I/O failure.
        TransportProtocolError
            When the line is not a valid ``Envelope``.
        """
        if self._closed:
            raise TransportClosedError(
                "Transport is closed; cannot read."
            )

        try:
            # Read one line, bounded. ``readline(limit)`` returns AT
            # MOST ``limit`` bytes; if the line is longer, no newline
            # appears in the result, which is how we detect overflow.
            line = await asyncio.to_thread(
                self._stdin.readline,
                MAX_MESSAGE_BYTES + 1,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportReadError(
                f"Unexpected stdin read failure: {exc}"
            ) from exc

        if not line:
            raise TransportClosedError(
                "Supervisor closed stdin (clean EOF)."
            )

        if len(line) > MAX_MESSAGE_BYTES:
            raise TransportMessageTooLargeError(
                f"Received a line exceeding {MAX_MESSAGE_BYTES} "
                f"bytes; aborting. First 200 bytes: "
                f"{_truncate_for_log(line)}"
            )

        # A line longer than MAX_MESSAGE_BYTES but shorter than the
        # read limit will NOT end with \n. If the supervisor is
        # sending well-formed messages this cannot happen; if it does,
        # treat it as a protocol error.
        if not line.endswith(b"\n"):
            raise TransportReadError(
                "Received a partial line (no terminating newline). "
                "The supervisor may have crashed mid-write."
            )

        return _parse_envelope(line)

    # ------------------------------------------------------------------

    async def write_envelope(self, envelope: Envelope) -> None:
        """
        Write one JSON-line message to the supervisor.

        Raises
        ------
        TransportClosedError
            When the supervisor closed its stdin (main process gone).
        TransportWriteError
            On any underlying I/O failure.
        """
        if self._closed:
            raise TransportClosedError(
                "Transport is closed; cannot write."
            )

        line = envelope.to_json_line()
        if len(line) > MAX_MESSAGE_BYTES:
            raise TransportMessageTooLargeError(
                f"Attempted to send a {len(line)}-byte message, which "
                f"exceeds {MAX_MESSAGE_BYTES}."
            )

        async with self._get_write_lock():
            try:
                self._stdout.write(line)
                self._stdout.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise TransportClosedError(
                    f"Supervisor closed stdin: {exc}"
                ) from exc
            except Exception as exc:
                raise TransportWriteError(
                    f"Unexpected write failure: {exc}"
                ) from exc

    # ------------------------------------------------------------------

    async def close(self) -> None:
        """
        Close the transport. Idempotent.

        Does NOT close the underlying stdout — the worker process
        exits immediately after its main loop returns, and closing
        stdout before that point would prevent the final
        ``shutdown_ack`` message from being delivered.
        """
        self._closed = True


# ---------------------------------------------------------------------------
# Convenience: reading raw bytes (for tests)
# ---------------------------------------------------------------------------


class BytesBufferTransport(Transport):
    """
    In-memory transport used by unit tests.

    Feeds messages from a preloaded byte buffer and records every
    written message for later inspection. Not thread-safe by design —
    tests exercise one task at a time.
    """

    def __init__(self, incoming: bytes = b"") -> None:
        super().__init__()
        self._reader = io.BytesIO(incoming)
        self._written: list = []
        self._closed = False

    @property
    def written_lines(self) -> list:
        """Return every line written so far, decoded to text."""
        return [
            line.decode("utf-8", errors="replace") for line in self._written
        ]

    async def read_envelope(self) -> Envelope:
        if self._closed:
            raise TransportClosedError("Transport closed.")
        line = self._reader.readline()
        if not line:
            raise TransportClosedError("No more messages.")
        if len(line) > MAX_MESSAGE_BYTES:
            raise TransportMessageTooLargeError(
                f"Message exceeded {MAX_MESSAGE_BYTES} bytes."
            )
        return _parse_envelope(line)

    async def write_envelope(self, envelope: Envelope) -> None:
        if self._closed:
            raise TransportClosedError("Transport closed.")
        self._written.append(envelope.to_json_line())

    async def close(self) -> None:
        self._closed = True


__all__ = [
    # Exceptions
    "TransportError",
    "TransportClosedError",
    "TransportReadError",
    "TransportWriteError",
    "TransportMessageTooLargeError",
    "TransportProtocolError",
    # Base
    "Transport",
    # Concrete
    "SubprocessTransport",
    "StdioTransport",
    "BytesBufferTransport",
    # Helpers
    "capture_stdout_for_protocol",
]
