"""
PELDRUN Core LM Studio LLM Provider.
Specialized provider for LM Studio local inference with thinking-tag extraction
and model discovery.
"""

from __future__ import annotations

import os
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from peldrun.llm.client import AsyncLLMClient, LLMConfig, LLMResponse, StreamChunk
from peldrun.llm.providers.openai_compat import OpenAICompatProvider

# Documented default endpoint for LM Studio.
# Override via the LMSTUDIO_BASE_URL environment variable or
# [llm.providers.lmstudio].base_url in config.toml.
DEFAULT_LMSTUDIO_BASE_URL = "http://localhost:1234/v1"


def resolve_lmstudio_base_url(config: Optional[LLMConfig]) -> str:
    """Resolve the LM Studio base URL.

    Precedence:
        1. LMSTUDIO_BASE_URL environment variable
        2. config.api_base (loaded from config.toml or caller-supplied)
        3. DEFAULT_LMSTUDIO_BASE_URL
    """
    env_url = os.environ.get("LMSTUDIO_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    if config is not None:
        api_base = getattr(config, "api_base", None)
        if api_base:
            return str(api_base).rstrip("/")
    return DEFAULT_LMSTUDIO_BASE_URL


def _apply_lmstudio_env_override(config: Optional[LLMConfig]) -> Optional[LLMConfig]:
    """Apply LMSTUDIO_BASE_URL override to the LLMConfig when present.

    Ensures all inference calls routed through the parent provider (which reads
    config.api_base) honor the environment variable, not only model discovery.
    """
    env_url = os.environ.get("LMSTUDIO_BASE_URL")
    if not env_url or config is None or not hasattr(config, "api_base"):
        return config
    env_url = env_url.rstrip("/")
    if hasattr(config, "model_copy"):
        return config.model_copy(update={"api_base": env_url})
    config.api_base = env_url
    return config


class LMStudioProvider(OpenAICompatProvider):
    """LM Studio local provider supporting deep-thought extraction and model discovery."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        client: Optional[AsyncLLMClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        **kwargs: Any,
    ) -> None:
        config = _apply_lmstudio_env_override(config)
        super().__init__(config=config, client=client, transport=transport, **kwargs)
        self.name = "lmstudio"

    @staticmethod
    def extract_thinking_trace(content: str) -> Tuple[str, Optional[str]]:
        """Extract clean response content and isolated thinking trace from <think>...</think> tags."""
        think_match = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL)
        if think_match:
            thought = think_match.group(1).strip()
            clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return clean, thought
        return content, None

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Generate response and decouple reasoning traces into thought metadata."""
        resp = await super().generate(messages=messages, tools=tools, **kwargs)
        clean, thought = self.extract_thinking_trace(resp.content or "")
        if thought is not None:
            resp.thought = thought
            resp.content = clean
        return resp

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Stream chunks with transparent pass-through."""
        async for chunk in super().stream(messages=messages, tools=tools, **kwargs):
            yield chunk

    async def discover_models(self) -> List[Dict[str, Any]]:
        """Query LM Studio local models discovery endpoint."""
        http_client = self._get_http_client()
        if http_client is not None:
            base_endpoint = resolve_lmstudio_base_url(self.config)
            clean_base = str(base_endpoint).replace("/v1", "").rstrip("/")
            endpoints = [
                f"{clean_base}/api/v1/models",
                f"{clean_base}/api/v0/models",
                "/v1/models",
                "/models",
            ]
            for ep in endpoints:
                try:
                    resp = await http_client.get(ep)
                    if resp.status_code == 200:
                        data = resp.json()
                        return data.get("models") or data.get("data") or []
                except Exception:
                    continue
        return []


__all__ = [
    "LMStudioProvider",
    "DEFAULT_LMSTUDIO_BASE_URL",
    "resolve_lmstudio_base_url",
]
