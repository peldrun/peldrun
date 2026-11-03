# backend/peldrun/llm/plugin_host/protocol.py
"""
IPC protocol between the Plugin Host supervisor (main process) and the
worker subprocess that runs an externally-installed Provider Package.

Wire format
-----------

**JSON Lines over stdin/stdout.** Each message is a single UTF-8 line
of JSON, terminated by ``\n``. No framing headers, no length prefixes,
no binary delimiters. Rationale:

    * Universal tooling — every language can read/write JSON lines.
    * Survives process crash — a partial line is discarded, not
      misinterpreted as the next message's start.
    * Debuggable — piping the channel through ``tee`` produces a
      human-readable transcript of every exchange.
    * Ordered — TCP-like ordering is guaranteed by the OS pipe
      semantics; no sequence numbers required.

``stderr`` is used exclusively for worker logs, never for protocol
traffic. This lets the supervisor attach worker logs to the main
process's logging handler without interfering with request handling.

Message envelope
----------------

Every message — in either direction — has the same envelope::

    {
      "v":     1,                 # protocol version (see PROTOCOL_VERSION)
      "id":    "req_abc123",      # request ID (UUID hex), echoed by replies
      "type":  "generate",        # message type (see MessageType below)
      "payload": { ... }          # type-specific body
    }

**Request ID correlation**
    * Every request from the supervisor carries an ``id``.
    * Every reply from the worker echoes the SAME ``id`` in its ``id``
      field. This lets the supervisor correlate concurrent requests
      without a per-call mutex (the worker serializes its own handling
      internally).
    * The worker MAY emit unsolicited messages (e.g. ``log``,
      ``stream_event`` while a stream is open, ``ready`` at startup).
      Those carry ``id: null`` or the id of the request they belong to.

**Version negotiation**
    * The supervisor sends ``PROTOCOL_VERSION`` in every request.
    * The worker validates on every request. Mismatch → error reply
      with ``code: PROTOCOL_VERSION_MISMATCH`` and no processing.
    * A future breaking change bumps the constant. Old workers refuse
      new supervisors and vice versa; the supervisor surfaces the
      mismatch as a ``ProviderContractIncompatibleError``.

Design invariants
-----------------

1. **All payloads are Pydantic-validated on receipt.** An
   unexpected field name, a missing required field, or a
   wrong-typed value is a hard error — the message is rejected with
   a structured error reply, never silently accepted.

2. **No binary payloads.** Multimodal content (images) is base64-
   encoded before transmission. The extra 33 % bandwidth cost is
   acceptable given that image tokens dominate real requests; the
   benefit is that the whole channel stays text-readable for
   debugging.

3. **No callback URLs, no network.** The worker never accepts a
   message that asks it to call back to a URL, evaluate a string, or
   open a socket other than the one implied by ``generate`` / ``stream``
   / ``discover_models`` / ``validate_connection``. This keeps the
   protocol surface auditable.

4. **Idempotent shutdown.** A ``shutdown`` message is sent by the
   supervisor exactly once. If the worker does not exit within
   ``SHUTDOWN_TIMEOUT_SECONDS``, the supervisor sends SIGTERM, then
   SIGKILL. Every supervisor method assumes the worker may already
   be gone; calls return errors rather than block.

5. **Error taxonomy mirrors ``llm/errors.py``.** The worker converts
   every adapter exception to a ``ProviderError.to_dict()`` payload
   before replying. The supervisor rehydrates it into the same
   exception class. This means the caller of
   ``custom_plugin.generate()`` sees the exact same error types as
   the caller of a built-in adapter — the IPC boundary is invisible.

See also
--------

    * ``backend/peldrun/llm/plugin_host/transport.py`` — envelope I/O
    * ``backend/peldrun/llm/plugin_host/worker.py`` — receives and
      handles messages
    * ``backend/peldrun/llm/plugin_host/supervisor.py`` — sends and
      correlates messages
    * ``backend/peldrun/llm/errors.py`` — canonical error taxonomy
"""

from __future__ import annotations

import logging
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.llm.plugin_host.protocol")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Current protocol version. Bumped ONLY when the message schema or
#: the envelope format changes incompatibly. Adding a new message type
#: or a new optional payload field does NOT bump this — old workers
#: simply receive a type they do not recognize, and the supervisor
#: falls back gracefully.
PROTOCOL_VERSION: int = 1


#: Maximum size, in bytes, of a single JSON line. Guards against a
#: malicious or buggy package sending an arbitrarily large payload.
#: 64 MB accommodates a 16 MB image after base64 expansion (≈ 21 MB)
#: plus normal overhead.
MAX_MESSAGE_BYTES: int = 64 * 1024 * 1024


#: How long the supervisor waits for a worker to send its ``ready``
#: message after spawn. If the worker is not ready within this window,
#: the supervisor kills it and returns a startup error. 30s covers
#: cold Python import times on slow disks.
WORKER_STARTUP_TIMEOUT_SECONDS: float = 30.0


#: How long the supervisor waits for a reply to a non-streaming
#: request. Longer than the per-request timeout of any adapter, so
#: that legitimate long-running generations are not cut off.
REQUEST_TIMEOUT_SECONDS: float = 600.0


#: How long the supervisor waits for a graceful ``shutdown`` reply
#: before escalating to SIGTERM. Kept short — a well-behaved worker
#: exits within milliseconds.
SHUTDOWN_TIMEOUT_SECONDS: float = 5.0


#: How long the supervisor waits between SIGTERM and SIGKILL. Short
#: on purpose — SIGKILL is a last resort, not a routine step.
SIGTERM_GRACE_SECONDS: float = 3.0


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class MessageType(str, Enum):
    """
    Every message kind exchanged over the IPC channel.

    Naming convention: ``<direction>_<action>`` where direction is
    implicit (supervisor→worker = imperative verb; worker→supervisor
    = past-tense verb or noun). Both directions share the same enum
    so type dispatch is a single ``match`` statement in the worker.
    """

    # ==================================================================
    # Supervisor → Worker (requests)
    # ==================================================================

    #: Load the adapter class. Sent once, immediately after the worker
    #: announces ``ready``. Payload carries the connection context,
    #: permissions, and adapter entry point.
    LOAD_ADAPTER = "load_adapter"

    #: Execute a non-streaming completion. Payload carries the full
    #: ``LLMRequest`` serialized to JSON (messages, tools, generation
    #: config, reasoning config, response_format).
    GENERATE = "generate"

    #: Begin streaming. Payload carries the same ``LLMRequest`` shape
    #: as ``GENERATE``. The worker responds with ``stream_started``
    #: followed by zero or more ``stream_event`` messages, ending with
    #: ``stream_end``.
    STREAM_START = "stream_start"

    #: Cancel an active stream. Payload carries the ``stream_id``
    #: returned by ``stream_started``. The worker emits ``stream_end``
    #: with ``cancelled: true`` on success.
    STREAM_CANCEL = "stream_cancel"

    #: Discover models available on the connection. Payload carries
    #: the connection context (same shape as ``LOAD_ADAPTER``).
    DISCOVER_MODELS = "discover_models"

    #: Probe the connection without sending a generation request.
    VALIDATE_CONNECTION = "validate_connection"

    #: Fetch provider-specific metadata for a single model.
    GET_MODEL_METADATA = "get_model_metadata"

    #: Graceful shutdown. The worker responds with ``shutdown_ack``
    #: and exits. The supervisor falls back to signals if the ack
    #: does not arrive within ``SHUTDOWN_TIMEOUT_SECONDS``.
    SHUTDOWN = "shutdown"

    # ==================================================================
    # Worker → Supervisor (replies and unsolicited)
    # ==================================================================

    #: Sent once at startup, BEFORE any request is accepted. Carries
    #: the worker's protocol version and Python version so the
    #: supervisor can detect incompatibility early.
    READY = "ready"

    #: Reply to ``LOAD_ADAPTER``. Payload carries the loaded
    #: adapter's ``AdapterProfile`` dict (for capability display in
    #: the UI) — or an error if the adapter failed to import.
    ADAPTER_LOADED = "adapter_loaded"

    #: Reply to ``GENERATE``. Payload carries the serialized
    #: ``LLMResponse``.
    GENERATE_RESULT = "generate_result"

    #: Reply to ``STREAM_START``. Payload carries the ``stream_id``
    #: the supervisor must use for cancellation and correlation.
    STREAM_STARTED = "stream_started"

    #: Emitted for each event in an active stream. Payload carries
    #: a single ``LLMStreamEvent`` dict.
    STREAM_EVENT = "stream_event"

    #: Emitted at the end of a stream (success, error, or cancel).
    #: Payload carries ``cancelled: bool``, optional ``error``, and
    #: a final ``usage`` object.
    STREAM_END = "stream_end"

    #: Reply to ``DISCOVER_MODELS``. Payload carries a list of model
    #: descriptor dicts (same shape the in-process adapter returns).
    MODELS_RESULT = "models_result"

    #: Reply to ``VALIDATE_CONNECTION``. Payload carries the
    #: ``{ok, message, latency_ms}`` dict.
    CONNECTION_RESULT = "connection_result"

    #: Reply to ``GET_MODEL_METADATA``. Payload carries the metadata
    #: dict, or ``null`` when the provider has no per-model endpoint.
    METADATA_RESULT = "metadata_result"

    #: Reply to ``SHUTDOWN``. The worker exits immediately after
    #: sending this.
    SHUTDOWN_ACK = "shutdown_ack"

    #: Generic error reply. Payload carries ``ProviderError.to_dict()``.
    #: Emitted for any message that fails validation, any request
    #: that hits an unhandled exception, and protocol-level failures
    #: (version mismatch, oversized message).
    ERROR = "error"

    #: Unsolicited log line from the worker. Payload carries
    #: ``{level, message, logger_name}``. Used to forward worker
    #: diagnostics to the main process's logs WITHOUT polluting the
    #: protocol channel with non-message content.
    LOG = "log"


# ---------------------------------------------------------------------------
# Error codes (protocol-level, not adapter-level)
# ---------------------------------------------------------------------------


class ProtocolErrorCode(str, Enum):
    """
    Stable string codes for protocol-level errors.

    These are DISTINCT from ``llm/errors.py``'s codes — those are
    provider failures, these are transport failures. The supervisor
    maps each one to an appropriate ``ProviderError`` subclass when
    re-raising to the caller.
    """

    #: ``v`` field in the envelope does not match ``PROTOCOL_VERSION``.
    PROTOCOL_VERSION_MISMATCH = "PROTOCOL_VERSION_MISMATCH"

    #: The line exceeded ``MAX_MESSAGE_BYTES``.
    MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE"

    #: The line was not valid JSON.
    INVALID_JSON = "INVALID_JSON"

    #: The envelope was valid JSON but failed Pydantic validation.
    INVALID_ENVELOPE = "INVALID_ENVELOPE"

    #: The payload failed Pydantic validation for its ``type``.
    INVALID_PAYLOAD = "INVALID_PAYLOAD"

    #: The message ``type`` is unknown to this worker.
    UNKNOWN_MESSAGE_TYPE = "UNKNOWN_MESSAGE_TYPE"

    #: A request arrived before ``LOAD_ADAPTER`` completed.
    ADAPTER_NOT_LOADED = "ADAPTER_NOT_LOADED"

    #: ``LOAD_ADAPTER`` failed (import error, missing class, etc.).
    ADAPTER_LOAD_FAILED = "ADAPTER_LOAD_FAILED"

    #: The worker tried to use a permission the manifest did not grant.
    PERMISSION_DENIED = "PERMISSION_DENIED"

    #: The worker crashed mid-request. Emitted by the supervisor
    #: (never by the worker itself — a crashed worker cannot emit).
    WORKER_CRASHED = "WORKER_CRASHED"

    #: The request did not receive a reply within the timeout.
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"

    #: The worker did not become ready within
    #: ``WORKER_STARTUP_TIMEOUT_SECONDS``.
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"

    #: The stream was cancelled by the supervisor (expected when the
    #: user clicks Stop); not a real error but surfaced as one for
    #: uniform error handling.
    STREAM_CANCELLED = "STREAM_CANCELLED"


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """Generate a fresh request ID for correlation."""
    return f"req_{uuid.uuid4().hex[:16]}"


class Envelope(BaseModel):
    """
    The outer wrapper for every IPC message.

    ``payload`` is deliberately typed as ``Dict[str, Any]`` because
    its concrete shape depends on ``type``. Each handler casts it to
    the correct Pydantic model on receipt. This design lets the
    envelope stay stable across future message additions — a new
    message type is a new handler, not a schema migration of the
    envelope.

    ``id`` is ``None`` for unsolicited messages (``ready``, ``log``)
    and for stream events that are not correlated to a specific
    request. Requests always have an id; replies echo it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    v: int = Field(
        ...,
        description="Protocol version. Must equal PROTOCOL_VERSION.",
    )
    id: Optional[str] = Field(
        default=None,
        description=(
            "Request ID. Required for every message in a request/"
            "reply pair. None for unsolicited messages (ready, log)."
        ),
    )
    type: MessageType = Field(
        ...,
        description="Message kind. Dispatched by the worker's handler.",
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Type-specific body. Validated against the corresponding "
            "Pydantic model on receipt."
        ),
    )

    def to_json_line(self) -> bytes:
        """
        Serialize to a UTF-8 JSON line, ready to be written to the
        transport channel.

        The output is exactly one line — no embedded newlines are
        possible because Pydantic's ``model_dump_json`` escapes them.
        The trailing ``\\n`` is the line terminator.
        """
        return (self.model_dump_json() + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Request payloads (supervisor → worker)
# ---------------------------------------------------------------------------


class LoadAdapterPayload(BaseModel):
    """
    Payload for ``LOAD_ADAPTER``.

    The worker uses these fields to construct the adapter instance:

        * ``entry_point`` — ``module.path:ClassName`` resolved via
          ``importlib``.
        * ``connection`` — the per-connection context (base_url,
          api_key, extra_headers, extra_params, timeout). This is the
          ONLY connection the adapter may read secrets from — see
          ``permissions.py``.
        * ``profile`` — the adapter's ``AdapterProfile`` dict, sent by
          the supervisor so the worker has a canonical copy even if
          the adapter class does not declare one.
        * ``permissions`` — the flat ``PermissionKind`` value set the
          manifest declared. The worker enforces these at every
          adapter call.
        * ``package_dir`` — absolute path to the extracted package
          root. The worker inserts it into ``sys.path`` before
          importing the adapter, so packages shipped WITHOUT a
          ``pyproject.toml`` (whose dependencies were installed via
          ``pip install -r requirements.txt`` — which does NOT put
          the adapter source on ``sys.path``) can still be imported.
          Empty string means "the adapter module is already
          importable via the venv's site-packages" — the editable-
          install case where ``pip install -e .`` was used.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider_id: str
    entry_point: str
    connection: Dict[str, Any]
    profile: Dict[str, Any]
    permissions: List[str] = Field(default_factory=list)
    package_dir: str = ""


class GeneratePayload(BaseModel):
    """
    Payload for ``GENERATE``.

    Carries the ``LLMRequest`` in its serialized form. The worker
    rehydrates it into a real ``LLMRequest`` instance before calling
    the adapter — the adapter never sees the raw dict.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    request: Dict[str, Any]


class StreamStartPayload(BaseModel):
    """
    Payload for ``STREAM_START``.

    Identical shape to ``GeneratePayload``. The worker distinguishes
    the two by the message type, not by the payload contents.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    request: Dict[str, Any]


class StreamCancelPayload(BaseModel):
    """
    Payload for ``STREAM_CANCEL``.

    Correlates the cancellation to an active stream via ``stream_id``,
    which was assigned by the worker in its ``stream_started`` reply.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stream_id: str


class DiscoverModelsPayload(BaseModel):
    """
    Payload for ``DISCOVER_MODELS``.

    Carries the connection context. The worker uses it to construct a
    fresh adapter instance for the discovery call — the adapter may
    cache HTTP clients per connection, and reusing them across
    discovery and generation avoids repeated auth round-trips.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    connection: Dict[str, Any]


class ValidateConnectionPayload(BaseModel):
    """
    Payload for ``VALIDATE_CONNECTION``.

    Same shape as ``DiscoverModelsPayload`` — both are connection-only
    probes that do not touch the generation pipeline.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    connection: Dict[str, Any]


class GetModelMetadataPayload(BaseModel):
    """
    Payload for ``GET_MODEL_METADATA``.

    Carries the model ID only. The worker uses the cached adapter
    instance (from ``LOAD_ADAPTER``) to perform the query.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model_id: str


class ShutdownPayload(BaseModel):
    """
    Payload for ``SHUTDOWN``.

    Empty on purpose. The reason field exists so future versions can
    include a shutdown reason (e.g. ``"reload"`` vs ``"exit"``)
    without breaking the message type.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    reason: str = ""


# ---------------------------------------------------------------------------
# Reply payloads (worker → supervisor)
# ---------------------------------------------------------------------------


class ReadyPayload(BaseModel):
    """
    Payload for the initial ``READY`` message.

    Sent exactly once, at worker startup, BEFORE any request is
    accepted. The supervisor uses it to:

        * Confirm the worker can start (venv is functional).
        * Detect Python version incompatibility early.
        * Confirm protocol version compatibility.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol_version: int
    python_version: str
    worker_pid: int


class AdapterLoadedPayload(BaseModel):
    """
    Payload for ``ADAPTER_LOADED``.

    Returns the adapter's effective ``AdapterProfile`` so the
    supervisor can register the provider with its declared
    capabilities without re-importing the adapter in the main process
    (which would defeat the isolation).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    profile: Dict[str, Any]


class GenerateResultPayload(BaseModel):
    """
    Payload for ``GENERATE_RESULT``.

    Carries the serialized ``LLMResponse``. Every field an in-process
    adapter would populate is present — this is what makes the IPC
    boundary invisible to the caller.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    response: Dict[str, Any]


class StreamStartedPayload(BaseModel):
    """
    Payload for ``STREAM_STARTED``.

    Assigned by the worker so the supervisor can correlate
    ``stream_event`` messages and issue ``stream_cancel`` against
    the correct stream.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stream_id: str


class StreamEventPayload(BaseModel):
    """
    Payload for ``STREAM_EVENT``.

    Carries a single ``LLMStreamEvent`` dict. The supervisor
    rehydrates it and yields it to its own caller as if the adapter
    had produced it in-process.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stream_id: str
    event: Dict[str, Any]


class StreamEndPayload(BaseModel):
    """
    Payload for ``STREAM_END``.

    Signals the end of a stream. Exactly one of ``error`` or
    ``cancelled=True`` may be set; both ``None``/``False`` means
    the stream completed normally.

    ``usage`` is the final token usage from the stream — included
    here so the supervisor does not have to hunt for the last
    ``USAGE_UPDATE`` event when reconciling.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stream_id: str
    cancelled: bool = False
    error: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None


class ModelsResultPayload(BaseModel):
    """
    Payload for ``MODELS_RESULT``.

    Carries the raw model list the adapter produced. The supervisor
    forwards it verbatim — normalization into the metadata vault
    happens in the caller (``config_rtr.py``), not here.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    models: List[Dict[str, Any]] = Field(default_factory=list)


class ConnectionResultPayload(BaseModel):
    """
    Payload for ``CONNECTION_RESULT``.

    Same shape as the in-process adapter's ``validate_connection``
    return value: ``{ok, message, latency_ms}``, plus optional
    ``code`` and ``status_code`` when the failure was structured.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ok: bool
    message: str = ""
    latency_ms: float = 0.0
    code: Optional[str] = None
    status_code: Optional[int] = None


class MetadataResultPayload(BaseModel):
    """
    Payload for ``METADATA_RESULT``.

    ``metadata`` is ``None`` when the provider has no per-model
    metadata endpoint (which is the common case — only Ollama,
    LM Studio, and Google expose one).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    metadata: Optional[Dict[str, Any]] = None


class ErrorPayload(BaseModel):
    """
    Payload for ``ERROR``.

    Carries the canonical ``ProviderError.to_dict()`` shape, plus an
    optional ``code`` (protocol-level error) and ``details`` for
    context. The supervisor rehydrates this into the appropriate
    exception class.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    code: str
    message: str
    retryable: bool = False
    status_code: Optional[int] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class LogPayload(BaseModel):
    """
    Payload for ``LOG``.

    Unsolicited. The worker emits one per log record it produces.
    The supervisor forwards these to the main process's log handlers
    so worker diagnostics appear in the same log file as the rest of
    the backend.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    level: str
    message: str
    logger_name: str = ""


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def make_request(
    msg_type: MessageType,
    payload: BaseModel,
    *,
    request_id: Optional[str] = None,
) -> Envelope:
    """
    Build a request envelope for a supervisor → worker message.

    Generates a fresh request ID when ``request_id`` is omitted.
    Callers that need idempotency across retries pass an explicit id.
    """
    return Envelope(
        v=PROTOCOL_VERSION,
        id=request_id or new_request_id(),
        type=msg_type,
        payload=payload.model_dump(),
    )


def make_reply(
    msg_type: MessageType,
    payload: BaseModel,
    *,
    request_id: Optional[str],
) -> Envelope:
    """
    Build a reply envelope for a worker → supervisor message.

    ``request_id`` is echoed from the originating request. ``None``
    is allowed for unsolicited messages (``ready``, ``log``).
    """
    return Envelope(
        v=PROTOCOL_VERSION,
        id=request_id,
        type=msg_type,
        payload=payload.model_dump(),
    )


__all__ = [
    # Constants
    "PROTOCOL_VERSION",
    "MAX_MESSAGE_BYTES",
    "WORKER_STARTUP_TIMEOUT_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "SHUTDOWN_TIMEOUT_SECONDS",
    "SIGTERM_GRACE_SECONDS",
    # Enums
    "MessageType",
    "ProtocolErrorCode",
    # Envelope
    "Envelope",
    "new_request_id",
    # Request payloads
    "LoadAdapterPayload",
    "GeneratePayload",
    "StreamStartPayload",
    "StreamCancelPayload",
    "DiscoverModelsPayload",
    "ValidateConnectionPayload",
    "GetModelMetadataPayload",
    "ShutdownPayload",
    # Reply payloads
    "ReadyPayload",
    "AdapterLoadedPayload",
    "GenerateResultPayload",
    "StreamStartedPayload",
    "StreamEventPayload",
    "StreamEndPayload",
    "ModelsResultPayload",
    "ConnectionResultPayload",
    "MetadataResultPayload",
    "ErrorPayload",
    "LogPayload",
    # Constructors
    "make_request",
    "make_reply",
]
