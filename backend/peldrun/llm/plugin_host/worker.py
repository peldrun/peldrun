# backend/peldrun/llm/plugin_host/worker.py
"""
Plugin Host worker subprocess.

Entry point for an externally-installed Provider Package adapter.

The worker is spawned by the supervisor (main process) with the
package's isolated venv interpreter. It communicates over stdin/stdout
via the JSON Lines protocol defined in ``protocol.py``. It NEVER
touches the main process's memory, registry, metadata vault, or
filesystem beyond its own package directory.

Startup sequence
----------------

The order of operations below is load-bearing — changing it can
introduce protocol corruption or permission bypasses:

    1. **Capture stdout for protocol use.**
       ``capture_stdout_for_protocol()`` redirects ``sys.stdout`` to
       ``sys.stderr`` and returns the ORIGINAL binary stdout. Everything
       the adapter prints (directly or via a logging handler) now goes
       to stderr, which the supervisor sees as its own stderr. The
       protocol channel is exclusively owned by the transport.

    2. **Import the worker's dependencies.**
       After step 1, any import can safely print/log without corrupting
       the channel.

    3. **Create the transport and announce ``ready``.**
       The supervisor waits for this message before sending any
       request. It carries the worker's protocol version and PID so
       the supervisor can detect incompatibilities early.

    4. **Install the httpx allowlist guard.**
       Done inside ``handle_load_adapter`` — MUST happen BEFORE the
       adapter module is imported, so that any ``httpx.AsyncClient``
       the adapter (or its SDK) creates at import time is wrapped.

    5. **Import and instantiate the adapter.**
       The adapter's module path is resolved via ``importlib``; the
       package directory (from ``LoadAdapterPayload.package_dir``) is
       inserted into ``sys.path`` first so that adapters shipped
       WITHOUT ``pyproject.toml`` are still importable.

    6. **Serve requests until ``shutdown``.**
       Generate, stream, discover, validate, metadata. Each handler
       enforces the corresponding permission via the runtime enforcer
       BEFORE touching the adapter.

Message handlers
----------------

Every handler follows the same three-part contract:

    * **Validate** the payload against its Pydantic model.
    * **Enforce** the required permissions (if any).
    * **Reply** with the correct message type — or with an ``error``
      envelope if anything fails.

Streams are the only asynchronous path: ``handle_stream_start``
returns immediately with a ``stream_id``, and a background task
emits ``stream_event`` messages until the adapter's async generator
exhausts (or the supervisor sends ``stream_cancel``). The task sends
exactly ONE ``stream_end`` message in a ``finally`` block — guarded
by ``asyncio.shield`` so a re-delivered cancellation cannot suppress
it.

Exception handling
------------------

The handler dispatch loop catches every exception from a handler and
converts it to an ``ErrorPayload``. Conversion preserves the
``ProviderError`` taxonomy — a package's adapter raising
``ProviderRateLimitError`` produces the same error code the caller
would see if the adapter ran in-process. This is what makes the IPC
boundary invisible to consumers.

A transport error (EOF, write failure, protocol violation) exits
the loop cleanly. The supervisor treats a worker exit as "the worker
crashed or shut down"; it does not retry automatically.

Cancellation
------------

``asyncio.CancelledError`` propagates unwrapped. The stream task
sends its ``stream_end`` message from a ``finally`` block, so the
supervisor always receives a terminal event — even when the
cancellation arrives mid-emit.

See also
--------

    * ``backend/peldrun/llm/plugin_host/protocol.py`` — message schema
    * ``backend/peldrun/llm/plugin_host/transport.py`` — I/O
    * ``backend/peldrun/llm/plugin_host/permissions.py`` — enforcer
    * ``backend/peldrun/llm/plugin_host/supervisor.py`` — the
      counterpart that spawns and manages this worker
"""

from __future__ import annotations

# =====================================================================
# STEP 1 — Capture stdout BEFORE any other import.
#
# `sys.stdout` is the TEXT stdout, which every `print()` and every
# logging handler writes to. Redirecting it to `sys.stderr` before
# any import guarantees that an adapter (or its SDK) that prints at
# import time does not corrupt the JSON-Lines protocol channel.
#
# The ORIGINAL binary stdout (which the OS has wired to the parent's
# pipe) is captured in `_PROTOCOL_STDOUT` and used EXCLUSIVELY by the
# `StdioTransport` instance created in `worker_main()`.
# =====================================================================
import sys as _sys_bootstrap  # noqa: E402 — must precede capture

from peldrun.llm.plugin_host.transport import (  # noqa: E402
    capture_stdout_for_protocol,
)

_PROTOCOL_STDOUT = capture_stdout_for_protocol()


# =====================================================================
# STEP 2 — Normal imports. Safe now that stdout is redirected.
# =====================================================================

import asyncio
import importlib
import logging
import os
import traceback
import uuid
from typing import Any, Dict, List, Optional

from peldrun.llm.errors import ProviderError, is_cancellation
from peldrun.llm.plugin_host.permissions import (
    PERM_LLM_GENERATE,
    PERM_LLM_STREAM,
    PERM_MODELS_DISCOVER,
    PERM_NETWORK_CONNECT,
    PluginPermissionDeniedError,
    RuntimePermissionEnforcer,
    URLNotAllowedError,
    install_httpx_guard,
)
from peldrun.llm.plugin_host.protocol import (
    PROTOCOL_VERSION,
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
    make_reply,
)
from peldrun.llm.plugin_host.transport import (
    StdioTransport,
    Transport,
    TransportClosedError,
    TransportError,
)


logger = logging.getLogger("peldrun.llm.plugin_host.worker")


# ---------------------------------------------------------------------------
# JSON safety
# ---------------------------------------------------------------------------


def _json_safe(obj: Any, _depth: int = 0) -> Any:
    """
    Recursively convert ``obj`` into a JSON-serializable structure.

    Adapters are third-party code — we cannot assume that every field
    they place on an ``LLMResponse`` or ``LLMStreamEvent`` is
    JSON-native. The most common failure mode is
    ``provider_metadata`` containing a raw httpx response object or a
    Pydantic model that leaks across the wire.

    This function is defensive: it handles primitives, dicts, lists,
    Pydantic v2 models (via ``model_dump()``), dataclasses (via
    ``__dict__``), and falls back to ``str()`` for anything else.
    Depth is bounded at 20 to prevent pathological cycles from
    blowing the stack.

    Never raises. Worst case returns a ``str`` representation.
    """
    if _depth > 20:
        try:
            return str(obj)[:200]
        except Exception:
            return "<max-depth>"

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_json_safe(v, _depth + 1) for v in obj]

    # Pydantic v2 models.
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            return _json_safe(obj.model_dump(), _depth + 1)
        except Exception:
            pass

    # Pydantic v1 models (rare in this codebase, but defensively).
    if hasattr(obj, "dict") and callable(obj.dict):
        try:
            return _json_safe(obj.dict(), _depth + 1)
        except Exception:
            pass

    # Enum values.
    if hasattr(obj, "value") and isinstance(getattr(obj, "value"), (str, int)):
        return obj.value

    try:
        return str(obj)
    except Exception:
        try:
            return repr(obj)
        except Exception:
            return "<unserializable>"


def _serialize_llm_response(response: Any) -> Dict[str, Any]:
    """
    Convert an ``LLMResponse`` dataclass to a JSON-safe dict.

    ``LLMResponse.raw`` is deliberately EXCLUDED — it holds the raw
    SDK response, which is almost never JSON-serializable and is
    only useful for in-process debugging. Diagnostics payloads that
    need it must request it explicitly via a dedicated field.

    Every other field is passed through ``_json_safe()`` so that a
    misbehaving adapter that stuffs a non-JSON value into
    ``tool_calls[i].arguments`` cannot crash the reply.
    """
    tool_calls_serialized: List[Dict[str, Any]] = []
    for tc in getattr(response, "tool_calls", None) or []:
        tool_calls_serialized.append(
            {
                "id": str(getattr(tc, "id", "")),
                "name": str(getattr(tc, "name", "")),
                "arguments": _json_safe(getattr(tc, "arguments", {})),
                "type": str(getattr(tc, "type", "function")),
            }
        )

    usage = getattr(response, "usage", None)
    usage_serialized = None
    if usage is not None:
        try:
            if hasattr(usage, "model_dump"):
                usage_serialized = _json_safe(usage.model_dump())
            else:
                usage_serialized = _json_safe(usage)
        except Exception:
            usage_serialized = None

    return {
        "content": getattr(response, "content", None),
        "reasoning": getattr(response, "reasoning", None),
        "reasoning_content": getattr(response, "reasoning_content", None),
        "thought": getattr(response, "thought", None),
        "tool_calls": tool_calls_serialized,
        "finish_reason": getattr(response, "finish_reason", None),
        "usage": usage_serialized,
        "model": getattr(response, "model", None),
        "latency_ms": getattr(response, "latency_ms", None),
        "ttft_ms": getattr(response, "ttft_ms", None),
    }


# ---------------------------------------------------------------------------
# Error conversion
# ---------------------------------------------------------------------------


def _exception_to_error_payload(exc: BaseException) -> ErrorPayload:
    """
    Convert any exception into a wire-safe ``ErrorPayload``.

    Precedence
    ----------
    1. ``asyncio.CancelledError`` — should never reach this function
       (callers handle cancellation separately) but if it does, we
       surface it as ``STREAM_CANCELLED`` rather than crashing.
    2. ``ProviderError`` — preserved verbatim: code, message,
       retryable, status_code, details. This is what makes the IPC
       boundary invisible to callers: a rate-limit error raised by
       the adapter becomes a rate-limit error at the caller.
    3. ``PluginPermissionDeniedError`` — a runtime permission
       violation. ``retryable=False`` by construction.
    4. Any other exception — surfaced as ``ADAPTER_LOAD_FAILED`` with
       a truncated traceback in ``details``.
    """
    if is_cancellation(exc):
        return ErrorPayload(
            code=ProtocolErrorCode.STREAM_CANCELLED.value,
            message="Stream cancelled.",
            retryable=False,
        )

    if isinstance(exc, ProviderError):
        return ErrorPayload(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            status_code=exc.status_code,
            details=_json_safe(exc.details),
        )

    if isinstance(exc, PluginPermissionDeniedError):
        # Subclass of ProviderError, but we double-check in case a
        # future refactor decouples them.
        return ErrorPayload(
            code=exc.code,
            message=exc.message,
            retryable=False,
            details=_json_safe(exc.details),
        )

    # Anything else — keep a truncated traceback for diagnostics but
    # never include the full frames (they can be huge).
    try:
        tb = traceback.format_exc()[-4000:]
    except Exception:
        tb = "<traceback unavailable>"

    return ErrorPayload(
        code=ProtocolErrorCode.ADAPTER_LOAD_FAILED.value,
        message=f"{type(exc).__name__}: {exc}",
        retryable=False,
        details={
            "exception_type": type(exc).__name__,
            "traceback_tail": tb,
        },
    )


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------


class WorkerState:
    """
    All mutable state for a running worker.

    A single instance is created in ``worker_main()`` and passed to
    every handler. Handlers mutate it freely — the main loop runs one
    handler at a time, except for stream tasks that run concurrently.

    Concurrency notes
    -----------------
    * ``streams`` is accessed from both the main loop (register at
      start, pop on completion) and from ``handle_stream_cancel``.
      All mutations happen on the event loop's thread, so no lock is
      required — but the invariant is that a stream task MUST pop
      itself from ``streams`` in its ``finally`` block.

    * ``adapter`` is set once in ``handle_load_adapter`` and read by
      every other handler. The main loop is single-threaded, so
      there is no race on assignment vs. read.

    * ``shutting_down`` is only mutated by ``handle_shutdown`` and
      checked by the main loop. No locking needed.
    """

    __slots__ = (
        "adapter",
        "adapter_profile",
        "enforcer",
        "streams",
        "shutting_down",
    )

    def __init__(self) -> None:
        self.adapter: Any = None
        self.adapter_profile: Optional[Any] = None
        self.enforcer: Optional[RuntimePermissionEnforcer] = None
        self.streams: Dict[str, asyncio.Task] = {}
        self.shutting_down: bool = False


# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------


async def _send_reply(
    transport: Transport,
    request_id: Optional[str],
    msg_type: MessageType,
    payload: Any,
) -> None:
    """
    Send a reply envelope. Suppresses transport errors so a handler
    can call this without guarding every line.

    A failed write means the supervisor is gone; the main loop will
    detect the closed transport on the next read and exit.
    """
    try:
        await transport.write_envelope(
            make_reply(msg_type, payload, request_id=request_id)
        )
    except TransportError as exc:
        logger.warning(
            "Failed to send %s reply (request_id=%r): %s",
            msg_type.value,
            request_id,
            exc,
        )


async def _send_error(
    transport: Transport,
    request_id: Optional[str],
    error_payload: ErrorPayload,
) -> None:
    """Send an error reply. Convenience wrapper around ``_send_reply``."""
    await _send_reply(transport, request_id, MessageType.ERROR, error_payload)


async def _send_error_from_exception(
    transport: Transport,
    request_id: Optional[str],
    exc: BaseException,
) -> None:
    """Convert and send an error reply in one call."""
    await _send_error(transport, request_id, _exception_to_error_payload(exc))


# ---------------------------------------------------------------------------
# Handler — load adapter
# ---------------------------------------------------------------------------


async def handle_load_adapter(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Import and instantiate the adapter class.

    Critical ordering
    -----------------
    1. Build the ``RuntimePermissionEnforcer`` (validates the
       permission set, parses the base_url).
    2. **Install the httpx allowlist guard BEFORE importing the
       adapter.** This guarantees that any ``httpx.AsyncClient``
       created by the adapter or its SDK — including clients created
       at module import time — is wrapped.
    3. Insert ``package_dir`` into ``sys.path`` (if provided) so
       adapters shipped without ``pyproject.toml`` are importable.
    4. Import the module, look up the class, instantiate it.
    5. Reply with the adapter's ``AdapterProfile`` (canonical form)
       so the supervisor can register the provider.

    Re-entrancy
    -----------
    Calling this a second time REPLACES the current adapter. The
    previous adapter is closed before the new one is loaded. This
    allows connection reconfiguration without restarting the worker.
    """
    payload = LoadAdapterPayload.model_validate(envelope.payload)

    # --------------------------------------------------------------
    # 1. Build enforcer — may raise PluginPermissionDeniedError.
    # --------------------------------------------------------------
    enforcer = RuntimePermissionEnforcer(
        provider_id=payload.provider_id,
        connection_id=payload.connection.get("connection_id", "<unknown>"),
        declared_permissions=payload.permissions or [],
        connection_base_url=payload.connection.get("base_url"),
        connection_api_key=payload.connection.get("api_key"),
        extra_headers=payload.connection.get("extra_headers") or {},
    )

    # --------------------------------------------------------------
    # 2. Install httpx guard BEFORE importing the adapter.
    # --------------------------------------------------------------
    install_httpx_guard(enforcer)

    # --------------------------------------------------------------
    # 3. Import the adapter module.
    #
    # The package_dir (if provided) is prepended to sys.path so the
    # adapter source is importable. This matters for packages shipped
    # WITHOUT a pyproject.toml — the dependency_installer in that
    # case only ran `pip install -r requirements.txt`, which puts the
    # adapter's DEPENDENCIES on sys.path but not the adapter source
    # itself.
    # --------------------------------------------------------------
    package_dir = payload.connection.get("extra_params", {}).get(
        "_peldrun_package_dir"
    )
    sys_path_inserted = False
    if (
        isinstance(package_dir, str)
        and package_dir
        and package_dir not in _sys_bootstrap.path
    ):
        _sys_bootstrap.path.insert(0, package_dir)
        sys_path_inserted = True

    try:
        # Close any previously-loaded adapter before replacing it.
        if state.adapter is not None:
            try:
                await state.adapter.close()
            except Exception as exc:
                logger.warning(
                    "Previous adapter close failed during reload: %s", exc
                )
            state.adapter = None
            state.adapter_profile = None

        # Split "module.path:ClassName".
        if ":" not in payload.entry_point:
            raise ValueError(
                f"Invalid entry_point format: {payload.entry_point!r}. "
                f"Expected 'module.path:ClassName'."
            )
        module_path, class_name = payload.entry_point.split(":", 1)
        module_path = module_path.strip()
        class_name = class_name.strip()

        if not module_path or not class_name:
            raise ValueError(
                f"entry_point has empty module or class segment: "
                f"{payload.entry_point!r}"
            )

        module = importlib.import_module(module_path)
        adapter_class = getattr(module, class_name, None)
        if adapter_class is None:
            raise ImportError(
                f"Module '{module_path}' has no attribute "
                f"'{class_name}'. Check the entry_point in the "
                f"manifest."
            )

        # Build the ProviderContext expected by V1 adapters.
        from peldrun.llm.contracts import (
            AdapterProfile,
            ConnectionContext,
        )
        from peldrun.llm.providers.factory import ProviderContext

        connection_ctx = ConnectionContext(
            **{
                **payload.connection,
                # Preserve anything the supervisor passed verbatim
                # but drop keys that don't belong to ConnectionContext.
            }
        )
        profile = AdapterProfile(**payload.profile)
        context = ProviderContext.build(connection_ctx, profile)

        # Instantiate. V1 adapters accept context=; legacy adapters
        # are wrapped by the supervisor before reaching the worker,
        # so we only support the V1 signature here.
        try:
            adapter_instance = adapter_class(context=context)
        except TypeError as first_err:
            # Try the explicit keyword form (some adapters prefer it).
            try:
                adapter_instance = adapter_class(
                    connection=connection_ctx, profile=profile
                )
            except TypeError:
                raise TypeError(
                    f"Adapter '{payload.provider_id}' does not accept "
                    f"either `context=` or "
                    f"`connection=, profile=` constructor signatures. "
                    f"Original error: {first_err}"
                ) from first_err

        state.adapter = adapter_instance
        state.adapter_profile = profile
        state.enforcer = enforcer

    finally:
        # Always remove the package_dir we added — otherwise a
        # subsequent LOAD_ADAPTER for a different package would see
        # stale paths and might import the wrong module.
        if sys_path_inserted:
            try:
                _sys_bootstrap.path.remove(package_dir)
            except ValueError:
                pass

    # --------------------------------------------------------------
    # 4. Reply with the canonical profile.
    # --------------------------------------------------------------
    await _send_reply(
        transport,
        envelope.id,
        MessageType.ADAPTER_LOADED,
        AdapterLoadedPayload(
            profile=_json_safe(state.adapter_profile.model_dump())
        ),
    )


# ---------------------------------------------------------------------------
# Handler — generate (non-streaming)
# ---------------------------------------------------------------------------


async def handle_generate(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Execute a non-streaming completion.

    Requires ``llm.generate`` permission. If ``network.connect`` is
    also required (which it is for every remote provider), the
    adapter's first network call will be blocked by the allowlist if
    the manifest did not declare it — so an explicit network check
    here is redundant, but harmless. We skip it for brevity; the
    allowlist enforcement is enough.
    """
    if state.adapter is None:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.ADAPTER_NOT_LOADED.value,
                message=(
                    "Adapter not loaded. Send a LOAD_ADAPTER request "
                    "before GENERATE."
                ),
            ),
        )
        return

    payload = GeneratePayload.model_validate(envelope.payload)

    # Enforce permission BEFORE touching the adapter.
    try:
        assert state.enforcer is not None
        state.enforcer.require(PERM_LLM_GENERATE)
    except PluginPermissionDeniedError as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    # Reconstruct the LLMRequest.
    from peldrun.llm.request import LLMRequest

    try:
        llm_request = LLMRequest.model_validate(payload.request)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid LLMRequest payload: {exc}",
                details={"field": "request"},
            ),
        )
        return

    # Execute.
    try:
        response = await state.adapter.generate(llm_request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    await _send_reply(
        transport,
        envelope.id,
        MessageType.GENERATE_RESULT,
        GenerateResultPayload(response=_serialize_llm_response(response)),
    )


# ---------------------------------------------------------------------------
# Handler — stream start
# ---------------------------------------------------------------------------


async def handle_stream_start(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Begin a streaming completion.

    Returns immediately with ``stream_started`` (carrying a fresh
    ``stream_id``), then a background task iterates the adapter's
    async generator and emits ``stream_event`` messages, ending with
    exactly one ``stream_end``.

    The main message loop continues reading while the stream task
    runs, so a subsequent ``stream_cancel`` can interrupt it.
    """
    if state.adapter is None:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.ADAPTER_NOT_LOADED.value,
                message=(
                    "Adapter not loaded. Send a LOAD_ADAPTER request "
                    "before STREAM_START."
                ),
            ),
        )
        return

    payload = StreamStartPayload.model_validate(envelope.payload)

    try:
        assert state.enforcer is not None
        state.enforcer.require(PERM_LLM_STREAM)
    except PluginPermissionDeniedError as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    from peldrun.llm.request import LLMRequest

    try:
        llm_request = LLMRequest.model_validate(payload.request)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid LLMRequest payload: {exc}",
                details={"field": "request"},
            ),
        )
        return

    stream_id = f"stream_{uuid.uuid4().hex[:12]}"

    # --------------------------------------------------------------
    # CRITICAL ORDERING
    # --------------------------------------------------------------
    # 1. Reply with `stream_started` FIRST so the supervisor has the
    #    stream_id before any events arrive.
    # 2. THEN start the task. If we started the task first and it
    #    managed to emit events before the supervisor processed
    #    `stream_started`, those events would arrive with no way to
    #    correlate them (the supervisor's stream registry would not
    #    yet have the stream_id).
    # --------------------------------------------------------------
    await _send_reply(
        transport,
        envelope.id,
        MessageType.STREAM_STARTED,
        StreamStartedPayload(stream_id=stream_id),
    )

    task = asyncio.create_task(
        _run_stream_task(
            state=state,
            transport=transport,
            stream_id=stream_id,
            request=llm_request,
            request_id=envelope.id,
        )
    )
    state.streams[stream_id] = task
    task.add_done_callback(_on_stream_task_done)


def _on_stream_task_done(task: asyncio.Task) -> None:
    """
    Log unhandled exceptions from a stream task.

    The task's ``finally`` block always sends ``stream_end``, so the
    supervisor knows the stream is over regardless. But without this
    callback, an unexpected exception inside the task would be
    silently dropped by asyncio's default "task exception was never
    retrieved" behavior — confusing when debugging.
    """
    try:
        task.result()
    except asyncio.CancelledError:
        pass  # Expected on cancel; the STREAM_END event was already sent.
    except Exception as exc:
        logger.warning(
            "Stream task exited with unexpected exception: %s", exc
        )


async def _run_stream_task(
    *,
    state: WorkerState,
    transport: Transport,
    stream_id: str,
    request: Any,
    request_id: Optional[str],
) -> None:
    """
    Iterate the adapter's stream generator and forward every event.

    Emits exactly one ``stream_end`` message in a ``finally`` block.
    The send is wrapped in ``asyncio.shield`` so that a second
    cancellation delivered mid-cleanup cannot suppress the terminal
    event — the supervisor always receives it.
    """
    captured_usage: Optional[Dict[str, Any]] = None
    cancelled = False
    error_payload: Optional[Dict[str, Any]] = None

    try:
        async for event in state.adapter.stream(request):
            # Serialize the event. ``event`` is an ``LLMStreamEvent``
            # (Pydantic model) — we go through ``_json_safe`` on the
            # dumped dict to guard against adapter-injected values.
            try:
                event_dict = event.model_dump()
            except Exception:
                event_dict = {"type": "unknown", "provider_metadata": {}}

            # Cache the last usage we saw — the supervisor uses
            # ``stream_end.usage`` to reconcile accounting even if it
            # missed a usage event.
            if event_dict.get("usage"):
                captured_usage = _json_safe(event_dict["usage"])

            await transport.write_envelope(
                make_reply(
                    MessageType.STREAM_EVENT,
                    StreamEventPayload(
                        stream_id=stream_id,
                        event=_json_safe(event_dict),
                    ),
                    request_id=request_id,
                )
            )

    except asyncio.CancelledError:
        cancelled = True
        raise

    except Exception as exc:
        error_payload = _json_safe(
            _exception_to_error_payload(exc).model_dump()
        )

    finally:
        # Remove from the active set BEFORE sending the terminal
        # message, so a concurrent `stream_cancel` cannot find the
        # task and try to cancel it again.
        state.streams.pop(stream_id, None)

        # Send the terminal event. Shielded so re-delivered
        # cancellations cannot abort the send.
        try:
            await asyncio.shield(
                transport.write_envelope(
                    make_reply(
                        MessageType.STREAM_END,
                        StreamEndPayload(
                            stream_id=stream_id,
                            cancelled=cancelled,
                            error=error_payload,
                            usage=captured_usage,
                        ),
                        request_id=request_id,
                    )
                )
            )
        except Exception as exc:
            logger.warning(
                "Failed to send STREAM_END for stream %s: %s",
                stream_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Handler — stream cancel
# ---------------------------------------------------------------------------


async def handle_stream_cancel(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Cancel an active stream.

    No reply is sent — the ``stream_end`` message (which the stream
    task always emits from its ``finally`` block) is the terminal
    signal for this stream. Sending an additional ack would be
    redundant and would require a new message type in the protocol.

    If the stream_id is unknown (already ended, or never existed),
    the cancel is a no-op. This is intentional: the supervisor may
    cancel a stream that just finished, and that is not an error.
    """
    try:
        payload = StreamCancelPayload.model_validate(envelope.payload)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid StreamCancelPayload: {exc}",
            ),
        )
        return

    task = state.streams.get(payload.stream_id)
    if task is None:
        logger.debug(
            "stream_cancel for unknown stream %r — already ended.",
            payload.stream_id,
        )
        return

    if not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Handler — discover models
# ---------------------------------------------------------------------------


async def handle_discover_models(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """Discover available models. Requires ``models.discover``."""
    if state.adapter is None:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.ADAPTER_NOT_LOADED.value,
                message="Adapter not loaded.",
            ),
        )
        return

    try:
        payload = DiscoverModelsPayload.model_validate(envelope.payload)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid DiscoverModelsPayload: {exc}",
            ),
        )
        return

    try:
        assert state.enforcer is not None
        state.enforcer.require(PERM_MODELS_DISCOVER)
    except PluginPermissionDeniedError as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    from peldrun.llm.contracts import ConnectionContext

    try:
        connection = ConnectionContext(**payload.connection)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid connection: {exc}",
            ),
        )
        return

    try:
        models = await state.adapter.discover_models(connection)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    await _send_reply(
        transport,
        envelope.id,
        MessageType.MODELS_RESULT,
        ModelsResultPayload(models=_json_safe(models or [])),
    )


# ---------------------------------------------------------------------------
# Handler — validate connection
# ---------------------------------------------------------------------------


async def handle_validate_connection(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Probe the connection without sending a generation request.

    No specific permission is required — validation is a read-only
    reachability check that any loaded adapter may perform. The
    underlying HTTP client is still subject to the network allowlist,
    so a denied URL raises ``URLNotAllowedError`` which becomes a
    normal error reply.
    """
    if state.adapter is None:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.ADAPTER_NOT_LOADED.value,
                message="Adapter not loaded.",
            ),
        )
        return

    try:
        payload = ValidateConnectionPayload.model_validate(envelope.payload)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid ValidateConnectionPayload: {exc}",
            ),
        )
        return

    from peldrun.llm.contracts import ConnectionContext

    try:
        connection = ConnectionContext(**payload.connection)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid connection: {exc}",
            ),
        )
        return

    try:
        result = await state.adapter.validate_connection(connection)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    # Normalize the result — adapters are supposed to return
    # {ok, message, latency_ms} but we fill in defaults defensively.
    result_dict = result if isinstance(result, dict) else {}
    await _send_reply(
        transport,
        envelope.id,
        MessageType.CONNECTION_RESULT,
        ConnectionResultPayload(
            ok=bool(result_dict.get("ok", False)),
            message=str(result_dict.get("message", "")),
            latency_ms=float(result_dict.get("latency_ms", 0.0) or 0.0),
            code=(
                str(result_dict["code"])
                if result_dict.get("code") is not None
                else None
            ),
            status_code=(
                int(result_dict["status_code"])
                if isinstance(result_dict.get("status_code"), int)
                else None
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Handler — get model metadata
# ---------------------------------------------------------------------------


async def handle_get_model_metadata(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Fetch provider-specific metadata for a single model.

    No specific permission is required — this is a read-only metadata
    query, and the underlying HTTP call is subject to the network
    allowlist just like any other adapter method.
    """
    if state.adapter is None:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.ADAPTER_NOT_LOADED.value,
                message="Adapter not loaded.",
            ),
        )
        return

    try:
        payload = GetModelMetadataPayload.model_validate(envelope.payload)
    except Exception as exc:
        await _send_error(
            transport,
            envelope.id,
            ErrorPayload(
                code=ProtocolErrorCode.INVALID_PAYLOAD.value,
                message=f"Invalid GetModelMetadataPayload: {exc}",
            ),
        )
        return

    try:
        metadata = await state.adapter.get_model_metadata(payload.model_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _send_error_from_exception(transport, envelope.id, exc)
        return

    await _send_reply(
        transport,
        envelope.id,
        MessageType.METADATA_RESULT,
        MetadataResultPayload(
            metadata=_json_safe(metadata) if metadata is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Handler — shutdown
# ---------------------------------------------------------------------------


async def handle_shutdown(
    state: WorkerState,
    transport: Transport,
    envelope: Envelope,
) -> None:
    """
    Graceful shutdown.

    Order of operations:

        1. Mark the worker as shutting down — the main loop checks
           this flag and exits after the current handler completes.
        2. Cancel every active stream. Each stream task sends its
           ``stream_end`` from its ``finally`` block before returning.
        3. Close the adapter (releases HTTP clients and any other
           resources the adapter holds).
        4. Send ``shutdown_ack``.
        5. The main loop exits; the process terminates naturally.

    Streams that do not honour cancellation within
    ``SHUTDOWN_TIMEOUT_SECONDS`` are killed by the supervisor (which
    sends SIGTERM, then SIGKILL). The worker does not attempt to
    forcibly kill asyncio tasks itself — that is the supervisor's
    responsibility and would violate the transport's own guarantees.
    """
    try:
        ShutdownPayload.model_validate(envelope.payload)
    except Exception:
        # Payload is best-effort; an empty dict is fine.
        pass

    state.shutting_down = True

    # Cancel all active streams. Give them a short grace period to
    # emit their stream_end messages.
    if state.streams:
        tasks_to_wait: List[asyncio.Task] = []
        for task in list(state.streams.values()):
            if not task.done():
                task.cancel()
            tasks_to_wait.append(task)

        if tasks_to_wait:
            try:
                await asyncio.wait(
                    tasks_to_wait, timeout=2.0
                )
            except Exception as exc:
                logger.debug(
                    "Error waiting for stream tasks during shutdown: %s",
                    exc,
                )

    # Close the adapter.
    if state.adapter is not None:
        try:
            await state.adapter.close()
        except Exception as exc:
            logger.warning(
                "Adapter close raised during shutdown: %s", exc
            )
        state.adapter = None

    # Acknowledge.
    await _send_reply(
        transport,
        envelope.id,
        MessageType.SHUTDOWN_ACK,
        ShutdownPayload(),
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


#: Message type → handler function mapping. Handlers not listed here
#: receive an UNKNOWN_MESSAGE_TYPE error reply.
_HANDLERS: Dict[MessageType, Any] = {
    MessageType.LOAD_ADAPTER: handle_load_adapter,
    MessageType.GENERATE: handle_generate,
    MessageType.STREAM_START: handle_stream_start,
    MessageType.STREAM_CANCEL: handle_stream_cancel,
    MessageType.DISCOVER_MODELS: handle_discover_models,
    MessageType.VALIDATE_CONNECTION: handle_validate_connection,
    MessageType.GET_MODEL_METADATA: handle_get_model_metadata,
    MessageType.SHUTDOWN: handle_shutdown,
}


async def worker_main() -> None:
    """
    Worker entry point. Runs until the supervisor closes the channel
    or sends ``SHUTDOWN``.

    Order of operations on startup
    ------------------------------
        1. Create the ``StdioTransport`` with the captured binary
           stdout from Step 1.
        2. Send ``READY`` (protocol_version + python_version + pid).
        3. Loop reading envelopes, dispatching to handlers.

    Order of operations on exit
    ---------------------------
        * Break the loop on any of:
            - ``TransportClosedError``: supervisor closed stdin.
            - ``TransportError``: unrecoverable I/O failure.
            - ``state.shutting_down`` set by ``handle_shutdown``.
        * Cancel any stream tasks that are still running.
        * Close the transport (no-op on the worker side).

    Never raises. Every exception is logged and leads to a clean
    exit — the supervisor treats a worker exit as a failure and
    spawns a fresh one on the next request.
    """
    transport = StdioTransport(stdout_binary=_PROTOCOL_STDOUT)
    state = WorkerState()

    # ------------------------------------------------------------------
    # Announce READY.
    # ------------------------------------------------------------------
    python_version = (
        f"{_sys_bootstrap.version_info.major}."
        f"{_sys_bootstrap.version_info.minor}."
        f"{_sys_bootstrap.version_info.micro}"
    )
    await _send_reply(
        transport,
        request_id=None,
        msg_type=MessageType.READY,
        payload=ReadyPayload(
            protocol_version=PROTOCOL_VERSION,
            python_version=python_version,
            worker_pid=os.getpid(),
        ),
    )
    logger.info(
        "Worker ready (pid=%d, python=%s, protocol=%d).",
        os.getpid(),
        python_version,
        PROTOCOL_VERSION,
    )

    # ------------------------------------------------------------------
    # Main loop.
    # ------------------------------------------------------------------
    try:
        while not state.shutting_down:
            try:
                envelope = await transport.read_envelope()
            except TransportClosedError:
                logger.info(
                    "Supervisor closed the channel; worker exiting."
                )
                break
            except TransportError as exc:
                logger.error(
                    "Transport error: %s — worker exiting.", exc
                )
                break

            # Protocol version check.
            if envelope.v != PROTOCOL_VERSION:
                await _send_error(
                    transport,
                    envelope.id,
                    ErrorPayload(
                        code=ProtocolErrorCode.PROTOCOL_VERSION_MISMATCH.value,
                        message=(
                            f"Protocol version {envelope.v} is not "
                            f"supported; this worker speaks version "
                            f"{PROTOCOL_VERSION}."
                        ),
                        details={
                            "received": envelope.v,
                            "supported": PROTOCOL_VERSION,
                        },
                    ),
                )
                continue

            handler = _HANDLERS.get(envelope.type)
            if handler is None:
                await _send_error(
                    transport,
                    envelope.id,
                    ErrorPayload(
                        code=ProtocolErrorCode.UNKNOWN_MESSAGE_TYPE.value,
                        message=(
                            f"Unknown message type: "
                            f"{envelope.type.value if hasattr(envelope.type, 'value') else envelope.type}"
                        ),
                    ),
                )
                continue

            # Dispatch.
            try:
                await handler(state, transport, envelope)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Unhandled exception in handler %s",
                    envelope.type.value
                    if hasattr(envelope.type, "value")
                    else envelope.type,
                )
                await _send_error_from_exception(
                    transport, envelope.id, exc
                )

    finally:
        # Cancel any leftover streams.
        for task in list(state.streams.values()):
            if not task.done():
                task.cancel()
        state.streams.clear()

        # Close the transport (best-effort).
        try:
            await transport.close()
        except Exception:
            pass

        logger.info("Worker exiting cleanly.")


# ---------------------------------------------------------------------------
# Process entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Synchronous entry point for ``python -m peldrun.llm.plugin_host.worker``.

    This is what the supervisor spawns. The function is deliberately
    trivial: it wraps ``asyncio.run(worker_main())`` and translates
    an unexpected crash into a non-zero exit code so the supervisor
    can distinguish "worker crashed" from "worker exited on
    shutdown".

    Exit codes
    ----------
        0   — normal shutdown (received SHUTDOWN, or transport EOF).
        1   — unexpected crash during startup or the main loop.
        130 — interrupted by Ctrl+C (SIGINT). The supervisor treats
              this the same as exit code 1; it is preserved mainly
              for manual debugging.
    """
    try:
        asyncio.run(worker_main())
    except KeyboardInterrupt:
        # Ctrl+C — likely from a developer testing the worker
        # manually. Exit with the standard SIGINT code.
        print("Worker interrupted by SIGINT.", file=_sys_bootstrap.stderr)
        raise SystemExit(130)
    except Exception as exc:
        # Any uncaught exception from worker_main. Print to stderr
        # (which the supervisor may capture) and exit non-zero.
        print(
            f"Worker crashed with unhandled exception: "
            f"{type(exc).__name__}: {exc}",
            file=_sys_bootstrap.stderr,
        )
        traceback.print_exc(file=_sys_bootstrap.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()


__all__ = [
    "WorkerState",
    "worker_main",
    "main",
    # Handlers (exposed for unit tests that drive them directly)
    "handle_load_adapter",
    "handle_generate",
    "handle_stream_start",
    "handle_stream_cancel",
    "handle_discover_models",
    "handle_validate_connection",
    "handle_get_model_metadata",
    "handle_shutdown",
]
