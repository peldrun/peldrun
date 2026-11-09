# Path: backend/peldrun/tools/execution_result.py
"""
PELDRUN Core Tool Execution Result Contract.

Defines the canonical ``ToolExecutionResult`` — the single structure
that every tool invocation MUST return. This replaces the historical
``List[str]`` / bare-dict returns that made ``ToolCallAgent.step()``
unable to verify tool success without inspecting tool names.

Design invariants
-----------------

1. **Single success path.** Every tool invocation — builtin, MCP,
   delegated child, web tool, plugin — returns exactly one
   ``ToolExecutionResult``. There is no alternative success channel
   (no bare strings, no ad-hoc dicts, no ``bool`` returns).

2. **``status`` and ``exit_code`` are aligned by construction.**
   Factory methods enforce:

       status == "success"     ⇔  exit_code == 0
       status in {"failure",
                  "cancelled",
                  "timeout"}   ⇔  exit_code != 0

   This guarantees that ``TerminateTool(status="failure")`` cannot be
   mistaken for success by any caller that only inspects ``exit_code``
   (the historical B4 bug in the PELDRUN execution repair plan).

3. **Immutable by convention.** Result instances are read-only data.
   ``ToolCallAgent``, ``TerminationEvaluator``, and ``ExecutionManager``
   consume them; they never mutate fields in place.

4. **JSON-safe.** ``to_dict()`` produces a representation that can be
   written to checkpoints, embedded in SSE payloads, and persisted in
   the token ledger without a custom encoder.

5. **Zero external imports.** This module depends only on the Python
   standard library. Any circular-import concern with ``peldrun.tools``
   or ``peldrun.engine`` is structurally impossible.

See also
--------

    * ``backend/peldrun/tools/execution_manager.py``
        The single call-site that constructs ``ToolExecutionResult``
        instances from ``BaseTool`` executions.

    * ``backend/peldrun/agents/termination.py``
        Consumes ``List[ToolExecutionResult]`` to produce a
        ``TerminationDecision``.

    * ``backend/peldrun/agents/tool_call_agent.py``
        ``act()`` returns ``List[ToolExecutionResult]``; ``step()``
        relies on it (via ``termination.evaluate``) to decide whether
        the task is complete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union
import time
import uuid


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Fine-grained tool outcome. Matches the ``status`` field of the
#: canonical ``ToolExecutionResult`` contract.
#:
#: ``success``   — the tool completed its work and produced an output.
#: ``failure``   — the tool ran but could not complete its work (bad
#:                 arguments, tool-level error, non-zero shell exit).
#: ``cancelled`` — the tool was interrupted by the cancellation
#:                 contract (user pressed Stop, run cancelled, or the
#:                 parent run terminated).
#: ``timeout``   — the tool exceeded its per-call deadline without
#:                 completing. Distinct from ``failure`` so that the
#:                 termination evaluator can recommend a retry instead
#:                 of a hard abort.
ToolExecutionStatus = Literal["success", "failure", "cancelled", "timeout"]

#: Coarse-grained outcome summary. Derived deterministically from
#: ``status`` via ``ToolExecutionResult.derive_outcome()``. Consumed by
#: UI displays and high-level summaries; NOT used for control flow.
ToolExecutionOutcome = Literal["completed", "failed", "aborted"]

#: The set of statuses considered "aborted" — the tool did not finish
#: its work, but it also did not produce a semantic failure (it was
#: interrupted externally).
_ABORTED_STATUSES: frozenset = frozenset({"cancelled", "timeout"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_tool_call_id() -> str:
    """Generate a unique tool_call_id for calls that did not originate
    from an LLM tool invocation (internal helpers, system calls)."""
    return f"tc_{uuid.uuid4().hex[:12]}"


def derive_outcome(status: ToolExecutionStatus) -> ToolExecutionOutcome:
    """
    Map a fine-grained ``status`` to its coarse ``outcome``.

    Deterministic and total: every ``ToolExecutionStatus`` maps to
    exactly one ``ToolExecutionOutcome``.
    """
    if status == "success":
        return "completed"
    if status == "failure":
        return "failed"
    # cancelled | timeout
    return "aborted"


# ---------------------------------------------------------------------------
# Canonical result
# ---------------------------------------------------------------------------


@dataclass
class ToolExecutionResult:
    """
    Canonical result of a single tool invocation.

    Construct with one of the factory methods (``success()``,
    ``failure()``, ``cancelled()``, ``timeout()``) rather than calling
    the dataclass constructor directly, so that the ``status`` /
    ``exit_code`` / ``outcome`` invariants are enforced at the call
    site.

    Attributes
    ----------
    tool_call_id
        Unique identifier of this specific tool invocation. Matches
        ``ToolCall.id`` when the call originated from an LLM tool
        invocation; auto-generated otherwise.
    tool_name
        Canonical tool identifier (``"shell"``, ``"terminate"``,
        ``"mcp__filesystem__read_file"``, ...).
    status
        Fine-grained execution outcome.
    outcome
        Coarse-grained summary, derived from ``status``.
    output
        Tool-specific payload on success. Most builtins return a
        ``str``; structured tools may return ``dict`` / ``list``.
    error
        Human-readable error message on non-success. ``None`` when
        ``status == "success"``.
    exit_code
        Numeric exit code. ``0`` on success, non-zero otherwise.
    artifacts
        Workspace-relative paths of files the tool produced.
    metadata
        Free-form tool-specific metadata. Diagnostics only; MUST NOT
        drive control flow.
    duration_ms
        Wall-clock execution time in milliseconds.
    """

    tool_call_id: str = field(default_factory=_default_tool_call_id)
    tool_name: str = ""
    status: ToolExecutionStatus = "success"
    outcome: ToolExecutionOutcome = "completed"
    output: Any = None
    error: Optional[str] = None
    exit_code: int = 0
    artifacts: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    # ------------------------------------------------------------------
    # Factory methods (the ONLY sanctioned construction path)
    # ------------------------------------------------------------------

    @classmethod
    def success(
        cls,
        *,
        tool_name: str,
        output: Any = None,
        tool_call_id: Optional[str] = None,
        artifacts: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
    ) -> "ToolExecutionResult":
        """Build a successful result (exit_code = 0, outcome = completed)."""
        return cls(
            tool_call_id=tool_call_id or _default_tool_call_id(),
            tool_name=tool_name,
            status="success",
            outcome="completed",
            output=output,
            error=None,
            exit_code=0,
            artifacts=list(artifacts or []),
            metadata=dict(metadata or {}),
            duration_ms=int(duration_ms),
        )

    @classmethod
    def failure(
        cls,
        *,
        tool_name: str,
        error: str,
        output: Any = None,
        tool_call_id: Optional[str] = None,
        exit_code: int = 1,
        artifacts: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
    ) -> "ToolExecutionResult":
        """
        Build a failed result.

        ``exit_code`` is forced to a non-zero value: passing 0 is
        rejected by clamping to 1. This is the direct fix for the B4
        bug where ``terminate(status="failure")`` returned exit_code=0
        and was therefore misinterpreted as success by ``step()``.
        """
        if exit_code == 0:
            # Defensive: never allow a failed status to look successful
            # to exit-code-only callers.
            exit_code = 1
        return cls(
            tool_call_id=tool_call_id or _default_tool_call_id(),
            tool_name=tool_name,
            status="failure",
            outcome="failed",
            output=output,
            error=error or "Tool execution failed without an error message.",
            exit_code=int(exit_code),
            artifacts=list(artifacts or []),
            metadata=dict(metadata or {}),
            duration_ms=int(duration_ms),
        )

    @classmethod
    def cancelled(
        cls,
        *,
        tool_name: str,
        error: str = "Tool execution was cancelled.",
        tool_call_id: Optional[str] = None,
        output: Any = None,
        artifacts: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
    ) -> "ToolExecutionResult":
        """Build a cancelled result (exit_code = 130, mimicking SIGINT)."""
        return cls(
            tool_call_id=tool_call_id or _default_tool_call_id(),
            tool_name=tool_name,
            status="cancelled",
            outcome="aborted",
            output=output,
            error=error,
            # 130 = 128 + SIGINT(2). Standard convention for "the
            # process was interrupted by the user".
            exit_code=130,
            artifacts=list(artifacts or []),
            metadata=dict(metadata or {}),
            duration_ms=int(duration_ms),
        )

    @classmethod
    def timeout(
        cls,
        *,
        tool_name: str,
        timeout_seconds: float,
        tool_call_id: Optional[str] = None,
        output: Any = None,
        artifacts: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
    ) -> "ToolExecutionResult":
        """Build a timeout result (exit_code = 124, mimicking `timeout(1)`)."""
        return cls(
            tool_call_id=tool_call_id or _default_tool_call_id(),
            tool_name=tool_name,
            status="timeout",
            outcome="aborted",
            output=output,
            error=(
                f"Tool '{tool_name}' exceeded its deadline of "
                f"{timeout_seconds:.1f}s."
            ),
            # 124 = the exit code GNU `timeout` uses for "timed out".
            exit_code=124,
            artifacts=list(artifacts or []),
            metadata=dict(metadata or {}),
            duration_ms=int(duration_ms),
        )

    # ------------------------------------------------------------------
    # Convenience predicates
    # ------------------------------------------------------------------

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    @property
    def is_failure(self) -> bool:
        return self.status == "failure"

    @property
    def is_aborted(self) -> bool:
        return self.status in _ABORTED_STATUSES

    @property
    def is_terminal(self) -> bool:
        """Every ToolExecutionResult is terminal by definition."""
        return True

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a JSON-safe representation.

        ``output`` is passed through unchanged when it is a JSON-safe
        primitive (``str`` / ``int`` / ``float`` / ``bool`` / ``None`` /
        ``dict`` / ``list``). Any other type is stringified so that the
        resulting dict can always be written via ``json.dumps``.
        """
        data = asdict(self)
        # ``output`` may be an arbitrary object (e.g., a Path, a custom
        # tool result class). Stringify non-JSON-safe values defensively
        # so that checkpointing and SSE emission never raise.
        output = data.get("output")
        if output is not None and not isinstance(
            output, (str, int, float, bool, dict, list)
        ):
            data["output"] = str(output)
        return data

    # ------------------------------------------------------------------
    # Developer-facing helpers
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """
        One-line human-readable summary, suitable for log lines and
        diagnostic event payloads.
        """
        head = f"[{self.status}] {self.tool_name or '<unnamed>'} " f"({self.duration_ms}ms, exit={self.exit_code})"
        if self.is_success:
            return f"{head}: {self._short(self.output)}"
        return f"{head}: {self.error or '<no error message>'}"

    @staticmethod
    def _short(value: Any, limit: int = 120) -> str:
        text = "" if value is None else str(value)
        text = text.replace("\n", " ").strip()
        return text[:limit] + ("..." if len(text) > limit else "")

    def __repr__(self) -> str:
        return self.summary()


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def coalesce_results(
    results: List[ToolExecutionResult],
) -> ToolExecutionResult:
    """
    Fold multiple tool results into a single representative result.

    Used by ``ExecutionManager`` for parallel batch calls where the
    caller needs one outcome for the whole batch (e.g., a tool that
    internally invoked three sub-tools and must report one exit code
    to the model).

    Precedence (highest severity wins):

        cancelled > timeout > failure > success

    The aggregated ``exit_code`` is the FIRST non-zero exit code from
    the batch (preserves the "which tool actually failed" signal), or
    0 when every result succeeded.

    The aggregated ``output`` is a list of per-result outputs, in the
    order the results were provided.
    """
    if not results:
        # Degenerate case: return a success result with empty output.
        # Callers are expected to never pass an empty list, but we
        # refuse to raise — the manager guarantees non-empty batches.
        return ToolExecutionResult.success(tool_name="<empty_batch>", output=[])

    statuses = [r.status for r in results]

    if "cancelled" in statuses:
        first_cancelled = next(r for r in results if r.status == "cancelled")
        return ToolExecutionResult.cancelled(
            tool_name=first_cancelled.tool_name,
            tool_call_id=first_cancelled.tool_call_id,
            error=first_cancelled.error,
            output=[r.output for r in results],
            artifacts=[a for r in results for a in r.artifacts],
            metadata={"batch_size": len(results), "statuses": statuses},
            duration_ms=sum(r.duration_ms for r in results),
        )

    if "timeout" in statuses:
        first_timeout = next(r for r in results if r.status == "timeout")
        return ToolExecutionResult.timeout(
            tool_name=first_timeout.tool_name,
            tool_call_id=first_timeout.tool_call_id,
            timeout_seconds=0.0,  # already captured per-result
            output=[r.output for r in results],
            artifacts=[a for r in results for a in r.artifacts],
            metadata={"batch_size": len(results), "statuses": statuses},
            duration_ms=sum(r.duration_ms for r in results),
        )

    if "failure" in statuses:
        first_failure = next(r for r in results if r.status == "failure")
        return ToolExecutionResult.failure(
            tool_name=first_failure.tool_name,
            tool_call_id=first_failure.tool_call_id,
            error=first_failure.error or "Batch contained failed tool calls.",
            output=[r.output for r in results],
            exit_code=next((r.exit_code for r in results if r.exit_code != 0), 1),
            artifacts=[a for r in results for a in r.artifacts],
            metadata={"batch_size": len(results), "statuses": statuses},
            duration_ms=sum(r.duration_ms for r in results),
        )

    # All succeeded.
    return ToolExecutionResult.success(
        tool_name=results[0].tool_name,
        tool_call_id=results[0].tool_call_id,
        output=[r.output for r in results],
        artifacts=[a for r in results for a in r.artifacts],
        metadata={"batch_size": len(results), "statuses": statuses},
        duration_ms=sum(r.duration_ms for r in results),
    )


def merge_artifacts(results: List[ToolExecutionResult]) -> List[str]:
    """Return the deduplicated, order-preserving union of all artifacts."""
    seen: set = set()
    ordered: List[str] = []
    for r in results:
        for path in r.artifacts:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
    return ordered


__all__ = [
    "ToolExecutionOutcome",
    "ToolExecutionResult",
    "ToolExecutionStatus",
    "coalesce_results",
    "derive_outcome",
    "merge_artifacts",
]
