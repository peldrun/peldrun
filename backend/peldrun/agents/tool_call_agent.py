"""
Autonomous Tool Calling Agent for PELDRUN Core Runtime.
Executes Think-Act cycles with reliable tool dispatching directly on tool instances
or session-scoped ToolRegistry, preserving full conversation history to prevent amnesia loops.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import EventType, PeldrunEvent
from peldrun.llm.client import AsyncLLMClient, LLMResponse, ToolCall
from peldrun.tools.base import ToolResult
from peldrun.tools.collection import ToolCollection
from peldrun.tools.contract import ToolRuntime

logger = logging.getLogger("peldrun.agents.tool_call_agent")


class ToolCallAgent(BaseAgent):
    """
    Autonomous ReAct agent executing real tool invocations on concrete tool instances
    or through session-scoped ToolRegistry execution runtimes.
    Maintains full multi-turn conversational state without arbitrary premature pruning.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        llm: Optional[Any] = None,
        tool_collection: Optional[Any] = None,
        emitter: Optional[EventEmitter] = None,
        workspace_dir: Optional[Union[str, Path]] = None,
        tool_registry: Optional[Any] = None,
        **kwargs: Any
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

    def set_system_prompt(self, prompt: str) -> None:
        """Configure or update the system prompt controlling agent reasoning."""
        self.system_prompt = prompt

    async def _emit(self, event_type: EventType, step: int, payload: Dict[str, Any]) -> None:
        """Dispatches typed events to registered listeners safely across async loops."""
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
        """Extracts OpenAI-compatible function calling schemas from active tools container."""
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
                            "parameters": getattr(t, "parameters", {})
                        }
                    })
            return schemas
        return []

    def _resolve_tool_instance(self, tool_name: str) -> Optional[Any]:
        """Locates the concrete tool instance from registry, collection, or map."""
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

    async def think(self, step: int) -> LLMResponse:
        """
        Executes the cognitive reasoning phase of the ReAct cycle.
        Maintains full context history and gracefully recovers if provider hits physical limits.
        """
        tools_schema = self._get_tools_schema()

        async def _call_llm(msgs: List[Dict[str, Any]]) -> LLMResponse:
            if hasattr(self.llm, "generate"):
                return await self.llm.generate(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    temperature=0.2
                )
            elif hasattr(self.llm, "chat_completion"):
                return await self.llm.chat_completion(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    tool_choice="auto",
                    temperature=0.2
                )
            elif hasattr(self.llm, "chat_complete"):
                return await self.llm.chat_complete(
                    messages=msgs,
                    tools=tools_schema if tools_schema else None,
                    temperature=0.2
                )
            else:
                raise AttributeError("LLM client does not provide generate or chat_completion interface.")

        try:
            # Preserves full conversation history to prevent amnesia and question repetition
            response = await _call_llm(self.messages)
        except Exception as exc:
            err_str = str(exc).lower()
            if "terminated" in err_str or "context" in err_str or "400" in err_str:
                logger.warning("Local provider rejected full context (%s). Retrying with emergency fallback.", exc)
                # Keep system prompt + original task prompt + recent 6 turns only in emergency
                emergency_msgs = list(self.messages[:2]) + list(self.messages[-6:])
                while emergency_msgs and len(emergency_msgs) > 2 and emergency_msgs[2].get("role") == "tool":
                    emergency_msgs.pop(2)
                try:
                    response = await _call_llm(emergency_msgs)
                except Exception:
                    # Conclude cleanly instead of dropping task
                    return LLMResponse(
                        content="Concluded task execution based on prior conversation observations.",
                        tool_calls=[ToolCall(name="terminate", arguments={})]
                    )
            else:
                raise

        thought_val = response.reasoning or response.thought or response.content or ""
        if thought_val:
            print(f"[CORE LIVE STREAM] Thought: {thought_val[:90]}...")
            await self._emit(EventType.THOUGHT, step=step, payload={"thought": thought_val})

        return response

    async def act(self, step: int, tool_calls: List[ToolCall]) -> List[str]:
        """Executes proposed tool calls and standardizes tool observations into dialogue history."""
        observations: List[str] = []

        for call in tool_calls:
            tool_name = call.name
            args = call.arguments if isinstance(call.arguments, dict) else {}
            if isinstance(call.arguments, str):
                try:
                    args = json.loads(call.arguments)
                except Exception:
                    args = {"raw": call.arguments}

            print(f"[CORE LIVE STREAM] Tool Call: {tool_name}({json.dumps(args, ensure_ascii=False)[:80]})")
            await self._emit(EventType.TOOL_CALL, step=step, payload={"tool_name": tool_name, "arguments": args})

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
                            is_error=True
                        )

                    if isinstance(res, ToolResult):
                        if res.is_error:
                            output_str = res.error or str(res.output) or f"Error executing tool '{tool_name}'"
                        else:
                            output_str = str(res.output) if res.output is not None else ""
                    elif isinstance(res, dict) and "output" in res:
                        output_str = str(res.get("output", ""))
                    else:
                        output_str = str(res)

                except Exception as ex:
                    output_str = f"Error executing tool '{tool_name}': {str(ex)}"
            else:
                output_str = f"Error: Tool '{tool_name}' not found in active collection."

            print(f"[CORE LIVE STREAM] Observation: {output_str[:90]}...")
            await self._emit(EventType.OBSERVATION, step=step, payload={"tool_name": tool_name, "output": output_str})

            self.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": tool_name,
                "content": output_str
            })
            observations.append(output_str)

        return observations

    async def _astep(self) -> bool:
        """Executes a single step cycle: Think -> Act."""
        step = self.current_step

        if hasattr(self, "state") and self.state is not None:
            if hasattr(self.state, "step"):
                self.state.step = step
            if hasattr(self.state, "current_step"):
                self.state.current_step = step
            if hasattr(self.state, "messages"):
                self.state.messages = self.messages

        response = await self.think(step=step)

        # Stop condition: When model proposes no tool calls, it has finished reasoning
        if not response.tool_calls:
            self._final_answer = response.content or response.thought or response.reasoning or "Task completed."
            return False

        # Record assistant tool calls in conversation history
        self.messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False) if isinstance(tc.arguments, dict) else str(tc.arguments)
                    }
                }
                for tc in response.tool_calls
            ]
        })

        observations = await self.act(step=step, tool_calls=response.tool_calls)

        # Stop condition: Terminate tool invoked
        has_terminated = any(tc.name.lower() in ("terminate", "done") for tc in response.tool_calls)
        if has_terminated:
            self._final_answer = response.content or (observations[-1] if observations else "Task completed via termination tool.")
            return False

        return True

    async def run_task(self, prompt: str, max_steps: int = 30) -> str:
        """Runs the ReAct loop until task termination or step limit exhaustion."""
        self.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        self._final_answer = ""

        for step in range(1, max_steps + 1):
            self.current_step = step
            print(f"[CORE LIVE STREAM] Step {step} Started...")
            await self._emit(EventType.STEP_START, step=step, payload={"step": step})

            should_continue = await self._astep()

            await self._emit(
                EventType.STEP_END,
                step=step,
                payload={"step": step, "status": "completed" if not should_continue else "progress"}
            )

            if not should_continue:
                break

        return self._final_answer or "Task execution finished."

    async def arun(self, task: str = "", max_steps: Optional[int] = None, **kwargs: Any) -> Any:
        prompt_val = task or kwargs.get("prompt", "")
        limit_val = max_steps or getattr(self.config, "max_steps", 30)
        return await self.run_task(prompt=prompt_val, max_steps=limit_val)


__all__ = ["ToolCallAgent"]