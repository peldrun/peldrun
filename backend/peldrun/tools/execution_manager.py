# Path: backend/peldrun/tools/execution_manager.py
"""
PELDRUN Core Tool Execution Manager.

The SINGLE gateway through which every tool invocation runs. It
converts the heterogeneous raw results that different tool
implementations produce (``ToolResult``, bare strings, dicts,
``None``) into the canonical ``ToolExecutionResult`` contract that
``ToolCallAgent.step()``, ``TerminationEvaluator``, and
``CoreRuntimeExecutor`` consume.

This module exists to fix B5 and B6 in the PELDRUN execution repair
plan:

  * B5 — ``ToolCallAgent.act()`` returned ``List[str]``. ``step()``
    had no structured information to decide whether the terminating
    tool actually succeeded; it had to guess from tool names (which
    is exactly what produced B3). The manager returns
    ``ToolExecutionResult``, which carries ``status``, ``outcome``,
    ``exit_code``, and tool-specific metadata — everything the
    evaluator needs.

  * B6 — ``ToolCollection.execute`` called ``fn(**args)`` inside the
    running event loop. A synchronous tool (shell command with
    ``time.sleep``, a blocking HTTP request, a slow file copy) froze
    the entire backend — SSE stopped streaming, other runs stalled,
    even UI health checks timed out. The manager offloads every
    synchronous tool to a worker thread via ``asyncio.to_thread``,
    which lets the event loop keep serving other coroutines while
    the tool runs.

Design invariants
-----------------

1. **One return shape, everywhere.**
   ``execute()`` and ``execute_by_name()`` ALWAYS return
   ``ToolExecutionResult``. Never a bare string, never a dict, never
   ``None``, never an exception. Failure paths — validation,
   resolution, timeout, cancellation, unexpected exceptions — each
   produce the appropriate ``ToolExecutionResult`` factory result.

2. **Sync offload is unconditional.**
   Any callable that is not a coroutine function runs in a worker
   thread. There is no "fast path" that runs it inline: a synchronous
   tool that LOOKS fast at code-review time can block for seconds
   in production (locking, DNS, NFS), and the whole point of B6's
   fix is that the loop must never trust that assumption. The cost
   of ``to_thread`` (~100µs scheduling) is negligible relative to
   the >0ms latency of any real tool.

3. **Timeouts are per-tool, not per-batch.**
   The manager uses ``tool.execution_policy.timeout_seconds`` when
   available, then the caller's override, then ``None`` (no
   timeout). A timeout produces ``ToolExecutionResult.timeout()``
   with ``exit_code=124`` — the standard GNU ``timeout(1)`` exit
   code — so downstream layers can distinguish "the tool ran but
   failed" from "the tool never got to finish".

4. **Cancellation produces a cancelled result, not an exception.**
   When the parent run's cancellation event fires mid-execution,
   ``asyncio.CancelledError`` propagates out of ``execute()`` (so
   the runner's cancellation contract still works). But tool-level
   cancellations that are NOT run-level (e.g. an inner
   ``asyncio.wait_for`` inside the tool itself raising
   ``CancelledError``) produce ``ToolExecutionResult.cancelled()``
   so that the caller sees a structured result rather than an
   untrusted exception.

5. **``terminate`` invariants are enforced defensively.**
   The historical ``TerminateTool`` returns ``exit_code=0`` even
   when ``metadata["status"]=="failure"`` (B4). This module
   inspects that metadata and coerces the result into a failure
   ``ToolExecutionResult`` when it detects the mismatch, so the
   contract holds at the manager boundary even before Phase C fixes
   the tool itself. After Phase C the coercion is a no-op — the
   tool already returns the correct shape.

6. **Raw results are coerced, never rejected.**
   Different tool implementations return different shapes:
   ``ToolResult``, ``{"output": ..., "is_error": ...}``,
   ``"a string"``, ``[a, list, of, things]``, ``None``. The
   manager inspects the return value and produces the
   best-possible ``ToolExecutionResult`` without ever raising a
   "wrong type" error — the caller's model needs SOMETHING to see,
   and a well-formed failure is always better than a crash.

7. **Artifacts are collected, not inferred.**
   The manager does not scan the workspace for new files. Tools
   that produce files must declare them in their ``metadata["artifacts"]``
   (or ``ToolResult.artifacts`` if the field exists on the tool's
   result). The manager copies them into the result verbatim. This
   keeps artifact tracking explicit and prevents the "every file in
   the workspace is a deliverable" bug class.

8. **No global state.**
   Each ``ExecutionManager`` instance is independent. Multiple
   managers may coexist (root run, delegated child, MCP session).
   The manager is cheap to instantiate, so callers create one per
   run rather than reaching for a singleton.

See also
--------

    * ``backend/peldrun/tools/execution_result.py``  — the canonical
                                                        result contract

    * ``backend/peldrun/tools/base.py``              — BaseTool +
                                                        ToolResult
    * ``backend/peldrun/tools/collection.py``        — ToolCollection
    * ``backend/peldrun/tools/contract.py``          — ToolRuntime +
                                                        ToolExecutionPolicy
    * ``backend/peldrun/agents/tool_call_agent.py``  — the caller that
                                                        Phase C rewires to
                                                        ``execute_many``
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)

from peldrun.tools.contract import ToolExecutionContext
from peldrun.tools.execution_result import (
    ToolExecutionResult,
    derive_outcome,
)

logger = logging.getLogger("peldrun.tools.execution_manager")


# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

#: A tool reference: either the live object or its registered name.
ToolRef = Union[str, Any]

#: One entry of a batch call. ``(tool_ref, arguments, tool_call_id)``.
ToolCallSpec = Tuple[ToolRef, Dict[str, Any], Optional[str]]


# ---------------------------------------------------------------------------
# Result coercion helpers
# ---------------------------------------------------------------------------


def _safe_get(obj: Any, field: str, default: Any = None) -> Any:
    """
    Read ``field`` from ``obj`` safely, regardless of whether ``obj``
    is a dataclass, a Pydantic model, or a plain dict.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def _coerce_artifacts(value: Any) -> List[str]:
    """
    Normalize an artifacts field to ``List[str]``.

    Accepts ``None``, ``List[str]``, ``List[Dict]`` with a ``"path"``
    key, or any iterable. Non-string entries are coerced via ``str()``
    so the canonical contract (``List[str]``) is preserved.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return [str(value)]

    out: List[str] = []
    for item in value:
        if isinstance(item, str):
            if item:
                out.append(item)
        elif isinstance(item, dict):
            path = item.get("path") or item.get("artifact")
            if path:
                out.append(str(path))
        else:
            out.append(str(item))
    return out


def _coerce_metadata(value: Any) -> Dict[str, Any]:
    """Normalize a metadata field to ``Dict[str, Any]``."""
    if isinstance(value, dict):
        return dict(value)
    return {}


def _coerce_output(value: Any) -> Any:
    """
    Normalize a raw output value.

    JSON-safe primitives pass through; everything else is stringified
    so that ``ToolExecutionResult.to_dict()`` never raises when the
    caller persists the result to a checkpoint.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            # Round-trip through json to guarantee JSON-safety.
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return str(value)
    return str(value)


# ---------------------------------------------------------------------------
# Raw-result → canonical-result conversion
# ---------------------------------------------------------------------------


def _looks_like_tool_result(obj: Any) -> bool:
    """
    Return True if ``obj`` looks like a ``ToolResult``.

    We cannot import ``ToolResult`` unconditionally because this
    module is designed to work in environments where the tools
    package is loaded lazily. Detection is duck-typed on the fields
    the conversion needs.
    """
    if obj is None or isinstance(obj, (str, bytes, int, float, bool)):
        return False
    if isinstance(obj, dict):
        return "output" in obj or "is_error" in obj
    return (
        hasattr(obj, "output")
        and (hasattr(obj, "is_error") or hasattr(obj, "exit_code"))
    )


def _convert_raw_to_execution_result(
    raw: Any,
    *,
    tool_name: str,
    tool_call_id: Optional[str],
    duration_ms: int,
) -> ToolExecutionResult:
    """
    Convert an arbitrary raw tool return value into a
    ``ToolExecutionResult``.

    Handled shapes:

      * ``ToolExecutionResult`` already → returned as-is (idempotent)
      * ``ToolResult`` (with ``.is_error``, ``.output``, etc.)
      * ``dict`` with at least ``"output"`` or ``"is_error"``
      * ``str``            → success with that string as output
      * ``None``           → success with empty output
      * anything else      → success with ``str(raw)`` as output

    The function NEVER raises. Malformed inputs degrade to a
    success-with-stringified-output result, matching the pre-existing
    behavior of ``ToolCollection.execute`` (which also treated any
    non-``ToolResult`` return as success).
    """
    # Idempotent — already canonical.
    if isinstance(raw, ToolExecutionResult):
        return raw

    # ToolResult path.
    if _looks_like_tool_result(raw):
        is_error = bool(_safe_get(raw, "is_error", False))
        exit_code = _safe_get(raw, "exit_code", 1 if is_error else 0) or 0
        output = _coerce_output(_safe_get(raw, "output", None))
        error_msg = _safe_get(raw, "error", None)
        metadata = _coerce_metadata(_safe_get(raw, "metadata", None))
        artifacts = _coerce_artifacts(_safe_get(raw, "artifacts", None))

        # ``error`` may be the informative field; fall back to
        # ``output`` as the human-readable message (existing tools
        # disagree about which one to use).
        if is_error:
            err_text = (
                str(error_msg)
                if isinstance(error_msg, str) and error_msg.strip()
                else str(output) if output else f"Tool '{tool_name}' failed."
            )
            return ToolExecutionResult.failure(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=err_text,
                output=output,
                exit_code=int(exit_code) if exit_code else 1,
                artifacts=artifacts,
                metadata=metadata,
                duration_ms=duration_ms,
            )

        return ToolExecutionResult.success(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            output=output,
            artifacts=artifacts,
            metadata=metadata,
            duration_ms=duration_ms,
        )

    # Dict fallback (some tools return a plain dict rather than a
    # ToolResult instance).
    if isinstance(raw, dict):
        is_error = bool(raw.get("is_error", False))
        exit_code = raw.get("exit_code", 1 if is_error else 0) or 0
        output = _coerce_output(raw.get("output"))
        if output is None:
            output = _coerce_output(raw.get("content"))
        error_msg = raw.get("error")
        metadata = _coerce_metadata(raw.get("metadata"))
        artifacts = _coerce_artifacts(raw.get("artifacts"))

        if is_error:
            return ToolExecutionResult.failure(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=(
                    str(error_msg)
                    if isinstance(error_msg, str) and error_msg.strip()
                    else f"Tool '{tool_name}' failed."
                ),
                output=output,
                exit_code=int(exit_code) if exit_code else 1,
                artifacts=artifacts,
                metadata=metadata,
                duration_ms=duration_ms,
            )
        return ToolExecutionResult.success(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            output=output,
            artifacts=artifacts,
            metadata=metadata,
            duration_ms=duration_ms,
        )

    # Everything else — stringify defensively. This preserves the
    # historical behavior where a bare ``str`` return meant "success
    # with this text as the observation".
    return ToolExecutionResult.success(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        output=_coerce_output(raw) if raw is not None else "",
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# Terminate invariant enforcement
# ---------------------------------------------------------------------------


def _enforce_terminate_invariant(
    result: ToolExecutionResult,
    original_raw: Any,
) -> ToolExecutionResult:
    """
    Fix B4 at the manager boundary, before Phase C touches
    ``TerminateTool``.

    Historical behavior: ``TerminateTool(status="failure")`` returned
    ``ToolResult(exit_code=0, metadata={"status": "failure",
    "terminated": True})``. Any caller that inspected ``exit_code``
    saw success.

    This helper inspects ``metadata["status"]`` and, when it says
    "failure" but the result was recorded as success, rebuilds the
    result as a proper failure — so the canonical
    ``ToolExecutionResult`` returned by this manager is CORRECT even
    when the underlying tool is not.

    After Phase C the underlying tool returns the right shape and
    this function is a no-op.

    Cases handled:

      * ``metadata.status="failure"`` + ``result.is_success=True``
        → coerced to ``ToolExecutionResult.failure()``
      * ``metadata.status="success"`` + ``result.is_failure=True``
        → coerced to ``ToolExecutionResult.success()`` (defensive:
        the tool reported success intent, so downstream layers must
        see a success result — the failure that caused the raw
        mismatch is captured in ``metadata["coercion_note"]``)

    Non-terminate tools, and terminate calls without a recognized
    ``metadata.status``, pass through unchanged.
    """
    if not isinstance(result, ToolExecutionResult):
        return result

    metadata = result.metadata or {}
    raw_status = metadata.get("status")
    if not isinstance(raw_status, str):
        return result
    requested = raw_status.strip().lower()
    if requested not in ("success", "failure"):
        return result

    if requested == "failure" and result.is_success:
        note = (
            "Coerced to failure by ExecutionManager because "
            "metadata.status='failure' but the tool returned "
            "success. See B4 in the execution repair plan."
        )
        new_meta = dict(metadata)
        new_meta["coercion_note"] = note
        logger.warning(
            "B4 coercion: tool '%s' returned success with "
            "metadata.status='failure'; promoting to failure result.",
            result.tool_name,
        )
        return ToolExecutionResult.failure(
            tool_name=result.tool_name,
            tool_call_id=result.tool_call_id,
            error=result.error
            or str(result.output)
            or "Terminate reported failure.",
            output=result.output,
            exit_code=max(1, result.exit_code or 0),
            artifacts=list(result.artifacts),
            metadata=new_meta,
            duration_ms=result.duration_ms,
        )

    if requested == "success" and result.is_failure:
        # [P2 FIX — B4-R]
        # The historical implementation promoted a failure to a
        # success here, silently completing the run on a broken
        # terminate. See "B4-R" in the PELDRUN execution repair plan.
        #
        # The actual ``is_failure`` signal is authoritative. Coercing
        # to success would let ``TerminateTool(status="success")``
        # overwrite the real tool outcome whenever the tool call
        # itself failed -- exactly the class of silent recovery the
        # plan forbids.
        #
        # The diagnostic metadata and warning remain so operators
        # can see the mismatch in logs and in the result without
        # any silent state change.
        note = (
            "Tool reported metadata.status='success' but the raw "
            "call failed. NOT coerced to success (B4-R). The "
            "actual failure is preserved; see metadata.raw_error."
        )
        new_meta = dict(metadata)
        new_meta["coercion_note"] = note
        new_meta["raw_error"] = result.error
        logger.warning(
            "B4-R: tool '%s' claimed status='success' but the raw "
            "call failed (%s). Returning the ACTUAL failure; not "
            "coercing to success.",
            result.tool_name,
            result.error or "no error message",
        )
        # Attach diagnostic metadata without changing the outcome.
        # Downstream consumers (TerminationEvaluator, ToolCallAgent
        # .step) receive the true result.
        result.metadata = new_meta
        return result

    return result


# ---------------------------------------------------------------------------
# Concrete ToolExecutionContext implementation
# ---------------------------------------------------------------------------


class ToolExecutionContextImpl:
    """
    Concrete implementation of :class:`ToolExecutionContext`.

    Lifecycle
    ---------
    One instance is created by the agent loop for each
    ``(tool_call_id, attempt)`` pair. The agent owns the backing
    ``asyncio.Queue`` and drains it in a background task, translating
    each item into a ``TOOL_OUTPUT`` or ``TOOL_PROGRESS`` SSE event.

    The context is a write-only view for the tool; the agent is the
    sole reader of the queue. This separation keeps the tool agnostic
    about event dispatch and the agent agnostic about how the tool
    produces its chunks.

    Thread safety
    -------------
    Two method families:

    * ``async emit_output`` / ``async emit_progress`` -- for async tools
      running on the main event loop. They enqueue via
      ``queue.put_nowait`` directly.

    * ``emit_output_sync`` / ``emit_progress_sync`` -- for synchronous
      tools offloaded to a worker thread via ``asyncio.to_thread``. They
      enqueue via ``loop.call_soon_threadsafe`` so the queue is mutated
      only from the main loop thread.

    Both families share a lock-protected sequence counter, so the
    ``sequence`` field is strictly monotonic even when a sync tool
    interleaves async sub-calls (which can happen when a sync tool
    internally spawns its own async runtime).

    Best-effort semantics
    ---------------------
    Per the ``ToolExecutionContext`` contract, ``emit_output`` and
    ``emit_progress`` MUST NOT raise. If the queue is full (bounded
    queue with a slow consumer) or the loop has shut down, the chunk is
    silently dropped. Losing a progress tick is always preferable to
    failing a running tool.

    The sync family additionally swallows ``RuntimeError`` from
    ``call_soon_threadsafe`` -- that exception fires when the loop is
    already closed (run cancelled, process shutting down), which is
    again a "drop the chunk" situation, not a tool failure.
    """

    def __init__(
        self,
        tool_call_id: str,
        tool_name: str,
        attempt: int,
        queue: "asyncio.Queue[Optional[Dict[str, Any]]]",
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.attempt = attempt
        self._queue = queue
        # Capture the loop eagerly so a later ``emit_output_sync`` call
        # from a worker thread can still schedule onto it via
        # ``call_soon_threadsafe``. When no loop is running (e.g. the
        # context is constructed in a unit test with no event loop),
        # ``_loop`` is None and the sync emits become silent no-ops.
        if loop is not None:
            self._loop: Optional[asyncio.AbstractEventLoop] = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
        self._seq: int = 0
        self._seq_lock = threading.Lock()

    # -- Internal -----------------------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _build_chunk(self, chunk: str, stream: str) -> Dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "stream": stream,
            "chunk": chunk,
            "sequence": self._next_seq(),
            "attempt": self.attempt,
        }

    def _build_progress(
        self,
        *,
        current: Optional[int],
        total: Optional[int],
        phase: Optional[str],
        message: Optional[str],
    ) -> Dict[str, Any]:
        payload = self._build_chunk(message or "", "progress")
        payload["current"] = current
        payload["total"] = total
        payload["phase"] = phase
        return payload

    # -- Async path (for async tools) --------------------------------

    async def emit_output(
        self,
        chunk: str,
        *,
        stream: Literal["stdout", "stderr", "output"] = "stdout",
    ) -> None:
        try:
            self._queue.put_nowait(self._build_chunk(chunk, stream))
        except asyncio.QueueFull:
            # Bounded queue -- drop rather than block the tool.
            pass

    async def emit_progress(
        self,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
        phase: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        payload = self._build_progress(
            current=current,
            total=total,
            phase=phase,
            message=message,
        )
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    # -- Sync path (for sync tools running in a worker thread) -------

    def emit_output_sync(
        self,
        chunk: str,
        *,
        stream: Literal["stdout", "stderr", "output"] = "stdout",
    ) -> None:
        """
        Thread-safe emit for synchronous tools.

        Sync tools run inside ``asyncio.to_thread``'s worker thread,
        where ``await`` is not available. This method provides the same
        semantics as ``emit_output`` but is a plain function that
        schedules the enqueue on the main event loop.
        """
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait,
                self._build_chunk(chunk, stream),
            )
        except RuntimeError:
            # Loop already closed -- run was cancelled or is shutting
            # down. Drop the chunk silently; the tool itself is
            # unaffected.
            pass

    def emit_progress_sync(
        self,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
        phase: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """Thread-safe variant of :meth:`emit_progress`."""
        if self._loop is None:
            return
        payload = self._build_progress(
            current=current,
            total=total,
            phase=phase,
            message=message,
        )
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ExecutionManager:
    """
    Stateless gateway for tool invocation.

    One instance is created per run (cheap). The manager holds a
    reference to the run's tool registry so that
    ``execute_by_name()`` can resolve names without the caller
    pre-resolving them. Direct-call paths
    (``execute(tool_instance, ...)``) work without a registry.

    Concurrency
    -----------
    The manager is safe for concurrent ``execute`` calls. It holds no
    mutable state — every helper is pure. Internally the manager
    never awaits on shared locks.
    """

    def __init__(
        self,
        registry: Optional[Any] = None,
        *,
        default_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._registry = registry
        self._default_timeout = default_timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        tool: Any,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        run_id: Optional[str] = None,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """
        Execute a single tool instance and return its canonical result.

        Parameters
        ----------
        tool
            The live tool object. Must expose ``aexecute`` (async),
            ``execute`` (sync/async), or ``_arun`` (async).
        arguments
            The keyword arguments to pass to the tool. ``None`` is
            treated as ``{}``.
        tool_name
            Optional explicit name. Defaults to ``tool.name``. Used
            for the ``tool_name`` field of the result, and for
            logging when the tool is not a ``BaseTool`` instance.
        tool_call_id
            Optional correlation id (the originating ``ToolCall.id``).
            Forwarded to the result for pairing with the assistant
            message that emitted the call.
        timeout_seconds
            Optional override. Falls back to the tool's
            ``execution_policy.timeout_seconds``, then to
            ``default_timeout_seconds``. ``None`` disables the timeout.
        run_id
            Optional. Only used for log enrichment.
        context
            Optional streaming context. When supplied AND the tool's
            chosen entrypoint declares an explicit ``context``
            parameter, the manager forwards the context to the tool
            so it can publish intermediate output via
            ``await context.emit_output(...)`` (or the sync variants
            for sync tools). When ``None``, no context is created and
            the tool runs exactly as before -- the feature is fully
            opt-in.

        Returns
        -------
        ToolExecutionResult
            NEVER raises. All exceptions are captured in the result
            (as ``status="failure"``) or propagated as
            ``status="cancelled"``.
        """
        resolved_name = (
            tool_name
            or _safe_get(tool, "name", None)
            or "<unnamed_tool>"
        )

        args = dict(arguments or {})

        effective_timeout = self._resolve_timeout(
            tool,
            explicit=timeout_seconds,
        )

        started_at = time.monotonic()

        # ------------------------------------------------------------------
        # Resolution failure — tool object is unusable.
        # ------------------------------------------------------------------
        if tool is None:
            return ToolExecutionResult.failure(
                tool_name=resolved_name,
                tool_call_id=tool_call_id,
                error=(
                    f"Tool '{resolved_name}' could not be resolved. "
                    f"The tool name is not registered in the active "
                    f"registry, or the tool object is None."
                ),
                exit_code=1,
                duration_ms=0,
            )

        callable_kind, callable_obj = self._find_entrypoint(tool)
        if callable_obj is None:
            return ToolExecutionResult.failure(
                tool_name=resolved_name,
                tool_call_id=tool_call_id,
                error=(
                    f"Tool '{resolved_name}' does not expose a "
                    f"callable entrypoint (expected 'aexecute', "
                    f"'execute', or '_arun')."
                ),
                exit_code=1,
                duration_ms=0,
            )

        # ------------------------------------------------------------------
        # Invoke with timeout + cancellation + thread offload.
        # ------------------------------------------------------------------
        try:
            raw = await self._invoke(
                callable_obj=callable_obj,
                callable_kind=callable_kind,
                args=args,
                timeout_seconds=effective_timeout,
                tool_name=resolved_name,
                run_id=run_id,
                context=context,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.warning(
                "[run=%s] Tool '%s' timed out after %.1fs.",
                run_id or "<none>",
                resolved_name,
                effective_timeout or 0.0,
            )
            return ToolExecutionResult.timeout(
                tool_name=resolved_name,
                tool_call_id=tool_call_id,
                timeout_seconds=float(effective_timeout or 0.0),
                duration_ms=elapsed_ms,
            )
        except asyncio.CancelledError:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            # Re-raise after producing a result — but the CALLER
            # needs to know the run was cancelled. The idiomatic
            # solution is: result is returned, but we ALSO
            # re-raise so the runner's cancellation contract still
            # works.
            #
            # We achieve this by producing the result, logging it,
            # then re-raising. Callers that catch
            # ``CancelledError`` observe the cancellation; callers
            # that don't (buggy) still get a log entry.
            result = ToolExecutionResult.cancelled(
                tool_name=resolved_name,
                tool_call_id=tool_call_id,
                error="Tool execution was cancelled.",
                duration_ms=elapsed_ms,
            )
            logger.info(
                "[run=%s] Tool '%s' cancelled after %.1fs.",
                run_id or "<none>",
                resolved_name,
                (time.monotonic() - started_at),
            )
            # Re-raise AFTER logging. The result is constructed for
            # symmetry with the other branches but not returned —
            # cancellation must propagate to the runner.
            raise
        except Exception as exc:  # pragma: no cover — defensive only
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.exception(
                "[run=%s] Tool '%s' raised an unexpected exception.",
                run_id or "<none>",
                resolved_name,
            )
            return ToolExecutionResult.failure(
                tool_name=resolved_name,
                tool_call_id=tool_call_id,
                error=f"Tool execution raised: {exc}",
                exit_code=1,
                metadata={"error_type": type(exc).__name__},
                duration_ms=elapsed_ms,
            )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)

        # ------------------------------------------------------------------
        # Convert + enforce terminate invariant + normalize.
        # ------------------------------------------------------------------
        result = _convert_raw_to_execution_result(
            raw,
            tool_name=resolved_name,
            tool_call_id=tool_call_id,
            duration_ms=elapsed_ms,
        )

        # Ensure duration is set even when conversion produced a
        # fresh result with the default 0.
        if result.duration_ms == 0:
            result.duration_ms = elapsed_ms

        if is_terminating_tool(resolved_name):
            result = _enforce_terminate_invariant(result, raw)

        return result

    async def execute_by_name(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        tool_call_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        run_id: Optional[str] = None,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """
        Resolve a tool by name from the registry and execute it.

        Equivalent to ``execute(self._registry.get(name), ...)``. A
        resolution failure produces a ``ToolExecutionResult.failure()``
        with a clear "not found" message — never raises.

        ``context`` is forwarded to ``execute`` unchanged; see that
        method for the full opt-in streaming semantics.
        """
        if self._registry is None:
            return ToolExecutionResult.failure(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=(
                    f"ExecutionManager has no registry configured; "
                    f"cannot resolve tool '{tool_name}' by name."
                ),
                exit_code=1,
            )

        tool = self._resolve_tool(tool_name)
        if tool is None:
            available = self._list_tool_names()
            return ToolExecutionResult.failure(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=(
                    f"Tool '{tool_name}' is not registered. "
                    f"Available tools: {', '.join(available) if available else '(none)'}."
                ),
                exit_code=1,
                metadata={"available_tools": available},
            )

        return await self.execute(
            tool,
            arguments,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            timeout_seconds=timeout_seconds,
            run_id=run_id,
            context=context,
        )

    async def execute_many(
        self,
        specs: Iterable[ToolCallSpec],
        *,
        run_id: Optional[str] = None,
        sequential: bool = False,
    ) -> List[ToolExecutionResult]:
        """
        Execute multiple tools, returning results in the same order
        as the input.

        By default runs concurrently via ``asyncio.gather`` — the
        typical case where the model emits multiple parallel tool
        calls benefits from overlapping I/O. Synchronous tools are
        already offloaded to threads, so "concurrent" here means
        multiple threads running in parallel, which is exactly the
        behavior we want.

        Pass ``sequential=True`` for tools that MUST run in order
        (e.g., a write followed by a read of the same file).

        Cancellation
        ------------
        When the outer task is cancelled, ``asyncio.gather`` cancels
        all in-flight tool coroutines. Results already collected are
        discarded (caller observes ``CancelledError``). This matches
        the runner contract — cancellation is not a per-tool event,
        it aborts the whole batch.

        Parameters
        ----------
        specs
            Iterable of ``(tool_ref, arguments, tool_call_id)``.
            ``tool_ref`` may be a live tool object OR a name (resolved
            via the registry).
        run_id
            Optional. Forwarded to each individual ``execute`` call
            for log enrichment.
        sequential
            Run tools one at a time in input order.
        """
        spec_list = list(specs)
        if not spec_list:
            return []

        async def _run_one(spec: ToolCallSpec) -> ToolExecutionResult:
            tool_ref, args, call_id = spec
            if isinstance(tool_ref, str):
                return await self.execute_by_name(
                    tool_ref,
                    args,
                    tool_call_id=call_id,
                    run_id=run_id,
                )
            return await self.execute(
                tool_ref,
                args,
                tool_call_id=call_id,
                run_id=run_id,
            )

        if sequential:
            results: List[ToolExecutionResult] = []
            for spec in spec_list:
                results.append(await _run_one(spec))
            return results

        # Concurrent path. ``gather(return_exceptions=False)``
        # propagates ``CancelledError`` from any child — matching
        # the run-level cancellation contract.
        return list(
            await asyncio.gather(*[_run_one(s) for s in spec_list])
        )

    # ------------------------------------------------------------------
    # Internals — resolution
    # ------------------------------------------------------------------

    def _resolve_tool(self, name: str) -> Optional[Any]:
        """Resolve a tool name via the registry, tolerating API variants."""
        if self._registry is None:
            return None
        registry = self._registry

        if hasattr(registry, "get") and callable(registry.get):
            try:
                tool = registry.get(name)
                if tool is not None:
                    return tool
            except Exception:
                pass

        if hasattr(registry, "get_tool") and callable(registry.get_tool):
            try:
                tool = registry.get_tool(name)
                if tool is not None:
                    return tool
            except Exception:
                pass

        if hasattr(registry, "list_tools") and callable(registry.list_tools):
            try:
                for candidate in registry.list_tools():
                    if _safe_get(candidate, "name", None) == name:
                        return candidate
            except Exception:
                pass

        return None

    def _list_tool_names(self) -> List[str]:
        """Return the names of all registered tools (best-effort)."""
        if self._registry is None:
            return []
        registry = self._registry

        if hasattr(registry, "list_tools") and callable(registry.list_tools):
            try:
                return [
                    _safe_get(t, "name", None) or str(t)
                    for t in registry.list_tools()
                ]
            except Exception:
                pass

        if hasattr(registry, "_collection"):
            collection = getattr(registry, "_collection", None)
            if collection is not None and hasattr(collection, "list_tools"):
                try:
                    return [
                        _safe_get(t, "name", None) or str(t)
                        for t in collection.list_tools()
                    ]
                except Exception:
                    pass

        return []

    # ------------------------------------------------------------------
    # Internals — entrypoint discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_entrypoint(tool: Any) -> Tuple[str, Optional[Any]]:
        """
        Return ``(kind, callable)`` describing how to invoke ``tool``.

        ``kind`` is one of ``"async"``, ``"sync"``, ``"async_callable"``,
        ``"sync_callable"``. ``callable`` is ``None`` when no
        entrypoint exists.

        Preference order: ``aexecute`` > ``execute`` > ``_arun``.
        ``aexecute`` is the canonical public async entrypoint (defined
        in ``BaseTool``); it handles validation and timeout
        internally, so using it prefers the tool's own policy. The
        manager's external timeout wraps it as a safety net.
        """
        for name in ("aexecute", "execute", "_arun"):
            candidate = getattr(tool, name, None)
            if candidate is None or not callable(candidate):
                continue
            if inspect.iscoroutinefunction(candidate):
                return ("async", candidate)
            return ("sync", candidate)
        return ("none", None)

    # ------------------------------------------------------------------
    # Internals — timeout resolution
    # ------------------------------------------------------------------

    def _resolve_timeout(
        self,
        tool: Any,
        *,
        explicit: Optional[float],
    ) -> Optional[float]:
        """
        Resolve the effective per-call timeout.

        Precedence (highest to lowest):

          1. ``explicit`` argument (caller's override)
          2. Tool's own ``get_execution_policy().timeout_seconds``
          3. Tool's own ``execution_policy.timeout_seconds`` attribute
          4. Manager's ``default_timeout_seconds``
          5. ``None`` (no timeout)
        """
        if explicit is not None:
            try:
                return float(explicit) if float(explicit) > 0 else None
            except (TypeError, ValueError):
                pass

        policy = None
        getter = getattr(tool, "get_execution_policy", None)
        if callable(getter):
            try:
                policy = getter()
            except Exception:
                policy = None
        if policy is None:
            policy = getattr(tool, "execution_policy", None)

        if policy is not None:
            raw = getattr(policy, "timeout_seconds", None)
            if raw is not None:
                try:
                    val = float(raw)
                    if val > 0:
                        return val
                except (TypeError, ValueError):
                    pass

        if self._default_timeout is not None:
            try:
                val = float(self._default_timeout)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass

        return None

    # ------------------------------------------------------------------
    # Internals — invocation with timeout + sync offload
    # ------------------------------------------------------------------

    @staticmethod
    def _callable_accepts_context(callable_obj: Any) -> bool:
        """
        Return True if ``callable_obj`` declares an explicit
        ``context`` parameter in its signature.

        A method whose only context-related hook is ``**kwargs`` is NOT
        considered opt-in: forwarding ``context`` into ``**kwargs``
        would leak it into the tool's domain arguments (the exact class
        of bug this signature check is designed to avoid). This mirrors
        ``BaseTool._arun_accepts_context`` and must remain consistent
        with it -- both layers guard the same boundary.
        """
        try:
            sig = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return False
        if "context" not in sig.parameters:
            return False
        param = sig.parameters["context"]
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return False
        return True

    async def _invoke(
        self,
        *,
        callable_obj: Any,
        callable_kind: str,
        args: Dict[str, Any],
        timeout_seconds: Optional[float],
        tool_name: str,
        run_id: Optional[str],
        context: Optional[ToolExecutionContext] = None,
    ) -> Any:
        """
        Invoke the tool, honoring ``callable_kind``.

        * ``"async"`` — call the coroutine function directly.
        * ``"sync"``  — offload to a worker thread via
                       ``asyncio.to_thread``.

        When ``timeout_seconds`` is set, the entire invocation (sync
        or async) is wrapped in ``asyncio.wait_for``.

        Context forwarding
        ------------------
        When ``context`` is not None AND ``callable_obj`` declares an
        explicit ``context`` parameter, the context is injected into
        the kwargs before invocation. Otherwise the call proceeds
        untouched -- legacy tools see the exact same kwargs they always
        received.

        Known limitation for sync tools with timeout
        --------------------------------------------
        ``asyncio.wait_for`` on ``to_thread`` cancels the FUTURE, not
        the underlying thread. A synchronous tool that blocks
        forever continues running in its worker thread even after
        the future is cancelled. There is no portable way to abort
        an arbitrary Python thread. Mitigations:

          * The worker thread is daemonized (``to_thread`` uses the
            default ``concurrent.futures`` pool, whose threads are
            daemons) — it will not prevent process exit.
          * The runner's cancellation path emits the terminal event
            immediately and continues cleanup; the leaked thread is
            bounded by the tool's OWN internal timeout (or, for
            tools without one, by process restart).
          * Well-behaved tools (``BaseTool.aexecute``) already
            enforce their own timeout via ``wait_for`` inside
            ``_arun``; this outer timeout is a safety net for
            third-party tools that do not.
        """
        # Build the effective kwargs: base arguments plus context when
        # the target callable explicitly opts in.
        call_kwargs: Dict[str, Any] = dict(args)
        if context is not None and self._callable_accepts_context(callable_obj):
            call_kwargs["context"] = context

        if callable_kind == "async":
            coro = callable_obj(**call_kwargs)
        else:
            # Sync offload. ``to_thread`` schedules the callable on
            # the default executor; the event loop is free to serve
            # other coroutines while the worker runs.
            coro = asyncio.to_thread(callable_obj, **call_kwargs)

        if timeout_seconds is not None and timeout_seconds > 0:
            return await asyncio.wait_for(coro, timeout=timeout_seconds)
        return await coro


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def is_terminating_tool(tool_name: str) -> bool:
    """
    Return True if ``tool_name`` should trigger termination invariant
    enforcement.

    Delegates to ``peldrun.agents.termination.is_terminating_tool``
    to keep a SINGLE definition of "which tools can terminate". The
    import is performed lazily inside this function to avoid an
    import cycle at module load time — ``termination`` imports from
    ``execution_result`` which is independent, so the cycle would
    not fire today, but the lazy import is defensive against future
    refactors.
    """
    try:
        from peldrun.agents.termination import is_terminating_tool as _t

        return _t(tool_name)
    except Exception:
        # Fallback to the literal set. If the termination module is
        # unavailable for any reason, B4 enforcement must still work.
        if not tool_name:
            return False
        return str(tool_name).strip().lower() in ("terminate",)


__all__ = [
    "ExecutionManager",
    "ToolCallSpec",
    "ToolExecutionContextImpl",
    "ToolRef",
    "is_terminating_tool",
]
