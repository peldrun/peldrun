"""
PELDRUN Core ReAct Autonomous Agent Implementation.
Executes Think-Act cognitive cycles, extracts thought reasoning traces,
dispatches tool executions, and satisfies BaseAgent lifecycle contracts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Union
from pydantic import Field

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.events.emitter import EventEmitter
from peldrun.llm.providers.openai_compat import BaseLLMProvider
from peldrun.memory import MemoryManager
from peldrun.sandbox.base import BaseSandbox
from peldrun.tools.collection import ToolCollection
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.agents.react_agent")


class ReActAgentConfig(AgentConfig):
    """Configuration options governing ReAct reasoning and execution loops."""
    name: str = Field(default="react_agent", description="ReAct agent identifier")
    description: str = Field(
        default="Autonomous ReAct agent executing reason-and-act cycles.",
        description="Functional description"
    )


class ReActAgent(BaseAgent):
    """
    Autonomous ReAct Agent.
    Executes reasoning (Think) followed by concrete tool executions (Act)
    until goal fulfillment or step ceiling exhaustion.
    """

    def __init__(
        self,
        config: Optional[Union[AgentConfig, ReActAgentConfig]] = None,
        llm_provider: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        memory_manager: Optional[Any] = None,
        sandbox: Optional[BaseSandbox] = None,
        emitter: Optional[EventEmitter] = None,
        llm: Optional[Any] = None,
        tool_collection: Optional[Any] = None,
        workspace_dir: Optional[str] = None,
        **kwargs: Any
    ) -> None:
        """
        Initialize the ReActAgent with dual signature compatibility.
        Supports both modern BaseAgent parameters and legacy framework signatures.
        """
        resolved_config = config or ReActAgentConfig()
        if workspace_dir and not resolved_config.workspace_root:
            resolved_config.workspace_root = workspace_dir

        resolved_llm = llm_provider if llm_provider is not None else llm

        # Normalize tools container to ensure uniform interface
        resolved_tools: Optional[ToolRegistry] = None
        if isinstance(tool_registry, ToolRegistry):
            resolved_tools = tool_registry
        elif isinstance(tool_collection, ToolRegistry):
            resolved_tools = tool_collection
        elif tool_collection is not None:
            resolved_tools = ToolRegistry(
                workspace_root=resolved_config.workspace_root,
                tools=tool_collection.list_tools() if hasattr(tool_collection, "list_tools") else None,
            )

        super().__init__(
            config=resolved_config,
            llm_provider=resolved_llm,
            tool_registry=resolved_tools,
            memory_manager=memory_manager,
            sandbox=sandbox,
            emitter=emitter,
        )

        self.workspace_dir = workspace_dir or resolved_config.workspace_root
        self.tool_collection = tool_collection or (self.tools._collection if hasattr(self.tools, "_collection") else None)

    def set_system_prompt(self, prompt: str) -> None:
        """Update system instruction prompt."""
        self.config.system_prompt = prompt

    def _extract_reasoning_and_content(self, text: str) -> tuple[Optional[str], str]:
        """Extract chain-of-thought traces enclosed in <think> tags."""
        think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
        if think_match:
            thought = think_match.group(1).strip()
            clean_text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return thought if thought else None, clean_text
        return None, text.strip()

    async def _astep(self) -> bool:
        """
        Execute one ReAct reasoning and action cycle:
        1. Query LLM with conversation history and available tool schemas.
        2. Extract chain-of-thought traces and emit THOUGHT event.
        3. Dispatch tool calls and emit TOOL_CALL and OBSERVATION events.
        4. Conclude task when model returns final answer without tool calls.
        """
        if not self.llm:
            raise RuntimeError("LLM provider must be configured to execute ReActAgent steps.")

        budgeted_messages = self.memory.short_term.get_budgeted_messages()
        tool_schemas = self.tools.get_openai_schemas()

        # Query LLM provider
        response = await self.llm.generate(
            messages=budgeted_messages,
            tools=tool_schemas if tool_schemas else None,
        )

        raw_content = response.content or ""
        thought, clean_content = self._extract_reasoning_and_content(raw_content)

        # Fallback to model reasoning fields if available
        if not thought:
            thought = getattr(response, "thought", None) or getattr(response, "reasoning", None)

        if thought:
            await self.emitter.emit_thought(thought=thought)
            self.state.add_message("assistant", f"<think>{thought}</think>")

        # Handle tool execution phase
        if response.tool_calls:
            tool_calls_payload: List[Dict[str, Any]] = []

            for idx, tc in enumerate(response.tool_calls):
                if isinstance(tc, dict):
                    t_id = tc.get("id", f"call_{idx}")
                    func = tc.get("function", {})
                    t_name = func.get("name") or tc.get("name", "unknown_tool")
                    t_args_raw = func.get("arguments") or tc.get("arguments", "{}")
                else:
                    t_id = getattr(tc, "id", f"call_{idx}")
                    func = getattr(tc, "function", None)
                    t_name = getattr(func, "name", None) or getattr(tc, "name", "unknown_tool")
                    t_args_raw = getattr(func, "arguments", None) or getattr(tc, "arguments", "{}")

                if isinstance(t_args_raw, dict):
                    t_args = t_args_raw
                    t_args_str = json.dumps(t_args_raw, ensure_ascii=False)
                elif isinstance(t_args_raw, str):
                    t_args_str = t_args_raw
                    try:
                        t_args = json.loads(t_args_raw) if t_args_raw.strip() else {}
                    except Exception:
                        t_args = {"raw_input": t_args_raw}
                else:
                    t_args = {}
                    t_args_str = "{}"

                tool_calls_payload.append({
                    "id": t_id,
                    "type": "function",
                    "function": {"name": t_name, "arguments": t_args_str},
                })

                await self.emitter.emit_tool_call(tool_name=t_name, arguments=t_args, tool_call_id=t_id)

                # Execute tool via ToolRegistry
                result = await self.tools.aexecute(t_name, **t_args)

                self.state.record_tool_execution(
                    tool_name=t_name,
                    arguments=t_args,
                    output=result.output,
                    exit_code=result.exit_code,
                    is_error=result.is_error,
                    artifacts=result.artifacts,
                    tool_call_id=t_id,
                )

                await self.emitter.emit_observation(
                    output=result.output,
                    tool_name=t_name,
                    exit_code=result.exit_code,
                    is_error=result.is_error,
                    artifacts=result.artifacts,
                    tool_call_id=t_id,
                )

                obs_str = result.output if isinstance(result.output, str) else json.dumps(result.output, ensure_ascii=False)
                self.memory.short_term.add_message(
                    role="tool",
                    content=obs_str,
                    name=t_name,
                    tool_call_id=t_id,
                )

            # Record assistant turn with tool calls into dialogue history
            self.memory.short_term.add_message(
                role="assistant",
                content=clean_content if clean_content else None,
                tool_calls=tool_calls_payload,
            )
            return True

        # Final answer reached when model proposes no tool calls
        final_text = clean_content if clean_content else raw_content
        if final_text:
            self.state.add_message("assistant", final_text)
            self.memory.short_term.add_message("assistant", final_text)
            self.state.mark_completed(output=final_text)
            return False

        self.state.mark_completed(output="Task completed.")
        return False


__all__ = [
    "ReActAgentConfig",
    "ReActAgent",
]