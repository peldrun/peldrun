# Path: backend/peldrun/tools/builtins/terminate.py
"""
PELDRUN Core Terminate Tool.
Signals successful or failed termination of the agent execution loop.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from peldrun.tools.base import BaseTool, ToolResult
from peldrun.tools.contract import ToolExecutionPolicy

logger = logging.getLogger("peldrun.tools.builtins.terminate")

# Detects the exact tool-call syntax that a confused model may
# accidentally place inside the ``message`` argument. The pattern
# matches (optionally with a leading "call ") "terminate(" or "done("
# at the very start of the string.
#
# This guard is intentionally narrow:
#   * It is anchored with ``^`` so a legitimate message like
#     "The task will terminate(...) once complete" is NOT rejected —
#     only a message that *starts* with the tool-call syntax is.
#   * It is case-insensitive so ``Terminate(`` and ``TERMINATE(`` are
#     caught.
#   * It permits optional leading whitespace so model output that
#     begins with a newline is still caught.
_TOOL_CALL_SYNTAX_RE = re.compile(
    r"^\s*(?:call\s+)?(?:terminate|done)\s*\(",
    re.IGNORECASE,
)


class TerminateArgs(BaseModel):
    """Pydantic schema defining arguments for TerminateTool."""
    status: str = Field(
        default="success",
        description=(
            "The final status of the task execution. Use 'success' only "
            "when the user's objective has been fully met. Use 'failure' "
            "when the task cannot be completed."
        ),
    )
    message: str = Field(
        default="",
        description=(
            "Summary or final message describing the result. Shown "
            "verbatim to the user as the task's closing message."
        ),
    )


# Backward-compatibility alias for any external code that imported it.
TerminateParameters = TerminateArgs


class TerminateTool(BaseTool):
    """
    Terminates the current agent execution loop.

    [B4 FIX] The pre-fix implementation ALWAYS returned ``exit_code=0``
    regardless of the ``status`` argument. Callers that inspected
    ``exit_code`` (the natural thing to do) saw "success" even for
    ``status="failure"``. The only place the failure intent was
    recorded was ``metadata["status"]``, which most callers ignored.
    See B4 in the PELDRUN execution repair plan.

    The post-fix behavior enforces the invariants that
    ``ToolExecutionResult`` (in ``peldrun.tools.execution_result``)
    already guarantees at the contract level:

        status == "success"  ⇔  exit_code == 0, is_error == False
        status == "failure"  ⇔  exit_code != 0, is_error == True

    Both the exit code AND the metadata now agree, so any caller
    that inspects either signal (or both) reaches the same
    conclusion.

    Additional safeguards
    ---------------------
    * ``args_schema = TerminateArgs`` is set so that
      ``BaseTool.aexecute`` validates arguments BEFORE invoking
      ``_arun`` and so that ``BaseTool.to_openai_schema`` produces
      the non-empty schema the model needs. The pre-fix class only
      set the class-level ``parameters`` dict, which
      ``to_openai_schema`` does NOT read — the schema advertised to
      the LLM was empty (``properties: {}``), so models could not
      see that ``status`` and ``message`` were accepted parameters.

    * Unrecognized ``status`` values (``"done"``, ``"failed"``,
      ``"partial"``, empty string, ...) are REJECTED with
      ``exit_code=2`` and ``metadata["terminated"] = False``. The
      termination evaluator (``peldrun/agents/termination.py``)
      refuses to end the run on a rejected terminate, so the model
      sees the error observation and can retry with a valid value.
      This is what closes the "agent hallucinated the tool name
      'done'" gap that B3 documented.

    * The ``error`` field on ``ToolResult`` now carries the failure
      message (previously it was always ``""`` even on failure), so
      callers that read ``error`` — such as
      ``ExecutionManager._convert_raw_to_execution_result`` — surface
      the reason correctly.
    """

    name: str = "terminate"
    description: str = (
        "Call this tool to terminate the current execution when the "
        "task is accomplished or cannot proceed. Use status='success' "
        "only when the user's objective has been fully met; use "
        "status='failure' when you cannot complete it."
    )
    args_schema = TerminateArgs
    parameters: Dict[str, Any] = TerminateArgs.model_json_schema()

    # Terminate is a terminal action: never retried internally, and
    # marked as having side effects (it drives the run's terminal
    # state via the termination evaluator — a side effect of the
    # first order). This matches the safe policy the agent uses as
    # its fallback in ``_get_tool_policy``.
    execution_policy: ToolExecutionPolicy = ToolExecutionPolicy(
        max_attempts=1, retryable=False, side_effects=True
    )

    #: Canonical valid values for the ``status`` argument. Anything
    #: else is rejected by ``_arun``.
    _VALID_STATUSES = frozenset({"success", "failure"})

    def __init__(
        self, workspace_root: Optional[str] = None, **kwargs: Any
    ):
        super().__init__(workspace_root=workspace_root)
        if self.workspace_root is None:
            self.workspace_root = os.getcwd()

    async def _arun(
        self,
        status: str = "success",
        message: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """
        Execute the termination action.

        Returns a ``ToolResult`` whose ``exit_code``, ``is_error``,
        ``error``, and ``metadata`` all agree with the requested
        ``status``. See the class docstring for the invariants.
        """
        # Normalize the incoming status. Missing/None → "success"
        # (matches the historical default). Whitespace and casing are
        # stripped so that ``"Success"`` and ``"SUCCESS"`` behave
        # identically to ``"success"``.
        raw_status = "" if status is None else str(status)
        normalized = raw_status.strip().lower() or "success"

        if normalized not in self._VALID_STATUSES:
            # Reject unknown statuses explicitly. The terminate
            # evaluator will treat this as a malformed call and NOT
            # end the run, forcing the model to retry.
            error_msg = (
                f"Invalid terminate status: {status!r}. "
                f"Must be 'success' or 'failure'."
            )
            return ToolResult(
                output=error_msg,
                error=error_msg,
                exit_code=2,
                is_error=True,
                metadata={
                    "status": "failure",
                    "terminated": False,
                    "error_code": "INVALID_STATUS",
                    "requested_status": raw_status,
                },
            )

        # Strip the message, using the default when empty.
        cleaned_message = (
            str(message).strip() if message is not None else ""
        )

        # Reject malformed messages that are literally the tool-call
        # syntax the model is supposed to be INVOKING, not copying
        # into its argument.
        #
        # Real-world failure mode:
        #   A small or confused model (or a model recovering from an
        #   INCOMPLETE_STREAM) sometimes emits:
        #
        #       terminate(status="success", message="
        #
        #   as the VALUE of the ``message`` parameter itself, instead
        #   of a human-readable summary. Passing that string straight
        #   through to the user surfaces garbage in the final answer
        #   bubble and visually wipes out the real content the model
        #   streamed earlier in the turn.
        #
        # Policy: discard the malformed value and fall through to the
        # default summary. This is non-destructive — the tool still
        # terminates with the requested status, so the lifecycle is
        # preserved; only the display text is sanitized.
        if cleaned_message and _TOOL_CALL_SYNTAX_RE.match(cleaned_message):
            logger.warning(
                "TerminateTool received tool-call syntax as the "
                "'message' argument (%r). Discarding it and using "
                "the default summary instead.",
                cleaned_message[:120],
            )
            cleaned_message = ""

        if normalized == "success":
            final_msg = cleaned_message or (
                "Task execution finished with status: success"
            )
            return ToolResult(
                output=final_msg,
                error="",
                exit_code=0,
                is_error=False,
                metadata={
                    "status": "success",
                    "terminated": True,
                },
            )

        # normalized == "failure"
        final_msg = cleaned_message or (
            "Task terminated with status: failure"
        )
        return ToolResult(
            output=final_msg,
            error=final_msg,
            exit_code=1,
            is_error=True,
            metadata={
                "status": "failure",
                "terminated": True,
            },
        )

    async def execute(
        self,
        status: str = "success",
        message: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """
        Async alias for ``_arun`` retained for backward compatibility.

        Pre-fix code called ``tool.execute(...)`` directly from
        ``ToolCollection.execute`` and from the agent's ``act()``
        fallback path. Those callers now prefer ``aexecute``
        (inherited from ``BaseTool``), but the alias is preserved so
        any external tooling that still calls ``execute`` does not
        suddenly receive a coroutine from a path that expected a
        concrete ``ToolResult``.
        """
        return await self._arun(
            status=status, message=message, **kwargs
        )


__all__ = [
    "TerminateArgs",
    "TerminateParameters",
    "TerminateTool",
]
