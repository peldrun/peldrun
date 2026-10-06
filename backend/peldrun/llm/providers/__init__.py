"""
PELDRUN Core LLM Providers Subsystem.
Exports base LLM provider interface, provider adapters, and dynamic provider factory.
"""

from typing import Any, Optional

from peldrun.llm.client import LLMConfig
from peldrun.llm.providers.lmstudio import LMStudioProvider
from peldrun.llm.providers.ollama import OllamaProvider
from peldrun.llm.providers.openai_compat import BaseLLMProvider, OpenAICompatProvider


def get_provider(
    provider_name: str = "openai_compat",
    config: Optional[LLMConfig] = None,
    **kwargs: Any,
) -> BaseLLMProvider:
    """
    Factory function to resolve and instantiate an LLM provider adapter by identifier.
    Supports 'openai_compat', 'lmstudio', and 'ollama'.
    """
    key = provider_name.lower().strip()
    if key in ("lmstudio", "lm_studio", "lm-studio"):
        return LMStudioProvider(**kwargs)
    elif key == "ollama":
        return OllamaProvider(**kwargs)
    elif key in ("openai", "openai_compat", "default"):
        return OpenAICompatProvider(config=config, **kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider identifier: '{provider_name}'")


__all__ = [
    "BaseLLMProvider",
    "OpenAICompatProvider",
    "LMStudioProvider",
    "OllamaProvider",
    "get_provider",
]