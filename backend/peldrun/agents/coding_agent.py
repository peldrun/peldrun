"""
PELDRUN Core Coding Autonomous Agent Implementation.
Specialized software engineering agent capable of workspace inspection, code authoring,
automated test execution, error diagnostics, and iterative self-repair.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from pydantic import Field

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.events.emitter import EventEmitter
from peldrun.llm.client import DeltaToolCall
from peldrun.llm.providers.openai_compat import BaseLLMProvider
from peldrun.memory import MemoryManager
from peldrun.sandbox.base import BaseSandbox
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.agents.coding_agent")


class CodingAgentConfig(AgentConfig):
    """Configuration options governing software engineering agent behaviors."""
    name: str = Field(default="coding_agent", description="Coding agent identifier")
    description: str = Field(
        default="Software engineering agent specialized in reading, writing, testing, and debugging source code.",
        description="Functional description"
    )
    max_repair_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum consecutive retry attempts to automatically fix runtime/syntax errors"
    )
    auto_verify_tests: bool = Field(
        default=True,
        description="Automatically execute tests or code execution verification after modifications"
    )


class CodingAgent(BaseAgent):
    """
    Autonomous Coding Agent.
    Orchestrates repository exploration, file modifications, command execution,
    diagnostic evaluation, and iterative code refinement.
    """

    def __init__(
        self,
        config: Optional[CodingAgentConfig] = None,
        llm_provider: Optional[BaseLLMProvider] = None,
        tool_registry: Optional[ToolRegistry] = None,
        memory_manager: Optional[MemoryManager] = None,
        sandbox: Optional[BaseSandbox] = None,
        emitter: Optional[EventEmitter] = None,
    ) -> None:
        super().__init__(
            config=config or CodingAgentConfig(),
            llm_provider=llm_provider,
            tool_registry=tool_registry,
            memory_manager=memory_manager,
            sandbox=sandbox,
            emitter=emitter,
        )
        self._consecutive_errors: int = 0
        self._modified_artifacts: set[str] = set()

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
        Execute one coding agent cycle:
        1. Query LLM with current dialogue turns and registered developer tools.
        2. Stream tokens, extracting reasoning thought traces.
        3. Dispatch tool invocations (file ops, shell execution, testing).
        4. Capture errors and trigger self-repair feedback loops if needed.
        5. Conclude when deliverables are verified.
        """
        if not self.llm:
            raise RuntimeError("LLM provider must be configured to execute CodingAgent steps.")

        budgeted_messages = self.memory.short_term.get_budgeted_messages()
        tool_schemas = self.tools.get_openai_schemas()

        content_buffer = ""
        tool_calls_accumulator: Dict[int, Dict[str, Any]] = {}

        logger.debug("CodingAgent step %d executing with %d available tools.", self.state.step, len(tool_schemas))

        # 1. Stream response from LLM provider
        async for chunk in self.llm.stream(
            messages=budgeted_messages,
            tools=tool_schemas if tool_schemas else None,
        ):
            chunk_content = getattr(chunk, "content", None) or getattr(chunk, "content_delta", None)
            if chunk_content:
                content_buffer += chunk_content

            chunk_tool_calls = getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_deltas", None)
            if chunk_tool_calls:
                for dtc in chunk_tool_calls:
                    idx = getattr(dtc, "index", 0)
                    dtc_id = getattr(dtc, "id", None)
                    dtc_name = getattr(dtc, "name", None)
                    dtc_args = getattr(dtc, "arguments", None)

                    if idx not in tool_calls_accumulator:
                        tool_calls_accumulator[idx] = {
                            "id": dtc_id or f"code_call_{idx}",
                            "name": dtc_name or "",
                            "arguments": dtc_args or "",
                        }
                    else:
                        if dtc_id:
                            tool_calls_accumulator[idx]["id"] = dtc_id
                        if dtc_name:
                            tool_calls_accumulator[idx]["name"] += dtc_name
                        if dtc_args:
                            tool_calls_accumulator[idx]["arguments"] += dtc_args

        # 2. Extract and broadcast reasoning
        thought, clean_content = self._extract_reasoning_and_content(content_buffer)
        if thought:
            await self.emitter.emit_thought(thought=thought)
            self.state.add_message("assistant", f"<think>{thought}</think>")

        # 3. Handle Tool Calls
        if tool_calls_accumulator:
            tool_calls_payload: List[Dict[str, Any]] = []
            has_error_in_turn = False

            for idx in sorted(tool_calls_accumulator.keys()):
                item = tool_calls_accumulator[idx]
                t_id = item["id"]
                t_name = item["name"]
                t_args_raw = item["arguments"]

                try:
                    t_args = json.loads(t_args_raw) if t_args_raw.strip() else {}
                except Exception:
                    t_args = {"raw_input": t_args_raw}

                tool_calls_payload.append({
                    "id": t_id,
                    "type": "function",
                    "function": {"name": t_name, "arguments": t_args_raw},
                })

                await self.emitter.emit_tool_call(tool_name=t_name, arguments=t_args, tool_call_id=t_id)

                # Execute developer tool
                result = await self.tools.aexecute(t_name, **t_args)

                # Track artifacts
                if result.artifacts:
                    for art in result.artifacts:
                        self._modified_artifacts.add(art)

                if result.is_error:
                    has_error_in_turn = True

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

            # Record turn in short-term buffer
            self.memory.short_term.add_message(
                role="assistant",
                content=clean_content if clean_content else None,
                tool_calls=tool_calls_payload,
            )

            # Update self-repair counter
            if has_error_in_turn:
                self._consecutive_errors += 1
                max_repair = getattr(self.config, "max_repair_attempts", 3)
                if self._consecutive_errors >= max_repair:
                    logger.warning("CodingAgent exceeded consecutive error threshold (%d).", max_repair)
                    await self.emitter.emit_thought(
                        thought=f"Encountered repeated execution failures ({self._consecutive_errors}). Requesting diagnosis."
                    )
            else:
                self._consecutive_errors = 0

            return True

        # 4. Final output synthesis
        if clean_content:
            self.state.add_message("assistant", clean_content)
            self.memory.short_term.add_message("assistant", clean_content)

            # Attach collected artifacts to final state
            if self._modified_artifacts:
                self.state.metadata["modified_artifacts"] = sorted(list(self._modified_artifacts))

            self.state.mark_completed(output=clean_content)
            return False

        logger.warning("CodingAgent received empty turn with no actions.")
        self.state.mark_completed(output="Code generation and workspace modifications concluded.")
        return False