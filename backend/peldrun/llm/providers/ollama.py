"""
PELDRUN Core Ollama LLM Provider.
Specialized provider for Ollama local service endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import httpx

from peldrun.llm.client import AsyncLLMClient, LLMConfig
from peldrun.llm.providers.openai_compat import OpenAICompatProvider


class OllamaProvider(OpenAICompatProvider):
    """Ollama local provider targeting Ollama OpenAI-compatible routes."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        client: Optional[AsyncLLMClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config=config, client=client, transport=transport, **kwargs)
        self.name = "ollama"

    async def fetch_installed_models(self) -> List[str]:
        """Retrieve list of installed model names from Ollama endpoint."""
        http_client = self._get_http_client()
        endpoints = [
            "/models",
            "/v1/models",
            "models",
            f"{str(self.config.api_base).rstrip('/')}/models",
            f"{str(self.config.api_base).replace('/v1', '').rstrip('/')}/api/tags",
        ]
        for ep in endpoints:
            try:
                resp = await http_client.get(ep)
                if resp.status_code == 200:
                    payload = resp.json()
                    data = payload.get("data") or payload.get("models") or []
                    results: List[str] = []
                    for m in data:
                        if isinstance(m, dict):
                            val = m.get("id") or m.get("name") or m.get("model")
                            if val:
                                results.append(str(val))
                        elif isinstance(m, str):
                            results.append(m)
                    if results:
                        return results
            except Exception:
                continue
        return []

    async def check_health(self) -> bool:
        """Verify Ollama service health and reachability."""
        if hasattr(self.client, "test_connection"):
            try:
                if await self.client.test_connection():
                    return True
            except Exception:
                pass

        http_client = self._get_http_client()
        endpoints = [
            "/models",
            "/v1/models",
            "models",
            f"{str(self.config.api_base).replace('/v1', '').rstrip('/')}/api/version",
        ]
        for ep in endpoints:
            try:
                resp = await http_client.get(ep)
                if resp.status_code == 200:
                    return True
            except Exception:
                continue
        return False


__all__ = ["OllamaProvider"]