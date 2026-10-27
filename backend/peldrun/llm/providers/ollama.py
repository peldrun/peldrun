"""
backend/peldrun/llm/providers/ollama.py

PELDRUN Core Ollama LLM Provider.
Specialized provider for Ollama local service endpoints.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

from peldrun.llm.client import AsyncLLMClient, LLMConfig
from peldrun.llm.providers.openai_compat import OpenAICompatProvider

# Documented default endpoint for Ollama.
# Override via the OLLAMA_HOST environment variable (Ollama's canonical
# configuration variable) or [llm.providers.ollama].base_url in config.toml.
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


def resolve_ollama_base_url(config: Optional[LLMConfig]) -> str:
    """Resolve the Ollama base URL.

    Precedence:
        1. OLLAMA_HOST environment variable (Ollama's canonical setting)
        2. config.api_base (loaded from config.toml or caller-supplied)
        3. DEFAULT_OLLAMA_BASE_URL
    """
    env_host = os.environ.get("OLLAMA_HOST")
    if env_host:
        host = env_host.strip()
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host.rstrip("/")
    if config is not None:
        api_base = getattr(config, "api_base", None)
        if api_base:
            return str(api_base).rstrip("/")
    return DEFAULT_OLLAMA_BASE_URL


def _apply_ollama_env_override(config: Optional[LLMConfig]) -> Optional[LLMConfig]:
    """Apply OLLAMA_HOST override to the LLMConfig when present.

    Ensures all inference calls routed through the parent provider (which reads
    config.api_base) honor OLLAMA_HOST, not only discovery and health checks.
    """
    env_host = os.environ.get("OLLAMA_HOST")
    if not env_host or config is None or not hasattr(config, "api_base"):
        return config
    host = env_host.strip()
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    host = host.rstrip("/")
    if hasattr(config, "model_copy"):
        return config.model_copy(update={"api_base": host})
    config.api_base = host
    return config


def _join_path(base: str, suffix: str) -> str:
    """Join a base URL with a path suffix, avoiding duplicate slashes."""
    return f"{base.rstrip('/')}/{suffix.lstrip('/')}"


class OllamaProvider(OpenAICompatProvider):
    """Ollama local provider targeting Ollama OpenAI-compatible routes."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        client: Optional[AsyncLLMClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        **kwargs: Any,
    ) -> None:
        config = _apply_ollama_env_override(config)
        super().__init__(config=config, client=client, transport=transport, **kwargs)
        self.name = "ollama"

    async def fetch_installed_models(self) -> List[str]:
        """Retrieve list of installed model names from Ollama endpoint."""
        http_client = self._get_http_client()
        if http_client is None:
            return []

        base_url = resolve_ollama_base_url(self.config)
        native_base = base_url.replace("/v1", "").rstrip("/")
        endpoints = [
            "/models",
            "/v1/models",
            "models",
            _join_path(base_url, "models"),
            _join_path(native_base, "api/tags"),
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
        if http_client is None:
            return False

        base_url = resolve_ollama_base_url(self.config)
        native_base = base_url.replace("/v1", "").rstrip("/")
        endpoints = [
            "/models",
            "/v1/models",
            "models",
            _join_path(native_base, "api/version"),
        ]
        for ep in endpoints:
            try:
                resp = await http_client.get(ep)
                if resp.status_code == 200:
                    return True
            except Exception:
                continue
        return False


__all__ = [
    "OllamaProvider",
    "DEFAULT_OLLAMA_BASE_URL",
    "resolve_ollama_base_url",
]
