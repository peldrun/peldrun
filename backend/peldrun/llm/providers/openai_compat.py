"""
OpenAI-compatible universal provider bridge.
Delegates generation directly to the unified AsyncLLMClient.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional
from peldrun.llm.client import AsyncLLMClient, LLMConfig, LLMResponse, StreamChunk


class BaseLLMProvider:
    """Universal provider wrapper delegating directly to AsyncLLMClient."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        client: Optional[AsyncLLMClient] = None,
        **kwargs: Any
    ) -> None:
        self.config = config or LLMConfig()
        self.client: AsyncLLMClient = client or AsyncLLMClient(
            config=self.config,
            **kwargs
        )

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        **kwargs: Any
    ) -> LLMResponse:
        return await self.client.generate(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs
        )

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        **kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        async for chunk in self.client.stream(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs
        ):
            yield chunk

    async def chat_completion(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self.generate(*args, **kwargs)

    async def chat_complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return await self.generate(*args, **kwargs)


class OpenAICompatProvider(BaseLLMProvider):
    """Universal provider for standard and local OpenAI-compatible endpoints."""
    pass