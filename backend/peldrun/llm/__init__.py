"""
PELDRUN Core LLM Module.
Exports universal client contracts, configuration models, and response structures.
"""

from peldrun.llm.client import (
    AsyncLLMClient,
    DeltaToolCall,
    LLMConfig,
    LLMResponse,
    StreamChunk,
    ToolCall,
    sanitize_tools_for_openai,
)
from peldrun.llm.providers.openai_compat import BaseLLMProvider, OpenAICompatProvider

__all__ = [
    "AsyncLLMClient",
    "BaseLLMProvider",
    "DeltaToolCall",
    "LLMConfig",
    "LLMResponse",
    "OpenAICompatProvider",
    "StreamChunk",
    "ToolCall",
    "sanitize_tools_for_openai",
]