"""
backend/peldrun/agents/tool_call_agent.py

Autonomous Tool Calling Agent for PELDRUN Core Runtime.
Implements the StepExecutableAgent protocol driving atomic reasoning-action steps.
Hardened under PR 2 (Single Execution Authority):
- Removes internal standalone execution loops; delegates run_task() and arun() to AgentRunner.
- Completely removes fake synthetic 'terminate' fallback on LLM failure, ensuring genuine error propagation.
- Enforces strict canonical tool_call_id generation.
- Emits atomic THINK and ACT cycles strictly per step.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import uuid

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.engine.state import ExecutionState, MessageRole
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.llm.client import LLMResponse, ToolCall
from peldrun.tools.base import ToolResult
from peldrun.tools.collection import ToolCollection

logger = logging.getLogger("peldrun.agents.tool_call_agent")


class ToolCallAgent(BaseAgent):
    """
    Autonomous ReAct agent executing real tool invocations on concrete tool instances.
    Maintains full multi-turn conversational state synchronized with ExecutionState
    and satisfies the StepExecutableAgent protocol contract for AgentRunner.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm: Optional[Any] = None,
        tool_collection: Optional[Any] = None,
        emitter: Optional[EventEmitter] = None,
        workspace_dir: Optional[Union[str, Path]] = None,
        tool_registry: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        active_tools = tool_registry if tool_registry is not None else (tool_collection or ToolCollection())

        base_params = inspect.signature(BaseAgent.__init__).parameters
        base_kwargs: Dict[str, Any] = {}

        if "config" in base_params:
            base_kwargs["config"] = config
        if "tool_registry" in base_params:
            base_kwargs["tool_registry"] = active_tools
        elif "tools" in base_params:
            base_kwargs["tools"] = active_tools

        if "emitter" in base_params and emitter is not None:
            base_kwargs["emitter"] = emitter
        elif "event_emitter" in base_params and emitter is not None:
            base_kwargs["event_emitter"] = emitter

        if "workspace_dir" in base_params and workspace_dir is not None:
            base_kwargs["workspace_dir"] = str(workspace_dir)

        for k, v in kwargs.items():
            if k in base_params and k not in base_kwargs and k != "llm":
                base_kwargs[k] = v

        super().__init__(**base_kwargs)

        self._name: str = getattr(config, "name", "ToolCallAgent") if config else "ToolCallAgent"
        if hasattr(self, "config") and self.config and not getattr(self.config, "name", None):
            self.config.name = self._name

        self.llm = llm
        self.tool_registry = tool_registry
        self.tool_collection = active_tools
        self.tools = active_tools
        self.emitter = emitter or getattr(self, "emitter", None) or EventEmitter()
        self.workspace_dir = Path(workspace_dir).resolve() if workspace_dir else Path.cwd()
        self.system_prompt = getattr(config, "system_prompt", "You are an autonomous specialist agent.")
        self.messages: List[Dict[str, Any]] = []
        self.current_step = 1
        self._final_answer: str = ""
        self.state: Optional[ExecutionState] = None

    @property
    def name(self) -> str:
        """Return agent identifier name from config or internal attribute."""
        if hasattr(self, "config") and self.config and getattr(self.config, "name", None):
            return self.config.name
        return getattr(self, "_name", self.__class__.__name__)

    @name.setter
    def name(self, value: str) -> None:
        """Set agent identifier name, synchronizing with config if present."""
        self._name = value
        if hasattr(self, "config") and self.config:
            self.config.name = value

    def set_system_prompt(self, prompt: str) -> None:
        """Configure or update the system prompt controlling agent reasoning."""
        self.system_prompt = prompt

    async def _emit(self, event_type: EventType, step: int, payload: Dict[str, Any]) -> None:
        """Dispatch typed events safely to the registered EventEmitter."""
        if not self.emitter:
            return
        try:
            event_obj = PeldrunEvent(type=event_type, step=step, payload=payload)
            res = self.emitter.emit(event_obj)
            if inspect.isawaitable(res):
                await res
        except Exception:
            try:
                res = self.emitter.emit(event_type, step=step, payload=payload)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

    def _get_tools_schema(self) -> List[Dict[str, Any]]:
        """Extract function calling schemas from active tools container."""
        tools_obj = getattr(self, "tool_registry", None) or getattr(self, "tool_collection", None) or getattr(self, "tools", None)
        if tools_obj is None:
            return []

        if hasattr(tools_obj, "get_openai_schemas") and callable(tools_obj.get_openai_schemas):
            return tools_obj.get_openai_schemas()
        elif hasattr(tools_obj, "to_openai_schemas") and callable(tools_obj.to_openai_schemas):
            return tools_obj.to_openai_schemas()
        elif hasattr(tools_obj, "to_params") and callable(tools_obj.to_params):
            return tools_obj.to_params()
        elif hasattr(tools_obj, "tools") and isinstance(tools_obj.tools, dict):
            schemas: List[Dict[str, Any]] = []
            for t in tools_obj.tools.values():
                if hasattr(t, "to_openai_schema") and callable(t.to_openai_schema):
                    schemas.append(t.to_openai_schema())
                elif hasattr(t, "to_param") and callable(t.to_param):
                    schemas.append(t.to_param())
                elif hasattr(t, "parameters"):
                    schemas.append({
                        "type": "function",
                        "function": {
                            "name": getattr(t, "name", str(t)),
                            "description": getattr(t, "description", ""),
                            "parameters": getattr(t, "parameters", {}),
                        },
                    })
            return schemas
        return []

    def _resolve_tool_instance(self, tool_name: str) -> Optional[Any]:
        """Locate concrete tool instance from registry or collection."""
        tools_obj = getattr(self, "tool_registry", None) or getattr(self, "tool_collection", None) or getattr(self, "tools", None)
        if not tools_obj:
            return None

        if hasattr(tools_obj, "get") and callable(tools_obj.get):
            try:
                t = tools_obj.get(tool_name)
                if t:
                    return t
            except Exception:
                pass

        if hasattr(tools_obj, "get_tool") and callable(tools_obj.get_tool):
            t = tools_obj.get_tool(tool_name)
            if t:
                return t

        if hasattr(tools_obj, "tool_map") and isinstance(tools_obj.tool_map, dict):
            if tool_name in tools_obj.tool_map:
                return tools_obj.tool_map[tool_name]

        if hasattr(tools_obj, "tools") and isinstance(tools_obj.tools, dict):
            if tool_name in tools_obj.tools:
                return tools_obj.tools[tool_name]

        if hasattr(tools_obj, "tools") and isinstance(tools_obj.tools, (list, tuple)):
            for t in tools_obj.tools:
                if getattr(t, "name", None) == tool_name:
                    return t

        return None

    def _sync_messages_from_state(self, state: ExecutionState) -> None:
        """Synchronize in-memory message history from ExecutionState messages."""
        if state.messages:
            self.messages = [msg.to_llm_dict() for msg in state.messages]
        else:
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": state.task_prompt or "Start task."},
            ]
            state.add_message(role=MessageRole.SYSTEM, content=self.system_prompt)
            state.add_message(role=MessageRole.USER, content=state.task_prompt or "Start task.")

    async def think(self, step: int) -> LLMResponse:
        """Execute cognitive reasoning phase of the ReAct cycle."""
        tools_schema = self._get_tools_schema()

        if not self.messages or not any(m.get("role") in ("user", "system") for m in self.messages):
            prompt_text = (self.state.task_prompt if self.state else "") or "Execute assigned task."
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt_text},
            ]
            if self.state and not self.state.messages:
                self.state.add_message(role=MessageRole.SYSTEM, content=self.system_prompt)
                self.state.add_message(role=MessageRole.USER, content=prompt_text)

        async def _call_llm(msgs: List[Dict[str, Any]]) -> LLMResponse:
            if hasattr(self.llm, "generate"):
                return await self.llm.generate(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    temperature=0.2,
                )
            elif hasattr(self.llm, "chat_completion"):
                return await self.llm.chat_completion(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    temperature=0.2,
                )
            elif hasattr(self.llm, "chat_complete"):
                return await self.llm.chat_complete(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    temperature=0.2,
                )
            else:
                raise AttributeError("LLM client does not provide generate or chat_completion interface.")

        try:
            response = await _call_llm(self.messages)
        except Exception as exc:
            err_str = str(exc).lower()
            if "terminated" in err_str or "context" in err_str or "400" in err_str:
                logger.warning("Local provider rejected full context (%s). Retrying with compacted context.", exc)
                compact_msgs = list(self.messages[:2]) + list(self.messages[-6:])
                while compact_msgs and len(compact_msgs) > 2 and compact_msgs[2].get("role") == "tool":
                    compact_msgs.pop(2)

                if not compact_msgs or not any(m.get("role") == "user" for m in compact_msgs):
                    compact_msgs = [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": (self.state.task_prompt if self.state else "") or "Proceed with task."},
                    ]

                # Genuine LLM retry with compacted context; no synthetic termination on secondary failure
                response = await _call_llm(compact_msgs)
            else:
                raise

        thought_val = response.reasoning or response.thought or response.content or ""
        if thought_val:
            await self._emit(EventType.THOUGHT, step=step, payload={"thought": thought_val})

        return response

    async def act(self, step: int, tool_calls: List[ToolCall]) -> List[str]:
        """Execute proposed tool calls and record observations into state and message log."""
        observations: List[str] = []

        for call in tool_calls:
            tool_name = call.name
            call_id = call.id or f"call_{uuid.uuid4().hex[:12]}"
            call.id = call_id

            args = call.arguments if isinstance(call.arguments, dict) else {}
            if isinstance(call.arguments, str):
                try:
                    args = json.loads(call.arguments)
                except Exception:
                    args = {"raw": call.arguments}

            is_human_tool = tool_name in ("ask_human", "human_input")
            if is_human_tool:
                if not args.get("request_id"):
                    args["request_id"] = str(uuid.uuid4())
                if self.state is not None:
                    self.state.mark_waiting_for_input(reason="Agent invoked interactive human tool")

            await self._emit(
                EventType.TOOL_CALL,
                step=step,
                payload={"tool_name": tool_name, "arguments": args, "tool_call_id": call_id},
            )

            output_str = ""
            tool_inst = self._resolve_tool_instance(tool_name)

            if tool_inst is not None:
                try:
                    res: Any
                    if hasattr(tool_inst, "aexecute") and callable(tool_inst.aexecute):
                        res = await tool_inst.aexecute(**args)
                    elif hasattr(tool_inst, "_arun") and callable(tool_inst._arun):
                        res = await tool_inst._arun(**args)
                    elif hasattr(tool_inst, "execute") and callable(tool_inst.execute):
                        fn = tool_inst.execute
                        if inspect.iscoroutinefunction(fn):
                            res = await fn(**args)
                        else:
                            res = fn(**args)
                            if inspect.isawaitable(res):
                                res = await res
                    else:
                        res = ToolResult(
                            output=f"Tool '{tool_name}' has no executable entrypoint.",
                            exit_code=1,
                            is_error=True,
                        )

                    if is_human_tool and self.state is not None:
                        self.state.mark_running(reason="Human response received and processed")

                    if isinstance(res, ToolResult):
                        if res.is_error:
                            output_str = res.error or str(res.output) or f"Error executing tool '{tool_name}'"
                        else:
                            output_str = str(res.output) if res.output is not None else ""
                    elif isinstance(res, dict) and "output" in res:
                        output_str = str(res.get("output", ""))
                    else:
                        output_str = str(res)

                except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError) as flow_err:
                    if is_human_tool and self.state is not None:
                        self.state.mark_failed(f"Human interaction aborted: {flow_err}", error_code="FLOW_ABORTED")
                    raise flow_err

                except Exception as ex:
                    if is_human_tool and self.state is not None:
                        self.state.mark_running(reason="Error handled during human interaction")
                    output_str = f"Error executing tool '{tool_name}': {str(ex)}"
            else:
                if is_human_tool and self.state is not None:
                    self.state.mark_running(reason="Missing human tool handled")
                output_str = f"Error: Tool '{tool_name}' not found in active collection."

            await self._emit(
                EventType.OBSERVATION,
                step=step,
                payload={"tool_name": tool_name, "output": output_str, "tool_call_id": call_id},
            )

            self.messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": tool_name,
                "content": output_str,
            })

            if self.state is not None:
                self.state.add_message(
                    role=MessageRole.TOOL,
                    content=output_str,
                    name=tool_name,
                    tool_call_id=call_id,
                )
                self.state.record_tool_execution(
                    tool_name=tool_name,
                    arguments=args,
                    output=output_str,
                    tool_call_id=call_id,
                )

            observations.append(output_str)

        return observations

    async def step(self, state: ExecutionState, emitter: EventEmitter) -> bool:
        """
        Execute a single ReAct step driving the AgentRunner lifecycle contract.
        Returns True when the task has concluded, False to request the next iteration.
        """
        self.state = state
        self.emitter = emitter
        self.current_step = state.current_step

        self._sync_messages_from_state(state)

        response = await self.think(step=self.current_step)

        # Stop condition 1: No tool calls emitted
        if not response.tool_calls:
            self._final_answer = response.content or response.thought or response.reasoning or "Task completed."
            state.final_output = self._final_answer
            return True

        # Pre-assign guaranteed non-empty call IDs to satisfy strict schema validators
        for tc in response.tool_calls:
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:12]}"

        tool_calls_payload = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False) if isinstance(tc.arguments, dict) else str(tc.arguments),
                },
            }
            for tc in response.tool_calls
        ]
        self.messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": tool_calls_payload,
        })
        state.add_message(
            role=MessageRole.ASSISTANT,
            content=response.content or "",
            tool_calls=tool_calls_payload,
        )

        observations = await self.act(step=self.current_step, tool_calls=response.tool_calls)

        # Stop condition 2: Terminate tool invoked
        has_terminated = any(tc.name.lower() in ("terminate", "done") for tc in response.tool_calls)
        if has_terminated:
            self._final_answer = response.content or (observations[-1] if observations else "Task completed via termination tool.")
            state.final_output = self._final_answer
            return True

        return False

    async def _astep(self) -> bool:
        """Internal step delegate for backward compatibility."""
        if self.state is None:
            self.state = ExecutionState(task_prompt="Default task", workspace_root=str(self.workspace_dir))
        return not (await self.step(state=self.state, emitter=self.emitter))

    async def run_task(self, prompt: str, max_steps: int = 30) -> str:
        """
        Compatibility shim delegating execution directly to canonical AgentRunner.
        Ensures all executions pass through the unified runtime authority.
        """
        from peldrun.engine.runner import AgentRunner, RunnerConfig

        runner_config = RunnerConfig(max_steps=max_steps)
        runner = AgentRunner(agent=self, emitter=self.emitter, config=runner_config)
        state = await runner.run(task_prompt=prompt, workspace_root=str(self.workspace_dir))
        return state.final_output or self._final_answer or "Task execution finished."

    async def arun(self, task: str = "", max_steps: Optional[int] = None, **kwargs: Any) -> Any:
        """Compatibility entrypoint delegating to run_task which executes via AgentRunner."""
        prompt_val = task or kwargs.get("prompt", "")
        limit_val = max_steps or getattr(self.config, "max_steps", 30)
        return await self.run_task(prompt=prompt_val, max_steps=limit_val)


__all__ = ["ToolCallAgent"]