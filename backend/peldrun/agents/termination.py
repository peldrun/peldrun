# Path: backend/peldrun/agents/termination.py
"""
PELDRUN Core Termination Evaluator.

Centralizes the semantics of "did the model terminate the task, and
did it terminate successfully?" — the decision that every layer of
the runtime (ToolCallAgent.step, AgentRunner, CoreRuntimeExecutor)
depends on.

This module exists to fix B3 and B4 in the PELDRUN execution repair
plan:

  * B3 — ``ToolCallAgent.step`` decided completion by checking tool
    NAMES (``tc.name.lower() in ("terminate", "done")``). A failed
    ``terminate`` call still produced "COMPLETED". A model that
    hallucinated the tool name ``done`` still produced "COMPLETED".
    The framework had no way to verify that the terminate had
    actually run and reported success.

  * B4 — ``TerminateTool`` returned ``exit_code=0`` even for
    ``status="failure"``. Any downstream layer that only inspected
    ``exit_code`` (the natural implementation of ``step()``) would
    see success. The tool's own ``metadata["status"]`` argument was
    the only place the failure intent was recorded, and the caller
    ignored it.

Design invariants
-----------------

1. **Results, not names.**
   ``evaluate()`` accepts ``List[ToolExecutionResult]``. Every fact
   it needs comes from a result object — its ``tool_name``,
   ``status``, ``output``, and ``metadata``. Nothing about the
   original ``ToolCall`` name, and no heuristics like
   "``response.content`` contains the word 'done'".

2. **Metadata is the source of truth for termination intent.**
   ``TerminateTool`` records ``metadata["status"]`` ("success" /
   "failure") and ``metadata["terminated"]=True`` when it runs.
   ``evaluate()`` reads those fields — NOT ``result.status`` and
   NOT ``exit_code`` — because until Phase C is applied,
   ``TerminateTool`` still returns ``exit_code=0`` for failures.
   After Phase C the fields agree, and the metadata check remains
   authoritative without a second migration.

3. **Only ``terminate`` terminates.**
   ``TERMINATING_TOOL_NAMES`` contains exactly ``{"terminate"}``.
   The historical acceptance of ``"done"`` (B3) is gone — a tool
   named "done" is a regular tool, not a terminator. If it is not
   registered, calling it produces a normal "tool not found"
   failure and the task continues.

4. **One terminate per step, or none.**
   If the model emits multiple ``terminate`` calls in a single
   step, the evaluator rejects the batch. The task does not end.
   The runner will re-prompt the model (via the standard
   no-tool-call reminder path in ``ToolCallAgent.step``). This
   makes the "two terminates, contradictory messages" case
   deterministic instead of order-dependent.

5. **Terminate failure ≠ task failure.**
   Three distinct failure modes are possible:

     a. **Task failure** — model called ``terminate(status="failure")``.
        Intent recorded: task is over, outcome is FAILED. Evaluator
        returns ``should_terminate=True, is_success=False``.

     b. **Tool failure** — ``terminate`` could not run (missing
        registration, argument validation, internal exception).
        Intent NOT recorded (``metadata.terminated`` is absent).
        Evaluator returns ``should_terminate=False`` so the model
        can retry. If the model cannot fix it, the run eventually
        hits the step ceiling.

     c. **Interrupted terminate** — the tool was cancelled or timed
        out mid-execution. Evaluator returns ``should_terminate=False``.
        The cancellation subsystem (not this module) is responsible
        for transitioning the run.

6. **The evaluator does not touch state.**
   ``evaluate()`` is pure — it reads ``state`` only for diagnostic
   logging (run_id in log lines) and never mutates it. The caller
   applies the decision via ``apply_decision()``, which is the
   single place that transitions the FSM based on the decision.
   This separation is essential for Phase G2 (RecoveryManager needs
   to re-evaluate a persisted step without touching the live
   state).

7. **Case-insensitive tool names.**
   Providers occasionally preserve the model's casing
   (``"Terminate"``, ``"TERMINATE"``). ``is_terminating_tool()``
   normalizes to lowercase before the membership check, so both
   forms work.

8. **No side effects at import.**
   This module imports only from ``peldrun.tools.execution_result``,
   ``peldrun.engine.state``, and ``peldrun.engine.lifecycle`` — all
   pure modules with no runtime state. Import order issues (a
   circular import with ``tool_call_agent.py``, for instance) are
   structurally impossible.

See also
--------

    * ``backend/peldrun/tools/execution_result.py`` — the ToolExecutionResult
                                                       contract this module
                                                       consumes

    * ``backend/peldrun/agents/tool_call_agent.py``  — the caller that
                                                       invokes ``evaluate()``
                                                       from ``step()``

    * ``backend/peldrun/engine/lifecycle.py``        — the FSM authority
                                                       that ``apply_decision``
                                                       drives

    * ``backend/peldrun/tools/builtins/terminate.py`` — the tool whose
                                                        metadata this module
                                                        interprets
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from peldrun.engine.lifecycle import (
    is_terminal,
    mark_completed,
    mark_failed,
)
from peldrun.engine.state import ExecutionState
from peldrun.tools.execution_result import ToolExecutionResult

logger = logging.getLogger("peldrun.agents.termination")


# ---------------------------------------------------------------------------
# Canonical termination tool names
# ---------------------------------------------------------------------------
#
# This is the FULL set. There is no configurable extension mechanism:
# adding a new terminating tool name requires editing this constant
# and the corresponding tool implementation together, so that the
# metadata contract (``terminated`` + ``status``) is guaranteed to
# match. Silent drift — "the config added a name, but no tool was
# written to satisfy it" — is exactly the class of bug B3 described.
TERMINATING_TOOL_NAMES: frozenset = frozenset({"terminate"})


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminationDecision:
    """
    Result of evaluating a step's tool results for termination.

    Immutable so that it can safely be logged, compared in tests, and
    passed between layers without accidental mutation.

    Attributes
    ----------
    should_terminate
        True only when a valid ``terminate`` call was executed AND it
        recorded a clear intent (success or failure). False when no
        termination was requested, when the terminate call was
        malformed, or when it was interrupted.

    is_success
        Only meaningful when ``should_terminate`` is True. True when
        the model reported ``status="success"``; False when it
        reported ``status="failure"``.

    final_output
        Text to display as the run's final message, extracted from
        the terminate tool's output. ``None`` when the terminate
        produced no text or the decision is not a termination.

        NOTE: this is the terminate's suggested output. Callers may
        override with the model's own ``response.content`` when that
        is more appropriate (both are meaningful; the caller chooses
        precedence based on UX concerns).

    failure_reason
        Human-readable failure explanation. Only set when
        ``should_terminate=True`` and ``is_success=False``.

    terminating_call
        The specific ``ToolExecutionResult`` that triggered the
        decision. Always set when ``should_terminate=True``, and
        also set when ``should_terminate=False`` because of a
        rejected terminate (for diagnostics and audit).

    rejection_reason
        When ``should_terminate=False`` but a terminate call was
        present, this explains why it was rejected. ``None`` when
        no terminate call was present at all (the common
        "continue iterating" case).
    """

    should_terminate: bool = False
    is_success: bool = False
    final_output: Optional[str] = None
    failure_reason: Optional[str] = None
    terminating_call: Optional[ToolExecutionResult] = None
    rejection_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Convenience predicates
    # ------------------------------------------------------------------

    @property
    def was_rejected(self) -> bool:
        """True when a terminate was present but rejected by validation."""
        return self.rejection_reason is not None

    # ------------------------------------------------------------------
    # Human-readable summary (log lines, test failures)
    # ------------------------------------------------------------------

    def summary(self) -> str:
        if not self.should_terminate:
            if self.rejection_reason:
                return f"continue (terminate rejected: {self.rejection_reason})"
            return "continue"
        if self.is_success:
            return "terminate:success"
        return f"terminate:failure ({self.failure_reason or 'no reason given'})"


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def is_terminating_tool(tool_name: str) -> bool:
    """
    Return True if ``tool_name`` is a canonical terminating tool.

    Case-insensitive — providers that preserve the model's casing
    (``"Terminate"``) must still be recognized. ``None`` and empty
    strings are treated as non-terminating (defensive: the caller
    typically passes ``result.tool_name`` which is always a string,
    but a wrong-type caller should not crash the evaluator).
    """
    if not tool_name:
        return False
    return str(tool_name).strip().lower() in TERMINATING_TOOL_NAMES


# ---------------------------------------------------------------------------
# Output coercion
# ---------------------------------------------------------------------------


def _coerce_output_to_str(value: Any) -> Optional[str]:
    """
    Coerce a terminate tool's output to a string.

    Terminate outputs are always strings in the canonical
    implementation, but a plugin or a future tool variation might
    return a dict / list. This helper flattens non-string outputs to
    a stable representation so the caller never receives a
    non-string ``final_output``.

    Returns ``None`` when the output is empty / meaningless, so the
    caller can distinguish "no message" from "empty message".
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (dict, list)):
        # Best-effort: stringify. The terminate tool's actual output
        # is expected to be text; anything else is a bug in the tool
        # or a plugin. Logging the shape would be noise.
        return str(value) if value else None
    text = str(value)
    return text if text.strip() else None


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------


def _find_terminating_results(
    results: List[ToolExecutionResult],
) -> List[ToolExecutionResult]:
    """Return the subset of ``results`` whose tool_name is terminating."""
    if not results:
        return []
    return [r for r in results if is_terminating_tool(r.tool_name)]


def evaluate(
    results: List[ToolExecutionResult],
    state: Optional[ExecutionState] = None,
) -> TerminationDecision:
    """
    Evaluate a step's tool results and decide whether to terminate.

    This is the SOLE authority for the "should the task end?" question.
    ``ToolCallAgent.step()``, ``AgentRunner.run()``, and
    ``CoreRuntimeExecutor.execute()`` all consume its output. No
    other module checks tool names directly.

    Parameters
    ----------
    results
        The ``ToolExecutionResult`` objects produced by the current
        step's tool calls. May be empty (no tools invoked).

    state
        Optional. Used only for diagnostic logging (run_id in log
        lines). Never mutated. Kept in the signature so Phase G2 can
        pass a recovered state without changing the call shape.

    Returns
    -------
    TerminationDecision
        See the class docstring for field semantics.

    Behavior matrix
    ---------------

    ┌────────────────────────────────────────────┬──────────────────────┐
    │ Input                                      │ Decision             │
    ├────────────────────────────────────────────┼──────────────────────┤
    │ No results                                 │ continue             │
    │ No terminate result                        │ continue             │
    │ Two or more terminate results              │ continue + rejected  │
    │ Terminate result status=cancelled/timeout  │ continue + rejected  │
    │ Terminate without metadata.terminated=True │ continue + rejected  │
    │ Terminate with invalid metadata.status     │ continue + rejected  │
    │ Terminate metadata.status="success" but    │ continue + rejected  │
    │   the tool itself failed                   │                      │
    │ Terminate metadata.status="success" (ok)   │ terminate:success    │
    │ Terminate metadata.status="failure" (ok)   │ terminate:failure    │
    └────────────────────────────────────────────┴──────────────────────┘

    Note on the "continue + rejected" outcomes: the task does NOT
    end when a terminate is rejected. The runner continues the loop
    and the model sees the terminate failure in the observation
    history, prompting it to either correct the call or make more
    progress with other tools. This is the correct behavior — a
    malformed terminate must not silently succeed, and must not
    silently fail either; it must be visible to the model.
    """
    run_id = state.run_id if state is not None else "<no-state>"

    # Case 1 — no results, nothing to evaluate.
    if not results:
        return TerminationDecision(should_terminate=False)

    # Case 2 — no terminate tool was called.
    terminating = _find_terminating_results(results)
    if not terminating:
        return TerminationDecision(should_terminate=False)

    # Case 3 — multiple terminate calls in one step.
    #
    # Semantically ambiguous: which message is the final answer? The
    # safest resolution is to reject and let the model re-issue a
    # single, well-formed terminate on the next step. The rejection
    # is visible in the observations, so the model can react.
    if len(terminating) > 1:
        logger.warning(
            "[run=%s] Multiple terminate calls in one step (%d); "
            "rejecting termination.",
            run_id,
            len(terminating),
        )
        return TerminationDecision(
            should_terminate=False,
            rejection_reason=(
                f"Multiple terminate calls in one step "
                f"({len(terminating)}); only one is allowed per step."
            ),
        )

    call = terminating[0]

    # Case 4 — the terminate call was interrupted mid-execution.
    #
    # The cancellation subsystem is responsible for the run-level
    # state transition. The evaluator only reports that this
    # particular terminate call did not complete; the caller's
    # cancellation path (via ``is_cancelled(run_id)``) will decide
    # whether to end the loop.
    if call.status in ("cancelled", "timeout"):
        logger.info(
            "[run=%s] Terminate call was %s; not treating as a "
            "termination request.",
            run_id,
            call.status,
        )
        return TerminationDecision(
            should_terminate=False,
            rejection_reason=(
                f"Terminate call was {call.status}; it did not "
                f"complete cleanly."
            ),
            terminating_call=call,
        )

    # ------------------------------------------------------------------
    # Read the termination intent from metadata.
    #
    # ``TerminateTool`` is the only sanctioned producer of this
    # metadata; any other tool named "terminate" that omits it is
    # considered malformed. This is deliberately strict: the whole
    # point of B4's fix is that "the terminate succeeded" and "the
    # task succeeded" are TWO separate facts, and only the metadata
    # carries the second one.
    # ------------------------------------------------------------------
    terminated_marker = bool(call.metadata.get("terminated"))
    raw_status = call.metadata.get("status")
    requested_status = str(raw_status).strip().lower() if raw_status is not None else ""

    # Case 5 — the tool did not set ``metadata.terminated``.
    #
    # Either it is a custom tool named "terminate" that does not
    # follow the contract, or the call went through a framework path
    # that lost the metadata (which would itself be a bug). Reject
    # to force a clean retry.
    if not terminated_marker:
        logger.warning(
            "[run=%s] Terminate tool '%s' returned without setting "
            "metadata.terminated; rejecting.",
            run_id,
            call.tool_name,
        )
        return TerminationDecision(
            should_terminate=False,
            rejection_reason=(
                "Terminate returned without setting "
                "metadata.terminated=True. The terminate tool is "
                "misbehaving or a non-canonical tool with a "
                "reserved name is in the registry."
            ),
            terminating_call=call,
        )

    # Case 6 — terminated, but the recorded status is not recognized.
    if requested_status not in ("success", "failure"):
        logger.warning(
            "[run=%s] Terminate called with unrecognized status %r; "
            "rejecting.",
            run_id,
            raw_status,
        )
        return TerminationDecision(
            should_terminate=False,
            rejection_reason=(
                f"Terminate reported status={raw_status!r}; "
                f"valid values are 'success' and 'failure'."
            ),
            terminating_call=call,
        )

    # ------------------------------------------------------------------
    # Case 7 — the model claimed success, but the tool call itself
    # failed.
    #
    # This can only happen in a buggy terminate implementation (the
    # canonical one cannot return success intent + tool failure;
    # Phase C enforces the invariant). Reject so the caller does not
    # accidentally mark the run COMPLETED on top of a broken tool.
    # ------------------------------------------------------------------
    if requested_status == "success" and not call.is_success:
        logger.warning(
            "[run=%s] Terminate claimed status='success' but the "
            "tool call itself failed (%s); rejecting.",
            run_id,
            call.error or "no error message",
        )
        return TerminationDecision(
            should_terminate=False,
            rejection_reason=(
                f"Terminate claimed success but the tool call itself "
                f"failed: {call.error or 'no error message'}."
            ),
            terminating_call=call,
        )

    # ------------------------------------------------------------------
    # Case 8 — valid terminate: success.
    # ------------------------------------------------------------------
    if requested_status == "success":
        final_text = _coerce_output_to_str(call.output)
        logger.info(
            "[run=%s] Terminate:success accepted (output=%d chars).",
            run_id,
            len(final_text) if final_text else 0,
        )
        return TerminationDecision(
            should_terminate=True,
            is_success=True,
            final_output=final_text,
            terminating_call=call,
        )

    # ------------------------------------------------------------------
    # Case 9 — valid terminate: failure.
    #
    # The error is preferred over the output because ``error`` is the
    # conventional failure-message field. When the tool sets both
    # (output="...", error="specific reason"), the error is what a
    # developer debugging the run wants to see first.
    # ------------------------------------------------------------------
    failure_text = (
        (call.error.strip() if isinstance(call.error, str) and call.error.strip() else None)
        or _coerce_output_to_str(call.output)
        or "Task terminated with failure (no reason given)."
    )
    logger.info(
        "[run=%s] Terminate:failure accepted (reason=%r).",
        run_id,
        failure_text[:120],
    )
    return TerminationDecision(
        should_terminate=True,
        is_success=False,
        failure_reason=failure_text,
        terminating_call=call,
    )


# ---------------------------------------------------------------------------
# Apply decision to state
# ---------------------------------------------------------------------------


def apply_decision(
    state: ExecutionState,
    decision: TerminationDecision,
    *,
    default_success_output: Optional[str] = None,
) -> None:
    """
    Apply a ``TerminationDecision`` to an ``ExecutionState``.

    This is the SINGLE place where a termination decision is converted
    into an FSM transition. All callers (``ToolCallAgent.step``,
    ``AgentRunner.run``, ``CoreRuntimeExecutor.execute``) invoke this
    helper rather than calling ``mark_completed`` / ``mark_failed``
    directly, so Phase F's strict guards and Phase G2's recovery
    logic have exactly one integration point.

    Idempotent and non-destructive: if the state is already in a
    terminal status, no transition is attempted. If a decision is
    "continue" (``should_terminate=False``), no transition is
    attempted.

    Parameters
    ----------
    state
        The state to transition.

    decision
        The evaluation result. Typically produced by ``evaluate()``.

    default_success_output
        Optional fallback text to use when the decision is a success
        termination but the terminate tool produced no output. Callers
        that have a better fallback (e.g., the model's
        ``response.content``) pass it here so the state's
        ``final_output`` is never empty on a completed run.
    """
    if is_terminal(state):
        logger.debug(
            "[run=%s] apply_decision: state is already terminal "
            "('%s'); skipping.",
            state.run_id,
            state.status.value,
        )
        return

    if not decision.should_terminate:
        return

    if decision.is_success:
        output = decision.final_output or default_success_output
        mark_completed(
            state,
            output=output,
            reason="terminate:success",
        )
        return

    mark_failed(
        state,
        message=(
            decision.failure_reason
            or "Task terminated with failure (no reason given)."
        ),
        error_code="TERMINATE_FAILURE",
        reason="terminate:failure",
    )


# ---------------------------------------------------------------------------
# Convenience helpers for callers
# ---------------------------------------------------------------------------


def extract_terminating_result(
    results: List[ToolExecutionResult],
) -> Optional[ToolExecutionResult]:
    """
    Return the single ``terminate`` result from ``results``, or ``None``.

    Does NOT validate the result — a caller that needs only the raw
    call object (e.g., to log its arguments or extract artifacts) can
    use this without going through the full decision pipeline.

    Returns ``None`` when there are zero or multiple terminate
    results. Callers that need "the first one" in a multi-terminate
    scenario should call ``_find_terminating_results`` via the
    public ``evaluate`` API instead — the "multiple terminates"
    rejection is part of the decision, not this lookup.
    """
    terminating = _find_terminating_results(results)
    if len(terminating) != 1:
        return None
    return terminating[0]


__all__ = [
    "TERMINATING_TOOL_NAMES",
    "TerminationDecision",
    "apply_decision",
    "evaluate",
    "extract_terminating_result",
    "is_terminating_tool",
]
