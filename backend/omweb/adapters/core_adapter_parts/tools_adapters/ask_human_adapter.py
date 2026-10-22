# backend/omweb/adapters/core_adapter_parts/tools_adapters/ask_human_adapter.py
"""
Human Interaction Adapter for PELDRUN Web-to-Core Layer.

Bridges the Web-layer ``ask_human`` (and alias ``human_input``) tool to
one of two execution paths:

  1. A caller-supplied ``human_input_callback`` (used by interactive
     Web sessions that suspend the agent loop and wait for user input).
  2. The canonical peldrun-core ``HumanInputTool`` when no callback is
     provided (used by programmatic runs that produce a structured
     ``HumanInputRequest`` for later resolution).

Served tool names:
  * ``ask_human``
  * ``human_input``

Factory signature:
  ``create_ask_human_executor(workspace_dir=None,
  human_input_callback=None, **kwargs)``
  returns an async callable ``(question, **kwargs) -> ToolResult``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from peldrun.tools.contract import ToolResult

from ._contract import AdapterSpec
from ._helpers import WorkspacePathType, _ensure_tool_result, _resolve_workspace_path

logger = logging.getLogger("omweb.adapters.tools_adapters.ask_human")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_ask_human_executor(
    workspace_dir: WorkspacePathType = None,
    human_input_callback: Optional[Callable[..., Awaitable[str]]] = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Callable[..., Awaitable[ToolResult]]:
    """
    Create an async executor for ``ask_human`` bound to
    ``workspace_dir``.

    If ``human_input_callback`` is provided, it is invoked directly
    with the question and any extra kwargs. Otherwise the canonical
    ``HumanInputTool`` is used, which emits a durable
    ``HumanInputRequest`` through the runtime.

    M7.8: ``settings`` is provided by ``resolve_tool_runtime`` when the
    adapter declares ``requires_context={"settings"}``. It carries
    ``default_timeout_seconds``, ``max_prompt_chars``, ``max_options``,
    and ``on_timeout`` from the ``ask_human`` manifest. The values are
    captured by the returned executor's closure and forwarded to
    ``HumanInputTool`` on each invocation (the tool is constructed
    per-call, not per-factory).

    The returned callable accepts ``question`` (or aliases ``prompt`` /
    ``query``) and returns a canonical ``ToolResult``.
    """
    ws_path = _resolve_workspace_path(workspace_dir)
    settings_snapshot: Dict[str, Any] = dict(settings or {})

    async def _execute_ask_human(
        question: str = "",
        **call_kwargs: Any,
    ) -> ToolResult:
        # Accept ``prompt`` and ``query`` as aliases for ``question``.
        prompt_q = (
            question
            or call_kwargs.pop("prompt", None)
            or call_kwargs.pop("query", None)
            or ""
        ).strip()
        if not prompt_q:
            return ToolResult(
                output="Ask Human Error: Question cannot be empty.",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "EMPTY_HUMAN_QUESTION"},
            )

        # Path 1: caller-supplied callback (interactive sessions).
        if callable(human_input_callback):
            try:
                ans = await human_input_callback(prompt_q, **call_kwargs)
                return ToolResult(
                    output=str(ans),
                    exit_code=0,
                    is_error=False,
                )
            except Exception as exc:
                return ToolResult(
                    output=f"Ask Human Error: {exc}",
                    exit_code=1,
                    is_error=True,
                    metadata={"error_code": "HUMAN_INPUT_CALLBACK_FAILED"},
                )

        # Path 2: canonical HumanInputTool (programmatic runs).
        try:
            from peldrun.tools.builtins.human_input import HumanInputTool

            tool = HumanInputTool(
                workspace_root=str(ws_path),
                settings=settings_snapshot,
            )
            res = await tool.aexecute(question=prompt_q, **call_kwargs)
            return _ensure_tool_result(
                res, default_error_code="HUMAN_INPUT_ERROR"
            )
        except Exception as exc:
            return ToolResult(
                output=f"Ask Human Error: {exc}",
                exit_code=1,
                is_error=True,
                metadata={"error_code": "HUMAN_INPUT_TOOL_FAILED"},
            )

    return _execute_ask_human


# ---------------------------------------------------------------------------
# Adapter declaration
# ---------------------------------------------------------------------------
#
# ``human_input_callback`` is intentionally NOT declared in
# ``requires_context``: it is an executor-construction concern, not a
# per-call context value. Callers (e.g. resolve_tool_runtime) construct
# the executor without it, and downstream ``HumanInputTool`` is used
# by default. Interactive callers may inject a callback by invoking
# this factory directly.

ADAPTER_SPEC = AdapterSpec(
    tool_names=("ask_human", "human_input"),
    factory=create_ask_human_executor,
    # M7.7: forward user settings (timeout, prompt cap, options cap,
    # on_timeout) so the HumanInputTool honors them at construction
    # time. Settings are captured by the closure and passed to each
    # per-call HumanInputTool instance.
    requires_context=frozenset({"settings"}),
)


__all__ = ["create_ask_human_executor", "ADAPTER_SPEC"]
