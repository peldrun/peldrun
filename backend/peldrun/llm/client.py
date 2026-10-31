"""
backend/peldrun/llm/client.py

Universal LLM Client for PELDRUN Core Runtime.
Built on official AsyncOpenAI with deep reasoning extraction from model_extra,
true streaming token generation, TTFT latency accounting, and canonical TokenUsage metering.
Propagates dynamic reasoning effort control (none, low, medium, high) to both Cloud and Local OpenAI-compatible backends.
Strictly normalizes 'off' to 'none' ensuring complete compliance with OpenAI API schema standards.
Includes model-aware prompt compatibility normalization (e.g. Gemma chat template constraints).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union
import uuid

# Documented fallback defaults for the universal LLM client.
# These values are used only when neither an explicit argument nor a
# config object supplies the corresponding setting. All three can be
# overridden at runtime via environment variables.
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_LLM_API_KEY = "EMPTY"
DEFAULT_LLM_TIMEOUT_SECONDS = 300.0

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("peldrun.llm.client")
from collections import deque as _deque
_RECENT_TOOL_SIGNATURES: _deque = _deque(maxlen=12)
from collections import defaultdict as _defaultdict
_TOOL_CALL_COUNTER: Dict[str, int] = _defaultdict(int)


def _is_oom_error(exc: BaseException) -> bool:
    """
    Detect provider out-of-memory errors (GPU or CPU).

    LM Studio emits ``"bad allocation"`` when the model cannot allocate
    enough VRAM/RAM for the current request. Other providers emit
    variations of ``"out of memory"`` / ``"CUDA out of memory"``.

    Retrying these errors is counterproductive — the retry consumes
    additional memory before the original allocation is freed, which is
    exactly the failure mode observed when a model OOMs during streaming
    and the client falls back to non-streaming, consuming MORE memory
    only to fail again with a worse error.
    """
    if exc is None:
        return False
    err_str = str(exc).lower()
    if not err_str:
        return False
    patterns = (
        "bad allocation",
        "out of memory",
        "cuda out of memory",
        "insufficient memory",
        "memory exhausted",
        "cannot allocate",
        "oom error",
    )
    return any(p in err_str for p in patterns)


class UsageSource(str, Enum):
    """Authoritative origin of invocation token metrics."""

    PROVIDER = "provider"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class TokenUsage(BaseModel):
    """
    Canonical typed domain model for token accounting and cost metering.
    Strictly adheres to the accounting invariant: Unknown != 0.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    input_tokens: Optional[int] = Field(
        default=None,
        description="Number of prompt/input tokens processed by the model",
    )
    output_tokens: Optional[int] = Field(
        default=None,
        description="Number of generated tokens emitted in the completion",
    )
    total_tokens: Optional[int] = Field(
        default=None,
        description="Total tokens consumed across input and output cycles",
    )
    cached_input_tokens: Optional[int] = Field(
        default=None,
        description="Number of input tokens served from provider prompt cache",
    )
    reasoning_output_tokens: Optional[int] = Field(
        default=None,
        description="Number of tokens consumed by internal reasoning/thought",
    )
    source: UsageSource = Field(
        default=UsageSource.PROVIDER,
        description="Origin of the usage metrics (provider, estimated, unknown)",
    )
    estimated: bool = Field(
        default=False,
        description="Flag denoting whether metrics were locally estimated",
    )
    tokenizer_id: Optional[str] = Field(
        default=None,
        description="Identifier of tokenizer used if estimated",
    )
    tokenizer_version: Optional[str] = Field(
        default=None,
        description="Version string of the estimator tokenizer",
    )
    estimation_method: Optional[str] = Field(
        default=None,
        description="Algorithm used for estimation (e.g. bpe_encoding, heuristic)",
    )

    @property
    def prompt_tokens(self) -> Optional[int]:
        return self.input_tokens

    @property
    def completion_tokens(self) -> Optional[int]:
        return self.output_tokens

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dictionary-style safe retrieval for backward compatibility."""
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        if key == "prompt_tokens":
            return self.input_tokens if self.input_tokens is not None else default
        if key == "completion_tokens":
            return self.output_tokens if self.output_tokens is not None else default
        return default

    def __getitem__(self, key: str) -> Any:
        """Allow dictionary-style key indexing for legacy callers."""
        val = self.get(key, None)
        if val is not None:
            return val
        raise KeyError(key)

    def to_dict(self) -> Dict[str, Any]:
        """Export serialized representation formatted for session projection."""
        return {
            "prompt_tokens": self.input_tokens or 0,
            "completion_tokens": self.output_tokens or 0,
            "total_tokens": self.total_tokens or 0,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "source": self.source.value,
            "estimated": self.estimated,
        }


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
    timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS
    reasoning_effort: Optional[str] = None
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
    usage: Optional[TokenUsage] = None
    model: Optional[str] = None
    raw: Optional[Any] = None
    latency_ms: Optional[float] = None
    ttft_ms: Optional[float] = None

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
    usage: Optional[TokenUsage] = None
    raw: Optional[Any] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Any]] = None

    def __post_init__(self) -> None:
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
            "required": [],
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
                        item.get("type")
                        for item in spec["anyOf"]
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
                "parameters": clean_params,
            },
        })

    return cleaned_tools


def _content_to_plain_text(content: Any) -> str:
    """
    Flatten a message ``content`` field into a plain string.

    Handles three shapes:
      * ``str``          — returned as-is.
      * ``list`` of parts — concatenates ``text`` fields of every
        ``type == "text"`` part; ignores image parts since they cannot
        be folded into a text prompt.
      * anything else   — coerced with ``str()``.

    This helper exists so that the Gemma-folding path never calls
    ``str(content)`` on a multimodal parts list (which would produce a
    corrupt representation of the payload).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_fragments: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    val = part.get("text", "")
                    if isinstance(val, str) and val:
                        text_fragments.append(val)
            elif hasattr(part, "type") and getattr(part, "type", None) == "text":
                val = getattr(part, "text", "")
                if isinstance(val, str) and val:
                    text_fragments.append(val)
        return "\n".join(text_fragments)
    return str(content)


def normalize_messages_for_target_model(
    messages: List[Dict[str, Any]],
    model_name: str,
) -> List[Dict[str, Any]]:
    """
    Normalizes message sequences based on target model template restrictions.
    For models that reject 'system' roles in their Jinja templates (such as Google Gemma),
    system directives are seamlessly folded into the initial 'user' turn.

    Multimodal safety
    -----------------
    When the first user message carries a multimodal ``content`` list
    (text parts + image_url parts), the folded system directive is
    prepended as an additional ``{"type": "text"}`` part rather than
    being stringified. This preserves the multimodal payload exactly as
    the provider expects it, and never replaces a parts list with its
    string repr.

    Plain-text ``content`` keeps the original string-concatenation
    behaviour unchanged.
    """
    if not messages:
        return []

    norm_model = (model_name or "").lower()
    is_gemma = "gemma" in norm_model

    if not is_gemma:
        return list(messages)

    system_parts: List[str] = []
    dialogue: List[Dict[str, Any]] = []

    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        raw_content = msg.get("content", "")
        if role == "system":
            # System directives are semantically plain text; use the
            # flattening helper in case the caller ever passes parts.
            flat = _content_to_plain_text(raw_content).strip()
            if flat:
                system_parts.append(flat)
        else:
            dialogue.append(dict(msg))

    if not system_parts:
        return dialogue

    combined_system_text = "\n\n".join(system_parts)

    if dialogue and str(dialogue[0].get("role", "")).lower() == "user":
        first_user_content = dialogue[0].get("content", "")
        if isinstance(first_user_content, list):
            # Multimodal first user turn: prepend the system text as a
            # leading text part so the parts list survives intact.
            dialogue[0]["content"] = [
                {"type": "text", "text": combined_system_text}
            ] + list(first_user_content)
        else:
            flat_first = _content_to_plain_text(first_user_content)
            dialogue[0]["content"] = (
                f"{combined_system_text}\n\n{flat_first}"
            )
    else:
        dialogue.insert(0, {"role": "user", "content": combined_system_text})

    return dialogue


class AsyncLLMClient:
    """
    Universal asynchronous client utilizing official AsyncOpenAI.
    Extracts reasoning_content reliably from local/cloud models, delivers true streaming,
    measures TTFT latency, and normalizes usage metrics into canonical TokenUsage.
    """

    def __init__(
        self,
        config: Optional[Any] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 300.0,
        reasoning_effort: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.config = config
        resolved_base_url = os.environ.get(
            "PELDRUN_LLM_DEFAULT_BASE_URL", DEFAULT_LLM_BASE_URL
        )
        resolved_api_key = os.environ.get(
            "PELDRUN_LLM_DEFAULT_API_KEY", DEFAULT_LLM_API_KEY
        )
        resolved_timeout = float(
            timeout
            or os.environ.get(
                "PELDRUN_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT_SECONDS
            )
        )
        self.model = "default"
        self.reasoning_effort = reasoning_effort

        if isinstance(config, dict):
            resolved_base_url = config.get("base_url") or config.get("api_base") or base_url or resolved_base_url
            resolved_api_key = config.get("api_key") or api_key or resolved_api_key
            resolved_timeout = float(config.get("timeout") or resolved_timeout)
            self.model = config.get("model", self.model)
            self.reasoning_effort = config.get("reasoning_effort") or self.reasoning_effort
        elif config is not None and hasattr(config, "base_url"):
            resolved_base_url = config.base_url or getattr(config, "api_base", None) or base_url or resolved_base_url
            resolved_api_key = config.api_key or api_key or resolved_api_key
            resolved_timeout = float(getattr(config, "timeout", None) or resolved_timeout)
            self.model = getattr(config, "model", self.model)
            self.reasoning_effort = getattr(config, "reasoning_effort", None) or self.reasoning_effort
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
            timeout=self.timeout,
        )

    def _normalize_raw_usage(self, raw_usage: Any) -> Optional[TokenUsage]:
        """Extract canonical TokenUsage from provider response object or dict."""
        if not raw_usage:
            return None

        if isinstance(raw_usage, dict):
            inp = raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens")
            out = raw_usage.get("completion_tokens") or raw_usage.get("output_tokens")
            tot = raw_usage.get("total_tokens")
            cached = raw_usage.get("cached_tokens") or raw_usage.get("cached_input_tokens")
            reasoning = raw_usage.get("reasoning_tokens") or raw_usage.get("reasoning_output_tokens")

            prompt_details = raw_usage.get("prompt_tokens_details")
            if isinstance(prompt_details, dict) and cached is None:
                cached = prompt_details.get("cached_tokens")

            comp_details = raw_usage.get("completion_tokens_details")
            if isinstance(comp_details, dict) and reasoning is None:
                reasoning = comp_details.get("reasoning_tokens")

            if inp is not None or out is not None or tot is not None:
                return TokenUsage(
                    input_tokens=int(inp) if inp is not None else None,
                    output_tokens=int(out) if out is not None else None,
                    total_tokens=int(tot) if tot is not None else (int(inp or 0) + int(out or 0)),
                    cached_input_tokens=int(cached) if cached is not None else None,
                    reasoning_output_tokens=int(reasoning) if reasoning is not None else None,
                    source=UsageSource.PROVIDER,
                    estimated=False,
                )

        inp = getattr(raw_usage, "prompt_tokens", None)
        out = getattr(raw_usage, "completion_tokens", None)
        tot = getattr(raw_usage, "total_tokens", None)

        cached = None
        prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
        if prompt_details:
            cached = getattr(prompt_details, "cached_tokens", None)

        reasoning = None
        comp_details = getattr(raw_usage, "completion_tokens_details", None)
        if comp_details:
            reasoning = getattr(comp_details, "reasoning_tokens", None)

        if inp is not None or out is not None or tot is not None:
            return TokenUsage(
                input_tokens=int(inp) if inp is not None else None,
                output_tokens=int(out) if out is not None else None,
                total_tokens=int(tot) if tot is not None else (int(inp or 0) + int(out or 0)),
                cached_input_tokens=int(cached) if cached is not None else None,
                reasoning_output_tokens=int(reasoning) if reasoning is not None else None,
                source=UsageSource.PROVIDER,
                estimated=False,
            )

        return None

    def _fallback_usage_estimation(
        self,
        messages: List[Dict[str, Any]],
        output_text: Optional[str] = None,
        reasoning_text: Optional[str] = None,
        tool_calls: Optional[List[Any]] = None,
        model: str = "default",
    ) -> TokenUsage:
        """Calculate fallback token accounting when provider usage is absent."""
        try:
            from peldrun.llm.tokenizer import estimate_invocation_usage

            est = estimate_invocation_usage(
                messages=messages,
                output_text=output_text,
                reasoning_text=reasoning_text,
                tool_calls=tool_calls,
                model=model,
            )
            return TokenUsage(**est)
        except Exception as exc:
            logger.warning("Tokenizer fallback estimation failed: %s", exc)
            return TokenUsage(
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                source=UsageSource.UNKNOWN,
                estimated=True,
                estimation_method="failed_estimation",
            )

    def _process_non_streaming_choice(
        self,
        response: Any,
        effective_messages: List[Dict[str, Any]],
        chosen_model: str,
        start_time: float,
    ) -> LLMResponse:
        """Helper to process standard non-streaming ChatCompletion response."""
        import sys

        total_latency_ms = (time.time() - start_time) * 1000.0
        if not getattr(response, "choices", None):
            return LLMResponse(
                content="",
                raw=response,
                latency_ms=total_latency_ms,
                usage=self._fallback_usage_estimation(messages=effective_messages, model=chosen_model),
            )

        first_choice = response.choices[0]
        message = first_choice.message
        content = message.content or ""
        if content:
            sys.stdout.write(content)
            sys.stdout.flush()

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
                        type="function",
                    )
                )

        usage = self._normalize_raw_usage(response.usage)
        if usage is None:
            usage = self._fallback_usage_estimation(
                messages=effective_messages,
                output_text=content,
                reasoning_text=reasoning,
                tool_calls=parsed_tool_calls,
                model=chosen_model,
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
            raw=response,
            latency_ms=total_latency_ms,
            ttft_ms=total_latency_ms,
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
        reasoning_effort: Optional[str] = None,
        stream: bool = True,
        on_chunk: Optional[Callable[[str], Any]] = None,
        on_thought_chunk: Optional[Callable[[str], Any]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Executes a chat completion request with resilient stream accumulation and auto-fallback."""
        import sys

        start_time = time.time()
        chosen_model = model or getattr(self.config, "model", None) or self.model or "default"
        if isinstance(self.config, dict):
            chosen_model = self.config.get("model", chosen_model)

        temp = temperature if temperature is not None else getattr(self.config, "temperature", 0.7)
        if isinstance(self.config, dict):
            temp = self.config.get("temperature", temp)

        effective_messages = normalize_messages_for_target_model(messages, chosen_model)

        call_kwargs: Dict[str, Any] = {
            "model": chosen_model,
            "messages": effective_messages,
            "temperature": float(temp),
            "stream": stream,
        }

        effort = (
            reasoning_effort
            or kwargs.get("reasoning_effort")
            or getattr(self.config, "reasoning_effort", None)
            or self.reasoning_effort
        )
        if isinstance(self.config, dict) and not effort:
            effort = self.config.get("reasoning_effort")

        if effort and str(effort).lower() in ("none", "off", "0", "low", "medium", "high", "minimal", "xhigh"):
            raw_eff = str(effort).lower().strip()
            norm_effort = "none" if raw_eff in ("none", "off", "0") else raw_eff

            call_kwargs["reasoning_effort"] = norm_effort

            extra_body = kwargs.get("extra_body") or {}
            if not isinstance(extra_body, dict):
                extra_body = {}
            extra_body["reasoning_effort"] = norm_effort
            call_kwargs["extra_body"] = extra_body

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
            call_kwargs["max_tokens"] = int(limit_tokens)

        if call_kwargs.get("stream"):
            try:
                response = await self.client.chat.completions.create(**call_kwargs)
            except Exception as stream_init_err:
                # [FIX-OOM-FAST-FAIL] OOM must never trigger a retry.
                if _is_oom_error(stream_init_err):
                    logger.error(
                        "[LLM CLIENT] OOM detected for model '%s' during "
                        "streaming init: %s. Raising immediately (no retry).",
                        chosen_model,
                        stream_init_err,
                    )
                    raise RuntimeError(
                        f"The model '{chosen_model}' ran out of memory (OOM). "
                        f"Restart LM Studio to free GPU/CPU memory, or switch "
                        f"to a smaller/quantized model. "
                        f"Original error: {stream_init_err}"
                    ) from stream_init_err
                logger.warning(
                    "[LLM CLIENT] Streaming init failed for model '%s' (%s). Retrying with stream=False fallback...",
                    chosen_model,
                    stream_init_err,
                )
                call_kwargs["stream"] = False
                try:
                    fallback_resp = await self.client.chat.completions.create(**call_kwargs)
                except Exception as fallback_err:
                    if _is_oom_error(fallback_err):
                        raise RuntimeError(
                            f"The model '{chosen_model}' ran out of memory (OOM) "
                            f"during non-streaming fallback. "
                            f"Restart LM Studio or switch to a smaller model. "
                            f"Original error: {fallback_err}"
                        ) from fallback_err
                    raise
                return self._process_non_streaming_choice(
                    response=fallback_resp,
                    effective_messages=effective_messages,
                    chosen_model=chosen_model,
                    start_time=start_time,
                )

            accumulated_content: List[str] = []
            accumulated_reasoning: List[str] = []
            accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
            finish_reason: Optional[str] = None
            captured_usage: Optional[TokenUsage] = None
            first_token_at: Optional[float] = None
            stream_interrupted = False

            try:
                async for chunk in response:
                    now = time.time()
                    if hasattr(chunk, "usage") and chunk.usage:
                        captured_usage = self._normalize_raw_usage(chunk.usage)

                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue

                    first_choice = choices[0]
                    if getattr(first_choice, "finish_reason", None):
                        finish_reason = first_choice.finish_reason

                    delta = getattr(first_choice, "delta", None)
                    if not delta:
                        continue

                    content_delta = getattr(delta, "content", None)
                    reasoning_delta = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                        or getattr(delta, "thought", None)
                    )
                    if not reasoning_delta and hasattr(delta, "model_extra") and isinstance(delta.model_extra, dict):
                        reasoning_delta = (
                            delta.model_extra.get("reasoning_content")
                            or delta.model_extra.get("reasoning")
                            or delta.model_extra.get("thought")
                        )

                    if (content_delta or reasoning_delta) and first_token_at is None:
                        first_token_at = now

                    if content_delta:
                        accumulated_content.append(content_delta)
                        sys.stdout.write(content_delta)
                        sys.stdout.flush()
                        if on_chunk:
                            try:
                                cb_res = on_chunk(content_delta)
                                if asyncio.iscoroutine(cb_res):
                                    await cb_res
                            except Exception as cb_err:
                                logger.debug("on_chunk callback error: %s", cb_err)

                    if reasoning_delta:
                        accumulated_reasoning.append(reasoning_delta)
                        sys.stdout.write(reasoning_delta)
                        sys.stdout.flush()
                        if on_thought_chunk:
                            try:
                                cb_res = on_thought_chunk(reasoning_delta)
                                if asyncio.iscoroutine(cb_res):
                                    await cb_res
                            except Exception as cb_err:
                                logger.debug("on_thought_chunk callback error: %s", cb_err)

                    tc_deltas = getattr(delta, "tool_calls", None)
                    if tc_deltas:
                        for tc in tc_deltas:
                            idx = getattr(tc, "index", 0)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": getattr(tc, "id", None) or f"call_{idx}_{uuid.uuid4().hex[:6]}",
                                    "name": "",
                                    "arguments": "",
                                }
                            if getattr(tc, "id", None):
                                accumulated_tool_calls[idx]["id"] = tc.id

                            fn = getattr(tc, "function", None)
                            if fn:
                                if getattr(fn, "name", None):
                                    if not accumulated_tool_calls[idx]["name"]:
                                        print(f"\n[ENGINE PELDRUN] Tool call requested: '{fn.name}'", flush=True)
                                    accumulated_tool_calls[idx]["name"] += fn.name
                                if getattr(fn, "arguments", None):
                                    accumulated_tool_calls[idx]["arguments"] += fn.arguments

            except Exception as stream_read_err:
                # [FIX-OOM-FAST-FAIL] Same rationale as streaming init.
                if _is_oom_error(stream_read_err):
                    logger.error(
                        "[LLM CLIENT] OOM detected during active stream for "
                        "model '%s': %s. Raising immediately (no fallback).",
                        chosen_model,
                        stream_read_err,
                    )
                    raise RuntimeError(
                        f"The model '{chosen_model}' ran out of memory (OOM) "
                        f"during generation. Restart LM Studio to free memory, "
                        f"or switch to a smaller/quantized model. "
                        f"Original error: {stream_read_err}"
                    ) from stream_read_err
                logger.warning(
                    "[LLM CLIENT] Active stream interrupted for model '%s': %s",
                    chosen_model,
                    stream_read_err,
                )
                stream_interrupted = True

            if stream_interrupted and not accumulated_content and not accumulated_reasoning and not accumulated_tool_calls:
                print(
                    f"\n[LLM CLIENT] Stream interrupted for '{chosen_model}' with zero tokens. Falling back to non-streaming...",
                    flush=True,
                )
                call_kwargs["stream"] = False
                try:
                    non_stream_resp = await self.client.chat.completions.create(**call_kwargs)
                except Exception as fallback_err:
                    if _is_oom_error(fallback_err):
                        raise RuntimeError(
                            f"The model '{chosen_model}' ran out of memory (OOM) "
                            f"during non-streaming fallback. "
                            f"Restart LM Studio or switch to a smaller model. "
                            f"Original error: {fallback_err}"
                        ) from fallback_err
                    raise
                return self._process_non_streaming_choice(
                    response=non_stream_resp,
                    effective_messages=effective_messages,
                    chosen_model=chosen_model,
                    start_time=start_time,
                )

            total_latency_ms = (time.time() - start_time) * 1000.0
            ttft_ms = (first_token_at - start_time) * 1000.0 if first_token_at else total_latency_ms

            full_content = "".join(accumulated_content)
            full_reasoning = "".join(accumulated_reasoning) or None
            # Register a compact signature of every tool call for downstream
            # loop detection by the engine layer. Signature = name + args (hashed).
            import hashlib as _hashlib
            for idx in sorted(accumulated_tool_calls.keys()):
                _tc = accumulated_tool_calls[idx]
                _sig_raw = f"{_tc['name']}::{_tc['arguments']}"
                _sig = _hashlib.sha256(_sig_raw.encode("utf-8")).hexdigest()[:16]
                _RECENT_TOOL_SIGNATURES.append((_tc["name"], _sig))
            parsed_tool_calls: List[ToolCall] = []
            for idx in sorted(accumulated_tool_calls.keys()):
                tc_item = accumulated_tool_calls[idx]
                raw_args = tc_item["arguments"]
                if raw_args:
                    try:
                        args_obj = json.loads(raw_args)
                    except Exception:
                        args_obj = {"raw": raw_args}
                else:
                    args_obj = {}

                parsed_tool_calls.append(
                    ToolCall(
                        id=tc_item["id"],
                        name=tc_item["name"],
                        arguments=args_obj,
                        type="function",
                    )
                )

            usage = captured_usage
            if usage is None:
                usage = self._fallback_usage_estimation(
                    messages=effective_messages,
                    output_text=full_content,
                    reasoning_text=full_reasoning,
                    tool_calls=parsed_tool_calls,
                    model=chosen_model,
                )

            return LLMResponse(
                content=full_content,
                reasoning=full_reasoning,
                reasoning_content=full_reasoning,
                thought=full_reasoning,
                tool_calls=parsed_tool_calls,
                finish_reason=finish_reason or "stop",
                usage=usage,
                model=chosen_model,
                raw=None,
                latency_ms=total_latency_ms,
                ttft_ms=ttft_ms,
            )

        response = await self.client.chat.completions.create(**call_kwargs)
        return self._process_non_streaming_choice(
            response=response,
            effective_messages=effective_messages,
            chosen_model=chosen_model,
            start_time=start_time,
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
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """
        Executes a true live token stream request via AsyncOpenAI.
        Measures Time-To-First-Token (TTFT), streams content deltas, and extracts final usage.
        """
        start_time = time.time()
        first_token_at: Optional[float] = None

        chosen_model = model or getattr(self.config, "model", None) or self.model or "default"
        if isinstance(self.config, dict):
            chosen_model = self.config.get("model", chosen_model)

        temp = temperature if temperature is not None else getattr(self.config, "temperature", 0.7)
        if isinstance(self.config, dict):
            temp = self.config.get("temperature", temp)

        effective_messages = normalize_messages_for_target_model(messages, chosen_model)

        call_kwargs: Dict[str, Any] = {
            "model": chosen_model,
            "messages": effective_messages,
            "temperature": float(temp),
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        effort = (
            reasoning_effort
            or kwargs.get("reasoning_effort")
            or getattr(self.config, "reasoning_effort", None)
            or self.reasoning_effort
        )
        if isinstance(self.config, dict) and not effort:
            effort = self.config.get("reasoning_effort")

        if effort and str(effort).lower() in ("none", "off", "0", "low", "medium", "high", "minimal", "xhigh"):
            raw_eff = str(effort).lower().strip()
            norm_effort = "none" if raw_eff in ("none", "off", "0") else raw_eff

            call_kwargs["reasoning_effort"] = norm_effort

            extra_body = kwargs.get("extra_body") or {}
            if not isinstance(extra_body, dict):
                extra_body = {}
            extra_body["reasoning_effort"] = norm_effort
            call_kwargs["extra_body"] = extra_body

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
            call_kwargs["max_tokens"] = int(limit_tokens)

        accumulated_content = []
        accumulated_reasoning = []
        captured_usage: Optional[TokenUsage] = None

        stream_resp = await self.client.chat.completions.create(**call_kwargs)

        async for chunk in stream_resp:
            now = time.time()

            if hasattr(chunk, "usage") and chunk.usage:
                captured_usage = self._normalize_raw_usage(chunk.usage)

            choices = chunk.choices or []
            if not choices:
                continue

            first_choice = choices[0]
            delta = first_choice.delta

            content_delta = getattr(delta, "content", None)
            reasoning_delta = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or getattr(delta, "thought", None)
            )
            if not reasoning_delta and hasattr(delta, "model_extra") and isinstance(delta.model_extra, dict):
                reasoning_delta = (
                    delta.model_extra.get("reasoning_content")
                    or delta.model_extra.get("reasoning")
                    or delta.model_extra.get("thought")
                )

            if (content_delta or reasoning_delta) and first_token_at is None:
                first_token_at = now

            if content_delta:
                accumulated_content.append(content_delta)
            if reasoning_delta:
                accumulated_reasoning.append(reasoning_delta)

            tc_deltas: List[DeltaToolCall] = []
            if getattr(delta, "tool_calls", None):
                for idx, tc in enumerate(delta.tool_calls):
                    fn = getattr(tc, "function", None)
                    tc_deltas.append(
                        DeltaToolCall(
                            index=getattr(tc, "index", idx),
                            id=getattr(tc, "id", None),
                            name=getattr(fn, "name", None) if fn else None,
                            arguments=getattr(fn, "arguments", None) if fn else None,
                            type="function",
                        )
                    )

            finish_reason = getattr(first_choice, "finish_reason", None)

            yield StreamChunk(
                content_delta=content_delta,
                reasoning_delta=reasoning_delta,
                tool_call_deltas=tc_deltas,
                finish_reason=finish_reason,
                usage=captured_usage,
                raw=chunk,
            )

        if captured_usage is None:
            full_out = "".join(accumulated_content)
            full_reas = "".join(accumulated_reasoning)
            final_usage = self._fallback_usage_estimation(
                messages=effective_messages,
                output_text=full_out,
                reasoning_text=full_reas,
                model=chosen_model,
            )
            yield StreamChunk(
                finish_reason="stop",
                usage=final_usage,
            )
