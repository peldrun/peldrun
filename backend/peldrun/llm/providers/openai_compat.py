# backend/peldrun/llm/providers/openai_compat.py
"""
OpenAI-compatible universal provider bridge.

This module is the provider-agnostic entry point used by every LLM backend
(LM Studio, Ollama, OpenAI, and any other OpenAI-compatible endpoint). It
delegates generation and true token streaming to the unified
``AsyncLLMClient`` and lowers typed multimodal messages into the plain
dictionary payloads that the underlying SDK expects.

Design notes
------------
- ``BaseLLMProvider`` accepts either plain ``Dict`` messages (legacy
  callers, tool_call-bearing assistant turns, provider-specific extra
  fields) or typed ``ChatMessage`` instances (multimodal flows built on
  ``peldrun.llm.schema``).

- ``OpenAICompatPayloadBuilder.build_messages_payload`` performs the
  lowering step. Its key invariant is: ``Dict`` messages pass through
  untouched so that no OpenAI field is ever silently dropped. Only typed
  ``ChatMessage`` instances are projected via ``to_openai_dict``.

- This module does not touch the wire format. SSRF, retry, timeout, and
  reasoning extraction all remain in ``AsyncLLMClient``.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Union

from peldrun.llm.client import (
    AsyncLLMClient,
    LLMConfig,
    LLMResponse,
    StreamChunk,
    TokenUsage,
    UsageSource,
)
from peldrun.llm.schema import ChatMessage


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


class OpenAICompatPayloadBuilder:
    """
    Lower typed multimodal messages into OpenAI-compatible dictionaries.

    The builder is intentionally conservative: ``Dict`` messages are
    forwarded verbatim so that provider-specific fields (``tool_calls``,
    ``function_call``, ``name``, ``tool_call_id``, and any future OpenAI
    extensions) are never lost. Only typed ``ChatMessage`` instances are
    projected through the canonical ``to_openai_dict`` view.
    """

    @staticmethod
    def build_messages_payload(
        messages: List[Union[ChatMessage, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """
        Return a list of OpenAI-compatible message dictionaries.

        - Typed ``ChatMessage`` instances are lowered via
          ``ChatMessage.to_openai_dict``.
        - Any other input (plain ``Dict``) is passed through untouched.
        """
        payload: List[Dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, ChatMessage):
                payload.append(msg.to_openai_dict())
            else:
                # Preserve the raw dictionary exactly as supplied. This
                # protects ``tool_calls`` on assistant turns and any
                # provider-specific extension fields.
                payload.append(msg)
        return payload


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------


class BaseLLMProvider:
    """Universal provider wrapper delegating directly to ``AsyncLLMClient``."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        client: Optional[AsyncLLMClient] = None,
        **kwargs: Any,
    ) -> None:
        self.config = config or LLMConfig()
        self.client: AsyncLLMClient = client or AsyncLLMClient(
            config=self.config,
            **kwargs,
        )

    async def generate(
        self,
        messages: List[Union[ChatMessage, Dict[str, Any]]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> LLMResponse:
        normalized = OpenAICompatPayloadBuilder.build_messages_payload(messages)
        return await self.client.generate(
            messages=normalized,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

    async def stream(
        self,
        messages: List[Union[ChatMessage, Dict[str, Any]]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        normalized = OpenAICompatPayloadBuilder.build_messages_payload(messages)
        async for chunk in self.client.stream(
            messages=normalized,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        ):
            yield chunk

    async def chat_completion(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self.generate(*args, **kwargs)

    async def chat_complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self.generate(*args, **kwargs)


# ---------------------------------------------------------------------------
# Concrete provider
# ---------------------------------------------------------------------------


class OpenAICompatProvider(BaseLLMProvider):
    """Universal provider for standard and local OpenAI-compatible endpoints."""

    pass


__all__ = [
    "BaseLLMProvider",
    "OpenAICompatPayloadBuilder",
    "OpenAICompatProvider",
    "TokenUsage",
    "UsageSource",
]
