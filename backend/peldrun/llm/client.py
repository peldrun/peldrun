"""
Universal LLM Client for PELDRUN Core Runtime.
Built on official AsyncOpenAI client with deep reasoning extraction from model_extra
to guarantee live thought streaming across local (LM Studio/Ollama) and cloud providers.
Configured with resilient extended timeouts optimized for local edge model inference.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Union

from openai import AsyncOpenAI


@dataclass
class ToolCall:
    """Represents an executable function call request from an LLM."""
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")
    name: str = ""
    arguments: Union[Dict[str, Any], str] = field(default_factory=dict)
    type: str = "function"

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    @property
    def function(self) -> Any:
        class _FunctionWrapper:
            def __init__(self, name: str, args: Any):
                self.name = name
                self.arguments = args
        return _FunctionWrapper(self.name, self.arguments)


@dataclass
class DeltaToolCall:
    """Represents an incremental streaming token delta for a tool call."""
    index: int = 0
    id: Optional[str] = None
    name: Optional[str] = None
    arguments: Optional[str] = None
    type: str = "function"


@dataclass
class LLMConfig:
    """
    Universal configuration payload for any LLM provider.
    Defaults to 300.0s timeout to allow local quantization models sufficient inference time.
    """
    model: str = "default"
    base_url: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    provider: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: float = 1.0
    timeout: float = 300.0
    extra_headers: Dict[str, str] = field(default_factory=dict)
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.api_base and not self.base_url:
            self.base_url = self.api_base
        if self.base_url and not self.api_base:
            self.api_base = self.base_url


@dataclass
class LLMResponse:
    """Canonical model response across all inference providers."""
    content: Optional[str] = None
    reasoning: Optional[str] = None
    reasoning_content: Optional[str] = None
    thought: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    model: Optional[str] = None
    raw: Optional[Any] = None

    def __post_init__(self) -> None:
        val = self.reasoning or self.reasoning_content or self.thought
        if val:
            self.reasoning = val
            self.reasoning_content = val
            self.thought = val



@dataclass
class StreamChunk:
    """Represents a token or event payload yielded during active response streaming."""
    content_delta: Optional[str] = None
    reasoning_delta: Optional[str] = None
    tool_call_deltas: List[DeltaToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    raw: Optional[Any] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Any]] = None

    def __post_init__(self) -> None:
        """Synchronize textual content and tool call collections for complete interoperability."""
        if self.content is not None and self.content_delta is None:
            self.content_delta = self.content
        elif self.content_delta is not None and self.content is None:
            self.content = self.content_delta

        if self.tool_calls is not None and not self.tool_call_deltas:
            self.tool_call_deltas = self.tool_calls
        elif self.tool_call_deltas and self.tool_calls is None:
            self.tool_calls = self.tool_call_deltas
        elif self.tool_calls is None:
            self.tool_calls = []


def sanitize_tools_for_openai(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    Sanitizes arbitrary tool schemas into compliant OpenAI function-calling specifications.
    Guarantees 'type: object' and eliminates invalid schema constructs.
    """
    if not tools:
        return None

    cleaned_tools: List[Dict[str, Any]] = []

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        fn = tool.get("function") if "function" in tool else tool
        if not isinstance(fn, dict):
            continue

        name = fn.get("name") or tool.get("name") or "unknown_tool"
        description = fn.get("description") or tool.get("description") or ""
        params = fn.get("parameters") or tool.get("parameters") or {}

        clean_params: Dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": []
        }

        if isinstance(params, dict) and "properties" in params and isinstance(params["properties"], dict):
            clean_params["type"] = "object"
            clean_params["required"] = list(params.get("required") or [])
            properties_map: Dict[str, Any] = {}

            for prop_name, prop_spec in params["properties"].items():
                if not isinstance(prop_spec, dict):
                    properties_map[prop_name] = {"type": "string", "description": str(prop_spec)}
                    continue

                spec = dict(prop_spec)
                if "anyOf" in spec and isinstance(spec["anyOf"], list):
                    non_nulls = [
                        item.get("type") for item in spec["anyOf"]
                        if isinstance(item, dict) and item.get("type") not in ("null", None)
                    ]
                    del spec["anyOf"]
                    spec["type"] = non_nulls[0] if non_nulls else "string"

                if "type" not in spec:
                    spec["type"] = "string"

                properties_map[prop_name] = spec

            clean_params["properties"] = properties_map

        elif isinstance(params, dict) and params:
            props = {}
            for k, v in params.items():
                t = "string"
                if isinstance(v, str) and v.lower() in ("string", "integer", "number", "boolean", "array", "object"):
                    t = v.lower()
                props[k] = {"type": t, "description": f"Parameter {k}"}
            clean_params["properties"] = props
            clean_params["required"] = list(props.keys())

        cleaned_tools.append({
            "type": "function",
            "function": {
                "name": str(name),
                "description": str(description),
                "parameters": clean_params
            }
        })

    return cleaned_tools


class AsyncLLMClient:
    """
    Universal asynchronous client utilizing official AsyncOpenAI.
    Extracts reasoning_content reliably from local models with generous inference timeouts.
    """

    def __init__(
        self,
        config: Optional[Any] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 300.0,
        **kwargs: Any
    ) -> None:
        self.config = config
        resolved_base_url = "http://127.0.0.1:1234/v1"
        resolved_api_key = "EMPTY"
        resolved_timeout = float(timeout or 300.0)
        self.model = "default"

        if isinstance(config, dict):
            resolved_base_url = config.get("base_url") or config.get("api_base") or base_url or resolved_base_url
            resolved_api_key = config.get("api_key") or api_key or resolved_api_key
            resolved_timeout = float(config.get("timeout") or resolved_timeout)
            self.model = config.get("model", self.model)
        elif config is not None and hasattr(config, "base_url"):
            resolved_base_url = config.base_url or getattr(config, "api_base", None) or base_url or resolved_base_url
            resolved_api_key = config.api_key or api_key or resolved_api_key
            resolved_timeout = float(getattr(config, "timeout", None) or resolved_timeout)
            self.model = getattr(config, "model", self.model)
        else:
            resolved_base_url = base_url or resolved_base_url
            resolved_api_key = api_key or resolved_api_key

        clean_base = resolved_base_url.rstrip("/")
        if not clean_base.endswith("/v1"):
            clean_base = f"{clean_base}/v1"

        self.base_url = clean_base
        self.api_key = resolved_api_key or "EMPTY"
        self.timeout = resolved_timeout

        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout
        )

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        **kwargs: Any
    ) -> LLMResponse:
        """Executes a chat completion request and extracts reasoning content across local/cloud providers."""
        chosen_model = model or getattr(self.config, "model", None) or self.model or "default"
        if isinstance(self.config, dict):
            chosen_model = self.config.get("model", chosen_model)

        temp = temperature if temperature is not None else getattr(self.config, "temperature", 0.7)
        if isinstance(self.config, dict):
            temp = self.config.get("temperature", temp)

        call_kwargs: Dict[str, Any] = {
            "model": chosen_model,
            "messages": messages,
            "temperature": float(temp),
            "stream": False
        }

        call_timeout = timeout or getattr(self.config, "timeout", None) or self.timeout
        if call_timeout:
            call_kwargs["timeout"] = float(call_timeout)

        if tools:
            sanitized = sanitize_tools_for_openai(tools)
            if sanitized:
                call_kwargs["tools"] = sanitized
                call_kwargs["tool_choice"] = tool_choice

        limit_tokens = max_tokens or getattr(self.config, "max_tokens", None)
        if isinstance(self.config, dict):
            limit_tokens = limit_tokens or self.config.get("max_tokens")
        if limit_tokens:
            call_kwargs["max_tokens"] = min(int(limit_tokens), 4096)

        response = await self.client.chat.completions.create(**call_kwargs)

        if not response.choices:
            return LLMResponse(content="", raw=response)

        first_choice = response.choices[0]
        message = first_choice.message
        content = message.content or ""

        # Extract reasoning content from standard attributes or model_extra dictionary
        reasoning = (
            getattr(message, "reasoning_content", None)
            or getattr(message, "reasoning", None)
            or getattr(message, "thought", None)
        )
        if not reasoning and hasattr(message, "model_extra") and isinstance(message.model_extra, dict):
            reasoning = (
                message.model_extra.get("reasoning_content")
                or message.model_extra.get("reasoning")
                or message.model_extra.get("thought")
            )

        finish_reason = first_choice.finish_reason
        usage = dict(response.usage) if response.usage else None

        parsed_tool_calls: List[ToolCall] = []

        if message.tool_calls:
            for idx, call in enumerate(message.tool_calls):
                fn = call.function
                args = fn.arguments
                if isinstance(args, str):
                    try:
                        parsed_args = json.loads(args)
                    except Exception:
                        parsed_args = {"raw": args}
                else:
                    parsed_args = args or {}
                parsed_tool_calls.append(
                    ToolCall(
                        id=call.id or f"call_{idx}_{uuid.uuid4().hex[:6]}",
                        name=fn.name,
                        arguments=parsed_args,
                        type="function"
                    )
                )

        return LLMResponse(
            content=content,
            reasoning=reasoning,
            reasoning_content=reasoning,
            thought=reasoning,
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=response.model or chosen_model,
            raw=response
        )

    async def generate(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self.chat_completion(*args, **kwargs)

    async def chat_complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self.chat_completion(*args, **kwargs)

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        **kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        res = await self.chat_completion(messages=messages, tools=tools, tool_choice=tool_choice, **kwargs)
        yield StreamChunk(
            content_delta=res.content,
            reasoning_delta=res.reasoning,
            finish_reason=res.finish_reason,
            usage=res.usage,
            raw=res.raw
        )